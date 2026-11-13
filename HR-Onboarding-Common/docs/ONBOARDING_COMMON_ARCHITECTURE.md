# HR Onboarding Orchestrator — Architecture

## 1. What this is

`hr_onboarding_common.orchestration` drives the five independently-built UC agents (UC01
Employee Record, UC02 Onboarding Tasks, UC03 Document Validation, UC04 Equipment/Access, UC05
Orientation Scheduling) through a single resumable, approval-aware LangGraph workflow, calling
each agent's real public interface. UC01–UC04 source and configuration are not modified (their
owners maintain them); each UC remains independently runnable and independently tested. UC05 is
maintained together with this orchestration: its `tests/test_orchestration_contract.py` pins what
the orchestrator relies on, and the UC05 changes made for the orchestrated lifecycle (a configured
`CLIENT-TEST-A` tenant, typed configuration errors, decision validation before recording,
authorized welcome redelivery) are listed in `docs/UC05_REMAINING_GAPS_COMPLETION_REPORT.md`.

`hr_onboarding_common.agent.BaseAgent` (unchanged) remains the generic harness every agent,
including the orchestrator itself, subclasses. `OnboardingOrchestratorAgent` lives in the
`orchestration/` subpackage — not in `agent.py` — because it is the only part of the harness that
needs to know about the five UC agents by name, their module-name collision, and their five
different contracts; `agent.py` re-exports it lazily (`from hr_onboarding_common.agent import
OnboardingOrchestratorAgent` works, without `agent.py` importing LangGraph or any UC-specific
plumbing at module load time).

> **The workflow is state-driven (orchestrator 0.3.0).** Document collection, the wait for
> documents, UC03-driven routing, UC02 task planning as the sequential prerequisite, the parallel
> fan-out to UC04 and UC05, the merge and joining-date reconciliation are described in
> [STATE_DRIVEN_ONBOARDING_WORKFLOW.md](STATE_DRIVEN_ONBOARDING_WORKFLOW.md),
> which supersedes §2 below where they differ. The UC05 sections (§3, §7, §8) still apply as written.

| Module | Responsibility |
|---|---|
| `orchestration/orchestrator_agent.py` | The graph, routing table, stage and branch wrappers (history, errors, write-through), case operations |
| `orchestration/state.py` | `OnboardingCaseState`, `Stage`, `StageStatus`, `WorkflowStatus`, `Branch`, status derivation |
| `orchestration/workflow.py`, `workflow_models.py` | Branch fold/merge, pending actions, derived view; the pydantic contract of the state |
| `orchestration/adapters/uc02_document_collection.py`, `notifications.py` | The orchestrator's document-request stage (not UC02 task management; never calls UC02) and its mock notification port |
| `orchestration/case_store.py` | Last-known case state between calls: in-memory (default) or SQLite |
| `orchestration/agent_contract.py`, `execution.py`, `dependencies.py`, `state_sync.py` | The standard UC contract every stage runs through, the dependency registry, field ownership -- see the workspace `docs/UC_INTEGRATION_CONTRACTS.md`, `docs/ORCHESTRATION_WORKFLOW.md`, `docs/SHARED_STATE_AND_CONTEXT.md` |
| `orchestration/adapters/uc0X_*.py` | One adapter per UC: translation, validation, routing verdicts — no UC business rules |
| `orchestration/workers/` | One persistent worker process per colliding UC (UC01/UC04/UC05) |
| `orchestration/api.py` | The orchestrator's HTTP entry points |
| `orchestration/tenant_config.py` + `config/tenants.yaml` | Per-tenant timezone, joining-day run time, employee identity scheme (§3.3) |
| `orchestration/joining_day_scheduler.py` | Joining-day scheduler for orchestrated cases, with a persisted job store (§10.1) |
| `guardrails/policy.yaml` | The orchestrator's own policy envelope: two gates, role allowlists, kill switch |

## 2. The workflow

### 2.1 Lifecycle sequence

```
UC01 intake ─► UC02 document collection (op A) ─► [wait: documents] ─► UC03 document validation
   ─► ┬ UC02 task planning (op B)                                              ┐
      ├ UC04 equipment & access                                                ├► merge ─► evaluate
      └ UC05 orientation (induction) ─► UC05 buddy assignment ─► UC05 team intro ┘
   ─► day-1 readiness (HR signoff) ─► [joining-day event] ─► UC05 joining-day welcome ─► complete
                         UC05 buddy introduction: optional, HR-requested, never blocks
```

The three post-verification branches run in parallel; invalid or incomplete documents park the
case for correction and UC03 re-validates once corrected documents arrive (details, routing and
statuses: STATE_DRIVEN_ONBOARDING_WORKFLOW.md). UC01's intake opens the case and checks offer/form
consistency; the only document validation is UC03's, run per document revision.

**The UC05 order is UC05's own** (its `agent.py` graph edges), not a choice made here:
an *approved* induction makes UC05 propose a buddy (`apply_meeting_decision → assign_buddy`); an
*approved* buddy makes UC05 draft the team introduction (`apply_buddy_decision →
prepare_team_intro`). Team and buddy introductions are therefore **not** before orientation. The
buddy introduction is not in UC05's automatic chain at all: UC05 offers it only as an HR-console
operation with an HR-chosen start (`schedule_buddy_intro`, which itself requires an assigned
buddy), so it is optional here too and never holds up readiness.

**The welcome follows readiness signoff.** UC05 drafts the welcome only on its `joining_day`
trigger, keyed by the case's start date. Here it runs after HR signs off day-1 readiness (so no
employee is welcomed whose prerequisites are unverified) and only once the joining-day event has
arrived; until then the stage is `WAITING`, which is not an error. The case is `complete` after the
welcome is delivered.

### 2.2 The compiled graph

The compiled graph (generated with `agent.graph.get_graph().draw_mermaid()`) is reproduced in
STATE_DRIVEN_ONBOARDING_WORKFLOW.md §1: sequential stages up to UC03 with conditional validation
routing (`record_verification` | `await_document_correction` | `await_document_review` |
`await_validation_retry`), then `plan_tasks` (UC02) as a sequential prerequisite
(`route_after_plan_tasks`: `dispatch_branches` | `await_plan_tasks`), a two-way fan-out from
`dispatch_branches` once `plan_tasks` has completed, a join at `merge_outcomes`, then
`evaluate_overall_status` -> day-1 readiness -> joining-day welcome. See
STATE_DRIVEN_ONBOARDING_WORKFLOW.md §4 for why `plan_tasks` is a barrier, not a branch, how its
own DATE_CHANGE gate feeds the joining date both branches read, and how results produced for an
earlier joining date are reconciled before day-1 signoff.

