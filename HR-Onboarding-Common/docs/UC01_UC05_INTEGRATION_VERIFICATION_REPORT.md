# UC01–UC05 Integration Verification Report

**Scope:** Independent, evidence-based verification of the integration between UC01–UC05 and the
central `HR-Onboarding-Common` orchestrator. Verification-only: no application source, tests,
config, or dependency files were modified. Local virtual environments (`.venv`) for
`HR-Onboarding-Common`, `UC01_Employee_Record_Creation`, `UC03_Document_Validation`,
`UC04_Equipment_Access_Integration_Ready`, and `UC05_Orientation-Schedule-Agent` were
(re)synchronized from their existing `uv.lock` files purely so their existing test suites could
actually execute (see §20); no `pyproject.toml`/`uv.lock` was edited.

**Date:** 2026-11-01 · **Branch:** `Prasanna` · **Verifier role:** independent audit (per task brief)

---

## 1. Executive summary

The orchestrator is a real, working LangGraph state machine that drives UC01, UC03, UC04, and UC05
through their **actual, real, production code** — two as subprocess workers (UC01, UC04; UC05 is a
third subprocess worker), one in-process via a compiled LangGraph graph (UC03). All of this is
verified by execution, not by reading code: **417/417** tests in
`HR-Onboarding-Common/tests/orchestration` pass with real UC01/UC04/UC05 subprocess workers
actually spawned and a real UC03 graph actually invoked.

**UC02 is the one confirmed defect**, and it is pre-existing and already known: a stray `UC` token
immediately before the module docstring at
`UC02_Onboarding_Task_Agent/src/onboarding_task_management/agent/advisor.py:1` is a literal Python
`SyntaxError`. This makes the entire `onboarding_task_management.agent` subpackage — and therefore
`onboarding_task_management.sub_agent.adapter.UC02SubAgent.run()`, UC02's real public entry point —
unimportable. Confirmed independently in this audit:

```
$ python -m pytest tests  (inside UC02_Onboarding_Task_Agent)
SyntaxError: invalid syntax
  tests/test_agent.py:17 -> onboarding_task_management/agent/__init__.py:42
  -> onboarding_task_management/agent/advisor.py:1: "UC\"\"\"The LLM layer..."
```

The orchestrator's `plan_tasks` adapter (`adapters/uc02_tasks.py:53-64`) catches exactly this
`SyntaxError` and surfaces it as a clean `StageStatus.FAILED` with code `UC02_PACKAGE_UNIMPORTABLE`
— **it does not crash the orchestrator run, and it does not fabricate a success.** This is correct,
defensible defensive engineering around a defect the orchestrator team does not own. But it means
**UC02's real task-planning logic has never actually executed inside this integration** — every
"successful task plan" scenario in the test suite uses a `PlanDouble` stand-in
(`tests/orchestration/_workflow.py:120-140`), explicitly labelled as such in test comments. UC02's
document-collection trigger is a separate concern (orchestrator-owned mock notification port, by UC02's
own design — UC02 exposes no document-collection operation at all) and is unaffected by the advisor.py
defect.

Net effect: **4 of 5 UC integrations (UC01, UC03, UC04, UC05) are verified end-to-end against real
agent code.** UC02's integration is architecturally correct and defensively handled, but **cannot be
verified as a working real-agent integration today** because UC02's own code does not import. This
is a UC02-owned blocker, not an orchestrator defect.

---

## 2. Actual architecture and execution flow

```
START -> intake (UC01, real subprocess worker)
      -> intake_mismatch_proposal (HITL gate: ambiguous match/duplicate)
      -[route_after_intake]-> document_collection (UC02 op A: mock notification outbox, not a UC02 call)
      -[route_after_document_collection]-> document_submission (orchestrator-only wait, no UC call)
      -[route_after_document_submission]-> validate_documents (UC03, real in-process LangGraph graph)
      -[route_after_validation]-> dispatch_branches (fan-out, 3 parallel branches)
            -> plan_tasks (UC02 op B: UC02SubAgent.run — BLOCKED by advisor.py defect today)
            -> equipment_access (UC04, real subprocess worker)
            -> orientation_scheduling -> buddy_assignment -> team_introduction (UC05, real subprocess worker)
      -> merge_outcomes -> evaluate_overall_status
      -[route_after_evaluation]-> day_one_readiness (HR signoff gate) -> joining_day_welcome (UC05)
      -[route_after_welcome]-> complete
```

Source of truth: `orchestrator_agent.py:1-46` (module docstring reproduces this exact graph) and
`build_graph()` at `orchestrator_agent.py:591-647`. Every stage runs through one standard pipeline
(`execution.invoke`, `execution.py:82-138`): build `UCAgentRequest` → check dependencies
(`dependencies.py`) → call the adapter → synchronize the result under field ownership
(`state_sync.py`) → check the status transition is legal (`agent_contract.py`) → record a
`StageOutcome`. This pipeline is identical for all five UCs — the adapters are the only code that
knows each UC's native interface.

---

## 3–7. Per-UC verification (UC01–UC05)

### UC01 — Employee Record Creation: **COMPLETE**

