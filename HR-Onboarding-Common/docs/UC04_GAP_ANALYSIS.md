# UC04 Equipment & Access Readiness: Gap Analysis

Audit date 2026-10-29. Read-only audit: no source, test or config was changed. Evidence is source read (`file:line`), test runs and one read-only probe script that drives the **real** orchestrator UC04 adapter against a **real** UC04 worker process (`C:\Users\ajaym\AppData\Local\Temp\claude\c--Studies-Projects-HR-Onboarding-Agent\4c74be37-a994-4d47-bd20-bc79e0c23931\scratchpad\probe_uc04.py`, output `...\scratchpad\probe_out.txt`). Earlier docs were checked as claims, not used as evidence. Paths: `UC04/` = `UC04_Equipment_Access_Integration_Ready/`, `ORCH/` = `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/`, `TESTS/` = `HR-Onboarding-Common/tests/orchestration/`.

## 1. Summary

| Area | Verdict |
|---|---|
| UC04 standalone business rules | **Mostly COMPLETE for a local simulation.** Matrix allowlist and exception rules, approval validity, the state machine, readiness precedence, rejection and failure handling, rollback and idempotency are real logic and tested (470 tests pass). Everything outside the rules is simulated: approvals, tickets, provisioning and delivery evidence all arrive as caller data. The parent case registry and IT desk are in-memory mocks, and nothing is persisted. |
| UC04 MCP | **DISCONNECTED (unused scaffolding).** `mcp/server.py` is imported only by `UC04/tests/test_mcp.py`. It bypasses UC04's Policy/ToolGateway. |
| Orchestrator integration | **Not functional on the real path.** Real orchestrator identifiers fail UC04 validation (`INVALID_INPUT`) or policy (`POLICY_DENIED`), so the stage is always FAILED. With UC04-shaped ids it still cannot reach READY: no request items, no approvals, no evidence channel and no submit op. Resuming after any payload change fails on an idempotency conflict. The adapter treats UC04 `IN_PROGRESS` (approved, nothing provisioned) as COMPLETE, which opens UC05 scheduling. |
| Test evidence for integration | Real UC04 is called only by direct adapter or worker tests that use UC04's own `SYN-*` fixture ids. Every graph-level lifecycle test uses `FakeUc04Worker`, and the e2e test sets EQUIPMENT_ACCESS to COMPLETE by hand. No test drives real UC04 through the graph. |
| Prescribed standalone test command | **BLOCKED.** The given uc04 interpreter has no pytest. The suite was run with the Common interpreter instead: 470 passed. |

Findings: 18 in total (P1: 7, P2: 6, P3: 5). Section 8 lists them all.

## 2. Code map

| Layer | File : symbol | Role |
|---|---|---|
| UC04 contracts | `UC04/src/sub_agent/contracts.py:14-21` (id patterns), `:152-185` `Uc04Input`, `:188-196` `CallerContext`, `:302-327` `Uc04Result` | Strict pydantic, `extra=forbid`; identifiers must be `SYN-CASE-\d{3,}`, `SYN-EMP-…`, `SYN-USER-…`; `client_id`/`role_id` must be `^[a-z][a-z0-9_]{1,40}$` |
| UC04 enums | `UC04/src/sub_agent/states.py:28-44` ItemState (16), `:47-54` CaseStatus (7) | |
| UC04 service (all rules) | `UC04/src/sub_agent/service.py:76-124` `evaluate`, `:174-297` `_assess`, `:401-434` `_apply_transitions`, `:465-503` `submit_eligible` | Single decision point (both entry points call it) |
| Rules | `rules.py:27-40` start gate, `:51-88` config gaps; `items.py:57-215` per-item evaluation; `approvals.py:21-79`; `evidence.py:20-46`; `lifecycle.py:9-114`; `readiness.py:8-26`; `risk.py` | |
| State | `UC04/src/sub_agent/store.py:41-84` `CaseStore` (in memory) | Dedupe, transitions, idempotency cache |
| Entry points | `direct.py` `DirectEntry`; `orchestrated.py:70-102` `OrchestratedEntry`; `agent.py:50-261` `Uc04Agent` facade | Worker uses `Uc04Agent.evaluate_orchestrated` (`agent.py:230-232`) |
| Policy | `UC04/policy/policy.py:54-58` `check_caller`, `:77-82` `ToolGateway.call`; `UC04/policy/uc04_policy.json:9-14` | Role allowlist `hr_onboarding_officer`, `hiring_manager`, `onboarding_orchestrator` |
| Mocks | `adapters/it_service_desk_mock.py`, `case_context_mock.py`, `approval_mock.py`; `fixtures.py:133-137` `new_environment`; `UC04/data/*.json` | |
| Sibling packages (not in wheel) | `UC04/guardrails/`, `UC04/policy/`, `UC04/connectors/`, `UC04/mcp/`, `UC04/data/` | `src/sub_agent/guardrails.py:2` and `policy.py:2` do `from guardrails.guardrails import *` and `from policy.policy import *`; wheel ships only `src/sub_agent` (`pyproject.toml:54`) |
| Made importable by | UC04 pytest `pythonpath = ["src","tests","."]` (`pyproject.toml:58`); worker `sys.path.insert(0, src_dir.parent)` (`ORCH/workers/uc_worker_runtime.py:590-591`) | Probe: `import sub_agent` under the uc04 interpreter without repo root → `ModuleNotFoundError: No module named 'guardrails'` |
| Orchestrator adapter | `ORCH/adapters/uc04_equipment_access.py:38-119` `call_uc04_equipment_access` | Builds `Uc04Input`, calls worker, maps status |
| Worker handler | `ORCH/workers/uc_worker_runtime.py:123-157` `_Uc04Handler` (only op `evaluate_orchestrated`); `:52-64` `_classify` | Registers parent case into UC04's mock registry (`:133-139`) |
| Graph | `ORCH/orchestrator_agent.py:101-102` routes, `:114` `await_access_approval`, `:344` node, `:271-319` `_stage_node`, `:584-639` `decide_approval` | |
| State | `ORCH/state.py:250-251` `idempotency_key`, `:45-48` READINESS_STAGES | |
| Tests | `TESTS/test_uc04_adapter.py`, `test_durability_recovery.py`, `test_module_isolation.py`, `_lifecycle.py:100-116` `FakeUc04Worker`, `test_orchestrator_e2e.py` | See §7 |