Every `await_*` node ends the turn (checkpointed). Resuming re-invokes the graph with the case's
last-known state: completed stages are skipped, the same routing function re-evaluates the
(possibly now changed) real condition, and the case proceeds or parks again — no polling loop
inside the graph.

### 2.3 Routing table

Every sequential stage routes the same way: **only `COMPLETE` proceeds**; every other status parks
the case at that stage's `await_*` node (validation distinguishes correction, HR review and
technical retry; a parked branch stage ends its branch and the case parks at `await_branches` after
the merge) (`route_after_*`, pure and status-driven — never free text;
`ROUTING_TABLE` in `orchestrator_agent.py`). What each situation produces:

| Situation | Stage status | Next | Example codes (`stage_errors[stage].code`) |
|---|---|---|---|
| Successful stage completion | `complete` | next stage; skipped on every later resume | — |
| Missing or invalid input | `blocked` (retryable) | park; resume after correction | `MISSING_DATA`, `INTAKE_INPUT_UNAVAILABLE`, `UC05_OFFER_EVENT_MISSING`, `EMPLOYEE_ID_MISSING`/`_MALFORMED`/`_AMBIGUOUS`/`_CONFLICT`/`_UNRESOLVED`, `IDENTITY_MISMATCH`, `JOINING_DATE_MISMATCH`, `INCOMPLETE_EMPLOYEE_DATA`, `EMPLOYEE_NOT_IN_UC05_DIRECTORY` |
| Pending approval | `awaiting_approval` | park; the approval is in `pending_approvals` | — |
| Rejected approval | `blocked` | park; HR re-requests the step (operation) or resumes | `INDUCTION_REJECTED`, `BUDDY_REJECTED`, `TEAM_INTRO_REJECTED`, `WELCOME_REJECTED`, `DAY_ONE_SIGNOFF_REJECTED` |
| Scheduling conflict | `blocked` (retryable) | park; resume once calendars are free, or reschedule with an HR-chosen start | `NO_AVAILABLE_SLOT`, `SCHEDULING_CONFLICT`, `INVALID_MEETING_TIME` |
| Retryable failure | `blocked` (retryable) | park; resume (UC05 resumes its run at the failed step) or retry-apply a decision | `UC05_PROVIDER_UNAVAILABLE`, `UC05_UNAVAILABLE`, `DECISION_NOT_APPLIED`, `NO_BUDDY_CANDIDATES`, `UC05_MISSING_DATA`, `UC05_CLIENT_CONFIG_MISSING`, `UC05_CLIENT_CONFIG_INVALID` |
| Terminal failure | `failed` | park; overall `failed` | `UC02_PACKAGE_UNIMPORTABLE`, `UC05_CASE_INACTIVE`, `UC05_CONTRACT_VIOLATION`, `UC05_INVALID_INPUT` |
| Decision refused before it is recorded (§8) | unchanged (`awaiting_approval`) | the request fails (403/409/422); the approval stays pending for the right person | — |
| Decision UC05 refused to apply | `blocked`, not retryable | park; HR re-requests the step. Only for a decision recorded without pre-validation (directly in UC05's inbox) | `UNAUTHORIZED_APPROVER`, `BUDDY_INELIGIBLE` |
| Uncertain outcome | `uncertain` | park; nothing is written again until a fresh read is conclusive; overall `at_risk` | `UNCERTAIN_OUTCOME`, `INDUCTION_BOOKING_UNCERTAIN`, `WELCOME_DELIVERY_UNCERTAIN` |
| Normal wait | `waiting` | park at `await_joining_day`; overall `in_progress` | — |
| Resume after correction | (re-derived) | the parked stage re-runs; it re-reads the UC first | — |
| Already-completed stage | `complete` | skipped (`_skip_if_complete`); its UC is not called | — |

A failed prerequisite prevents what depends on it: a sequential stage is only reachable through its
predecessor's `COMPLETE` edge, no branch starts before UC03 verifies the documents and UC02's
`plan_tasks` completes, the UC05 steps follow each other inside their branch, and each UC05 node
re-checks its own prerequisite (`PREREQUISITE_NOT_MET` otherwise). The two parallel branches (UC04,
UC05) do not depend on each other.

## 3. UC05 integration

### 3.1 Adapter responsibilities (`orchestration/adapters/uc05_orientation.py`)

The adapter translates, validates and routes; it contains no UC05 business rule. Every node:

1. **Reads before it writes.** It takes a read-only `case_snapshot` of UC05's records and derives
   the stage from them; it calls a writing UC05 operation only when the step has not happened.
2. **Never writes into an uncertain stage** until a fresh snapshot is conclusive (no unsettled
   ledger operation). If UC05 no longer holds records it had (a worker restarted without
   persistence), the stage stays `UNCERTAIN` — the event is not resent.
3. **Validates UC05's results at the boundary** (`Uc05Snapshot`, `Uc05Preflight`,
   `Uc05DecisionOutcome` — pydantic, with UC05's exact status vocabularies). An undocumented value
   fails the stage with `UC05_CONTRACT_VIOLATION`; nothing is guessed.
4. **Reports an interrupted UC05 run with UC05's own reason.** `BaseAgent.run` turns an error into
   a response exception and leaves the run checkpointed on the failed node (`pending_nodes`); the
   adapter reports `UC05_<code>` and resuming redelivers the event, which UC05 resumes at that
   node. A tenant whose welcome configuration is missing or unusable is a typed UC05 stage
   (`welcome_not_configured`), reported as `UC05_CLIENT_CONFIG_MISSING` / `UC05_CLIENT_CONFIG_INVALID`
   (retryable once the configuration is fixed), never a raw `KeyError`.
5. **Threads the verified actor through unchanged** and refuses an actor of another tenant before
   UC05 is called; UC05's registry checks the tenant again on every record.

| Capability | Orchestrator stage | UC05 entry point (via worker op) | Approval (UC05 gate, approver) |
|---|---|---|---|
| Orientation scheduling | `orientation_scheduling` | `preflight_offer_event` (read: EmailPort + EmployeeDirectoryPort, `REQUIRED_DIRECTORY_FIELDS`), then `OnboardingAgent.run_event(offer_accepted)` | `scheduling_approval`, hr_admin |
| Buddy assignment | `buddy_assignment` | proposed by UC05 inside the induction decision's run; else `OnboardingToolRegistry.propose_buddy_assignment` (+ `notify_approver`) | `buddy_assignment`, hr_admin |
| Team introduction | `team_introduction` | drafted by UC05 inside the buddy decision's run; HR reschedule: `OnboardingToolRegistry.schedule_session(team_intro)` | `scheduling_approval`, the team lead on record |
| Buddy introduction (optional) | `buddy_introduction` | `OnboardingToolRegistry.schedule_buddy_intro` (HR-chosen start) | `scheduling_approval`, the buddy on record |
| Joining-day welcome | `joining_day_welcome` | `OnboardingAgent.run_event(joining_day, as_of=start date)`; redraft: `run_event(draft_welcome)` | `comms_approval`, hr_admin; delivered by UC05 through its ledger |
| Decisions | the approval's stage | `OnboardingToolRegistry.validate_decision` (tenant, the specific team lead/buddy, applicability, edited payload), then `HITLInbox.decide` with the approver's identity; UC05's listener `handle_approval_decision` applies it | — |
| Recovery | the approval's stage | `HITLInbox.retry_apply` (approver role or hr_admin, as UC05's API requires) | — |
| Uncertain operations | the operation's stage | `OnboardingToolRegistry.resolve_operation` (hr_admin, enforced by UC05; resolver, time and reason kept on the operation) | — |
| Welcome delivery recovery | `joining_day_welcome` | `resolve_welcome_delivery` → `resolve_operation`; `retry_welcome_delivery` → `OnboardingToolRegistry.redeliver_welcome` (§7) | — |
| RSVP / attendance | not a stage (independent event) | `run_event(participant_responded)`; a decline opens UC05's `attendee_declined` decision | `attendance_decision`, hr_admin |
| HR Policies Q&A | not a stage (unchanged, employee-facing) | `run_event(policy_question)` | — |

