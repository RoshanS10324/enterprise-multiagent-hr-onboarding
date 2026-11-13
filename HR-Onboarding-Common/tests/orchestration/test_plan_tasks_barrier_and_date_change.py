"""plan_tasks as a sequential barrier before equipment_access/orientation_scheduling, and UC02's
real DATE_CHANGE gate (adapters/uc02_tasks.py's `request_uc02_date_change`/`relay_uc02_decision`),
relayed through the orchestrator's generic approval mechanism exactly as UC05's gates are.

Execution order, parallel execution and plan_tasks failure blocking both branches are covered by
test_execution_order.py (entry/exit timing, a 2-party barrier, LangGraph's own supersteps) and by
test_state_driven_workflow.py (`test_plan_tasks_blocked_prevents_both_downstream_branches_from_
starting`, `test_plan_tasks_failing_prevents_either_downstream_branch_from_running`). Reconciling
results produced for an earlier joining date is covered by test_joining_date_reconciliation.py.
This file covers the UC02 date-change gate (scenarios 2, 3, 6, 7, 8, 9) and a regression check
(10) that the rest of the lifecycle is unaffected.

What is real here: UC02's `OnboardingService` (its DATE_CHANGE business logic) -- see _uc02_real.py.
"""

from __future__ import annotations

from datetime import date

import pytest

from _uc02_real import real_uc02_flow as _real_uc02_flow
from _workflow import HR, HR_2, Flow, status
from hr_onboarding_common.orchestration.adapters.uc05_orientation import PrerequisiteNotMetError
from hr_onboarding_common.orchestration.state import Stage, StageStatus, WorkflowStatus

from onboarding_task_management.errors import InvalidStateError

S, ST, W = Stage, StageStatus, WorkflowStatus
ORIGINAL_JOINING_DATE = date(2026, 12, 1)
NEW_JOINING_DATE = date(2026, 12, 15)


def _rewind_to_before_fanout(flow: Flow, state) -> dict:
    """Back equipment_access/orientation_scheduling (and the UC05 steps behind it) out to PENDING,
    as if plan_tasks had just completed and the fan-out had not run yet. `to_branches()` otherwise
    drives plan_tasks and both branches through in the same invocation (the barrier's whole point);
    this isolates the date-change propagation so it can be observed in a state where the branches
    have genuinely not consumed a date yet. A case reaches that state only if the process stops
    between plan_tasks's write-through and the fan-out; the usual order -- the branches already ran
    for the earlier date -- is covered by test_joining_date_reconciliation.py."""
    stored = flow.stored(state["case_id"])
    for stage in (S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING, S.BUDDY_ASSIGNMENT, S.TEAM_INTRODUCTION):
        stored["stage_statuses"][stage.value] = ST.PENDING.value
        stored["stage_outcomes"].pop(stage.value, None)
    for key in ("equipment_access_result", "orientation_schedule_result", "buddy_assignment_result",
                "team_introduction_result"):
        stored[key] = None
    flow.deps.case_states[state["case_id"]] = stored
    return stored


# ============================================================ 2. updated date reaches both branches


def test_updated_joining_date_reaches_both_downstream_branches():
    flow, _service = _real_uc02_flow()
    state = flow.to_branches()
    assert state["joining_date"] == ORIGINAL_JOINING_DATE.isoformat()
    _rewind_to_before_fanout(flow, state)

    state, outcome = flow.agent.request_uc02_date_change(state["case_id"], joining_date=NEW_JOINING_DATE, actor=HR)
    assert state["joining_date"] == ORIGINAL_JOINING_DATE.isoformat()  # not applied until approved
    (approval,) = [p for p in state["pending_approvals"] if p["reference"] == outcome["request_id"]]
    assert approval["agent"] == "onboarding-task-management" and approval["gate"] == "date_change_approval"

    state = flow.agent.decide_approval(state["case_id"], outcome["request_id"], decision="approved", actor=HR_2)
    assert state["joining_date"] == NEW_JOINING_DATE.isoformat()
    assert status(state, S.EQUIPMENT_ACCESS) == ST.COMPLETE.value
    assert status(state, S.ORIENTATION_SCHEDULING) == ST.COMPLETE.value
    # Same canonical date in both branches' recorded inputs (workflow.stage_inputs's shared `base`).
    assert state["stage_outcomes"][S.EQUIPMENT_ACCESS.value]["inputs"]["joining_date"] == NEW_JOINING_DATE.isoformat()
    assert state["stage_outcomes"][S.ORIENTATION_SCHEDULING.value]["inputs"]["joining_date"] == \
        NEW_JOINING_DATE.isoformat()
    assert state["field_provenance"]["joining_date"]["use_case_id"] == "UC02"
    assert state["field_provenance"]["joining_date"]["source"] == "date_change_approval"
    (decided,) = [d for d in state["approval_decisions"] if d["reference"] == outcome["request_id"]]
    assert decided["applied"] is True and decided["decision"] == "approved"


