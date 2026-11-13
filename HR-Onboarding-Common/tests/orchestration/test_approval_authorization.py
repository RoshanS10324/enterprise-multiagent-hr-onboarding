"""Gap 4 -- an approval is consumed only by an authenticated, authorized, valid decision. Real
orchestrator + real UC05 worker (UC01-UC04 doubles). Every refused attempt must leave the approval
PENDING and usable by the legitimate approver."""

from __future__ import annotations

import threading

import pytest
from _lifecycle import (
    BUDDY,
    BUDDY_CANDIDATE,
    CLIENT,
    HR,
    HR_2,
    OTHER_TENANT_HR,
    TEAM_LEAD,
    World,
    start_uc05_worker,
    status,
)

from hr_onboarding_common.orchestration.adapters.uc05_orientation import Uc05RelayError
from hr_onboarding_common.orchestration.case_store import SqliteCaseStateStore
from hr_onboarding_common.orchestration.state import Stage, StageStatus

S = Stage


@pytest.fixture(scope="module")
def uc05():
    worker = start_uc05_worker()
    yield worker
    worker.stop()


@pytest.fixture
def world(uc05):
    w = World(uc05)
    w.hook("reset")
    w.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    return w


def _item(world, approval_id):
    (item,) = [a for a in world.uc05_snapshot()["approvals"] if a["approval_id"] == approval_id]
    return item


def _refused(world, state, stage, actor, **fields):
    with pytest.raises(Uc05RelayError) as err:
        world.decide(state, stage, actor=actor, **fields)
    return err.value.code


def test_the_authorized_approver_can_approve(world):
    state = world.to_buddy_pending()
    approval = world.pending(state, S.BUDDY_ASSIGNMENT)
    state = world.decide(state, S.BUDDY_ASSIGNMENT, actor=HR_2)
    assert _item(world, approval)["status"] == "approved"
    assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.COMPLETE.value


def test_the_wrong_buddy_cannot_approve_a_buddy_introduction_and_the_right_one_still_can(world):
    state = world.decide(world.to_buddy_pending(), S.BUDDY_ASSIGNMENT)
    state, _ = world.agent.request_uc05_operation(state["case_id"], operation="schedule_buddy_intro",
                                                  params={"start": "2026-12-02T14:00:00+04:00"}, actor=HR)
    approval = world.pending(state, S.BUDDY_INTRODUCTION)
    impostor = {"on_behalf_of": "EMP-TEST-999", "roles": ["buddy"], "client_id": CLIENT}
    assert _refused(world, state, S.BUDDY_INTRODUCTION, impostor) == "POLICY_DENIED"
    assert _refused(world, state, S.BUDDY_INTRODUCTION, HR) == "POLICY_DENIED"  # hr_admin is not the buddy
    assert _item(world, approval)["status"] == "pending"
    state = world.decide(world.agent.get_case(state["case_id"], client_id=CLIENT), S.BUDDY_INTRODUCTION, actor=BUDDY)
    assert status(state, S.BUDDY_INTRODUCTION) == StageStatus.COMPLETE.value


def test_an_employee_role_cannot_approve_and_a_malformed_decision_changes_nothing(world):
    state = world.start()
    approval = world.pending(state, S.ORIENTATION_SCHEDULING)
    employee = {"on_behalf_of": "user:someone", "roles": ["employee"], "client_id": CLIENT}
    assert _refused(world, state, S.ORIENTATION_SCHEDULING, employee) == "POLICY_DENIED"
    assert _refused(world, state, S.ORIENTATION_SCHEDULING, HR, decision="maybe") == "INVALID_INPUT"
    with pytest.raises(Uc05RelayError) as err:  # a rejection needs a reason_code
        world.agent.decide_approval(state["case_id"], approval, decision="rejected", actor=HR)
    assert err.value.code == "INVALID_INPUT"
    assert _item(world, approval)["status"] == "pending" and world.hook("counts")["calendar_invited"] == 0


def test_a_cross_tenant_actor_cannot_decide(world):
    state = world.start()
    approval = world.pending(state, S.ORIENTATION_SCHEDULING)
    with pytest.raises(KeyError):
        world.agent.decide_approval(state["case_id"], approval, decision="approved", actor=OTHER_TENANT_HR)
    assert _item(world, approval)["status"] == "pending"


def test_an_approval_cannot_be_used_for_another_activity(world):
    """The case records which activity (proposal type + record) each approval decides; if the two
    disagree -- a stale or tampered mapping -- UC05 is not asked to decide anything."""
    state = world.to_team_intro_pending()
    approval = world.pending(state, S.TEAM_INTRODUCTION)
    stored = world.deps.case_states[state["case_id"]]
    (entry,) = [p for p in stored["pending_approvals"] if p["reference"] == approval]
    entry["record_id"] = state["orientation_schedule_result"]["session_id"]  # claims to decide the induction
    with pytest.raises(Uc05RelayError) as err:
        world.agent.decide_approval(state["case_id"], approval, decision="approved", actor=TEAM_LEAD)
    assert err.value.code == "CONFLICT"
    assert _item(world, approval)["status"] == "pending"


