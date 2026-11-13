# UC05 Remaining Gaps — Completion Report

Date: 2026-10-29. Scope: `HR-Onboarding-Common` (the orchestration package) and
`UC05_Orientation-Schedule-Agent`. UC01–UC04 source and `hr-recruitment-common` were not modified
(§14 shows how that was checked). Every statement below was checked against the code and a test run
on this date. The previous report (`UC05_ORCHESTRATION_IMPLEMENTATION_REPORT.md`) is historical; where
it differs from this one, this one reflects the current code.

## 1. Executive summary

All six outstanding gaps and the §9 persistence/security review are implemented and tested within
the stated scope, using the existing simulated connectors and offline LLM only.

- **Gap 1, tenant configuration.** `CLIENT-TEST-A` now has a UC05 welcome configuration. A missing
  or unusable configuration is a typed, sanitized, retryable error instead of a raw `KeyError`.
- **Gap 2, employee identity.** Every UC05 call uses a canonical, tenant-scoped `employee_ref`.
  Missing, malformed, ambiguous, conflicting and unresolved identifiers block with stable codes
  before UC05 is touched.
- **Gap 3, joining-day scheduler.** The orchestrator has its own joining-day scheduler. It is
  tenant- and timezone-aware and idempotent, keeps its jobs in a persisted job store, uses an
  injected clock in tests, and has a `--once` command-line entry point. Nothing starts it
  automatically.
- **Gap 4, unauthorized approvals.** Decisions are validated *before* `HITLInbox` records them, on
  both UC05's API and the orchestrator's relay. An unauthorized, cross-tenant, stale, duplicate or
  malformed decision leaves the approval pending for the right person.
- **Gap 5, uncertain welcome delivery.** Delivery has explicit states. An operator resolves it with
  a required reason, and the actor, time and reason are recorded and audited. A new delivery
  attempt has to be authorized and is refused while the outcome is unknown or once the welcome has
  been delivered. Nothing is resent blindly, and no receipt is fabricated.
- **Gap 6, RSVPs.** RSVPs are integrated as independent events, not lifecycle stages. A decline
  surfaces UC05's attendance decision, and a cancel or replan re-opens the affected stage.
- **§9, persistence and sensitive data.** The intake-only personal data (`id_number`, email, form)
  no longer reaches any checkpoint or the case store. Restart behaviour is verified with SQLite
  persistence.

Results:

| Suite | Before (task-2 start) | After |
|---|---|---|
| HR-Onboarding-Common | 164 passed | **232 passed** |
| UC05 | 505 passed | **533 passed** |
| UC01, UC03, UC04 | 690, 200, 307 passed (+5 skipped) | unchanged |
| UC02 | the existing `advisor.py` SyntaxError | unchanged (it belongs to UC02's owner) |

## 2. Original gaps and their final statuses

| # | Gap | Status | Evidence |
|---|---|---|---|
| 1 | Tenant-specific client configuration | **Done** | UC05 `test_client_configuration.py` (10), Common `test_uc05_lifecycle.py::test_a_tenant_without_welcome_configuration_gets_a_structured_reason_not_a_success`, `test_uc05_adapter.py::test_welcome_not_configured_is_mapped_by_uc05s_error_type` |
| 2 | Canonical employee identifier mapping | **Done** | `test_employee_identity.py` (18) |
| 3 | Joining-day welcome scheduler | **Done**, not auto-started (§8) | `test_joining_day_scheduler.py` (19, including the CLI) |
| 4 | Unauthorized HITL approval | **Done** on both entry points (UC05 API and orchestrator relay). One residual: §15 | `test_approval_authorization.py` (9), UC05 `test_decision_validation.py` (9) |
| 5 | Safe recovery from uncertain welcome delivery | **Done** | `test_welcome_recovery.py` (7), UC05 `test_welcome_redelivery.py` (6) |
| 6 | RSVP and attendance conflict events | **Integrated as independent events** | `test_rsvp_and_sensitive_data.py` (7 RSVP tests), `test_orchestrator_api.py::test_rsvp_route_forwards_to_uc05_and_surfaces_the_conflict` |
| §9 | Persistence, recovery, security | **Done**, with stated limits (§12, §15) | `test_rsvp_and_sensitive_data.py` (2 data tests), restart tests listed in §12 |

## 3. Root cause of each gap

1. **Gap 1.** UC05's synthetic seed configured welcome content only for `CLIENT-ACME` and
   `CLIENT-GLOBEX`, so `get_client_config("CLIENT-TEST-A")` raised a bare `KeyError`. The joining-day
   run then stopped part-way (`MISSING_DATA`, an interrupted node). The orchestrator could only
   report `UC05_MISSING_DATA`, with the internal node name in the detail. Templates were never
   validated, so a template with an unknown placeholder would have failed only while drafting.
2. **Gap 2.** The orchestrator passed UC01's `employee_no` (`state["employee_id"]`) to UC05 as its
   directory `employee_id`, with no tenant-scoped translation. A mismatch surfaced only through the
   preflight's `IDENTITY_MISMATCH`, and there was no way to declare how the two systems relate.
3. **Gap 3.** The only joining-day trigger was the manual `POST …/joining-day`. UC05's own scheduler
   runs inside UC05's process and sees UC05's records only. It knows neither orchestrated cases nor
   the orchestrator's day-1 readiness signoff, so it would draft welcomes the orchestrator had not
   cleared.
4. **Gap 4.** `HITLInbox.decide` records the decision first and notifies listeners afterwards.
   UC05's checks for the specific team lead or buddy, and for an eligible edited buddy, ran in the
   listener, after the item was already decided. A wrong person therefore consumed the approval.
5. **Gap 5.** After an operator resolved an uncertain delivery as `released`, UC05 had no path to
   deliver the approved draft again. The orchestrator labelled an operator-confirmed delivery
   `confirmed_by_ledger`, which read like a provider receipt.
6. **Gap 6.** UC05's `participant_responded` event and its `attendee_declined` decision had no
   orchestrator entry point, so declines never reached the case.
7. **§9.** The intake inputs were removed from `intake_trigger` only *after* intake. Because they
   were part of the graph input of the run that executed intake, LangGraph's checkpoint still held
   them.

## 4. Files changed and why

**HR-Onboarding-Common**

| File | Change |
|---|---|
| `config/tenants.yaml` (new) | Tenant timezone, joining-day run time, identity scheme. Synthetic `CLIENT-TEST-A`/`-B` only |
| `src/hr_onboarding_common/orchestration/tenant_config.py` (new) | `TenantConfig`, `EmployeeRef`, `resolve_employee` with the `EMPLOYEE_ID_*` codes (Gap 2) |
| `src/hr_onboarding_common/orchestration/joining_day_scheduler.py` (new) | Scheduler, job stores (memory/SQLite), settings, CLI (Gap 3) |
| `src/hr_onboarding_common/api.py` | `create_app(..., decision_validator=)` and `DecisionConflictError`: validation before `hitl.decide` on the shared decision route (Gap 4) |
| `orchestration/adapters/uc05_orientation.py` | `employee_ref` resolution and use (Gap 2); config-error mapping (Gap 1); expected proposal type/record on relays (Gap 4); delivery states and the `resolve_welcome_delivery`/`retry_welcome_delivery` operations (Gap 5); RSVP relay and attendance-conflict surfacing (Gap 6) |
| `orchestration/workers/uc_worker_runtime.py` | Validate-then-decide in `decide_approval`; `redeliver_welcome`; `reason` on `resolve_operation`; resolver fields in snapshots; test-only hooks (`remove_client_config`, `sweep_timeouts`, `clear_rejections`) |
| `orchestration/orchestrator_agent.py` | Intake-only inputs kept out of state and checkpoints (§9); per-case locking and audit on every case operation; `record_participant_response`; `list_case_states` (for the scheduler) |
| `orchestration/state.py`, `contracts.py`, `deps.py`, `bootstrap.py`, `adapters/_shared.py` | New optional state fields (`employee_ref`, `case_timezone`, `participant_responses`, approval `proposal_type`/`record_id`); `employee_id`/`timezone` case inputs; `tenants` and `intake_inputs` deps; tenant-config loading |
| `orchestration/api.py` | `employee_id`/`timezone` on start (422 on an unknown zone); new operation fields; RSVP route; `employee_ref`/`participant_responses` in the case view |
| `docs/ONBOARDING_COMMON_ARCHITECTURE.md` | Updated where behaviour changed (§1, §2.3, §3, §5–§11) |
| Tests | New: `test_employee_identity.py`, `test_joining_day_scheduler.py`, `test_approval_authorization.py`, `test_welcome_recovery.py`, `test_rsvp_and_sensitive_data.py`. Extended: `test_orchestrator_api.py` (+3), `test_uc05_adapter.py` (+3). Updated: `test_uc05_lifecycle.py` (4 tests, §14), `_lifecycle.py` (helpers) |

**UC05_Orientation-Schedule-Agent**

| File | Change |
|---|---|
| `src/sub_agent/fakes.py` | `CLIENT-TEST-A` `ClientConfig` (sender "Test Client A People Team", English welcome template) |
| `src/sub_agent/models.py` | `ClientConfigurationError` → `MissingClientConfigError`, `InvalidClientConfigError` (both still `KeyError`s, so existing handling applies) |
| `src/sub_agent/services/welcome.py`, `joining_trigger.py` | `client_config_for` (sanitized missing-config error); `validate_template` (empty subject/body, channel, malformed braces, unknown placeholders) before drafting |
| `src/sub_agent/agent.py` | Stage `welcome_not_configured`: both drafting paths end there with a sanitized `MISSING_DATA` exception instead of an interrupted run |
| `src/sub_agent/services/decisions.py` (new) | `validate_decision`: the specific team lead/buddy, applicability, edited buddy/welcome/replan payloads |
| `src/sub_agent/services/scheduling.py` | `check_decider` extracted for reuse; office-hours check now rejects a meeting that crosses midnight (found while testing replans: 23:00–00:30 passed before) |
| `src/sub_agent/services/delivery.py` | `resolve_operation` keeps `resolved_by`, `resolved_at`, `resolution_reason` |
| `src/sub_agent/tool_registry.py` | `validate_decision` (tenant-checked); `redeliver_welcome`; `reason` on `resolve_operation` |
| `src/sub_agent/api.py` | `decision_validator` passed to `create_app`; `reason` on operation resolve; `POST /v1/onboarding/admin/welcome/{draft_id}/redeliver` |
| `README.md` | Test count, the new route and validation, the fixed limitation, welcome-configuration behaviour |
| Tests | New: `test_client_configuration.py`, `test_decision_validation.py`, `test_welcome_redelivery.py`, `test_scope_regressions.py`. Updated: `test_orchestration_contract.py` (§14) |

## 5. Updated architecture and workflow

The lifecycle and graph are unchanged (intake → tasks → documents → equipment/access → orientation →
buddy → team introduction → day-1 readiness signoff → joining-day welcome → complete). What changed
around it:

```
start ──► intake-only inputs held in process memory, merged into the UC01 call only
          (never in state, checkpoints, case store or responses)
UC05 stage ──► employee_ref = TenantConfig.resolve_employee(...)  ──► blocked EMPLOYEE_ID_* on failure
          └──► every UC05 call uses employee_ref.employee_id
decision ──► orchestrator relay: expected proposal type/record ──► UC05 validate_decision ──► HITLInbox.decide
          UC05 API: create_app(decision_validator) ──► validate_decision ──► HITLInbox.decide
joining day ──► JoiningDayScheduler.tick (cron --once, or in-process) ──► trigger_joining_day (unchanged)
welcome ──► delivery state from UC05's draft + ledger ──► resolve_welcome_delivery / retry_welcome_delivery
RSVP ──► POST …/sessions/{id}/responses ──► UC05 participant_responded (independent event, not a stage)
```

No parallel orchestration was introduced. The scheduler calls the existing `trigger_joining_day`,
the RSVP route relays an existing UC05 event, and the recovery operations call existing UC05
registry methods through the existing worker. Details: `ONBOARDING_COMMON_ARCHITECTURE.md` §3.3, §7,
§8 and §10.1.

## 6. Employee identifier mapping contract

- **Input.** UC01's `employee_no` (stored as the case's `employee_id`) and an optional
  caller-supplied `employee_id` (the start body or `OnboardingCaseInput.employee_id`).
