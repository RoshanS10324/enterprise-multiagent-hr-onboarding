"""Failure handling without false success states (BR-18, BR-21, BR-22, EC-20).

D1: an idempotent replay must not hide a parent-status change (WITHDRAWN / ON_HOLD).
D2: if an evaluation fails part-way (privacy guard or any unexpected error), the stored state must be exactly what it was before,
    so a retry behaves as a first attempt and nothing is half-applied."""

import pytest

from helpers import ALL, approvals, make, ticket
from sub_agent.guardrails import PrivacyViolation
from sub_agent.states import CaseStatus, ItemState, ParentCaseStatus


def _fail_result_guard(monkeypatch):
    monkeypatch.setattr("sub_agent.service.assert_clean", lambda *a, **k: (_ for _ in ()).throw(PrivacyViolation("forced for the test")))


# ------------------------------------------------------------------------------------------------ D1: replay vs parent status
def test_replay_with_the_same_key_does_not_hide_a_withdrawn_parent(env):
    payload = make(approval_records=approvals(ALL))
    first = env.run(payload, idempotency_key="K1")
    assert first.overall_status == CaseStatus.IN_PROGRESS
    env.cases.set_status("SYN-CASE-001", ParentCaseStatus.WITHDRAWN)
    replay = env.run(payload, idempotency_key="K1")
    assert replay.overall_status == CaseStatus.CANCELLED and all(i.state == ItemState.CANCELLED for i in replay.items)


def test_replay_with_the_same_key_does_not_hide_an_on_hold_parent(env):
    payload = make(approval_records=approvals(ALL))
    env.run(payload, idempotency_key="K1")
    env.cases.set_status("SYN-CASE-001", ParentCaseStatus.ON_HOLD)
    held = env.run(payload, idempotency_key="K1")
    assert held.overall_status == CaseStatus.BLOCKED and {b.code for b in held.blockers} == {"CASE_ON_HOLD"}


def test_replay_follows_the_parent_through_hold_and_resume(env):
    payload = make(approval_records=approvals(ALL))
    assert env.run(payload, idempotency_key="K1").overall_status == CaseStatus.IN_PROGRESS
    env.cases.set_status("SYN-CASE-001", ParentCaseStatus.ON_HOLD)
    assert env.run(payload, idempotency_key="K1").overall_status == CaseStatus.BLOCKED
    env.cases.set_status("SYN-CASE-001", ParentCaseStatus.ACTIVE)
    assert env.run(payload, idempotency_key="K1").overall_status == CaseStatus.IN_PROGRESS


def test_a_replay_is_still_served_from_the_store_when_nothing_changed(env):
    payload = make()
    first = env.run(payload, idempotency_key="K1")
    second = env.run(payload, idempotency_key="K1")
    assert second == first
    assert [e["event_type"] for e in env.service.audit.events].count("replay.served") == 1


def test_the_same_key_with_a_different_payload_is_still_a_conflict_after_a_parent_change(env):
    from sub_agent.store import IdempotencyConflict
    env.run(make(), idempotency_key="K1")
    env.cases.set_status("SYN-CASE-001", ParentCaseStatus.ON_HOLD)
    with pytest.raises(IdempotencyConflict):
        env.run(make(offer_accepted=False), idempotency_key="K1")


# ------------------------------------------------------------------------------------------------ D2: rollback on failure
def test_a_failed_first_evaluation_leaves_no_case_record_behind(env, monkeypatch):
    _fail_result_guard(monkeypatch)
    with pytest.raises(PrivacyViolation):
        env.run(make(), idempotency_key="K1")
    assert env.service.store.get("SYN-CASE-001") is None and env.service.store.case_ids == []
    monkeypatch.undo()
    assert env.run(make(), idempotency_key="K1").overall_status == CaseStatus.AWAITING_APPROVAL


def test_a_failed_evaluation_does_not_advance_stored_item_states(env, monkeypatch):
    env.run(make(approval_records=approvals(["201"])), idempotency_key="K1")           # 201 -> ELIGIBLE
    version = env.service.store.version
    _fail_result_guard(monkeypatch)
    with pytest.raises(PrivacyViolation):
        env.run(make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")]), idempotency_key="K2")
    record = env.service.store.get("SYN-CASE-001")
    assert record.items["SYN-REQ-201"].state == ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION and record.items["SYN-REQ-201"].ticket_id is None
    assert env.service.store.version == version


def test_a_failed_cancellation_does_not_lose_the_cancellation_drafts_on_retry(env, monkeypatch):
    env.submit(make(approval_records=approvals(ALL)), idempotency_key="S1")               # tickets exist on the mock desk
    env.cases.set_status("SYN-CASE-001", ParentCaseStatus.WITHDRAWN)
    _fail_result_guard(monkeypatch)
    with pytest.raises(PrivacyViolation):
        env.run(make(approval_records=approvals(ALL)), idempotency_key="W1")
    assert {i.state for i in env.service.store.get("SYN-CASE-001").items.values()} == {ItemState.MOCK_TICKET_CREATED}
    monkeypatch.undo()
    retry = env.run(make(approval_records=approvals(ALL)), idempotency_key="W1")
    assert retry.overall_status == CaseStatus.CANCELLED and len(retry.cancellation_drafts) == 4


def test_an_unexpected_error_mid_evaluation_also_restores_state(env, monkeypatch):
    env.run(make(approval_records=approvals(["201"])), idempotency_key="K1")

    def explode(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("sub_agent.service.derive_status", explode)                             # fails after the state has been changed
    with pytest.raises(RuntimeError):
        env.run(make(approval_records=approvals(ALL)), idempotency_key="K2")
    states = {k: v.state for k, v in env.service.store.get("SYN-CASE-001").items.items()}
    assert states == {"SYN-REQ-101": ItemState.APPROVAL_PENDING, "SYN-REQ-102": ItemState.APPROVAL_PENDING,
                      "SYN-REQ-201": ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION, "SYN-REQ-202": ItemState.APPROVAL_PENDING}


def test_the_failure_is_audited_and_never_cached_as_a_success(env, monkeypatch):
    _fail_result_guard(monkeypatch)
    with pytest.raises(PrivacyViolation):
        env.run(make(), idempotency_key="K1")
    assert any(e["event_type"] == "state.rolled_back" and e["case_id"] == "SYN-CASE-001" for e in env.service.audit.events)
    monkeypatch.undo()
    env.run(make(), idempotency_key="K1")
    assert [e["event_type"] for e in env.service.audit.events].count("replay.served") == 0   # the retry was computed, not replayed
