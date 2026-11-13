# plan_tasks → Parallel Fan-out: Execution Verification

> **Historical report (2026-11-02).** The discrepancies it lists (D1–D8) were addressed afterwards:
> see [ORCHESTRATION_REMAINING_FIXES_COMPLETION_REPORT.md](ORCHESTRATION_REMAINING_FIXES_COMPLETION_REPORT.md).
> The test it calls `test_plan_tasks_completes_strictly_before_either_downstream_branch_starts` is now
> `test_stage_history_records_plan_tasks_before_either_downstream_branch`; timing is proven in
> `test_execution_order.py`.
>
> **Correction to §9.1:** without the worker variables the run was 307 passed, 1 failed and
> **118** skipped (the per-file results recorded then sum to 118), not "92 skips" as written below.

**Scope:** Whether the orchestration graph and its tests run `plan_tasks` (UC02) first and on its
own, then `equipment_access` (UC04) and `orientation_scheduling` (UC05) in parallel, then
`merge_outcomes`. Also checks how `joining_date` flows into the two downstream branches.

**Mode:** Verification only. No application source, graph definition, test, configuration or UC
file was modified. A SHA-256 manifest of all 680 tracked files in `HR-Onboarding-Common` and the
five UC folders (excluding `.venv`, `__pycache__`, `.git` and caches) was taken before any command
ran and again at the end. The two manifests are identical. Runtime probes were written as separate
scripts outside the repository (listed in §9).

**Date:** 2026-11-02

**Verdict:** **TEST INCORRECT.** The graph does what the design requires, and the pytest suite
matches the graph. The mismatch is in the scenario-4 walkthrough: `scripts/run_workflow_demo.py`
prints a heading that says UC02 planning runs in parallel with UC04 and UC05, and it does not.
Separately, the joining-date contract has a gap: a UC02 date change approved after the fan-out
does not reach branches that already ran (§8, §10).

---

## 1. Expected workflow

```text
plan_tasks (UC02)  -- runs alone; must COMPLETE
      |
      v
 fan-out ---------+--------------------------+
                  v                          v
          equipment_access (UC04)   orientation_scheduling (UC05)
                  |                          |
                  +------------+-------------+
                               v
                        merge_outcomes
                               v
                    evaluate_overall_status
```

The required diagram puts `dispatch_branches` *before* `plan_tasks`. In the actual graph the node
named `dispatch_branches` *is* the fan-out node, and it comes *after* `plan_tasks` (§2). This is a
naming and position difference only. Every one of the five critical requirements is about
behaviour, and all five hold for the graph (§2, §3). The test
`test_the_graph_runs_plan_tasks_before_fanning_out_to_the_two_branches` asserts this reversed
order on purpose: it checks that there is **no** `dispatch_branches → plan_tasks` edge.

## 2. Actual graph structure

The graph is built in `OnboardingOrchestratorAgent.build_graph`
(`src/hr_onboarding_common/orchestration/orchestrator_agent.py:611-671`). `agent.py` defines no
graph: it only re-exports `OnboardingOrchestratorAgent` lazily (`agent.py:179-191`).

### Edges that matter here

| Edge | Kind | Location |
|---|---|---|
| `validate_documents → record_verification` | conditional (`route_after_validation`, verified only) | `orchestrator_agent.py:196-213`, `:647` |
| `record_verification → plan_tasks` | static | `orchestrator_agent.py:654` |
| `plan_tasks → dispatch_branches` \| `await_plan_tasks` | conditional (`route_after_plan_tasks`: `dispatch_branches` only when `plan_tasks` is `complete`) | `:189`, `:648`, `_route` at `:161-167` |
| `dispatch_branches → equipment_access` | static (fan-out) | `:657-658` |
| `dispatch_branches → orientation_scheduling` | static (fan-out) | `:657-658` |
| `orientation_scheduling → buddy_assignment` \| `uc05_branch_end` | conditional (`_branch_route`) | `:190-191`, `:649` |
| `buddy_assignment → team_introduction` \| `uc05_branch_end` | conditional | `:192`, `:649` |
| `team_introduction → uc05_branch_end` | static | `:659` |
| `[equipment_access, uc05_branch_end] → merge_outcomes` | **join** (waits for both) | `:661` |
| `merge_outcomes → evaluate_overall_status` | static | `:662` |
| `evaluate_overall_status → dispatch_branches` \| `day_one_readiness` \| `await_branches` | conditional (`route_after_evaluation`; re-dispatch at most `MAX_DISPATCH_ROUNDS` = 3 times) | `:216-228`, `:650`, `:106` |

### Answers to §2's questions

1. **Does `dispatch_branches` route directly to `plan_tasks`?** No. The order is the other way
   round: `plan_tasks → dispatch_branches`, on the condition that `plan_tasks` is `complete`
   (`:648`, `:189`).
