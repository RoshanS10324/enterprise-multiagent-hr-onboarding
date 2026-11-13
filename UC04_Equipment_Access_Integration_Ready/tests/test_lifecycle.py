"""State machine (BR-07; TC-30)."""

import pytest

from helpers import ALL, ready_payload
from sub_agent.lifecycle import ALLOWED, InvalidTransition, path, transition
from sub_agent.states import ItemKind, ItemState as S


def test_every_state_has_a_transition_entry_and_cancelled_is_terminal():
    assert set(ALLOWED) == set(S)
    assert ALLOWED[S.CANCELLED] == frozenset()


@pytest.mark.parametrize("prev,new", [
    (S.DRAFT_PREPARED, S.ITEM_COMPLETED), (S.ITEM_COMPLETED, S.IT_IN_PROGRESS), (S.CANCELLED, S.DRAFT_PREPARED),
    (S.ELIGIBLE_FOR_SIMULATED_SUBMISSION, S.ITEM_COMPLETED), (S.APPROVAL_PENDING, S.MOCK_TICKET_CREATED),
    (S.REJECTED_CORRECTION_REQUIRED, S.ELIGIBLE_FOR_SIMULATED_SUBMISSION), (S.EQUIPMENT_DELIVERED, S.PROVISIONING_CONFIRMED),
    (S.IT_IN_PROGRESS, S.APPROVAL_PENDING), (S.MOCK_TICKET_CREATED, S.APPROVAL_RECEIVED),
])
def test_tc30_illegal_transitions_are_rejected(prev, new):
    with pytest.raises(InvalidTransition):
        transition(prev, new)


@pytest.mark.parametrize("prev,new", [
    (S.DRAFT_PREPARED, S.APPROVAL_PENDING), (S.APPROVAL_RECEIVED, S.ELIGIBLE_FOR_SIMULATED_SUBMISSION),
    (S.ELIGIBLE_FOR_SIMULATED_SUBMISSION, S.MOCK_TICKET_CREATED), (S.IT_IN_PROGRESS, S.PROVISIONING_CONFIRMED),
    (S.ITEM_COMPLETED, S.CANCELLED), (S.BLOCKED, S.APPROVAL_PENDING), (S.ELIGIBLE_FOR_SIMULATED_SUBMISSION, S.APPROVAL_PENDING),
])
def test_legal_transitions_pass(prev, new):
    transition(prev, new)


def test_access_path_shows_every_stage():
    steps = path(None, S.ITEM_COMPLETED, kind=ItemKind.ACCESS)
    assert steps == [S.DRAFT_PREPARED, S.APPROVAL_PENDING, S.APPROVAL_RECEIVED, S.ELIGIBLE_FOR_SIMULATED_SUBMISSION,
                     S.MOCK_TICKET_CREATED, S.IT_IN_PROGRESS, S.PROVISIONING_CONFIRMED, S.ITEM_COMPLETED]


def test_equipment_path_includes_collection_only_when_required():
    with_c = path(None, S.ITEM_COMPLETED, kind=ItemKind.EQUIPMENT, collection_required=True)
    without = path(None, S.ITEM_COMPLETED, kind=ItemKind.EQUIPMENT, collection_required=False)
    assert S.EQUIPMENT_COLLECTED in with_c and S.EQUIPMENT_COLLECTED not in without
    assert S.EQUIPMENT_DELIVERED in without


def test_exception_and_client_site_items_pass_through_their_own_states():
    steps = path(None, S.ITEM_COMPLETED, kind=ItemKind.ACCESS, exception_item=True, clearance_required=True)
    assert S.EXCEPTION_RECEIVED in steps and S.APPROVAL_RECEIVED not in steps
    assert S.CLIENT_SITE_CLEARANCE_CONFIRMED in steps


def test_backward_path_is_rejected():
    with pytest.raises(InvalidTransition):
        path(S.ITEM_COMPLETED, S.IT_IN_PROGRESS, kind=ItemKind.ACCESS)
    with pytest.raises(InvalidTransition):
        path(S.CANCELLED, S.APPROVAL_PENDING, kind=ItemKind.ACCESS)


def test_ticketed_item_cannot_return_to_approval():
    with pytest.raises(InvalidTransition):
        path(S.IT_IN_PROGRESS, S.APPROVAL_PENDING, kind=ItemKind.ACCESS)


def test_tc30_service_rejects_withdrawn_completion_evidence_and_keeps_stored_state(env):
    first = env.run(ready_payload())
    assert all(i.state == S.ITEM_COMPLETED for i in first.items)
    withdrawn = ready_payload(provisioning_status=[], as_of_date="2026-10-21")
    again = env.run(withdrawn)
    codes = {b.code for b in again.blockers}
    assert "INVALID_STATE_TRANSITION" in codes
    assert again.overall_status.value == "NEEDS_HUMAN_REVIEW"  # never READY on withdrawn evidence
    assert any(e["event_type"] == "transition.rejected" for e in again.audit_events)
