# Orchestration end-to-end test report

Date: 2026-11-04. Every number below comes from a run made on this date. A test is listed as passed only if it ran.

The tests ran in three modes:

| Mode | What is real | What is a double |
|---|---|---|
| **Mode 2: local deterministic** | Orchestrator graph, `SupabaseCaseStateStore` (over HTTP), UC03 document collection, UC02's `OnboardingService` where noted | Database = in-process PostgREST (`tests/orchestration/_fake_postgrest.py`: PK, FK, tenant filters, atomic conditional PATCH); UC01/UC04/UC05 and UC03 validation = contract-shaped doubles (`_workflow.py`); LLM = FakeLLM |
| **Live Supabase** | The Supabase database (`case_orchestration_state`, `onboarding_cases`) | UC01 is a link to the authoritative case row; UC02–UC05 doubles; FakeLLM |
| **Mode 1: live OpenRouter** | OpenRouter through `LiteLLMGateway`; the real UC01–UC05 code, and the real worker processes in §4 | Calendar, email, Teams, HRIS and storage mocks; case store in memory |

No mock result is counted as evidence of an OpenRouter request or of a database write.

## 1. Suite totals (offline, `LLM_MODE=offline`)

| Suite | Before this change | After |
|---|---|---|
| HR-Onboarding-Common | **Did not import** (staged `uc02_tasks.py`); real-worker tests skipped (no UC01/UC04 venv) | **638 passed**, 1 skipped (the opt-in live Supabase test, run separately in §3) |
| UC01 | 997 passed, 3 failed | 1000 passed, 16 skipped (opt-in live-database tests) |
| UC02 | 2 collection errors, 1 failure | 1565 passed, 10 skipped, 2595 subtests |
| UC03 | 206 passed | 210 passed |
| UC04 | not runnable (no venv) | 686 passed, 1 skipped |
| UC05 | 1 failed (plus 6 errors once, not reproduced) | 670 passed, 3 skipped |

New in HR-Common:
- `tests/orchestration/test_e2e_supabase_orchestration_state.py`: 23 tests, scenarios A–G over the Supabase store.
- 3 provider-boundary tests covering every UC.
- 1 UC01 placement test.

The existing scenario suites still pass:
- `test_state_driven_workflow.py`
- `test_uc02_uc03_replanning_workflow.py`
- `test_joining_date_reconciliation.py`
- `test_execution_order.py`
- `test_durability_recovery.py`
- `test_orchestrator_e2e.py`, which uses the real UC01, UC04 and UC05 workers

## 2. Scenarios A–G (Mode 2, Supabase store)

All in `tests/orchestration/test_e2e_supabase_orchestration_state.py`. Each assertion reads the committed database row, not the in-memory state.