2. **Does `plan_tasks` have to complete before the fan-out?** Yes, and two separate mechanisms
   enforce it. (a) Topology: the only edge into `dispatch_branches` from upstream is
   `route_after_plan_tasks`, which returns `dispatch_branches` only for `complete`. Every other
   status goes to `await_plan_tasks → END` (`:238`, `:664-665`). (b) The dependency registry:
   `equipment_access` and `orientation_scheduling` both declare `_TASK_PLAN` as a **required**
   dependency. It needs `plan_tasks` to be `complete` and a non-empty `task_plan`
   (`dependencies.py:89-90`, `:101-106`). `execution.invoke` checks this before it calls any
   adapter (`execution.py:93-107`). An unmet check sets the stage to `BLOCKED` /
   `PREREQUISITE_NOT_MET`, and the UC is not called.
3. **Are the two branches registered as parallel successors only after `plan_tasks`?** Yes. Their
   only incoming edges come from `dispatch_branches` (`:657-658`).
4. **Is there a direct edge from `dispatch_branches` to either downstream node that skips
   `plan_tasks`?** The `dispatch_branches → branch` edges exist, but `dispatch_branches` can only
   be reached after `plan_tasks` has completed. Nothing leads from `validate_documents` or
   `record_verification` to `dispatch_branches`; the pytest suite asserts this
   (`test_state_driven_workflow.py:135-146`).
5. **Can any conditional route skip `plan_tasks`?** No. The only other route into
   `dispatch_branches` is `route_after_evaluation` (re-dispatch). `evaluate_overall_status` can
   only be reached through `merge_outcomes`, which is downstream of a dispatch that has already
   happened. The re-dispatch runs only branches whose `PREREQUISITE_NOT_MET` stage now has every
   required dependency met, `_TASK_PLAN` included (`_runnable_waiting_branches`, `:583-592`).
6. **Does `merge_outcomes` wait for both branches?** Yes. It is a LangGraph join over
   `["equipment_access", "uc05_branch_end"]` (`:661`). `uc05_branch_end` runs whether the UC05
   branch finished or stopped part-way (`:267-270`), so the join is always satisfied. A branch
   left out of a re-dispatch still runs its node and returns `{}` (`active_branches`,
   `:517-519`), so the join is satisfied then too.
7. **Can an interrupted or resumed workflow run downstream nodes before task planning is
   complete?** No. Nothing resumes a LangGraph checkpoint part-way through. There is no
   `invoke(None)`, `Command`, `interrupt` or `update_state` anywhere in `src/`. Every operation
   (`advance_case`, `resume_case`, `submit_documents`, `review_documents`, `decide_approval`,
   `retry_stage`, …) calls `_invoke` (`:830-884`), which runs `graph.invoke(state_input, …)` from
   the entry point `intake` with the case's stored state (`:859`). Completed and failed stages are
   skipped (`_NOT_RERUN`, `:111`; `_stage_node` `:496-507`; `_branch_node` `:509-529`).
   `route_after_plan_tasks` then checks the stored status again. `retry_stage` cannot target a
   `pending` branch stage (`:991-994`). Even if a branch stage were run directly, the dependency
   guard would block it (probe in §3.4). Runtime probes G and H below confirm this.

### State schema and reducers

- `Branch` has two members only: `uc04_equipment_access` and `uc05_orientation`
  (`state.py:56-63`). `BRANCH_STAGES` (`state.py:66-70`) does not include `plan_tasks`.
- `branch_outcomes` is the only channel with a reducer: `merge_branch_outcomes`
  (`state.py:164-171`, `:240`). Each branch writes under its own key, and `__reset__` clears the
  channel at dispatch (`:560`) and after the merge (`:570`).
- `plan_tasks` is a plain sequential `_stage_node`. It writes `task_plan` straight into shared
  state, which is allowed because the field is owned by `PLAN_TASKS` (`state_sync.py:58`).
- Branches never write shared fields directly. `merge_outcomes` folds each branch's outcome in,
  and each branch replaces only the fields it owns (`workflow.py:114-133`). `joining_date` is not
  owned by either branch (`state.py:71-75`, `BRANCH_RESULT_KEYS`).

## 3. Actual runtime execution order

Ordering was **not** inferred from Mermaid, from `completed_stages` or from print order. Two
independent sources were used:

- **LangGraph's own checkpoint history** (`graph.get_state_history`): which nodes LangGraph
  scheduled at each superstep.
- **Recording wrappers** around the stage functions, installed through the documented
  `OrchestratorDeps.stage_adapters` hook. Each wrapper logs entry and exit time
  (`perf_counter_ns`), the thread id, and the `joining_date` the stage received.

The orchestrator was built exactly as the demo's `build_synthetic_agent` builds it (same
stand-ins, real UC03 in-process, real UC04 adapter over the UC04 stand-in worker) and driven
through the same HTTP calls as scenario 4.

### 3.1 Supersteps (scenario 4, from LangGraph's checkpoints)

