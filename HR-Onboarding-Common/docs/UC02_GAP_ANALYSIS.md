# UC02 Onboarding Task Management: Gap Analysis

Audit date 2026-10-29. Scope: `UC02_Onboarding_Task_Agent` standalone, then its integration with the
central orchestrator (`HR-Onboarding-Common/src/hr_onboarding_common/orchestration`). The audit was
read-only. No UC02 or common file was changed; this report is the only file written. Evidence is
source code (cited `file:line`) and test runs (Section 6). Existing docs are treated as claims to check, not as evidence.

Path prefixes used below: `UC02/` = `UC02_Onboarding_Task_Agent/`, `otm/` = `UC02/src/onboarding_task_management/`,
`orch/` = `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/`, `tests/orch/` = `HR-Onboarding-Common/tests/orchestration/`.

"Shim run" means the following check, run only for this audit. A Python meta-path loader read `otm/agent/advisor.py`
from disk, removed the 2 leading bytes in memory, and compiled the result. No file was written, and
`PYTHONDONTWRITEBYTECODE=1` and `-B` were set. It shows how UC02 behaves after the owner's one-line fix. It does not verify the shipped code.

---

## 1 Summary

| Area | Verdict | Basis |
|---|---|---|
| UC02 domain layer (`service`, `planner`, `tracker`, `selection`, `approvals`, `milestones`, `reminders`, `persistence`) | **Works.** 533 passed, 1 skipped | 17 importable test modules (Section 6) |
| UC02 agentic layer (`agent.*`) and the entry point `UC02SubAgent.run` | **BLOCKED** (UC02-F01) | `SyntaxError` at `otm/agent/advisor.py:1`; 3 test modules cannot be collected |
| Orchestrator `plan_tasks` stage, current state | **FAILED** on every real run, with `UC02_PACKAGE_UNIMPORTABLE` | `orch/adapters/uc02_tasks.py:53-64` |
| Orchestrator `plan_tasks` stage, after the one-line fix | **Still cannot complete.** The adapter's payload breaks UC02's real contract in 3 independent ways (F03, F04, F05) | Unpatched validation against the importable `OnboardingTrigger` / `validate_preconditions`, plus shim runs |
| Integration tests | **No test exercises real UC02 beyond the import failure.** Every other lifecycle test uses `World._plan_double`, which returns an empty plan | `tests/orch/_lifecycle.py:139-163` |

Headline facts:
- No P0 was found. No tenant leak was observed: UC02 partitions every record by `client_id` (`otm/sub_agent/service.py:231-268`).
- The blocker was introduced by commit `e4a5722` (2026-10-28, "fix: remove empty deleted file from repository"). That commit changed line 1 from `"""The LLM…` to `UC"""The LLM…`. The parent commit `4e087f9` is clean.
- The orchestrator does not import the UC02 workspace source. It imports a **non-editable copy** in `HR-Onboarding-Common/.venv/Lib/site-packages/onboarding_task_management` (`direct_url.json`: `"editable":false`). That copy is currently byte-identical to the source (`diff -rq` exit 0). A fix in UC02 does not reach the orchestrator until the copy is reinstalled (F02).
- `git -C UC02_Onboarding_Task_Agent status --porcelain` printed `?? uv.lock` before and after the audit. The file is 222,406 bytes with mtime 2026-10-29 09:39:50 +0530, so it predates this session; it was not touched. The pre-existing `.mypy_cache`, `.pytest_cache` and `.ruff_cache` folders (09:39) and `__pycache__` folders (10:53) also predate this session. At 14:47 no file in UC02 was newer than 10:53.

## 2 Code map

| Module (`otm/…`) | Role | Imports today? |
|---|---|---|
| `contracts.py`, `errors.py`, `config.py`, `audit.py`, `calendar_uae.py` | Models and enums, typed errors with `.code`, audit log, UAE business calendar | yes |
| `persistence.py` | `TableClient` protocol, `InMemoryTableClient`, `OnboardingStore` | yes |
| `providers.py`, `seed.py`, `synthetic.py` | Mock providers, a JSON file repository, synthetic clients `CLIENT-ALPHA`/`CLIENT-BETA`/`CLIENT-EMPTY` (`synthetic.py:32-34`) | yes |
| `sub_agent/checklist.py`, `selection.py`, `planner.py`, `milestones.py`, `tracker.py`, `approvals.py`, `reminders.py` | Rules engine: templates, selection, dependencies, due dates, status transitions, gates, reminder drafts | yes |
| `sub_agent/connectors/outlook/{base,mock}.py` | Mock outlook port (simulated delivery only) | yes |
| `sub_agent/service.py` | `OnboardingService` and `OnboardingTrigger`: every operation takes an `Actor` | yes |
| `agent/__init__.py` | Eagerly imports `advisor` (`:42-47`) | **no** (SyntaxError) |
| `agent/advisor.py`, `loop.py`, `tools.py`, `harness.py`, `fake_llm.py` | Advisor, fixed agent loop `UC02Agent`, tool allowlist, harness binding, fake LLM | **no** |
| `sub_agent/fakes.py`, `sub_agent/adapter.py` (`UC02SubAgent`, `WorkflowRequest`, `WorkflowResult`) | The entry point the orchestrator calls | **no** (`adapter.py:36` imports `agent.loop`) |
| `sub_agent/api.py` | UC02's own FastAPI surface | **no** (`fastapi` is absent from the audit venv; it would then hit the SyntaxError through `api.py:64`) |

Orchestrator side:
- `orch/adapters/uc02_tasks.py`: `call_uc02_plan_tasks`.
- `orch/orchestrator_agent.py`: node `plan_tasks` (`:342`), router `route_after_plan` (`:100`), node `await_plan_review` (`:112`, `:125-134`), wrapper `_stage_node` (`:271-317`).
- `orch/deps.py:45`: the `_uc02_sub_agent` cache.

**Why UC02 runs in-process.** UC01, UC04 and UC05 all ship a top-level package called `sub_agent` (checked with `ls */src`), so they collide and run as workers. UC02's top-level package, `onboarding_task_management`, is unique, so it is imported in-process (`uc02_tasks.py:3-4`; `HR-Onboarding-Common/pyproject.toml:27-28`).

Two consequences:
1. A UC02 import error surfaces inside the orchestrator process.
2. UC02's whole state lives in orchestrator memory (F09).

UC02 is also declared only in the **dev** dependency group (`pyproject.toml:36`) through a non-editable path source (`:39`).

## 3 The blocker (UC02-F01)

**Bytes and characters.** The first line of `otm/agent/advisor.py`, from `od -c`:
```
0000000   U   C   "   "   "   T   h   e       L   L   M       l   a   y
```
The file starts with `0x55 0x43` (`U`, `C`), immediately followed by the docstring's opening `"""`. The committed blob at HEAD contains the same bytes. The working copy differs only in CRLF line endings (`core.autocrlf=true`).

