"""Execution order and concurrency of intake -> plan_tasks (UC02) -> document collection, submission and
validation (UC03) -> dispatch_branches -> {equipment_access, orientation_scheduling} on the REAL
orchestrator graph, proven from what actually ran -- never from the order of stage_history or
completed_stages:

  - the stage doubles record each call's entry and exit (perf_counter_ns) and thread on a shared gate
    (_workflow._Gate), with controlled delays so a misordering or a serialization would show;
  - `_Gate.violations` records, under the gate's lock at the instant it happens, a branch that
    entered while plan_tasks or UC03's validation had not finished -- deterministic, not a timing estimate;
  - a 2-party threading.Barrier that only the two branches wait on passes only if both are running
    at the same time (BrokenBarrierError after its timeout otherwise);
  - LangGraph's own checkpoint history (graph.get_state_history) names the nodes the runtime
    scheduled in each superstep.

What is real and what is a double: see _workflow.py. plan_tasks and validate_documents are
contract-compatible doubles here, so this proves the orchestrator's ordering; the real UC02/UC03 run is
in test_uc02_uc03_replanning_workflow.py.
"""

from __future__ import annotations

import threading

import pytest

from _workflow import HR, HR_2, JOINING, SCHEDULER, Flow, code, status
from hr_onboarding_common.orchestration.state import Stage, StageStatus, WorkflowStatus

S, ST, W = Stage, StageStatus, WorkflowStatus
BRANCHES = ["equipment_access", "orientation_scheduling"]


def _supersteps(flow: Flow, case_id: str) -> list[list[str]]:
    """The nodes LangGraph EXECUTED in each superstep of the case's last invocation, in order: the
    tasks of each checkpoint that carry a result or an error. Not `snapshot.next`, which can list a
    join node (merge_outcomes) while its barrier is only half-filled -- it then runs once, later."""
    run = []
    for snapshot in flow.agent.graph.get_state_history({"configurable": {"thread_id": case_id}}):  # newest first
        run.append(sorted(t.name for t in snapshot.tasks if t.result is not None or t.error is not None))
        if snapshot.metadata.get("source") == "input":
            break
    return [step for step in reversed(run) if step]


def _ms(ns: int) -> float:
    return ns / 1e6


# ============================================================ 1. sequential prerequisite


def test_plan_tasks_exits_before_either_branch_enters():
    flow = Flow()
    flow.gate.delay["plan_tasks"] = 0.3  # a slow plan: a branch started early would enter inside this window
    state = flow.to_branches()

    plan_in, plan_out = flow.gate.interval("plan_tasks")
    uc04_in, _ = flow.gate.interval("equipment_access")
    uc05_in, _ = flow.gate.interval("orientation_scheduling")
    assert _ms(plan_out - plan_in) >= 300
    assert plan_out <= uc04_in and plan_out <= uc05_in, "a branch entered before plan_tasks exited"
    assert flow.gate.violations == []  # checked at each branch's entry, under the gate's lock
    # The orchestrator's own per-stage timestamps agree.
    outcomes = state["stage_outcomes"]
    assert outcomes["plan_tasks"]["finished_at"] <= outcomes["equipment_access"]["started_at"]
    assert outcomes["plan_tasks"]["finished_at"] <= outcomes["orientation_scheduling"]["started_at"]