## 3. Standalone functionality

| # | Capability | Status | Evidence |
|---|---|---|---|
| 1 | Equipment/access requirements | **Evaluation COMPLETE; generation MISSING (by design)** | UC04 checks caller-supplied `requested_access`/`requested_equipment` against role matrices (`items.py:60-63`); out-of-matrix requires an exception (`items.py:63-69`). It never derives the list from the role (spec inputs `specs/UC04_SPEC.md:47`). Client-site required items are only flagged (`risk.py:46-51`). Tests: `test_rules.py:51-130`. |
| 2 | Client-specific readiness config | **COMPLETE (rules); config source is caller payload** | Tri-state `ConfigBlock` (`config_state.py`); scope mismatch → blocking gap (`rules.py:54-55`); missing/empty → blocking, never defaulted (`rules.py:57-97`); client overrides that conflict with guardrails are ignored (`service.py:361-368`). Tests: `test_rules.py:111-217`, `test_approvals.py:75`. |
| 3 | Employee/tenant/case association | **COMPLETE (against a mock registry)** | Parent case must exist; employee and client must match; one case per employee (`service.py:126-141`); request id = `case_id:request_key` (`service.py:50-51`). Tests: `test_failures.py:55,164,170`, `test_two_paths.py:43-66`, `test_approvals.py:35`. |
| 4 | Access/equipment state transitions | **COMPLETE** | `ALLOWED` table and `path()` (`lifecycle.py:9-29,78-114`); an illegal or backward move keeps the stored state and raises a review blocker (`service.py:405-415`); CANCELLED is terminal. Tests: `test_lifecycle.py` (all), `test_readiness.py`. |
| 5 | Approval requirements / authorized decisions | **Rules COMPLETE; approver authenticity MISSING** | Approver per class with delegate or HR escalation (`approvals.py:21-30`); validity = role, item, scope, not future-dated (`approvals.py:40-53`); a same-day tie fails safe to REJECTED (`approvals.py:61`). `approver_id` is never verified, and there is no requester ≠ approver check (`requested_by` is unused outside contracts). Tests: `test_approvals.py`, `test_rules.py:60-88`. |
| 6 | Identity/actor mapping | **COMPLETE locally; SYN-only** | Role allowlist and kill switch (`policy.py:54-58`); the actor "never influences a business decision" (`contracts.py:189`); real-looking ids are refused (`test_failures.py:37`). Tests: `test_failures.py:175-187`. |
| 7 | Conflict / rejection / unavailable / failure | **COMPLETE** | Idempotency conflict (`service.py:90-94`); IT rejection → `REJECTED_CORRECTION_REQUIRED`, later updates ignored (`items.py:158-167`, `evidence.py:31-32`); unavailable approver (`items.py:95-99`); adapter failure is recorded with no blind retry until a person acknowledges (`service.py:480-494,505-512`); faulty or wrong equipment (`items.py:197-200`); provisioning failed (`items.py:185-188`). Tests: `test_failures.py:89-130`, `test_readiness.py:68-136`. |
| 8 | Persistence / recovery | **PARTIAL** | Rollback on any failure (`service.py:103-119`, `store.py:58-73`) is tested (`test_failure_atomicity.py`, 10 tests). Durable store MISSING (`store.py:1-2`). |
| 9 | Mock ITSM vs business rules | **Rules real, data simulated** | The desk is called only through ToolGateway with `approved=True` hard-coded (`service.py:484-486`); the real gate is the ELIGIBLE item state. Ticket status and evidence are not read from any desk; they come in the input (`evidence.py:20-40`; acknowledged in `docs/INTEGRATION_BOUNDARIES.md:17`). The mock desk is idempotent per request id (`it_service_desk_mock.py:18`). |
| 10 | Tests, success and failure provisioning | **COMPLETE (standalone, mocks)** | READY `test_readiness.py:8`; not-ready and failure paths `test_readiness.py:17-136`; submission and adapter failure `test_failures.py:95-130`; withdrawal `test_failures.py:132-160`. |

## 4. Client configuration, identity and authorization