**Identity and joining-date cross-check.** UC05 schedules for the employee its own directory (the
future HRMS) resolves from the offer-acceptance event. Before any UC05 write, the preflight
compares that employee and joining date with the case's (`employee_id` from UC01,
`joining_date`); a mismatch blocks with nothing scheduled. The event reference is the explicit
input `offer_acceptance_event_id` — the case id is never substituted for it.

### 3.2 Worker invocation contract (UC05)

`orchestration/workers/uc_worker_runtime.py` (`_Uc05Handler`) builds UC05 through **UC05's own
`sub_agent.runtime.build_context`** — the wiring its API and joining-day scheduler use: the LLM that
`LLM_MODE` selects (offline: UC05's offline LLM with its welcome-note and policy-Q&A handlers; online:
the common `LiteLLMGateway`, e.g. OpenRouter routes, validated at startup — see
[OPENROUTER_INTEGRATION_GUIDE.md](OPENROUTER_INTEGRATION_GUIDE.md)), its simulated connectors, and its
HITL inbox with `handle_approval_decision` subscribed exactly as UC05's `api.on_decision` does.

| Worker env var | Effect inside the UC05 process |
|---|---|
| `UC05_WORKER_DATA_DIR` | `HR_AGENT_PERSIST=1`, `HR_AGENT_DATA_DIR=<dir>`: UC05's SQLite repository + ledger (`onboarding.db`), checkpoints (`checkpoints.db`), HITL (`hitl.db`), audit |
| `UC05_WORKER_HR_AGENT_ENV` | default `development` (UC05 refuses `production` with simulated connectors — fail closed) |
| `UC05_WORKER_LLM_MODE` | overrides `LLM_MODE` for the UC05 worker only; unset: the orchestrator's `LLM_MODE` (default `offline`) |
| `UC0X_WORKER_LLM_ACCESS` | read by the orchestrator when it starts the worker (`load_worker_config`): whether that process receives the provider credentials (default `1` for UC05, `0` for UC01/UC04 — they make no model calls) |
| `HR_ORCH_WORKER_TEST_HOOKS=1` | **tests only**: enables `__test_hooks__`, which touches only UC05's simulated connectors |

Wire protocol: one JSON line per request and response. stdout carries only the protocol
(`sys.stdout` is redirected to stderr inside the worker); stderr goes to a log file, never an
undrained pipe. Failures are classified — `RETRYABLE_PROVIDER_ERROR | POLICY_DENIED | NOT_FOUND |
INVALID_INPUT | CONFLICT | INTERNAL_ERROR`, plus `error_type` and a short message — and never carry
a traceback. A call that gets no reply within the timeout raises `WorkerTimeoutError` and **kills
the worker** (a late reply must never be read as the answer to the next request); a closed pipe
raises `WorkerCrashedError`. For a write, both mean "outcome unknown" → `UNCERTAIN`.

`decide_approval` in the worker validates before it records: the item must still be the proposal
type/record the case expects (`CONFLICT` otherwise — no stale or replayed relay), then UC05's
`registry.validate_decision` runs (`POLICY_DENIED`, `INVALID_INPUT`, `CONFLICT` for a booking no
longer pending), and only then `HITLInbox.decide`. `redeliver_welcome` and `__test_hooks__`
(`remove_client_config`, `sweep_timeouts`, `clear_rejections`; tests only) were added.

### 3.3 Canonical employee identity and tenant configuration

UC01 issues an `employee_no`; UC05 keys everything by its directory's `employee_id`. Every UC05 call
uses the case's **`employee_ref`** (`{client_id, employee_id, employee_no, source, scheme}`),
resolved by `TenantConfig.resolve_employee` from `config/tenants.yaml` (or the file in
`ORCHESTRATOR_TENANT_CONFIG`), tenant by tenant:

| `employee_identity.scheme` | `employee_id` sent to UC05 | `source` |
|---|---|---|
| `shared` (default) | UC01's `employee_no` as is — the two systems share the HRMS id | `shared_scheme` |
| `mapped` | the entry in the tenant's explicit `employee_no_map`; an unlisted number is unresolved | `mapped` |
| `none` | only the caller-supplied `employee_id` (start body / `OnboardingCaseInput.employee_id`) | `explicit` |

A caller-supplied `employee_id` always wins, but must agree with the upstream number when both
exist. Failures block the UC05 stage (retryable after a correction) with a stable code and nothing
written to UC05: `EMPLOYEE_ID_MISSING`, `EMPLOYEE_ID_MALFORMED` (not
`^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`), `EMPLOYEE_ID_AMBIGUOUS` (one number to several ids, or one
id from several numbers), `EMPLOYEE_ID_CONFLICT`, `EMPLOYEE_ID_UNRESOLVED`. Once UC05 holds a case
for the employee the stored `employee_ref` is kept: a later configuration change cannot switch UC05
to a different person mid-lifecycle. The preflight's cross-check against UC05's own directory
(above) still runs on the resolved id.

The same file holds each tenant's `timezone` (a case can carry its own `timezone`, validated as an
IANA zone) and `joining_day_run_at` (local `HH:MM` from which the joining day is due). An unknown
tenant gets `default`. UC05's own welcome configuration (languages, branding, templates) is UC05
data (`ClientConfig`), not this file; `CLIENT-TEST-A` is seeded in UC05's synthetic data, and
`CLIENT-TEST-B` is deliberately left unconfigured to exercise the missing-configuration path.