```text
step 21  record_verification
step 22  plan_tasks                                     <- alone
step 23  dispatch_branches                              <- alone, after plan_tasks finished
step 24  equipment_access, orientation_scheduling       <- same superstep
step 25  buddy_assignment
step 26  team_introduction
step 27  uc05_branch_end
step 28  merge_outcomes                                 <- only after both join inputs
step 29  evaluate_overall_status
step 30  day_one_readiness
step 31  day_one_signoff_proposal
step 32  await_day_one_blockers  (END)
```

(Step numbers count across all invocations on the case's thread. Only their relative order
matters.) `equipment_access` finished at step 24, but `merge_outcomes` did not run until step 28,
after `uc05_branch_end`. So the join really did wait for the longer UC05 chain.

### 3.2 Wall-clock intervals (ms since the probe started; thread ids differ)

| Run | plan_tasks | equipment_access | orientation_scheduling |
|---|---|---|---|
| A: normal timing | 238.706 → 239.016 | 243.765 → 248.723 (thread T1) | 245.319 → 245.926 (thread T2) |
| B: 300 ms delay in plan_tasks, 200 ms in each branch | 506.816 → 807.225 | 813.908 → 1015.060 (T1) | 815.281 → 1017.099 (T2) |

In both runs `plan_tasks` **exits** before either branch **enters**, and the two branch intervals
overlap on different threads. Run B rules out a timing coincidence. Even with a 300 ms planning
step, no branch started early, and the two 200 ms branches overlapped almost completely (about
200 ms of shared time, not 400 ms back to back). The `StageOutcome` timestamps the orchestrator
itself recorded agree: `plan_tasks` finished at `…35.881352`, before `equipment_access` started
at `…35.885198` and `orientation_scheduling` started at `…35.886647`.

### 3.3 Failure, wait and resume paths (same probe harness)

| Probe | plan_tasks result | Supersteps after plan_tasks | UC04 / UC05 adapter calls |
|---|---|---|---|
| C: `blocked` | blocked | `await_plan_tasks` → END | 0 / 0 |
| C, then `resume_case` once planning succeeds | complete | `dispatch_branches` → both branches in one superstep | 1 / 1 (planning ran 2 times in total) |
| D: `failed` | failed | `await_plan_tasks` → END | 0 / 0 |
| E: `awaiting_approval` (hypothetical: UC02 has no gate on the initial plan, see `uc02_tasks.py:19-20`) | awaiting_approval | `await_plan_tasks` → END | 0 / 0 |
| E2: `retry_pending` | retry_pending | `await_plan_tasks` → END | 0 / 0 |
| F: UC01 supplies no joining date | blocked, `MISSING_DATA` ("intake completed without joining_date") | `await_plan_tasks` → END | 0 / 0 (UC02 adapter also never called) |
| G: process stopped *inside* plan_tasks, new orchestrator resumes the same store and checkpointer | first process: stored `pending`; after resume: `complete` | resume: `plan_tasks` → `dispatch_branches` → both branches together | first process 0 / 0; after restart 1 / 1 |
| H: process stopped inside UC05 during the fan-out (UC04 had finished), new orchestrator resumes | complete (not run again) | resume: only `orientation_scheduling` calls its UC | after restart: planning 0, UC04 0, UC05 1 |

### 3.4 Dependency guard tested on its own

Setup: documents verified, `plan_tasks` blocked. `equipment_access` and `orientation_scheduling`
were then run directly through the orchestrator's stage wrapper (`_execute_stage`), the way a
retry, resume or override path could reach them. Both ended `blocked` / `PREREQUISITE_NOT_MET`.
The only unmet required dependency was `uc02.task_plan`, and the adapter was called 0 times.

### 3.5 Conclusion

- `plan_tasks` runs alone, in its own superstep.
- Neither downstream node starts until `plan_tasks` has finished with `complete`.
- UC04 and UC05 genuinely run at the same time: same superstep, separate threads, overlapping
  wall-clock intervals.
- `merge_outcomes` waits for both branches.

## 4. The test behind scenario 4

