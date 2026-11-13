"""Gap 5 -- recovering a welcome whose delivery did not clearly succeed, through UC05's simulated
notifier (no real provider). Real orchestrator + real UC05 worker (UC01-UC04 doubles)."""

from __future__ import annotations

import pytest
from _lifecycle import (
    BUDDY_CANDIDATE,
    CLIENT,
    HR,
    TEAM_LEAD,
    World,
    start_uc05_worker,
    status,
)

from hr_onboarding_common.orchestration.adapters._shared import (
    OrchestratorAuthorizationError,
)
from hr_onboarding_common.orchestration.adapters.uc05_orientation import (
    PrerequisiteNotMetError,
    Uc05RelayError,
)
from hr_onboarding_common.orchestration.state import Stage, StageStatus

W = Stage.JOINING_DAY_WELCOME


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


def _op(world, state, operation, actor=HR, **params):
    return world.agent.request_uc05_operation(state["case_id"], operation=operation, params=params, actor=actor)


def _welcomes(world) -> int:
    return world.hook("counts")["messages_by_kind"].get("welcome", 0)


def _operation(world, state):
    key = state["welcome_message_result"]["operation_key"]
    (op,) = [o for o in world.uc05_snapshot()["operations"] if o["key"] == key]
    return op


def _uncertain_before_delivery(world):
    """The provider is down (nothing delivered) and cannot deduplicate: UC05 cannot know."""
    state = world.to_welcome_pending()
    world.hook("set", target="notifier", attrs={"idempotent_delivery": False, "fail_next": 1})
    state = world.decide(state, W)
    assert status(state, W) == StageStatus.UNCERTAIN.value and state["welcome_message_result"]["delivery"] == "uncertain"
    return state


def test_a_confirmed_delivery_is_never_repeated(world):
    state = world.decide(world.to_welcome_pending(), W)
    assert state["welcome_message_result"]["delivery"] == "delivered" and _welcomes(world) == 1
    with pytest.raises(PrerequisiteNotMetError):
        _op(world, state, "retry_welcome_delivery", reason="resend please")
    with pytest.raises(PrerequisiteNotMetError):
        _op(world, state, "resolve_welcome_delivery", resolution="delivered", reason="seen")
    assert _welcomes(world) == 1


def test_a_permanent_refusal_before_delivery_needs_an_authorized_retry(world):
    state = world.to_welcome_pending()
    world.hook("reject_recipient", recipient_id="EMP-TEST-001")  # e.g. a bad address
    state = world.decide(state, W)
    assert state["stage_errors"][W.value]["code"] == "WELCOME_DELIVERY_REJECTED"
    assert state["welcome_message_result"]["delivery"] == "failed_before_delivery" and _welcomes(world) == 0
    state = world.agent.resume_case(state["case_id"], actor=HR)  # resuming does not resend
    assert _welcomes(world) == 0

    with pytest.raises(PrerequisiteNotMetError):  # a reason is required
        _op(world, state, "retry_welcome_delivery")
    world.hook("clear_rejections")  # the address was fixed
    state, outcome = _op(world, state, "retry_welcome_delivery", reason="recipient address corrected in HRMS")
    assert outcome["outcome"] == "sent" and _welcomes(world) == 1
    assert status(state, W) == StageStatus.COMPLETE.value and state["overall_status"] == "complete"
    (review,) = state["welcome_message_result"]["delivery_reviews"]
    assert review["action"] == "retry" and review["by"] == HR["on_behalf_of"] and review["outcome"] == "sent"


def test_an_uncertain_delivery_is_not_resent_by_resumes_or_retries_of_the_request(world):
    state = _uncertain_before_delivery(world)
    for _ in range(2):
        state = world.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, W) == StageStatus.UNCERTAIN.value and _welcomes(world) == 0
    with pytest.raises(PrerequisiteNotMetError, match="resolve it first"):
        _op(world, state, "retry_welcome_delivery", reason="try again")
    assert _welcomes(world) == 0


def test_resolution_requires_an_authorized_operator(world):
    state = _uncertain_before_delivery(world)
    officer = {"on_behalf_of": "user:officer", "roles": ["hr_onboarding_officer"], "client_id": CLIENT}
    with pytest.raises(Uc05RelayError) as err:  # passes the orchestrator's role, refused by UC05 (hr_admin)
        _op(world, state, "resolve_welcome_delivery", actor=officer, resolution="not_delivered", reason="checked")
    assert err.value.code == "POLICY_DENIED"
    with pytest.raises(OrchestratorAuthorizationError):
        _op(world, state, "resolve_welcome_delivery", actor=TEAM_LEAD, resolution="not_delivered", reason="x")
    with pytest.raises(PrerequisiteNotMetError):  # the evidence must be stated
        _op(world, state, "resolve_welcome_delivery", resolution="not_delivered")
    assert _operation(world, state)["status"] == "uncertain"


def test_an_authorized_resolution_is_persisted_audited_and_a_retry_is_then_allowed(world):
    state = _uncertain_before_delivery(world)
    state, outcome = _op(world, state, "resolve_welcome_delivery", resolution="not_delivered",
                         reason="provider console shows no message for this recipient")
    assert outcome["outcome"] == "released"
    assert state["welcome_message_result"]["delivery"] == "not_delivered"
    assert state["stage_errors"][W.value]["code"] == "WELCOME_NOT_DELIVERED"
    op = _operation(world, state)
    assert op["resolved_by"] == HR["on_behalf_of"] and op["resolution_reason"].startswith("provider console")
    assert world.ctx.audit.find("orchestrator.uc05_operation")[-1]["reason"].startswith("provider console")

    state, outcome = _op(world, state, "retry_welcome_delivery", reason="operator confirmed nothing was delivered")
    assert outcome["outcome"] == "sent" and _welcomes(world) == 1
    assert status(state, W) == StageStatus.COMPLETE.value
    with pytest.raises(PrerequisiteNotMetError):  # a repeated request creates nothing
        _op(world, state, "retry_welcome_delivery", reason="again")
    assert _welcomes(world) == 1


def test_operator_confirmation_completes_without_a_fabricated_receipt(world):
    state = _uncertain_before_delivery(world)
    state, _ = _op(world, state, "resolve_welcome_delivery", resolution="delivered",
                   reason="recipient confirmed by phone", provider_ref="ticket-42")
    assert status(state, W) == StageStatus.COMPLETE.value
    assert state["welcome_message_result"]["delivery"] == "confirmed_by_operator"  # not "delivered"
    assert state["welcome_message_result"]["delivery_resolution"]["by"] == HR["on_behalf_of"]
    assert _welcomes(world) == 0  # the simulated provider never accepted it, and nothing was sent


def test_keeping_it_unresolved_records_the_review_and_leaves_it_recoverable(world):
    state = _uncertain_before_delivery(world)
    state, outcome = _op(world, state, "resolve_welcome_delivery", resolution="keep_unresolved",
                         reason="recipient not reachable yet")
    assert outcome["outcome"] == "kept_unresolved"
    assert status(state, W) == StageStatus.UNCERTAIN.value and _operation(world, state)["status"] == "uncertain"
    assert state["welcome_message_result"]["delivery_reviews"][-1]["action"] == "keep_unresolved"
    assert any(b["code"] == "WELCOME_DELIVERY_UNCERTAIN" for b in state["blockers"])  # still visible
    state, _ = _op(world, state, "resolve_welcome_delivery", resolution="delivered", reason="recipient confirmed")
    assert status(state, W) == StageStatus.COMPLETE.value