| Test | Scenario | Result | What it proves |
|---|---|---|---|
| A | `test_A_complete_workflow_every_stage_output_is_persisted_in_case_orchestration_state` | **pass** | Intake through the joining-day welcome. Every stage's `stage_outcomes` entry, `completed_stages`, `branch_statuses`, `verified_context` and `pending_actions` are persisted. Final `workflow_status=completed`, `overall_status=complete`, row `version` = state version. The provisional id resolves through the database. |
| A | `test_A_every_step_runs_in_a_new_process_from_the_committed_row_only` | **pass** | Four orchestrator processes, one per step; the next UC's input comes only from the row. UC01 and UC02 are not re-run; UC03 validates the persisted plan version; branches are not re-run after signoff. |
| B | `test_B_a_correctable_document_waits_for_correction_without_replanning` | **pass** | `validate_documents=waiting_for_input`, `replan_tasks=not_requested`, branches pending, `pending_actions=correct_documents`. Revision 2 is validated, and then the branches run; UC02 planned exactly once. |
| C | `test_C_a_genuine_plan_change_reaches_uc02_and_uc03_revalidates_against_the_new_version` | **pass** | Real UC02 `OnboardingService`. UC03's structured feedback goes to UC02, which produces plan v2 (v1 → v2). UC03 collects and validates against v2 (`plan_versions == [1, 2]`), `verified_context.plan_version == 2`, and no branch ran on the stale plan. |
| D | `test_D_both_branches_run_in_parallel_and_both_outcomes_are_persisted` | **pass** | A barrier forces the two branches to overlap on different threads; both results and both `stage_outcomes` are in the row. |
| D | `test_D_a_concurrent_writer_never_loses_a_branch_result_or_its_own_change` | **pass** | A second process writes the case mid-branch. The database refuses the stale branch write; the branch is re-folded and kept. The run stops with `StateVersionConflict` instead of overwriting; the resume keeps both, and UC04 is evaluated once. |
| D | `test_D_merge_waits_for_both_branches` | **pass** | One branch awaiting approval, so no readiness evaluation. |
| E | `test_E_supabase_unavailable_fails_loudly_writes_nowhere_else_and_resumes_after_recovery` | **pass** | `StatePersistenceUnavailable`; nothing written to the provisional store; row version unchanged; completes after recovery. |
| E | `test_E_a_duplicate_start_is_one_case_one_row_and_no_repeated_uc_call` | **pass** | One case, one row; UC01, UC02 and the document request once each. |
| E | `test_E_uc02_failure_holds_the_case_before_any_document_request` | **pass** | `plan_tasks=failed`, `last_error_code=UC02_PACKAGE_UNIMPORTABLE`, no collection, `workflow_status=failed`. |
| E | `test_E_uc03_technical_failure_is_retry_pending_and_recovers_on_retry` | **pass** | `RETRY_PENDING`, never an invalid document; an authorized retry completes it. |
| E | `test_E_uc04_failure_is_never_complete_and_uc05s_result_is_preserved` | **pass** | UC04 failed, UC05's result kept, no readiness, `workflow_status=failed`. |
| E | `test_E_uc05_scheduling_failure_blocks_signoff_and_uc04s_result_is_preserved` | **pass** | UC05 failed, UC04's result kept, no signoff request. |
| E | `test_E_a_process_stopping_after_a_successful_stage_resumes_from_the_committed_row` | **pass** | Killed at the start of UC03 validation. The new process resumes from the row; nothing before it re-runs and no document request is resent. |
| F | `test_F_another_tenant_can_neither_read_nor_change_the_case_and_the_query_is_tenant_scoped` | **pass** | Read, submit, resume and retry by another tenant all give `KeyError`. Every database query carried `client_id=eq.<that tenant>`; the row is unchanged. |
| F | `test_F_a_write_can_never_touch_another_tenants_row` | **pass** | `CaseOwnershipError`; the row is unchanged. |
| G | `test_G_a_joining_date_change_reopens_stale_branch_results_and_readiness_cannot_use_them` | **pass** | Approved date change: revision 2. Both branches re-ran with the new date (persisted `stage_outcomes[*].inputs.joining_date`). The old signoff was withdrawn and cannot be decided; the new signoff carries revision 2. |
| — | 6 store unit tests (row mapping round trip, foreign row refused, tenant/FK check on first write, database-enforced CAS, concurrent first writes, no credential in errors) | **pass** | — |

### LLM failure modes (Test E, gateway level)

Offline tests with a stand-in transport, all passing:
- `tests/llm/test_gateway_openrouter.py`: invalid output retried then falling back; transient retry with backoff; timeout sent and retried; attempt limit; non-transient not retried; refusal final; provider error without the key.
- `tests/llm/test_litellm_openrouter_transport.py`: rate limit; authentication failure without the key.
- At orchestrator level, `tests/orchestration/test_uc02_llm_gateway.py`: a gateway failure degrades UC02 without leaking provider text.
- UC03: `test_gateway_failure_routes_the_name_to_hr`, `test_invalid_structured_name_answer_routes_to_hr`, `test_provider_failure_returns_defaults_and_leaks_nothing`.

Live against OpenRouter: an invalid key and an unknown model id each gave a typed `LLMError` with no key in the message.

## 3. Live Supabase run

Command: `test_live_supabase_orchestration.py` with `ORCH_SUPABASE_LIVE=1`. Result: **1 passed (45 s)**.