| Aspect | Standalone UC04 | Through orchestrator |
|---|---|---|
| Config source | `Uc04Input.config` (caller payload), fixtures `UC04/data/client_*_config.json` | JSON file at `state.uc04_client_config_ref`, a free path from the API start body (`ORCH/api.py:73`), read by the orchestrator process (`adapter:39-47`). Not bound to the tenant (no UC04 entry in `ORCH/tenant_config.py`). |
| Cross-tenant config | Refused (`CONFIG_SCOPE_MISMATCH`, `rules.py:54-55`) | Probe P9: bravo config on an alpha case → BLOCKED `CONFIG_SCOPE_MISMATCH` (UC04 defends). |
| Arbitrary file via config ref | n/a | Probe P11: `config_ref` = `UC04/policy/uc04_policy.json` → `error_detail` contains that file's content (`input_value={'policy': 'uc04-equipmen..._desk.submit_request']}}`). `stage_errors` is returned by `CaseView` (`api.py:141,168`). Probe P12: a missing path gives a distinct `MISSING_DATA`, so existence can be tested. |
| Actor id | `SYN-USER-\d{3,}` (`contracts.py:192`) | Verified actor `user_id` (`api.py:225-226`), e.g. `user:hr-admin-01` → `INVALID_INPUT: actor_id` (probe P2b) |
| Roles | allowlist `uc04_policy.json:9` | Placeholder actor role `hr_admin` (`orchestrator_agent.py:383`); test actors use `hr_admin` (`TESTS/_lifecycle.py:44`, `TESTS/_helpers.py:60`) → `POLICY_DENIED` (probe P2) |
| Case/employee/tenant ids | `SYN-CASE-`, `SYN-EMP-`, lowercase client | UC01 case id is a uuid4 (`UC01/src/sub_agent/models/common.py:5-6`); employee number is `EMP-%06d` (`UC01/.../adapters/memory.py:221`); orchestrator case id is `CASE-<hex>` (`orchestrator_agent.py:415`); tenant e.g. `CLIENT-ALPHA` → 3 validation errors `case_id, employee_ref, client_id` (probe P1) |
| Parent case registry | Mock preloaded from `UC04/data/parent_cases.json` | Same mock (`agent.py:73` → `fixtures.py:134`). The worker registers the case from the same input it then validates (`uc_worker_runtime.py:133-147`), so BR-02 cannot fail on the first call. Fixture ids collide: probe P8 (`SYN-CASE-001`) → CaseMismatchError, P8b (`SYN-EMP-002`) → DuplicateCaseError, both FAILED. The parent status is never updated, so WITHDRAWN, ON_HOLD and CANCELLED are unreachable. |
| Tenant on actor | UC04 `Actor` has no `client_id` | Worker drops `actor.client_id` (`uc_worker_runtime.py:152`); tenant ownership rests on the orchestrator (`orchestrator_agent.py:456-461,509-511`) plus UC04's first-registration lock (probe P10: same case id under another client → CaseMismatchError). |
| Approval provenance | Approval records and evidence are caller-asserted (role strings only) | No approval or evidence channel exists (F05). Config, including approver and evidence roles, is chosen by the API caller via path. |

## 5. Mock / MCP / tool integration status

| Component | Status | Evidence |
|---|---|---|
| `ITServiceDeskPort` + `MockITServiceDesk` | Functional mock standalone; **DISCONNECTED** in orchestration (no submit op) | `service.py:484`; worker ops `uc_worker_runtime.py:145-157`; probe P13 `uc04: unknown op 'submit_eligible'` |
| `CaseContextPort` + `MockCaseContext` | Functional mock; used by the orchestrator via a private attribute | `uc_worker_runtime.py:131,137` (`_cases._cases[...]`) |
| `MockApprovalDesk` | Tests and demo only | `adapters/approval_mock.py` |
| `mcp/server.py` `MCPServer`, `mcp/tools.py` | **Unused scaffolding**: only `tests/test_mcp.py` imports it (grep). `execute_tool` trusts a `has_approval` flag (`server.py:61,70-74`) and never consults `Policy`/`ToolGateway`; `outlook.send_notification` is classed `read` (`tools.py:88`) and is not in the policy allowlist (`uc04_policy.json:10-14`). The top-level name `mcp` would shadow the official MCP SDK if one were installed. | |
| `MockOutlookConnector` | `Uc04Agent.notify_readiness` only; not on the worker path | `agent.py:180-227` |
| `FakeLlmAdvisor` | Advisory only; not on the worker path | `agent.py:106-128` |
| Policy / ToolGateway | Functional: `check_caller` runs on every evaluate | `service.py:82-85` |
| Real ITSM / provisioning / identity / DB | MISSING (documented) | `ports.py:1-2`, `store.py:1-2`, `docs/INTEGRATION_HANDOVER.md:109` |
| Orchestrator policy tool `uc04.get_case_status` | DISCONNECTED: declared, no such op | `HR-Onboarding-Common/guardrails/policy.yaml:21` |

## 6. Integration contract with the orchestrator

### 6.1 Input mapping (`adapter:48-73`, worker `:146-155`)