## 4. The module-name collision, and why workers (not `sys.modules` swapping)

**Verified fact:** UC01, UC04, and UC05 each install a top-level Python module literally named
`sub_agent`. Only one can be bound in `sys.modules["sub_agent"]` per interpreter — they cannot be
`pip install`ed together.

**Chosen design: one persistent worker process per colliding UC, never a pool.**

- `orchestration/workers/uc_worker_runtime.py` is stdlib-only at module level and is launched
  under *that UC's own interpreter*, as a bare script path.
- **Exactly one worker process per colliding UC.** UC01/UC04 hold their business data in memory
  (UC05 too, unless `UC05_WORKER_DATA_DIR` is set); one worker means every request for that UC
  reaches the same store.
- `orchestration/workers/uc_worker_client.py` serializes every request to a worker through one
  `threading.Lock`; no OS process ever imports more than one of {UC01, UC04, UC05}.
- UC03's and UC04's own code imports a sibling package next to `src/` that their wheels do not
  bundle; the worker runtime and the in-process UC03 adapter insert the repo root onto `sys.path`,
  as their own test suites do.

**Worker interpreter configuration** (`workers/config.py`): `worker_config.yaml`, then
`UC0X_WORKER_PYTHON`/`UC0X_WORKER_SRC_DIR`, then — as a convenience default only — the UC's own
`.venv`. Nothing resolves → `WorkerConfigurationError`; the orchestrator's own interpreter is never
substituted. The startup identity check proves the entry point was loaded from the configured
`src_dir`.

## 5. State

`OnboardingCaseState` (`orchestration/state.py`) is a projection — ids, statuses, actionable
reasons — never raw documents, message bodies, names or contact details beyond intake. All fields
added for this integration are optional and back-filled (`ensure_lifecycle_fields`), so a state
persisted by the previous version still loads.

| Field | Holds |
|---|---|
| `stage_statuses` | `Stage` → `StageStatus` for every stage |
| `orientation_schedule_result` | UC05 case reference (`uc05_case`, `uc05_employee_id`, `uc05_start_date`), the offer event's outcome, induction `session_id`/`session_status`/`scheduled_start`, `approval_id`, booking `operation_key` |
| `buddy_assignment_result` | `buddy_id` or `proposed_buddy_id`, `approval_id`, `status` |
| `team_introduction_result`, `buddy_introduction_result` | session id/status/start, approval id/status, `approver_role`, booking `operation_key`, `last_request` (an HR operation's outcome) |
| `welcome_message_result` | `draft_id`, `draft_status`, `language`, `approval_id`, `operation_key`/`operation_status`, `delivery` (§7), `delivery_resolution` (who resolved, when, why), `delivery_reviews` (operator reviews that kept it unresolved) |
| `employee_ref` | the canonical employee identity UC05 is called with (§3.3) |
| `case_timezone` | the employee's IANA timezone when supplied at start (else the tenant's) |
| `participant_responses` | each RSVP forwarded to UC05: session, participant, response, outcome, time, actor |
| `joining_day_event` | `as_of` (the start date used), `received_on`, `received_at`, `triggered_by` |
| `pending_approvals` | every open approval — the orchestrator's own gates and each UC's — with `agent`, `reference`, `role`, `stage`, `gate`, and for UC05 `proposal_type`/`record_id` (what a relayed decision must still match) |
| `approval_decisions` | each relayed decision: approver, decision, `applied`, `apply_error_type`, the UC05 run's stage/meeting type/error types |
| `gate_rejections` | the orchestrator's own rejected gates; cleared only by an explicit resume |
| `stage_errors` | stage → `{code, detail, retryable}`; cleared when the stage completes |
| `stage_history` | status transitions with time and code (appended only when a status changes) |
| `stage_attempts` | how often each stage node actually ran (skipped runs do not count) |
| `blockers`, `next_action`, `error_code`/`error_detail` | what is holding the case and who acts next; `error_*` is the last stage's error |
| `idempotency_keys` | stage → stable key sent with every worker call |
| `workflow_status`, `branch_statuses`, `pending_actions`, `completed_stages`, `document_collection`, `submitted_documents`, `validation_issues`, `correction_requirements`, `verified_context`, `stage_outcomes`, `retry_metadata`, `created_at`/`updated_at` | the state-driven workflow's fields -- STATE_DRIVEN_ONBOARDING_WORKFLOW.md §5 |

`StageStatus`: `pending → in_progress → complete`, or `awaiting_approval`, `blocked` (resumable),
`waiting` (normal wait), `uncertain` (no writes until reconciled), `failed` (terminal),
`not_requested` (optional stage). Overall status is derived from stage statuses only: any
`failed` → `failed`; any `uncertain` → `at_risk`; parked on `waiting` → `in_progress`; otherwise
parked → `blocked`; `complete` only after the welcome.

**Minimisation.** `id_number`, `candidate_email`, the form and a raw acceptance email are
**intake-only inputs**: they are held in process memory (`deps.intake_inputs`, keyed by the case's
original id), merged into the payload of the UC01 call only, and never placed in graph state, a
LangGraph checkpoint, the case store or a response; the stage wrapper strips them from every state
it returns and drops the in-memory copy once intake completes. If the process restarts before intake
completes, the inputs are gone by design: the intake stage blocks with `INTAKE_INPUT_UNAVAILABLE`
and the start request is resubmitted (idempotent per offer reference). A state persisted by the
previous version that still carries them in `intake_trigger` keeps working and loses them at its
next stage run. Verified on the stored bytes of the SQLite checkpoint and case store
(`test_rsvp_and_sensitive_data.py`).

## 6. Durability: three layers kept distinct

| Layer | What it covers | What persists it | A restart means |
|---|---|---|---|
| Orchestrator case state | `OnboardingCaseState`, written through after every stage node | `CaseStateStore`: in memory (default) or `SqliteCaseStateStore` (`build_deps(persist_dir=…)`) | With SQLite, a new orchestrator instance resumes the same case (`test_orchestrator_and_uc05_restart_resume_the_same_case_without_duplicates`) |
| Orchestrator graph checkpoints / own gates | Every graph step; `intake_mismatch_review`, `day_one_signoff` items | `MemorySaver` / `SqliteSaver`; HITL in memory / `hitl.db` (`persist_dir`) | Survive with `persist_dir` |
| Each UC's business data | Cases, forms, tasks, documents, tickets, sessions, approvals | UC01/02/03/04: in memory. UC05: in memory, or its own SQLite stores with `UC05_WORKER_DATA_DIR` | UC05 with persistence keeps its case, sessions and approvals; UC01–UC04 lose theirs (their own prototype state, not changed here) |
| External side effects | Calendar holds, messages, tickets | Each UC's idempotency (UC05's `OperationRecord` ledger) | UC05's ledger survives with persistence; the simulated calendar/notifier do not (they are test doubles) |
| Joining-day jobs | One job per (case, joining date): status, attempts, lease, outcome | `InMemoryJoiningDayJobStore` / `SqliteJoiningDayJobStore` (`joining_day_jobs.db`) | DONE jobs never re-run; an expired lease is reclaimed; FAILED jobs retry up to `max_attempts` |
| Intake-only inputs | `id_number`, email, form | Process memory only (§5) | Lost on purpose; intake blocks with `INTAKE_INPUT_UNAVAILABLE` until resubmitted |