**Traceback** (`python -B -c "import sys; sys.path.insert(0,'src'); import onboarding_task_management.agent.advisor"`):
```
  File "...\src\onboarding_task_management\agent\__init__.py", line 42, in <module>
    from onboarding_task_management.agent.advisor import (
  File "...\src\onboarding_task_management\agent\advisor.py", line 1
    UC"""The LLM layer: interpretation and recommendation only.
      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
SyntaxError: invalid syntax
```

**Affected modules.** Each module was probed in a fresh interpreter.
- Fail with `SyntaxError`: `agent`, `agent.advisor`, `agent.loop`, `agent.tools`, `agent.harness`, `agent.fake_llm`, `sub_agent.adapter`, `sub_agent.fakes`. The lazy re-exports `sub_agent.UC02SubAgent`/`WorkflowRequest`/`WorkflowResult` (`sub_agent/__init__.py:71-93`) fail the same way.
- `sub_agent.api` fails first with `ModuleNotFoundError: fastapi`, then would hit the SyntaxError.
- These scripts also fail: `demo/uc02_agentic_workflow.py:48-57`, `evaluation/run_evals.py:45-55`.
- All 20 other modules import (Section 2).

**Affected tests.** pytest stops at collection, so it runs nothing at all (exit 2). `unittest` runs the other 534 tests and reports 3 import errors.

| Module | `def test_` count (grep) | What it covers | Result in the shim run |
|---|---|---|---|
| `UC02/tests/test_agent.py` | 60 | agent loop, tools, advisor validation, harness | 60 ran: 52 passed, 7 skipped (`hr_recruitment_common` not installed), 1 failed. The failure is `test_the_agent_package_imports_without_the_harness`, which spawns a fresh subprocess without the shim, so it can only pass after the real fix. |
| `UC02/tests/test_fake_llm.py` | 44 | fake-LLM behaviours scripted/error/invalid/hallucinate | 44/44 passed |
| `UC02/tests/testcase.py` | 40 | `UC02SubAgent`, idempotent replay, mock outlook, API | 32 passed; `ApiTests` skipped (fastapi absent) |

**Impact on `UC02SubAgent.run`.** It cannot be imported, so nothing in it runs: request validation, idempotent create, checklist, advice, agent run, reminders.

**Impact on the orchestrator.**
1. `uc02_tasks.py:54` raises `SyntaxError` from the installed copy.
2. `:55-64` catch it and set `PLAN_TASKS=failed`, `error_code=UC02_PACKAGE_UNIMPORTABLE`.
3. `route_after_plan` sends the case to `await_plan_review`, and `derive_overall_status` returns `failed` (`orch/state.py:262-263`).
4. UC03, UC04 and UC05 never run.

Because `_stage_node` re-runs any stage that is not COMPLETE (`orchestrator_agent.py:278-279`), `resume_case` re-attempts the stage once UC02 has been fixed and reinstalled.

**The one-line fix for the owner.** In `src/onboarding_task_management/agent/advisor.py`, delete the two characters `UC` at the start of line 1, so the line starts with `"""The LLM layer`. Then:
- re-run `python -m unittest discover -s tests -t .`;
- reinstall UC02 into the common venv (F02);
- fix F03–F05 in the common adapter, because the stage still will not complete after the fix alone.

## 4 Standalone functionality

| # | Capability | Status | Evidence (code · test) |
|---|---|---|---|
| 1 | Task creation and assignment | **COMPLETE** (service level) | `planner.build_tasks` (`planner.py:63-115`): owner, due date and dependencies come from templates; deterministic ids (`:33-52`). Tests: `test_onboarding.py:45-95`, `test_business_rules.py:139-187` pass. Templates exist only for the synthetic clients. |
| 2 | Dependencies and completion criteria | **COMPLETE** | `planner.recompute_readiness`/`unmet_prerequisites` (`:118-144`); prerequisite checks (`tracker.py:95-101,137-143`); evidence gate (`:145-150`). Tests: `test_planner_dependencies.py` (14), `test_business_rules.py:450-482` pass. |
| 3 | Employee and tenant association | **COMPLETE** in the service, **PARTIAL** at the entry point | Each `TaskRecord` carries `onboarding_id`/`client_id`/`employee_id` (`contracts.py:161-168`); isolation (`service.py:249-268,276-287`). Tests: `test_authorization.py:79-124`, `test_persistence.py:357-373` pass. However, `UC02SubAgent` builds the `Actor` from the request plus `trigger.client_id` (`adapter.py:178-183,196`), so it has no caller identity of its own; the orchestrator compensates. |
| 4 | Duplicate prevention | **COMPLETE** in the service, **BLOCKED** at the entry point | Live-duplicate refusal and a re-onboarding suffix after cancellation (`service.py:300-325`); store guard (`persistence.py:415-424`). Tests: `test_onboarding.py:317-336`, `test_persistence.py:197`, `test_security_regressions.py:385-414` pass. `UC02SubAgent` returning `created=False` (`adapter.py:199-208`) is tested only in `testcase.py` (blocked). In the shim run, 3 runs gave `created=False` twice and the task count stayed at 10. |
| 5 | Task status transitions | **PARTIAL** | BLOCKED↔READY is derived; READY→IN_PROGRESS→COMPLETE (`tracker.py:80-159`); a second completion is refused (`:132-135`; `test_security_regressions.py:329`); open→CANCELLED only through an approved cancellation (`service.py:664-728`; `test_cancellation.py`, 14 tests). Missing: `OnboardingStatus.COMPLETE` is never assigned anywhere (grep), there is no reopen and no hold (F16). |
| 6 | Incomplete / failed / blocked / completed handling | **PARTIAL** | Overdue state is derived (`tracker.py:162-168`); joining risk, escalations and outstanding activities (`service.py:737-946`; `test_mock_integration.py` EscalationTests, `test_business_rules.py:687`). There is no task-level FAILED status and no blocker that is not a dependency (`contracts.py:49-61`). |
| 7 | Re-planning and regeneration | **PARTIAL / INCORRECT** | Gated `request_change`→`decide_change`→`apply_change` (`service.py:467-638`) and `planner.replan` (`:195-329`). Happy paths pass: `test_replanning.py` (16), `test_persistence.py:287-345`. **The failure path corrupts the record** (F15; reproduced). |
| 8 | Persistence of task state | **PARTIAL** | The port and the in-memory client work. Restart is simulated with `snapshot`/`from_snapshot` + `hydrate` (`persistence.py:205-220`, `service.py:212-227`; `test_persistence.py` RestartTests pass). There is no real backend: `LiveDatabaseIntegrationTests` always skips (`test_persistence.py:593-611`). `_persist_audit` (`service.py:204-210`) is called only by tests. `UC02SubAgent` defaults to having no store (`adapter.py:173`). See F17. |
| 9 | Error handling and recovery | **PARTIAL** | Typed errors carry `.code` (MISSING_DATA/DUPLICATE/POLICY_BREACH/INTERNAL_ERROR, `errors.py:10-88`). A failed write rolls back (`service.py:344-358`; `test_persistence.py:405-418` pass). The agent loop's EXCEPTION/degraded paths (`loop.py:217-382`) are tested only in the blocked modules (passed in the shim run). Re-plan rollback is wrong (F15). |
| 10 | Unit and integration tests | **PARTIAL** | 533 passed and 1 skipped across 17 modules. 3 modules (144 `def test_` lines) are blocked. There are no tests against a real database or the HTTP API. |
| – | Agentic layer: advisor, loop, fake LLM | **BLOCKED** | Real tests cannot run. The shim run gave 44/44 and 52/60. The advice is **simulated** (`FakeLLMClient`, source `fake_llm:…`); there is no real LLM path in this environment. |
| – | Reminders and the outlook connector | COMPLETE (drafts) / **simulated** delivery | `test_reminders.py` (22) passes. Delivery is mock-only (`connectors/outlook/mock.py`). |