| UC04 field | Source | Result |
|---|---|---|
| `case_id`, `employee_ref`, `client_id` | `state.case_id` (UC01 uuid), `state.employee_id` (UC01 `EMP-…`), `state.client_id` (tenant) | Pattern mismatch → FAILED (F01) |
| `role_id` | `uc02_role_context.role_id`, else `config.role_id` | OK only if it equals the config's role; else `CONFIG_SCOPE_MISMATCH` |
| `joining_date` | `state.joining_date` (UC01) | OK |
| `offer_accepted`, `onboarding_form_approved`, `form_version` | Hard-coded `True`, `True`, `"v1"` (`adapter:52`) | UC04 start gate and form-version reassessment bypassed (F07) |
| `work_location`, `work_mode`, `client_site_required` | `role_ctx` keys nobody sets → `"Office"`, `OFFICE`, `False` | Intake `location`/`work_location_type` ignored (F07) |
| `requested_access`, `requested_equipment`, `approval_records` | `role_ctx` keys nobody sets (`orchestrator_agent.py:391-394`; not in `OnboardingCaseInput` `contracts.py:20-63` or `StartCaseRequest` `api.py:54-77`) | `[]` → `NO_ITEMS_REQUESTED` → NEEDS_HUMAN_REVIEW (probe P3) (F03, F05) |
| tickets / provisioning / delivery / collection / clearance | Not sent | READY unreachable (F03) |
| `as_of_date` | `role_ctx.as_of_date`, else server `date.today()` (`adapter:58`) | Changes the payload hash daily (F04) |
| `config` | JSON at the caller-supplied path | F09 |
| caller | `state.actor` → `on_behalf_of`, `roles`; `run_id` = `correlation_id`; key = `{case_id}.equipment_access.v1` (`adapter:66`, `state.py:250-251`) | F01, F04 |

### 6.2 Status mapping (`adapter:34-35,86-119`)

| UC04 outcome | Stage status | Assessment |
|---|---|---|
| READY | complete | Correct (unreachable in practice) |
| IN_PROGRESS (approved, not provisioned or delivered) | **complete** | **Wrong**: opens UC05 and day-1 readiness (probe P6) (F02) |
| AWAITING_APPROVAL | awaiting_approval | Status correct, but **0 pending approvals** listed (probe P4) (F05/F08) |
| AT_RISK | blocked, **0 blockers**, risks dropped | Dead branch `adapter:107` (AT_RISK ∉ `_READY_STATUSES`) (probe P7) (F06) |
| NEEDS_HUMAN_REVIEW / BLOCKED | blocked + UC04 blockers | Correct |
| CANCELLED | blocked (resumable) | Should be terminal (F06) |
| Worker `ok=false` (POLICY_DENIED, INVALID_INPUT, INTERNAL_ERROR incl. IdempotencyConflict) | failed | "Terminal" per `orchestrator_agent.py:77`, but re-run on every resume. A stale `equipment_access_result` is kept (probe P5). |
| Crash / timeout | uncertain | No re-query despite the comment (`adapter:76-84`) (F11) |
| Unknown status string | blocked | No response validation (F06) |

### 6.3 Edge contracts

| Edge | Inputs | Tenant/actor | Validation | Statuses | Precondition | Idempotency | Exercised | Proving test |
|---|---|---|---|---|---|---|---|---|
| UC03→UC04 | **None**: `document_validation_result` is not mapped; `Uc04Input` has no field for it | Same `state.actor`/`client_id` | Graph ordering only | n/a | `route_after_documents` requires VALIDATE_DOCUMENTS complete (`orchestrator_agent.py:101`) | n/a | Double only (`_docs_double` + `FakeUc04Worker`) | `TESTS/test_routing.py:41`; `test_uc05_lifecycle.py:147,167` (`uc04.calls == 0` when docs are blocked) |
| UC04→UC05 | **None**: UC05 adapter never reads `equipment_access_result` (grep) | Same actor | None on the UC04 response | 7 UC04 CaseStatus → 9 StageStatus; IN_PROGRESS→complete and AT_RISK→blocked are wrong | `route_after_access` requires EQUIPMENT_ACCESS complete (`:102`); complete ⇐ READY or IN_PROGRESS | Fixed key; `_stage_node` skips COMPLETE stages (`orchestrator_agent.py:278-279`), so UC04 is never re-queried for real READY | Double (`FakeUc04Worker` READY/AWAITING_APPROVAL only); probe P6 (real) shows IN_PROGRESS → complete | `test_uc05_lifecycle.py:177-187` (double: AWAITING_APPROVAL keeps UC05 untouched); no test for IN_PROGRESS/AT_RISK |

## 7. Tests