Resume does not depend on in-memory `case_states`: with `SqliteCaseStateStore` a new instance
continues a case (`test_a_new_orchestrator_instance_continues_a_case_from_its_sqlite_case_store`).
A case is addressable by its original id too (the derived `CASE-…` id is stored as an alias of
UC01's case id). Operations on one case are serialized within a process (striped locks); several
orchestrator processes on one case need a shared lock this store does not provide.

**Neither store makes an external operation exactly-once.** LangGraph checkpoints persist the
orchestrator's projection only; UC05's ledger gives at-most-once delivery when the provider
honours idempotency keys, and otherwise an `UNCERTAIN` outcome for a person to resolve.

## 7. Idempotency and uncertain outcomes

| Mechanism | Guarantees |
|---|---|
| `_skip_if_complete` | A `COMPLETE` stage's UC is never called again on resume |
| Read-before-write (UC05 nodes) | A step UC05 already did is observed, not repeated |
| UC05's own idempotency | Offer events by event id (redelivery resumes or reports a duplicate), buddy proposals while one is pending, one welcome draft per employee per template, bookings and messages through its ledger |
| Gate dedupe | A repeated resume never raises a second `day_one_signoff`/`intake_mismatch_review` request |
| Idempotent start | A repeated start for a case past intake resumes that case (a duplicate offer acceptance) |
| `pending_approvals`/`blockers` | Cleared per stage before it re-runs and de-duplicated: they show exactly what is true now |

Uncertain outcomes, tested separately:
1. **Worker crash or timeout during a write** (`test_worker_crash_mid_decision_is_uncertain…`):
   the stage becomes `UNCERTAIN`; after a restart without persistence UC05's records are gone and
   the stage stays `UNCERTAIN` — the offer event is not resent.
2. **Provider accepted, response lost, provider cannot deduplicate**
   (`test_uncertain_welcome_delivery_is_never_resent_and_hr_resolves_it`): UC05's ledger records
   `uncertain`; retry-apply does not resend; HR checks and resolves the operation and the stage
   completes with `delivery: confirmed_by_operator` (not "delivered": no provider receipt exists).
3. **Provider outage before delivery** (`test_provider_outage_…delivered_once`): `UNCERTAIN` (UC05
   cannot know), retry-apply resends under the same idempotency key, delivered once.
4. **Operator establishes it was not delivered** (`test_welcome_recovery.py`): resolved
   `not_delivered`, then an explicitly authorized `retry_welcome_delivery` sends the approved content
   once more under a fresh ledger operation.

**Welcome delivery states** (`welcome_message_result.delivery`, derived only from UC05's draft and
ledger — never assumed):

| `delivery` | Meaning | Stage | Next action |
|---|---|---|---|
| `not_attempted` | approved, no delivery operation yet | per UC05's run | — |
| `delivered` | the draft is `sent` / the ledger holds a provider-accepted operation | `complete` | — |
| `confirmed_by_operator` | an operator confirmed delivery after an unknown outcome (`resolved_by` set) | `complete` | — |
| `uncertain` | the ledger operation is `pending`/`uncertain` | `uncertain` (`WELCOME_DELIVERY_UNCERTAIN`) | `resolve_welcome_delivery` |
| `failed_before_delivery` | the provider refused it permanently (`rejected`) | `blocked` (`WELCOME_DELIVERY_REJECTED`) | fix the cause, `retry_welcome_delivery` |
| `not_delivered` | an operator confirmed it was not delivered (`released`) | `blocked` (`WELCOME_NOT_DELIVERED`) | `retry_welcome_delivery` |

Operator procedure (orchestrator `orientation/operations`, `uc05_operation_roles` + hr_admin in UC05):
`resolve_welcome_delivery` with `resolution` = `delivered` | `not_delivered` | `keep_unresolved`
and a required `reason` (the evidence checked); only a `pending`/`uncertain` operation can be
resolved; `keep_unresolved` records the review and changes nothing. `retry_welcome_delivery` with a
required `reason` is accepted only when the draft is approved and the operation is absent,
`released` or `rejected`; it is refused while the outcome is unknown and once delivered. Every
resolution and retry is audited on both sides (orchestrator `orchestrator.uc05_operation`, UC05
`welcome.redelivery_authorized` / the operation's `resolved_by`, `resolved_at`,
`resolution_reason`).

## 8. Approval gates and authorization boundaries

The orchestrator owns exactly two gates (`guardrails/policy.yaml`): `intake_mismatch_review` and
`day_one_signoff`. Every UC approval stays with its UC:

| Approval | Owner | Decided through |
|---|---|---|
| `intake_mismatch_review`, `day_one_signoff` | orchestrator | its HITL inbox (`decide_approval`) |
| UC05 `scheduling_approval`, `buddy_assignment`, `comms_approval` (and `attendance_decision`) | UC05 | relayed to UC05's HITL inbox; UC05 checks role, tenant and the specific team lead/buddy |
| UC02 re-plan gates, UC03 HR review, UC04 approval records | UC02/UC03/UC04 | their own mechanisms; the orchestrator lists them and refuses to decide them (409) |

The orchestrator never decides anything itself: the decider is always the authenticated person.

**A decision is validated before it is recorded.** `HITLInbox.decide` (unchanged) records first and
notifies listeners afterwards, so a check inside UC05's listener came too late: the wrong team lead
consumed the item. Both entry points now validate first — UC05's own API (`create_app(...,
decision_validator=...)` in `hr_onboarding_common.api`) and the orchestrator's relay (the worker's
`decide_approval`). Refused before recording, with the approval still pending for the right person:
another tenant's item (404), a person other than the team lead/buddy on record (403, the message
names no one), a booking no longer pending, e.g. released by the timeout sweep (409), an EDITED
buddy who is not an eligible candidate of the same client or retargets the employee, an edited
welcome with an empty subject/body or another draft id, a replan without a valid office-hours start
(422), an item that is no longer the proposal the case expects (409), and an already-decided item
(409, replay/duplicate). Recording itself is atomic in `HITLInbox` (its lock; SQLite in UC05 with
persistence), and the orchestrator serializes operations per case. UC05's listener check stays as
defence in depth for code that calls `HITLInbox.decide` directly.
A rejection of an orchestrator gate holds the stage (`gate_rejections`) until HR explicitly
resumes; a UC05 rejection is applied by UC05 (a rejected induction's hold is released) and the stage
is `blocked` until HR re-requests the step.

| Agent | Entry point | Enforces caller identity itself? |
|---|---|---|
| UC01 | `open_onboarding_case` | **No** — compensated by `intake_trigger_roles` |
| UC02 | `UC02SubAgent.run` | **No** — compensated by `task_planning_trigger_roles` |
| UC03 | `DocumentValidationService.validate` | **Yes** — `authorized_actor_ids` |
| UC04 | `evaluate_orchestrated` | **Yes** — `Policy.check_caller` |
| UC05 | `run_event`, registry, HITL | **Yes** — registry tenant checks on every record; HITL role + tenant; specific team lead/buddy |

The orchestrator always threads through the real, already-verified initiating actor; it never
constructs a synthetic principal. Orchestrator-side role allowlists (`policy.yaml settings`, all
fail closed): `case_operator_roles` (start/resume), `case_reader_roles`,
`joining_day_trigger_roles` (includes `onboarding_scheduler`, the scheduler's system role),
`uc05_operation_roles`. RSVPs are open to any authenticated person of the tenant: UC05 decides who
may answer (the participant, or hr_admin on their behalf).

## 9. API entry points (`orchestration/api.py`)

`create_orchestrator_app(agent=…, identity_provider=…)`. Identity comes only from the supplied
`IdentityProvider`; the tenant is the verified actor's `client_id` (the start body has no
`client_id` and forbids unknown fields). Another tenant's case is answered exactly like a missing
one. The kill switch (`enabled: false`) stops every write route.

| Route | Who | Does |
|---|---|---|
| `POST /v1/orchestration/cases` | `case_operator_roles` | start (idempotent per offer reference); optional `employee_id` (§3.3) and `timezone` (IANA, 422 if unknown) |
| `GET /v1/orchestration/cases/{case_id}` | `case_reader_roles` | case and stage status |
| `POST /v1/orchestration/cases/{case_id}/resume` | `case_operator_roles` | resume after a correction (re-opens a rejected orchestrator gate) |
| `POST …/approvals/{approval_id}/decision` | any authenticated person of the tenant; the owning inbox checks the approver | `{decision, reason_code?, note?, edited_payload?}` |
| `POST …/approvals/{approval_id}/retry-apply` | the approval's role or hr_admin (UC05 rule) | retry a decision whose follow-up failed |
| `POST …/joining-day` | `joining_day_trigger_roles` | `{as_of}`; 409 before the joining date |
| `POST …/orientation/operations` | `uc05_operation_roles` | `{operation: reschedule_session \| schedule_buddy_intro \| propose_buddy \| redraft_welcome \| resolve_operation \| resolve_welcome_delivery \| retry_welcome_delivery, session_type?, start? (tz-aware), key?, outcome?, resolution?, reason?, provider_ref?}` |
| `POST …/sessions/{session_id}/responses` | any authenticated person of the tenant; UC05 checks the participant | `{participant_id, response: accepted \| tentative \| declined \| no_response, responded_at?}` (RSVP, §11) |
| `POST …/documents`, `POST …/documents/reviews` | `document_submitter_roles`, `document_review_roles` | supply / mark available documents; an HR reviewer's decision (UC03's input) |
| `POST …/stages/{stage}/retry` | `stage_retry_roles` | retry a stage that failed technically |
| `GET …/pending-actions` | `case_reader_roles` | what must happen next and who acts |
| `GET /healthz` | — | policy and kill-switch state |

Errors: 401 identity, 403 role/policy/kill switch (and UC05 refusing the approver), 404 not found or
another tenant's, 409 not applicable to the case's state (prerequisite, already decided, premature
event), 422 validation, 502 a UC failed or broke its contract. No stack trace or provider text is
returned. A UC worker that is down does not fail the request: the affected stage comes back
`blocked` (`UC05_UNAVAILABLE`) or, after a write, `uncertain`.

Examples (captured from a test run; UC01–UC04 were doubles):

```http
POST /v1/orchestration/cases
X-User-Id: user:hr-admin-01   X-Client-Id: CLIENT-TEST-A   X-User-Roles: hr_admin   (dev identity provider)