| Question | Finding |
|---|---|
| 1. Which file and function produce the output? | `scripts/run_workflow_demo.py`, `walk_through()` lines 213-216. The heading string is hard-coded at line 214, and line 216 prints the branch statuses. It runs from `main()` in default mode with `build_synthetic_agent()` (`:140-158`). **This is not a pytest test.** `scripts/` is outside `testpaths = ["tests"]` (`pyproject.toml:43`), and no pytest test prints this text. Re-running the script reproduced the reported output exactly: `workflow_status=waiting_for_approval current_stage=day_one_readiness`, `branches: {'uc04_equipment_access': 'completed', 'uc05_orientation': 'completed'}`. |
| 2. Real LangGraph app or simulation? | The **real** `OnboardingOrchestratorAgent` graph, called through the real FastAPI app (`POST …/documents/reviews` → `review_documents` → `_invoke` → `graph.invoke`). Real UC03 runs in-process. UC01 and UC04 are worker stand-ins behind the real adapters. UC02 `plan_tasks` and UC05 are stage-adapter stand-ins. |
| 3. Does `plan_tasks` run before UC04/UC05? | Yes, because the graph orders it (supersteps in §3.1). The script itself does nothing to order the stages. |
| 4. Is UC02 mocked? | Yes. `_plan_stand_in` (`:95-102`) replaces the UC02 adapter and returns `complete` with a synthetic `task_plan`. No UC02 code runs. In `--real` mode the real UC02 adapter fails on UC02's own `agent/advisor.py:1` SyntaxError (`UC02_PACKAGE_UNIMPORTABLE`). The same heading is printed anyway, even though both branches then stay `pending` (§9.3). |
| 5. Does it start all three nodes in parallel? | No. It makes one HTTP call, and the graph decides what runs. |
| 6. Does it call the run parallel when the graph is sequential then parallel? | **Yes. This is the discrepancy.** The heading `documents verified -> UC02 planning, UC04, UC05 run in parallel -> merge` is a hard-coded string left over from the earlier three-branch design. The printed `branches:` dict is correct: it has two keys, because `plan_tasks` is no longer a branch. So the script's own output contradicts its heading. |
| 7. Does it assert execution order? | No. The script asserts nothing. `_ok()` only exits on an HTTP status of 400 or above (`:187-190`). |
| 8. Does it check that UC04/UC05 got the joining date after UC02? | No. |
| 9. Does it check branch outputs and merge behaviour? | No. It prints the status of each branch only. |

## 5. Does the test suite match the graph?

**Graph vs pytest suite: yes.** The suite that checks this barrier is in
`tests/orchestration/test_state_driven_workflow.py` and
`tests/orchestration/test_plan_tasks_barrier_and_date_change.py`. All of its tests run the real
graph with test doubles. How strong each one's evidence is:

| Test | What it proves | How strong |
|---|---|---|
| `test_the_graph_runs_plan_tasks_before_fanning_out_to_the_two_branches` (`:135-146`) | The compiled graph's edges: no `dispatch_branches → plan_tasks`, no `plan_tasks → branch`, the fan-out edges, the join | Strong (structure only) |
| `test_plan_tasks_blocked_prevents_both_downstream_branches_from_starting` (`:149-165`) | `plan_tasks` blocked → `uc04.calls == 0`, `uc05.calls["orientation"] == 0`; after resume both run once | Strong (call counts) |
| `test_plan_tasks_failing_prevents_either_downstream_branch_from_running` (`:280-292`) | Real UC02 adapter → `failed` → 0 downstream calls | Strong |
| `test_equipment_access_and_orientation_scheduling_run_at_the_same_time` (`:126-132`) | A 2-party `threading.Barrier` (10 s timeout) inside the UC04 worker and the UC05 double; passes only if both are running at once on 2 threads | Strong for concurrency. `plan_tasks` takes no part in the barrier, so the test says nothing about ordering. |
| `test_plan_tasks_completes_strictly_before_either_downstream_branch_starts` (`:117-123`) | `_order(state)` (`:35-41`): the order in which stages *first appear* in `stage_history` | **Weak.** Branch history events are appended by `merge_outcomes`'s fold *after* `plan_tasks`'s events are already in shared state, so the list would put `plan_tasks` first even if timing were wrong. The test's name says "strictly before", but it compares no timestamps. |
| `test_parallel_branch_results_merge_without_overwriting_each_other` (`:306-321`), `test_the_branch_reducer_never_lets_one_branch_overwrite_another` (`:324+`) | Both branches' approvals, blockers and history survive the merge; the channel is cleared | Strong |
| `test_resume_and_retry_never_duplicate_a_side_effect` (`:374-390`) | Three resumes plus a duplicate start → every UC called once | Strong |
| `test_route_after_plan_tasks_fans_out_only_once_it_is_complete` (`test_routing.py:56-60`) | Every non-`complete` status routes to `await_plan_tasks` | Strong (the routing function on its own) |

Leftovers from the three-branch design in the test code (they do not change any result):

- `tests/orchestration/_workflow.py:56` defines `BRANCH_STAGES = (PLAN_TASKS, EQUIPMENT_ACCESS,
  ORIENTATION_SCHEDULING)`. It has the same name as `state.BRANCH_STAGES` but includes
  `plan_tasks`. The happy-path and structured-outcome tests use it.
- The docstring of `test_state_driven_workflow.py` (lines 3-4) still says "the parallel fan-out
  to UC02 task planning / UC04 / UC05".

## 6. Does UC02 run sequentially?

