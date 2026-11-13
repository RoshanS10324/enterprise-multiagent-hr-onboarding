# Shared State and Context

What the orchestrator keeps about a case, who may change each part of it, how a UC's update is
validated before it enters, what each UC is handed, and what is durable.

Code: `orchestration/state.py` (schema, statuses), `orchestration/workflow_models.py` (typed contract,
validated on every write-through), `orchestration/state_sync.py` (ownership and synchronization),
`orchestration/case_store.py` (persistence).

Related: [UC_INTEGRATION_CONTRACTS.md](UC_INTEGRATION_CONTRACTS.md),
[ORCHESTRATION_WORKFLOW.md](ORCHESTRATION_WORKFLOW.md).

## 1. Principle: a projection, not a record store

The case state carries **IDs, statuses, codes, dates and small summaries**: enough to route, resume and
show HR what is outstanding. Each UC's business data stays in that UC:

| Business record | Owner |
|---|---|
| Employee record, onboarding case, form | UC01 |
| Onboarding record, task checklist, re-plan gates | UC02 |
| Document case, validation findings, document audit | UC03 |
| Equipment/access requests, tickets, approval records | UC04 |
| Onboarding record, sessions, buddy, welcome drafts, HITL inbox, operation ledger | UC05 |

## 2. State layers: temporary and durable

| Layer | Holds | Persisted by | Durable? |
|---|---|---|---|
| LangGraph checkpoints | every graph step, thread id = case id | `MemorySaver` (default) / `SqliteSaver` (`build_deps(persist_dir=…)` → `checkpoints.db`) | with `persist_dir` |
| Orchestrator case state | `OnboardingCaseState`, written through after every stage and branch step | `InMemoryCaseStateStore` / `SqliteCaseStateStore` (`case_states.db`) | with `persist_dir`; **the authoritative resume point** |
| Orchestrator approvals and audit | its two gates (`hitl.db`), audit events (`audit.jsonl`) | `HITLInbox`, `AuditLog` | with `persist_dir` |
| Execution and idempotency records | `stage_outcomes` (one normalized response per stage), `idempotency_keys`, `retry_metadata`, `processed_events`, `field_provenance`, `state_version` | inside the case state | with the case state |
| UC business records | section 1 | each UC: UC01–UC04 in memory (prototype), UC05 in memory or SQLite (`UC05_WORKER_DATA_DIR`) | per UC; **not** made durable by the orchestrator |
| Intake-only personal inputs | id number, email, onboarding form | process memory (`deps.intake_inputs`) | **never**, by design: a restart before intake completes asks for the start to be resubmitted (`INTAKE_INPUT_UNAVAILABLE`) |
| Joining-day jobs | one job per (case, joining date) | `InMemoryJoiningDayJobStore` / `SqliteJoiningDayJobStore` | with `persist_dir` |
| Mock notification outbox | the document request | process memory | no (a test double) |

Checkpointing persists the orchestrator's projection only. It never persists a UC's business data and
does not make any external side effect exactly-once. Restart durability is verified by
`test_the_new_bookkeeping_survives_a_restart` and
`test_10_process_restart_during_a_paused_workflow_resumes_from_durable_state`.

## 3. Schema (grouped)

| Group | Fields |
|---|---|
| Identity | `case_id`, `client_id`, `employee_id`, `employee_ref`, `offer_reference`, `joining_date`, `case_timezone`, `correlation_id`, `actor` |
| Joining date | `joining_date_revision` (1 from UC01, +1 per applied UC02 change), `joining_date_history` (one entry per revision), `joining_date_consistency` (derived: whether every date-dependent result was produced for the current date) |
| Position | `current_stage`, `overall_status`, `workflow_status`, `stage_statuses`, `branch_statuses`, `completed_stages`, `pending_actions`, `next_action`, `error_code`/`error_detail` |
| Documents | `document_collection`, `submitted_documents` (refs and extraction metadata, never contents), `document_validation_result`, `validation_issues`, `correction_requirements`, `evidence_references`, `verified_context` |
| Stage results | `task_plan`, `equipment_access_result`, `orientation_schedule_result`, `buddy_assignment_result`, `team_introduction_result`, `buddy_introduction_result`, `welcome_message_result` |
| Approvals and events | `pending_approvals`, `approval_decisions`, `gate_rejections`, `intake_mismatch_approved`, `day_one_signoff_approved`, `participant_responses`, `joining_day_event`, `processed_events` |
| Execution bookkeeping | `stage_outcomes`, `stage_errors`, `stage_history`, `stage_attempts`, `retry_metadata`, `idempotency_keys`, `blockers`, `outstanding_tasks`, `field_provenance`, `state_version`, `dispatch_round`, `active_branches`, `redispatch_branches`, `created_at`, `updated_at` |
| Inputs carried from intake | `intake_trigger` (minimized after intake), `uc02_role_context`, `uc04_client_config_ref` |