## 5 Integration with the orchestrator

### 5.1 Integration checks

| Check | Status | Evidence |
|---|---|---|
| UC02 receives the employee record and onboarding context | **INCORRECT** | `employee_id`/`joining_date` come from UC01's `case.employee_no`/`joining_date` (`uc01_intake.py:82-83`). They are left as `None` on the `LINKED_EXISTING` path (`:84-85`), and the adapter does not check them (`uc02_tasks.py:45`). Role context comes from orchestrator input, not from UC01 (`orchestrator_agent.py:391-394`). `grade`/`nationality_group`/`attributes` are never sent. Offer and form facts are hard-coded `True` (`uc02_tasks.py:75-87`). See F05, F14. |
| The adapter maps common state onto UC02's real contract | **INCORRECT** | Validated against real UC02 code **without** the shim: `approved_by_role="HR_OPS"` → `ValidationError` (F04). With `hr_ops`, the result is `PreconditionError: offer acceptance has no timestamp` (F05). In the shim run, `llm_behaviour="offline"` → `ValueError` *after* the onboarding is created (F03). |
| Results are validated before continuing | **MISSING** | `uc02_tasks.py:108-119` always sets COMPLETE. It ignores `agent_run.status` (exception/proposed), `degraded`, `review_required` and `issues` (F07). |
| Task completion or blocking controls graph routing | **MISSING** | `route_after_plan` reads only the stage status (`orchestrator_agent.py:90-100`). `node_day_one_readiness` reads only stage statuses (`:160-186`). No node reads or progresses UC02 tasks after planning (F08). |
| Failures are visible to the orchestrator and the API | **PARTIAL** | FAILED/BLOCKED, `error_code`, `stage_errors` and `stage_history` are set (`orchestrator_agent.py:299-310`) and exposed in `CaseView` (`api.py:128-153`). However, every UC02 exception becomes `BLOCKED/MISSING_DATA` (F07), no blocker is added, and `task_plan` is not in `CaseView` (F20). No API test uses real UC02. |
| A resumed workflow does not recreate tasks | **PARTIAL** | Within one process: `_skip_if_complete` (`:278-279`) plus UC02's duplicate recognition. In the shim run the stage failed, was retried, and gave `created=False` with one record. After a restart, a new `UC02SubAgent` re-creates the plan with `created=True` (shim S5), F09. |
| State survives a restart | **MISSING** (UC02 data) | `deps._uc02_sub_agent` is process memory (`deps.py:45`), and `UC02SubAgent()` has no store (`adapter.py:173`). Only the orchestrator's `task_plan` snapshot survives, through `SqliteCaseStateStore`. |

### 5.2 Contract the common layer depends on (actual UC02 code)

**Call:** `UC02SubAgent(service=None, outlook=None).run(WorkflowRequest) -> WorkflowResult` (`adapter.py:160-229`). It is synchronous and keeps state in memory for the instance's lifetime.

| `WorkflowRequest` field (`adapter.py:104-118`) | Type / allowed values | What the adapter sends (`uc02_tasks.py:97-100`) |
|---|---|---|
| `trigger` | dict validated as `OnboardingTrigger` | built dict (below) |
| `as_of` | `date`; UC02 never reads the clock itself | `joining_date` (F06) |
| `actor_id`, `actor_role` | str, `Role` | `on_behalf_of`, `Role.HR_OPS` (hard-coded) |
| `llm_behaviour` | one of `scripted`, `error`, `invalid`, `hallucinate` (`fakes.py:59`; anything else raises `ValueError`, `:69-72`) | **`"offline"`** (invalid, F03) |
| `prepare_reminders`, `outlook_approval_id` | bool, str or None | `False`, not sent |

| `OnboardingTrigger` (`service.py:86-108`) | Required | Notes |
|---|---|---|
| `client_id`, `employee_id`, `joining_date`(date), `role_id`, `department`, `manager_id`, `location`, `project` | yes | `joining_date` must fall inside the holiday-calendar coverage (`CalendarRangeError`) |
| `grade`, `nationality_group` (an opaque group code), `attributes` (tuple of flags) | no (default empty) | These drive checklist selection rules BR-02/BR-08; never sent (F14) |
| `offer: {employee_id, accepted, accepted_at: datetime, offer_reference}` | yes | `validate_preconditions` requires `accepted` and a non-null `accepted_at` (`service.py:137-144`) |
| `form: {employee_id, submitted, approved, approved_by_role: Role, approved_at, missing_fields}` | yes | `approved_by_role` must be `hr_ops` or `hr_lead`, **lower-case enum values** (`contracts.py:28-29,46`; `service.py:159-164`) |

**Output:** `WorkflowResult.as_dict()` (`adapter.py:145-157`):
- `onboarding_id` (`ONB-<client>-<employee>[-R<n>]`), `client_id`, `employee_id`, `created`, `as_of`;
- `checklist{revision, total, excluded, ready[], blocked[], in_progress[], complete[], overdue[], awaiting_approval[], review_required, issues[{code,severity,subject}], tasks[{task_id, template_key, status, owner, due_date, blocked_by, reason_included}]}`;
- `advice{…}`;
- `agent_run{run_id, status, advisor, model, degraded, degraded_reason, verified, actions_taken[], tool_calls[], exceptions[], proposals[{action, subject_id, requires_approval_by}], steps[]}`;
- `reminders{…}`, `audit_actions[]`.