| Item | Evidence |
|---|---|
| Adapter | `adapters/uc01_intake.py:36` `call_uc01_intake(state, deps)` |
| Transport | Real subprocess worker (`workers/uc_worker_client.py`), identity-checked at startup against `config.EXPECTED_ENTRY` |
| Real entry point | `UC01_Employee_Record_Creation/src/sub_agent/onboarding_entry.py:70` `open_onboarding_case(request, deps) -> OpenCaseResult`, dispatched via `workers/uc_worker_runtime.py:97-120` |
| Output mapping | `uc01_intake.py:89-91`: `case_id`, `employee_id` (= UC01's `employee_no`), `joining_date` |
| Tenant guard | `uc01_intake.py:77-83`: foreign-tenant case → `FAILED`/`TENANT_CONTEXT_CONFLICT`, nothing from the foreign case enters state |
| Error normalization | Policy denial → `POLICY_BREACH`; worker crash/timeout → `UNCERTAIN`/`UNCERTAIN_OUTCOME`; business rejection → worker's own classified code |
| Real-agent tests | `test_uc01_adapter.py` (fixture `uc01_worker`, real subprocess) |
| Mock-based tests (labelled) | `test_uc_adapter_contracts.py::TestUC01EmployeeRecord` uses `FakeUc01Worker` explicitly for envelope-shape/crash-mapping checks |

No over-exposure: only `case_id`, `employee_no`, `joining_date`, `approvals_created`,
`existing_case_id`, `reasons` leave UC01's result; no UC01-internal type/module name reaches
orchestrator state.

### UC02 — Onboarding Task Management: **PARTIAL / BLOCKED (real-agent) — DISCONNECTED for task planning today**

| Item | Evidence |
|---|---|
| Op A: Document collection adapter | `adapters/uc02_document_collection.py:56`. **Does not call UC02 at all** — by UC02's own design, it has no document-collection operation. Uses UC03's checklist (`applicable_documents`) + an orchestrator-owned mock notification port (`deps.notifier`, explicitly labelled "MOCK" in the module docstring) |
| Op B: Task planning adapter | `adapters/uc02_tasks.py:38` `call_uc02_plan_tasks`, real target `onboarding_task_management.sub_agent.adapter.UC02SubAgent.run()` at `sub_agent/adapter.py:193` |
| **Confirmed blocker** | `onboarding_task_management/agent/advisor.py:1`: `UC"""The LLM layer...` — literal `SyntaxError`, independently reproduced in this audit both via `ast.parse` and via `python -m pytest tests` inside UC02's own repo |
| Catch site | `uc02_tasks.py:53-64`: `except SyntaxError as exc:` → `StageStatus.FAILED`, `error_code = "UC02_PACKAGE_UNIMPORTABLE"` (line 35), `retryable=False` (confirmed `ErrorCategory.INFRASTRUCTURE` in `contracts.py:184`) |
| Real-agent tests | `test_uc02_adapter.py::test_known_import_defect_surfaces_as_failed_not_a_fake_success` and `test_uc_adapter_contracts.py::TestUC02TaskPlanning::test_the_real_uc02_package_reports_its_import_defect_as_a_terminal_infrastructure_failure` — both exercise the **real, broken** package and assert the structured failure |
| Mock-based tests (labelled) | `test_uc_adapter_contracts.py::test_a_plan_is_normalized_with_its_output__contract_double` uses `PlanDouble` (`_workflow.py:120-140`), explicitly commented "NOT a UC02 result (UC02 cannot import)" |
| Downstream isolation | `test_uc05_lifecycle.py::test_real_uc02_defect_fails_task_planning_while_uc04_and_uc05_still_run` — confirms the fault is contained to the `plan_tasks` branch only |

**Classification nuance:** the *integration wiring* for UC02 is COMPLETE (correct request/response
contract, correct error classification, correct branch isolation, correct non-retryable terminal
state, an explicit `retry_stage` path to re-run it once fixed). The *verified real-agent execution*
of UC02's task-planning logic is **BLOCKED** — it has never produced a real result in this audit,
because UC02's own code does not import. This is UC02-owned, not an orchestrator defect; the
orchestrator adapter's comment explicitly states it was "not fixed by this orchestrator per explicit
instruction."

### UC03 — Document Validation: **COMPLETE**

| Item | Evidence |
|---|---|
| Adapter | `adapters/uc03_documents.py` (full file read) |
| Transport | In-process; lazily imports and caches UC03's real compiled graph: `_build_uc03_runtime` (lines 75-94) → `from src.sub_agent.workflow import build_uc03_graph`, UC03's real function at `UC03_Document_Validation/src/sub_agent/workflow.py:111` |
| Invocation | `_validate_all` (173-227) builds a `DocumentValidationRequest` per document (with `correlation_id`), `graph.ainvoke(graph_state)` (line 225) — a genuine async LangGraph call, wrapped in `asyncio.wait_for(..., timeout=deps.uc03_timeout_s)` |
| Output mapping | `_classify`/`_project` (lines 141-170): statuses/findings/review decisions only — **no raw document content**, asserted by `test_no_raw_document_content_leaks_into_state` |
| Status→routing | `_REQUIREMENT` table (57-63) maps UC03 classifications to `WAITING_FOR_INPUT`/`AWAITING_APPROVAL`/`BLOCKED`/`COMPLETE`, consistent with `route_after_validation` (`orchestrator_agent.py:186-204`) |
| Real-agent tests | `test_uc03_adapter.py` (real graph, `require_uc("uc03")`), `test_uc_adapter_contracts.py::TestUC03DocumentValidation` (docstring states "the REAL UC03 graph in-process") |
| Mock-based (labelled) | Only the timeout test monkeypatches `_validate_all` with a slow coroutine — **timing only**, never UC03's business logic. `test_a_uc03_exception_is_a_technical_failure_not_a_document_verdict` injects a raising graph double to prove technical failures are never confused with document verdicts |

### UC04 — Equipment & Access Readiness: **COMPLETE**

| Item | Evidence |
|---|---|
| Adapter | `adapters/uc04_equipment_access.py:39-127` `call_uc04_equipment_access` |
| Transport | Real subprocess worker; `config.py:24` `EXPECTED_ENTRY["uc04"] = ("sub_agent.agent", "Uc04Agent")`, enforced at startup identity check |
| Real entry point | `worker.call("evaluate_orchestrated", ...)` → `uc_worker_runtime.py:141-157` → `Uc04Agent.evaluate_orchestrated` (`UC04.../sub_agent/agent.py:230-232`) → UC04's real orchestrated-service path |
| Output mapping | Lines 101-107: `overall_status`, per-item `request_id`/`item_id`/`state`/`ticket_id` into `equipment_access_result`; UC04 proposals → `pending_approvals` (109-112) |
| Status mapping | `overall_status in {"READY","IN_PROGRESS"}` → `COMPLETE`; `AWAITING_APPROVAL`/other → `AWAITING_APPROVAL`/`BLOCKED` (lines 114-126) |
| Real-agent tests | `test_uc04_adapter.py` (real subprocess worker fixture), including a genuine UC04-side policy rejection test (`test_unauthorized_actor_role_is_rejected_by_uc04_itself`) |
| Mock-based (labelled) | `test_uc_adapter_contracts.py::TestUC04EquipmentAccess` explicitly uses `FakeUc04Worker`/scripted doubles for crash/identity-conflict paths |

### UC05 — Orientation, Buddy, Team Introduction, Joining-Day Welcome: **COMPLETE**

UC05 is the largest integration (adapter file: 1,340 lines) covering five operations plus four HITL
relays, all real subprocess calls to `UC05_Orientation-Schedule-Agent`'s own `OnboardingAgent`
(`agent.py:349`) and `OnboardingToolRegistry` (`tool_registry.py`).

| Operation | Adapter fn | Real UC05 entry point | Status derivation |
|---|---|---|---|
| Orientation scheduling | `call_uc05_orientation` (389-447) | `run_event("offer_accepted")` → `OnboardingAgent.run_event` | `_derive_orientation` (540-598): confirmed/pending/uncertain/rejected/cancelled per UC05's own session snapshot |
| Buddy assignment | `call_uc05_buddy_assignment` (608-654) | `propose_buddy` → `OnboardingToolRegistry.propose_buddy_assignment` (`tool_registry.py:345`) | lines 636-675: complete/pending/no-candidates/already-assigned/rejected |
| Team introduction | `call_uc05_team_introduction` (686-709) | shared `_derive_session_stage` (712-772), approver = "the team lead on record" per UC05, not hardcoded |
| Buddy introduction (optional) | `refresh_buddy_introduction` (775-792) | `NOT_REQUESTED` until explicitly scheduled; never blocks readiness (OPTIONAL_STAGES) |
| Joining-day welcome | `call_uc05_joining_day_welcome` (821-862) | `run_event("joining_day")` → UC05's welcome-prep node; delivery ledger is authoritative (`_derive_welcome`, 913-967) |

**Identity mapping** (`resolve_employee_ref`, 219-231; `tenant_config.py:153-190`): UC01's
`employee_no` is resolved to UC05's canonical employee id through a validated, tenant-configured
scheme (`shared`/`mapped`/`none`) with 5 distinct typed error codes
(`EMPLOYEE_ID_MALFORMED/AMBIGUOUS/CONFLICT/UNRESOLVED/MISSING`); once resolved for a case, the
mapping is frozen for that case's lifetime — a later config change cannot retarget it
(`test_employee_identity.py::test_a_mapping_changed_mid_lifecycle_never_switches_uc05_to_another_employee`).

**Duplicate prevention**: transport-level idempotency keys on every worker call (`_call`, 260-267);
welcome drafted only `if snap.latest_welcome() is None` (853); UC05's own joining-day trigger "skips
anyone already drafted" — proven by `test_uc05_lifecycle.py::test_repeated_joining_day_event_drafts_and_sends_one_welcome`
(asserts exactly one draft and one sent message after three triggers).

**Real-agent tests**: all of `test_uc05_lifecycle.py` (every test using the `world`/`uc05` fixtures)
and the `uc05_worker`-parametrized tests in `test_uc05_adapter.py` spawn the real UC05 subprocess.

**Mock-based (labelled)**: the remainder of `test_uc05_adapter.py` (from its own documented "unit
tests: recording UC05 double" section) uses `FakeUc05Worker` (`_uc05_fakes.py`), explicitly stated to
mirror a shape "verified against a live UC05 run" — these pin the contract, they do not
independently re-verify UC05's current behavior (that is the real-subprocess tests' job).

**Noted coupling (minor, documented)**: UC05's internal LangGraph stage/node names (e.g.
`"welcome_awaiting_approval"`, `"scheduling_failed"`) are relayed into orchestrator state
(`approval_decisions`, `blocker_action` text) and used as branch conditions
(`_derive_session_stage`, lines 761-768). The module's own docstring acknowledges this: "the order is
UC05's own ..., not invented here." This is a coupling to UC05's internal vocabulary, not a
correctness defect, but it means a future UC05 refactor of its internal stage names would need a
coordinated change here.

