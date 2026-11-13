# Orchestration Remaining Fixes: Completion Report

**Date:** 2026-11-02 · **Follows:** [PLAN_TASKS_PARALLEL_EXECUTION_VERIFICATION.md](PLAN_TASKS_PARALLEL_EXECUTION_VERIFICATION.md)
(findings D1–D8) · **Scope:** `HR-Onboarding-Common` and the workspace `README.md` / `docs/`. No
UC01–UC05 file and no `hr-recruitment-common` file was changed (§13).

---

## 1. Executive summary

| Item | Result |
|---|---|
| Graph structure | **Unchanged.** The compiled graph has the same 44 edges as before the work (compared from the baseline code and the current code). `plan_tasks` runs alone; `dispatch_branches` follows it; UC04 and UC05 run in parallel; `merge_outcomes` joins them. |
| Scenario 4 description | Corrected. The demo now also prints the LangGraph steps it executed and each stage's start/finish. |
| Parallel-branch test constant | `BRANCH_STAGES` now holds only `equipment_access` and `orientation_scheduling`; `plan_tasks` moved to a separate `POST_VERIFICATION_STAGES`. |
| Joining-date reconciliation | **Implemented.** Revision + history on the one authoritative date, per-result date/revision stamps, stale detection, automatic re-execution of UC04, a read-only check of UC05 that stops for a person, withdrawal of an outdated signoff, readiness and joining-day protection. |
| Execution-order tests | Timing, deterministic gate, barrier and LangGraph-executed-superstep evidence; failure/approval/missing-date/regression cases. |
| Tests | **453 passed, 0 failed, 0 skipped** with the UC worker environments (was 426). |
| UC02 blocker | **Still present** (`advisor.py:1`, teammate-owned). Real `plan_tasks` success cannot be verified; it is a labelled double everywhere. |
| UC05 limitation | UC05 cannot change a case's start date. After a date change the orchestrator blocks with `JOINING_DATE_MISMATCH` until UC05's owner aligns UC05's record. Verified against the **real UC05 worker**. |

Bugs found and fixed during the work: a readiness that was awaiting signoff for the old date stayed
"awaiting approval" after the change (fixed in `joining_date.reconcile`); an unparseable joining date
(`2026-13-45`) passed the dependency check and reached UC04 (fixed in `dependencies._present`).

## 2. Corrected workflow structure

```text
record_verification -> plan_tasks --(complete)--> dispatch_branches -+-> equipment_access ------------------+
                           |                                         +-> orientation_scheduling -> buddy -> |
                           +--(anything else)--> await_plan_tasks        team_introduction -> uc05_branch_end
                                                                                                            v
                         merge_outcomes (join: both) -> evaluate_overall_status -> day_one_readiness -> signoff
```

Edges: `orchestrator_agent.py:679` (`record_verification → plan_tasks`), `:673` with
`route_after_plan_tasks` (`dispatch_branches` only when complete), `:683` (fan-out), `:686` (join).
Before every run, `_invoke` now calls `_reconcile_joining_date` (`:875`); this is not a graph change.

## 3. Scenario 4 heading correction

`scripts/run_workflow_demo.py`, `walk_through()` (`:229`), step 4 (`:244`):

```text
before: 4. HR reviewer approves: documents verified -> UC02 planning, UC04, UC05 run in parallel -> merge
after:  4. HR reviewer approves: documents verified -> UC02 plan_tasks runs alone and must complete
           -> dispatch_branches -> UC04 equipment_access + UC05 orientation_scheduling in parallel
           -> merge -> day-1 readiness
```

The step also prints what actually ran. Mock mode:

```text
   plan_tasks: complete  joining_date=2026-12-01 (revision 1)
   branches (UC04, UC05): {'uc04_equipment_access': 'completed', 'uc05_orientation': 'completed'}
   LangGraph steps: plan_tasks -> dispatch_branches -> equipment_access + orientation_scheduling -> buddy_assignment -> team_introduction
   plan_tasks              started 06:07:26.102270  finished 06:07:26.103930  (complete, joining_date 2026-12-01)
   equipment_access        started 06:07:26.111633  finished 06:07:26.118653  (complete, joining_date 2026-12-01)
   orientation_scheduling  started 06:07:26.113286  finished 06:07:26.115128  (complete, joining_date 2026-12-01)
```