def test_langgraph_schedules_uc01_uc02_uc03_sequentially_then_both_branches_together():
    flow = Flow()
    state = flow.start()
    # The first invocation ran under the id the case was started with (UC01 renames it at intake).
    first = [s for s in _supersteps(flow, state["intake_trigger"]["origin_case_id"]) if s != ["__start__"]]
    # form_approval is the graph's entry: an attested in-process start passes straight through it.
    assert first[:7] == [["form_approval"], ["intake"], ["intake_mismatch_proposal"], ["plan_tasks"],
                         ["document_collection"], ["document_submission"], ["await_documents"]]  # up to the wait
    assert not any(n in step for step in first for n in ("validate_documents", "dispatch_branches", *BRANCHES))
    state = flow.supply(state)
    steps = _supersteps(flow, state["case_id"])
    i = steps.index(["validate_documents"])
    assert steps[i + 1:i + 4] == [["evaluate_document_result"], ["record_verification"], ["dispatch_branches"]]
    assert steps[i + 4] == BRANCHES  # one superstep: both branches, nothing else
    assert all(n not in step for step in steps[i + 1:] for n in ("plan_tasks", "validate_documents"))
    assert steps.index(["merge_outcomes"]) > steps.index(["uc05_branch_end"]) > i + 4  # the join waited for UC05
    assert flow.gate.violations == []


# ============================================================ 2. parallel execution


def test_equipment_access_and_orientation_scheduling_overlap_on_separate_threads():
    flow = Flow()
    flow.gate.barrier = threading.Barrier(2, timeout=10)  # passes only if both branches are in their calls at once
    flow.gate.delay.update(equipment_access=0.3, orientation_scheduling=0.3)
    state = flow.to_branches()

    assert not flow.gate.barrier.broken
    uc04_in, uc04_out = flow.gate.interval("equipment_access")
    uc05_in, uc05_out = flow.gate.interval("orientation_scheduling")
    overlap = min(uc04_out, uc05_out) - max(uc04_in, uc05_in)
    assert _ms(overlap) >= 250, f"the branch calls overlapped for {_ms(overlap):.0f} ms only"
    assert flow.gate.threads["equipment_access"] != flow.gate.threads["orientation_scheduling"]
    # Each branch was invoked exactly once, independently, after plan_tasks.
    assert flow.gate.entered("equipment_access") == flow.gate.entered("orientation_scheduling") == 1
    assert flow.gate.violations == []
    assert status(state, S.EQUIPMENT_ACCESS) == status(state, S.ORIENTATION_SCHEDULING) == ST.COMPLETE.value


# ============================================================ 3. plan_tasks failure


@pytest.mark.parametrize("plan_status", [ST.FAILED, ST.BLOCKED, ST.RETRY_PENDING])
def test_plan_tasks_not_completing_starts_neither_document_collection_nor_a_branch(plan_status):
    flow = Flow()
    flow.plan.status = plan_status
    state = flow.start()

    assert flow.gate.entered("plan_tasks") == 1
    assert flow.gate.entered("validate_documents") == 0 and flow.notifier.sent_count() == 0
    assert flow.gate.entered("equipment_access") == flow.gate.entered("orientation_scheduling") == 0
    assert flow.uc04.calls == 0 and flow.uc05.calls["orientation"] == 0
    steps = _supersteps(flow, state["intake_trigger"]["origin_case_id"])  # the starting invocation's thread
    i = steps.index(["plan_tasks"])
    assert steps[i + 1] == ["await_plan_tasks"]
    assert not any(n in step for step in steps for n in ("document_collection", "dispatch_branches"))
    for stage in (S.DOCUMENT_COLLECTION, S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING):
        assert status(state, stage) == ST.PENDING.value
    assert state["current_stage"] == S.PLAN_TASKS.value


# ============================================================ 4. missing or invalid joining date


@pytest.mark.parametrize("joining_date", [None, "2026-13-45", "next monday"])
def test_a_missing_or_invalid_joining_date_starts_neither_task_planning_nor_a_branch(joining_date):
    flow = Flow()
    flow.uc01.joining_date = joining_date  # UC01's record carries no usable date
    state = flow.start()

    assert status(state, S.PLAN_TASKS) == ST.BLOCKED.value and code(state, S.PLAN_TASKS) == "MISSING_DATA"
    assert status(state, S.DOCUMENT_COLLECTION) == ST.PENDING.value and flow.notifier.sent_count() == 0
    assert "joining_date" in state["stage_errors"]["plan_tasks"]["detail"]
    assert flow.gate.entered("plan_tasks") == 0  # the dependency check stops it before the UC is called
    assert flow.gate.entered("equipment_access") == flow.gate.entered("orientation_scheduling") == 0
    (check,) = [c for c in state["stage_outcomes"]["plan_tasks"]["dependencies"] if c["key"] == "uc01.joining_date"]
    assert (check["satisfied"], check["code"]) == (False, "MISSING_DATA")
    # No fallback: no stage was handed a date, and none was invented.
    assert state["joining_date"] == joining_date and state["joining_date_revision"] == (1 if joining_date else 0)


