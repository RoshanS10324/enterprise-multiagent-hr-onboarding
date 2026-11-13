"""Validation, duplicates, adapter failures, cancellation, policy (BR-14, BR-18, BR-21, BR-22; TC-17, 18, 20, 21, 23, 31, 35, 37;
EC-22/23/38/39)."""

import pytest

from helpers import ALL, Env, approval, approvals, codes, item, make, ready_payload, rid, ticket
from sub_agent.adapters.case_context_mock import MockCaseContext
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk
from sub_agent.entrypoints.direct import DirectEntry
from sub_agent.policy import PolicyViolation
from sub_agent.ports import AdapterTimeout, ParentCase
from sub_agent.service import CaseMismatchError, DuplicateCaseError, UnknownCaseError, Uc04ValidationError
from sub_agent.states import CaseStatus, ItemState, ParentCaseStatus
from sub_agent.store import IdempotencyConflict


# ---- TC-20: invalid input has no side effect
@pytest.mark.parametrize("patch,field", [
    ({"joining_date": "2026-13-45"}, "joining_date"),
    ({"case_id": "CASE-1"}, "case_id"),
    ({"offer_accepted": "yes"}, "offer_accepted"),
    ({"work_mode": "MOON"}, "work_mode"),
    ({"unexpected_field": 1}, "unexpected_field"),
    ({"mock_it_ticket_updates": [{"request_id": "x", "ticket_id": "SYN-TKT-0001", "status": "PAUSED", "updated_on": "2026-10-15"}]},
     "mock_it_ticket_updates"),
    ({"requested_access": [{"request_key": "SYN-REQ-101", "item_id": "x1", "item_kind": "EQUIPMENT", "requested_by": "SYN-USER-201"}]},
     "Value error"),
])
def test_tc20_malformed_input_is_rejected_with_no_side_effect(env, patch, field):
    with pytest.raises(Uc04ValidationError):
        env.run(make(**patch))
    assert env.service.store.case_ids == [] and env.desk.calls == [] and env.cases.created == []
    event = [e for e in env.service.audit.events if e["event_type"] == "input.validation_failed"][-1]
    assert event["case_id"] is None and "SYN-EMP" not in str(event)  # field names only, never values


def test_tc20_real_looking_identifiers_are_refused_in_local_mode(env):
    with pytest.raises(Uc04ValidationError):
        env.run(make(employee_ref="EMP-48213"))


# ---- TC-17 / TC-31 / TC-18: deduplication and idempotency
def test_tc17_duplicate_request_keys_and_duplicate_items_create_no_second_record(env):
    dup_key = {"request_key": "SYN-REQ-201", "item_id": "laptop-standard", "item_kind": "EQUIPMENT", "requested_by": "SYN-USER-201"}
    dup_item = {"request_key": "SYN-REQ-299", "item_id": "laptop-standard", "item_kind": "EQUIPMENT", "requested_by": "SYN-USER-201"}
    base = make()
    base["requested_equipment"] += [dup_key, dup_item]
    r = env.run(base)
    assert env.service.store.request_ids("SYN-CASE-001") == sorted(rid(k) for k in ALL)
    assert {(d.request_key, d.reason_code) for d in r.duplicates_ignored} == {
        ("SYN-REQ-201", "DUPLICATE_REQUEST_KEY"), ("SYN-REQ-299", "DUPLICATE_ITEM_FOR_CASE")}
    assert len(r.items) == 4


def test_ec38_duplicate_onboarding_case_for_the_same_employee_is_linked_not_opened(env):
    cases = MockCaseContext([ParentCase(case_id="SYN-CASE-001", employee_ref="SYN-EMP-001", client_id="client_alpha"),
                             ParentCase(case_id="SYN-CASE-005", employee_ref="SYN-EMP-001", client_id="client_alpha")])
    e = Env(cases=cases)
    with pytest.raises(DuplicateCaseError) as exc:
        e.run(make(n=5, employee_ref="SYN-EMP-001"))
    assert exc.value.existing_case_id == "SYN-CASE-001"
    assert e.service.store.case_ids == [] and cases.created == []


def test_tc18_two_employees_with_the_same_name_stay_separate_and_the_name_is_never_used(env):
    p1 = make(n=1, display_label="Alex Sample", approval_records=approvals(["201"], n=1))
    p3 = make(n=3, display_label="Alex Sample")
    r1, r3 = env.run(p1), env.run(p3)
    assert item(r1, "201", 1).state == ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION
    assert item(r3, "201", 3).state == ItemState.APPROVAL_PENDING  # case 1's approval does not leak to case 3
    assert env.service.store.case_ids == ["SYN-CASE-001", "SYN-CASE-003"]
    assert "Alex" not in str(r1.model_dump()) and "Alex" not in str(env.service.audit.events)


def test_tc31_repeated_processing_is_idempotent(env):
    payload = make(approval_records=approvals(ALL))
    first = env.run(payload)
    ids = env.service.store.all_request_ids()
    again = env.run(payload)  # same key: replay
    assert again.model_dump() == first.model_dump()
    other = env.run(payload, run_id="another-run", idempotency_key="another-key")  # new key: recomputed, same decisions
    assert other.items == first.items and other.overall_status == first.overall_status and other.blockers == first.blockers
    assert env.service.store.all_request_ids() == ids
    env.submit(payload)
    env.submit(payload)
    assert len(env.desk.calls) == 4 and len(env.desk.tickets) == 4  # submitted once per request


def test_idempotency_key_reused_with_a_different_payload_is_refused(env):
    env.run(make(), idempotency_key="k-1")
    with pytest.raises(IdempotencyConflict):
        env.run(make(as_of_date="2026-10-16"), idempotency_key="k-1")