Every field added after the first release is optional and back-filled by `ensure_lifecycle_fields`,
so a state persisted by an earlier version still loads.

## 4. Field ownership

Each field has one authoritative owner (`state_sync.FIELD_OWNERS`, `STAGE_SCOPE`):

| Field | Owner (the only writer) |
|---|---|
| `case_id`, `employee_id`, `intake_trigger` | `intake` (UC01) |
| `joining_date` | `intake` (UC01) sets it; `plan_tasks` (UC02) may revise it only through an approved, applied DATE_CHANGE (`relay_uc02_decision`). Every change is the next `joining_date_revision`, recorded in `joining_date_history` and `field_provenance` |
| `document_collection` | `document_collection` (UC02 op A), `document_submission` (what is outstanding) |
| `document_validation_result`, `validation_issues`, `correction_requirements`, `evidence_references` | `validate_documents` (UC03) |
| `task_plan` | `plan_tasks` (UC02 op B) |
| `equipment_access_result` | `equipment_access` (UC04) |
| `employee_ref` | the UC05 stages (resolved from `config/tenants.yaml`; frozen once UC05 holds the case) |
| `orientation_schedule_result`, `buddy_assignment_result`, `team_introduction_result`, `welcome_message_result` | the matching UC05 stage |
| `buddy_introduction_result` | `team_introduction` (refreshed from the same UC05 snapshot), `buddy_introduction` |
| `stage_statuses`, `stage_errors`, `blockers`, `pending_approvals`, `idempotency_keys` | per entry: only the stage it names (plus `STAGE_SCOPE` extras: `team_introduction` → `buddy_introduction`) |
| `current_stage`, `overall_status`, `workflow_status`, `next_action`, `error_*`, `outstanding_tasks`, `pending_actions`, `branch_statuses`, `completed_stages`, `joining_date_consistency`, `updated_at` | derived bookkeeping; recomputed by `refresh_workflow_view` |
| `joining_date_revision`, `joining_date_history` | the orchestrator (`joining_date.record_change`); never a stage adapter |
| `client_id`, `correlation_id`, `actor`, `created_at` | immutable |
| `submitted_documents`, `verified_context`, approval and event fields, `stage_history`, `stage_attempts`, `stage_outcomes`, `retry_metadata`, `field_provenance`, `processed_events`, `state_version` | the orchestrator itself (case operations, the stage wrapper); never a stage adapter |

## 5. Update rules

`state_sync.synchronize(before, after, stage, …)` runs on every adapter result before it enters the
state. The adapter works on its own copy, so an adapter that shallow-copies and appends cannot change
the snapshot it is validated against.

1. **Ownership.** A changed field the stage does not own, or another stage's status, error, blocker or
   approval → `STATE_OWNERSHIP_VIOLATION`. A changed tenant → `TENANT_CONTEXT_CONFLICT`.
2. **Identity.** A known `employee_id` replaced by another → `IDENTITY_CONFLICT`. A canonical
   `employee_ref` of another tenant → `TENANT_CONTEXT_CONFLICT`. UC05's canonical employee changed after
   UC05 holds the case → `IDENTITY_CONFLICT`.
3. **No null overwrite.** An owner returning a known value as missing keeps the previous value
   (`NULL_OVERWRITE_IGNORED` warning).
4. **All or nothing.** A violation discards the whole update. The stage is `failed`, not retryable;
   an operator reconciles. The UC's own record is untouched, and the next run re-reads it.
5. **Transitions.** The stage's status change must be valid (UC_INTEGRATION_CONTRACTS.md §4).
6. **Provenance.** Every accepted owned-field change records
   `field_provenance[field] = {stage, use_case_id, execution_id, at}`: which execution produced it
   (the last writer).
