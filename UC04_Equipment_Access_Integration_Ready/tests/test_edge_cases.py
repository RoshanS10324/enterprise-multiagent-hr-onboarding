"""UC-04 edge cases from the onboarding framework, one explicit test per in-scope EC id.

Several of these are also exercised in the TC files; the names here make the EC-to-test mapping explicit. EC ids that
belong to the wider onboarding product are listed in specs/TRACEABILITY.md as outside the UC-04 boundary.
"""

from helpers import ALL, approval, approvals, codes, delivery, item, make, rid, ticket
from sub_agent.states import CaseStatus, ItemState, ParentCaseStatus


def test_ec11_joining_date_or_designation_change_replans_and_flags_it(env):
    env.run(make(approval_records=approvals(ALL)))
    r = env.run(make(approval_records=approvals(ALL), joining_date="2026-11-30", form_version="v2"))
    assert r.reassessment.required and item(r, "201").due_date.isoformat() == "2026-11-16"


def test_ec22_and_ec23_withdrawal_drafts_cancellations_and_equipment_release_for_approval(env):
    payload = make(approval_records=approvals(ALL))
    env.submit(payload)
    env.cases.set_status("SYN-CASE-001", ParentCaseStatus.WITHDRAWN)
    r = env.run(payload, idempotency_key="withdrawn")
    assert {d.proposal_type for d in r.cancellation_drafts} == {"NOTIFY_IT_WITHDRAWAL"}
    assert all(d.requires_approval_by == "HR_OFFICER" and not d.executed for d in r.cancellation_drafts)


def test_ec29_laptop_delivery_delayed_flags_manager_and_hr_and_proposes_a_loan_device(env):
    r = env.run(make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")],
                     delivery_status=[delivery("201", "PENDING", expected="2026-12-01")]))
    risk = next(x for x in r.risks if x.code == "EQUIPMENT_DELAYED_BEYOND_JOINING")
    assert "manager" in risk.next_action and "HR approval" in risk.next_action
    assert any(p.proposal_type == "LOAN_DEVICE" and p.requires_approval_by == "HR_OFFICER" for p in r.proposals)


def test_ec30_it_rejection_tells_the_manager_the_recorded_reason_and_asks_for_a_corrected_request(env):
    r = env.run(make(approval_records=approvals(["101"]), mock_it_ticket_updates=[
        ticket("101", "CREATED"), ticket("101", "REJECTED", reason="Licence not available for this role")]))
    block = next(b for b in r.blockers if b.code == "IT_REJECTED")
    assert block.owner == "HIRING_MANAGER" and "Licence not available" in block.reason and "corrected request" in block.next_action


def test_ec31_manager_requests_access_outside_the_role_list_raises_an_exception(env):
    extra = {"request_key": "SYN-REQ-103", "item_id": "production-database-access", "item_kind": "ACCESS",
             "requested_by": "SYN-USER-201"}
    r = env.run(make(requested_access=[extra], requested_equipment=[]))
    out = item(r, "103")
    assert out.exception_required and out.state == ItemState.APPROVAL_PENDING and env.desk.calls == []


def test_ec34_client_site_clearance_longer_than_the_time_left_is_flagged_with_options(env):
    rules = {"state": "VERIFIED", "value": {"required_items": [], "block_on_late_clearance": False, "owner_role": "account_manager"}}
    r = env.run(make(client_site_required=True, config={"client_site_rules": rules}, as_of_date="2026-11-09",
                     client_site_clearance={"status": "PENDING"}))
    assert "CLIENT_SITE_CLEARANCE_LATE" in {x.code for x in r.risks}
    assert any(p.proposal_type == "OFFICE_START_OR_LATER_DEPLOYMENT" for p in r.proposals)


def test_ec36_remote_employee_gets_a_courier_and_remote_first_day_plan_and_no_shipment(env):
    rules = {"state": "VERIFIED", "value": {"country_code": "ZZ", "courier_extra_days": 5, "remote_first_day_required": True}}
    r = env.run(make(work_mode="REMOTE", config={"country_rules": rules}, approval_records=approvals(ALL)))
    assert r.remote_plan.remote_first_day_required is True and r.remote_plan.courier_extra_days == 5
    assert item(r, "201").lead_time_days == 15 and env.desk.calls == []


def test_ec39_two_employees_with_the_same_name_are_told_apart_by_references(env):
    a = env.run(make(n=1, display_label="Sam Sample"))
    b = env.run(make(n=3, display_label="Sam Sample"))
    assert a.case_id != b.case_id and not set(i.request_id for i in a.items) & set(i.request_id for i in b.items)


def test_ec42_internal_transfer_flags_existing_access_and_equipment_for_review(env):
    r = env.run(make(internal_transfer=True))
    assert "INTERNAL_TRANSFER_REVIEW" in codes(r.blockers) and r.overall_status == CaseStatus.NEEDS_HUMAN_REVIEW


def test_ec44_rehire_still_holding_equipment_stops_new_equipment_until_hr_and_it_review(env):
    payload = make(prior_equipment_held=True, approval_records=approvals(ALL))
    r = env.run(payload)
    assert {item(r, k).state for k in ("201", "202")} == {ItemState.BLOCKED}
    assert {item(r, k).state for k in ("101", "102")} == {ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION}  # access unaffected
    assert "PRIOR_EQUIPMENT_HELD" in codes(r.blockers)
    env.submit(payload)
    assert not {rid("201"), rid("202")} & {c["request_id"] for c in env.desk.calls}