- **Output.** `employee_ref = {client_id, employee_id, employee_no, source, scheme}` in the case
  state. `employee_id` is the only id ever sent to UC05.
- **Per-tenant scheme** (`config/tenants.yaml` → `employee_identity.scheme`):
  - `shared` (the default): `employee_no` is the HRMS id. Source `shared_scheme`.
  - `mapped`: an explicit `employee_no_map`; an unlisted number is unresolved. Source `mapped`.
  - `none`: the caller must supply `employee_id`. Source `explicit`.
- **Precedence.** A supplied `employee_id` wins, but when an upstream id also resolves, the two
  must be equal.
- **Errors.** Each blocks the UC05 stage, is retryable after a correction, and leaves UC05
  untouched:

  | Code | When |
  |---|---|
  | `EMPLOYEE_ID_MISSING` | no identifier at all |
  | `EMPLOYEE_ID_MALFORMED` | an identifier that does not match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`, including a mapped target |
  | `EMPLOYEE_ID_AMBIGUOUS` | one number maps to several ids, or one id is mapped from several numbers |
  | `EMPLOYEE_ID_CONFLICT` | the supplied and upstream ids differ |
  | `EMPLOYEE_ID_UNRESOLVED` | the scheme cannot resolve the number |

- **Tenant scope.** Mappings are per tenant. The same `employee_no` in another tenant never
  resolves to this tenant's employee.
- **Stability.** Once UC05 holds a case for the employee, the stored `employee_ref` is kept, so a
  later configuration change cannot switch UC05 to another person
  (`test_a_mapping_changed_mid_lifecycle_never_switches_uc05_to_another_employee`).
- **Unchanged safety net.** The UC05 preflight still cross-checks the resolved id and joining date
  against UC05's own directory (`IDENTITY_MISMATCH`, `JOINING_DATE_MISMATCH`).

## 7. Tenant configuration requirements

**Orchestrator (`config/tenants.yaml`, or the path in `ORCHESTRATOR_TENANT_CONFIG`).** These keys
apply under `default` and each `tenants.<client_id>`:

| Key | Format and fallback |
|---|---|
| `timezone` | IANA zone; an unknown zone fails loading |
| `joining_day_run_at` | `HH:MM`, local time |
| `employee_identity.scheme` | `shared`, `mapped` or `none` |
| `employee_identity.employee_no_map` | entries of `{employee_no, employee_id}` |

An unlisted tenant uses `default`. A case may carry its own `timezone`, which overrides the
tenant's. The shipped file contains only the synthetic `CLIENT-TEST-A`/`-B` tenants; real tenants
must be confirmed with HR.

**UC05 (`ClientConfig`, UC05 data).** The required fields are:
- `client_id`, `name`;
- `branding.sender_name`;
- `default_language` within `enabled_languages`;
- `templates[template_id]` with a non-empty `subject` and `body`, a supported `channel`,
  well-formed braces, and only these placeholders: `{full_name}`, `{role_title}`, `{start_date}`,
  `{sender_name}`.

Seeded: `CLIENT-ACME`, `CLIENT-GLOBEX`, `CLIENT-TEST-A`. `CLIENT-TEST-B` is deliberately
unconfigured to exercise the missing path. Behaviour:

| Situation | UC05 | Orchestrator |
|---|---|---|
| Configuration missing | stage `welcome_not_configured`, `MissingClientConfigError`, a sanitized `MISSING_DATA` detail naming no client | `UC05_CLIENT_CONFIG_MISSING`, retryable |
| Configuration unusable | `InvalidClientConfigError` / `UnknownTemplateError`; nothing drafted, no approval item | `UC05_CLIENT_CONFIG_INVALID`, retryable |

Once HR configures the client, a resume (or the next scheduler tick) drafts the welcome.

## 8. Joining-day scheduler behavior and runtime invocation instructions

**What it does.** For each orchestrated case, every tick:
- It evaluates eligibility, reported as one of `due`, `not_due`, `already_complete`, `case_failed`,
  `uncertain_needs_review`, `missing_joining_date`, `outside_catch_up_window`,
  `awaiting_day_one_signoff` or `awaiting_welcome_approval`.
- It claims one job per (case, joining date). The claim is atomic: SQLite `BEGIN IMMEDIATE` in the
  persisted store.
- It calls `trigger_joining_day` as `system:orchestrator-joining-day-scheduler` (role
  `onboarding_scheduler`) in the case's own tenant.

**When a case is due.** On the joining date, in the employee's timezone (the case's, else the
tenant's, else `HR_AGENT_TZ`), from the tenant's `joining_day_run_at`. It stays due until `catch_up_days`
after the joining date, and only once day-1 readiness is signed off.

**Job outcomes:**

| Welcome result | Job |
|---|---|
| `complete` / `awaiting_approval` | DONE |
| `uncertain` | DONE `needs_review`; never re-triggered |
| blocked and retryable | FAILED; retried on later ticks up to `max_attempts` |
| anything else | DONE `needs_attention:<code>` |

A RUNNING job whose lease expired is reclaimed, and DONE jobs never run again. Beneath the job
store, `trigger_joining_day` is idempotent and UC05 drafts and delivers at most once.

**Runtime.** Nothing starts it automatically.

```
# one tick (cron / Windows Task Scheduler, e.g. every 5 minutes); prints the tick report as JSON
python -m hr_onboarding_common.orchestration.joining_day_scheduler --once --persist-dir <orchestrator data dir>
#   required: --persist-dir or ORCHESTRATOR_DATA_DIR (job store: <dir>/joining_day_jobs.db)
#   optional: --worker-config/ORCHESTRATOR_WORKER_CONFIG, --uc03-repo-root/UC03_REPO_ROOT, --agent-home/ORCHESTRATOR_HOME
#   without --once: loops every ORCH_SCHEDULER_INTERVAL_S
# in-process beside the orchestrator API (preferred; UC01/UC04 worker data is in memory per process):
JoiningDayScheduler(agent, SqliteJoiningDayJobStore(path)).start_background()
```

Settings: `ORCH_SCHEDULER_CATCH_UP_DAYS` (2), `ORCH_SCHEDULER_LEASE_S` (900),
`ORCH_SCHEDULER_MAX_ATTEMPTS` (5), `ORCH_SCHEDULER_INTERVAL_S` (300). In an orchestrated deployment,
do not run UC05's own scheduler against the UC05 worker's data directory, because it cannot see the
readiness signoff.

**Tests** inject the clock (`JoiningDayScheduler(clock=...)`). They cover:
- timezone boundaries per tenant and per case;
- the run time and the catch-up window;
- signoff gating;
- idempotent ticks, lease reclaim and bounded retries;
- the SQLite job store across a restart;
- the `--once` CLI against a persisted directory.

## 9. Approval authorization and state-transition behavior

**Validation order** (orchestrator relay; UC05's own API does steps 3–5):

1. The verified actor's tenant must own the case (another tenant's case: 404).
2. The approval must be open on this case and still be the proposal the case expects
   (`proposal_type`, `record_id`). A stale or replayed relay is a 409.
3. The tenant must own the item (otherwise "not found").
4. UC05's `validate_decision` runs:
   - **who** decides: the specific team lead or buddy on record. Otherwise 403, and the message
     names a role, never a person.
   - **applicability:** the booking must still be pending. Otherwise 409, e.g. a hold released by
     the timeout sweep.
   - **the edited payload:**

     | Edited approval | Must satisfy (422 otherwise) |
     |---|---|
     | buddy | an eligible candidate of the same client, for the same employee |
     | welcome | the same draft id, with a non-empty subject and body |
     | replan | a valid office-hours start, with no weekend or midnight crossing |

   - the decision value itself (422 if malformed).
5. `HITLInbox.decide` records it atomically under the inbox lock. An already-decided item gets 409
   ("already decided"), so a duplicate or concurrent second decision is refused. In UC05 the inbox
   is SQLite-backed when `HR_AGENT_PERSIST=1`.

**Transitions:**

| From | Event | To |
|---|---|---|
| pending | refused at steps 1–4 | **pending** (unchanged; usable by the right person) |
| pending | valid decision | approved / edited / rejected, then UC05's listener applies it |
| decided | apply failed | decided, `apply_error` set; the stage is `blocked` `DECISION_NOT_APPLIED`; `retry-apply` by the approver role or hr_admin |
| decided | any further decision | refused 409; nothing changes |
| pending, booking released by the timeout sweep | decision | refused 409; the inbox item stays pending (the inbox has no expiry state), and HR re-requests the meeting |

UC05's listener check is kept as defence in depth.

## 10. Welcome-delivery recovery states and operator procedure

`welcome_message_result.delivery` is derived only from UC05's draft and delivery ledger:

| State | Meaning | Stage / code |
|---|---|---|
| `not_attempted` | no delivery operation yet | per UC05's run |
| `delivered` | the draft was marked sent, or the ledger holds a provider-accepted operation | `complete` |
| `confirmed_by_operator` | an operator confirmed delivery after an unknown outcome | `complete`, with `delivery_resolution {by, reason}`; **no provider receipt is implied** |
| `uncertain` | ledger `pending`/`uncertain` | `uncertain` / `WELCOME_DELIVERY_UNCERTAIN` |
| `failed_before_delivery` | the provider refused it permanently (`rejected`) | `blocked` / `WELCOME_DELIVERY_REJECTED` |
| `not_delivered` | an operator confirmed it was not delivered (`released`) | `blocked` / `WELCOME_NOT_DELIVERED` |

**Operator procedure.** Use `POST /v1/orchestration/cases/{id}/orientation/operations`. The caller
needs `uc05_operation_roles`, and UC05 additionally requires hr_admin.

1. **Check the evidence outside the system:** ask the recipient, or look at the provider's console.
2. **Record a verdict** with `{"operation": "resolve_welcome_delivery", "resolution": …, "reason": "<evidence>"}`:
   - `delivered` resolves the operation as succeeded; the stage completes as
     `confirmed_by_operator`.
   - `not_delivered` resolves it as released; the stage is blocked until a retry is authorized.
   - `keep_unresolved` records the review in `delivery_reviews` and changes nothing.

   This is only accepted while the operation is pending or uncertain.
3. **Request a new attempt** with
   `{"operation": "retry_welcome_delivery", "reason": "<why a new attempt is appropriate>"}`. It is
   only accepted for an approved draft whose operation is absent, `released` or `rejected`. It is
   refused while the outcome is unknown and once delivered. UC05 then sends the HR-approved content
   once, under a fresh ledger operation, and audits `welcome.redelivery_authorized`.

The same exists directly on UC05: `POST /v1/onboarding/admin/operations/resolve` (with `reason`) and
`POST /v1/onboarding/admin/welcome/{draft_id}/redeliver`. Every step is audited on both sides
(`orchestrator.uc05_operation` / `uc05_operation_refused`; UC05's operation `resolved_by`,
`resolved_at`, `resolution_reason`). Resumes, scheduler ticks and retry-apply never resend an
uncertain delivery.

## 11. RSVP/attendance event handling and scope decision

**Decision.** RSVPs are **independent events**, not lifecycle stages. Participants answer
whenever they do, UC05 keeps the newest answer per participant, and an RSVP must never hold up or
advance the lifecycle by itself.

**Entry point:**
```
POST /v1/orchestration/cases/{case_id}/sessions/{session_id}/responses
{participant_id, response: accepted|tentative|declined|no_response, responded_at?}
```
It is relayed as UC05's `participant_responded`.

**How a response is handled:**
- **Session check.** The session is checked against this case's UC05 snapshot first (an unknown
  session gives 404, and no UC05 run is started). UC05 then decides who may answer: the participant
  themself or hr_admin on their behalf. Otherwise UC05 returns `POLICY_DENIED`, and an older answer
  gives `CONFLICT`.
- **Recording.** Accepted responses are recorded in `participant_responses` and audited.
- **Declines.** A decline creates UC05's `attendee_declined` decision. It is surfaced in
  `pending_approvals` (agent `employee-onboarding`, role hr_admin) exactly once, however often the
  decline is repeated.

HR's decision on it:

| Decision | Effect |
|---|---|
| keep | nothing changes |
| cancel | cancels the session, and re-opens the session's stage if it was complete (history code `REOPENED_BY_ATTENDANCE_DECISION_CANCEL`) |
| replan (EDITED with `new_start`) | must be a valid office-hours time, otherwise it is refused and the decision stays open; a valid one creates a new meeting that needs a fresh approval |

**Transport failure** gives `UC05_UNAVAILABLE`. Re-sending is safe.

## 12. Persistence and restart behavior

| State | Where | Survives restart? |
|---|---|---|
| Orchestrator case state | `CaseStateStore`: memory, or SQLite (`case_states.db`) with `persist_dir` | with `persist_dir` |
| Orchestrator graph checkpoints, own gates | `MemorySaver`/`SqliteSaver`, HITL in memory/`hitl.db` | with `persist_dir` |
| Joining-day jobs | `InMemoryJoiningDayJobStore` / `SqliteJoiningDayJobStore` (`joining_day_jobs.db`) | with the SQLite store |
| UC05 cases, sessions, RSVPs, approvals, delivery ledger, audit | UC05's SQLite stores with `UC05_WORKER_DATA_DIR` (`HR_AGENT_PERSIST=1`) | with `UC05_WORKER_DATA_DIR` |
| UC01–UC04 business data | in memory in their workers (owned by those teams) | no |
| Intake-only inputs (`id_number`, email, form) | process memory only, deliberately | no; intake blocks `INTAKE_INPUT_UNAVAILABLE` until the start is resubmitted |
| Simulated calendar/notifier | test doubles | no |

Approval status, scheduler progress, delivery outcomes and idempotency records therefore need
`persist_dir` (orchestrator) and `UC05_WORKER_DATA_DIR` (UC05) to survive a restart.

**Verified by:**
- `test_approval_state_survives_a_restart_with_durable_persistence`;
- `test_a_retryable_failure_is_retried_after_a_restart_until_it_succeeds` and
  `test_a_job_whose_runner_died_is_reclaimed_after_its_lease` (scheduler);
- `test_orchestrator_and_uc05_restart_resume_the_same_case_without_duplicates` and
  `test_a_new_orchestrator_instance_continues_a_case_from_its_sqlite_case_store`;
- `test_intake_only_personal_data_never_reaches_a_checkpoint_or_the_case_store` (the raw SQLite
  bytes contain neither the id number nor the email);
- `test_a_restart_before_intake_completes_asks_for_the_start_to_be_resubmitted`.

**No completed side effect repeats** on resume: completed stages are skipped, UC05 is read before
anything is written, and UC05's ledger and `trigger_joining_day` are idempotent.

**Not claimed: full crash recovery.** An operation interrupted mid-write (a worker crash or timeout)
is `UNCERTAIN` and waits for an operator; it is not completed or replayed automatically
(`test_worker_crash_mid_decision_is_uncertain_and_lost_records_are_never_replayed`).

## 13. Security and tenant-isolation considerations

**Identity and tenancy**
- Identity comes only from the configured `IdentityProvider`; the tenant is the verified actor's
  `client_id`. The development header provider is used in tests only. No identity header is
  trusted in production configurations.
- Another tenant's case, approval, session or draft is reported exactly like a missing one (404).
- Identity mappings are per tenant. Decisions are tenant-checked at the orchestrator, in UC05's
  registry and in `HITLInbox`.
- The scheduler acts per case, in that case's tenant, under a named system actor.

**Decisions and operator actions**
- Wrong-person decisions are refused before recording, and messages name a role, not a person.
- Operator resolutions and redeliveries require hr_admin and a reason, and are audited with actor,
  time and reason.

**Errors and sensitive data**
- Configuration errors are sanitized: no client id, no `KeyError` repr. API errors carry no stack
  traces or provider text, and a pydantic `ValidationError` raised inside an operation maps to 422,
  not 500.
- Intake personal data is never persisted by the orchestrator. The case view never returns it
  (verified in §16 step 14).

**Scope**
- No real external integration was added. Every connector remains simulated, and UC05 still refuses
  `HR_AGENT_ENV=production`.
- NDA/policy signature tracking stays retired (`test_scope_regressions.py`). HR Policies Q&A still
  works:
  - `test_hr_policy_qa_still_answers_through_uc05`;
  - `test_uc05_policy_qa_still_works_independently_of_orchestration`;
  - UC05's `test_policy_qa_service.py` and `test_policy_knowledge_base.py`;
  - `test_hr_policies_qa_stays_reachable`.

## 14. Tests executed, exact pass/fail/skip counts, and commands used

Environments: `uv sync --frozen` into scratch directories (UC01–UC05) and HR-Onboarding-Common's own
`.venv`, as in the architecture doc §10. Worker interpreters were passed as `UC01_WORKER_PYTHON`,
`UC04_WORKER_PYTHON` and `UC05_WORKER_PYTHON`. After Common changes, UC05's environment needs
`uv pip install --python <uc05 python> --reinstall --no-deps ../HR-Onboarding-Common`.

The commands in the table are bash with placeholders (`<uc05 python>`, `VAR=… command`); they do
not run in PowerShell. On Windows, run both suites with:

```powershell
powershell -ExecutionPolicy Bypass -File HR-Onboarding-Common\scripts\run_tests.ps1
```

The script builds the worker environments outside the UC folders, sets the worker variables,
refreshes Common inside the UC05 environment, and runs both suites. `-Suite common|uc05` runs one
suite; `-Rebuild` recreates the environments.

| Suite | Command (from the project directory) | Result |
|---|---|---|
| HR-Onboarding-Common | `UC01_WORKER_PYTHON=… UC04_WORKER_PYTHON=… UC05_WORKER_PYTHON=… .venv/Scripts/python.exe -m pytest` | **232 passed**, 0 failed, 0 skipped |
| UC05 | `<uc05 python> -m pytest` | **533 passed**, 0 failed, 0 skipped |
| UC01 | `<uc01 python> -m pytest -q` | 690 passed (1 warning), unchanged |
| UC02 | `<uc02 python> -m pytest -q` | **3 collection errors**: `tests/test_agent.py`, `tests/test_fake_llm.py`, `tests/testcase.py` → `SyntaxError: invalid syntax` in `src/onboarding_task_management/agent/advisor.py` line 1. Unchanged from baseline |
| UC02 | `<uc02 python> -m unittest discover -s tests -t .` | Ran 537, FAILED (errors=3, skipped=1), the same SyntaxError. Unchanged |
| UC03 | `<uc03 python> -m pytest -q tests` | 200 passed (1 warning), unchanged |
| UC04 | `<uc04 python> -m pytest` | 307 passed, 5 skipped, unchanged |
| Lint | `ruff check` (UC02 environment's ruff, each project's own config) | New files: clean except `UP017` (`timezone.utc`, kept to match the codebase) and one deliberate `BLE001`: the scheduler's per-job `except Exception`, so one failing case cannot stop a tick; it records only the exception type. Modified files: one more `B008` each (`Depends(...)` in a new route signature, the existing FastAPI pattern); all other findings were already there at the start |

**New tests:**
- Common: 68 tests (identity 18, scheduler 19, approval authorization 9, welcome recovery 7, RSVP
  and sensitive data 9, API +3, adapter +3).
- UC05: 27 tests (configuration 10, decision validation 9, redelivery 6, scope regressions 2) plus
  1 contract test.

**Existing tests changed, and why.** Each encoded the defective behaviour that Gap 1, 4 or 5
required to change. None was changed merely to pass.

| Test | Was | Now |
|---|---|---|
| Common `test_uc05_lifecycle.py::test_ineligible_buddy_is_never_assigned…` → `test_an_ineligible_edited_buddy_is_refused_without_consuming_the_proposal` | an ineligible edited buddy consumed the proposal (`BUDDY_INELIGIBLE`) | refused before recording (`INVALID_INPUT`); the proposal stays pending and can then be approved |
| `…::test_team_introduction_decided_by_someone_other_than_the_team_lead_is_not_applied` → `test_a_team_intro_decision_by_another_team_lead_is_refused_and_leaves_the_approval_usable` | the wrong team lead consumed the item (`UNAUTHORIZED_APPROVER`) | refused (`POLICY_DENIED`, no person named); the right team lead then approves |
| `…::test_a_tenant_without_welcome_configuration_gets_uc05s_real_reason_not_a_success` → `…gets_a_structured_reason_not_a_success` | relied on `CLIENT-TEST-A` being unconfigured; expected `UC05_MISSING_DATA` with a node name | removes the configuration via a test hook; expects `UC05_CLIENT_CONFIG_MISSING`, sanitized and retryable, then a resume drafts |
| `…::test_uncertain_welcome_delivery_is_never_resent_and_hr_resolves_it` | `delivery == "confirmed_by_ledger"` | `"confirmed_by_operator"` (no receipt is implied) |
| UC05 `test_orchestration_contract.py` | expected `CLIENT-TEST-A` joining day to stop with `MISSING_DATA` (and used ACME for the result shape) | `CLIENT-TEST-A` drafts; the new `welcome_not_configured` stage is in the vocabulary; a new test covers `CLIENT-TEST-B` |

**Ownership checks:**
- **SHA-256 manifest.** Taken at task start (523 files across UC01–UC05) and re-verified: 0
  mismatches in UC01–UC04. All 11 mismatches are the UC05 files listed in §4 (9 source files,
  `test_orchestration_contract.py`, `README.md`).
- **Modification times.** No file under UC01–UC04 (excluding `.venv` and caches) is newer than the
  start manifest.
- **`hr-recruitment-common`.** It is not present in this workspace (UC02's `pyproject.toml`
  mentions it only in a comment pointing at a sibling repository), so it cannot have been modified.

## 15. Known limitations and cross-team blockers

| Owner | Item | Action |
|---|---|---|
| UC02 | `src/onboarding_task_management/agent/advisor.py:1` begins with a stray `UC` before the docstring → `SyntaxError`; UC02's agent package and `UC02SubAgent.run` are unimportable; the orchestrator reports `UC02_PACKAGE_UNIMPORTABLE` at task planning | UC02 owner: delete the two characters, re-run UC02's suite. Not modified here. The full orchestrated lifecycle with *real* UC02 has therefore not been verified; the lifecycle tests use a UC02 double, which is not a verification of UC02 |
| UC01–UC04 | Business data in memory; a worker restart loses it | Durable stores in those agents |
| HR / UC01 | Which identity scheme each real tenant uses | Fill `config/tenants.yaml` per tenant; `shared` only if UC01 and UC05 truly share the HRMS id |
| HR / UC05 | Welcome templates and branding for real clients | A `ClientConfig` per client |

Limitations within this scope:
- **Direct `HITLInbox.decide` calls.** A direct call on a UC05 item that bypasses both validated
  entry points still records before UC05's listener refuses to apply it. `HITLInbox` was
  intentionally not changed: it is shared harness code.
- **Refused RSVP runs.** A refused RSVP (someone answering for another participant) stays as an
  interrupted run in UC05's checkpointer. This is existing UC05 behaviour; no business state
  changes.
- **Inbox items after a timeout sweep.** An inbox item whose booking the timeout sweep released
  stays `pending` in the inbox. Decisions on it are refused (409).
- **Scheduler.** No service manager or cron entry is configured here (§8).
- **Crash recovery.** In-flight operations are not recovered automatically (§12).
- **Single host.** SQLite stores are single-host. Several orchestrator processes on one case need
  a shared lock, which the stores do not provide.
- **No real integrations.** All external integrations are simulated. The orchestrator API has been
  exercised only with the development header identity provider.

## 16. Manual verification steps using the local mock/FakeLLM environment

**UC05 alone** (from `UC05_Orientation-Schedule-Agent`):

1. Start UC05:
   ```
   HR_AGENT_ENV=development IDENTITY_MODE=insecure-dev-headers uv run uvicorn sub_agent.api:app --port 8010
   ```
2. Book a team introduction for an ACME employee: `POST /v1/onboarding/{employee_id}/sessions`
   with `{"session_type": "team_intro", "start": "<a weekday 09:00+04:00>", "attendees": ["TL-001"]}`,
   using headers `X-User-Id: user:hr-admin-01`, `X-User-Roles: hr_admin`,
   `X-Client-Id: CLIENT-ACME`.
3. Decide the approval with `X-User-Id: TL-OTHER`, `X-User-Roles: team_lead`
   (`POST /v1/approvals/{id}/decision {"decision": "approved"}`). Expect **403**, and
   `GET /v1/approvals/{id}` still shows `pending`. Repeat with `X-User-Id: TL-001`: expect 200.
4. `POST /v1/onboarding/admin/welcome/comm-unknown/redeliver {"reason": "x"}` as hr_admin: expect
   404. With `X-User-Roles: employee`: expect 403. With `{"reason": ""}`: expect 422.

**Orchestrator with the real UC05 worker** (UC01–UC04 doubles, in-process HTTP). The walkthrough
script used for this report drives the orchestrator API through `TestClient`, using the test
helpers in `tests/orchestration/_lifecycle.py`:
- `World` and `start_uc05_worker`;
- `create_orchestrator_app(agent=world.agent, identity_provider=InsecureHeaderIdentityProvider(enabled=True))`;
- `JoiningDayScheduler(world.agent, InMemoryJoiningDayJobStore(), clock=…)`.

Run it from `HR-Onboarding-Common` with the `UC0X_WORKER_PYTHON` variables set. Observed output on
2026-10-29:

```
1 start: 201 {"current_stage": "orientation_scheduling", "overall_status": "blocked", "employee_ref": {"client_id": "CLIENT-TEST-A", "employee_id": "EMP-TEST-001", "employee_no": "EMP-TEST-001", "source": "shared_scheme", "scheme": "shared"}}
2 approve induction: 200 {"current_stage": "buddy_assignment", ...}
3 approve buddy: 200 {"current_stage": "team_introduction", ...}
4 wrong team lead: 403 {"detail": "UnauthorizedApproverError: only the team_lead on record for this employee may decide approval apr-26e166a4eb67"}
   approval still pending: True
