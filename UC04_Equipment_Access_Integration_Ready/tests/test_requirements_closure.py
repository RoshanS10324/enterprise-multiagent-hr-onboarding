"""Gap-closure tests found while auditing UC-04 against the assigned edge cases (EC-29, EC-31, EC-35) and the mock IT desk.
Boundary, negative and record-keeping cases that the scenario tests did not assert. Synthetic data only."""

from helpers import approval, approvals, collection, delivery, make, rid, ticket
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk
from sub_agent.agent import Uc04Agent
from sub_agent.states import ItemState


def run(payload, agent=None, key="k"):
    a = agent or Uc04Agent()
    return a, a.evaluate(payload, actor_id="SYN-USER-100", idempotency_key=key)


def codes(r):
    return {x.code for x in r.risks}


def laptop(r):
    return next(i for i in r.items if i.request_id.endswith("201"))


def base(**delivery_kw):
    d = delivery("201", **{k: v for k, v in delivery_kw.items() if k in ("status", "expected")})
    if "delivered_on" in delivery_kw:
        d["delivered_on"] = delivery_kw["delivered_on"]
    return make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")], delivery_status=[d])


# ---------------------------------------------------------------------------------------------------------------- EC-29 boundaries
def test_ec29_expected_delivery_on_the_joining_date_is_on_time():
    _, r = run(make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")],
                    delivery_status=[delivery("201", "PENDING", expected="2026-11-16")]))
    assert "EQUIPMENT_DELAYED_BEYOND_JOINING" not in codes(r)


def test_ec29_expected_delivery_one_day_after_the_joining_date_is_late():
    _, r = run(make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")],
                    delivery_status=[delivery("201", "PENDING", expected="2026-11-17")]))
    assert "EQUIPMENT_DELAYED_BEYOND_JOINING" in codes(r)
    risk = next(x for x in r.risks if x.code == "EQUIPMENT_DELAYED_BEYOND_JOINING")
    assert risk.owner == "HR_OFFICER"


def test_ec29_a_recorded_delivery_after_the_joining_date_is_detected_while_the_item_is_open():
    _, r = run(base(delivered_on="2026-11-18"))  # alpha collects equipment: delivered but not yet collected, so the item is open
    assert laptop(r).state == ItemState.EQUIPMENT_DELIVERED
    assert "EQUIPMENT_DELAYED_BEYOND_JOINING" in codes(r)
    assert next(x for x in r.risks if x.code == "EQUIPMENT_DELAYED_BEYOND_JOINING").reason == "Delivery happened after the joining date."
    emp = next(p for p in r.proposals if p.proposal_type == "MESSAGE_DRAFT_EMPLOYEE_DELAY")
    assert "was delivered on 2026-11-18" in emp.payload["body"] and emp.executed is False


def test_ec29_a_recorded_delivery_on_the_joining_date_is_on_time():
    _, r = run(base(delivered_on="2026-11-16"))
    assert laptop(r).state == ItemState.EQUIPMENT_DELIVERED
    assert "EQUIPMENT_DELAYED_BEYOND_JOINING" not in codes(r)


def test_ec29_a_late_delivery_of_a_finished_item_stays_on_the_audit_record_without_a_false_alarm():
    payload = base(delivered_on="2026-11-18")
    payload["collection_status"] = [collection("201")]  # collected with authoritative evidence: the item is complete
    _, r = run(payload)
    assert laptop(r).state == ItemState.ITEM_COMPLETED
    assert "EQUIPMENT_DELAYED_BEYOND_JOINING" not in codes(r)
    assert any(e["event_type"] == "delivery.after_joining" for e in r.audit_events)


# ---------------------------------------------------------------------------------------------------------------- EC-31 negatives
def test_ec31_a_standard_approval_never_substitutes_for_the_exception_approval():
    # the out-of-matrix laptop is item 202 or similar: find any exception-required item and give it only a standard approval
    payload = make(requested_equipment=[{"request_key": "SYN-REQ-299", "item_id": "gaming-laptop", "item_kind": "EQUIPMENT",
                                         "requested_by": "SYN-USER-100"}],
                   approval_records=[approval("201", request_id=rid("299"), item="gaming-laptop")])
    _, r = run(payload)
    it = next(i for i in r.items if i.request_id.endswith("299"))
    assert it.exception_required is True
    assert it.state in (ItemState.EXCEPTION_PENDING, ItemState.APPROVAL_PENDING, ItemState.BLOCKED)
    assert it.state != ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION


# ---------------------------------------------------------------------------------------------------------------- EC-35 record keeping
def test_ec35_the_previous_client_is_kept_on_record_after_a_client_change():
    from test_client_change import alpha_round, bravo_round
    from helpers import Env
    env = Env()
    env.run(alpha_round(), idempotency_key="a1")
    env.cases.set_client("SYN-CASE-001", "client_bravo")
    r = env.run(bravo_round(), idempotency_key="b1")
    assert r.reassessment.previous_client_id == "client_alpha"
    assert r.reassessment.previous_config_version  # the old configuration version is preserved
    assert r.reassessment.previous_config_version != r.config_version


def test_ec35_unchanged_fields_leave_the_previous_values_empty():
    a, first = run(make(), key="x1")
    r = a.evaluate(make(joining_date="2026-11-17"), actor_id="SYN-USER-100", idempotency_key="x2")
    assert r.reassessment.required and "joining_date" in r.reassessment.changed_fields
    assert r.reassessment.previous_client_id is None and r.reassessment.previous_config_version is None


# ---------------------------------------------------------------------------------------------------------------- mock IT desk
def test_mock_it_desk_is_idempotent_per_request_and_never_real():
    desk = MockITServiceDesk()
    one = desk.submit_request(request_id="c:r1", case_id="c", item_kind="EQUIPMENT", item_id="laptop-std")
    two = desk.submit_request(request_id="c:r1", case_id="c", item_kind="EQUIPMENT", item_id="laptop-std")
    other = desk.submit_request(request_id="c:r2", case_id="c", item_kind="EQUIPMENT", item_id="headset")
    assert one.ticket_id == two.ticket_id != other.ticket_id
    assert desk.real_side_effects is False and len(desk.tickets) == 2
