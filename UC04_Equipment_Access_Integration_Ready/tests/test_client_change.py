"""EC-35 (Framework section 11): "Project or client changes before joining -> New onboarding form version; cancel the old
client-site requests and start the new ones."

UC-04 never cancels anything itself: the old client-site requests are cancelled locally and DRAFT cancellations are proposed for
HR approval (same pattern as DL-15). Non-client-site items are not cancelled; their approvals are scope-checked as before.
All data is synthetic; the parent registry is the mock."""

import pytest

from helpers import Env, approval, make, rid, ticket
from sub_agent.guardrails import PrivacyViolation
from sub_agent.states import CaseStatus, ItemState

SITE = {"state": "VERIFIED", "value": {"required_items": [], "block_on_late_clearance": False, "owner_role": "account_manager"}}


def req(key: str, item_id: str, kind: str, site: bool) -> dict:
    return {"request_key": f"SYN-REQ-{key}", "item_id": item_id, "item_kind": kind, "requested_by": "SYN-USER-201", "client_site": site}


EMAIL = req("101", "email-teams-account", "ACCESS", False)          # normal item
SRC_SITE = req("102", "source-control-access", "ACCESS", True)      # client-site, will be ticketed
LAPTOP_SITE = req("201", "laptop-standard", "EQUIPMENT", True)      # client-site, will be ticketed
HEADSET_SITE = req("202", "headset-basic", "EQUIPMENT", True)       # client-site, approved but never ticketed
VPN_SITE = req("401", "vpn-access", "ACCESS", True)                 # new client's client-site requests
MONITOR_SITE = req("402", "monitor-27", "EQUIPMENT", True)


def alpha_round() -> dict:
    a = lambda k, **kw: approval(k, site=True, **kw)  # noqa: E731
    return make(requested_access=[EMAIL, SRC_SITE], requested_equipment=[LAPTOP_SITE, HEADSET_SITE], client_site_required=True,
                config={"client_site_rules": SITE}, form_version="v1",
                approval_records=[a("101"), a("102"), a("201"), a("202")],
                mock_it_ticket_updates=[ticket("101", "CREATED"), ticket("102", "CREATED"), ticket("201", "CREATED")])


def bravo_round(*, extra_items=(), form_version="v2", **patch) -> dict:
    b = lambda k, **kw: approval(k, site=True, client="client_bravo", role="line_manager", **kw)  # noqa: E731
    base = dict(requested_access=[EMAIL, VPN_SITE], requested_equipment=[MONITOR_SITE, *extra_items], client_site_required=True,
                config={"client_site_rules": SITE}, form_version=form_version,
                approval_records=[b("401", item="vpn-access"), b("402", item="monitor-27")])
    base.update(patch)
    return make("bravo", 1, **base)


@pytest.fixture
def moved(env: Env):
    """alpha case with items raised, then the parent case moves to client_bravo and UC-04 is called again."""
    first = env.run(alpha_round(), idempotency_key="alpha-1")
    assert first.overall_status == CaseStatus.IN_PROGRESS
    env.cases.set_client("SYN-CASE-001", "client_bravo")
    env.bravo = bravo_round()                       # keep the exact payload so a replay is byte-identical
    return env, env.run(env.bravo, idempotency_key="bravo-1")


def by_key(result, key):
    return next(i for i in result.items if i.request_key == f"SYN-REQ-{key}")


def test_client_change_cancels_the_old_client_site_requests_and_reports_them(moved):
    _, r = moved
    assert r.reassessment.required and {"client_id", "form_version"} <= set(r.reassessment.changed_fields)
    for key in ("102", "201", "202"):
        old = by_key(r, key)
        assert old.state == ItemState.CANCELLED and old.superseded is True and old.completed is False


def test_cancellations_are_drafts_for_hr_never_executed_and_only_for_requests_that_were_raised(moved):
    env, r = moved
    drafts = {(d.proposal_type, d.request_id) for d in r.cancellation_drafts}
    assert drafts == {("NOTIFY_IT_SUPERSEDED_REQUEST", rid("102")), ("NOTIFY_IT_SUPERSEDED_REQUEST", rid("201"))}   # 202 was never ticketed
    assert all(d.executed is False and d.requires_approval_by == "HR_OFFICER" for d in r.cancellation_drafts)
    assert any(a.owner == "HR_OFFICER" and "notice to IT" in a.action for a in r.next_actions)
    assert env.desk.calls == []                                                                   # nothing was sent anywhere