{"offer_accepted": true, "offer_reference": "OFR-T-001", "offer_acceptance_event_id": "EVT-001",
 "candidate_name": "…", "candidate_email": "…", "designation": "Software Engineer",
 "joining_date": "2026-12-01", "id_number": "…", "role_id": "software_engineer",
 "department": "Engineering", "manager_id": "TL-TEST-001", "location": "Dubai", "project": "platform",
 "uc04_client_config_ref": "…/client_alpha_config.json"}

201
{"case_id": "UC01-REQ-6818e39a5846", "client_id": "CLIENT-TEST-A", "employee_id": "EMP-TEST-001",
 "current_stage": "orientation_scheduling", "overall_status": "blocked",
 "next_action": "hr_review:orientation_scheduling:induction_awaiting_approval",
 "pending_approvals": [{"agent": "employee-onboarding", "reference": "apr-073e75c76217", "role": "hr_admin",
   "description": "UC05 induction (orientation) booking -- tentative hold awaiting HR",
   "stage": "orientation_scheduling", "gate": "scheduling_approval"}],
 "orientation": {"uc05_case": true, "uc05_employee_id": "EMP-TEST-001", "uc05_start_date": "2026-12-01",
   "session_id": "session-d39767c23210", "session_status": "scheduled",
   "scheduled_start": "2026-12-01T09:00:00+04:00", "approval_id": "apr-073e75c76217",
   "approval_status": "pending",
   "operation_key": "calendar:CLIENT-TEST-A:EMP-TEST-001:induction:2026-12-01T05:00:00+00:00", "…": "…"},
 "…": "…"}
```

```http
POST /v1/orchestration/cases/UC01-REQ-6818e39a5846/approvals/apr-073e75c76217/decision
{"decision": "approved"}

200
{"current_stage": "buddy_assignment", "overall_status": "blocked",
 "stage_statuses": {"orientation_scheduling": "complete", "buddy_assignment": "awaiting_approval",
                    "team_introduction": "pending", "buddy_introduction": "not_requested", "…": "…"},
 "buddy_assignment": {"proposed_buddy_id": "EMP-TEST-050", "approval_id": "apr-380cec887672", "status": "proposed"},
 "approval_decisions": [{"agent": "employee-onboarding", "reference": "apr-073e75c76217", "decision": "approved",
   "approver": "user:hr-admin-01", "applied": true, "run_stage": "buddy_awaiting_approval",
   "run_meeting_type": "induction", "run_errors": [], "…": "…"}], "…": "…"}