---

## 8. Standardized communication verification

One envelope pair for all five UCs: `UCAgentRequest`/`UCAgentResponse` (`contracts.py:258-395`).
Verified fields present and enforced on every request: `case_id`, `tenant_id`, `correlation_id`,
`trace_id`, `execution_id`, `attempt`, `actor`, `idempotency_key`, `input_data`, `shared_context`,
`dependencies`. `build_request()` (311-347) raises `ContractViolation` for missing required fields
or a cross-tenant actor/identity **before any UC is called** — confirmed by
`execution.py:93-107` (`reject()` short-circuits, the adapter never runs).

Response normalization (`normalize_response`, 375-395) is identical across UCs: `status`
(`AgentStatus`, a 9-value canonical enum), `stage_status`, `output_data` (only the stage's own
result key, only when it actually changed), `updated_context` (only shared-context fields),
`errors`/`warnings` (typed `AgentError`/`AgentWarning`), `pending_actions`. **Verdict: COMPLETE** for
UC01/UC03/UC04/UC05; **COMPLETE for the contract, BLOCKED for real execution** for UC02 (per §3-7).

No adapter silently substitutes a mock for a real agent in production code. Every mock/fake
(`FakeUc01Worker`, `PlanDouble`, `FakeUc04Worker`, `FakeUc05Worker`) exists only in test modules and
is clearly labelled as such in file/test names and docstrings.

---

## 9. Shared context and field mapping verification

`shared_context()` (`contracts.py:350-363`) is the actual cross-UC payload every stage receives:
`employee_id`, `joining_date`, `case_timezone`, `offer_reference`, `canonical_employee_id`,
`documents_verified`, `verified_document_types`, `task_plan_available`, `equipment_access_ready`,
`day_one_signed_off`. This is a deliberately small, flag-based surface — not the full case record —
confirmed by its own comment: "What downstream UCs need from upstream ones, and no more."

