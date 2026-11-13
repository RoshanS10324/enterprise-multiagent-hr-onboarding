# UC Integration Contracts

How the central orchestrator (`HR-Onboarding-Common`, package `hr_onboarding_common.orchestration`)
talks to the five use-case agents: one request envelope and one response envelope for every stage,
one canonical status vocabulary, one error contract, and one set of identifiers that travels with
every call.

Code: `orchestration/agent_contract.py` (models, statuses, errors, transitions),
`orchestration/execution.py` (the seam that applies the contract to a stage),
`orchestration/adapters/` (one adapter per UC: the only code that knows a UC's native interface).

Related: [ORCHESTRATION_WORKFLOW.md](ORCHESTRATION_WORKFLOW.md),
[SHARED_STATE_AND_CONTEXT.md](SHARED_STATE_AND_CONTEXT.md),
[END_TO_END_INTEGRATION_REPORT.md](END_TO_END_INTEGRATION_REPORT.md).

## 1. Three envelopes, three jobs

| Envelope | Defined in | Job |
|---|---|---|
| `UCAgentRequest` / `UCAgentResponse` | `orchestration/agent_contract.py` | What the orchestrator hands one UC stage, and what it gets back, in the same shape for all five UCs |
| `WorkerRequest` / `WorkerResponse` | `orchestration/contracts.py` | Stdio JSON transport to the UC01, UC04 and UC05 worker processes. It carries the UC envelope's correlation, trace and execution ids |
| `AgentRequest` / `AgentResponse` | `hr_onboarding_common/contracts.py` | A `BaseAgent`'s own run envelope (the orchestrator's own run and its HITL proposals). Never handed to a UC |

They layer rather than compete. The adapters keep their signature `(state, deps) -> state`. The stage
wrapper builds the request before the adapter runs and normalizes the response after it returns.

```
_execute_stage (orchestrator_agent.py)
  └─ execution.invoke
       1. build_request + validate          -> ContractViolation: the UC is never called
       2. dependencies.evaluate             -> unmet required dependency: the UC is never called
       3. adapter(state) in execution_scope -> worker calls carry the execution's ids
       4. state_sync.synchronize            -> ownership/identity violation: update discarded, stage FAILED
       5. transition check                  -> invalid status change: update discarded, stage FAILED
  └─ retry accounting, history (wrapper)
  └─ execution.respond -> UCAgentResponse -> persisted as the stage's StageOutcome
```

`execution.execute(stage, state, adapter)` runs steps 1–5 and the response for a single stage outside
the graph. The contract tests use it (`tests/orchestration/test_uc_adapter_contracts.py`).

## 2. `UCAgentRequest`

| Field | Source | Notes |
|---|---|---|
| `contract_version` | constant | `"1.0"` |
| `case_id` | `state.case_id` | Required (non-empty). UC01 may rename the case at intake (its own case id) |
| `tenant_id` | `state.client_id` | Required. The verified actor's tenant |
| `employee_id` | `state.employee_id` | UC01's employee number. `None` before intake |
| `canonical_employee_id` | `state.employee_ref.employee_id` | The HRMS id UC05 uses (`config/tenants.yaml`) |
| `use_case_id` | `USE_CASE_OF[stage]` | `UC01`…`UC05`, or `orchestrator` for the document wait and the day-1 signoff |
| `stage`, `operation` | `workflow.STAGE_OPERATIONS` | e.g. `equipment_access` / `equipment_access_readiness` |
| `correlation_id` | `state.correlation_id` | Required. Constant for the case's whole lifecycle |
| `trace_id` | per invocation | The OpenTelemetry trace id when tracing is configured, otherwise a fresh 32-hex id |
| `execution_id` | per stage attempt | `EXE-<16 hex>` |
| `attempt` | `state.stage_attempts[stage]` | ≥ 1 |
| `idempotency_key` | `state.idempotency_keys[stage]` | Set by the adapters that use one |
| `actor` | `state.actor` | The real, verified initiating actor (`on_behalf_of`, `roles`, `client_id`), never a synthetic principal |
| `input_data` | `workflow.stage_inputs` | IDs, dates, flags and document types the stage is handed |
| `shared_context` | `agent_contract.shared_context` | The cross-UC allowlist (section 6) |
| `dependencies` | `dependencies.evaluate` | Every prerequisite with `satisfied`, `code` and `detail` |
| `metadata` | wrapper | `data_origin: synthetic`, orchestrator id and version |

**Refused before any UC is called** (`ContractViolation`):

| Code | When | Stage becomes |
|---|---|---|
| `MISSING_REQUIRED_FIELD` | empty `case_id`, `tenant_id` or `correlation_id`, or no actor | `failed` (not retryable) |
| `TENANT_CONTEXT_MISMATCH` | the actor's `client_id` or the canonical employee identity belongs to another tenant | `blocked` (not retryable) |