Authorized synthetic case: tenant `SYN-UAE-TEST`, case `62e3b13e-4448-5959-9b2e-56908d33678b` (employee `SYN-EMP-0001`, joining date 2026-11-25, read from `onboarding_cases`). Each phase ran in a fresh orchestrator process.

```
after intake/plan/collection: version=7 workflow_status=waiting_for_input
CAS: second writer from version 7 refused; row version=8
after documents + parallel UC04/UC05: version=23 workflow_status=waiting_for_approval
completed: version=42 workflow_status=completed completed_stages=11
```

The committed row, read back independently over REST:

```
case_id 62e3b13e-4448-5959-9b2e-56908d33678b  client_id SYN-UAE-TEST  version 42
workflow_status completed  overall_status complete  last_error_code null
completed_stages: intake, plan_tasks, document_collection, document_submission, validate_documents,
  equipment_access, orientation_scheduling, buddy_assignment, team_introduction, day_one_readiness, joining_day_welcome
branch_statuses: uc04_equipment_access=completed, uc05_orientation=completed
stage_outcomes: 11 entries (one per executed stage)    pending_actions: []    blockers: []
verified_context: case_id, documents_verified, employee_id, joining_date, plan_id, plan_version, verified_at, verified_document_types
orchestrator_context.format hr_onboarding_common.orchestration/case_state@1  origin CASE-f4931c4d737e
joining_date 2026-11-25 (the authoritative onboarding_cases value)
```

The live run also showed:
- A cross-tenant write was refused with `CaseOwnershipError`, and the row was unchanged.
- A tenant-scoped load for another tenant returned nothing.

This row remains in the shared database as evidence. Re-running the test replaces it, because the test only deletes a row this orchestrator wrote.

## 4. Live OpenRouter runs (Mode 1)

| Run | Result |
|---|---|
| `openrouter_smoke_test.py --tier all` | 3/3 tiers OK, structured output validated |
| `openrouter_uc_live_check.py` | **5/5 UCs made verified real calls**: UC01 `discrepancy_summary`, UC02 `uc02.advise` (through the orchestrator's adapter), UC03 `name_consistency` + `document_extraction` + `draft_communication`, UC04 `advisory`, UC05 `policy_qa`. Latency and tokens are in OPENROUTER_COMPLETE_INTEGRATION_REPORT.md §3.2 |
| `run_online_orchestration_check.py` (real UC01/UC04/UC05 workers, UC02/UC03 in process) | See below |

The online run with real agents, stage by stage:

| Stage | Result | Model call |
|---|---|---|
| Intake (real UC01) | Complete; uuid case | No: UC01 decides deterministically |
| Plan (real UC02) | Complete | `uc02.advise` live, 6145 ms |
| Document submission (real UC03) | `awaiting_approval` | `uc03.name_consistency` live, 1893 ms, for a father-name variant; the model approved nothing |
| HR review | Validation complete | — |
| Real UC04 | **Failed**, `INVALID_INPUT` (synthetic-id contract, blocker B-2) | — |
| Real UC05 | **Blocked**, `UC05_OFFER_EVENT_NOT_FOUND` (blocker B-4) | — |
| Overall | `workflow_status=failed`; readiness never evaluated | — |

The real agents cannot complete a case together today. The complete workflow ran with the doubles (Mode 2) and with the live database (§3), and is reported as such.

## 5. Tests that use real services vs mocks

| Test or script | OpenRouter | Supabase | UC agents |
|---|---|---|---|
| Offline suites (all of §1) | no (FakeLLM / stand-in transport) | no (fake PostgREST or in memory) | Real code; doubles where noted |
| `test_live_supabase_orchestration.py` | no | **real** | Doubles; UC01 linked to the real case row |
| `openrouter_smoke_test.py`, `openrouter_uc_live_check.py` | **real** | no | Real UC LLM paths |
| `run_online_orchestration_check.py` | **real** | no (memory) | **Real** UC01–UC05 |

## 6. Commands

See PRODUCTION_READINESS_GAP_REPORT.md §7.