`--real` (real UC02 fails on its own defect):

```text
   plan_tasks: failed  joining_date=2026-12-01 (revision 1)
   branches (UC04, UC05): {'uc04_equipment_access': 'pending', 'uc05_orientation': 'pending'}
   LangGraph steps: plan_tasks -> await_plan_tasks
   equipment_access        not started
   orientation_scheduling  not started
```

New helpers: `_print_executed_steps` (`:187`; reads the nodes that executed from the checkpoint
history) and `_print_fan_out_timing` (`:203`). `walk_through` now takes the agent. With the demo's
instant stand-ins the two branch calls do not always overlap in time; the helper's docstring says
so, and the overlap is proven in the tests (§11).

## 4. Parallel branch constant correction

`tests/orchestration/_workflow.py:59-60`:

```python
BRANCH_STAGES = (Stage.EQUIPMENT_ACCESS, Stage.ORIENTATION_SCHEDULING)
POST_VERIFICATION_STAGES = (Stage.PLAN_TASKS, *BRANCH_STAGES)
```

Every consumer was reviewed (all in `test_state_driven_workflow.py`). The eight assertions that
meant "everything after verification" (lines 55, 103, 123, 178, 207, 242, 455, 603) now use
`POST_VERIFICATION_STAGES`, so their coverage is exactly what it was. The happy-path order
assertion (`:85-87`) is stricter: `validate_documents < plan_tasks < min(both branches)`. No other
file used the constant.

## 5. Documentation and comment corrections

Every occurrence was found by search, then classified.

| File | Change |
|---|---|
| `README.md` (workspace) | Overview, Mermaid diagram and demo step 4: UC02 plans first, then UC04 ∥ UC05; date changes reconciled |
| `docs/ORCHESTRATION_WORKFLOW.md` | §1 sequence and diagram; §2 compiled-graph Mermaid **regenerated** from `get_graph()` (it still showed `validate_documents → dispatch_branches → plan_tasks`); join over two edges plus a note on LangGraph's `next`; §3 node table (`record_verification`, `plan_tasks` sequential, 10 `await_*`); §4 `route_after_validation` → `record_verification`, new `route_after_plan_tasks`, outcome rows; §5 `uc02.task_plan` shown as **required** (the doc said optional; the code always made it required), invalid date = missing; §6 signoff per revision, UC02 date-change approvals; §8 new "Joining-date change after the branches ran"; §9 resume note; §12 date-change route |
| `docs/SHARED_STATE_AND_CONTEXT.md` | Joining-date fields; `joining_date` ownership (UC01, revised only by an applied UC02 change) |
| `docs/UC_INTEGRATION_CONTRACTS.md` | UC04 idempotency key per revision |
| `docs/END_TO_END_INTEGRATION_REPORT.md`, `docs/LOCAL_TESTING_GAP_ANALYSIS.md` | **Historical (2026-10-30):** dated "superseded in part" note at the top; body left as written |
| `HR-Onboarding-Common/docs/ONBOARDING_COMMON_ARCHITECTURE.md` | State-driven summary; §2 "two parallel branches"; test table (two new files); §11 UC05 start-date row |
| `HR-Onboarding-Common/docs/STATE_DRIVEN_ONBOARDING_WORKFLOW.md` | §4 "Limitation (documented, not built)" replaced by "Joining-date revisions and reconciliation"; §5 schema rows |
| `HR-Onboarding-Common/docs/PLAN_TASKS_PARALLEL_EXECUTION_VERIFICATION.md` | **Historical:** note pointing here; renamed test; correction of its skip count (§14) |
| `state.py:42`, `state.py:166`, `orchestrator_agent.py:272` (`node_uc05_branch_end`), `workflow.py:44-46` | "three branches" → plan_tasks sequential + two branches |
| `adapters/uc02_tasks.py` module and `relay_uc02_decision` docstrings | the limitation text replaced by the reconciliation |
| `test_state_driven_workflow.py:1-4`, `test_plan_tasks_barrier_and_date_change.py:1-13` | test descriptions |

Left unchanged on purpose: lines that list stand-ins ("UC01, UC02 task planning, UC04 and UC05"),
approval-tagging comments, and the tests asserting `uc02_task_planning` is *not* a branch.

