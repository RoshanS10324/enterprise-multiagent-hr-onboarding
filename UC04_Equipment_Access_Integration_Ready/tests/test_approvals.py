"""Approval validation and routing (BR-05, BR-16; TC-10; EC-31/37; GR-02, GR-17)."""

import pytest

from helpers import ALL, approval, approvals, codes, item, make, rid
from sub_agent.states import CaseStatus, ItemState


def _reasons(result):
    return {(r.approval_id, r.reason_code) for r in result.rejected_approvals}


@pytest.mark.parametrize("bad,reason", [
    (dict(role="intern"), "WRONG_APPROVER_ROLE"),
    (dict(item="headset-basic"), "ITEM_MISMATCH"),  # approval is for a different item than the request
    (dict(client="client_bravo"), "STALE_SCOPE"),
    (dict(role_id="data_engineer"), "STALE_SCOPE"),
    (dict(site=True), "STALE_SCOPE"),
    (dict(on="2026-10-20"), "FUTURE_DATED"),  # as_of is 2026-10-15
])
def test_tc10_invalid_approval_evidence_is_rejected_and_does_not_count(env, bad, reason):
    rec = approval("201", **bad)
    r = env.run(make(approval_records=[rec]))
    assert item(r, "201").state == ItemState.APPROVAL_PENDING
    assert (rec["approval_id"], reason) in _reasons(r)


def test_tc10_approval_for_an_unknown_request_id_is_rejected(env):
    rec = approval("201", request_id="SYN-CASE-001:SYN-REQ-999")
    r = env.run(make(approval_records=[rec]))
    assert (rec["approval_id"], "UNKNOWN_REQUEST") in _reasons(r)
    assert all(i.state == ItemState.APPROVAL_PENDING for i in r.items)


def test_approval_for_another_case_never_satisfies_this_case(env):
    other_case = approval("201", n=3)  # request id belongs to SYN-CASE-003
    r = env.run(make(approval_records=[other_case]))
    assert item(r, "201").state == ItemState.APPROVAL_PENDING and r.rejected_approvals


def test_exception_approval_on_an_in_matrix_item_is_not_accepted(env):
    rec = approval("201", role="exception_approver", typ="EXCEPTION")
    r = env.run(make(approval_records=[rec]))
    assert (rec["approval_id"], "EXCEPTION_NOT_REQUIRED") in _reasons(r)


def test_rejected_decision_by_authorised_approver_blocks_the_item_for_review(env):
    r = env.run(make(approval_records=[approval("201", decision="REJECTED")]))
    assert item(r, "201").state == ItemState.BLOCKED
    assert "APPROVAL_REJECTED" in codes(r.blockers) and r.overall_status == CaseStatus.NEEDS_HUMAN_REVIEW


def test_same_day_approve_and_reject_fails_safe_to_rejected(env):
    r = env.run(make(approval_records=[approval("201"), approval("201", decision="REJECTED")]))
    assert item(r, "201").approval_state.value == "REJECTED"


def test_ec37_manager_on_leave_routes_to_the_configured_delegate(env):
    payload = make(unavailable_approver_roles=["hiring_manager"],
                   approval_records=[approval("201", role="hiring_manager_delegate"),
                                     approval("202", role="hiring_manager")])
    r = env.run(payload)
    assert item(r, "201").state == ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION  # delegate accepted
    assert item(r, "202").state == ItemState.APPROVAL_PENDING  # unavailable primary no longer valid
    assert item(r, "201").required_approver == "hiring_manager_delegate"


def test_ec37_no_delegate_escalates_to_hr(env):
    r = env.run(make("bravo", 2, unavailable_approver_roles=["line_manager"]))
    assert "APPROVER_UNAVAILABLE_NO_DELEGATE" in codes(r.blockers)
    assert r.overall_status == CaseStatus.NEEDS_HUMAN_REVIEW
    assert any(b.owner == "HR_OFFICER" for b in r.blockers)


def test_ec51_client_instruction_cannot_skip_a_guardrail(env):
    overrides = {"skip_standard_approval": True, "allow_out_of_matrix_without_exception": True,
                 "client_hr_admin_ref": "SYN-USER-300"}
    r = env.run(make(config={"client_overrides": overrides}))
    assert {i.state for i in r.items} == {ItemState.APPROVAL_PENDING}  # still waits for approval (GR-17)
    assert any(e["event_type"] == "client_instruction_conflict" for e in r.audit_events)
    assert any(a.owner == "CLIENT_HR_ADMIN" for a in r.next_actions)


def test_every_blocker_and_risk_names_an_owner(env):
    r = env.run(make(config={"approver_config": {"state": "MISSING"}}, offer_accepted=True))
    assert r.blockers and all(b.owner and b.next_action for b in r.blockers)