| Field | Source UC | Destination UC | Mapping implementation | Verified |
|---|---|---|---|---|
| Employee identifier (`employee_id`) | UC01 | UC02–UC05 | `uc01_intake.py:90` sets `state["employee_id"]`; read by `uc02_tasks.py:68`, `uc04_equipment_access.py` input payload, `resolve_employee_ref` (`uc05_orientation.py:219-231`) | **Yes** (real-worker tests + `test_employee_identity.py`, 18 tests) |
| Canonical employee id | UC01→(tenant_config)→UC05 | UC05 only | `tenant_config.py:153-190` `resolve_employee()`; frozen once resolved (`uc05_orientation.py:224-228`) | **Yes** |
| Employee profile (role/dept/manager/location/project) | orchestrator intake input (not UC01 — documented gap) | UC02 (`PLAN_TASKS`) | `state["uc02_role_context"]`, read at `uc02_tasks.py:44-50` | **Yes** (blocked only by advisor.py downstream, not by the mapping itself) |
| Task context | UC02 (would-be) | UC03 | **No such edge exists.** `dependencies.py:85` `_TASK_PLAN` is explicitly OPTIONAL and "no UC04/UC05 contract consumes it" | **N/A — not a real dependency; verified absent, not missing** |
| Validation result (`document_validation_result`) | UC03 | UC04, UC05 (via `verified_context`/`shared_context.documents_verified`) | `uc03_documents.py` `apply_validation_results` (256-303); `orchestrator_agent.py:528-533` `_dispatch_branches` sets `verified_context` once; read via `dependencies.py:82-84` `_DOCUMENTS_VERIFIED` | **Yes** (`test_state_driven_workflow.py`) |
| Readiness status (`equipment_access_result`) | UC04 | DAY_ONE_READINESS only (not UC05 directly) | `dependencies.py:108-111`; `uc04_equipment_access.py:101-107` | **Yes** — note UC05 does **not** wait on UC04 by default (optional, `configurable=True` dependency at `dependencies.py:98-102`); this is a documented design choice, not an oversight |
| Orientation/welcome result | UC05 | Common state (`orientation_schedule_result`, `welcome_message_result`, etc.) | per-operation in `uc05_orientation.py` (see §7 table) | **Yes** |

**Null/conflicting-update protection**: `state_sync.py:171-174` — a new value of `None`/missing when
the old value was known is kept (never silently erased), logged as `NULL_OVERWRITE_IGNORED`.
**Field ownership**: `FIELD_OWNERS` (`state_sync.py:42-66`) is a strict one-owner map; a stage writing
outside its own ownership raises `STATE_OWNERSHIP_VIOLATION` and the entire sync is rejected
(`synchronize()`, 143-188) — the adapter's output is discarded, not partially applied. **Identity
conflict detection**: `_identity_violation()` (129-140) — `employee_id` changing once known, or
`employee_ref` crossing tenants/switching person mid-case, is rejected as `IDENTITY_CONFLICT`/
`TENANT_CONTEXT_CONFLICT`.

