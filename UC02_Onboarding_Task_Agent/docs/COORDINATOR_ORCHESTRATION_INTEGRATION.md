# UC-02 Coordinator Orchestration Integration & Verification Report

**Use Case:** UC-02 — Onboarding Task Management  
**Status:** Verified & Passing (907 unit/integration tests, 16 evaluation scenarios)  
**Target Module:** `UC02_Onboarding_Task_Agent`  
**Shared Integration Boundary:** `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/adapters/uc02_tasks.py`

---

## 1. Advisor Syntax Correction & Verification

### Historic Defect
In earlier commits on `Orchestration-Branch` (`0f5816d`), `advisor.py:1` contained a stray two-letter token (`UC"""The LLM layer...`) immediately prior to the module docstring. This caused a Python `SyntaxError` on import, which was previously documented and built around by the orchestrator authors as `UC02_PACKAGE_UNIMPORTABLE`.

### Verification & Resolution
1. On branch `logesh` (commit `dc172e8`), Logesh's clean code from `8070c76` was aligned into `UC02_Onboarding_Task_Agent/src/onboarding_task_management/agent/advisor.py`.
2. Line 1 begins cleanly with `"""The LLM layer: interpretation and recommendation only.`.
3. `py_compile` and dynamic `importlib.import_module` compile and load with zero syntax errors.
4. All 44 advisor unit tests in `test_fake_llm.py` and 60 agent loop tests in `test_agent.py` pass without error.
5. In accordance with user directives, `advisor.py` was preserved untouched.

---

## 2. Coordinator Invocation Contract & Compatibility

The Central Orchestrator interacts with UC-02 through `call_uc02_plan_tasks(state, deps)` in `uc02_tasks.py`, calling `UC02SubAgent().run(WorkflowRequest(...))`.

### Resolved Incompatibilities
1. **`llm_behaviour="offline"` Support:**
   - **Issue:** The Coordinator specifies `llm_behaviour="offline"`. Previously, `fakes.py:make_fake_llm` only accepted `("scripted", "error", "invalid", "hallucinate")` and raised `ValueError`.
   - **Fix:** Both `make_fake_llm()` and `UC02SubAgent.run()` now directly route `"offline"` to `OfflineAdvisor()`, allowing deterministic, offline rule-based execution without invoking any language model.
2. **Case-Insensitive Role Normalization:**
   - **Issue:** The Coordinator passes string roles with varying cases (e.g. `"approved_by_role": "HR_OPS"`, `actor_role="hr_ops"`). Pydantic's strict `StrEnum` validation previously rejected uppercase strings.
   - **Fix:** Implemented `Role._missing_` to normalize role strings case-insensitively without granting extra privileges. `Role("HR_OPS")` maps cleanly to `Role.HR_OPS`, while unauthorized roles remain strictly restricted.
3. **Genuine Approval Timestamps:**
   - **Rule Preserved:** Per explicit user directive, UC-02 **does not** fabricate timestamps (`utc_now()`). Preconditions strictly enforce genuine timestamps from trusted approval sources (`offer.accepted_at` and `form.approved_at`). When omitted by the caller, UC-02 raises a controlled `PreconditionError`, which the Coordinator catches and transitions to `StageStatus.BLOCKED` with `error_code="MISSING_DATA"` and `next_action="hr_review:plan_tasks:precondition_failed"`.

---

## 3. End-to-End Orchestration Flow (13 Steps)