An actor without a `client_id` is accepted, as it is everywhere else in the orchestrator
(`_check_tenant`, the UC05 adapter's `_actor_for`). An actor of *another* tenant never reaches a UC.

## 3. `UCAgentResponse`

| Field | Meaning |
|---|---|
| `case_id`, `tenant_id`, `use_case_id`, `stage`, `operation` | Which case, tenant, UC and operation |
| `execution_id`, `trace_id`, `correlation_id` | The same ids as the request |
| `status` | Canonical `AgentStatus` (section 4) |
| `stage_status` | The orchestrator's precise `StageStatus` |
| `output_data` | `{result_key: value}`: the stage's result field as written to the case state, when this execution changed it |
| `updated_context` | Shared-context fields this execution changed (`employee_id`, `joining_date`, `case_id`, `employee_ref`, …) with their new values |
| `updated_fields` | Every owned field this execution changed (names) |
| `errors` | `AgentError` list (section 5) |
| `warnings` | `AgentWarning` list: `NULL_OVERWRITE_IGNORED`, `DEPENDENCY_OPTIONAL_UNMET` |
| `pending_actions` | The stage's own approvals (`kind: approval`) and blockers (`kind: blocker`) |
| `started_at`, `finished_at`, `metadata` | Timing and request metadata |

**Persisted form.** Each response is stored as the stage's `StageOutcome` in
`state.stage_outcomes[stage]` (`workflow_models.py`). The outcome holds `canonical_status`,
`execution_id`, `trace_id`, `correlation_id`, `dependencies`, `warnings` and `updated_fields`, plus
the earlier fields (`uc`, `operation`, `status`, `attempt`, `inputs`, `result_key`, `next_action`,
`error`, `data_origin`). The UC's output is never stored twice: it stays in the result field named by
`result_key`.

**Normalization the contract layer applies** (`execution._normalize_reported_items`): approvals an
adapter adds without a stage tag (UC01, UC02 planning, UC04) are tagged with the running stage. A
failure an adapter reports only through `error_code`/`error_detail` becomes the stage's recorded
error. Every response therefore carries errors and approvals the same way.

## 4. Statuses

`StageStatus` (state.py) stays the persisted, precise status. `AgentStatus` is the canonical status on
every response and in every stage-execution view.

| `StageStatus` (+ code) | `AgentStatus` | Meaning |
|---|---|---|
| `pending` | `NOT_STARTED` | The stage has not run |
| `in_progress` | `RUNNING` | Running, or re-opened for re-derivation |
| `complete` | `COMPLETED` | Done. Never executed again |
| `awaiting_approval` | `WAITING_FOR_APPROVAL` | An approval is pending (surfaced in `pending_approvals`) |
| `awaiting_approval` + `DOCUMENT_REVIEW_REQUIRED` | `REQUIRES_REVIEW` | An HR reviewer decides on documents (UC03's `hr_review_status` input) |
| `waiting` | `PENDING` | A normal wait for a lifecycle event (the joining day) |
| `waiting_for_input` | `PENDING` | Documents to supply or correct |
| `blocked` + `PREREQUISITE_NOT_MET` | `PENDING` | Waiting for an upstream stage (section 7 of the workflow doc) |
| `blocked` + `UC05_PROVIDER_UNAVAILABLE` / `UC05_UNAVAILABLE` / `DECISION_NOT_APPLIED` | `FAILED` (retryable) | A technical failure a resume or retry fixes |
| `blocked` (any other code) | `BLOCKED` | A named cause must be corrected |
| `retry_pending` | `FAILED` (retryable) | A technical failure, counted against `stage_max_attempts` |
| `uncertain` | `REQUIRES_REVIEW` | A write's outcome is unknown. An operator reconciles; never auto-retried |
| `failed` | `FAILED` | Terminal until an authorized `retry_stage` |
| `not_requested` | `SKIPPED` | The optional buddy introduction nobody requested |

**Valid transitions** (`agent_contract.transition_error`, enforced on every adapter result and, for
completed stages, on every write-through to the case store):

- A stage that has run never returns to `pending` or `not_requested`.
- A `complete` required stage only becomes `in_progress` (an explicit re-open: HR correction,
  `retry_stage`, an attendance decision that cancelled a session) or `uncertain` (a relayed write on
  its follow-up whose outcome is unknown).
- The optional `buddy_introduction` is exempt: UC05 alone decides it.

An invalid adapter result fails the stage with `INVALID_STATUS_TRANSITION` and discards the update. An
invalid write-through raises `StatusTransitionError` and nothing is stored.

## 5. Errors and warnings

`AgentError`: `code` (stable), `category`, `detail` (IDs and codes only, at most 500 characters),
`retryable`, `stage`. The category is derived from the code (`classify_error`). It is never stored
separately, so it cannot drift.

| Category | Retry? | Example codes |
|---|---|---|
| `validation` | after a person corrects the input | `MISSING_DATA`, `INVALID_INPUT`, `MISSING_REQUIRED_FIELD`, `EMPLOYEE_ID_*`, `IDENTITY_MISMATCH`, `JOINING_DATE_MISMATCH`, `DOCUMENTS_NEED_CORRECTION`, `UC05_OFFER_EVENT_MISSING` |
| `authorization` | no | `POLICY_BREACH`, `UNAUTHORIZED_ACTOR`, `TENANT_CONTEXT_MISMATCH`, `TENANT_CONTEXT_CONFLICT` |
| `dependency` | automatically, once the upstream stage completes | `PREREQUISITE_NOT_MET` |
| `business` | after the business cause changes | `SCHEDULING_CONFLICT`, `NO_AVAILABLE_SLOT`, `*_REJECTED`, `UC05_CASE_INACTIVE`, `NO_BUDDY_CANDIDATES`, `DOCUMENT_REVIEW_REQUIRED`, `HELD` |
| `infrastructure` | yes (resume / `retry_stage`), within the budget | `UC03_TECHNICAL_ERROR`, `UC05_PROVIDER_UNAVAILABLE`, `DOCUMENT_REQUEST_DELIVERY_FAILED`, `RETRY_LIMIT_REACHED`, `UC02_PACKAGE_UNIMPORTABLE` |
| `uncertain` | **never automatically** | `UNCERTAIN_OUTCOME`, `*_UNCERTAIN` (`WELCOME_DELIVERY_UNCERTAIN`, `DOCUMENT_REQUEST_DELIVERY_UNCERTAIN`) |
| `contract` | no; an operator reconciles | `UC05_CONTRACT_VIOLATION`, `STATE_OWNERSHIP_VIOLATION`, `IDENTITY_CONFLICT`, `INVALID_STATUS_TRANSITION` |
| `internal` | no | `INTERNAL_ERROR` (an adapter raised) |

Worker failures arrive classified from the worker runtime as `RETRYABLE_PROVIDER_ERROR`,
`POLICY_DENIED`, `NOT_FOUND`, `INVALID_INPUT`, `CONFLICT` or `INTERNAL_ERROR`, never with a
traceback. A worker crash or timeout during a write is `UNCERTAIN_OUTCOME`.

## 6. Shared context

`shared_context` is an allowlist (`agent_contract.SHARED_CONTEXT_FIELDS` plus derived flags). It
holds what downstream UCs need from upstream ones and nothing more:
`employee_id`, `joining_date`, `case_timezone`, `offer_reference`, `canonical_employee_id`,
`documents_verified`, `verified_document_types`, `task_plan_available`, `equipment_access_ready`,
`day_one_signed_off`. It holds no names, contact details or document contents.

## 7. Identifiers

| Identifier | Created by | Travels to |
|---|---|---|
| `correlation_id` | `OnboardingOrchestratorAgent.initial_state` (UUID4) | Every request, every worker call (`WorkerRequest.run_id`), UC03's `DocumentValidationRequest.correlation_id`, every `StageOutcome`, every orchestrator audit event, the `orchestration.stage_finished` log line |
| `trace_id` | `_invoke`: the OpenTelemetry trace id of the `orchestration.invoke` span when valid, else a fresh 32-hex id | Every stage execution of that invocation (`StageOutcome.trace_id`), the worker wire (`WorkerRequest.trace_id`), echoed in `WorkerResponse` and in the worker's failure log lines |
| `execution_id` | `execution.invoke`, per stage attempt | The same places as `trace_id`, plus `field_provenance` |
| `idempotency_key` | adapters: `<case>:<stage>:v1` (UC01, UC05 adds `:<op>`), `<case>.<stage>.v1` (UC04's pattern; `<case>.<stage>.v1.jd<n>` from joining-date revision 2 on, since UC04 refuses a key reused with a different payload), `doc-collection:<case>:r1` (document request) | The UC call |

**Propagation mechanism.** `execution_scope(request)` sets the context variable
`agent_contract.current_execution` while the adapter runs. `UcWorker.call` reads it and puts
`trace_id` and `execution_id` on the wire. The worker runtime (`workers/uc_worker_runtime.py`)
echoes them and writes them into its stderr failure lines, so a worker log line can be joined to the
stage execution that caused it. This is verified against the real UC01 worker
(`test_the_real_worker_runtime_echoes_the_execution_ids`) and the real UC04 worker
(`test_01_real_agents_run_through_the_contract_as_far_as_each_can_today`). The fields are optional
and additive, and `contract_version` stays `1.0`.

## 8. UC-specific mapping

| Stage | UC | Native entry point | Transport | Main inputs (from state) | Output field |
|---|---|---|---|---|---|
| `intake` | UC01 | `open_onboarding_case(OpenCaseRequest, OnboardingDeps)` | UC01 worker (`open_onboarding_case`) | client, actor, form and id number (intake-only, process memory) | `employee_id`, `joining_date`, `case_id` |
| `document_collection` | UC02 op A | none in UC02: orchestration adapter, UC03's `ChecklistService` (read-only) | in-process + `MockNotificationOutbox` | `employee_id`, role, location | `document_collection` |
| `document_submission` | orchestrator | none (the wait) | none | required types, submitted revisions | `document_collection.outstanding` |
| `validate_documents` | UC03 | `build_uc03_graph(...).ainvoke` per document | in-process (`asyncio.wait_for`, `uc03_timeout_s` 60 s) | per document: revision, `document_ref`, extraction metadata, HR review decision | `document_validation_result`, `validation_issues`, `correction_requirements`, `evidence_references` |
| `plan_tasks` | UC02 op B | `UC02SubAgent().run(WorkflowRequest)` | in-process | `verified_context`, role context (5 fields), `employee_id`, `joining_date` | `task_plan` |
| `equipment_access` | UC04 | `Uc04Agent().evaluate_orchestrated(OrchestratorRequest)` | UC04 worker (`evaluate_orchestrated`) | `case_id`, `employee_id` (as `employee_ref`), `joining_date`, role, client config file | `equipment_access_result` |
| `orientation_scheduling` | UC05 | `run_event(offer_accepted)` + read-only snapshot | UC05 worker | `employee_ref`, `offer_acceptance_event_id`, `joining_date` | `orientation_schedule_result`, `employee_ref` |
| `buddy_assignment` | UC05 | snapshot / `propose_buddy` | UC05 worker | `employee_ref` | `buddy_assignment_result` |
| `team_introduction` | UC05 | snapshot (UC05 drafts it after the buddy) | UC05 worker | `employee_ref` | `team_introduction_result` (+ optional `buddy_introduction_result`) |
| `day_one_readiness` | orchestrator | its own `day_one_signoff` gate | orchestrator HITL | every readiness stage's status | (status only) |
| `joining_day_welcome` | UC05 | `run_event(joining_day)` + snapshot | UC05 worker | `employee_ref`, `joining_day_event` | `welcome_message_result` |

How each adapter maps its UC's native outcome onto stage statuses is documented at the top of each
adapter module and in `HR-Onboarding-Common/docs/STATE_DRIVEN_ONBOARDING_WORKFLOW.md` (UC02-A
outcomes §2, UC03 classification table §3) and `ONBOARDING_COMMON_ARCHITECTURE.md` (UC05 §3, routing
§2.3).

## 9. Employee and tenant identity mapping

| Concept | Orchestrator field | UC01 | UC02 | UC03 | UC04 | UC05 |
|---|---|---|---|---|---|---|
| Tenant | `client_id` (= `tenant_id`) | `client_id` | `client_id` | `client_id` | `client_id` | registry tenant (actor `client_id`) |
| Case | `case_id` | issues its own (`UC01-…`); the original `CASE-…` id is kept as an alias | none | `case_id` | `case_id` (**must match `SYN-CASE-nnn`**, guardrail GR-13) | none (keyed by employee) |
| Employee | `employee_id` (UC01's `employee_no`) | `employee_no` | `employee_id` | `employee_id` | `employee_ref` (**must match `SYN-EMP-nnn`**) | not used |
| Canonical employee | `employee_ref.employee_id` | — | — | — | — | `employee_id` (its directory / HRMS id) |

Identity checks at the integration boundary:

- **Contract layer.** An actor or canonical employee identity of another tenant →
  `TENANT_CONTEXT_MISMATCH`, before any UC call.
- **State synchronization.** A changed tenant → `TENANT_CONTEXT_CONFLICT`. A known employee number
  replaced by another, or UC05's canonical employee changed after UC05 holds the case →
  `IDENTITY_CONFLICT`. The update is discarded.
- **UC01 adapter.** UC01 returns a case of another tenant → `TENANT_CONTEXT_CONFLICT`. Nothing is taken
  from it.
- **UC04 adapter.** UC04 answers for a different case id → `IDENTITY_CONFLICT`.
- **UC05 adapter (existing).** The canonical id is resolved through `config/tenants.yaml` (`shared`,
  `mapped` or `none`) and cross-checked against UC05's own directory; failures are `EMPLOYEE_ID_*`,
  `IDENTITY_MISMATCH` or `JOINING_DATE_MISMATCH`.