**Verdict: PASS.** Propagation is real (verified by execution, not just schema), minimal
(no UC-specific business detail copied into global state beyond each stage's own named result key),
and ownership-enforced.

---

## 10. Dependency management verification

`dependencies.py` is the single registry (`STAGE_DEPENDENCIES`, lines 91-117) — not duplicated
elsewhere. Required vs. optional is explicit (`DependencyKind.REQUIRED`/`OPTIONAL`); optional
dependencies that are unmet produce only a warning (`optional_warnings`, 190-192), never a block.
A tenant may only promote a `configurable=True` optional dependency to required — never relax a
required one (`validate_overrides`, 124-141).

Enforcement point: `execution.invoke()` (lines 93-107) — `dependencies.blocking()` is checked
**before** the adapter is called; if any required dependency is unmet, `dependency_block()` runs and
**the adapter is never invoked**. This was independently confirmed in this audit via
`test_dependencies.py::test_uc03_cannot_validate_before_uc02s_requirements_are_met_by_submitted_documents`,
which asserts `flow.uc03.calls == 0`.

| # | Required scenario | Result | Evidence |
|---|---|---|---|
| 1 | UC02 cannot execute without a valid UC01 result | PASS | `dependencies.py:93` `_EMPLOYEE_RECORD`; `test_uc02_cannot_request_documents_without_uc01s_employee_record` asserts `flow.notifier.sent_count() == 0` |
| 2 | UC03 cannot execute without required UC02 context | PASS | `dependencies.py:95` requires `_DOCUMENTS_SUPPLIED` (the document-submission wait, which depends on UC02's collection record) before UC03 runs; `flow.uc03.calls == 0` confirmed |
| 3 | UC04 cannot execute when mandatory UC03 validation is incomplete | PASS | `dependencies.py:97` `_DOCUMENTS_VERIFIED` required for `EQUIPMENT_ACCESS`; enforced identically through `execution.invoke` |
| 4 | UC05 cannot execute when required UC04 readiness conditions are unmet | **PARTIAL by design** | UC05's orientation stage treats UC04 readiness as **optional and tenant-configurable** (`dependencies.py:98-102`) — "no UC05 operation consumes UC04's result itself." UC04 readiness **is** a hard, required input to `DAY_ONE_READINESS` (line 108-111) and therefore to `JOINING_DAY_WELCOME`. So UC05's early stages are intentionally independent of UC04; the joining-day welcome is correctly gated on it. This matches the documented business rule, not a defect — but it means the task's literal expectation ("UC04 → UC05 required") does not hold at the orientation-scheduling stage. |
| 5 | A valid upstream result allows the next UC to execute | PASS | `test_integration_scenarios.py::test_01_*` (real-agent E2E) |
| 6 | An optional dependency does not block the workflow when legitimately absent | PASS | `_TASK_PLAN` optional dependency (`dependencies.py:85`); `optional_warnings` never blocks; confirmed in `test_dependencies.py` |

---

## 11. Conditional routing verification (LangGraph)

All five UCs are registered as real executable nodes (`build_graph()`, `orchestrator_agent.py:591-647`)
— each node closure actually calls the real adapter function (confirmed in §3-7, not merely a name on
the graph). Entry point: `intake`. Terminal points: `complete` → `END`, and every `await_*` node →
`END` (a checkpointed pause, not a crash).

Conditional edges read **actual stage output**, never free text — confirmed by direct inspection of
every routing function:
- `route_after_validation` (`orchestrator_agent.py:194-204`) reads `StageStatus` produced by UC03's
  own classification via `_REQUIREMENT` (uc03_documents.py:57-63) — a technical failure
  (RETRY_PENDING/FAILED) is never routed as an invalid-document outcome (explicit code comment).
- `route_after_evaluation` (206-218) distinguishes "all readiness stages complete" from "a
  dependency-blocked branch became runnable, re-dispatch" from "still waiting" — bounded by
  `MAX_DISPATCH_ROUNDS = 3` (line 99), preventing an infinite re-dispatch loop.
- Branch-internal routes (`_branch_route`, 163-173) read the branch's own `branch_outcomes` view, not
  the shared state directly, preventing one branch's in-flight write from leaking into another's
  routing decision this same turn.

Failure routing: `_NOT_RERUN = (COMPLETE, FAILED)` (line 104) — a `FAILED` stage parks, terminal
until an explicit `retry_stage`. Approval interrupts: `node_day_one_signoff_proposal` and
`node_intake_mismatch_proposal` raise HITL gates exactly once per undecided state
(`_gate_pending`, 324-329). Retry/recovery: `_count_technical_failure` (464-479) enforces
`stage_max_attempts`, after which the stage becomes FAILED/`RETRY_LIMIT_REACHED` (non-retryable).

**Verdict: PASS**, verified both by code inspection and by `test_routing.py` + `test_state_driven_workflow.py`
(69/69 passed in this audit's run) and the `evaluate_overall_status`/dispatch-round logic exercised by
`test_integration_scenarios.py` scenario 8 (orientation conflict → recovery) and scenario 5 (UC04
approval pause).

---

## 12. Workflow state synchronization verification

Three distinct persistence layers were confirmed distinct, not conflated:

1. **LangGraph checkpoint state** — per-invocation graph execution state (`MemorySaver` or
   `SqliteSaver`, `bootstrap.py`). The orchestrator does **not** rely on this for durable business
   state: `deps.py` and `case_store.py` both document that this LangGraph version's `get_state` is
   inconsistent, so resume reads from the **case store**, not the checkpoint.
2. **Central onboarding case state** (`OnboardingCaseState`, `state.py`) — the authoritative business
   record, written through on every stage execution (`_persist`, `orchestrator_agent.py:710-729`),
   version-stamped (`state_version`), and rejecting any write that would silently regress a completed
   required stage (`completed_stage_regressions`, `agent_contract.py:143-153`).
3. **Individual UC business data** — lives in each UC's own store (UC01's case DB, UC05's SQLite
   session/operation stores, etc.); the orchestrator holds only the normalized stage result
   (`output_data`), never a second copy of the UC's full record.
4. **Approval/audit records** — the HITL inbox (`ctx.hitl`), separate from case state; pending
   approvals are *read back* into `pending_approvals` for display, not stored twice as the source of
   truth.
5. **Idempotency/execution records** — `idempotency_keys` per stage (state.py) plus each UC's own
   ledger (UC05's operation ledger, UC04's `CallerContext.idempotency_key`).

Durable persistence is confirmed real (not assumed): `test_durability_recovery.py`,
`test_lifecycle_contracts.py::test_sqlite_case_store_round_trips_and_survives_reopening`, and
`test_integration_scenarios.py::test_10_process_restart_during_a_paused_workflow_resumes_from_durable_state`
all passed in this audit's run, using a `SqliteCaseStateStore` + `SqliteSaver`, i.e. actual file-backed
SQLite, not an in-memory stand-in.

**Verdict: PASS.**

---

## 13. Agent initialization and isolation verification

Each UC's worker is started with an identity check (`uc_worker_runtime.py:71-89`,
`config.EXPECTED_ENTRY`) that fails loudly (`WorkerConfigurationError`) if the loaded module/class
does not match the UC it claims to be — this is a real, enforced guard against module-name
collisions or a misconfigured `src_dir`, not a hope. `test_module_isolation.py` (12/12 passed)
exercises this directly, including `test_uc05_worker_bootstrap_clean_environment`.

Each UC runs under **its own, separate virtual environment** (`UC0X_WORKER_PYTHON`/`UC0X_WORKER_SRC_DIR`,
`conftest.py:10-18`), confirmed in this audit by independently inspecting
`UC01.../.venv`, `UC03.../.venv`, `UC04.../.venv`, `UC05.../.venv` — each is a distinct `uv`-managed
venv with its own locked dependency set (langgraph 1.2.16 for UC05 vs. none for UC01/UC04). UC02 runs
in-process in the orchestrator's own interpreter (by UC02's own design — "no module-name collision,
`onboarding_task_management` is unique") — confirmed; this is the one UC where a dependency defect in
the sub-process-equivalent code directly surfaces as an import error in the orchestrator's own
process space, which is exactly what was observed (§3-7, UC02).

Timeouts and crash handling are symmetric across the three worker UCs: `WorkerCrashedError`/
`WorkerTimeoutError` → `record_uncertain` (never a blind retry of a write whose effect is unknown).

**Verdict: PASS** for initialization isolation and identity verification; mock execution is never
confused with real execution in any adapter or default test path (every fake is named `Fake*`/`*Double`
and documented as such).

---

## 14. API and event entry-point verification

All routes live under `/v1/orchestration` in `api.py` (539 lines). Full table (method, handler,
orchestrator call, auth, test):

| Method/Path | Handler | Orchestrator call | Auth/tenant | Test |
|---|---|---|---|---|
| POST `/cases` | `start_case` | `advance_case(inp, case_id=None, actor)` | `case_operator_roles`; tenant from verified identity | `test_start_takes_the_tenant_from_the_identity_and_returns_no_personal_data` |
| GET `/cases/{case_id}` | `get_case` | `get_case(case_id, client_id=...)` | `case_reader_roles` | `test_reading_a_case_needs_a_reader_role_and_the_same_tenant` |
| POST `/cases/{case_id}/resume` | `resume_case` | `resume_case(case_id, actor)` | `case_operator_roles` | `test_10_process_restart_during_a_paused_workflow_resumes_from_durable_state` |
| POST `/cases/{id}/approvals/{id}/decision` | `decide` | `decide_approval(...)` | inbox-owning check | `test_decisions_are_checked_by_the_owning_inbox_and_mapped_to_http`, full `test_approval_authorization.py` suite |
| POST `/cases/{id}/approvals/{id}/retry-apply` | `retry_apply` | `retry_approval(...)` | internal | covered indirectly (HITLApplyError mapping) |
| POST `/cases/{id}/joining-day` | `joining_day` | `trigger_joining_day(...)` | `joining_day_trigger_roles` | `test_13_joining_day_welcome_with_the_real_uc05` |
| POST `/cases/{id}/orientation/operations` | `uc05_operation` | `request_uc05_operation(...)` | `uc05_operation_roles` | `test_uc05_operations_validate_input_roles_and_prerequisites` |
| POST `/cases/{id}/sessions/{id}/responses` | `participant_response` | `record_participant_response(...)` | UC05-decided | `test_rsvp_route_forwards_to_uc05_and_surfaces_the_conflict` |
| POST `/cases/{id}/documents` | `submit_documents` | `submit_documents(...)` | `document_submitter_roles` | `test_document_routes_check_identity_role_tenant_and_content` |
| POST `/cases/{id}/documents/reviews` | `review_documents` | `review_documents(...)` | `document_review_roles` | same file |
| POST `/cases/{id}/stages/{stage}/retry` | `retry_stage` | `retry_stage(...)` | `stage_retry_roles` | `test_review_and_retry_routes_refuse_what_they_cannot_decide` |
| GET `/cases/{id}/pending-actions` | `pending_actions` | `get_case(...)` (read) | `case_reader_roles` | **no dedicated named test found** — covered only incidentally |
| POST `/cases/{id}/events` | `case_event` | `handle_event(...)` | per-type role | `test_12_duplicate_triggers_and_events_are_applied_once` |
| GET `/cases/{id}/stages`, `/stages/{stage}`, `/errors`, `/approvals`, `/dependencies` | — | `stage_executions`/`case_errors`/dependency graph | `case_reader_roles` | `test_the_api_exposes_stage_executions_errors_approvals_events_and_dependencies` |
| GET `/healthz` | `healthz` | none | none | implicit |

No endpoint in the required list (start/status/retrieve UC status/list pending approvals/approve-reject/
resume/retry/process events/retrieve errors) is missing. **Verdict: PASS**, with one minor gap noted
(§21, Finding F-1): the `pending-actions` route has no dedicated test.

---

## 15. Persistence and checkpointing verification

Covered jointly with §12. Key distinct finding: the implementation explicitly does **not** assume
LangGraph checkpointing persists UC business records — `deps.py` and `case_store.py` carry comments
documenting that this LangGraph version's own `get_state` was found unreliable, and resume logic
reads from the independent `CaseStateStore` instead. This is a correct, verified design choice, not
an assumption.

---

## 16. Failure handling and recovery verification

| Capability | Status | Evidence |
|---|---|---|
| Captures agent execution exceptions | PASS | `execution.invoke`, `except Exception` → `FAILED`/`INTERNAL_ERROR`, never a crash |
| Normalizes errors | PASS | `AgentError`/`ErrorCategory` (`contracts.py:159-216`) |
| Distinguishes validation/authorization/business/infrastructure | PASS | `classify_error()` + `_CATEGORY_BY_CODE` (170-190) |
| Bounded retries | PASS | `stage_max_attempts`, `_count_technical_failure` |
| Idempotency keys | PASS | per-stage `idempotency_keys`, worker-level `idempotency_key` on every call |
| Prevents duplicate side effects | PASS | `test_12_duplicate_triggers_and_events_are_applied_once`, UC05 welcome dedup (§7) |
| Preserves state on failure | PASS | `state_sync.py` rejects partial/invalid writes, keeps prior known values |
| Pause/resume | PASS | every `await_*` node, `resume_case()` |
| Recovery from worker timeout | PASS | `WorkerTimeoutError` → `UNCERTAIN`, never blind retry |
| Prevents blind retry of uncertain side effects | PASS | `UNCERTAIN` status is never auto-retried (explicit in routing table and `test_welcome_recovery.py`, 7/7 passed) |
| Audit records | PASS | `stage_history`, `field_provenance`, HITL decision records |
| Sanitized API errors | PASS | `_http_error()` (api.py:332-361) maps internal exceptions to HTTP codes without leaking stack traces |

UC02's import failure is handled identically to any other infrastructure failure (`UC02_PACKAGE_UNIMPORTABLE`,
non-retryable, requires an explicit `retry_stage` after the fix) — this is the correct behavior for a
defect outside the orchestrator's ownership.