The end-to-end flow is validated in `tests/test_coordinator_e2e.py`:
1. **Intake Trigger:** Coordinator receives onboarding request with candidate and role context.
2. **Sub-Agent Invocation:** Coordinator invokes `UC02SubAgent.run(WorkflowRequest(...))`.
3. **Context Reception:** UC-02 extracts employee, role, client, and approval context.
4. **Precondition Validation:** Asserts offer accepted with timestamp and form approved by HR role with timestamp.
5. **Dynamic Checklist Generation:** Applies multi-dimensional filtering (role, department, location, project, attributes).
6. **Task Instantiation:** Creates 10 standard onboarding tasks (e.g. `hr_record_create`, `collect_documents`, `it_laptop`, `payroll_setup`).
7. **DAG Dependency Construction:** Constructs topological dependencies (`collect_documents` depends on `hr_record_create`, `it_laptop` depends on `hr_record_create`, etc.).
8. **HR Task Ownership:** Explicit role assignments preserved (`hr_ops`, `it_manager`, `facilities`, `finance`, `employee`).
9. **UAE Calendar Due Dates:** Dates calculated forward/backward from joining date using UAE business calendar and holiday exclusions.
10. **Persistence:** State stored in `InMemoryTableClient` / `OnboardingStore`.
11. **Lifecycle Progression:** Unblocks downstream tasks upon predecessor completion with evidence.
12. **Structured Response:** Generates complete `WorkflowResult` with checklist, advice, proposals, and audit actions.
13. **Coordinator Transition:** Coordinator advances `Stage.PLAN_TASKS` to `StageStatus.COMPLETE` and records outstanding tasks.

---

## 4. Failure Handling Coverage

| Scenario | Tested Condition | Expected Result | Verified Result |
|---|---|---|---|
| **A. Invalid Employee Context** | Missing `employee_id` or `client_id`; mismatched IDs | Controlled validation error, no unhandled crash | **PASS** (`PreconditionError` / Pydantic validation error) |
| **B. Checklist Generation Failure** | Unregistered client (`CLIENT-UNKNOWN`) | Structured failure captured, no false success | **PASS** (`MissingConfigurationError`, stage blocks with `MISSING_DATA`) |
| **C. Task Creation Failure & Idempotency** | Duplicate onboarding request for same employee | Idempotent reuse of existing case without duplication | **PASS** (`created=False`, existing record returned) |
| **D. Tool Execution Failure** | Attempting to start blocked task without prerequisites | Refusal with explicit unmet prerequisite message | **PASS** (`PrerequisiteNotMetError` enforced) |
| **E. Persistence Failure** | Storage layer error (e.g. timeout / connection drop) | Controlled exception, no false success response | **PASS** (`RuntimeError` cleanly propagated) |
| **F. Precondition Failure** | Offer not accepted, form not approved, or timestamp missing | Controlled block, next action prompts HR review | **PASS** (Stage status `BLOCKED`, `MISSING_DATA`) |
| **G. Advisor / LLM Failure** | LLM timeout or hallucinated action recommendation | Fallback to `OfflineAdvisor`, marked `degraded=True` | **PASS** (`OfflineAdvisor` takes over, business rules protected) |

---

## 5. Test Commands & Verification Results

### UC-02 Test Suite
```powershell
$env:PYTHONPATH="HR-Onboarding-Common/src;UC02_Onboarding_Task_Agent/src;HR-Onboarding-Common/tests/orchestration"
python -m pytest UC02_Onboarding_Task_Agent/tests -v
```
- **Result:** **907 passed, 1 skipped, 1 warning** in 18.86s
- *Note: The 1 skipped test is `test_live_database_round_trip` which requires `UC02_RUN_DB_INTEGRATION_TESTS=1`.*

### Evaluation Suite
```powershell
python UC02_Onboarding_Task_Agent/evaluation/run_evals.py
```
- **Result:** **16 passed, 0 failed, 0 skipped** across all 16 evaluation scenarios.

### Shared Orchestration Adapter Test
```powershell
python -m pytest HR-Onboarding-Common/tests/orchestration/test_uc02_adapter.py -v
```
- **Result:** **6 passed, 0 failed** in 1.65s.

---

## 6. Remaining Limitations

1. **Persistence:** Active runtime persists in-memory via `InMemoryTableClient`. Data does not survive process exit until `PostgresTableClient` is wired to Supabase.
2. **Notification Transport:** Reminder generation creates drafts only; actual email/Teams delivery transport belongs to UC-06.
3. **Live LLM Provider:** Production deployment will require exercising `LiteLLMGateway` with live API keys in a staging environment.