**Status enums:**
- Task: `blocked | ready | in_progress | complete | cancelled` (`contracts.py:57-61`).
- Run: `observed | proposed | acted | exception` (`loop.py:57-60`).
- Onboarding: `active | complete | cancelled`, where `complete` is unreachable (F16).
- The orchestrator's `StageStatus` (`state.py:56-65`) has **no mapping** from any of these.

**Errors `run()` can raise:**
- `pydantic.ValidationError`;
- `WorkflowError`, `PreconditionError`, `MissingConfigurationError`, `CalendarRangeError` (all MISSING_DATA);
- `DataIsolationError`, `UnauthorizedActionError` (POLICY_BREACH);
- `PersistenceError` (INTERNAL_ERROR);
- `ValueError` (bad `llm_behaviour`).

`DuplicateOnboardingError` is absorbed and returned as `created=False`.

**Side effect:** when a READY task exists and `as_of` is before its due date, `run()` **starts that task**. In shim S7 the result was `status=acted`, with actions `uc02.update_task_state:start:…#hr_record_create` (`loop.py:310-382`). The returned `checklist` was computed *before* that write (`adapter.py:210` vs `:214`), so it is stale (F10, F11).

### 5.3 Edges

| Aspect | UC01 → UC02 | UC02 → UC03 |
|---|---|---|
| Input schema | `OnboardingTrigger`, built at `uc02_tasks.py:66-88` from the UC01 case (`employee_no`, `joining_date`) plus the orchestrator's `uc02_role_context`. UC01 does not carry role/department/manager/location/project. | **No data edge.** The UC03 adapter reads `uc02_role_context` and `intake_trigger`, never `task_plan` (`uc03_documents.py:61-75,90-93`). |
| Tenant and employee context | `client_id` comes from orchestrator state, and UC02's `Actor.client_id` equals `trigger.client_id` (`adapter.py:196`), so UC02's isolation check is trivially satisfied. The only caller check is the orchestrator's `require_role` against `task_planning_trigger_roles` (`guardrails/policy.yaml:51`). The UC02 key is UC01's `employee_no`, while UC05 uses the resolved `employee_ref` (F21). | Both sides take `case_id`/`client_id`/`employee_id` from orchestrator state. UC02's `onboarding_id` is never passed to UC03. |
| Validation | The orchestrator checks 5 role fields only (`:45`). UC02's own validation **currently rejects** the adapter payload (F04, F05). | UC02's output is not validated; UC03 runs even if the UC02 run reports `exception`/`proposed`/`review_required`. |
| Outputs | `task_plan{onboarding_id, created, tasks, ready, blocked}`; `outstanding_tasks = blocked`; proposals go into `pending_approvals` (`:108-118`). | UC03 results never update UC02's document-collection tasks. `outstanding_tasks` mixes UC02 task ids (`uc02_tasks.py:114`) with UC03 document types (`uc03_documents.py:155`). |
| Status enum compatibility | None. Any return becomes COMPLETE, any exception becomes BLOCKED/MISSING_DATA, and a `SyntaxError` becomes FAILED. | Routing only: PLAN_TASKS COMPLETE → `validate_documents` (`orchestrator_agent.py:100`). |
| Idempotency | UC02 ids are derived and duplicates are recognised (`created=False`); the orchestrator adds `_skip_if_complete`. Both are in-process only and lost on restart (F09). | n/a |
| Real or double exercised | **Real only up to the import failure.** There is no real happy path, BLOCKED path or replay. This audit additionally exercised the mapping against the importable real `OnboardingTrigger`/`validate_preconditions` and the shim. | Never with real UC02, which stops at FAILED. Lifecycle tests use `_plan_double` + `_docs_double`. |
| Proving test | None passes on the real path. The tests that exist pin the defect: `test_uc02_adapter.py:42-49`, `test_uc05_lifecycle.py:139-148`, `test_orchestrator_e2e.py:40-49`. | None |

### 5.4 Which tests use real UC02, and which use a double

| Test | What stands in for UC02 | What it proves |
|---|---|---|
| `tests/orch/test_uc02_adapter.py::test_known_import_defect_surfaces_as_failed_not_a_fake_success` | **Real** adapter; real import attempt of the installed UC02 copy | The SyntaxError surfaces as FAILED. It will **fail once F01 is fixed**: it then gets BLOCKED, because client `CL1` is not in UC02's registry and the payload is invalid. |
| `…::test_unauthorized_actor_is_rejected_before_import_is_attempted`, `…::test_missing_uc01_to_uc02_field_gap_blocks_before_calling_uc02` | Real adapter; UC02 is never imported | The pre-checks only |
| `tests/orch/test_uc05_lifecycle.py::test_real_uc02_defect_fails_task_planning_and_nothing_after_it_runs` (`World(real_uc02=True)`) | **Real** adapter; import failure | Downstream stages do not run. Tenant `CLIENT-TEST-A` has no UC02 config (shim S6: `MissingConfigurationError`), so this would also break after the fix. *Not run here (it starts a real UC05 worker).* |
| `tests/orch/test_orchestrator_e2e.py` (`orchestrator` fixture → `build_deps`, no override) | **Real** adapter; import failure | `test_intake_completes_and_plan_tasks_surfaces_uc02s_known_defect…` asserts FAILED. `test_day_one_signoff…` (`:128-171`) bypasses UC02 by editing `case_states[...]["stage_statuses"]` directly. The module docstring (`:6-8`) refers to a `_force_uc02_complete` helper that does not exist (grep). *Not run here (real workers).* |
| Every `World(...)` without `real_uc02=True`: `test_approval_authorization.py:39,169,177`; `test_employee_identity.py:108,135,151,160,170`; `test_joining_day_scheduler.py:146`; `test_orchestrator_api.py:39`; `test_rsvp_and_sensitive_data.py:42,150,163,168`; `test_uc05_lifecycle.py:43,52,129,180,338,435,546,581` (includes `test_uc02_precondition_failure_blocks_and_resumes_once_fixed`); `test_welcome_recovery.py:38` | **Double** `World._plan_double` (`_lifecycle.py:154-163`): COMPLETE with an empty plan `ONB-DOUBLE`, or BLOCKED/MISSING_DATA when `plan_ok=False` | Orchestrator routing and resume only. **Not UC02 verification.** The double never builds or validates a trigger, never returns tasks, and never emits proposals, `exception` or `created=False`. |
| `tests/orch/test_durability_recovery.py:75-119` | A toy graph node named `plan_tasks` | LangGraph checkpointing only; unrelated to UC02 |
| `tests/orch/test_routing.py:32-37` | None (pure routing) | `route_after_plan`: COMPLETE → documents; FAILED/BLOCKED → `await_plan_review` (13 passed) |

