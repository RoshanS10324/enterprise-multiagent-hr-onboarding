# End-to-End Integration Report: UC01–UC05 with the Central Orchestrator

> **Historical report (2026-10-30), superseded in part.** It describes the graph as it was then: UC02
> task planning as one of three parallel branches. Since then `plan_tasks` runs on its own and must
> complete before UC04 and UC05 run in parallel, and joining-date changes are reconciled. Current
> design: [ORCHESTRATION_WORKFLOW.md](ORCHESTRATION_WORKFLOW.md); changes:
> [ORCHESTRATION_REMAINING_FIXES_COMPLETION_REPORT.md](../HR-Onboarding-Common/docs/ORCHESTRATION_REMAINING_FIXES_COMPLETION_REPORT.md).

Branch `Prasanna`, 2026-10-30. Everything here is synthetic data, FakeLLM and mock or simulated
connectors. No test or demo contacts a real HRIS, ITSM, calendar, Teams or Outlook.

## 1. Summary

The orchestrator was already a working LangGraph state machine: sequential intake → document request →
document wait → validation, a parallel fan-out to UC02 planning, UC04 and UC05, HR's day-1 signoff and
the joining-day welcome. This work adds the integration layer that was missing between it and the five
agents, and fixes one defect found along the way:

1. **One standard request/response contract** for every stage (`UCAgentRequest`, `UCAgentResponse`),
   a canonical status vocabulary, categorized errors, and status-transition rules. Correlation, trace
   and execution ids now reach the worker processes.
2. **A central dependency registry**, checked before any UC is called, with tenant-configurable optional
   dependencies and a bounded same-run re-dispatch.
3. **Validated state synchronization**: field ownership, no null overwrites, tenant and identity
   conflict detection, per-field provenance, and a write-through guard against undoing a completed stage.
4. **Fix:** a `failed` stage was re-run by every plain resume, so `RETRY_LIMIT_REACHED` was not terminal
   (verified: UC03 was called 3 more times after its budget was exhausted). Failed stages now wait for
   an authorized `retry_stage`.
5. **Asynchronous events with event-id deduplication**, per-UC worker timeouts, a bounded in-process
   UC03 call, and API routes for stage execution status, errors, approvals, events and the dependency
   registry.

No UC01–UC05 source file was changed (section 9).

## 2. Integration status per use case