| # | Command (PowerShell) | Verbatim result | Proves | Does not prove |
|---|---|---|---|---|
| T1 | `$env:PYTHONDONTWRITEBYTECODE="1"; Set-Location "...\UC04_Equipment_Access_Integration_Ready"; & "C:\Users\ajaym\AppData\Local\hr-onboarding-envs\uc04\Scripts\python.exe" -m pytest -p no:cacheprovider` | `C:\Users\ajaym\AppData\Local\hr-onboarding-envs\uc04\Scripts\python.exe: No module named pytest` | **BLOCKED**: env has only pydantic and pyyaml (no pytest, fastapi or httpx) | Anything |
| T2 (fallback) | Same directory, `& "...\HR-Onboarding-Common\.venv\Scripts\python.exe" -m pytest -p no:cacheprovider -rfE` | `470 passed in 46.76s` | UC04 rules and the suite pass under Python 3.12.6 with pydantic 2.14.1 and pytest 9.1.1 | That UC04's own declared env works; it runs against mocks |
| T3 (subset) | T2 with `tests/test_readiness.py tests/test_failures.py tests/test_failure_atomicity.py tests/test_approvals.py tests/test_lifecycle.py tests/test_two_paths.py tests/test_mcp.py` | `112 passed in 1.28s` | Provisioning success and failure, approval validity, transitions, rollback, both entry points, MCP mock | Real ITSM; approver authenticity |
| T4 | `$env:PYTHONDONTWRITEBYTECODE="1"; $env:UC04_WORKER_PYTHON="...\uc04\Scripts\python.exe"; Set-Location "...\HR-Onboarding-Common"; .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/orchestration/test_uc04_adapter.py` | `3 passed in 10.76s` | Missing config → BLOCKED without a call; a foreign role → FAILED by UC04 itself | Orchestrator-native ids (uses `SYN-*` and `hr_onboarding_officer`, `test_uc04_adapter.py:24-30`). The "awaiting approval" test actually passes via NEEDS_HUMAN_REVIEW (empty item list; probe P3); the "no mutation" check is only an identity call (`:64-66`) |
| T5 | Same env as T4: `test_durability_recovery.py test_routing.py` + 4 UC04 `test_module_isolation.py` node ids | `20 passed in 4.51s` | Crash before call → UNCERTAIN; restart loses state; routing table | Timeout path, reconciliation, resume after approval |
| T6 (probe) | `$env:PYTHONDONTWRITEBYTECODE="1"; & "...\HR-Onboarding-Common\.venv\Scripts\python.exe" -I "...\scratchpad\probe_uc04.py"` | P1 `failed … INVALID_INPUT` (case_id, employee_ref, client_id); P2 `POLICY_DENIED`; P2b `invalid input fields: actor_id`; P3 `blocked … NEEDS_HUMAN_REVIEW`; P4 `awaiting_approval … pending_approvals: []`; P5/P5b `failed … IdempotencyConflict: idempotency key reused with a different payload`; P6 `complete \| uc04 overall: IN_PROGRESS`, items `ELIGIBLE_FOR_SIMULATED_SUBMISSION`; P7 `blocked \| AT_RISK … blockers: []`, pending roles `HR_OFFICER`; P8/P8b fixture collisions FAILED; P11 file content in error_detail; P13 `unknown op 'submit_eligible'` | Real adapter + real UC04 behaviour on each edge | Full API/graph path (inferred from code) |

**Real UC04 vs double (`TESTS/`):**
- **Real UC04 business calls:** `test_uc04_adapter.py::test_no_approvals_yields_awaiting_approval_not_treated_as_ready`, `::test_unauthorized_actor_role_is_rejected_by_uc04_itself`; `test_durability_recovery.py::test_restarted_worker_starts_with_no_business_data_then_succeeds_fresh`; `test_module_isolation.py::test_worker_state_continuity_within_one_worker`, `::test_second_worker_process_does_not_see_first_workers_state`.
- **Real worker, no business call:** `test_durability_recovery.py::test_worker_crash_mid_call_…` (worker stopped first); `test_module_isolation.py::test_uc04_worker_bootstrap_clean_environment`, `::test_identity_check_rejects_wrong_src_dir`, `::test_concurrent_calls_…`; `test_orchestrator_e2e.py::test_uc_standalone_entry_points_…` (identity only).
- **`orchestrator` fixture (real worker started):** `test_orchestrator_e2e.py` never reaches equipment_access. Test 1 stops at the UC02 FAILED stage. `test_day_one_signoff_…` writes EQUIPMENT_ACCESS=COMPLETE into the case store (`:144-152`).
- **Double (`FakeUc04Worker`, `_lifecycle.py:100-116`, default for every `World`, `_lifecycle.py:135`):** all `World`-based tests in `test_uc05_lifecycle.py`, `test_approval_authorization.py`, `test_joining_day_scheduler.py`, `test_orchestrator_api.py`, `test_rsvp_and_sensitive_data.py`, `test_welcome_recovery.py` and `test_employee_identity.py` (36, 9, 11, 10, 9, 7 and 11 tests in total in those files). Only `test_uc05_lifecycle.py:180` uses a non-READY status (AWAITING_APPROVAL). The double has no IN_PROGRESS, AT_RISK, NEEDS_HUMAN_REVIEW or CANCELLED, and no blocker severity.

## 8. Findings register

Format: **ID · component · status/severity · label · common-layer fixable**, then the details.

**UC04-F01 · identity contract · INCORRECT / P1 · Confirmed defect · common: yes (translation shim); durable fix in UC04**
- **Where:** `UC04 contracts.py:14-21,153-156,192`; `ORCH orchestrator_agent.py:383,415`; `adapter:49-50,71`; `UC01 models/common.py:5-6`, `adapters/memory.py:221`.
- **Observed:** Real case, employee, tenant and actor ids, and role `hr_admin`, are rejected (`INVALID_INPUT` or `POLICY_DENIED`), so the stage is always FAILED (P1, P2, P2b).
- **Expected:** The orchestrator's verified identifiers are accepted or translated.
- **Root cause:** UC04 is locked to local-simulation `SYN-*` patterns; no identity translation exists (`ONBOARDING_COMMON_ARCHITECTURE.md:585` concedes this).
- **Impact:** UC04 can never succeed in the real graph.
- **Fix:** UC04 adds a non-local id profile, or common adds a case/employee/actor/tenant alias map in the adapter. Map `hr_admin` → `hr_onboarding_officer` only via explicit policy.
- **Owner:** UC04 owner + common.
- **Tests:** An adapter test using the ids from `World.case_input` and `HR`.
- **Acceptance:** A real UC04 call from a graph-started case returns `ok=true`.

