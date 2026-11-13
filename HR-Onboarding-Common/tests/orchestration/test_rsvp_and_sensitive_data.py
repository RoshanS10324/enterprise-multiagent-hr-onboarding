"""Gap 6 -- RSVPs and attendance conflicts as independent UC05 events forwarded by the orchestrator
(not a graph stage); and section 9 -- what is (not) persisted. Real orchestrator + real UC05 worker
(UC01-UC04 doubles)."""

from __future__ import annotations

import sqlite3

import pytest
from _lifecycle import (
    BUDDY_CANDIDATE,
    CLIENT,
    HR,
    OTHER_TENANT_HR,
    World,
    start_uc05_worker,
    status,
)
from langgraph.checkpoint.sqlite import SqliteSaver

from hr_onboarding_common.orchestration.adapters.uc05_orientation import (
    PrerequisiteNotMetError,
    Uc05RelayError,
)
from hr_onboarding_common.orchestration.case_store import SqliteCaseStateStore
from hr_onboarding_common.orchestration.state import Stage, StageStatus

O = Stage.ORIENTATION_SCHEDULING
NEW_HIRE = {"on_behalf_of": "EMP-TEST-001", "roles": ["employee"], "client_id": CLIENT}
LEAD = {"on_behalf_of": "TL-TEST-001", "roles": ["team_lead"], "client_id": CLIENT}


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


def _rsvp(world, state, actor, response, participant=None, session_id=None, responded_at=None):
    return world.agent.record_participant_response(
        state["case_id"], session_id=session_id or state["orientation_schedule_result"]["session_id"],
        participant_id=participant or actor["on_behalf_of"], response=response, responded_at=responded_at,
        actor=actor)


def _conflicts(state):
    return [p for p in state["pending_approvals"] if p.get("proposal_type") == "attendee_declined"]


def test_an_accepted_rsvp_is_recorded_without_changing_the_stage(world):
    state = world.start()
    state, outcome = _rsvp(world, state, NEW_HIRE, "accepted")
    assert outcome["outcome"] == "attendance_confirmed"  # the induction requires the employee only
    assert state["participant_responses"][-1]["response"] == "accepted"
    assert status(state, O) == StageStatus.AWAITING_APPROVAL.value and _conflicts(state) == []


def test_a_decline_surfaces_one_hr_decision_however_often_it_is_sent(world):
    state = world.start()
    for _ in range(2):  # a re-sent RSVP
        state, outcome = _rsvp(world, state, LEAD, "declined")
    assert outcome["outcome"] == "conflict_awaiting_hr_decision"
    (conflict,) = _conflicts(state)
    assert conflict["stage"] == O.value and conflict["gate"] == "attendance_decision" and conflict["role"] == "hr_admin"
    declined = [a for a in world.uc05_snapshot()["approvals"] if a["proposal_type"] == "attendee_declined"]
    assert len(declined) == 1


def test_hr_keeping_the_meeting_changes_nothing(world):
    state = world.decide(world.start(), O)  # the induction is confirmed
    state, _ = _rsvp(world, state, LEAD, "declined")
    (conflict,) = _conflicts(state)
    state = world.agent.decide_approval(state["case_id"], conflict["reference"], decision="approved", actor=HR)
    assert status(state, O) == StageStatus.COMPLETE.value and _conflicts(state) == []
    assert state["approval_decisions"][-1]["conflict_decision"] == "keep"


def test_hr_cancelling_after_a_decline_reopens_a_completed_stage(world):
    state = world.decide(world.start(), O)
    assert status(state, O) == StageStatus.COMPLETE.value
    state, _ = _rsvp(world, state, LEAD, "declined")
    (conflict,) = _conflicts(state)
    state = world.agent.decide_approval(state["case_id"], conflict["reference"], decision="rejected", actor=HR,
                                        reason_code="timing")
    assert status(state, O) == StageStatus.BLOCKED.value
    assert state["stage_errors"][O.value]["code"] == "INDUCTION_CANCELLED"
    assert any(e.get("code") == "REOPENED_BY_ATTENDANCE_DECISION_CANCEL" for e in state["stage_history"])
    assert state["current_stage"] == O.value  # nothing after it proceeds on a cancelled induction