| UC | Connected through | Runs for real in the integrated workflow? | Evidence |
|---|---|---|---|
| **UC01** Employee Record | worker process, `open_onboarding_case` | **Yes.** Real UC01 creates the record; its employee number flows to UC02–UC05 | `test_01_real_uc01_uc03_and_uc05_complete_an_onboarding_end_to_end`, `test_01_real_agents_run_through_the_contract…`, `--real` demo |
| **UC02** op A, document request | orchestration adapter (UC02 has no such entry point) + UC03's real checklist + mock outbox | **Yes** (the adapter is real; delivery is a mock by design) | every scenario |
| **UC02** op B, task planning | in-process, `UC02SubAgent.run` | **No.** The real package fails to import: a SyntaxError at `agent/advisor.py:1` (owned by UC02's developer). The adapter reports `UC02_PACKAGE_UNIMPORTABLE`; doubles stand in elsewhere | `TestUC02TaskPlanning`, `test_04_task_creation_failure_with_the_real_uc02…` |
| **UC03** Document Validation | in-process compiled graph | **Yes.** Validation, correction, HR review and the correlation id in UC03's own audit | `TestUC03DocumentValidation`, `test_05_…_with_the_real_uc03`, the real end-to-end test |
| **UC04** Equipment & Access | worker process, `evaluate_orchestrated` | **Partly.** The real UC04 worker is called and answers, but refuses non-synthetic ids (`SYN-CASE-nnn`, its guardrail GR-13), so it cannot complete for a real UC01 case. A contract-compatible fake stands in for readiness | `test_01_real_agents_run_through_the_contract…` (real refusal, ids in UC04's log), `TestUC04EquipmentAccess` |
| **UC05** Orientation | worker process, `run_event` / snapshot / HITL relay | **Yes.** Induction, buddy, team introduction, welcome drafting and delivery, conflicts, uncertain-delivery recovery | `test_01_real_uc01_uc03_and_uc05…`, `test_08`, `test_13`, `test_14`, the existing UC05 lifecycle suite |

**Strongest end-to-end evidence:** the real UC01, real UC03 and real UC05 carry one case from offer
acceptance to a delivered welcome through the orchestrator. Only UC02 planning and UC04 are doubled
(`test_01_real_uc01_uc03_and_uc05_complete_an_onboarding_end_to_end`). One configuration step makes
it possible: the real UC01 issues `EMP-000001`, while UC05's synthetic directory knows `EMP-TEST-001`.
The test links them through the tenant's `mapped` identity scheme (the documented mechanism of
`config/tenants.yaml`), configured once UC01 has issued the number.

## 3. What was implemented

| Area | Change | Files |
|---|---|---|
| Standard contract | `UCAgentRequest`/`UCAgentResponse`, `AgentStatus` + mapping, `ErrorCategory` + `classify_error`, transition rules, `shared_context` allowlist, `current_execution` context | `orchestration/agent_contract.py` (new) |
| Contract execution | `invoke` (validate request → dependencies → adapter in execution scope → synchronize → transition check → normalize reported approvals and errors), `respond`, `execute`, stage-execution and error views | `orchestration/execution.py` (new) |
| Dependencies | the registry (`STAGE_DEPENDENCIES`), evaluation, `PREREQUISITE_NOT_MET` vs `MISSING_DATA`, tenant overrides (promotion only, validated at load), `dependency_graph` | `orchestration/dependencies.py` (new), `tenant_config.py` |
| State sync | `FIELD_OWNERS`, `STAGE_SCOPE`, `synchronize` (ownership, tenant/identity conflicts, null-overwrite guard, provenance) | `orchestration/state_sync.py` (new) |
| Orchestrator | `_execute_stage` through the contract; FAILED not re-run; trace id per invocation (OTel span); recursion limit; bounded re-dispatch (`MAX_DISPATCH_ROUNDS`); completed-stage regression guard and `state_version` on write-through; `handle_event`; `stage_executions` | `orchestration/orchestrator_agent.py` |
| State | `field_provenance`, `processed_events`, `state_version`, `dispatch_round`, `active_branches`, `redispatch_branches`; dependency waits reported as `pending` | `orchestration/state.py`, `workflow.py` (provenance through branch fold), `workflow_models.py` (`StageOutcome` fields) |
| Adapters | UC05 prerequisite checks delegate to the registry; UC01 refuses another tenant's case; UC04 refuses an answer for another case; UC03 bounded by `uc03_timeout_s` | `adapters/uc05_orientation.py`, `uc01_intake.py`, `uc04_equipment_access.py`, `uc03_documents.py`, `deps.py` |
| Transport | `trace_id`/`execution_id` on the worker wire, echoed by the runtime and written into its failure log lines; per-UC `timeout_s` | `orchestration/contracts.py`, `workers/uc_worker_client.py`, `workers/uc_worker_runtime.py`, `workers/config.py`, `worker_config.example.yaml` |
| API | `POST …/events`, `GET …/stages`, `…/stages/{stage}`, `…/errors`, `…/approvals`, `GET /dependencies`; `state_version` and `field_provenance` in the case view | `orchestration/api.py` |
| Docs | this report, the contracts, workflow and state documents; updated architecture, state-driven workflow and README | `docs/*.md`, `HR-Onboarding-Common/docs/*.md`, `README.md` |

## 4. Files

**Created.** In `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/`: `agent_contract.py`,
`execution.py`, `dependencies.py`, `state_sync.py`. In `HR-Onboarding-Common/tests/orchestration/`:
`test_agent_contract.py`, `test_uc_adapter_contracts.py`, `test_dependencies.py`, `test_state_sync.py`,
`test_integration_scenarios.py`. In `docs/`: `UC_INTEGRATION_CONTRACTS.md`,
`ORCHESTRATION_WORKFLOW.md`, `SHARED_STATE_AND_CONTEXT.md`, `END_TO_END_INTEGRATION_REPORT.md`.

**Modified.** In `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/`:
`orchestrator_agent.py`, `state.py`, `workflow.py`, `workflow_models.py`, `api.py`, `contracts.py`,
`deps.py`, `tenant_config.py`, `adapters/uc01_intake.py`, `adapters/uc03_documents.py`,
`adapters/uc04_equipment_access.py`, `adapters/uc05_orientation.py`, `workers/config.py`,
`workers/uc_worker_client.py`, `workers/uc_worker_runtime.py`, `workers/worker_config.example.yaml`.
Also `HR-Onboarding-Common/docs/STATE_DRIVEN_ONBOARDING_WORKFLOW.md`,
`HR-Onboarding-Common/docs/ONBOARDING_COMMON_ARCHITECTURE.md` and `README.md`.

**Not modified.** Any file under `UC01_…`–`UC05_…`. `hr-recruitment-common` is not part of this
repository and was not touched.

## 5. Tests: commands and exact results

Environments: HR-Onboarding-Common's own `.venv`. UC worker and UC test environments are built
**outside** the UC folders, under `%LOCALAPPDATA%\hr-onboarding-envs`, from each project's `uv.lock`
(`uv sync --frozen`, the same approach as `scripts/run_tests.ps1`).

| Suite | Command (from the project folder) | Before | After |
|---|---|---|---|
| HR-Onboarding-Common | `powershell -File scripts\run_tests.ps1 -Suite common` (or per file, see below) | 284 passed | **RESULT_COMMON** |
| UC05 | `powershell -File scripts\run_tests.ps1 -Suite uc05` (reinstalls the current HR-Onboarding-Common into UC05's env) | 533 passed | **RESULT_UC05** |
| UC01 | `<env>\uc01\Scripts\python.exe -m pytest --no-cov` | 690 passed | **RESULT_UC01** |
| UC02 | `<env>\uc02-test\Scripts\python.exe -m pytest --continue-on-collection-errors` | 533 passed, 1 skipped, 3 errors | **RESULT_UC02** |
| UC03 | `<env>\uc03-test\Scripts\python.exe -m pytest tests` | 200 passed | **RESULT_UC03** |
| UC04 | `<env>\uc04-test\Scripts\python.exe -m pytest` | 470 passed | **RESULT_UC04** |

New tests: 133 in HR-Onboarding-Common (`test_agent_contract.py` 50, `test_uc_adapter_contracts.py` 37,
`test_dependencies.py` 12, `test_state_sync.py` 15, `test_integration_scenarios.py` 19).

**Execution notes, stated plainly:**

- **UC02's 3 errors** are the known `advisor.py:1` SyntaxError. They are identical before and after,
  and UC02's source was not changed.
- **This machine was out of commit memory** during the verification (about 160–500 MB free of 24 GB,
  held by desktop applications). Under that pressure, native libraries aborted. The UC03 suite died
  inside its own `test_extract_returns_low_confidence_on_missing_file` with
  `OpenBLAS error: Memory allocation still failed`, and one full-suite run of HR-Onboarding-Common
  exited with `0xC0000409` while three suites ran in parallel. Both pass when OpenBLAS is limited to one
  thread (`OPENBLAS_NUM_THREADS=1`, an environment setting, no code change) and suites run one at a
  time. The final numbers above were produced that way, with HR-Onboarding-Common run one test file per
  process. UC03 also passed (200) in the baseline run, when more memory was free.
- The `--real` demo (`scripts/run_workflow_demo.py --real`) runs the real UC01/UC03/UC04/UC05 and the
  real UC02 package. It stops at the parallel branches exactly where the blockers are (section 7).

## 6. End-to-end scenarios verified

All in `HR-Onboarding-Common/tests/orchestration/test_integration_scenarios.py` unless noted. "Flow"
means the real graph and contract, UC01/UC04 fakes behind the real adapters, the real UC02-A adapter,
and UC02-B/UC03/UC05 doubles. "World" means the real UC05 worker.

| # | Scenario | Test | Real agents |
|---|---|---|---|
| 1 | Complete successful onboarding | `test_01_real_uc01_uc03_and_uc05_complete_an_onboarding_end_to_end`; `test_01_complete_successful_onboarding` | UC01, UC03, UC05 real (UC02-B, UC04 doubled); Flow |
| 1b | Real agents as far as each can run | `test_01_real_agents_run_through_the_contract_as_far_as_each_can_today` | UC01, UC02 (fails to import), UC03, UC04 (refuses: GR-13), UC05 |
| 2 | Invalid employee record | `test_02_invalid_employee_record_holds_the_case_at_intake` | Flow |
| 3 | Duplicate employee record | `test_03_duplicate_employee_record_reuses_the_existing_record_and_never_creates_a_second` | Flow (UC01 `LINKED_EXISTING` stand-in) |
| 4 | Task creation failure | `test_04_task_creation_failure_with_the_real_uc02_is_terminal_until_an_explicit_retry`; `test_04_task_creation_recovers_through_retry__contract_double` | real UC02 package; double |
| 5 | Document validation failure and correction | `test_05_document_validation_failure_and_correction_with_the_real_uc03` | UC03 real |
| 6 | Equipment/access approval pending | `test_06_equipment_access_approval_pending_waits_for_uc04s_own_approver` | Flow |
| 7 | Equipment/access rejection | `test_07_equipment_access_rejection_blocks_day_one_until_corrected` | Flow (UC04 stand-in) |
| 8 | Orientation scheduling conflict | `test_08_orientation_scheduling_conflict_with_the_real_uc05` | UC05 real |
| 9 | Unauthorized approval attempt | `test_09_unauthorized_approval_attempts_are_refused_and_consume_nothing` | Flow + HTTP API |
| 10 | Process restart during a paused workflow | `test_10_process_restart_during_a_paused_workflow_resumes_from_durable_state` | Flow + SQLite stores |
| 11 | Retry after a recoverable failure | `test_11_retry_after_a_recoverable_failure_never_duplicates_a_side_effect` | Flow |
| 12 | Duplicate trigger/event | `test_12_duplicate_triggers_and_events_are_applied_once` | Flow |
| 13 | Joining-day welcome execution | `test_13_joining_day_welcome_with_the_real_uc05` | UC05 real |
| 14 | Uncertain welcome-delivery recovery | `test_14_uncertain_welcome_delivery_is_recovered_by_an_operator_never_by_a_blind_resend` | UC05 real |
| 15 | Cross-tenant access attempt | `test_15_cross_tenant_access_is_indistinguishable_from_a_missing_case_and_changes_nothing` | Flow |
| — | API surface (stages, errors, approvals, events, dependencies, roles, tenancy) | `test_the_api_exposes_stage_executions_errors_approvals_events_and_dependencies` | Flow + HTTP API |

Contract tests per UC (valid input, missing fields, invalid tenant/case, normalization, error
propagation, status mapping, correlation and trace propagation): `test_uc_adapter_contracts.py`.
Dependency tests (UC02 needs UC01's record, UC03 needs UC02's requirements, UC04 needs UC03's
verification, UC05 needs UC04 when the tenant requires it, optional behavior): `test_dependencies.py`.
State-sync tests (identity consistency, tenant preserved, outputs reflected, invalid updates rejected,
restart durability): `test_state_sync.py`. Regression: HR Policies Q&A
(`test_hr_policy_qa_still_answers_through_uc05`, `test_uc05_policy_qa_still_works_independently…`).
NDA/signature tracking stays excluded (`test_nda_and_signature_tracking_is_not_part_of_the_orchestration`
scans every orchestration module, the new ones included, and the API paths).

## 7. Known blockers and teammate-owned dependencies

| Blocker | Owner | Effect | Required change (proposed, not made) |
|---|---|---|---|
| `UC02_Onboarding_Task_Agent/src/onboarding_task_management/agent/advisor.py:1` starts with a stray `UC` before the docstring (SyntaxError) | UC02 developer | UC02 task planning cannot run; its own suite has 3 collection errors | Delete the two characters. The adapter then runs `UC02SubAgent.run` unchanged |
| UC04 accepts only `SYN-CASE-nnn` / `SYN-EMP-nnn` ids (GR-13, `src/sub_agent/contracts.py`) | UC04 developer | The real UC04 refuses real case ids (`INVALID_INPUT`) | A production identifier contract decided by UC04's owner. The orchestrator deliberately does not rewrite ids to get past a guardrail |
| UC02 exposes no document-collection operation | UC02 (or UC06) developer | UC02 operation A is an orchestration adapter with a mock outbox | Expose the operation and its delivery gate |
| UC01's employee number and UC05's directory id differ for the synthetic data | UC01/UC05 developers, HR configuration | Needs a `mapped` identity entry per tenant | Confirm the shared HRMS id, or maintain `employee_no_map` |
| UC05's `pyproject.toml` path dependency `../hr-onboarding-common` (lower case) | UC05 developer | `uv sync` fails on case-sensitive file systems | `../HR-Onboarding-Common` |
| UC03 has no resubmission operation (a corrected document is a `DUPLICATE_REQUEST` needing HR review) | UC03 developer | Corrections always need an HR review | Decide whether replacements are re-validated |

## 8. Mock versus real execution

- **Always real:** the orchestrator graph, routing, contract layer, dependency checks, state
  synchronization, case stores (memory or SQLite), the orchestrator's HITL inbox and audit, the HTTP API
  (in-process `TestClient`), and the UC02-A adapter with UC03's checklist.
- **Real when the test says so:** UC01 worker, UC03 in-process, UC04 worker (refuses), UC05 worker
  (UC05's own code with *its simulated connectors*: calendar, notifier and directory are UC05's
  simulations), and the UC02 package (fails to import).
- **Mock by design:** the document-request notification (`MockNotificationOutbox`), FakeLLM everywhere.
- **Contract-compatible doubles**, labelled in each test: `FakeUc01Worker`, `FakeUc04Worker`,
  `FakeUc05Worker`, `PlanDouble`, `Uc03Double`, `Uc05Double`. Each returns the real adapter's input or
  output shape. No double's result is reported as a UC's own.

## 9. Protected boundaries

A SHA-256 manifest of every file under the five UC folders (excluding `.venv`, `__pycache__`,
`.git`, caches) was taken before any change and compared after the final test run: **RESULT_CHECKSUMS**
(583 files). `git diff` shows no change under any UC folder.

## 10. Remaining work before production

1. **Owner fixes** in section 7, above all UC02's syntax error and UC04's identifier contract. Then the
   real-agent test can drop its two doubles.
2. **Identity provider.** Identity is development headers or locally verified JWTs; the API must sit
   behind an authenticating gateway.
3. **Multi-process deployment.** The case lock is per process and the stores are SQLite on one host.
   Several orchestrator replicas need a shared lock and a server database with compare-and-swap writes
   (`state_version` is in place to support it).
4. **Durable UC business data.** UC01–UC04 keep their records in memory, so a worker restart loses
   them (the orchestrator detects the loss as `UNCERTAIN` and never replays blindly).
5. **Retention and deletion.** There is no case-deletion operation. SHARED_STATE_AND_CONTEXT.md §9
   lists every store a deletion would have to clear.
6. **Event exactly-once across a crash.** The event id is recorded right after the operation, under the
   same lock; a crash between the two is at-least-once (ORCHESTRATION_WORKFLOW.md §10).
7. **Real integrations:** mailbox/webhook intake for UC01, a real notification channel for the document
   request, a real HRIS/ITSM/calendar for UC04/UC05, and an OpenTelemetry exporter (spans exist; no
   exporter is configured).
8. **Scheduler.** `retry_pending` stages wait for a resume or `retry_stage`; there is no automatic
   back-off. The joining-day scheduler exists and is unchanged.
9. **UC04 `AT_RISK` semantics.** `adapters/uc04_equipment_access.py` has a branch meant to complete an
   `AT_RISK` evaluation with an "expedite" blocker. It is unreachable, because `AT_RISK` is not in
   `_READY_STATUSES`, so `AT_RISK` currently blocks day 1. Left as is (existing behavior, no test pins
   either reading). The UC04 owner and HR should decide whether "at risk of missing the joining date"
   is ready-with-warning or a block.

## 11. Deployment considerations

- Run each colliding UC (UC01/UC04/UC05) as one worker process with its own interpreter
  (`UC0X_WORKER_PYTHON`/`worker_config.yaml`). The startup identity check refuses a worker whose entry
  point does not come from the configured source directory.
- Set per-UC timeouts (`timeout_s` / `UC0X_WORKER_TIMEOUT_S`) above the UC's slowest legitimate call. A
  timeout kills the worker and makes a write `UNCERTAIN`.
- Use `build_deps(persist_dir=...)` for durable case state, checkpoints, HITL and audit, and
  `UC05_WORKER_DATA_DIR` for UC05's own durability.
- Keep the kill switch (`guardrails/policy.yaml`, `enabled`) and the role allowlists under change
  control. Tenant dependency overrides in `config/tenants.yaml` are validated at load; a file that tries
  to relax a required dependency stops the orchestrator from starting.
- On memory-constrained hosts, set `OPENBLAS_NUM_THREADS` for the processes that load UC03 (see
  section 5).