---

## 17. End-to-end scenario results

All 12 required scenarios were mapped to actual test functions and independently re-run in this
audit (read-only `pytest` invocations). **All passed.**

| # | Scenario | Result | Test(s) |
|---|---|---|---|
| 1 | Successful onboarding UC01→UC05→Completed | **PASS** (real UC01/UC03/UC05; UC02 via PlanDouble, labelled) | `test_integration_scenarios.py::test_01_*` (3 variants) |
| 2 | UC01 failure blocks UC02-05 | **PASS** | `test_integration_scenarios.py::test_02_*`; mechanism confirmed via `flow.notifier.sent_count() == 0` |
| 3 | UC01 succeeds, UC02 fails, UC03 not incorrectly run | **PASS** | `test_dependencies.py::test_uc03_cannot_validate_before_uc02s_requirements_are_met_by_submitted_documents` (`flow.uc03.calls == 0`); `test_integration_scenarios.py::test_04_*` |
| 4 | UC03 invalid/incomplete docs → correction/review + blocking | **PASS** | `test_integration_scenarios.py::test_05_*`; `test_state_driven_workflow.py` (3 tests) |
| 5 | UC04 approval pending → pause, preserve, resume | **PASS** | `test_integration_scenarios.py::test_06_*` |
| 6 | Unauthorized approval refused, pending stays valid | **PASS** | `test_integration_scenarios.py::test_09_*`; `test_approval_authorization.py` (9/9) |
| 7 | UC05 scheduling conflict → recovery | **PASS** | `test_integration_scenarios.py::test_08_orientation_scheduling_conflict_with_the_real_uc05` (real UC05) |
| 8 | Restart/resume without duplicating | **PASS** | `test_integration_scenarios.py::test_10_*`; `test_durability_recovery.py`; `test_lifecycle_contracts.py` |
| 9 | Duplicate trigger / idempotency | **PASS** | `test_integration_scenarios.py::test_12_*`; `test_orchestrator_e2e.py::test_duplicate_offer_acceptance_links_existing_case_not_a_new_one`; `::test_03_duplicate_employee_record_reuses_the_existing_record_and_never_creates_a_second` |
| 10 | Tenant isolation | **PASS** | `test_integration_scenarios.py::test_15_*`; `test_state_driven_workflow.py::test_another_tenant_can_neither_read_nor_change_a_case`; `test_approval_authorization.py::test_a_cross_tenant_actor_cannot_decide`; `test_employee_identity.py` (tenant-scoped id resolution) |
| 11 | UC05 joining-day: trigger, welcome state, dedup | **PASS** (real UC05) | `test_integration_scenarios.py::test_13_*`, `::test_14_*`; `test_uc05_lifecycle.py::test_repeated_joining_day_event_drafts_and_sends_one_welcome` |
| 12 | HR Policies Q&A independently accessible | **PASS** | `test_orchestrator_e2e.py::test_uc05_policy_qa_still_works_independently_of_orchestration` — calls UC05's `run_event(POLICY_QUESTION)` directly, bypassing the orchestrator entirely |