7. **Write-through guard.** `_persist` validates the whole state against `WorkflowStateModel`. It
   refuses a state that would undo a completed stage (`StatusTransitionError`, nothing stored) and
   increments `state_version`.
8. **Determinism under parallelism.** Branches never write shared fields. Each returns its own
   outcome under its own `branch_outcomes` key, written through under a per-case store lock, and
   `merge_outcomes` folds them in. `primary_stage` and `workflow_status` do not depend on the order in
   which branches finished.
9. **Stale and duplicate updates.** Operations on one case are serialized per process (striped
   locks). A redelivered event is answered from `processed_events`. A completed stage is never
   re-executed, and a failed one only after an explicit retry. An unchanged document revision is
   never sent to UC03 twice.

## 6. What each UC is handed

Each stage receives `input_data` (`workflow.stage_inputs`) and `shared_context`
(`agent_contract.shared_context`). Both are recorded in its `StageOutcome`:

| Stage | Handed |
|---|---|
| UC01 intake | client, offer reference; the intake-only inputs are merged in for this one call only |
| UC02 op A | case, client, `employee_id`, joining date, role, location |
| UC03 | per document: revision, `document_ref`, HR review decision (extraction metadata goes to UC03 only, never to logs or responses) |
| UC02 op B | `verified_context` + role context (`role_id`, `department`, `manager_id`, `location`, `project`) |
| UC04 | `verified_context`, role, `task_plan_available` (UC04's contract has no task-plan field: recorded, not passed) |
| UC05 | `verified_context`, canonical employee id, offer-acceptance event id, `task_plan_available` |
| every stage | `shared_context`: `employee_id`, `joining_date`, `case_timezone`, `offer_reference`, `canonical_employee_id`, `documents_verified`, `verified_document_types`, `task_plan_available`, `equipment_access_ready`, `day_one_signed_off` |

## 7. Tenant isolation

- The tenant is the verified actor's `client_id`, never a request field. The start body has no
  `client_id` and forbids unknown fields.
- Every read and write checks the tenant before anything is read or changed. Another tenant's case is
  answered exactly like a missing one (`KeyError` / 404), and it is unchanged
  (`test_15_cross_tenant_access_is_indistinguishable_from_a_missing_case_and_changes_nothing`).
- The contract layer refuses an actor or canonical identity of another tenant before any UC call. UC03,
  UC04 and UC05 enforce their own tenant/actor checks as well.
- `SqliteCaseStateStore` keeps the tenant as an indexed column. HITL items carry `client_id` and cannot
  be listed or decided across tenants.

## 8. Sensitive data handling

- Document contents never enter the system: there is no field for them, and unknown fields are refused.
  Extraction metadata stays in the case state for UC03 only and never reaches a log, an audit event or
  an API response.
- The id number, email address, onboarding form and raw acceptance email live in process memory for the
  one UC01 call. They are never in graph state, a checkpoint, the case store or a response.
  `intake_trigger` is not recorded in provenance or `updated_fields` for the same reason.
- Logs (`orchestration.stage_finished`) carry IDs, codes, statuses and the correlation, trace and
  execution ids only. Error details are capped at 500 characters and carry no tracebacks or provider
  text.
- `shared_context` is an allowlist with no names or contact details. The API's case view omits intake
  inputs, document metadata, submitter and reviewer identities.

## 9. Cleanup

Today there is no case-deletion operation. What a deletion would have to remove, so that no stale
execution record survives: the case-store row and its alias row (an original `CASE-…` id that UC01
renamed), the LangGraph checkpoint thread (thread id = case id), the orchestrator's HITL items for the
case, any `deps.intake_inputs` entry, the joining-day job, and each UC's own records. The audit log is
append-only by design.

Within a case, the bookkeeping is bounded or cleaned:

- `intake_inputs` are dropped once intake completes.
- `processed_events` keeps the last 500 event ids.
- A stage's blockers and approvals are cleared before it re-runs, and its error when it completes.
- `branch_outcomes` is reset before each fan-out and after each fan-in.

Defining retention and deletion is open work (END_TO_END_INTEGRATION_REPORT.md §10).
