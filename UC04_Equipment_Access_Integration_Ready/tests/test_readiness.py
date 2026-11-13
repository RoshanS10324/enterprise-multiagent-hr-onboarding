"""Tickets, evidence and readiness (BR-07, BR-09, BR-11, BR-17; TC-11, 13, 14, 25, 26, 29; EC-30, 32, 33)."""

from helpers import (ACCESS_KEYS, ALL, EQUIPMENT_KEYS, approvals, codes, collection, delivery, item, make, provisioning,
                     ready_payload, rid, ticket)
from sub_agent.states import CaseStatus, ItemState


def test_tc26_all_evidence_and_no_blockers_is_ready(env):
    r = env.run(ready_payload())
    assert r.overall_status == CaseStatus.READY and r.blockers == [] and r.risks == []
    assert {i.state for i in r.items} == {ItemState.ITEM_COMPLETED} and all(i.completed for i in r.items)
    assert all(i.evidence_refs for i in r.items)
    assert item(r, "101").provisioning_state == "CONFIRMED_ACTIVE"
    assert env.desk.calls == []  # readiness came from evidence, not from any submission


def test_tc25_one_incomplete_item_prevents_ready(env):
    payload = ready_payload(provisioning_status=[provisioning("101")])  # 102 has no provisioning evidence
    r = env.run(payload)
    assert item(r, "102").state == ItemState.IT_IN_PROGRESS and r.overall_status != CaseStatus.READY


def test_tc14_ticket_complete_but_access_not_verified_is_not_active(env):
    r = env.run(make(approval_records=approvals(["101"]), mock_it_ticket_updates=[ticket("101")]))
    a = item(r, "101")
    assert a.state == ItemState.IT_IN_PROGRESS and a.provisioning_state == "UNVERIFIED" and not a.completed
    assert r.overall_status != CaseStatus.READY


def test_tc13_delivered_without_collection_evidence_is_not_complete_when_collection_is_required(env):
    payload = make(approval_records=approvals(EQUIPMENT_KEYS), mock_it_ticket_updates=[ticket(k) for k in EQUIPMENT_KEYS],
                   delivery_status=[delivery(k) for k in EQUIPMENT_KEYS])
    r = env.run(payload)
    laptop = item(r, "201")
    assert laptop.state == ItemState.EQUIPMENT_DELIVERED and laptop.collection_state == "NOT_COLLECTED"
    assert not laptop.completed and r.overall_status != CaseStatus.READY


def test_collection_is_not_required_when_the_client_delivers_instead(env):
    payload = make("bravo", 2, approval_records=[
        {**a, "client_id": "client_bravo", "approver_role": "line_manager", "request_id": a["request_id"].replace("001", "002")}
        for a in approvals(["201"])],
        requested_access=[], requested_equipment=[{"request_key": "SYN-REQ-201", "item_id": "laptop-standard",
                                                  "item_kind": "EQUIPMENT", "requested_by": "SYN-USER-201"}],
        mock_it_ticket_updates=[ticket("201", n=2)], delivery_status=[delivery("201", n=2)])
    r = env.run(payload)
    assert item(r, "201", 2).completed and item(r, "201", 2).collection_state == "NOT_REQUIRED"


def test_evidence_without_stored_reference_or_approved_role_does_not_count(env):
    bad_role = provisioning("101", role="random_role")
    r = env.run(make(approval_records=approvals(["101"]), mock_it_ticket_updates=[ticket("101")], provisioning_status=[bad_role]))
    assert item(r, "101").state == ItemState.IT_IN_PROGRESS and "EVIDENCE_NOT_AUTHORITATIVE" in codes(r.blockers)
    assert r.overall_status == CaseStatus.NEEDS_HUMAN_REVIEW


def test_evidence_before_the_ticket_is_complete_is_held(env):
    r = env.run(make(approval_records=approvals(["101"]), mock_it_ticket_updates=[ticket("101", "IN_PROGRESS")],
                     provisioning_status=[provisioning("101")]))
    assert item(r, "101").state == ItemState.IT_IN_PROGRESS and "EVIDENCE_BEFORE_TICKET_COMPLETE" in codes(r.blockers)


def test_evidence_without_any_ticket_is_ignored(env):
    r = env.run(make(approval_records=approvals(["101"]), provisioning_status=[provisioning("101")]))
    assert item(r, "101").state == ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION and "EVIDENCE_WITHOUT_TICKET" in codes(r.blockers)