## 6. Joining-date source of truth

| Step | Where | Behaviour |
|---|---|---|
| UC01 sets it | `adapters/uc01_intake.py:91` | `joining_date` from UC01's case → revision 1 (`_execute_stage` calls `record_change`, `orchestrator_agent.py:466`) |
| UC02 `plan_tasks` | `adapters/uc02_tasks.py:94` | reads it, never writes it. UC02's plan result has no date |
| UC02 date change | `request_uc02_date_change` / `relay_uc02_decision` (`uc02_tasks.py:217`) | only an approved and applied DATE_CHANGE writes it: the exact date requested, as the next revision. Refused once the joining-day event is recorded (`:163`) |
| Ownership | `state_sync.py:46-49` | only `intake` and `plan_tasks` may change `joining_date`; no stage may write the revision or history |
| UC04, UC05 | `uc04_equipment_access.py:53`, `uc05_orientation.py` | read `state["joining_date"]` |
| Readiness, signoff, joining day | `node_day_one_readiness`, signoff payload, `trigger_joining_day`, scheduler | use the authoritative date. The joining-day paths no longer prefer UC05's `uc05_start_date`; they refuse while it differs |

There is one value: `joining_date`. `verified_context.joining_date` is kept equal to it.

## 7. Revision mechanism

New module `src/hr_onboarding_common/orchestration/joining_date.py`:

| Field / function | Meaning |
|---|---|
| `joining_date_revision` (`state.py:219`) | 0 while unknown, 1 once UC01 sets the date, +1 per change. Back-filled to 1 on states saved earlier (`state.py:415`) |
| `joining_date_history` | append-only: `revision`, `joining_date`, `previous_joining_date`, `stage`, `use_case_id`, `source`, `approval_id`, `at` |
| `field_provenance.joining_date` | the current value's source, `revision`, approval id. This replaces separate `joining_date_source` / `_updated_at` fields, which would have duplicated it |
| `record_change` (`:63`) | the only writer of a new revision |
| `StageOutcome.inputs.joining_date` + `joining_date_revision` (`workflow.py:56-58`) | the date and revision every stage was handed |
| `equipment_access_result.joining_date`, `.joining_date_revision`, `.reassessment` | what UC04's evaluation was for, and UC04's own EC-11 re-plan record |
| `joining_date_consistency` (derived, `workflow.py:234`) | `consistent` / `stale` / `conflict` / `not_started`, with each date-dependent stage's used date |
| API (`api.py:227`, `:310`) | the case view returns the revision, history and consistency |

## 8. Stale detection and reconciliation

**Detection** (`stale_results`, `:102`): a result from `equipment_access` or `orientation_scheduling`
is stale when it was produced for a date other than the current one. Date equality decides, not the
revision number:
- a result produced for the current date is never re-run, however many changes happened in between;
- after a change back to an earlier date, only the results produced for the intermediate date are
  re-run.

UC05's own start date for the case must also equal the joining date (`uc05_conflict`, `:117`).
`buddy_assignment` and `team_introduction` are not date inputs; UC05 schedules them from the start
date that `orientation_scheduling` checks.

**Reconciliation** (`reconcile`, `:165`). It runs in `_reconcile_joining_date`
(`orchestrator_agent.py:912`) before every graph run, and is written through first:

| Stale result in status | Action |
|---|---|
| `complete` | re-opened to `in_progress` (`stage_history` code `JOINING_DATE_CHANGED`); the graph runs it again in the same invocation |
| `blocked`, `awaiting_approval`, `retry_pending`, `in_progress`, waits | runs again on its own with the current date |
| `uncertain`, `failed` | left for a person (no write while an outcome is unknown; `retry_stage` for a failed one) |
| day-1 readiness evaluated for another date (any status except `in_progress`/`failed`) | re-opened; `day_one_signoff_approved` cleared |
| an undecided `day_one_signoff` request for another revision | withdrawn (`_withdraw_superseded_signoff`, `:930`; recorded in `approval_decisions` as kind `withdrawal`) |