### 5.5 Recommended test-double strategy (common layer; no UC02 change)

1. **Split the adapter** into pure functions:
   - `build_uc02_request(state) -> dict` (trigger plus request fields);
   - `apply_uc02_result(state, result: dict) -> state`.

   `call_uc02_plan_tasks` should only resolve the sub-agent and call it. It should use `deps._uc02_sub_agent` when one is injected and import UC02 only when none is, so a double does not need the broken import.
2. **Add a contract test that runs today against real UC02 code.** Validate `build_uc02_request(state)["trigger"]` with the real `onboarding_task_management.sub_agent.service.OnboardingTrigger.model_validate` and `validate_preconditions`. Both import now. This test would have caught F04 and F05.
3. **Pin what cannot be checked yet, visibly.** Assert that `llm_behaviour` is in `sub_agent.fakes.FAKE_BEHAVIOURS`, and that the field names of the common result schema match `dataclasses.fields(WorkflowResult)`. Mark both `xfail(strict=True, reason="UC02-F01")`, so that the fix turns them into XPASS failures and forces their removal. Never use a silent skip.
4. **Build the sub-agent double from golden fixtures** recorded from the real `WorkflowResult.as_dict()` once F01 is fixed: acted, observed, proposed (`review_required`), exception (precondition failure, verification failure), and a `created=False` replay. Until then, build the double's tasks from real UC02 types: `TaskStatus` values and ids from `planner.make_task_id`.
5. **Make `_plan_double` call the real `build_uc02_request` plus the real `OnboardingTrigger` validation**, return non-empty tasks with real status values, and tag `task_plan["source"]="double"`. Results from the double must never be reported as UC02 verification.
6. **Replace the 3 defect-pinning assertions** with `xfail(strict=True)` on the import. Add a real-UC02 integration test (tenant `CLIENT-ALPHA`) that must pass once F01–F05 are fixed.

## 6 Tests

| # | Command | Verbatim result lines | What it proves / does not prove |
|---|---|---|---|
| T1 | `$env:PYTHONDONTWRITEBYTECODE="1"; Set-Location …\UC02_Onboarding_Task_Agent; & $py -m pytest -q -p no:cacheprovider` | `ERROR tests/test_agent.py` · `ERROR tests/test_fake_llm.py` · `ERROR tests/testcase.py` · `!!!!!!!!!!!!!!!!!!! Interrupted: 3 errors during collection !!!!!!!!!!!!!!!!!!!` (exit 2) | Proves F01 stops the whole pytest run. Proves nothing about behaviour. |
| T2 | `& $py -m unittest discover -s tests -t .` | `Ran 537 tests in 0.561s` · `FAILED (errors=3, skipped=1)` | 534 tests ran; the 3 errors are module-import failures. The skip is `UC02_RUN_DB_INTEGRATION_TESTS is not 1; no live database is configured`. |
| T3 | `& $py -m pytest -p no:cacheprovider --ignore=tests/test_agent.py --ignore=tests/test_fake_llm.py --ignore=tests/testcase.py -o addopts="--import-mode=importlib"` | `======================= 533 passed, 1 skipped in 7.40s ========================` | The domain layer works in memory (17 modules). It does **not** prove `UC02SubAgent`, the agent loop, a durable database or the HTTP API. |
| T4 | Shim run of `tests.test_agent`, `tests.test_fake_llm`, `tests.testcase` (in-memory 2-byte strip, no file written) | `tests.test_agent: ran=60 failures=1 errors=0 skipped=7` · `tests.test_fake_llm: ran=44 failures=0 errors=0 skipped=0` · `tests.testcase: ran=32 failures=0 errors=0 skipped=1` | The one-line fix is enough to un-block UC02's own suite (the 1 failure is the subprocess test). Forecast only, not verification. |
| T5 | `$env:PYTHONDONTWRITEBYTECODE="1"; Set-Location …\HR-Onboarding-Common; .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/orchestration/test_uc02_adapter.py` | `3 passed in 0.70s` | The adapter's pre-checks, and that the import defect is reported as FAILED. **Nothing about real UC02 behaviour.** |
| T6 | Same, `tests/orchestration/test_routing.py -o addopts="--import-mode=importlib"` | `============================= 13 passed in 0.54s ==============================` | Pure routing, including `route_after_plan` |
| T7 | Unpatched contract check: real `OnboardingTrigger.model_validate` + `validate_preconditions` on the payload shape from `uc02_tasks.py:66-88` (E2E inputs: `CLIENT-ALPHA`, `EMP-900`, joining 2026-11-16) | `as sent (HR_OPS) -> ValidationError: 1 validation error for OnboardingTrigger \| form.approved_by_role` · `lower-case hr_ops -> PreconditionError: offer acceptance has no timestamp (employee_id='EMP-900')` · `employee_id=None -> ValidationError employee_id` | **Confirms F04 and F05 against shipped UC02 code.** |
| T8 | Shim scenarios S1–S7 on `UC02SubAgent.run` | S3: `ValueError: unknown fake LLM behaviour 'offline'; expected one of ('scripted', 'error', 'invalid', 'hallucinate')`, and `records held after S3 failure: {'CLIENT-ALPHA': ['ONB-CLIENT-ALPHA-EMP-900']}`. S4: `run#2 OK created=False … overdue=8`. S5: `new UC02SubAgent (restart) OK created=True`. S6: `MissingConfigurationError: no onboarding configuration registered for client (client_id='CLIENT-TEST-A')`. S7: `run#1 created=True status=acted actions=['uc02.update_task_state:start:ONB-CLIENT-ALPHA-EMP-900#hr_record_create'] \| returned checklist in_progress=[] … \| LIVE in_progress=['hr_record_create']` | Forecasts F03, F06, F09, F10, F11 and F13 after the fix |
| T9 | Unpatched re-plan reproduction: real `OnboardingService` with a store, one template limited to Dubai, a location change to Abu Dhabi, approved, applied twice | `apply#1: MissingConfigurationError re-plan would leave the onboarding with no applicable tasks …` · `apply#2: OK drivers=[] revision=0` · `stored location after apply#2: Abu Dhabi \| stored tasks: ['dubai_only']` · `audit change.apply outcomes: [('change.apply', 'replanned')]` | **Confirms F15.** |

Not run, and why: `test_orchestrator_e2e.py` and the `World`-based suites start real UC01/UC05 worker processes, and this audit could not rule out that they write into UC folders. Their UC02 behaviour above is taken from reading the code.

## 7 Findings register

Summary:

| ID | Status | Sev | Title | Owner | Common-fixable |
|---|---|---|---|---|---|
| UC02-F01 | BLOCKED | P1 | Stray `UC` before the docstring in `advisor.py:1` makes the agent layer and `UC02SubAgent` unimportable | UC02 | no |
| UC02-F02 | INCORRECT | P2 | Orchestrator imports a non-editable, dev-group-only copy of UC02 | Common | yes |
| UC02-F03 | INCORRECT | P1 | Adapter sends `llm_behaviour="offline"`, which raises `ValueError` after the onboarding is created | Common | yes |
| UC02-F04 | INCORRECT | P1 | Adapter's default `approved_by_role="HR_OPS"` fails UC02's enum check | Common | yes |
| UC02-F05 | INCORRECT | P1 | Adapter sends no offer/form timestamps and hard-codes the approval facts; `employee_id`/`joining_date` are unchecked | Common (+UC01 data) | yes |
| UC02-F06 | INCORRECT | P2 | `as_of` is set to the joining date, which skews overdue and progress logic | Common | yes |
| UC02-F07 | PARTIAL | P2 | UC02 result is not interpreted; every exception becomes BLOCKED/MISSING_DATA | Common | yes |
| UC02-F08 | MISSING | P2 | UC02 task state never feeds routing or readiness; UC03 results never close UC02 tasks | Common | yes |
| UC02-F09 | MISSING | P2 | UC02 state is held in orchestrator memory; lost on restart and plan re-created | Common + UC02 | yes (inject a store) |
| UC02-F10 | INCORRECT | P2 | `WorkflowResult.checklist` is captured before the agent's own write (stale) | UC02 | yes (re-read) |
| UC02-F11 | Risk | P2 | The "plan" stage performs an autonomous task start as `HR_OPS` | Common + UC02 | yes |
| UC02-F12 | INCORRECT | P2 | UC02 proposals become pending approvals that have no decision path | Common | yes |
| UC02-F13 | PARTIAL | P2 | UC02 only knows the synthetic tenants; other tenants get `MissingConfigurationError` | UC02 + Common | partly |
| UC02-F14 | PARTIAL | P2 | `grade`/`nationality_group`/`attributes` are never passed, so BR-02/BR-08 tasks are never selected | Common | yes |
| UC02-F15 | INCORRECT | P1 | A failed re-plan mutates the record; re-applying the approval persists the corrupted plan | UC02 | no |
| UC02-F16 | PARTIAL | P2 | Onboarding can never become COMPLETE; there is no failed or held task state | UC02 | no |
| UC02-F17 | PARTIAL | P2 | No durable backend; audit write-through is not wired; `UC02SubAgent` has no store | UC02 | partly |
| UC02-F18 | PARTIAL | P2 | Common tests pin the defect, and the double is too thin to catch F03–F05 | Common | yes |
| UC02-F19 | INCORRECT | P3 | Docs and docstrings misstate the fix path, test counts and capabilities | Common + UC02 | partly |
| UC02-F20 | PARTIAL | P3 | API omits `task_plan`; `outstanding_tasks` mixes key spaces; no blocker on UC02 failures | Common | yes |
| UC02-F21 | Risk | P3 | UC02 is keyed by UC01's `employee_no`, but UC05 uses the resolved `employee_ref` | Common | yes |

Details:

**UC02-F01: advisor.py SyntaxError.** Confirmed defect.
- Component: `otm/agent/advisor.py` (module header), imported via `agent/__init__.py:42`.
- Observed: `SyntaxError: invalid syntax` at line 1. 9 modules, 3 test modules and 2 scripts fail.
- Expected: the module imports.
- Evidence: Section 3; T1, T2; `git show e4a5722`.
- Root cause: commit `e4a5722` added `UC` in front of `"""`.
- Impact: the entry point and agent layer are dead, and the orchestrator stage is always FAILED.
- Fix: delete the 2 characters.
- Owner: UC02. Dependency: F02 (reinstall).
- Tests: the UC02 suite with zero errors.
- Acceptance: T1 collects all 20 modules; T2 shows `errors=0`; a fresh-process import of `onboarding_task_management.agent` succeeds.
- Common-fixable: no.

**UC02-F02: stale, non-editable, dev-only UC02 install.** Confirmed defect.
- Component: `HR-Onboarding-Common/pyproject.toml:36,39`; `.venv/…/onboarding_task_management-0.1.0.dist-info/direct_url.json`.
- Observed: `"editable":false`; UC02 is only in the `dev` group; the orchestrator loads `site-packages\onboarding_task_management\__init__.py`.
- Expected: the orchestrator runs the UC02 source it is tested against, and has UC02 in its runtime dependencies.
- Root cause: path source without `editable = true`; the dependency sits in the dev group.
- Impact: the owner's fix is invisible until reinstall; a production install without the dev group raises `ModuleNotFoundError`, which `_stage_node` turns into FAILED/INTERNAL_ERROR.
- Fix: add `editable = true`, or reinstall after F01; move UC02 into a runtime extra (for example `orchestration`).
- Owner: Common (environment owner).
- Tests: a check that `onboarding_task_management.__file__` resolves under `UC02_Onboarding_Task_Agent/src`.
- Acceptance: that check passes in CI.
- Common-fixable: yes.

**UC02-F03: invalid `llm_behaviour`.** Confirmed defect (code, plus forecast in shim run S3).
- Component: `uc02_tasks.py:97-100` (`call_uc02_plan_tasks`).
- Observed: `"offline"` is not in `FAKE_BEHAVIOURS` (`fakes.py:59,69-72`). `make_fake_llm` raises at `adapter.py:212`, after `create_onboarding` (`:200`) has stored the record. The adapter then sets BLOCKED/MISSING_DATA "precondition_failed", and every retry repeats the failure.
- Expected: a supported behaviour, and a COMPLETE stage.
- Root cause: the adapter assumed an "offline" mode that `WorkflowRequest` does not offer. The OfflineAdvisor exists, but only `UC02Agent` accepts it directly.
- Impact: PLAN_TASKS can never complete; the failure is reported as an HR data problem.
- Fix: pass `"scripted"` (deterministic fake), or have the UC02 owner add an `offline` behaviour that maps to `OfflineAdvisor`.
- Owner: Common (or UC02 for the new mode).
- Tests: F18 item 3.
- Acceptance: a real `UC02SubAgent.run` from the adapter returns a `WorkflowResult`.
- Common-fixable: yes.

**UC02-F04: role-enum case mismatch.** Confirmed defect (T7, unpatched).
- Component: `uc02_tasks.py:85`.
- Observed: `"HR_OPS"` fails `ValidationError form.approved_by_role`.
- Expected: `"hr_ops"`/`"hr_lead"` (`contracts.py:28-29`).
- Root cause: enum name used instead of value.
- Impact: every real call is rejected.
- Fix: send `Role.HR_OPS.value`, or map the orchestrator's approving role.
- Owner: Common.
- Tests: F18 item 2.
- Acceptance: T7 shows a valid trigger.
- Common-fixable: yes.