Yes. `plan_tasks` is a `_stage_node`, not a `_branch_node` (`:617`). It runs in its own superstep
(§3.1), and both downstream stages start only after it exits (§3.2). Caveat: UC02's real entry
point cannot run in this workspace (UC02's own `agent/advisor.py:1` SyntaxError, owned by the UC02
developer). Every successful `plan_tasks` run in this verification used a stand-in or test double.
The ordering does not depend on the adapter, though: routing, the dependency guard and the
superstep placement are all independent of what the adapter does. The real-UC02 *failure* path
was run (`test_plan_tasks_failing_prevents_either_downstream_branch_from_running`, demo
`--real`), and it blocks both branches.

## 7. Do UC04 and UC05 really run in parallel?

Yes:

- same superstep (§3.1);
- different threads, overlapping intervals (§3.2);
- the pytest barrier test passes, and it would fail with `BrokenBarrierError` if the two were
  serialized.

Inside the UC05 branch, `buddy_assignment` and `team_introduction` follow `orientation_scheduling`
in order, as UC05 requires. That is not a parallelism defect.

## 8. Where `joining_date` comes from and how it propagates

### 8.1 Source and ownership

| Step | Where | What happens |
|---|---|---|
| UC01 sets it | `adapters/uc01_intake.py:91` | `new_state["joining_date"] = case.get("joining_date")` from UC01's case. Provenance: `stage=intake, use_case_id=UC01`. |
| Ownership | `state_sync.py:46-48` | Only `INTAKE` and `PLAN_TASKS` may write `joining_date`. Any other stage that writes it is rejected with `STATE_OWNERSHIP_VIOLATION`. |
| Stale or empty overwrite | `state_sync.py:171-175` | An owner that returns `None` cannot erase a known value (`NULL_OVERWRITE_IGNORED`). |
| `plan_tasks` | `adapters/uc02_tasks.py:91`, `:118` | Reads `state["joining_date"]` and **never writes it**. The plan result has no date field. |
| Planning dependency | `dependencies.py:80`, `:100` | `plan_tasks` requires `uc01.joining_date`. If it is missing, the stage is `MISSING_DATA` and UC02 is not called (probe F). So the `date.today()` fallback at `uc02_tasks.py:118` can never be reached through the graph. |
| UC04 reads it | `adapters/uc04_equipment_access.py:52` | `"joining_date": state["joining_date"]` |
| UC05 reads it | `adapters/uc05_orientation.py:392-395`, `:531-536` | Blocks with `MISSING_DATA` if it is empty, and with `JOINING_DATE_MISMATCH` if UC05's directory date differs from `state["joining_date"]`. |
| Recorded per stage | `workflow.py:53-54` | Every stage's `StageOutcome.inputs.joining_date` comes from the same `state["joining_date"]`. |

### 8.2 Does UC02 have a real date-change contract?

Yes, but `plan_tasks` does not use it. UC02's real `DATE_CHANGE` gate
(`OnboardingService.request_change` / `decide_change` / `apply_change`) is reached through
`request_uc02_date_change` (`uc02_tasks.py:148-174`; API `orchestrator_agent.py:1148-1170`) and
`relay_uc02_decision` (`uc02_tasks.py:177-217`, routed from `decide_approval` at
`orchestrator_agent.py:1091-1099`). Only an **approved and applied** decision changes the date
(`uc02_tasks.py:210`). It records provenance `{stage: plan_tasks, use_case_id: UC02, source:
date_change_approval}`. A request on its own, or a rejection, changes nothing. Tests:
`test_pending_date_change_is_visible_and_a_rejected_one_changes_nothing`,
`test_a_change_to_the_same_date_is_refused_and_changes_nothing`.

A change can only be requested once `plan_tasks` is `complete` (`uc02_tasks.py:156-159`).

### 8.3 Results

- **No approved UC02 change:** UC01's date stays authoritative. In scenario 4, `plan_tasks`,
  `equipment_access` and `orientation_scheduling` all received `2026-12-01`, in the probe
  wrappers and in `StageOutcome.inputs`, and provenance stayed `intake/UC01`. **Both branches get
  the same date.** ✔
- **Approved change when neither branch has used the date yet:** this is the case that
  `test_updated_joining_date_reaches_both_downstream_branches` and tests 8-10 check. Both branches
  then run with the new date. ✔ But the tests reach that state with `_rewind_to_before_fanout`
  (`test_plan_tasks_barrier_and_date_change.py:99-115`), which writes branch statuses back to
  `pending` directly in the case store. In normal operation that state only occurs if the process
  stops between `plan_tasks` saving its result and the fan-out. That is because a date change can
  only be requested after `plan_tasks` is complete, and by then the same invocation has already
  dispatched both branches.
- **Approved change in the normal order (no rewind)**, probe output in §9.4: `joining_date`
  becomes `2026-12-15` with UC02 provenance. Both branches stay `complete` and are **not run
  again** (0 extra UC04/UC05 calls). Their recorded inputs, and UC05's `uc05_start_date`, stay at
  `2026-12-01`. No blocker or warning is raised, and the case goes on to the day-1 signoff.
  `uc02_tasks.py:37-38` documents this as a known limitation.