def test_a_duplicate_decision_is_refused_and_changes_nothing(world):
    state = world.start()
    approval = world.pending(state, S.ORIENTATION_SCHEDULING)
    state = world.agent.decide_approval(state["case_id"], approval, decision="approved", actor=HR)
    invited = world.hook("counts")["calendar_invited"]
    with pytest.raises(Uc05RelayError) as err:
        world.agent.decide_approval(state["case_id"], approval, decision="rejected", actor=HR_2,
                                    reason_code="timing")
    assert err.value.code == "CONFLICT"
    assert _item(world, approval)["status"] == "approved" and world.hook("counts")["calendar_invited"] == invited
    state = world.agent.get_case(state["case_id"], client_id=CLIENT)
    assert [d["reference"] for d in state["approval_decisions"]].count(approval) == 1


def test_concurrent_decisions_on_one_approval_resolve_to_exactly_one(world):
    state = world.start()
    approval = world.pending(state, S.ORIENTATION_SCHEDULING)
    outcomes, barrier = [], threading.Barrier(4)

    def decide(actor, decision):
        barrier.wait()
        try:
            world.agent.decide_approval(state["case_id"], approval, decision=decision, actor=actor,
                                        reason_code="timing" if decision == "rejected" else None)
            outcomes.append(("ok", decision))
        except Uc05RelayError as e:
            outcomes.append((e.code, decision))

    threads = [threading.Thread(target=decide, args=args) for args in
               ((HR, "approved"), (HR_2, "rejected"), (HR, "approved"), (HR_2, "rejected"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [d for result, d in outcomes if result == "ok"]
    assert len(winners) == 1 and sorted(r for r, _ in outcomes) == ["CONFLICT", "CONFLICT", "CONFLICT", "ok"]
    session = world.uc05_snapshot()["sessions"][0]
    assert session["status"] == ("confirmed" if winners[0] == "approved" else "cancelled")
    assert _item(world, approval)["status"] == winners[0]


def test_an_approval_whose_booking_expired_is_refused_without_being_consumed(world):
    state = world.start()
    approval = world.pending(state, S.ORIENTATION_SCHEDULING)
    world.hook("sweep_timeouts", as_of="2027-01-31T00:00:00+00:00")  # UC05's sweep releases the stale hold
    assert world.uc05_snapshot()["sessions"][0]["status"] == "cancelled"
    with pytest.raises(Uc05RelayError) as err:
        world.agent.decide_approval(state["case_id"], approval, decision="approved", actor=HR)
    assert err.value.code == "CONFLICT" and "no longer pending" in err.value.detail
    assert _item(world, approval)["status"] == "pending"
    state = world.agent.resume_case(state["case_id"], actor=HR)
    assert state["stage_errors"][S.ORIENTATION_SCHEDULING.value]["code"] == "INDUCTION_CANCELLED"


def test_approval_state_survives_a_restart_with_durable_persistence(tmp_path):
    uc05_dir = tmp_path / "uc05"
    worker = start_uc05_worker(UC05_WORKER_DATA_DIR=str(uc05_dir))
    try:
        world = World(worker, case_states=SqliteCaseStateStore(tmp_path / "cases.db"))
        world.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
        state = world.to_buddy_pending()
        approval = world.pending(state, S.BUDDY_ASSIGNMENT)
    finally:
        worker.stop()
    worker = start_uc05_worker(UC05_WORKER_DATA_DIR=str(uc05_dir))  # both processes restart
    try:
        restarted = World(worker, case_states=SqliteCaseStateStore(tmp_path / "cases.db"))
        assert _item(restarted, approval)["status"] == "pending"  # UC05's persistent HITL inbox
        wrong = {"on_behalf_of": "user:x", "roles": ["employee"], "client_id": CLIENT}
        with pytest.raises(Uc05RelayError):
            restarted.decide(restarted.agent.get_case(state["case_id"], client_id=CLIENT), S.BUDDY_ASSIGNMENT,
                             actor=wrong)
        assert _item(restarted, approval)["status"] == "pending"
        state = restarted.decide(restarted.agent.get_case(state["case_id"], client_id=CLIENT), S.BUDDY_ASSIGNMENT)
        assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.COMPLETE.value
        assert _item(restarted, approval)["status"] == "approved" and restarted.uc05_snapshot()["buddy_id"]
    finally:
        worker.stop()
