# UC05 Orchestration — Implementation Report

Scope: connect UC05 (Orientation Scheduling, `employee-onboarding`) to the central orchestrator in
HR-Onboarding-Common and complete the orchestrator's lifecycle, persistence and API. The design is
in `ONBOARDING_COMMON_ARCHITECTURE.md`; this report records what was found, what changed, and
what was verified. Date: 2026-10-29.

## 1. Initial gaps discovered (Phase A audit)

| # | Gap | Where | Consequence before the change |
|---|---|---|---|
| G0 | Every `.venv` in the workspace was copied from another machine (`pyvenv.cfg` → `C:\Users\Prasanna Kumar\…`) | all six projects | No test suite could run on this machine |
| G1 | Test helpers hard-coded `D:\Projects\D-ACT\HR Onboarding Agent` | `tests/orchestration/_helpers.py`, `conftest.py` | 26 of 49 orchestration tests silently skipped (baseline: 23 passed, 26 skipped) |
| G2 | The UC05 worker built `FakeLLM()` with no handlers, never subscribed UC05's HITL listener, had no persistence option | `workers/uc_worker_runtime.py` | Any welcome draft raised `LLMError`; UC05 approvals could never be applied |
| G3 | No way to decide a UC05 approval through the orchestrator (UC05's inbox lives inside the worker process) | — | Buddy assignment, team introduction, welcome delivery unreachable (the analysis report's gap #5) |
| G4 | The adapter sent `event_id = case_id` to UC05's `offer_accepted` (UC05 does not know case ids); no identity/joining-date cross-check | `adapters/uc05_orientation.py` | Real cases resolved to `invalid_employee_data`; the wrong employee could have been scheduled with a mismatched reference |
| G5 | One UC05 stage only; no buddy, team-intro, buddy-intro, welcome stages; no joining-day trigger; resume of an `awaiting_approval` UC05 stage re-sent the same event and got the same answer forever | `state.py`, `orchestrator_agent.py` | The case could never progress past the induction approval |
| G6 | `WorkerTimeoutError` was never raised (`readline()` blocks forever); worker stderr was a never-read pipe (deadlock once it fills); tracebacks returned in `error_detail` and copied into case state | `workers/uc_worker_client.py`, runtime | Hangs; stack traces in state/API |
| G7 | `case_states` existed only in memory; `decide_gate` mutated it in place | `deps.py`, `orchestrator_agent.py` | No resume after an orchestrator restart (analysis gap #2) |
| G8 | Blockers and pending approvals appended again on every resume; `_await_node` used `setdefault` on a key that always exists, so `next_action` was never set; every resume raised another `day_one_signoff` request | `_shared.py`, `orchestrator_agent.py` | Duplicates in state and in the HITL inbox |
| G9 | No orchestrator HTTP API | — | Analysis gap #4 |
| G10 | No seam for contract-compatible doubles in front of UC01–UC04 | `deps.py` | The lifecycle could not be tested past UC02's defect except by editing state by hand |

Verified facts that shaped the design: UC05's lifecycle order comes from its own graph edges
(induction approval → buddy proposal → buddy approval → team intro); the buddy introduction is
only an HR-console operation with an HR-chosen start; the welcome is drafted only by UC05's
`joining_day` trigger (tenant + start date) and delivered once through its operation ledger; UC05
has no re-send path for an approved welcome after an operator releases an uncertain delivery.

## 2. Files created and modified

All changes are inside HR-Onboarding-Common, plus one new test file in UC05's `tests/`.

**Created**

| File | Purpose |
|---|---|
| `src/hr_onboarding_common/orchestration/case_store.py` | `InMemoryCaseStateStore` (default, a dict) and `SqliteCaseStateStore` |
| `src/hr_onboarding_common/orchestration/api.py` | Orchestrator HTTP entry points (`create_orchestrator_app`) |
| `tests/orchestration/_uc05_fakes.py` | Recording UC05 double with the real worker's reply shapes |
| `tests/orchestration/_lifecycle.py` | Test world: real graph + real UC05 worker + UC01–UC04 doubles |
| `tests/orchestration/test_uc05_lifecycle.py` | 41 integration tests (scenarios 1–20) |
| `tests/orchestration/test_orchestrator_api.py` | 7 API tests |
| `tests/orchestration/test_lifecycle_contracts.py` | 17 pure tests: routing, overall status, back-compat, store |
| `UC05_Orientation-Schedule-Agent/tests/test_orchestration_contract.py` | 5 tests pinning what the orchestrator relies on from UC05 |
| `docs/UC05_ORCHESTRATION_IMPLEMENTATION_REPORT.md` | This report |

**Modified**

| File | Change |
|---|---|
| `orchestration/adapters/uc05_orientation.py` | Rewritten: four stage nodes, boundary contracts, preflight, relays, HR operations |
| `orchestration/orchestrator_agent.py` | UC05 nodes and holds, routing table, stage wrapper (history/errors/attempts/write-through), case store, idempotent start, per-case locks, `decide_approval`, `retry_approval`, `trigger_joining_day`, `request_uc05_operation`, gate de-duplication and held rejections |
| `orchestration/state.py` | New stages and statuses, result fields, history/errors/attempts, `gate_rejections`, back-fill, overall-status derivation |
| `orchestration/contracts.py` | `OnboardingCaseInput.offer_acceptance_event_id`; `WorkerResponse.error_type`/`retryable` |
| `orchestration/deps.py` | `case_states` store, `stage_adapters` |
| `orchestration/bootstrap.py` | `ucs`, `persist_dir`, `stage_adapters`, `worker_timeout_s` |
| `orchestration/adapters/_shared.py` | De-duplicated blockers/approvals, per-stage clearing, `record_stage_error`, unused import removed |
| `orchestration/workers/uc_worker_runtime.py` | stdout protection, error classification, UC05 via `runtime.build_context`, relay ops, env-gated test hooks |
| `orchestration/workers/uc_worker_client.py` | Real timeout (reader thread; kill on timeout), stderr to a file, `kill()`, `stderr_tail()` |
| `guardrails/policy.yaml` | Tools for the new operations; role allowlists `case_operator_roles`, `case_reader_roles`, `joining_day_trigger_roles`, `uc05_operation_roles` |
| `tests/orchestration/_helpers.py`, `conftest.py` | Workspace path from the file's location; `UC0X_WORKER_PYTHON` honoured; an interpreter must actually start |
| `tests/orchestration/test_uc05_adapter.py` | Extended from 3 to 53 tests (the 3 originals unchanged) |
| `tests/orchestration/test_orchestrator_e2e.py` | One test adjusted — see §5 |
| `tests/orchestration/test_routing.py` | Two expected next-nodes adjusted — see §5 |
| `docs/ONBOARDING_COMMON_ARCHITECTURE.md` | Revised throughout |
| `HR-Onboarding-Common/.venv` | Rebuilt with `uv sync --frozen --group dev` (environment, not source) |

**Not modified** (verified by SHA-256 of every `.py/.yaml/.toml/.md/.json` file in UC01–UC05,
taken before the first edit and after the last test run): no file of UC01, UC02, UC03 or UC04
changed, and no UC05 source file changed. The only UC05 differences are the new
`tests/test_orchestration_contract.py` and, in UC05's `README.md`, the test count (500 → 505) with
a pointer to that contract test. The UC01–UC04 adapters in
Common (`uc01_intake.py` … `uc04_equipment_access.py`) are unchanged; `hr-recruitment-common` was
not touched. No UC05 source change was needed: every capability was reachable through its existing
public entry points.

## 3. UC05 capabilities connected

| Capability | Status | How |
|---|---|---|
| A. Orientation scheduling | Connected | Read-only preflight (event → directory employee, tenant-filtered, required fields) cross-checked against the case's employee and joining date, then UC05 `offer_accepted`; conflicts, invalid data, provider failures, uncertain bookings mapped; redelivery is UC05's own remedy |
| B. Buddy assignment | Connected | UC05 proposes inside the induction decision's run; the stage proposes through UC05's registry only when no buddy decision exists; ineligible (`IneligibleBuddyError`), rejected, no candidates handled; never auto-re-proposed after a rejection |
| C. Team introduction | Connected | Drafted by UC05 after buddy approval; approved by the team lead on record (UC05 enforces the person); drafting failures read from the decision run; HR reschedule |
| C. Buddy introduction | Connected (optional) | HR request with a start time after a buddy is assigned; approved by the buddy on record; never blocks readiness |
| D. Joining-day welcome | Connected | Only on the joining-day event, after readiness signoff; UC05's template, language and branding; HR approval; delivery once through UC05's ledger; uncertain → resolution, never a blind resend |
| HR Policies Q&A | Untouched and verified working | `run_event(policy_question)` through the worker |
| NDA / signature tracking | Not reintroduced | Verified by source and route scan |

Approvals of every UC05 gate are decided by the authenticated person through
`decide_approval`, which relays the decision into UC05's own HITL inbox; the orchestrator decides
nothing itself.

## 4. Workflow and routing implemented

```
intake → plan_tasks → validate_documents → equipment_access → orientation_scheduling
→ buddy_assignment → team_introduction → day_one_readiness (+ signoff) → joining_day_welcome → complete
```

Only `COMPLETE` proceeds; every other status parks the case at that stage's `await_*` node. The
routing table, the status meanings and the codes per situation are in
`ONBOARDING_COMMON_ARCHITECTURE.md` §2.3 and in `ROUTING_TABLE` (`orchestrator_agent.py`); a test
checks the table covers every `StageStatus` and that each UC05 route proceeds only on `COMPLETE`.

Decisions taken from the code, not assumed:
- UC05's own order (induction → buddy → team intro) is kept; introductions are not before orientation.
- The welcome runs after readiness signoff and only on the joining-day event; the case completes
  after the welcome. A premature joining-day event is refused (409); one received before signoff
  is kept and acted on once signoff is given.
- A rejected orchestrator gate holds until HR explicitly resumes (no other call re-raises it).

## 5. Tests executed and results

Commands (environments as in architecture §10; `UC0X_WORKER_PYTHON` pointing at the rebuilt
interpreters):

```
cd HR-Onboarding-Common && .venv/Scripts/python.exe -m pytest -o addopts="--import-mode=importlib" -q
cd UC05_Orientation-Schedule-Agent && <uc05 python> -m pytest -o addopts="--import-mode=importlib" -q
cd UC01_Employee_Record_Creation && <uc01 python> -m pytest -q
cd UC02_Onboarding_Task_Agent && <uc02 python> -m pytest -q ; <uc02 python> -m unittest discover -s tests -t .
cd UC03_Document_Validation && <uc03 python> -m pytest -q tests
cd UC04_Equipment_Access_Integration_Ready && <uc04 python> -m pytest
```

| Suite | Before (baseline, this machine) | After |
|---|---|---|
| HR-Onboarding-Common | 23 passed, **26 skipped** (G1) | **164 passed, 0 skipped** |
| UC05 standalone | 500 passed | **505 passed** (500 + 5 new contract tests) |
| UC01 | 690 passed | 690 passed |
| UC02 | pytest: 3 collection errors; unittest: 537 run, 3 errors, 1 skipped | identical (pre-existing, §6) |
| UC03 | 200 passed | 200 passed |
| UC04 | 307 passed, 5 skipped | 307 passed, 5 skipped |

Common breakdown: `test_uc05_adapter.py` 53 (the 3 original tests + 50 new),
`test_uc05_lifecycle.py` 41, `test_lifecycle_contracts.py` 17, `test_orchestrator_api.py` 7, the
other pre-existing files 46 — all 49 original tests pass, including the 26 that had never run here.

**Existing tests changed** (none deleted, skipped or weakened):
1. `test_orchestrator_e2e.py::test_day_one_signoff_requires_hr_approval_and_resumes_correct_case`
   — its short-circuit now also marks `buddy_assignment` and `team_introduction` complete (readiness
   requires them now), and its last assertion `overall_status == "complete"` became: readiness
   `complete`, welcome `waiting`, overall `in_progress`, case not complete. Signoff no longer closes
   the case; the joining-day welcome follows it.
2. `test_routing.py`: the expected next node after a complete orientation is now `buddy_assignment`
   (was `day_one_readiness`), and after complete readiness `joining_day_welcome` (was `complete`).
3. `_helpers.py`/`conftest.py`: environment discovery only (G1).

**Required scenarios → tests** (`test_uc05_lifecycle.py` unless noted):

| # | Scenario | Test(s) |
|---|---|---|
| 1 | Success through all stages | `test_successful_onboarding_through_every_stage`; API: `test_full_uc05_lifecycle_over_http` |
| 2 | Missing/invalid employee context | `test_missing_or_invalid_employee_context_blocks_orientation_and_writes_nothing` (6 cases); adapter: `test_invalid_employee_context_blocks_before_any_uc05_write` |
| 3 | UC02 task-planning failure | `test_real_uc02_defect_fails_task_planning_and_nothing_after_it_runs` (real UC02); `test_uc02_precondition_failure_blocks_and_resumes_once_fixed` (double) |
| 4–5 | Invalid documents block; corrected documents resume | `test_invalid_documents_block_dependent_stages_and_corrected_documents_resume` |
| 6 | Equipment/access not approved | `test_equipment_access_not_approved_keeps_uc05_untouched` |
| 7 | Orientation scheduling conflict | `test_orientation_conflict_blocks_then_resumes_once_the_calendar_is_free` |
| 8 | Buddy unavailable / ineligible | `test_no_eligible_buddy_blocks_until_one_exists`, `test_ineligible_buddy_is_never_assigned_…`, `test_rejected_buddy_is_not_re_proposed_automatically` |
| 9 | Introduction scheduling failure | `test_team_introduction_scheduling_failure_blocks_and_hr_reschedules`, `test_team_introduction_decided_by_someone_other_than_the_team_lead_…`, buddy-intro tests |
| 10 | Joining-day event at the right time | `test_joining_day_event_is_refused_before_the_joining_date`, `test_joining_day_event_before_signoff_is_kept_…`, `test_only_the_scheduler_or_hr_may_send_the_joining_day_event` |
| 11 | Rejected and pending approval | `test_pending_approval_parks_the_case_and_a_rejected_induction_is_released`, `test_rejected_day_one_signoff_holds_the_case_until_hr_resumes`, `test_an_approver_without_the_role_is_refused_by_uc05` |
| 12 | Repeated joining-day trigger | `test_repeated_joining_day_event_drafts_and_sends_one_welcome` |
| 13 | Repeated resume | `test_repeated_resume_repeats_no_side_effect`, `test_repeated_start_for_the_same_offer_resumes_the_same_case` |
| 14 | Worker timeout / crash | `test_worker_crash_mid_decision_is_uncertain_…`, `test_worker_timeout_stops_the_worker_…`; adapter: `test_worker_lost_during_the_offer_event_is_uncertain_and_never_resent` |
| 15 | Uncertain side effect | `test_uncertain_welcome_delivery_is_never_resent_and_hr_resolves_it`, `test_provider_outage_during_a_decision_is_retried_and_delivered_once` |
| 16 | Unauthorized access | `test_uc05_operations_need_an_operator_role`, `test_only_the_scheduler_…`; API: `test_reading_a_case_needs_a_reader_role_…`, `test_decisions_are_checked_by_the_owning_inbox_…`, `test_every_case_route_requires_a_verified_identity` |
| 17 | Cross-tenant access | `test_cross_tenant_access_to_a_case_is_indistinguishable_from_not_found`; API: 404 tests; adapter: `test_actor_of_another_tenant_is_refused_…`, `test_uc05_itself_refuses_another_tenants_offer_event_…` |
| 18 | Restart and recovery | `test_orchestrator_and_uc05_restart_resume_the_same_case_without_duplicates` (SQLite case store + SqliteSaver + UC05 `HR_AGENT_PERSIST=1`), `test_a_new_orchestrator_instance_continues_a_case_from_its_sqlite_case_store` |
| 19 | Completed stage not re-run when a later stage fails | `test_a_failure_in_a_later_stage_does_not_re_execute_completed_ones` (+ call counters in most tests) |
| 20 | HR Policies Q&A works; NDA/signature absent | `test_hr_policy_qa_still_answers_through_uc05`, `test_nda_and_signature_tracking_is_not_part_of_the_orchestration` |

**Integration honesty.** In the lifecycle and API tests UC05 is real (its code, its simulated
connectors, FakeLLM). UC01 and UC04 are fake worker processes that answer with their real result
shapes (the real UC01/UC04 adapters translate them); UC02 and UC03 are stage-level doubles. These
results are **not** a real end-to-end integration of UC01–UC04. Real UC01, UC03 and UC04 are still
exercised by the pre-existing adapter and e2e tests; real UC02 cannot run (§6).

**Lint and type checks.** Neither Common nor UC05 configures a linter or type checker. As a sanity
check only: ruff (default rules of 0.17) — the unused imports/variables and one single-element
slice it found in the new code were fixed; remaining findings are style rules the existing
codebase does not follow either (import ordering, FastAPI's `Depends` defaults, pre-existing
`noqa` markers). mypy (not configured) reports 82 strict-typing findings in `orchestration/` —
TypedDict copies, Optional narrowing after a guard, the `initial_state` override — of the same
kinds as in the unchanged UC01–UC04 adapters; none indicates a runtime defect, and the code paths
are covered by the tests above. Neither is enforced.

## 6. Existing failures and cross-team dependencies

| Owner | Item | Status / action |
|---|---|---|
| **UC02** | `src/onboarding_task_management/agent/advisor.py` line 1 is `UC"""The LLM layer…` — a stray `UC` before the docstring. `SyntaxError` makes `onboarding_task_management.agent` (and `UC02SubAgent.run`) unimportable. UC02's own suite: 3 collection errors (pytest), 3 errors (unittest). | Still present, not modified (no owner authorization). **Action for the UC02 owner:** delete the two characters before `"""` on line 1, re-run UC02's suite, then `test_real_uc02_defect_fails_task_planning_…` should be updated to expect a completed plan. Until then the orchestrator reports `UC02_PACKAGE_UNIMPORTABLE` and nothing after task planning runs. |
| UC01 + UC05 | No shared HRMS: UC01's `employee_no` and UC05's directory `employee_id` are separate identifier spaces | The preflight blocks on `IDENTITY_MISMATCH` rather than schedule a different person. Needs one agreed HRMS employee id. |
| UC01–UC04 | In-memory business data; lost on a worker restart | Durable stores in those agents (out of scope here). |
| UC04 | Synthetic `SYN-…` identifier patterns in its contracts | Identity translation before a real IdP. |
| UC05 data | Synthetic seed has no `ClientConfig` (welcome template/branding) for `CLIENT-TEST-A` | Each tenant needs one; the orchestrator reports `UC05_MISSING_DATA` (tested). |
| UC05 behaviour | No re-send path for an approved welcome after an uncertain delivery is resolved `released`; a team/buddy-intro decision by the wrong person consumes the HITL item | Documented; not changed (no defect blocking the required functionality). |

## 7. Persistence and recovery limitations

- The orchestrator's case store and checkpoints persist its projection only. They do not persist
  any UC's business data and do not make any external side effect exactly-once.
- SQLite (orchestrator `persist_dir`, UC05 `UC05_WORKER_DATA_DIR`) is single-host and optional;
  default local/test configuration stays in memory. Several orchestrator processes on one case
  need a shared lock and a server database behind the same `CaseStateStore` interface.
- After a UC05 worker restart **without** persistence, UC05's records are gone; affected stages stay
  `UNCERTAIN` and nothing is resent — an operator must reconcile with the providers.
- UC05's simulated calendar and notifier forget their bookings on restart (they are test doubles);
  with real providers the bookings persist outside the process. The restart test therefore checks
  that nothing is booked again, not that a pre-restart hold can still be confirmed.
- The LangGraph checkpoint of the run that executed intake contains the intake inputs (`id_number`,
  email); the case state drops them once intake completes.
- A worker that timed out is killed; it is restarted explicitly (`WorkerPool.restart`), not
  automatically.

## 8. Intentionally out of scope

- Any change to UC01–UC04 source, UC05 source, or `hr-recruitment-common`; fixing UC02's defect.
- NDA / policy-signature acknowledgement tracking (retired; not reintroduced).
- RSVP handling (`participant_responded`) and the attendance-conflict decision through the
  orchestrator: UC05 supports them, but they are participant-facing events outside the stage
  sequence; UC05's own routes remain the entry point, and the resulting `attendance_decision`
  approvals can be relayed like any other UC05 approval once surfaced.
- A per-UC HTTP API, or calling UC05's own HTTP API from the orchestrator.
- Automatic slot search for the buddy introduction (UC05 does not implement one).
- Real integrations (calendar, email/Teams, HRMS, IdP) and a production identity provider.

## 9. Remaining work before production

1. UC02 owner fixes `advisor.py:1`; then run a real UC01→UC05 integration (UC01 and UC03 real,
   UC04 with a tenant `CaseConfig`) end to end.
2. Agree one HRMS employee identifier across UC01 and UC05 (and translate UC04's synthetic ids).
3. A real `IdentityProvider` (JWT/JWKS) behind a trusted gateway for `orchestration/api.py`.
4. Real calendar/notification/email/HRMS connectors in UC05 (and UC01/UC04), advertising
   `idempotent_booking`/`idempotent_delivery` truthfully.
5. A server database for the orchestrator case store, LangGraph checkpoints and HITL, plus durable
   stores in UC01–UC04, and cross-process locking for more than one orchestrator instance.
6. A joining-day scheduler that calls `trigger_joining_day` per case (UC05's own scheduler drafts
   per tenant; the orchestrator's trigger is idempotent with it).
7. Worker supervision (restart policy, health checks) and alerting on `UNCERTAIN`/`at_risk` cases.
8. Decide whether the intake run's checkpoint may hold intake inputs, or pass them out of band.
9. Optionally configure ruff/mypy for Common and bring the orchestration package to that bar.