**UC02-F05: missing timestamps, fabricated approval facts, unchecked ids.** Confirmed defect (T7).
- Component: `uc02_tasks.py:75-87,45`; `orchestrator_agent.py:391-394`; `uc01_intake.py:84-85`.
- Observed:
  - `offer_accepted_at`/`form_approved_at` are never placed in `uc02_role_context`, giving `PreconditionError: offer acceptance has no timestamp`;
  - `accepted`, `submitted` and `approved` are hard-coded `True`;
  - `employee_id`/`joining_date` can be `None` after UC01 returns `LINKED_EXISTING`, giving `ValidationError`.
- Expected: values taken from UC01's verified acceptance and form approval.
- Root cause: UC01 → UC02 field gap bridged with constants.
- Impact: the real contract is rejected; and if bypassed, UC02's preconditions become a rubber stamp.
- Fix: carry the acceptance and approval timestamps and approver role from the UC01 result or intake into state; check all 8 required trigger fields before calling.
- Owner: Common (UC01 adapter and initial state). Dependency: UC01 must expose the timestamps (UNVERIFIED whether its result carries them).
- Tests: a contract test with real `validate_preconditions`; a `LINKED_EXISTING` case.
- Acceptance: a valid trigger built from E2E inputs; a missing id gives BLOCKED with a named field.
- Common-fixable: yes.

**UC02-F06: `as_of` semantics.** Confirmed defect (forecast in shim S4 vs S7).
- Component: `uc02_tasks.py:96`.
- Observed: `as_of=joining_date`, so 8 of 10 tasks are overdue at plan time and no task is progressed. The fallback `date.today()` is a clock read.
- Expected: the evaluation date, meaning the case clock in the tenant timezone.
- Impact: wrong overdue/risk data and wrong advice.
- Fix: pass the current case date.
- Owner: Common.
- Tests: an assertion that `overdue==0` on a fresh plan.
- Acceptance: the shim scenario S7 is reproduced through the adapter.
- Common-fixable: yes.

**UC02-F07: result not interpreted; error codes flattened.** Confirmed defect (code).
- Component: `uc02_tasks.py:101-119`.
- Observed:
  - COMPLETE is set regardless of `agent_run.status` (`exception`/`proposed`), `degraded`, `review_required` or `issues`;
  - the broad `except Exception` maps POLICY_BREACH, INTERNAL_ERROR and the adapter's own bugs to BLOCKED/MISSING_DATA with retryable=true (`orchestrator_agent.py:303-305`);
  - no blocker is added.
- Expected:
  - `exception` → BLOCKED or FAILED with UC02's reason;
  - `review_required` → AWAITING_APPROVAL or BLOCKED;
  - use `exc.code` (`errors.py`);
  - an adapter defect → FAILED/INTERNAL_ERROR.
- Impact: unsafe progression, and HR is pointed at the wrong fix.
- Fix: `apply_uc02_result` with an explicit mapping table.
- Owner: Common.
- Tests: golden fixtures per run status (Section 5.5, item 4).
- Acceptance: each UC02 run status maps to a defined stage status.
- Common-fixable: yes.

**UC02-F08: tasks do not drive the workflow.** Confirmed gap (code).
- Component: `orchestrator_agent.py:90-100,160-186`; `uc03_documents.py`.
- Observed: no node reads `status`, `joining_risk` or `joining_handoff` (`service.py:732-990`) after planning. Day-1 readiness can be proposed while every UC02 task is still open. UC03 results never complete the UC02 document task.
- Expected: readiness includes UC02 open, critical and overdue tasks, and UC03 evidence closes the matching UC02 task.
- Impact: Day-1 sign-off is not informed by the task plan.
- Fix: a UC02 status read inside `day_one_readiness`; an optional `complete_task(evidence_ref)` relay.
- Owner: Common. Dependency: F01, F09.
- Tests: readiness BLOCKED while a critical UC02 task is open.
- Acceptance: as stated.
- Common-fixable: yes.

**UC02-F09: no UC02 durability across restarts.** Confirmed gap (code, shim S5).
- Component: `deps.py:45`; `uc02_tasks.py:94-95`; `adapter.py:173`.
- Observed: process memory only; a restart loses task progress and approvals, and a re-run reports `created=True`.
- Expected: UC02 state survives restart, and a resume finds the existing plan.
- Fix: inject `UC02SubAgent(service=OnboardingService(build_registry(), store=OnboardingStore(<durable TableClient>)))` and call `hydrate(client_id)`. `TableClient` is a Protocol (`persistence.py:105-126`), so the common layer can supply a SQLite implementation.
- Owner: Common, with UC02 providing a production adapter later (F17).
- Tests: restart while PLAN_TASKS is BLOCKED and while it is COMPLETE.
- Acceptance: `created=False` and task statuses preserved after a restart.
- Common-fixable: yes.

**UC02-F10: stale checklist.** Confirmed defect (code `adapter.py:210` vs `:214,222`; shim S7).
- Observed: the returned `in_progress=[]` while the live record has `hr_record_create` in progress.
- Expected: the result reflects the state after the run.
- Fix: rebuild the view after `agent.run`. Common-side workaround: re-read with `sub_agent.service.checklist(...)`.
- Owner: UC02.
- Tests: a run that acts, then a check that the checklist shows the task in progress.
- Acceptance: as stated.
- Common-fixable: yes (workaround).

**UC02-F11: a write during planning.** Risk (inferred; shim S7).
- Component: `loop.py:310-382`; `uc02_tasks.py:98-99`.
- Observed: `run()` starts the first READY task, under actor `on_behalf_of` with a hard-coded `Role.HR_OPS`, whatever the orchestrator roles are (`hr_onboarding_officer` and others).
- Expected: an explicit policy on whether planning may act.
- Impact: task starts nobody requested, wrongly attributed in the audit.
- Fix: map the actor's role explicitly; decide (and document) whether to allow `progress_ready_task` in this stage.
- Owner: Common + UC02.
- Common-fixable: yes.

**UC02-F12: unresolvable proposals.** Confirmed defect (code).
- Component: `uc02_tasks.py:115-118`; `loop.py:85-91,258-267`.
- Observed: each UC02 `AgentProposal` (no request id) becomes a pending approval with `reference=action` (not unique), and `role="hr_ops"`, which is not an orchestrator role. The orchestrator refuses to decide UC-owned approvals, and UC02 has no inbox entry for these proposals, so they sit pending forever. The stage is COMPLETE anyway.
- Fix: surface proposals as blockers (`review_required`), or open the real UC02 gate (`request_change`) through a relay.
- Owner: Common.
- Tests: a golden "proposed" fixture.
- Common-fixable: yes.