# ============================================================ 3. no-op / invalid change


def test_a_change_to_the_same_date_is_refused_and_changes_nothing():
    flow, _service = _real_uc02_flow()
    state = flow.to_branches()
    with pytest.raises(InvalidStateError):
        flow.agent.request_uc02_date_change(state["case_id"], joining_date=ORIGINAL_JOINING_DATE, actor=HR)
    audit = flow.ctx.audit.find("orchestrator.uc02_date_change_refused")
    assert audit and audit[-1]["error_type"] == "InvalidStateError"
    after = flow.stored(state["case_id"])
    assert after["joining_date"] == ORIGINAL_JOINING_DATE.isoformat()
    assert after["pending_approvals"] == state["pending_approvals"]  # nothing opened


# ============================================================ 6. no onboarding record: never falls back


def test_a_date_change_is_refused_without_a_plan_tasks_onboarding_record():
    flow = Flow()  # the default PlanDouble: plan_tasks completes without ever touching UC02 for real
    state = flow.to_branches()
    assert status(state, S.PLAN_TASKS) == ST.COMPLETE.value and state["task_plan"]["onboarding_id"] == "ONB-SYN-001"
    # No real sub-agent behind it: requesting a change must refuse, never silently reuse the stale
    # UC01 date or fabricate an onboarding_id it does not actually hold with UC02.
    with pytest.raises(PrerequisiteNotMetError):
        flow.agent.request_uc02_date_change(state["case_id"], joining_date=NEW_JOINING_DATE, actor=HR)

    flow2, _service = _real_uc02_flow()
    flow2.plan.status_override = ST.BLOCKED  # UC02 could not plan: no onboarding record exists
    early = flow2.start()
    assert status(early, S.PLAN_TASKS) == ST.BLOCKED.value
    with pytest.raises(PrerequisiteNotMetError):
        flow2.agent.request_uc02_date_change(early["case_id"], joining_date=NEW_JOINING_DATE, actor=HR)


# ============================================================ 7. pending approval is visible; rejection


def test_pending_date_change_is_visible_and_a_rejected_one_changes_nothing():
    flow, _service = _real_uc02_flow()
    state = flow.to_branches()
    state, outcome = flow.agent.request_uc02_date_change(state["case_id"], joining_date=NEW_JOINING_DATE, actor=HR)
    actions = [a for a in state["pending_actions"] if a["action"] == "decide_approval"]
    assert any(a["agent"] == "onboarding-task-management" for a in actions)

    with pytest.raises(KeyError):  # an approval that does not exist (and never belonged to UC02)
        flow.agent.decide_approval(state["case_id"], "apr-does-not-exist", decision="approved", actor=HR_2)

    state = flow.agent.decide_approval(state["case_id"], outcome["request_id"], decision="rejected", actor=HR_2)
    assert state["joining_date"] == ORIGINAL_JOINING_DATE.isoformat()
    assert not [p for p in state["pending_approvals"] if p["reference"] == outcome["request_id"]]
    (decided,) = [d for d in state["approval_decisions"] if d["reference"] == outcome["request_id"]]
    assert decided["applied"] is False and decided["decision"] == "rejected"
    # Deciding the same (now-settled) request again reaches relay_uc02_decision's own no-op (routed
    # by approval_decisions, not just pending_approvals) rather than falling through to UC05's relay.
    again = flow.agent.decide_approval(state["case_id"], outcome["request_id"], decision="approved", actor=HR_2)
    assert again["joining_date"] == ORIGINAL_JOINING_DATE.isoformat()
    assert [d for d in again["approval_decisions"] if d["reference"] == outcome["request_id"]] == [decided]