```

```http
POST /v1/orchestration/cases/UC01-REQ-6818e39a5846/joining-day      {"as_of": "2026-11-30"}
409 {"detail": "joining-day event for 2026-11-30 is before the joining date 2026-12-01; the welcome is not drafted early"}

GET /v1/orchestration/cases/UC01-REQ-6818e39a5846   (X-Client-Id: CLIENT-TEST-B)
404 {"detail": "not found"}
```

Service-level equivalents (`OnboardingOrchestratorAgent`): `advance_case`, `get_case`,
`resume_case`, `decide_approval` (and the older `decide_gate`), `retry_approval`,
`trigger_joining_day`, `request_uc05_operation`, `record_participant_response`, `list_case_states`. UC05 is reached through the worker, not through
UC05's HTTP API; no per-UC HTTP API was added.

## 10. Running things

Environments: the `.venv` folders in this workspace were created on another machine (their
`pyvenv.cfg` points at `C:\Users\Prasanna Kumar\…`) and do not start here. They were rebuilt with
`uv sync --frozen` from each project's own `uv.lock`: HR-Onboarding-Common in place; UC01–UC05 into
directories outside their trees (`UV_PROJECT_ENVIRONMENT=<dir>`), so no UC directory was touched.
Point the workers at them with `UC01_WORKER_PYTHON`, `UC04_WORKER_PYTHON`, `UC05_WORKER_PYTHON`.

```
# HR-Onboarding-Common (orchestration unit, integration, API and regression tests)
cd HR-Onboarding-Common
uv sync --frozen --group dev
UC01_WORKER_PYTHON=… UC04_WORKER_PYTHON=… UC05_WORKER_PYTHON=… .venv/Scripts/python.exe -m pytest

# UC05 standalone (its venv installs hr-onboarding-common from ../HR-Onboarding-Common)
cd UC05_Orientation-Schedule-Agent
uv sync --frozen --group dev          # or UV_PROJECT_ENVIRONMENT=<dir> uv sync --frozen --group dev
<uc05 python> -m pytest
```

These lines are bash (`VAR=value command`) with placeholders; they do not run in PowerShell. On
Windows, `scripts/run_tests.ps1` does all of it: it builds the UC01/UC04/UC05 worker environments
once under `%LOCALAPPDATA%\hr-onboarding-envs` (outside the UC folders), sets the `UC0X_WORKER_PYTHON`
variables, refreshes HR-Onboarding-Common inside the UC05 environment, and runs both suites:

```powershell
# from the workspace root
powershell -ExecutionPolicy Bypass -File HR-Onboarding-Common\scripts\run_tests.ps1
#   -Suite common | uc05   run one suite only
#   -Rebuild               recreate the environments
#   -EnvRoot <dir>         put the environments elsewhere
```

Worker-backed tests skip with an explicit reason when a UC's interpreter cannot start
(`tests/orchestration/_helpers.py` checks that it actually runs, not only that the file exists).
The workspace root is derived from the test file's location (`HR_ONBOARDING_WORKSPACE` overrides
it). No test needs a live LLM, credentials, or any real external service: `tests/conftest.py` forces
`LLM_MODE=offline`, points `HR_ONBOARDING_ENV_FILE` away from a developer's real `.env` and removes
provider keys from the test process. The opt-in live check is `scripts/openrouter_smoke_test.py`.

### 10.1 Joining-day scheduler

Nothing starts the scheduler automatically. Two configured entry points exist:

```
# one tick, e.g. from cron or Windows Task Scheduler every 5 minutes (prints the tick's JSON report)
python -m hr_onboarding_common.orchestration.joining_day_scheduler --once --persist-dir <orchestrator data dir>
#   --persist-dir or ORCHESTRATOR_DATA_DIR is required; --worker-config / ORCHESTRATOR_WORKER_CONFIG,
#   --uc03-repo-root / UC03_REPO_ROOT, --agent-home / ORCHESTRATOR_HOME as for the API
# without --once it loops every ORCH_SCHEDULER_INTERVAL_S (default 300 s)