**UC02-F13: tenant configuration.** Confirmed (shim S6; `synthetic.py:32-34,485`; `checklist.py:498-510`).
- Observed: `UC02SubAgent()` uses only the synthetic registry; `CLIENT-TEST-A` (the lifecycle tenant) gets `MissingConfigurationError`.
- Fix: a per-tenant config registry injected through `service=`.
- Owner: UC02 (real client configs) + Common (injection).
- Common-fixable: partly.

**UC02-F14: selection dimensions dropped.** Confirmed gap (code `orchestrator_agent.py:391-394`; `service.py:100-107`).
- Observed: grade, nationality group and attributes are never sent, so visa, medical, travel and similar templates never apply.
- Fix: add these fields to `OnboardingCaseInput` / `uc02_role_context`.
- Owner: Common.
- Common-fixable: yes.

**UC02-F15: re-plan failure corrupts the plan.** Confirmed defect (T9, unpatched).
- Component: `planner.replan` (`planner.py:230-261`); `service.apply_change` (`service.py:561-638`).
- Observed: location/project are assigned (`:237-242`) before the empty-template check raises (`:255-261`). The in-memory record keeps the new location while the store keeps the old one; the failure is not audited. Re-applying the same approval finds no drivers (`:226-228`), persists the mutated record, and audits `replanned`.
- Expected: validate on a copy; leave the record untouched on error; audit the failure; consume the approval once applied.
- Impact: plan and audit integrity loss (single tenant).
- Fix: compute templates before mutating, or restore on exception; mark the approval consumed.
- Owner: UC02.
- Tests: the T9 scenario as a regression.
- Acceptance: after a failure, the in-memory record equals the stored one, and a re-apply is refused.
- Common-fixable: no.

**UC02-F16: incomplete lifecycle.** Confirmed gap (grep: there is no `OnboardingStatus.COMPLETE` assignment; `contracts.py:49-61,101-104`).
- Observed: an onboarding stays `active` forever; there is no failed or held task state.
- Fix: an `active → complete` transition when all non-cancelled tasks are complete (or on the `joining_confirmation` gate); add a hold reason if needed.
- Owner: UC02.
- Common-fixable: no.

**UC02-F17: persistence is in-memory only.** Confirmed gap (`persistence.py:28-34,129-220`; `test_persistence.py:593-611`; `_persist_audit` has no production caller, `service.py:204-210`).
- Fix: a real `TableClient` adapter; call `_persist_audit` after each operation; give `UC02SubAgent` a store.
- Owner: UC02 (the common layer may supply a TableClient, see F09).
- Common-fixable: partly.

**UC02-F18: tests pin the defect, and the double is too thin.** Confirmed (Section 5.4).
- Observed: 3 tests assert `FAILED/UC02_PACKAGE_UNIMPORTABLE` and will break when F01 is fixed. `_plan_double` bypasses the mapping, which is why F03–F05 were never caught.
- Fix: Section 5.5.
- Owner: Common.
- Acceptance: the contract test from Section 5.5 item 2 is green, and the xfails flip when F01 is fixed.
- Common-fixable: yes.

**UC02-F19: inaccurate docs.** Confirmed.
- `ONBOARDING_COMMON_ARCHITECTURE.md:582` and the `uc02_tasks.py:6-12` docstring imply that removing the 2 characters makes the stage run; it does not (F02–F05).
- `test_orchestrator_e2e.py:6-8` references a nonexistent `_force_uc02_complete`.
- `HR_ONBOARDING_ORCHESTRATION_AGENT_ANALYSIS.md:628` treats `created=False` as the primary guarantee; that holds in-process only.
- UC02 `CLAUDE.md:13` says "no FastAPI app", but `sub_agent/api.py` exists. `CLAUDE.md:38` says "257 tests"; `README.md:145-156` says 594 tests OK; the measured count is 534 runnable plus 3 erroring modules.
- Fix: correct each claim.
- Owner: Common (common docs) + UC02 (UC02 docs).
- Common-fixable: partly.

**UC02-F20: API visibility.** Confirmed (`api.py:128-180`; `uc02_tasks.py:114`; `uc03_documents.py:155`; `orchestrator_agent.py:167`).
- Observed: `CaseView` has no `task_plan`; `outstanding_tasks` mixes UC02 task ids, UC03 document types and stage names; UC02 failures add no blocker.
- Fix: a typed `tasks` section, separate lists, and blockers.
- Owner: Common.
- Common-fixable: yes.

**UC02-F21: employee key mismatch.** Risk (inferred).
- Observed: the UC02 trigger uses `state["employee_id"]` (UC01 `employee_no`, `uc01_intake.py:82`), while UC05 resolves the canonical `employee_ref` (`uc05_orientation.py:218-246`). Records may not correlate across UCs when the tenant maps identities.
- Fix: pass the resolved canonical id to UC02.
- Owner: Common.
- Common-fixable: yes.

## 8 Required actions

| Order | UC02 owner | Common layer |
|---|---|---|
| 1 | **F01**: delete `UC` at `advisor.py:1`; re-run T1/T2 (expect 0 errors) | **F02**: make UC02 an editable runtime dependency, or reinstall after F01; assert the import path |
| 2 | **F15**: validate-before-mutate in `replan`; audit failures; one-shot approvals | **F03/F04/F05**: valid `llm_behaviour`, lower-case role, real timestamps and approval facts, check required ids, so that T7 passes |
| 3 | **F10**: build the checklist after `agent.run` | **F18**: split the adapter; contract test against the real `OnboardingTrigger`; xfail(strict) pins; a richer `_plan_double` labelled as a double |
| 4 | **F16**: onboarding completion transition | **F06/F07/F12**: `as_of` = case date; interpret the run status and `review_required`; map `exc.code`; no orphan approvals |
| 5 | **F17**: production `TableClient`, audit write-through, a store in `UC02SubAgent` | **F09**: inject a durable `OnboardingService`, call `hydrate` on start |
| 6 | **F13**: real per-tenant configs and a registry seam | **F08/F14/F20/F21**: UC02 tasks in readiness, the UC03→UC02 evidence relay, selection dimensions, API visibility, canonical employee id |
| 7 | **F19**: fix the CLAUDE.md/README counts and the FastAPI claim; decide whether to commit or ignore `uv.lock` | **F19/F11**: fix the common docs; decide the policy for writes during planning |

UC02 can be considered integrated only when a real-UC02 test (not a double) passes for:
- tenant `CLIENT-ALPHA` reaching PLAN_TASKS=COMPLETE with a non-empty plan;
- a replay returning `created=False`;
- a restart that preserves task status;
- a UC02 `exception` run mapping to a non-COMPLETE stage.
