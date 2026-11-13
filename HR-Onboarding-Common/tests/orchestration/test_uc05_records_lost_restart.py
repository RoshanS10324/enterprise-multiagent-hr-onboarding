"""After UC05 lost a case's records (its worker restarted without persistence), HR restarts UC05's part of the case.

The orchestrator never replays the lost records: it reports UNCERTAIN_OUTCOME. Recovery is HR's explicit operation
`restart_after_records_lost`, allowed only when a UC05 stage is uncertain, UC05 itself confirms it holds nothing for the
employee, and HR states what was reconciled. The orientation branch then starts again: UC05 re-creates the case and the
induction must be approved again -- nothing from the lost records counts as approved. The UC05 worker is real.
"""

from __future__ import annotations

import pytest
from _lifecycle import BUDDY_CANDIDATE, CLIENT_CONFIG, HR, World, start_uc05_worker, status

from hr_onboarding_common.orchestration.adapters.uc05_orientation import PrerequisiteNotMetError
from hr_onboarding_common.orchestration.state import Stage, StageStatus

S = Stage
REASON = "Simulated calendar and notifier in this environment: nothing UC05 booked or sent is live elsewhere."


@pytest.fixture
def world():
    worker = start_uc05_worker()
    w = World(worker)
    w.hook("reset")
    w.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    w.hook("add_client_config", config=CLIENT_CONFIG)
    yield w
    worker.stop()


def _lose_uc05(world: World) -> None:
    """UC05's worker restarts without persistence: every record it held is gone."""
    world.hook("reset")
    world.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    world.hook("add_client_config", config=CLIENT_CONFIG)


def _restart(world: World, state, reason=REASON):
    return world.agent.request_uc05_operation(state["case_id"], operation="restart_after_records_lost",
                                              params={"reason": reason}, actor=HR)


def test_hr_restarts_uc05_after_lost_records_and_the_induction_is_approved_again(world):
    state = world.to_buddy_pending()  # induction approved in UC05, buddy proposed
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.COMPLETE.value

    _lose_uc05(world)
    state = world.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.UNCERTAIN.value
    (b,) = [b for b in state["blockers"] if b["code"] == "UNCERTAIN_OUTCOME"]
    assert b["resolution"] == "restart_uc05_after_records_lost" and b["required_from"] == "HR admin"

    with pytest.raises(PrerequisiteNotMetError, match="reason"):
        _restart(world, state, reason="")

    state, outcome = _restart(world, state)
    assert outcome["outcome"] == "restarted"
    # UC05 re-created the case from the form; the induction waits for HR again (the old approval is not replayed).
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.AWAITING_APPROVAL.value, state["blockers"]
    assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.PENDING.value
    assert not [b for b in state["blockers"] if b["code"] == "UNCERTAIN_OUTCOME"]
    assert state["uc05_restarts"][-1]["reason"] == REASON and state["uc05_restarts"][-1]["by"] == HR["on_behalf_of"]

    state = world.decide(state, S.ORIENTATION_SCHEDULING)  # HR approves the induction again
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.COMPLETE.value
    assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.AWAITING_APPROVAL.value


def test_nothing_is_restarted_while_uc05_still_holds_the_records_or_nothing_is_uncertain(world):
    state = world.to_buddy_pending()
    with pytest.raises(PrerequisiteNotMetError, match="no UC05 stage"):
        _restart(world, state)  # nothing uncertain

    # Uncertain, but UC05 still has the records (e.g. a transport failure, not a loss): refused, nothing changed.
    state["stage_statuses"][S.BUDDY_ASSIGNMENT.value] = StageStatus.UNCERTAIN.value
    world.deps.case_states[state["case_id"]] = state
    with pytest.raises(PrerequisiteNotMetError, match="still holds"):
        _restart(world, state)
    assert world.agent.get_case(state["case_id"], client_id=HR["client_id"])["orientation_schedule_result"]["uc05_case"]