5 right team lead: 200 {"current_stage": "day_one_readiness", ...}
6 day-1 signoff: 200 {"current_stage": "joining_day_welcome", "overall_status": "in_progress", ...}
7 scheduler before 06:00 local: ['not_due']
8 scheduler at 06:30 local: ['done']
9 scheduler again: ['awaiting_welcome_approval']
10 approve welcome: 200 {"current_stage": "complete", "overall_status": "complete", ...}
   delivery: delivered
11 retry a delivered welcome: 409 {"detail": "only an approved, unsent welcome can be retried"}
12 other tenant reads the case: 404 {"detail": "not found"}
13 scheduler after completion: ['already_complete']
14 'ID-T-001' or the email in the case view: False
```

To see the recovery path manually, simulate a provider outage before approving the welcome, as
`test_welcome_recovery.py` does. Use the worker's test hook (it requires `HR_ORCH_WORKER_TEST_HOOKS=1`
and touches only the simulated notifier):

```
world.hook("set", target="notifier", attrs={"idempotent_delivery": False, "fail_next": 1})
```

Then:
1. Approve the welcome. The stage becomes `uncertain`, with delivery `uncertain`.
2. Run `resolve_welcome_delivery` with `resolution: not_delivered` and a reason. The stage becomes
   `blocked` (`WELCOME_NOT_DELIVERED`).
3. Run `retry_welcome_delivery` with a reason. The welcome is delivered once, and the stage
   completes.

## 17. Remaining work

- **UC02 owner:** fix `advisor.py:1`, then re-run the orchestrated lifecycle against real UC02.
- **HR:** confirm each real tenant's `tenants.yaml` entry (timezone, run time, identity scheme)
  and UC05 `ClientConfig`.
- **Operations:** wire the scheduler (`--once` in cron/Task Scheduler, or `start_background()`
  beside the API) with `persist_dir` and `UC05_WORKER_DATA_DIR` set.
- **Platform:** a production `IdentityProvider` behind a trusted gateway, and real connectors; all
  of these are outside this task.
- **Optional:** move pre-decision validation into `HITLInbox` itself (a shared-harness change) so
  that direct callers are covered too.