- **Approved change while one branch is stopped** (UC04 `complete`, UC05 `blocked`): after the
  approval, UC05 runs again with `2026-12-15`, while UC04's result is still based on
  `2026-12-01`. **The two branches end up with different dates**, and the case still moves on to
  `waiting_for_approval` at `day_one_readiness` without flagging it. ✘
- **Old values cannot overwrite the current one:** ✔ Branches cannot write `joining_date`
  (ownership rules), and the merge never touches it (`BRANCH_RESULT_KEYS`). One value does go
  stale: `verified_context.joining_date` is a copy taken at verification time
  (`orchestrator_agent.py:543`) and is never refreshed (it stayed `2026-12-01` after the change).
  No adapter reads it, but the API returns it (`api.py:315`). Also, `trigger_joining_day` uses
  UC05's `uc05_start_date` before `joining_date` (`orchestrator_agent.py:1129`), so after an
  approved change the joining-day check still uses the old UC05 date.

## 9. Test commands and exact results

Everything was run from `HR-Onboarding-Common` with `OPENBLAS_NUM_THREADS=1`, one test file per
process (this machine runs short of memory under parallel suites). The worker interpreters were
the environments `scripts/run_tests.ps1` builds under `%LOCALAPPDATA%\hr-onboarding-envs`. Nothing
was reinstalled.

### 9.1 Full orchestration suite

```bash
export OPENBLAS_NUM_THREADS=1
export UC01_WORKER_PYTHON="$LOCALAPPDATA/hr-onboarding-envs/uc01/Scripts/python.exe"
export UC04_WORKER_PYTHON="$LOCALAPPDATA/hr-onboarding-envs/uc04/Scripts/python.exe"
export UC05_WORKER_PYTHON="$LOCALAPPDATA/hr-onboarding-envs/uc05/Scripts/python.exe"
for f in tests/orchestration/test_*.py; do .venv/Scripts/python.exe -m pytest "$f" -p no:cacheprovider; done
```

**426 passed, 0 failed, 0 skipped** across 24 files:

| File | Result | File | Result |
|---|---|---|---|
| test_agent_contract | 50 passed | test_orchestrator_e2e | 10 passed |
| test_approval_authorization | 9 passed | test_plan_tasks_barrier_and_date_change | 7 passed |
| test_dependencies | 12 passed | test_routing | 18 passed |
| test_durability_recovery | 3 passed | test_rsvp_and_sensitive_data | 9 passed |
| test_employee_identity | 18 passed | test_state_driven_workflow | 42 passed |
| test_integration_scenarios | 19 passed | test_state_sync | 15 passed |
| test_joining_day_scheduler | 19 passed | test_uc01_adapter | 3 passed |
| test_lifecycle_contracts | 18 passed | test_uc02_adapter | 3 passed |
| test_module_isolation | 9 passed | test_uc03_adapter | 6 passed |
| test_orchestrator_api | 12 passed | test_uc04_adapter | 3 passed |
| test_uc05_adapter | 56 passed | test_uc05_lifecycle | 41 passed |
| test_uc_adapter_contracts | 37 passed | test_welcome_recovery | 7 passed |

Without the three `UC0X_WORKER_PYTHON` variables, the same run gives 92 skips and 1 failure:
`test_joining_day_scheduler.py::test_the_command_line_entry_point_runs_one_tick` raises
`WorkerConfigurationError: uc01 worker: configured python_executable does not exist`. That failure
comes from the environment and has nothing to do with stage order.

### 9.2 Focused ordering, merge and date tests (`-rA`)

All 20 tests passed: the 7 in `test_plan_tasks_barrier_and_date_change.py`, plus these 13 from
`test_state_driven_workflow.py`:

- `test_happy_path_…`
- `test_each_stage_records_…`
- `test_plan_tasks_completes_strictly_before_…`
- `test_equipment_access_and_orientation_scheduling_run_at_the_same_time`
- `test_the_graph_runs_plan_tasks_before_…`
- `test_plan_tasks_blocked_prevents_…`
- `test_plan_tasks_failing_prevents_…`
- `test_a_branch_waiting_for_approval_…`
- `test_parallel_branch_results_merge_…`
- `test_the_branch_reducer_…`
- `test_resume_and_retry_never_duplicate_…`
- `test_a_restarted_orchestrator_resumes_…`
- `test_real_uc03_verifies_only_after_an_hr_review_and_then_fans_out`

`20 passed in 3.41s`

### 9.3 Scenario 4 reproduced

```bash
.venv/Scripts/python.exe scripts/run_workflow_demo.py
```

```text
== 4. HR reviewer approves: documents verified -> UC02 planning, UC04, UC05 run in parallel -> merge
   workflow_status=waiting_for_approval  current_stage=day_one_readiness
   pending: decide_approval(apr-…)
   branches: {'uc04_equipment_access': 'completed', 'uc05_orientation': 'completed'}
```