**UC04** (`uc04_equipment_access.py:69-74`): from revision 2 on, the idempotency key becomes
`<key>.jd<revision>`. The reasons:
- UC04 refuses a reused key with a different payload. The real worker confirmed this.
- UC04's EC-11 reassessment re-plans its due dates for the new date.
- Within one revision the key is stable, so a retry or a resume after a crash replays UC04's stored
  answer rather than evaluating again.

**UC05** (`uc05_orientation.py:548`): UC05 fixes a case's start date at its intake
(`services/intake.py:74`) and has no operation to change it. The adapter re-reads UC05 (read only).
If UC05 holds another start date, `orientation_scheduling` is `blocked` with `JOINING_DATE_MISMATCH`
and a blocker for a person to align UC05's record. Nothing is rescheduled from the orchestrator.

**HITL** (`platform/hitl.py`): new status `Decision.WITHDRAWN` (`:50`) and
`HITLInbox.withdraw()` (`:247`).
- A withdrawal is not a decision: no approver is recorded.
- A later `decide` is refused, and "withdrawn" can never be given as a decision (`:199`).
- Withdrawn items are excluded from approval statistics (`:263`).

A **failed or stopped** reconciliation is resumable. The re-opened state and the new date are saved
before the graph runs. A process stop after UC04 answered is resumed by replaying UC04 under the
same key (no second evaluation). A failed re-evaluation holds readiness until `retry_stage`.

## 9. Day-one readiness protection

`node_day_one_readiness` (`orchestrator_agent.py:311`, check at `:325`). Readiness is `complete` or
awaiting signoff only when all of these hold:
- every readiness stage is complete (as before);
- no date-dependent result is stale;
- UC05 holds the current date.

Otherwise it is `blocked` with `JOINING_DATE_UNRECONCILED`, a blocker and a `next_action`, and no
signoff is requested.

The signoff request names the `joining_date` and `joining_date_revision` (`:373`). An approval is
applied only for the current revision (`:1121`). `trigger_joining_day` (`:1209`) and the scheduler
(`joining_day_scheduler.py:244`, `joining_date_unreconciled`) refuse while UC05's start date differs.

## 10. Execution-order test improvements

New file `tests/orchestration/test_execution_order.py`. Ordering evidence never comes from
`stage_history` or `completed_stages`. It comes from:
- the stage doubles' entry/exit timestamps and threads (`_workflow._Gate`, `:116`);
- a deterministic violation check made under the gate's lock at each branch's entry;
- a 2-party barrier only the branches wait on;
- the nodes LangGraph **executed** per superstep, read from checkpoint `tasks`.

| Spec test | Test | Evidence |
|---|---|---|
| 1 Sequential prerequisite | `test_plan_tasks_exits_before_either_branch_enters`, `test_langgraph_schedules_plan_tasks_alone_then_dispatch_then_both_branches_together` | 300 ms plan: `plan exit ≤ UC04 enter, UC05 enter`; no gate violation; `StageOutcome` times agree; supersteps `[plan_tasks]`, `[dispatch_branches]`, `[equipment_access, orientation_scheduling]`; `merge_outcomes` after `uc05_branch_end` |
| 2 Parallel execution | `test_equipment_access_and_orientation_scheduling_overlap_on_separate_threads` | barrier not broken; ≥ 250 ms overlap with 300 ms delays; distinct threads; each entered once |
| 3 Plan failure | `test_plan_tasks_not_completing_starts_neither_branch` [failed, blocked, retry_pending] | 0 branch entries; `await_plan_tasks`; no `dispatch_branches` executed |
| 4 Missing/invalid date | `test_a_missing_or_invalid_joining_date_starts_neither_task_planning_nor_a_branch` [None, `2026-13-45`, `next monday`] | `plan_tasks` `MISSING_DATA` before UC02 is called; 0 entries; no date invented |
| 5 Approval pending | `test_task_planning_awaiting_approval_starts_neither_branch_until_it_completes` | 0 entries while awaiting; after completion, plan exits before both enter |
| 11 Regression | `test_the_whole_workflow_runs_in_order_to_completion` | executed order document_submission → … → day_one_signoff_proposal; signoff; joining day → `completed` |

The old test `test_plan_tasks_completes_strictly_before_either_downstream_branch_starts` was renamed
`test_stage_history_records_plan_tasks_before_either_downstream_branch`, with a comment saying what
it does and does not prove. Its assertions are unchanged.