def test_hr_replanning_after_a_decline_needs_a_fresh_approval(world):
    state = world.decide(world.start(), O)
    state, _ = _rsvp(world, state, LEAD, "declined")
    (conflict,) = _conflicts(state)
    state = world.agent.decide_approval(state["case_id"], conflict["reference"], decision="edited", actor=HR,
                                        reason_code="timing",
                                        edited_payload={"new_start": "2026-12-02T10:00:00+04:00"})
    assert status(state, O) == StageStatus.AWAITING_APPROVAL.value
    assert state["orientation_schedule_result"]["scheduled_start"] == "2026-12-02T10:00:00+04:00"
    state = world.decide(state, O)
    assert status(state, O) == StageStatus.COMPLETE.value


def test_a_replan_to_an_invalid_time_is_refused_and_the_decision_stays_open(world):
    state = world.decide(world.start(), O)
    state, _ = _rsvp(world, state, LEAD, "declined")
    (conflict,) = _conflicts(state)
    with pytest.raises(Uc05RelayError) as err:
        world.agent.decide_approval(state["case_id"], conflict["reference"], decision="edited", actor=HR,
                                    reason_code="timing", edited_payload={"new_start": "2026-12-02T23:00:00+04:00"})
    assert err.value.code == "INVALID_INPUT"
    state = world.agent.get_case(state["case_id"], client_id=CLIENT)
    assert len(_conflicts(state)) == 1 and status(state, O) == StageStatus.COMPLETE.value


def test_unknown_foreign_stale_and_impersonated_rsvps_are_refused(world):
    state = world.start()
    with pytest.raises(Uc05RelayError) as err:
        _rsvp(world, state, NEW_HIRE, "accepted", session_id="session-unknown")
    assert err.value.code == "NOT_FOUND"
    with pytest.raises(KeyError):  # another tenant's actor: the case itself is not found
        _rsvp(world, state, OTHER_TENANT_HR, "accepted", participant="EMP-TEST-001")
    with pytest.raises(Uc05RelayError) as err:  # answering for someone else without hr_admin
        _rsvp(world, state, NEW_HIRE, "declined", participant="TL-TEST-001")
    assert err.value.code == "POLICY_DENIED"
    _rsvp(world, state, LEAD, "accepted", responded_at="2026-11-02T10:00:00+04:00")
    with pytest.raises(Uc05RelayError) as err:  # an older answer arriving late
        _rsvp(world, state, LEAD, "declined", responded_at="2026-11-01T10:00:00+04:00")
    assert err.value.code == "CONFLICT"
    with pytest.raises(PrerequisiteNotMetError):
        _rsvp(world, state, LEAD, "maybe")
    assert _conflicts(world.agent.get_case(state["case_id"], client_id=CLIENT)) == []


# ------------------------------------------------------------------ section 9: what is persisted


def test_intake_only_personal_data_never_reaches_a_checkpoint_or_the_case_store(uc05, tmp_path):
    saver = SqliteSaver(sqlite3.connect(str(tmp_path / "checkpoints.db"), check_same_thread=False))
    saver.setup()
    world = World(uc05, case_states=SqliteCaseStateStore(tmp_path / "cases.db"), checkpointer=saver)
    world.hook("reset")
    state = world.start()
    assert world.uc01.payloads[0]["id_number"] == "ID-T-001"  # UC01 itself still receives it
    assert world.uc01.payloads[0]["form"]["candidate_email"] == "alex.morgan@example.com"
    for db in ("checkpoints.db", "cases.db"):
        data = b"".join(p.read_bytes() for p in tmp_path.glob(f"{db}*"))  # the file and its WAL
        assert b"ID-T-001" not in data and b"alex.morgan@example.com" not in data, db
    assert state["case_id"] and not world.deps.intake_inputs  # dropped once intake completed


def test_a_restart_before_intake_completes_asks_for_the_start_to_be_resubmitted(uc05, tmp_path):
    store = tmp_path / "cases.db"
    world = World(uc05, case_states=SqliteCaseStateStore(store))
    world.hook("reset")
    held = world.start(offer_accepted=False)  # UC01 holds an unverified acceptance
    assert status(held, Stage.INTAKE) == StageStatus.BLOCKED.value

    restarted = World(uc05, case_states=SqliteCaseStateStore(store))  # its memory holds no intake inputs
    state = restarted.agent.resume_case(held["case_id"], actor=HR)
    assert state["stage_errors"][Stage.INTAKE.value]["code"] == "INTAKE_INPUT_UNAVAILABLE"
    assert restarted.uc01.calls == 0  # UC01 is not called without its inputs

    state = restarted.start()  # resubmitted (the acceptance is now verified)
    assert status(state, Stage.INTAKE) == StageStatus.COMPLETE.value and state["case_id"] != held["case_id"]