```bash
.venv/Scripts/python.exe scripts/run_workflow_demo.py --real
```

```text
== 4. HR reviewer approves: documents verified -> UC02 planning, UC04, UC05 run in parallel -> merge
   workflow_status=failed  current_stage=plan_tasks
   pending: escalate(plan_tasks)
   branches: {'uc04_equipment_access': 'pending', 'uc05_orientation': 'pending'}
   plan_tasks: UC02_PACKAGE_UNIMPORTABLE -- onboarding_task_management is not importable: …
```

The `--real` run shows the barrier working with the real agents: UC02 fails, so UC04 and UC05
never start. The heading above it is wrong.

### 9.4 Runtime probes (outside the repository, verification only)

The scripts and their raw JSON output are in this session's scratchpad. They use only public
hooks (`stage_adapters`, `graph.get_state_history`, the test suite's own `Flow` and
`_real_uc02_flow`).

| Script | Covers | Section |
|---|---|---|
| `trace_plan_tasks_fanout.py` | Runs A-H | §3 |
| `probe_dependency_guard.py` | The dependency guard on its own | §3.4 |
| `probe_date_change.py` | Date change in the normal order | §8.3 |
| `probe_split_date.py` | Split date | §8.3 |

### 9.5 Section 6 scenario matrix

| Scenario | Expected | Evidence | Result |
|---|---|---|---|
| UC02 success | UC02 completes before UC04/UC05 start | Supersteps (§3.1), entry/exit times (§3.2), `StageOutcome` timestamps | **PASS** (the pytest evidence alone is weak: list order) |
| UC02 failure | Neither starts | `test_plan_tasks_failing_…` (real UC02), probe D | **PASS** |
| UC02 approval pending | Downstream not run | UC02 has no approval state for the initial plan. A date-change approval comes only after `plan_tasks` is complete. Probe E (hypothetical `awaiting_approval`): parks, 0 calls. `test_route_after_plan_tasks_…` | **PASS** (simulated status only; no real contract) |
| Valid joining date | Both branches get the same date | Probe A: both `2026-12-01` (wrapper and `StageOutcome.inputs`) | **PASS** when the fan-out runs. **FAIL** after a date change applied once the fan-out has run (§8.3) |
| Missing joining date | No stale fallback; blocked or error | Probe F: `plan_tasks` `MISSING_DATA`, 0 UC02/UC04/UC05 calls | **PASS** |
| Parallel execution | UC04 and UC05 run together after UC02 | Barrier test, same superstep, overlapping intervals | **PASS** |
| Merge | Both results kept | Merge and reducer tests; probe A (`equipment_access_result` and `orientation_schedule_result` both present) | **PASS** |
| Resume | Restarts from the right dependency stage | Probes C, G, H; `test_plan_tasks_blocked_…` resume; `test_a_restarted_orchestrator_resumes_…` | **PASS** |
| Retry | Completed work is not repeated | `test_resume_and_retry_never_duplicate_…`; probe H (after restart: planning 0, UC04 0, UC05 1) | **PASS** |

## 10. Discrepancies found

| # | Where | Discrepancy | Effect |
|---|---|---|---|
| D1 | **Test scenario text / printed output** | `scripts/run_workflow_demo.py:214` says "UC02 planning, UC04, UC05 run in parallel". `plan_tasks` actually runs alone, before the two branches. | Misleading. This is the discrepancy that was reported. The behaviour is correct. |
| D2 | **Test implementation (weak evidence)** | `test_plan_tasks_completes_strictly_before_either_downstream_branch_starts` relies on the order of `stage_history`, which the merge fold sets, not on timing. | Its name promises more than it checks. Other tests (structure, call counts) and this report's runtime trace make up the gap. |
| D3 | **Test implementation (leftover name)** | `_workflow.py:56` `BRANCH_STAGES` includes `PLAN_TASKS`; the docstring of `test_state_driven_workflow.py` (lines 3-4) says three parallel branches. | Confusing to readers; no effect on results. |
| D4 | **Test implementation (state not normally reachable)** | The date-change propagation tests use `_rewind_to_before_fanout`, which writes branch statuses directly in the store. The state it creates normally exists only after a process stops in a narrow window. | The normal case (branches already ran on the old date) is not tested. |
| D5 | **Joining-date contract** | An approved UC02 date change after the fan-out does not re-run branches that already finished, and raises no warning. If a branch was stopped, the two branches can end up with different dates (§8.3). | Requirement 3 holds only up to the moment the fan-out runs. Documented at `uc02_tasks.py:37-38` but not enforced or surfaced. |
| D6 | **Shared-state handling** | `verified_context.joining_date` is never refreshed after a change, and `trigger_joining_day` uses UC05's `uc05_start_date` first. | Old date exposed through the API and used by the joining-day trigger after a change. |
| D7 | **Documentation** | Still describes three parallel branches: `README.md:31`, `:95` (workspace); `docs/ORCHESTRATION_WORKFLOW.md:17`, `:74`; `docs/END_TO_END_INTEGRATION_REPORT.md:9`; `docs/LOCAL_TESTING_GAP_ANALYSIS.md:43`, `:669`; `HR-Onboarding-Common/docs/ONBOARDING_COMMON_ARCHITECTURE.md:24`, `:119`. Code comments: `state.py:42`, `state.py:166`, `orchestrator_agent.py:269`. `STATE_DRIVEN_ONBOARDING_WORKFLOW.md` §4 is correct. | Misleading for readers. |
| D8 | **Expected diagram vs graph (naming)** | The required diagram puts `dispatch_branches` before `plan_tasks`; the graph has `plan_tasks → dispatch_branches`. | None on behaviour. The graph meets all five critical requirements. Fix the diagram, not the graph. |