**Scenario 1 caveat (important):** the "successful onboarding" tests that include UC02 task planning
use `PlanDouble`, not the real UC02 agent, because the real one cannot currently be imported. No
scenario in the existing suite demonstrates a real UC02 task plan flowing end-to-end. This is the one
gap in "all five real agents, all scenarios, fully verified."

---

## 18. Master verification matrix

| Requirement | UC01 | UC02 | UC03 | UC04 | UC05 | Evidence |
|---|---|---|---|---|---|---|
| Standardized communication | PASS | PASS (contract) / BLOCKED (real exec) | PASS | PASS | PASS | §8 |
| Input validation | PASS | PASS | PASS | PASS | PASS | `build_request()` ContractViolation checks; per-adapter missing-field checks |
| Output normalization | PASS | NOT VERIFIED (real exec never succeeded) | PASS | PASS | PASS | §3-7 |
| Shared context propagation | PASS | PASS (mapping exists; consumption blocked downstream) | PASS | PASS | PASS | §9 |
| Employee identity consistency | PASS | PASS | PASS | PASS | PASS | `test_employee_identity.py` (18/18) |
| Tenant context propagation | PASS | PASS | PASS | PASS | PASS | §9, §16 |
| Dependency validation | PASS | PASS | PASS | PASS | PARTIAL (UC04 optional for UC05, by design — see §10 #4) | §10 |
| Conditional routing | PASS | PASS (routes correctly to FAILED) | PASS | PASS | PASS | §11 |
| State synchronization | PASS | PASS | PASS | PASS | PASS | §12 |
| Error propagation | PASS | PASS | PASS | PASS | PASS | §16 |
| Retry/idempotency | PASS | PASS (non-retryable classification is itself correct) | PASS | PASS | PASS | §16 |
| Persistence/recovery | PASS | N/A (no own state created yet) | PASS | PASS | PASS | §12, §15 |
| End-to-end execution | PASS (real) | **FAIL** (real agent never executes) | PASS (real) | PASS (real) | PASS (real) | §17 |

---

## 19. Exact files and functions inspected

**Orchestrator core:** `orchestration/{orchestrator_agent,workflow,workflow_models,state,contracts,
agent_contract,dependencies,execution,state_sync,api,deps,bootstrap,case_store,tenant_config,
joining_day_scheduler,notifications}.py`, `orchestration/workers/{config,uc_worker_client,
uc_worker_runtime}.py`, `orchestration/adapters/{_shared,uc01_intake,uc02_document_collection,
uc02_tasks,uc03_documents,uc04_equipment_access,uc05_orientation}.py`.

**Tests:** all 25 files under `HR-Onboarding-Common/tests/orchestration/` (11,274 lines total),
including `conftest.py` and `_lifecycle.py`/`_uc05_fakes.py`/`_workflow.py`/`_helpers.py` harness
modules.

**Per-UC real entry points confirmed:** `UC01.../src/sub_agent/onboarding_entry.py:70`;
`UC02.../src/onboarding_task_management/sub_agent/adapter.py:193` (blocked by
`UC02.../src/onboarding_task_management/agent/advisor.py:1`); `UC03.../src/sub_agent/workflow.py:111`;
`UC04.../src/sub_agent/agent.py:230-232`; `UC05.../src/sub_agent/agent.py:349` and `tool_registry.py`.

---

## 20. Exact test commands and results

Environment setup performed (read-only w.r.t. source; recreates/repairs local `.venv`s from each
repo's own pre-existing, unmodified `uv.lock` — no `pyproject.toml`/`uv.lock` edited):

```
cd HR-Onboarding-Common && uv sync --frozen
cd UC01_Employee_Record_Creation && uv sync --frozen
cd UC03_Document_Validation && uv sync --frozen --all-extras
cd UC04_Equipment_Access_Integration_Ready && uv sync --frozen --all-extras
cd UC05_Orientation-Schedule-Agent && uv sync --frozen
# (hardlink-copy failures on this machine silently dropped some package contents; all five
#  re-installed with UV_LINK_MODE=copy --reinstall to get a verified-complete environment)
```

Primary full-suite run:
```
cd HR-Onboarding-Common
.venv/Scripts/python.exe -m pytest tests/orchestration
-> 417 passed in 137.53s (0:02:17)
```

Scenario/area breakdown (all read-only reruns against the same suite, all passed):
```
pytest tests/orchestration/test_orchestrator_e2e.py                -> 10/10
pytest tests/orchestration/test_integration_scenarios.py           -> 19/19
pytest tests/orchestration/test_dependencies.py test_routing.py
       test_state_driven_workflow.py                                -> 69/69
pytest tests/orchestration/test_durability_recovery.py
       test_module_isolation.py test_lifecycle_contracts.py         -> 30/30
pytest tests/orchestration/test_orchestrator_api.py
       test_approval_authorization.py test_employee_identity.py    -> 39/39
```

UC02 known-blocker reproduction (inside `UC02_Onboarding_Task_Agent`, its own venv):
```
python -c "import ast; ast.parse(open('src/onboarding_task_management/agent/advisor.py').read())"
-> SyntaxError: invalid syntax (line 1)

python -m pytest tests
-> Interrupted: 3 errors during collection (test_agent.py, test_fake_llm.py, testcase.py)
   all traced to advisor.py:1
```

No test was skipped or hidden; no result was invented. All 417 orchestration tests plus the targeted
UC02 reproduction were actually executed in this session.

---

## 21. Identified gaps and root causes

### Finding F-1 — UC02 real task-planning agent cannot execute (pre-existing, UC02-owned)
- **Requirement:** End-to-end execution (§17 Scenario 1), Output normalization (§18)
- **Status:** FAIL (real-agent execution) / COMPLETE (orchestrator-side handling)
- **File/function:** `UC02_Onboarding_Task_Agent/src/onboarding_task_management/agent/advisor.py:1`
- **Evidence:** `ast.parse` SyntaxError; `pytest` collection errors; orchestrator's own
  `adapters/uc02_tasks.py:53-64` catching it as `UC02_PACKAGE_UNIMPORTABLE`
- **Expected behavior:** `UC02SubAgent.run()` imports and produces a real task plan
- **Missing implementation:** a one-character-class fix — remove the stray `UC` token before the
  docstring's opening `"""`
- **Recommended fix:** UC02 owner removes the two characters at `advisor.py:1`; no orchestrator
  change needed; re-run `test_uc02_adapter.py::test_known_import_defect_surfaces_as_failed_not_a_fake_success`
  and `test_integration_scenarios.py::test_01_*` to confirm the real path now produces a non-double
  result
- **Responsible owner:** UC02 team (`logesh` branch per its CLAUDE.md)
- **Acceptance criteria:** `python -m unittest discover -s tests -t .` inside
  `UC02_Onboarding_Task_Agent` reports its documented 257 tests, 0 failures; the orchestrator's
  `plan_tasks` stage reaches `COMPLETE` with a result that did not come from `PlanDouble`

### Finding F-2 — `GET /cases/{case_id}/pending-actions` has no dedicated test
- **Requirement:** API and event entry-point verification (§14)
- **Status:** PARTIAL
- **File/function:** `orchestration/api.py:467-477` `pending_actions`
- **Evidence:** no test function name references this route specifically; it is exercised only
  incidentally as a byproduct of other flows
- **Expected behavior:** a direct test asserting the route's response shape, auth, and tenant scoping
- **Recommended fix:** add one focused test (not performed here — verification-only scope)
- **Responsible owner:** orchestrator team
- **Acceptance criteria:** a test named for this route exists and passes

### Finding F-3 — UC05 is not blocked on UC04 readiness by default (documented design choice, flagged for the record)
- **Requirement:** Dependency management (§10, scenario 4)
- **Status:** NOT APPLICABLE as stated in the task brief's literal table, by explicit design
- **File/function:** `dependencies.py:98-102`
- **Evidence:** the dependency is `kind=OPTIONAL, configurable=True`; comment: "no UC05 operation
  consumes UC04's result itself"
- **Note:** UC04 readiness **is** correctly required before `DAY_ONE_READINESS`/`JOINING_DAY_WELCOME`
  (the point where it actually matters for Day-1). No fix needed; flagged only because the task
  brief's dependency table assumes a direct UC04→UC05 edge that the business rules do not require.
- **Responsible owner:** none (informational)

### Finding F-4 — UC05's internal stage-name vocabulary is coupled into orchestrator state
- **Requirement:** Agent-specific implementation details not unnecessarily exposed to the orchestrator (§2 checklist)
- **Status:** PARTIAL (acknowledged in code, not hidden)
- **File/function:** `uc05_orientation.py:761-768`, `_derive_session_stage`
- **Evidence:** UC05 internal LangGraph stage names (e.g. `"scheduling_failed"`) are matched directly
  in orchestrator routing logic and surfaced in `blocker_action` text
- **Risk:** a UC05-internal refactor of these names would silently break orchestrator routing without
  a type error
- **Recommended fix:** UC05 could expose a small stable enum of outcome codes instead of internal
  stage-name strings; low priority, no observed failure in current tests
- **Responsible owner:** UC05 + orchestrator teams jointly
- **Acceptance criteria:** a contract test pins UC05's stage-name vocabulary so a rename fails loudly

No other defects, missing branches, or silent failures were found in the 417-test run or the direct
code inspection performed for this report.

---

## 22. Teammate-owned blockers

- **UC02 — `advisor.py:1` syntax error** (Finding F-1). This is the only blocker that prevented a
  required verification activity (a real-agent "successful task plan" execution) from being
  completed in this audit. It is explicitly pre-existing, documented in the orchestrator's own code
  comments, and not touched by this audit per the strict no-modification rule.

No other UC's source required a workaround; UC01, UC03, UC04, UC05 venvs required re-synchronization
(environment setup, not source changes) before their real-agent tests could run at all in this
environment — see §20.

---

## 23. Recommended remediation plan

1. **Immediate (UC02 owner):** fix `advisor.py:1` (remove the stray `UC` token). Zero orchestrator-side
   change required — the adapter already has the correct catch/fallback and will simply stop hitting
   it once UC02 imports cleanly.
2. **Follow-up (UC02 owner + orchestrator):** once UC02 imports, re-run the full orchestration suite
   and specifically confirm `test_integration_scenarios.py::test_01_*` and
   `test_uc05_lifecycle.py::test_real_uc02_defect_fails_task_planning_while_uc04_and_uc05_still_run`
   (the latter's name will then be stale and should be renamed/retired since the defect it documents
   will no longer exist).
3. **Low priority (orchestrator team):** add a dedicated test for `GET /pending-actions` (F-2).
4. **Low priority (UC05 + orchestrator, optional):** consider a stable outcome-code contract to
   reduce the internal-vocabulary coupling noted in F-4; not blocking.

---

## 24. Final integration readiness assessment

**UC01 ↔ orchestrator: production-ready**, verified by real subprocess execution across 417 tests
plus this audit's independent reruns.

**UC03 ↔ orchestrator: production-ready**, verified by real in-process LangGraph graph execution.

**UC04 ↔ orchestrator: production-ready**, verified by real subprocess execution, including a
UC04-side policy-rejection test.

**UC05 ↔ orchestrator: production-ready**, verified by real subprocess execution across the largest
and most complex integration surface in the system (5 operations, 4 relay types, identity mapping,
duplicate prevention) — the most thoroughly tested of the five.

**UC02 ↔ orchestrator: contract-ready, execution-blocked.** The orchestrator's side of the
integration (request building, dependency checks, error classification, branch isolation, retry
path) is correctly implemented and already defensively handles UC02's current unimportable state.
But because `advisor.py:1` has not been fixed, **no test in this repository — including this
audit — has ever observed UC02's real task-planning logic actually run.** The integration cannot be
called verified end-to-end until that one-line defect is fixed by its owner and the real-agent path
is re-tested.

**Overall system readiness: 4 of 5 UC integrations fully verified against real agent code; the
orchestrator's engineering (contracts, dependency graph, routing, state sync, persistence, API,
failure handling) is sound and consistently applied across all five, including the one that cannot
currently execute.** HR Policies Q&A was independently confirmed to remain accessible outside the
orchestrator (§17, Scenario 12). No NDA/policy-signature-tracking functionality was found wired into
the orchestrator (consistent with its documented exclusion). No mock-based test result was reported
here as equivalent to real-agent verification; every such test was labelled explicitly in §3-7 and
§17.