# in-process, next to the orchestrator API (preferred: the UC workers' in-memory data is per process)
JoiningDayScheduler(agent, SqliteJoiningDayJobStore(path)).start_background()
```

Settings (`ORCH_SCHEDULER_*`): `CATCH_UP_DAYS` (2), `LEASE_S` (900), `MAX_ATTEMPTS` (5),
`INTERVAL_S` (300). A case is due on its joining date in the employee's timezone (case → tenant →
`HR_AGENT_TZ`) from the tenant's `joining_day_run_at`, within the catch-up window, once day-1 readiness
is signed off; it acts as `system:orchestrator-joining-day-scheduler` with role
`onboarding_scheduler` in the case's own tenant. Per-case eligibility is reported as
`due`, `not_due`, `already_complete`, `case_failed`, `uncertain_needs_review`,
`missing_joining_date`, `outside_catch_up_window`, `awaiting_day_one_signoff` or
`awaiting_welcome_approval`. A case whose welcome is `uncertain` is never re-triggered. UC05's own
scheduler must not run against the UC05 worker's data directory in an orchestrated deployment (it
cannot see the readiness signoff). Tests drive it with an injected clock
(`test_joining_day_scheduler.py`), including the `--once` CLI against a persisted directory.

### Test coverage for this integration

| File | Covers |
|---|---|
| `test_uc05_adapter.py` | Each UC05 capability against a recording double with the real worker's reply shapes: payloads sent, preflight stops, redelivery, rejected/ineligible/no-candidate buddies, team/buddy introductions, welcome outcomes per ledger status, contract violations, transport loss → `UNCERTAIN`, relays, HR-operation prerequisites; plus real-worker tenant isolation |
| `test_uc05_lifecycle.py` | The real graph + real UC05 worker (UC01–UC04 doubles): the 20 integration scenarios listed in the implementation report |
| `test_orchestrator_api.py` | Identity, tenancy, roles, validation, error mapping, the full lifecycle over HTTP, kill switch |
| `test_lifecycle_contracts.py` | Routing for every status, the routing table, overall status, back-compat of persisted state, SQLite store |
| `test_employee_identity.py` | Identity schemes (`shared`/`mapped`/`none`), every `EMPLOYEE_ID_*` code, blocking before UC05 is touched, per-tenant mappings, every UC05 operation using the mapped id end to end, the stored ref surviving a mapping change, invalid tenant configuration |
| `test_joining_day_scheduler.py` | Eligibility per timezone/run time/catch-up/signoff, idempotent ticks, leases and retries, SQLite job store across restarts, the `--once` CLI |
| `test_approval_authorization.py` | Unauthorized, cross-tenant, replayed, duplicate, expired and malformed decisions leave the approval usable; concurrency; durable restart |
| `test_welcome_recovery.py` | Delivery states, operator resolution with reason/actor/time/audit, authorized retry, no blind resend |
| `test_state_driven_workflow.py` | The state-driven workflow: collection, the document wait, UC03 routing (correction / HR review / technical), resume without re-running UC01 or resending, the plan_tasks barrier then the two parallel branches (barrier-proven), merge, status aggregation, retries, idempotency, tenancy, authorization, privacy; real UC03 in-process |
| `test_plan_tasks_barrier_and_date_change.py` | UC02's real DATE_CHANGE gate (request/decide/apply against a real `OnboardingService`, propagation to both branches, no-op/invalid requests, pending/rejected/repeated decisions, merge safety, retry/resume) |
| `test_execution_order.py` | plan_tasks exits before either branch enters (entry/exit timing with a delayed plan, a deterministic gate check, LangGraph's executed supersteps); UC04 and UC05 overlap on separate threads (2-party barrier, delays); failed/blocked/retry-pending/awaiting-approval planning or a missing/invalid joining date starts no branch; the whole workflow in order |
| `test_joining_date_reconciliation.py` | Joining-date revisions and history; stale UC04/UC05 results re-run for the current date; signoff withdrawn and held until consistent; one branch paused; repeated changes; a process stop mid-reconciliation; a failed re-evaluation; readiness protection; the UC05 mismatch stop; the real UC04 worker's reassessment and key refusal; the real UC05 worker's mismatch stop; HITL withdrawal |
| `test_rsvp_and_sensitive_data.py` | RSVPs as independent events, decline → attendance decision → replan/cancel, no `id_number`/email in checkpoints or the case store, `INTAKE_INPUT_UNAVAILABLE` |
| UC05 `tests/test_orchestration_contract.py` | What the orchestrator relies on from UC05: vocabularies, stages, signatures, runtime builder, result shapes |
| UC05 `tests/test_client_configuration.py`, `test_decision_validation.py`, `test_welcome_redelivery.py` | The UC05-side changes: tenant configuration, validation before recording (service and API), authorized redelivery |

## 11. Remaining blockers, gaps and mocked integrations

**Owned by other teams (not changed here):**

| Owner | Item | Action required |
|---|---|---|
| UC02 | No document-collection operation (only `UC02SubAgent.run`, i.e. task planning); its mock Outlook connector refuses delivery without an approved gate | Operation A runs as an orchestration adapter with a mock notifier until UC02/UC06 expose it (STATE_DRIVEN_ONBOARDING_WORKFLOW.md §2) |
| UC03 | Never assigns `document_status=ACCEPTED` (verification = `compliance_item_status=COMPLETE` with a caller-supplied HR approval); no resubmission operation (a replacement is a `DUPLICATE_REQUEST`) | The orchestration adapter now follows this contract (it previously required `ACCEPTED` and could not complete against real UC03); UC03 owner to decide how replacements are re-validated |
| UC02 | `src/onboarding_task_management/agent/advisor.py:1` starts with a stray `UC` before the module docstring → `SyntaxError`; the whole `agent` subpackage (and `UC02SubAgent.run`) is unimportable; UC02's own suite has 3 collection errors | Delete the two characters before `"""` on line 1, then re-run UC02's suite and `test_real_uc02_defect_fails_task_planning…` (it will then exercise real UC02) |
| UC01 / HR | No real HRIS/HRMS shared with UC05: UC01's `employee_no` and UC05's directory `employee_id` are separate identifier spaces | Per tenant, choose the identity scheme in `config/tenants.yaml` (§3.3): `shared` only if both really use the HRMS id, otherwise `mapped` or `none`. The UC05 preflight still blocks on `IDENTITY_MISMATCH` rather than scheduling the wrong person |
| UC01–UC04 | Business data is in memory; a worker restart loses it | Durable stores in those agents |
| UC04 | `CallerContext`/`Uc04Input` identifiers must match `SYN-…` patterns | Identity translation before a real IdP |
| UC05 | A case's start date is fixed at UC05's intake (`services/intake.py`, from its employee directory); UC05 offers no operation to change it, and schedules every session, the joining-day trigger and the welcome from it | After an approved joining-date change the orchestrator blocks `orientation_scheduling` with `JOINING_DATE_MISMATCH` until UC05's record holds the new date (STATE_DRIVEN_ONBOARDING_WORKFLOW.md §4). UC05 owner to decide on a start-date change capability (re-plan sessions, re-key the joining-day trigger) |
| Real tenants | `CLIENT-TEST-A` is configured in UC05's synthetic seed and in `config/tenants.yaml`; no real tenant is | Each real tenant needs a UC05 `ClientConfig` (until then: `UC05_CLIENT_CONFIG_MISSING`) and a `tenants.yaml` entry confirmed with HR (until then: `default`) |

**Closed since the first integration:** an approved welcome resolved as not delivered can be
redelivered through an authorized operation (§7); a decision by the wrong person, or an invalid
one, no longer consumes the HITL item on the API and orchestrator paths (§8); the intake inputs no
longer reach any checkpoint (§5).

**Still open:**
- RSVPs are forwarded as independent events (`participant_responded`), not orchestrator stages. A
  participant answering for someone else is refused by UC05 (`POLICY_DENIED`); that refused run
  stays interrupted in UC05's checkpointer (UC05's existing behaviour; no state is changed).
- A direct `HITLInbox.decide` call on a UC05 item that bypasses both entry points still records
  before UC05's listener check (which then refuses to apply it); outside tests, the only callers of
  `decide` are the two validated entry points and the orchestrator's decisions on its own gates.
- The scheduler is not started by any service manager here; it runs only where an operator wires
  the `--once` command or `start_background()` (§10.1).
- In-flight operations are not recovered automatically after a crash: an interrupted write is
  `UNCERTAIN` and an operator resolves it; nothing is resent blindly.

**Mocked or unconfigured:** every external integration across all five UCs (calendar, email,
Teams/Outlook, HRMS/directory, IT desk, identity provider) is simulated; UC05 refuses
`HR_AGENT_ENV=production`. The orchestrator API has only the development `InsecureHeaderIdentityProvider`
in tests — production needs a real `IdentityProvider` behind a trusted gateway (README). SQLite
stores are single-host.

## 12. SVG verification (historical)

No "HR Onboarding Document Intake Pipeline" SVG exists in the workspace. Sequencing rests on the
Mermaid flow in `UC02_Onboarding_Task_Agent/docs/uc01-uc02-uc03-workflow.md` (UC01→UC03) and on
each agent's verified behaviour (UC04, UC05 — §2.1).