def test_old_requests_leave_the_active_store_and_are_archived_with_an_audit_trail(moved):
    env, r = moved
    record = env.service.store.get("SYN-CASE-001")
    assert set(record.items) == {"SYN-REQ-101", "SYN-REQ-401", "SYN-REQ-402"}
    assert set(record.archived) == {"SYN-REQ-102", "SYN-REQ-201", "SYN-REQ-202"}
    events = [e["event_type"] for e in r.audit_events]
    assert events.count("item.transition") >= 3 and "client_change.client_site_items_cancelled" in events
    assert any(e["event_type"] == "item.transition" and e["rule_id"] == "EC-35" and e["new_status"] == "CANCELLED" for e in r.audit_events)


def test_new_client_site_requests_start_fresh_under_the_new_clients_rules(moved):
    _, r = moved
    for key in ("401", "402"):
        new = by_key(r, key)
        assert new.state == ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION and new.superseded is False and new.required_approver == "line_manager"
    assert not [x for x in r.rejected_approvals if x.request_id in (rid("401"), rid("402"))]


def test_a_non_client_site_item_does_not_silently_reuse_the_old_clients_approval_or_ticket(moved):
    _, r = moved
    email = by_key(r, "101")
    assert email.state == ItemState.BLOCKED and email.superseded is False       # not cancelled by EC-35, not silently carried over
    assert "TICKET_WITHOUT_VALID_APPROVAL" in {b.code for b in r.blockers} and r.overall_status == CaseStatus.NEEDS_HUMAN_REVIEW


def test_superseded_old_requests_never_count_towards_readiness_or_submission(moved):
    env, r = moved
    assert r.overall_status != CaseStatus.READY
    report = env.submit(env.bravo, idempotency_key="submit-1")
    assert {s.rsplit(":", 1)[1] for s in report.submitted} == {"SYN-REQ-401", "SYN-REQ-402"}   # only the new approved requests


def test_re_raising_a_cancelled_request_key_is_refused_with_a_new_key_instruction(env):
    env.run(alpha_round(), idempotency_key="alpha-1")
    env.cases.set_client("SYN-CASE-001", "client_bravo")
    old_approval = approval("201", site=True, client="client_bravo", role="line_manager", item="laptop-standard")
    r = env.run(bravo_round(extra_items=[LAPTOP_SITE], approval_records=[old_approval]), idempotency_key="bravo-1")
    refused = next(b for b in r.blockers if b.code == "REQUEST_KEY_CANCELLED_BY_CLIENT_CHANGE")
    assert refused.owner == "ACCOUNT_MANAGER" and refused.request_id == rid("201") and "new request key" in refused.next_action
    assert r.overall_status == CaseStatus.NEEDS_HUMAN_REVIEW
    assert by_key(r, "201").state == ItemState.CANCELLED                       # still the archived, cancelled record
    assert [(x.request_id, x.reason_code) for x in r.rejected_approvals] == [(rid("201"), "UNKNOWN_REQUEST")]


def test_nothing_is_cancelled_until_the_new_form_version_is_approved(env):
    env.run(alpha_round(), idempotency_key="alpha-1")
    env.cases.set_client("SYN-CASE-001", "client_bravo")
    held = env.run(bravo_round(onboarding_form_approved=False), idempotency_key="bravo-0")
    assert held.overall_status == CaseStatus.BLOCKED and "START_GATE_FORM_NOT_APPROVED" in {b.code for b in held.blockers}
    record = env.service.store.get("SYN-CASE-001")
    assert not record.archived and {"SYN-REQ-102", "SYN-REQ-201", "SYN-REQ-202"} <= set(record.items)
    approved = env.run(bravo_round(), idempotency_key="bravo-1")               # approval arrives: now the change is applied
    assert {d.request_id for d in approved.cancellation_drafts} == {rid("102"), rid("201")}