**UC04-F02 · status mapping / UC05 gate · INCORRECT / P1 · Confirmed defect · common: yes**
- **Where:** `adapter:34,106-111`; `readiness.py:26`; `orchestrator_agent.py:102,160-170,278-279` (`_stage_node` skips COMPLETE stages; `_skip_if_complete` at `:137` is unused).
- **Observed:** UC04 `IN_PROGRESS` (items `ELIGIBLE_FOR_SIMULATED_SUBMISSION`, nothing ticketed) becomes COMPLETE (P6). UC05 orientation starts, day-1 readiness counts equipment as done, and the stage is never re-queried.
- **Expected:** Only READY is complete. IN_PROGRESS is a non-complete status, re-evaluated before day-1 readiness and the joining day.
- **Root cause:** `_READY_STATUSES = {"READY","IN_PROGRESS"}`.
- **Impact:** Orientation and readiness run ahead of the equipment/access prerequisite.
- **Fix:** Map IN_PROGRESS to `in_progress`/`waiting`; re-query UC04 at the day-one readiness node; or make "orientation may start at approval" an explicit, documented business rule.
- **Owner:** Common (business decision with HR). Depends on F03.
- **Tests:** Real UC04 IN_PROGRESS → UC05 not called.
- **Acceptance:** `route_after_access` parks on IN_PROGRESS.

**UC04-F03 · input/evidence channel · MISSING / P1 · Confirmed defect · common: yes**
- **Where:** `adapter:48-60`; `orchestrator_agent.py:391-394`; `contracts.py:20-63`; `api.py:54-77`; `uc_worker_runtime.py:145-157`.
- **Observed:** Request items, approval records and ticket, provisioning, delivery and collection evidence have no source. The worker has no submit op (P13). With the intake-shaped context, UC04 returns NEEDS_HUMAN_REVIEW `NO_ITEMS_REQUESTED` (P3).
- **Expected:** The orchestrator supplies the requested items (role/UC02 derived or HR-entered), relays evidence, and can call `OrchestratedEntry.submit_eligible` (`orchestrated.py:88-90`).
- **Root cause:** The adapter was built for the evaluate-only stage.
- **Impact:** READY is unreachable; equipment readiness can never complete.
- **Fix:** Add fields and an API for items and evidence, plus a worker `submit_eligible` op behind an explicit HR action.
- **Owner:** Common; UC04 owner for a read-ticket port (`INTEGRATION_BOUNDARIES.md:17`).
- **Tests:** Items → approvals → submit → evidence → READY through the graph.
- **Acceptance:** Real UC04 READY reached end to end.

**UC04-F04 · idempotency / resume · INCORRECT / P1 · Confirmed defect · common: yes**
- **Where:** `state.py:250-251`; `adapter:58,66`; `service.py:90-94`; `uc_worker_runtime.py:52-64`; `store.py:12`.
- **Observed:** The same key `{case}.equipment_access.v1` is reused on every resume. Any payload change (approvals added, or only the next day via `date.today()`) raises `IdempotencyConflict` → `INTERNAL_ERROR` → FAILED, repeated on every resume until the worker restarts (P5, P5b). A stale result is kept.
- **Expected:** A new key per distinct payload (or per attempt), a stable `as_of_date`, and the conflict classified as `CONFLICT`.
- **Root cause:** The orchestrator's stable stage key conflicts with UC04's per-request key semantics ("approvals come back inside the next call's input", `INTEGRATION_HANDOVER.md:46`).
- **Impact:** Blocked or awaiting-approval cases cannot resume safely.
- **Fix:** key = hash(payload) or `v{attempt}`; pin `as_of_date` per attempt; map `IdempotencyConflict`.
- **Owner:** Common.
- **Tests:** Resume after an approval and on another day.
- **Acceptance:** The second evaluation advances the item state; no FAILED.

**UC04-F05 · UC04 approvals · MISSING / P1 · Confirmed defect · common: yes (depends on F01, F04)**
- **Where:** `adapter:101-104`; `orchestrator_agent.py:584-591,633-635`; `TESTS/test_uc05_lifecycle.py:186`.
- **Observed:** In AWAITING_APPROVAL the item approvals are not surfaced (P4 lists 0, although UC04 returns `next_actions` with owner `hiring_manager` (P14)). UC04 has no decision endpoint, and `decide_approval` refuses UC04 references. The case parks at `await_access_approval` with no exit.
- **Expected:** Pending UC04 approvals are listed per item with the approver role, and an authenticated decision becomes an `ApprovalRecord` in the next call.
- **Root cause:** UC04 keeps approvals as input data; the orchestrator assumed "their own mechanism" exists (`ONBOARDING_COMMON_ARCHITECTURE.md:380`, which is wrong).
- **Impact:** The approval path is a dead end.
- **Fix:** Add a common-side approval inbox for UC04 items that verifies the approver role and tenant, records approver identity, and feeds `approval_records`.
- **Owner:** Common + UC04 owner (approver directory, SoD).
- **Tests:** Approve, reject and wrong-approver cases against real UC04.
- **Acceptance:** Approve → ELIGIBLE; reject → BLOCKED `APPROVAL_REJECTED`.