**No issue was found in the graph implementation** for the required sequence.

## 11. Recommended corrections

None of these were applied. All of them belong in `HR-Onboarding-Common`; none touches UC01-UC05
code.

1. **D1:** Change the heading at `run_workflow_demo.py:214` to
   `documents verified -> UC02 plan_tasks (sequential) -> UC04 + UC05 in parallel -> merge`, and
   print `stage_statuses["plan_tasks"]` next to the branch statuses. Optionally, run
   `walk_through` from a pytest test that asserts the statuses, so the walkthrough cannot drift
   again.
2. **D2:** Make the ordering test check timing, not list order. Either assert that
   `stage_outcomes["plan_tasks"]["finished_at"]` is at or before each branch's `started_at`, or
   assert superstep placement from `agent.graph.get_state_history(...)` (`plan_tasks` and
   `dispatch_branches` each alone, both branches together in the next superstep). The barrier test
   could also set a `threading.Event` when `plan_tasks` exits and assert it is already set when
   each branch enters.
3. Add a test that isolates the dependency guard: documents verified, `plan_tasks` blocked, run
   each branch stage through `_execute_stage` → `PREREQUISITE_NOT_MET`, `uc02.task_plan`
   unsatisfied, adapter not called (§3.4).
4. **D3:** Rename `_workflow.py:56` `BRANCH_STAGES` (for example to `POST_VERIFICATION_STAGES`) and
   update the docstring of `test_state_driven_workflow.py`.
5. **D4/D5:** Decide what should happen to the date after the fan-out, then test it without the
   rewind helper.
   - Option (a): an applied UC02 date change reopens `equipment_access` and
     `orientation_scheduling` (`complete → in_progress` is an allowed transition, see `_reopen`),
     so they run again with the new date. UC05 bookings would go through UC05's own reschedule
     operation.
   - Option (b), the minimum: before day-1 readiness, compare each required branch stage's
     `StageOutcome.inputs.joining_date` with `state.joining_date`, and raise a blocker such as
     `JOINING_DATE_STALE` instead of proposing signoff.

   Either way, add a test for the case where the branches have already run, and one where a single
   branch was stopped.
6. **D6:** Refresh or drop `verified_context.joining_date` when the date changes (or label it as a
   historical snapshot). Re-check the precedence `uc05_start_date` → `joining_date` in
   `trigger_joining_day` once item 5 is decided.
7. **D7:** Update the listed documents and code comments to "plan_tasks (sequential prerequisite)
   → UC04 ∥ UC05".
8. **D8:** Change the expected-workflow diagram to `… → plan_tasks → dispatch_branches (fan-out) →
   {equipment_access ∥ orientation_scheduling} → merge_outcomes`.
9. UC02's `agent/advisor.py:1` SyntaxError still prevents any real `plan_tasks` success. The UC02
   developer owns that fix; it is outside this repository's scope.

## 12. Final verdict

**TEST INCORRECT**

The **graph is correct.** `plan_tasks` runs alone and must complete. Only then does
`dispatch_branches` fan out to `equipment_access` and `orientation_scheduling`, which run in the
same LangGraph superstep on separate threads. `merge_outcomes` waits for both. Failure, wait,
missing-date, crash/resume and retry paths all hold the barrier. LangGraph's checkpoint history,
timed entry/exit records and call counts all confirm this; none of it rests on diagrams or
completed-stage lists.

The **pytest suite matches the graph** and passes in full (426/426).

The discrepancy is in the **scenario-4 walkthrough** (`scripts/run_workflow_demo.py:214`). It is
not a pytest test and asserts nothing, and its heading describes UC02 planning as a third parallel
branch. That is the earlier design, and the script's own printed branch list contradicts it.

Two further findings outside that verdict:

- **Joining-date contract gap (D5/D6):** both branches receive the same, authoritative date when
  the fan-out runs. But a UC02 date change approved afterwards does not reach branches that
  already ran, and can leave UC04 and UC05 on different dates without being flagged.
- **Weaker evidence in a few tests (D2-D4).**
