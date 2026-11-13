"""Joining-date risk, lead times, workarounds, client sites, reassessment (BR-10, BR-12, BR-20; TC-12, 15, 22, 28, 38;
EC-11/12/13/29/34/35)."""

import pytest

from helpers import ALL, ACCESS_KEYS, approvals, approval, codes, delivery, item, make, ready_payload, rid, ticket
from sub_agent.service import CaseMismatchError
from sub_agent.states import CaseStatus, ItemState

SITE_RULES = {"state": "VERIFIED", "value": {"required_items": [], "block_on_late_clearance": False,
                                              "owner_role": "account_manager"}}
BLOCKING_SITE_RULES = {"state": "VERIFIED", "value": {"required_items": [], "block_on_late_clearance": True,
                                                       "owner_role": "account_manager"}}


def _laptop_delayed(**extra):
    return make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")],
                delivery_status=[delivery("201", "PENDING", expected="2026-11-20")], **extra)


def test_tc12_laptop_delayed_past_joining_flags_risk_and_proposes_a_loan_device_only(env):
    r = env.run(_laptop_delayed())
    assert "EQUIPMENT_DELAYED_BEYOND_JOINING" in {x.code for x in r.risks}
    loan = next(p for p in r.proposals if p.proposal_type == "LOAN_DEVICE")
    assert loan.request_id == rid("201") and loan.requires_approval_by == "HR_OFFICER" and loan.executed is False
    assert r.overall_status == CaseStatus.AT_RISK and r.overall_status != CaseStatus.READY
    assert all(x.owner and x.next_action for x in r.risks)


def test_tc28_loan_device_is_a_proposal_nothing_is_ordered_or_assigned(env):
    before = env.service.store.all_request_ids()
    r = env.run(_laptop_delayed())
    after = env.service.store.all_request_ids()
    assert len(after) == len(set(after)) == 4 and set(before) <= set(after)  # no extra request record for a loan device
    assert env.desk.calls == [] and env.desk.tickets == {}
    assert not item(r, "201").completed


def test_ec12_joining_date_too_close_for_the_lead_time_flags_risk_and_workaround(env):
    r = env.run(make(approval_records=approvals(ALL), as_of_date="2026-11-05"))  # equipment due was 2026-11-02
    assert {"REQUEST_NOT_RAISED_BY_DUE_DATE"} <= {x.code for x in r.risks}
    assert any(p.proposal_type == "LOAN_DEVICE" for p in r.proposals)
    assert r.overall_status == CaseStatus.AT_RISK


def test_stalled_approval_past_due_date_escalates(env):
    r = env.run(make(as_of_date="2026-11-10"))
    stalled = [x for x in r.risks if x.code == "APPROVAL_STALLED"]
    assert stalled and all("HR officer" in x.next_action for x in stalled)
    assert r.overall_status == CaseStatus.AT_RISK


def test_joining_date_reached_with_open_items_escalates(env):
    r = env.run(make(as_of_date="2026-11-16"))
    assert "JOINING_DATE_REACHED_INCOMPLETE" in {x.code for x in r.risks}


def test_ec13_joining_on_a_weekend_suggests_the_next_working_day_without_changing_it(env):
    r = env.run(make(joining_date="2026-11-14"))  # a Saturday
    assert "JOINING_DATE_NON_WORKING_DAY" in {x.code for x in r.risks}
    p = next(p for p in r.proposals if p.proposal_type == "NEXT_WORKING_DAY_SUGGESTION")
    assert "2026-11-16" in p.description and p.executed is False


def test_ec13_public_holiday_is_flagged_using_the_configured_calendar(env):
    r = env.run(make(joining_date="2026-12-02"))
    assert "JOINING_DATE_NON_WORKING_DAY" in {x.code for x in r.risks}


# ---- reassessment
def test_tc22_joining_date_change_recomputes_due_dates_and_flags_reassessment(env):
    first = env.run(make(approval_records=approvals(ALL)))
    assert item(first, "201").due_date.isoformat() == "2026-11-02" and first.reassessment.required is False
    moved = env.run(make(approval_records=approvals(ALL), joining_date="2026-11-23", form_version="v2"))
    assert moved.reassessment.required and moved.reassessment.changed_fields == ["form_version", "joining_date"]
    assert moved.reassessment.previous_joining_date.isoformat() == "2026-11-16"
    assert item(moved, "201").due_date.isoformat() == "2026-11-09" and item(moved, "101").due_date.isoformat() == "2026-11-16"
    assert any(e["event_type"] == "reassessment.triggered" for e in moved.audit_events)


def test_tc38_role_change_invalidates_old_approvals_and_requires_reapproval(env):
    env.run(make(approval_records=approvals(ALL)))
    cfg = {"role_id": "data_engineer"}
    changed = env.run(make(role_id="data_engineer", form_version="v2", approval_records=approvals(ALL),
                           config=cfg | {"role_access_matrix": {"state": "VERIFIED", "value": ["email-teams-account", "source-control-access"]}}))
    assert changed.reassessment.required and "role_id" in changed.reassessment.changed_fields
    assert set(changed.reassessment.reapproval_request_ids) == {rid(k) for k in ALL}
    assert {i.state for i in changed.items} == {ItemState.APPROVAL_PENDING}  # back to approval, nothing silently reused
    assert changed.overall_status == CaseStatus.AWAITING_APPROVAL