**UC04-F06 · status/response validation · INCORRECT / P2 · Confirmed defect · common: yes**
- **Where:** `adapter:35,92-119`.
- **Observed:** The AT_RISK branch is dead: AT_RISK becomes BLOCKED with 0 blockers and the risks are dropped (P7). CANCELLED becomes resumable BLOCKED. Unknown statuses become BLOCKED. `status` and `exceptions` are ignored; there is no schema check.
- **Expected:** An explicit mapping for all 7 statuses, risks projected, contract violations → FAILED `CONTRACT_VIOLATION`.
- **Root cause:** The set membership is wrong; no response model.
- **Impact:** HR sees a block with no reason.
- **Fix:** A mapping table plus a pydantic model of `OrchestratorResponse`.
- **Owner:** Common.
- **Tests:** One per status.
- **Acceptance:** Every status maps deterministically, and AT_RISK carries its risks.

**UC04-F07 · fabricated inputs · INCORRECT / P2 · Confirmed defect · common: yes**
- **Where:** `adapter:52-58`.
- **Observed:** `offer_accepted`, `onboarding_form_approved` and `form_version` are hard-coded. Location, work mode and client-site are never mapped, so the UC04 start gate (DL-01), form-version reassessment (EC-11/EC-35), remote/overseas courier rules and client-site clearance are unreachable.
- **Expected:** Map from intake: `intake_trigger.offer_accepted`, UC01 form status and version, `location`, `work_location_type`.
- **Root cause:** Adapter shortcuts.
- **Impact:** Silent rule bypass; the adapter docstring "never fabricated" is false.
- **Fix:** Map the real fields; block when they are unknown.
- **Owner:** Common.
- **Tests:** A remote hire gets courier days; an unaccepted offer gets START_GATE.
- **Acceptance:** UC04 start-gate blockers reachable via the graph.

**UC04-F08 · approval/blocker projection · INCORRECT / P2 · Confirmed defect · common: yes**
- **Where:** `adapter:95-104`.
- **Observed:** The result keeps only `request_id`, `item_id`, `state` and `ticket_id`; `required_approver`, due dates and `next_actions` are dropped. Workaround proposals (`LOAN_DEVICE` etc.) are listed as pending approvals with role `HR_OFFICER`, which matches no orchestrator role, and no mechanism can decide them. Case-level proposals have `subject_id=None`, so they collapse to one entry.
- **Expected:** A role vocabulary map and proposals distinguished from approvals.
- **Root cause:** No vocabulary mapping.
- **Impact:** HR cannot act on UC04 output.
- **Fix:** Project approvers and owners; map UC04 `Owner` constants to tenant roles.
- **Owner:** Common.
- **Tests:** The projection test.
- **Acceptance:** Each pending approval names a valid tenant role.

**UC04-F09 · config ref / security · INCORRECT / P1 · Confirmed at adapter level, API path inferred · common: yes**
- **Where:** `api.py:73`; `adapter:39-47`; `api.py:141,168`.
- **Observed:** A caller-chosen server path is read. Its existence is observable, and JSON content fragments are echoed into `stage_errors` (P11).
- **Expected:** The config is resolved from tenant config by `(client_id, role_id)`, never from a body path, and error details are sanitised.
- **Root cause:** The config pointer is passed through from the request.
- **Impact:** Arbitrary file probing and partial disclosure. Escalate to P0 if secrets or other tenants' data sit in readable JSON on the host.
- **Fix:** Add a tenant-scoped config registry, drop the body field, and strip `input_value` from worker errors.
- **Owner:** Common.
- **Tests:** A path outside the registry → 422; no `input_value` in any `error_detail`.
- **Acceptance:** The start body cannot select a file.

**UC04-F10 · parent case registry · INCORRECT / P2 · Confirmed defect · common: yes**
- **Where:** `uc_worker_runtime.py:127-147`; `agent.py:73`; `fixtures.py:134`; `data/parent_cases.json:2-5`.
- **Observed:** The fixture registry is live in production code paths; the case is self-registered from its own input; fixture-id collisions fail (P8, P8b); `DuplicateCaseError` becomes FAILED instead of a link; parent status is never synced, so cancellation drafts (DL-15) and the hold gate (EC-20) are unreachable.
- **Expected:** `Uc04Agent(cases=<port backed by orchestrator state>)`; withdraw/hold propagated.
- **Root cause:** The worker reuses the test harness pattern.
- **Impact:** Spurious failures; cancellations are never drafted.
- **Fix:** Inject a `CaseContextPort` (the constructor arg exists, `agent.py:64`).
- **Owner:** Common.
- **Tests:** Withdrawn case → UC04 CANCELLED with drafts.
- **Acceptance:** No fixture ids are visible to orchestrated calls.

**UC04-F11 · durability/recovery · PARTIAL / P2 · Confirmed defect · common: partial**
- **Where:** `store.py:1-2`; `uc_worker_client.py:142-151,190-194`; `adapter:76-84`.
- **Observed:** All UC04 state is in memory. A timeout kills the worker and nothing calls `restart()` (no caller found), so all later UC04 calls are UNCERTAIN. The re-query promised in the adapter comment is not implemented. There is no read-only status op.
- **Expected:** Durable UC04 store; supervised restart; a reconcile op.
- **Root cause:** Prototype persistence.
- **Impact:** Lost ticket ids once submit exists, which could duplicate ITSM requests.
- **Fix:** UC04 adds a repository behind `CaseStore` (`INTEGRATION_BOUNDARIES.md:21`); common adds worker supervision and a `get_case_status` op.
- **Owner:** UC04 owner + common.
- **Tests:** Timeout → restart → reconcile, with no second ticket.
- **Acceptance:** After a restart the same case returns the same tickets.