# ============================================================ 5. task planning awaiting approval


def test_task_planning_awaiting_approval_starts_neither_branch_until_it_completes():
    """UC02 has no approval gate on the initial plan (adapters/uc02_tasks.py); this drives the
    status through the double to prove the routing holds for it as for every non-complete status."""
    flow = Flow()
    flow.plan.status = ST.AWAITING_APPROVAL
    state = flow.start()
    assert status(state, S.PLAN_TASKS) == ST.AWAITING_APPROVAL.value
    assert status(state, S.DOCUMENT_COLLECTION) == ST.PENDING.value
    assert flow.gate.entered("equipment_access") == flow.gate.entered("orientation_scheduling") == 0
    assert state["workflow_status"] == W.WAITING_FOR_APPROVAL.value and state["current_stage"] == S.PLAN_TASKS.value

    flow.plan.status = ST.COMPLETE  # the approval has been given in UC02
    state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.DOCUMENT_SUBMISSION) == ST.WAITING_FOR_INPUT.value  # UC03 now collects
    assert flow.gate.entered("equipment_access") == 0
    state = flow.supply(state)
    assert flow.gate.entered("equipment_access") == flow.gate.entered("orientation_scheduling") == 1
    assert flow.gate.violations == []
    plan_out = flow.gate.interval("plan_tasks")[1]
    assert plan_out <= flow.gate.interval("equipment_access")[0]
    assert plan_out <= flow.gate.interval("orientation_scheduling")[0]


# ============================================================ 11. the whole workflow, regression


def test_the_whole_workflow_runs_in_order_to_completion():
    flow = Flow()
    state = flow.start()
    assert status(state, S.DOCUMENT_COLLECTION) == ST.COMPLETE.value
    assert status(state, S.DOCUMENT_SUBMISSION) == ST.WAITING_FOR_INPUT.value

    state = flow.supply(state)
    steps = _supersteps(flow, state["case_id"])
    sequence = [step[0] if len(step) == 1 else tuple(step) for step in steps]
    expected = ["document_submission", "validate_documents", "evaluate_document_result", "record_verification",
                "dispatch_branches", tuple(BRANCHES), "buddy_assignment", "team_introduction", "uc05_branch_end",
                "merge_outcomes", "evaluate_overall_status", "day_one_readiness", "day_one_signoff_proposal"]
    # Planned once, before the documents (the first invocation); this one re-entered at intake and passed
    # the completed plan_tasks node as a no-op -- UC02 was not called again.
    assert sequence.index("plan_tasks") < sequence.index("document_submission") and flow.plan.calls == 1
    positions = [sequence.index(node) for node in expected]
    assert positions == sorted(positions), sequence
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value
    assert state["joining_date_consistency"]["status"] == "consistent"

    state = flow.approve_signoff(state, actor=HR_2)
    assert status(state, S.JOINING_DAY_WELCOME) == ST.WAITING.value
    state = flow.agent.trigger_joining_day(state["case_id"], as_of=JOINING, actor=SCHEDULER)
    assert state["workflow_status"] == W.COMPLETED.value and state["current_stage"] == S.COMPLETE.value
    assert (flow.plan.calls, flow.uc04.calls, flow.uc05.calls["orientation"]) == (1, 1, 1)
    assert flow.gate.violations == []