def test_a_client_change_with_missing_configuration_still_cancels_but_blocks_the_new_plan(env):
    env.run(alpha_round(), idempotency_key="alpha-1")
    env.cases.set_client("SYN-CASE-001", "client_bravo")
    r = env.run(bravo_round(config={"client_site_rules": {"state": "MISSING"}}), idempotency_key="bravo-1")
    assert r.overall_status == CaseStatus.BLOCKED and "CONFIG_MISSING:client_site_rules" in {b.code for b in r.blockers}
    assert {d.request_id for d in r.cancellation_drafts} == {rid("102"), rid("201")}


def test_a_client_change_that_the_parent_case_does_not_reflect_is_refused_and_changes_nothing(env):
    from sub_agent.service import CaseMismatchError
    env.run(alpha_round(), idempotency_key="alpha-1")
    with pytest.raises(CaseMismatchError):
        env.run(bravo_round(), idempotency_key="bravo-1")                      # the parent registry still says client_alpha
    record = env.service.store.get("SYN-CASE-001")
    assert not record.archived and "SYN-REQ-201" in record.items


def test_repeating_the_call_does_not_duplicate_cancellations_or_drafts(moved):
    env, first = moved
    same = env.run(env.bravo, idempotency_key="bravo-1")                      # exact replay
    assert same == first and len(env.service.store.get("SYN-CASE-001").archived) == 3
    again = env.run(env.bravo, idempotency_key="bravo-2")                     # new key: the change was already applied
    assert again.cancellation_drafts == [] and not again.reassessment.required
    assert {i.request_key for i in again.items if i.superseded} == {"SYN-REQ-102", "SYN-REQ-201", "SYN-REQ-202"}
    assert all(i.state == ItemState.CANCELLED for i in again.items if i.superseded)


def test_a_role_change_alone_does_not_cancel_client_site_requests(env):
    env.run(alpha_round(), idempotency_key="alpha-1")
    r = env.run(make(requested_access=[EMAIL, SRC_SITE], requested_equipment=[LAPTOP_SITE, HEADSET_SITE], client_site_required=True,
                     config={"client_site_rules": SITE}, form_version="v2", role_id="data_engineer",
                     mock_it_ticket_updates=[ticket("101", "CREATED"), ticket("102", "CREATED"), ticket("201", "CREATED")]),
                idempotency_key="alpha-2")
    assert r.reassessment.required and "role_id" in r.reassessment.changed_fields
    assert not r.cancellation_drafts and not [i for i in r.items if i.superseded]


def test_a_failure_while_applying_the_change_leaves_nothing_half_cancelled(env, monkeypatch):
    env.run(alpha_round(), idempotency_key="alpha-1")
    env.cases.set_client("SYN-CASE-001", "client_bravo")
    monkeypatch.setattr("sub_agent.service.assert_clean", lambda *a, **k: (_ for _ in ()).throw(PrivacyViolation("forced")))
    with pytest.raises(PrivacyViolation):
        env.run(bravo_round(), idempotency_key="bravo-1")
    record = env.service.store.get("SYN-CASE-001")
    assert not record.archived and record.items["SYN-REQ-201"].state == ItemState.MOCK_TICKET_CREATED
    monkeypatch.undo()
    retry = env.run(bravo_round(), idempotency_key="bravo-1")                 # the retry still produces the cancellation drafts
    assert {d.request_id for d in retry.cancellation_drafts} == {rid("102"), rid("201")}


def test_the_same_story_gives_the_same_decision_every_time():
    def run():
        e = Env()
        e.run(alpha_round(), idempotency_key="alpha-1")
        e.cases.set_client("SYN-CASE-001", "client_bravo")
        return e.run(bravo_round(), idempotency_key="bravo-1").business_decision()
    assert run() == run()


def test_an_approval_given_under_the_old_client_is_not_reused_for_a_new_request(env):
    env.run(alpha_round(), idempotency_key="alpha-1")
    env.cases.set_client("SYN-CASE-001", "client_bravo")
    stale = approval("401", site=True, client="client_alpha", role="line_manager", item="vpn-access")   # right role, wrong client
    r = env.run(bravo_round(approval_records=[stale]), idempotency_key="bravo-1")
    assert [x.reason_code for x in r.rejected_approvals if x.request_id == rid("401")] == ["STALE_SCOPE"]
    assert by_key(r, "401").state == ItemState.APPROVAL_PENDING