**Finding while writing these tests.** LangGraph's checkpoint `snapshot.next` can list
`merge_outcomes` while only `equipment_access` has arrived (1 run of 3 in one probe, 1 of 12 in
another). A class-level probe counted real executions across those 12 runs. `merge_outcomes` ran **exactly once per run**, always with
both branches' outcomes. So this is how LangGraph reports a half-filled join, not a defect. The
tests read executed `tasks`, not `next`.

## 11. Parallel execution and reconciliation test evidence

New file `tests/orchestration/test_joining_date_reconciliation.py` (15 tests):

| Spec test | Test | What it shows |
|---|---|---|
| 6 Updated date | `test_an_approved_change_reaches_both_branches_when_they_resume` | the UC04 worker payload and the UC05 double both received `2026-12-15`; UC04 key `….jd2`; signoff for revision 2 |
| 7 After both completed | `test_a_change_after_both_branches_completed_is_reconciled_before_signoff` | both re-opened and re-run; UC04 `reassessment.previous_joining_date = 2026-12-01`; UC05 holds the old date → `JOINING_DATE_MISMATCH`, `conflict`; old signoff `withdrawn`, deciding it raises `HITLError`; joining day refused; after UC05 is aligned only UC05 re-runs, then signoff for revision 2 → welcome → `completed`; a change after the joining day is refused |
| 8 One branch paused | `test_one_branch_paused_when_the_date_changes_never_leaves_the_branches_on_different_dates` | UC04 re-opened and re-run; UC05 resumed with the new date; both outcomes `(2026-12-15, 2)` -- the split found by the verification can no longer happen |
| 9 Multiple changes | `test_repeated_changes_never_leave_an_earlier_result_marked_current` | 12-01 → 12-15 → 12-22 → 12-01: after each, both results for the current date; keys `base, .jd2, .jd3, .jd4`; 3 signoffs withdrawn; further resumes run nothing |
| 10 Crash and idempotency | `test_a_process_stopping_mid_reconciliation_resumes_without_repeating_uc04s_evaluation` | SQLite store; process stops after UC04 answered; restart → UC04 replayed under the same key (evaluations stay 2), `plan_tasks` not re-run |
| 10 Failure | `test_a_failed_reevaluation_holds_readiness_until_an_authorized_retry` | UC04 unavailable → `failed`, no signoff; `retry_stage` → current |
| Guards | `test_readiness_refuses_signoff_for_results_produced_for_another_date`, `…while_uc05_holds_another_start_date`, `test_reconcile_leaves_an_uncertain_or_failed_stale_result_to_a_person` | defence in depth |
| Revision | `test_uc01_sets_revision_1_and_an_approved_uc02_change_is_the_next_revision`, `test_a_state_saved_before_revisions_existed_counts_its_date_as_revision_1` | history, provenance, API view, back-fill |
| Adapters | `test_the_uc05_adapter_blocks_when_uc05_holds_an_earlier_start_date` | read-only snapshot, `JOINING_DATE_MISMATCH` |
| **Real UC04 worker** | `test_the_real_uc04_reassesses_a_revised_date_under_a_new_key` | UC04's EC-11 `reassessment.required`, `previous_joining_date = 2026-12-01`; same-key replay works; the old key with the new date is refused by UC04 (`idempotency key reused with a different payload`) |
| **Real UC05 worker** | `test_with_the_real_uc05_a_change_at_signoff_is_held_because_uc05_keeps_its_start_date` | after a change at signoff: UC05 counters unchanged (nothing booked, cancelled or sent), `JOINING_DATE_MISMATCH`, no signoff, joining day refused |
| HITL | `test_a_withdrawn_request_is_no_decision_and_can_no_longer_be_decided` | withdraw / decide / stats |

Plus one scheduler case (`test_joining_day_scheduler.py`, `joining_date_unreconciled`).

**Stability.** Both new files were run 5 more times consecutively after the final changes (130 test
executions); `test_execution_order.py` had also run 8 times consecutively earlier. All passed.

**What is real in these tests:**
- UC02's `OnboardingService` (its DATE_CHANGE logic), via `tests/orchestration/_uc02_real.py`. This
  is the helper block moved out of `test_plan_tasks_barrier_and_date_change.py`, unchanged in
  behaviour.