def test_submitted_requests_become_ticketed_and_stay_idempotent(env):
    payload = make(approval_records=approvals(ALL))
    report = env.submit(payload)
    assert sorted(report.submitted) == sorted(rid(k) for k in ALL)
    assert {i.state for i in report.result.items} == {ItemState.MOCK_TICKET_CREATED}
    assert all(c["request_id"].startswith("SYN-CASE-001") for c in env.desk.calls)
    assert env.submit(payload).submitted == []


# ---- TC-21: adapter failures fail closed
def test_tc21_adapter_timeout_fails_closed_preserves_state_and_blocks_blind_retry():
    desk = MockITServiceDesk(fail_with=AdapterTimeout("boom secret=abc123"))
    env = Env(it_desk=desk)
    payload = make(approval_records=approvals(["201"]))
    report = env.submit(payload)
    assert report.submitted == [] and report.failed == [{"request_id": rid("201"), "error": "AdapterTimeout"}]
    laptop = item(report.result, "201")
    assert laptop.state == ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION and laptop.ticket_id is None
    assert "ADAPTER_ERROR" in codes(report.result.blockers) and report.result.overall_status == CaseStatus.NEEDS_HUMAN_REVIEW
    assert any(a.owner == "IT_SERVICE_DESK" for a in report.result.next_actions)
    text = str(env.service.audit.events) + str(report.result.model_dump())
    assert "abc123" not in text and "secret" not in text  # only the error class is recorded
    assert env.submit(payload).skipped[0]["reason"] == "PREVIOUS_ADAPTER_ERROR_UNRESOLVED" and len(desk.calls) == 1
    env.service.acknowledge_adapter_error("SYN-CASE-001", rid("201"), ["hr_onboarding_officer"])
    desk.fail_with = None
    assert env.submit(payload).submitted == [rid("201")]


def test_tc21_unexpected_adapter_exception_also_fails_closed():
    desk = MockITServiceDesk(fail_with=RuntimeError("raw payload with password=hunter2"))
    env = Env(it_desk=desk)
    report = env.submit(make(approval_records=approvals(["201"])))
    assert report.failed[0]["error"] == "RuntimeError" and report.submitted == []
    assert "hunter2" not in str(env.service.audit.events) + str(report.result.model_dump())


# ---- cancellation (TC-23, TC-37)
def test_tc23_withdrawal_stops_processing_and_only_drafts_cancellations(env):
    payload = make(approval_records=approvals(ALL))
    env.submit(payload)
    calls = len(env.desk.calls)
    env.cases.set_status("SYN-CASE-001", ParentCaseStatus.WITHDRAWN)
    r = env.run(payload, idempotency_key="after-withdrawal")
    assert r.overall_status == CaseStatus.CANCELLED and {i.state for i in r.items} == {ItemState.CANCELLED}
    kinds = {d.proposal_type for d in r.cancellation_drafts}
    assert kinds == {"NOTIFY_IT_WITHDRAWAL"} and all(not d.executed and d.requires_approval_by == "HR_OFFICER"
                                                                                for d in r.cancellation_drafts)
    report = env.submit(payload, idempotency_key="after-withdrawal-2")
    assert report.submitted == [] and report.skipped == [{"request_id": "*", "reason": "CASE_NOT_ACTIVE"}]
    assert len(env.desk.calls) == calls  # nothing further was processed


def test_withdrawal_before_any_processing_creates_nothing(env):
    env.cases.set_status("SYN-CASE-001", ParentCaseStatus.CANCELLED)
    r = env.run(make(approval_records=approvals(ALL)))
    assert r.overall_status == CaseStatus.CANCELLED and r.items == [] and env.service.store.case_ids == []


def test_a_cancelled_item_cannot_be_revived(env):
    payload = make(approval_records=approvals(ALL))
    env.run(payload)
    env.cases.set_status("SYN-CASE-001", ParentCaseStatus.CANCELLED)
    env.run(payload, idempotency_key="c1")
    env.cases.set_status("SYN-CASE-001", ParentCaseStatus.ACTIVE)
    revived = env.run(payload, idempotency_key="c2")
    assert "INVALID_STATE_TRANSITION" in codes(revived.blockers) and revived.overall_status != CaseStatus.READY


# ---- case rules and policy
def test_unknown_case_is_rejected_and_never_created(env):
    with pytest.raises(UnknownCaseError):
        env.run(make(n=9))
    assert env.cases.created == [] and env.service.store.case_ids == []


def test_employee_or_client_must_match_the_parent_case(env):
    with pytest.raises(CaseMismatchError):
        env.run(make(employee_ref="SYN-EMP-777"))


def test_tc35_caller_without_an_allowed_role_cannot_invoke_uc04(env):
    with pytest.raises(PolicyViolation):
        env.direct.evaluate(make(), actor_id="SYN-USER-100", roles=["intern"])
    assert env.service.store.case_ids == [] and any(e["event_type"] == "caller.rejected" for e in env.service.audit.events)


def test_kill_switch_disables_the_capability(env):
    env.service.policy = env.service.policy.model_copy(update={"enabled": False})
    with pytest.raises(PolicyViolation):
        env.run(make())


def test_write_tool_requires_approval_and_allowlist(env):
    gw = env.service.gateway
    with pytest.raises(PolicyViolation):
        gw.call("it_service_desk.submit_request", lambda **k: None, approved=False)
    with pytest.raises(PolicyViolation):
        gw.call("hr_system.update_record", lambda **k: None, approved=True)  # not on the allowlist
    assert gw.calls == []
