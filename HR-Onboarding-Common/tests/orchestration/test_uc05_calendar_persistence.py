"""UC05's simulated calendar persists with UC05's records (UC05_WORKER_DATA_DIR), and a decision whose follow-up
failed is re-applied by retrying the stage.

Without it, a worker restart kept UC05's sessions (SQLite) but emptied the simulated calendar, so confirming an approved
booking failed with KeyError('sim-evt-...') -- DECISION_NOT_APPLIED. Pinned with the real UC05 worker:
  * a booking made before a restart is confirmed after it (the hold survived);
  * a calendar that lost its holds (state from before persistence) gets them back from UC05's own session records at
    start -- nothing new is booked, nobody is invited by the restore;
  * retrying a DECISION_NOT_APPLIED stage re-runs the recorded decision's follow-up (nothing is decided again).
"""

from __future__ import annotations

import sqlite3

from _lifecycle import (BUDDY_CANDIDATE, CLIENT_CONFIG, HR, TEAM_LEAD, World, start_uc05_worker, status)

from hr_onboarding_common.orchestration.case_store import SqliteCaseStateStore
from hr_onboarding_common.orchestration.state import Stage, StageStatus

S = Stage


def _world(worker, orch_dir):
    from langgraph.checkpoint.sqlite import SqliteSaver

    saver = SqliteSaver(sqlite3.connect(str(orch_dir / "checkpoints.db"), check_same_thread=False))
    saver.setup()
    return World(worker, case_states=SqliteCaseStateStore(orch_dir / "case_states.db"), checkpointer=saver,
                 persist_dir=orch_dir)


def _to_team_intro_pending(tmp_path):
    uc05_dir, orch_dir = tmp_path / "uc05", tmp_path / "orchestrator"
    orch_dir.mkdir()
    worker = start_uc05_worker(UC05_WORKER_DATA_DIR=str(uc05_dir))
    try:
        world = _world(worker, orch_dir)
        world.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
        world.hook("add_client_config", config=CLIENT_CONFIG)
        state = world.to_team_intro_pending()
        assert status(state, S.TEAM_INTRODUCTION) == StageStatus.AWAITING_APPROVAL.value
        return state["case_id"], uc05_dir, orch_dir
    finally:
        worker.stop()


def test_a_booking_made_before_a_restart_is_confirmed_after_it(tmp_path):
    case_id, uc05_dir, orch_dir = _to_team_intro_pending(tmp_path)
    worker = start_uc05_worker(UC05_WORKER_DATA_DIR=str(uc05_dir))  # the backend restarts
    try:
        world = _world(worker, orch_dir)
        state = world.agent.get_case(case_id, client_id=HR["client_id"])
        state = world.decide(state, S.TEAM_INTRODUCTION, actor=TEAM_LEAD)
        assert status(state, S.TEAM_INTRODUCTION) == StageStatus.COMPLETE.value, state["blockers"]
    finally:
        worker.stop()


def test_lost_simulated_holds_are_restored_from_uc05_records_without_booking_anything(tmp_path):
    case_id, uc05_dir, orch_dir = _to_team_intro_pending(tmp_path)
    (uc05_dir / "simulated_calendar.json").unlink()  # the state before this fix: UC05's sessions, no calendar

    worker = start_uc05_worker(UC05_WORKER_DATA_DIR=str(uc05_dir))
    try:
        world = _world(worker, orch_dir)
        before = world.hook("counts")
        assert before["calendar_live"] >= 1 and before["calendar_invited"] == 0  # restored holds, nobody invited
        state = world.agent.get_case(case_id, client_id=HR["client_id"])
        state = world.decide(state, S.TEAM_INTRODUCTION, actor=TEAM_LEAD)
        # The team lead's decision confirmed the restored hold: nothing new was booked.
        assert status(state, S.TEAM_INTRODUCTION) == StageStatus.COMPLETE.value, state["blockers"]
        assert world.hook("counts")["calendar_live"] == before["calendar_live"]
    finally:
        worker.stop()


def test_retrying_a_stage_whose_decision_did_not_apply_reapplies_that_decision(tmp_path):
    case_id, uc05_dir, orch_dir = _to_team_intro_pending(tmp_path)
    worker = start_uc05_worker(UC05_WORKER_DATA_DIR=str(uc05_dir))
    try:
        world = _world(worker, orch_dir)
        world.hook("set", target="calendar", attrs={"fail_next_confirms": 1})  # the follow-up fails once
        state = world.agent.get_case(case_id, client_id=HR["client_id"])
        state = world.decide(state, S.TEAM_INTRODUCTION, actor=TEAM_LEAD)
        assert state["stage_errors"][S.TEAM_INTRODUCTION.value]["code"] == "DECISION_NOT_APPLIED"
        (b,) = [b for b in state["blockers"] if b["code"] == "DECISION_NOT_APPLIED"]
        assert b["resolution"] == "retry_apply_decision" and b["approval_id"]

        b.pop("approval_id")  # a blocker recorded before it carried the id: found from the stage error instead
        world.deps.case_states[case_id] = state
        state = world.agent.retry_stage(case_id, S.TEAM_INTRODUCTION.value, actor=HR)
        assert status(state, S.TEAM_INTRODUCTION) == StageStatus.COMPLETE.value, state["blockers"]
        assert not [x for x in state["blockers"] if x["code"] == "DECISION_NOT_APPLIED"]
    finally:
        worker.stop()