- The real UC04 and real UC05 workers in the two tests marked as real.
- Elsewhere, contract-compatible doubles. `FakeUc04Worker` now models UC04's BR-21 idempotency and
  EC-11 reassessment. `Uc05Double` models UC05's fixed start date: by default it follows the case's
  date, and a test can pin an older one.

## 12. UC02 syntax blocker status

Still present (checked 2026-11-02). Issue report for the UC02 owner, ready to paste:

```text
UC02 (onboarding_task_management) is not importable
File:   UC02_Onboarding_Task_Agent/src/onboarding_task_management/agent/advisor.py, line 1
Error:  SyntaxError: invalid syntax
        UC"""The LLM layer: interpretation and recommendation only.
          ^
Cause:  a stray "UC" before the module docstring's opening triple quote.
Effect: the whole `agent` subpackage fails to import, so UC02SubAgent.run() -- UC02's task-planning
        entry point -- cannot be called. In the orchestrator, plan_tasks is FAILED
        (UC02_PACKAGE_UNIMPORTABLE) and UC04/UC05 never start (`run_workflow_demo.py --real`).
Fix:    delete the two characters "UC" so line 1 starts with the docstring's `"""`.
Verify: python -c "from onboarding_task_management.sub_agent.adapter import UC02SubAgent"
```

Until then:
- every successful `plan_tasks` in this work is a labelled double;
- UC02's DATE_CHANGE gate is exercised against its real `OnboardingService` (imported directly,
  bypassing the broken package `__init__`);
- **no full real-agent end-to-end run is claimed.**

## 13. Files changed

There is no git repository in the workspace, so changes were measured against a copy of every
editable file taken before the work, and a SHA-256 manifest of the whole workspace (excluding
`.venv` and caches).

**UC01–UC05:** byte-identical. **hr-recruitment-common:** not touched.

| File | +/− | Function / change |
|---|---|---|
| `src/…/orchestration/joining_date.py` | new, 202 | revision, history, stale detection, consistency, reconcile |
| `src/…/orchestration/orchestrator_agent.py` | +99 −17 | `_execute_stage` revision bump; `_invoke` → `_reconcile_joining_date`, `_withdraw_superseded_signoff`; `node_day_one_readiness` guard; `node_day_one_signoff_proposal` payload; `decide_approval` revision check; `trigger_joining_day` authoritative date; docstrings |
| `src/…/orchestration/adapters/uc02_tasks.py` | +15 −9 | `relay_uc02_decision` via `record_change`; `request_uc02_date_change` refused after the joining day; docstrings |
| `src/…/orchestration/adapters/uc04_equipment_access.py` | +10 | per-revision key; date, revision, reassessment in the result |
| `src/…/orchestration/adapters/uc05_orientation.py` | +12 | `_derive_orientation` start-date check |
| `src/…/orchestration/dependencies.py` | +19 −2 | `_iso_date`, `_WELL_FORMED`, "without a valid …" |
| `src/…/orchestration/state.py` | +18 −6 | new fields, `new_state`, `ensure_lifecycle_fields`, comments |
| `src/…/orchestration/workflow.py` | +9 −4 | `stage_inputs` revision; `refresh_workflow_view` consistency; comment |
| `src/…/orchestration/state_sync.py` | +2 −1 | bookkeeping field; comment |
| `src/…/orchestration/joining_day_scheduler.py` | +6 −2 | `eligibility` authoritative date, `joining_date_unreconciled` |
| `src/…/orchestration/api.py` | +8 −1 | `CaseView` fields |
| `src/…/platform/hitl.py` | +19 −2 | `Decision.WITHDRAWN`, `withdraw`, decide/stats guards |
| `scripts/run_workflow_demo.py` | +40 −4 | step-4 heading, `_print_executed_steps`, `_print_fan_out_timing`, `walk_through(agent)` |
| `tests/orchestration/_workflow.py` | +99 −16 | constants, `SimulatedCrash`, `_Gate` timing/violations/delays, `PlanDouble` gate, `Uc05Double` start date and received dates |
| `tests/orchestration/_lifecycle.py` | +37 −4 | `FakeUc04Worker` idempotency, reassessment, payloads, keys |
| `tests/orchestration/_uc02_real.py` | new, 90 | moved real-UC02 helpers |
| `tests/orchestration/test_execution_order.py` | new, 187 | §10 |
| `tests/orchestration/test_joining_date_reconciliation.py` | new, 465 | §11 |
| `tests/orchestration/test_plan_tasks_barrier_and_date_change.py` | +12 −77 | helpers moved out; docstrings |
| `tests/orchestration/test_state_driven_workflow.py` | +18 −14 | constant consumers, stricter order check, renamed weak test, docstring |
| `tests/orchestration/test_joining_day_scheduler.py` | +2 | one eligibility case |
| Documentation | see §5 | 10 documents |

Line endings: the repository uses CRLF. During the work, shell edits converted four files to LF and
left one with mixed endings; all touched and new files were normalized to CRLF before the final test
run, and the +/− counts above are after that normalization. The verification
report created in the previous task (LF) was normalized too, so its diff shows as a whole-file change;
its content change is the 9-line note.

## 14. Test commands and exact results

Environment: Windows, `HR-Onboarding-Common/.venv`, one test file per process (this machine runs
out of memory with parallel suites).

UC worker discovery needs three variables. The suite's `conftest.py` otherwise defaults each to that
UC's own `.venv`, which does not start on this machine:

| Variable | Value |
|---|---|
| `UC01_WORKER_PYTHON` | `%LOCALAPPDATA%\hr-onboarding-envs\uc01\Scripts\python.exe` |
| `UC04_WORKER_PYTHON` | `%LOCALAPPDATA%\hr-onboarding-envs\uc04\Scripts\python.exe` |
| `UC05_WORKER_PYTHON` | `%LOCALAPPDATA%\hr-onboarding-envs\uc05\Scripts\python.exe` |
| `OPENBLAS_NUM_THREADS` | `1` |

`UC0X_WORKER_SRC_DIR` defaults correctly. `scripts/run_tests.ps1` builds the environments and sets
the variables. It also reinstalls `HR-Onboarding-Common` into the UC05 environment, which was not
needed here.

```bash
cd HR-Onboarding-Common
export OPENBLAS_NUM_THREADS=1
E="$LOCALAPPDATA/hr-onboarding-envs"
export UC01_WORKER_PYTHON="$E/uc01/Scripts/python.exe" UC04_WORKER_PYTHON="$E/uc04/Scripts/python.exe" \
       UC05_WORKER_PYTHON="$E/uc05/Scripts/python.exe"
for f in tests/orchestration/test_*.py; do .venv/Scripts/python.exe -m pytest "$f" -p no:cacheprovider; done
```

| Run | Passed | Failed | Skipped | Notes |
|---|---|---|---|---|
| Full suite, workers configured (final run, 26 files) | **453** | **0** | **0** | was 426 before this work |
| Full suite, no worker variables | 332 | 1 | 120 | the failure is `test_the_command_line_entry_point_runs_one_tick` (`WorkerConfigurationError`: UC01 worker not found) -- environmental, as before |
| `test_execution_order.py` | 11 | 0 | 0 | ×8 earlier + ×5 final, all passed |
| `test_joining_date_reconciliation.py` | 15 | 0 | 0 | ×5 final, all passed; includes the real UC04 and UC05 workers |
| `scripts/run_workflow_demo.py` (mock) | -- | -- | -- | completes; output in §3 |
| `scripts/run_workflow_demo.py --real` | -- | -- | -- | stops at `plan_tasks` (UC02 defect); UC04/UC05 not started |

Per-file (final, workers configured):

| File | Passed | File | Passed |
|---|---|---|---|
| test_agent_contract | 50 | test_orchestrator_e2e | 10 |
| test_approval_authorization | 9 | test_plan_tasks_barrier_and_date_change | 7 |
| test_dependencies | 12 | test_routing | 18 |
| test_durability_recovery | 3 | test_rsvp_and_sensitive_data | 9 |
| test_employee_identity | 18 | test_state_driven_workflow | 42 |
| test_execution_order (new) | 11 | test_state_sync | 15 |
| test_integration_scenarios | 19 | test_uc01_adapter | 3 |
| test_joining_date_reconciliation (new) | 15 | test_uc02_adapter | 3 |
| test_joining_day_scheduler | 20 | test_uc03_adapter | 6 |
| test_lifecycle_contracts | 18 | test_uc04_adapter | 3 |
| test_module_isolation | 9 | test_uc05_adapter | 56 |
| test_orchestrator_api | 12 | test_uc05_lifecycle | 41 |
| test_uc_adapter_contracts | 37 | test_welcome_recovery | 7 |

**Correction to the previous report.** PLAN_TASKS_PARALLEL_EXECUTION_VERIFICATION.md §9.1 said the
run without worker variables had "92 skips". Its own per-file results sum to **118** (307 passed,
1 failed, 118 skipped). Today's 120 is those 118 plus the two new real-worker tests. A correction
note has been added to that report.

No existing test was weakened. Changes to existing tests:
- the moved helpers;
- the constant rename (§4);
- one stricter order assertion;
- the renamed weak test;
- one added scheduler case;
- the doubles' new capabilities, which existing tests do not depend on (all of them pass unchanged).

## 15. Remaining blockers and limitations

| # | Owner | Item |
|---|---|---|
| 1 | UC02 | `advisor.py:1` SyntaxError (§12). Real task planning and a real end-to-end run are unverified. |
| 2 | UC05 | No operation to change a case's start date. After an approved date change, UC05 must be aligned by its owner (or re-created) before `orientation_scheduling` completes; the orchestrator blocks with `JOINING_DATE_MISMATCH` until then. Sessions UC05 already booked (induction, team introduction) are UC05's to re-plan. The automatic success path for UC05 is shown only with the double modelling an aligned UC05. |
| 3 | UC04 adapter (pre-existing, not changed) | The UC04 payload carries `as_of_date = date.today()`. A retry or resume on a **different day** under the same idempotency key is therefore a different payload, and UC04 refuses it. This affects any UC04 retry across midnight, including a reconciliation interrupted overnight. Suggested fix: fix the evaluation date per attempt in state, or include it in the key. |
| 4 | Orchestrator (by design) | A stale result that is `uncertain` or `failed` is not reconciled automatically; it needs an operator or `retry_stage` (tested). |
| 5 | Coverage | The full graph with a date change was run with the UC04 double; the real UC04 worker was exercised at the adapter level (first evaluation, re-evaluation, replay, refused reuse). |
| 6 | LangGraph | `snapshot.next` over-reports join nodes (§10); use executed `tasks` as evidence. |
| 7 | Policy | A date change is refused once the joining-day event is recorded. Changing the date after the joining day was not in scope. |

## 16. Final verification assessment

| Definition of Done | Status |
|---|---|
| Verified LangGraph structure preserved | ✅ identical 44-edge compiled graph |
| `plan_tasks` sequential | ✅ executed-superstep and timing tests |
| `equipment_access` ∥ `orientation_scheduling` | ✅ barrier, overlap, separate threads, same superstep |
| Scenario 4 describes actual execution | ✅ heading, executed steps, timestamps; `--real` honest |
| `plan_tasks` removed from the branch list | ✅ |
| Three-branch documentation corrected | ✅ living docs fixed; historical ones annotated |
| Authoritative value plus revision | ✅ |
| Stale UC04/UC05 results detected | ✅ |
| Affected branches reconciled safely | ✅ UC04 automatically; UC05 read-only check that stops for a person (UC05 limitation, §15.2) |
| Signoff blocked until consistent | ✅ readiness guard, signoff bound to a revision, withdrawal |
| Execution-order tests verify actual timing | ✅ |
| Parallel tests prove overlap | ✅ |
| Failure, approval-pending, restart, retry tested | ✅ |
| Existing regression tests pass | ✅ 453/0/0 |
| UC02 blocker resolved or documented | ⚠️ documented (§12); owner must fix |
| No protected source modified | ✅ UC01–UC05 byte-identical |
| Report reflects verified behaviour | ✅ |

**Assessment:** the orchestrator side is complete and verified. The graph keeps its verified shape.
A joining-date change can no longer leave UC04 and UC05 on different dates, and no signoff is
possible while they disagree. What remains belongs to other owners:
- UC02's syntax error blocks real task planning.
- UC05 has no way to move a case's start date, so after a change it needs manual alignment.

The orchestrator now detects and stops on that, but cannot resolve it.