def test_changed_form_must_be_approved_before_anything_proceeds(env):
    env.run(make())
    r = env.run(make(form_version="v2", onboarding_form_approved=False))
    assert r.overall_status == CaseStatus.BLOCKED and "START_GATE_FORM_NOT_APPROVED" in codes(r.blockers)


def test_ec35_client_change_needs_the_parent_case_to_change_first(env):
    with pytest.raises(CaseMismatchError):
        env.run(make(client_id="client_bravo", config=__import__("sub_agent.fixtures", fromlist=["load"]).load("client_bravo_config.json")))


# ---- client site
def _site(**patch):
    base = dict(client_site_required=True, config={"client_site_rules": SITE_RULES},
                client_site_clearance={"status": "PENDING"})
    base.update(patch)
    return base


def test_tc15_clearance_that_cannot_finish_in_time_is_flagged_with_a_workaround(env):
    r = env.run(make(**_site(client_site_clearance={"status": "PENDING", "expected_date": "2026-11-30"})))
    assert "CLIENT_SITE_CLEARANCE_LATE" in {x.code for x in r.risks} and r.overall_status == CaseStatus.AT_RISK
    assert any(p.proposal_type == "OFFICE_START_OR_LATER_DEPLOYMENT" and not p.executed for p in r.proposals)
    assert next(x for x in r.risks if x.code == "CLIENT_SITE_CLEARANCE_LATE").owner == "account_manager"


def test_tc15_late_clearance_blocks_when_the_client_rule_says_so(env):
    r = env.run(make(**_site(config={"client_site_rules": BLOCKING_SITE_RULES},
                             client_site_clearance={"status": "PENDING", "expected_date": "2026-11-30"})))
    assert r.overall_status == CaseStatus.BLOCKED and "CLIENT_SITE_CLEARANCE_LATE" in codes(r.blockers)


def test_clearance_pending_but_on_time_keeps_the_case_in_progress_not_ready(env):
    payload = ready_payload(**_site(approval_records=approvals(ALL, site=True)))
    r = env.run(payload)
    assert r.overall_status == CaseStatus.IN_PROGRESS and r.clearance_status.value == "PENDING"
    assert {i.state for i in r.items} == {ItemState.ITEM_COMPLETED}  # items done, but the site is not cleared


def test_confirmed_clearance_with_authoritative_evidence_allows_ready(env):
    clearance = {"status": "CONFIRMED", "evidence_ref": "SYN-EVD-900", "asserted_by_role": "account_manager"}
    r = env.run(ready_payload(**_site(approval_records=approvals(ALL, site=True), client_site_clearance=clearance)))
    assert r.overall_status == CaseStatus.READY


def test_clearance_evidence_from_an_unapproved_role_is_not_accepted(env):
    clearance = {"status": "CONFIRMED", "evidence_ref": "SYN-EVD-900", "asserted_by_role": "intern"}
    r = env.run(ready_payload(**_site(approval_records=approvals(ALL, site=True), client_site_clearance=clearance)))
    assert r.overall_status == CaseStatus.NEEDS_HUMAN_REVIEW and "EVIDENCE_NOT_AUTHORITATIVE" in codes(r.blockers)


def test_denied_clearance_goes_to_a_person(env):
    r = env.run(make(**_site(client_site_clearance={"status": "DENIED"})))
    assert "CLIENT_SITE_CLEARANCE_DENIED" in codes(r.blockers) and r.overall_status == CaseStatus.NEEDS_HUMAN_REVIEW


def test_client_site_item_completes_only_after_clearance(env):
    site_item = {"request_key": "SYN-REQ-101", "item_id": "email-teams-account", "item_kind": "ACCESS",
                 "requested_by": "SYN-USER-201", "client_site": True}
    base = dict(requested_access=[site_item], requested_equipment=[], client_site_required=True,
                config={"client_site_rules": SITE_RULES}, approval_records=approvals(["101"], site=True),
                mock_it_ticket_updates=[ticket("101")], provisioning_status=[{**__import__("helpers").provisioning("101")}])
    pending = env.run(make(client_site_clearance={"status": "PENDING"}, **base))
    assert item(pending, "101").state == ItemState.PROVISIONING_CONFIRMED and not item(pending, "101").completed
    cleared = env.run(make(client_site_clearance={"status": "CONFIRMED", "evidence_ref": "SYN-EVD-900",
                                                  "asserted_by_role": "account_manager"}, as_of_date="2026-10-16", **base))
    assert item(cleared, "101").completed and cleared.overall_status == CaseStatus.READY


def test_configured_client_site_items_must_be_requested(env):
    rules = {"state": "VERIFIED", "value": {"required_items": ["client-badge", "client-nda"], "block_on_late_clearance": False,
                                            "owner_role": "account_manager"}}
    r = env.run(make(**_site(config={"client_site_rules": rules})))
    missing = [b for b in r.blockers if b.code == "CLIENT_SITE_ITEM_MISSING"]
    assert len(missing) == 2 and all(b.owner == "account_manager" for b in missing)


def test_missing_client_site_rules_block_the_case(env):
    r = env.run(make(client_site_required=True, client_site_clearance={"status": "PENDING"}))
    assert r.overall_status == CaseStatus.BLOCKED and "CONFIG_MISSING:client_site_rules" in r.config_gaps