**UC04-F12 · approval and evidence provenance · Risk (inferred) / P1 · common: partial**
- **Where:** `approvals.py:40-53`; `evidence.py:43-46`; `contracts.py:99-110,189`; `service.py:484-486`.
- **Observed:** Approvals, ticket status and completion evidence are caller-asserted. Validity is role-string equality; `approver_id` is not verified; there is no requester ≠ approver check; config (approver and evidence roles) comes from the caller; the write gate is `approved=True`.
- **Expected:** Approvals and evidence from authenticated sources with SoD.
- **Root cause:** Local-simulation design.
- **Impact:** In production any allowed caller could assert READY.
- **Fix:** A verified approval source (F05) and an ITSM read port.
- **Owner:** UC04 owner + common.
- **Tests:** A fabricated approver or a self-approval is rejected.
- **Acceptance:** Only inbox-issued approvals count.

**UC04-F13 · MCP · DISCONNECTED / P3 · Confirmed · common: no**
- **Where:** `mcp/server.py:25-110`; `mcp/tools.py:76-89`.
- **Observed:** Imported only by tests. It bypasses Policy, misclassifies the Outlook tool, and the package name collides with the MCP SDK.
- **Fix:** Either route `execute_tool` through `Uc04Service`/`ToolGateway` or delete it; rename the package.
- **Owner:** UC04 owner.
- **Tests:** An MCP submit honours the item state and kill switch.

**UC04-F14 · packaging · PARTIAL / P3 · Confirmed · common: yes (already compensated)**
- **Where:** `pyproject.toml:54,58`; `src/sub_agent/guardrails.py:2`, `policy.py:2`; `fixtures.py:17`.
- **Observed:** The wheel lacks the sibling packages and `data/`; the import fails without the repo root (probe).
- **Fix:** Move the packages under `sub_agent` or include them in the wheel.
- **Owner:** UC04 owner.
- **Acceptance:** `pip install` of the wheel imports cleanly.

**UC04-F15 · test environment · BLOCKED / P3 · Confirmed · common: n/a**
- **Where:** `hr-onboarding-envs/uc04`.
- **Observed:** No pytest, fastapi or httpx in the given env (T1).
- **Fix:** Install the `[api,dev]` extras into the audit env.
- **Owner:** Environment owner.
- **Acceptance:** T1 runs.

**UC04-F16 · integration tests · PARTIAL / P2 · Confirmed · common: yes**
- **Where:** §7 lists.
- **Observed:** No graph-level test with real UC04; adapter tests use UC04-shaped ids; a misleading test name and loose assertion (`test_uc04_adapter.py:40-48`); the double covers 2 of 7 statuses.
- **Fix:** Add real-UC04 graph tests for each status and for resume; extend the double to all statuses.
- **Owner:** Common.
- **Acceptance:** F01–F06 regressions are caught.

**UC04-F17 · docs · INCORRECT / P3 · Confirmed · common: yes**
- **Where:** `HR_ONBOARDING_ORCHESTRATION_AGENT_ANALYSIS.md:349,507` ("Implemented and tested"; READY/IN_PROGRESS → COMPLETE); `ONBOARDING_COMMON_ARCHITECTURE.md:380` ("lists" UC04 approval records); adapter docstring `:17-19`, comment `:76-80`.
- **Fix:** Correct them after the fixes land.
- **Owner:** Common.

**UC04-F18 · UC03→UC04 contract · Recommendation / P3 · common: yes**
- **Where:** `orchestrator_agent.py:101`; `adapter:48-60`.
- **Observed:** The dependency is ordering only; no document or task context reaches UC04.
- **Fix:** Document it as intended. If business requires it (e.g. right-to-work before provisioning), pass a gate flag mapped to the UC04 start gate, plus a contract test.
- **Owner:** Common + HR.

## 9. Required actions

| Priority | Common layer (HR-Onboarding-Common) | UC04 owner |
|---|---|---|
| 1 | F02 map IN_PROGRESS as not complete; F06 explicit 7-status mapping and response schema | F01 non-`SYN` identifier profile (or agree on the alias scheme) |
| 2 | F04 payload-derived idempotency key, pinned `as_of_date`, `CONFLICT` classification | F12 approver identity and SoD; ITSM read port for ticket and evidence status |
| 3 | F01 alias/translation for case, employee, tenant and actor; role mapping by policy | F11 durable repository behind `CaseStore` |
| 4 | F03 item, evidence and submit channel (worker `submit_eligible` op; HR-triggered) | F14 package the sibling modules; F13 wire MCP through `Uc04Service` or delete it |
| 5 | F05 UC04 approval inbox feeding `approval_records`; F08 projection and role vocabulary | Confirm whether orientation may start at "approved" (input to F02) |
| 6 | F09 tenant-scoped config registry; remove `uc04_client_config_ref` from the API body; sanitise `error_detail` | |
| 7 | F10 inject a `CaseContextPort` from orchestrator state; propagate withdraw/hold | |
| 8 | F11 worker supervision and restart, reconcile op (`uc04.get_case_status`) | |
| 9 | F16 real-UC04 graph tests; F17 doc corrections; F18 UC03→UC04 contract note | F15 (env owner) install dev extras |

HR Policies Q&A (UC05) is unaffected by every item above. No NDA or signature tracking is proposed.