# ============================================================ 8. parallel merge preserves the date


def test_the_updated_date_survives_the_parallel_branches_merging():
    flow, _service = _real_uc02_flow()
    state = flow.to_branches()
    _rewind_to_before_fanout(flow, state)
    state, outcome = flow.agent.request_uc02_date_change(state["case_id"], joining_date=NEW_JOINING_DATE, actor=HR)
    state = flow.agent.decide_approval(state["case_id"], outcome["request_id"], decision="approved", actor=HR_2)
    # joining_date is not owned by either branch (state.BRANCH_RESULT_KEYS), so merge_outcomes's
    # fold (workflow.fold_branch_outcome) never touches it; both branches completed without
    # overwriting each other's results or the date written outside any branch delta.
    assert state["joining_date"] == NEW_JOINING_DATE.isoformat()
    assert state["equipment_access_result"]["overall_status"] == "READY"
    assert state["orientation_schedule_result"]["session_id"] == "SES-SYN-1"
    assert state["branch_outcomes"] == {}
    assert state["workflow_status"] == W.WAITING_FOR_APPROVAL.value  # day_one_readiness proposal next
    stored = flow.stored(state["case_id"])
    assert stored["joining_date"] == state["joining_date"]


# ============================================================ 9. retry/resume: latest date, no duplicate effects


def test_resuming_after_a_date_change_uses_the_latest_date_and_never_reruns_plan_tasks():
    flow, _service = _real_uc02_flow()
    state = flow.to_branches()  # equipment_access/orientation_scheduling already ran once, on the original date
    _rewind_to_before_fanout(flow, state)
    plan_calls, uc04_calls, uc05_calls = flow.plan.calls, flow.uc04.calls, flow.uc05.calls["orientation"]
    assert plan_calls == 1

    state, outcome = flow.agent.request_uc02_date_change(state["case_id"], joining_date=NEW_JOINING_DATE, actor=HR)
    state = flow.agent.decide_approval(state["case_id"], outcome["request_id"], decision="approved", actor=HR_2)
    assert flow.plan.calls == plan_calls  # plan_tasks was COMPLETE already; never re-executed
    # Each ran exactly once more (this rewound re-dispatch), with the new date.
    assert flow.uc04.calls == uc04_calls + 1 and flow.uc05.calls["orientation"] == uc05_calls + 1

    resumed = flow.agent.resume_case(state["case_id"], actor=HR)
    assert flow.plan.calls == plan_calls and flow.uc04.calls == uc04_calls + 1 \
        and flow.uc05.calls["orientation"] == uc05_calls + 1  # nothing re-ran
    assert resumed["joining_date"] == NEW_JOINING_DATE.isoformat()


# ============================================================ 10. regression: the rest of the lifecycle


def test_the_rest_of_the_lifecycle_is_unaffected_by_a_mid_flow_date_change():
    flow, _service = _real_uc02_flow()
    state = flow.to_branches()
    _rewind_to_before_fanout(flow, state)
    state, outcome = flow.agent.request_uc02_date_change(state["case_id"], joining_date=NEW_JOINING_DATE, actor=HR)
    state = flow.agent.decide_approval(state["case_id"], outcome["request_id"], decision="approved", actor=HR_2)

    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value
    state = flow.approve_signoff(state)
    assert status(state, S.JOINING_DAY_WELCOME) == ST.WAITING.value
    state = flow.agent.trigger_joining_day(state["case_id"], as_of=NEW_JOINING_DATE, actor=HR)
    assert state["workflow_status"] == W.COMPLETED.value and state["overall_status"] == "complete"
    assert state["current_stage"] == S.COMPLETE.value