def test_failed_provisioning_never_marks_access_active(env):
    r = env.run(make(approval_records=approvals(["101"]), mock_it_ticket_updates=[ticket("101")],
                     provisioning_status=[provisioning("101", "FAILED")]))
    assert item(r, "101").provisioning_state == "FAILED" and "PROVISIONING_FAILED" in codes(r.blockers)


# ---- IT rejection (BR-09)
def _rejected(env, extra_updates=()):
    payload = make(approval_records=approvals(["201"]),
                   mock_it_ticket_updates=[ticket("201", "CREATED"), ticket("201", "REJECTED", reason="Wrong model requested"),
                                           *extra_updates])
    return payload, env.run(payload)


def test_tc11_it_rejection_records_reason_state_and_owner_without_retry(env):
    payload, r = _rejected(env)
    laptop = item(r, "201")
    assert laptop.state == ItemState.REJECTED_CORRECTION_REQUIRED
    block = next(b for b in r.blockers if b.code == "IT_REJECTED")
    assert "Wrong model requested" in block.reason and block.owner == "HIRING_MANAGER"
    assert "new request key" in block.next_action
    assert r.overall_status == CaseStatus.NEEDS_HUMAN_REVIEW
    assert env.submit(payload).submitted == [] and env.desk.calls == []  # not resubmitted


def test_tc11_updates_after_a_rejection_are_ignored(env):
    payload, first = _rejected(env)
    later = make(approval_records=approvals(["201"]), as_of_date="2026-10-16",
                 mock_it_ticket_updates=[ticket("201", "CREATED"), ticket("201", "REJECTED", reason="Wrong model requested"),
                                         ticket("201", "COMPLETED")])
    r = env.run(later)
    assert item(r, "201").state == ItemState.REJECTED_CORRECTION_REQUIRED
    assert "TICKET_UPDATE_IGNORED" in codes(r.blockers)


def test_tc29_rejected_access_is_never_marked_active(env):
    r = env.run(make(approval_records=approvals(["101"]),
                     mock_it_ticket_updates=[ticket("101", "CREATED"), ticket("101", "REJECTED", reason="Not permitted")],
                     provisioning_status=[provisioning("101")]))
    a = item(r, "101")
    assert a.state == ItemState.REJECTED_CORRECTION_REQUIRED and a.provisioning_state == "UNVERIFIED" and not a.completed
    assert "EVIDENCE_AFTER_REJECTION" in codes(r.blockers)


def test_corrected_request_under_a_new_key_supersedes_the_rejected_one(env):
    _rejected(env)
    new_item = {"request_key": "SYN-REQ-211", "item_id": "laptop-standard", "item_kind": "EQUIPMENT", "requested_by": "SYN-USER-201"}
    payload = make(requested_access=[], as_of_date="2026-10-16",
                   requested_equipment=[{"request_key": "SYN-REQ-201", "item_id": "laptop-standard", "item_kind": "EQUIPMENT",
                                         "requested_by": "SYN-USER-201"}, new_item],
                   approval_records=approvals(["201"]) + [{**approvals(["201"])[0], "request_id": rid("211")}],
                   mock_it_ticket_updates=[ticket("201", "CREATED"), ticket("201", "REJECTED", reason="Wrong model requested")])
    r = env.run(payload)
    assert item(r, "201").superseded and not item(r, "211").superseded
    assert item(r, "211").state == ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION
    assert "IT_REJECTED" not in codes(r.blockers)


# ---- equipment problems
def test_ec33_faulty_equipment_gets_an_exchange_draft_and_is_not_assigned(env):
    r = env.run(make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")],
                     delivery_status=[delivery("201", "FAULTY")]))
    laptop = item(r, "201")
    assert laptop.state == ItemState.IT_IN_PROGRESS and not laptop.completed
    assert any(p.proposal_type == "EXCHANGE_REQUEST_DRAFT" and not p.executed for p in r.proposals)
    assert "EQUIPMENT_FAULTY_OR_WRONG" in codes(r.blockers)


def test_ec32_missed_collection_slot_reminds_and_keeps_the_device_reserved(env):
    r = env.run(make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")],
                     delivery_status=[delivery("201")], collection_status=[collection("201", "MISSED_SLOT", next_slot="2026-11-09", evidence=None)]))
    risk = next(x for x in r.risks if x.code == "COLLECTION_SLOT_MISSED")
    assert "2026-11-09" in risk.next_action and "reserved" in risk.next_action
    assert item(r, "201").collection_state == "MISSED_SLOT" and not item(r, "201").completed
