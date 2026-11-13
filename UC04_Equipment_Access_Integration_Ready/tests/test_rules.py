"""Start gate, allowlists, configuration gaps, client isolation (BR-01..06, BR-20; TC-01..09, 16, 19, 27; EC-19/20/31/52)."""

import pytest

from helpers import ALL, approval, approvals, codes, item, make, rid
from sub_agent.adapters.approval_mock import MockApprovalDesk
from sub_agent.config_state import ConfigBlock
from sub_agent.contracts import Uc04Input
from sub_agent.states import CaseStatus, ConfigState, ItemState, ParentCaseStatus

OUT_OF_MATRIX = {"request_key": "SYN-REQ-103", "item_id": "admin-console-access", "item_kind": "ACCESS",
                 "requested_by": "SYN-USER-201"}
IN_MATRIX = {"request_key": "SYN-REQ-101", "item_id": "email-teams-account", "item_kind": "ACCESS",
             "requested_by": "SYN-USER-201"}


def _no_side_effects(env):
    assert env.desk.calls == [] and env.cases.created == []


# ---- start gate
def test_tc01_offer_not_accepted_blocks_and_prepares_nothing(env):
    r = env.run(make(offer_accepted=False))
    assert r.overall_status == CaseStatus.BLOCKED and r.items == []
    assert codes(r.blockers) == {"START_GATE_OFFER_NOT_ACCEPTED"}
    assert env.service.store.case_ids == [] and env.service.store.all_request_ids() == []
    _no_side_effects(env)


def test_tc02_form_not_approved_blocks_and_prepares_nothing(env):
    r = env.run(make(onboarding_form_approved=False))
    assert r.overall_status == CaseStatus.BLOCKED and r.items == []
    assert codes(r.blockers) == {"START_GATE_FORM_NOT_APPROVED"}
    assert env.service.store.all_request_ids() == []
    _no_side_effects(env)


def test_ec19_both_gates_failed_reports_both(env):
    r = env.run(make(offer_accepted=False, onboarding_form_approved=False))
    assert codes(r.blockers) == {"START_GATE_OFFER_NOT_ACCEPTED", "START_GATE_FORM_NOT_APPROVED"}


def test_ec20_parent_case_on_hold_blocks(env):
    env.cases.set_status("SYN-CASE-001", ParentCaseStatus.ON_HOLD)
    r = env.run(make())
    assert r.overall_status == CaseStatus.BLOCKED and "CASE_ON_HOLD" in codes(r.blockers) and r.items == []
    _no_side_effects(env)


# ---- approvals gate every request
def test_tc03_allowed_items_wait_for_approval_and_are_not_submitted(env):
    r = env.run(make())
    assert r.overall_status == CaseStatus.AWAITING_APPROVAL
    assert {i.state for i in r.items} == {ItemState.APPROVAL_PENDING}
    assert all(i.in_matrix is True and not i.exception_required for i in r.items)
    report = env.submit(make())
    assert report.submitted == [] and env.desk.calls == []


def test_tc04_out_of_matrix_access_is_an_exception_and_never_submitted(env):
    payload = make(requested_access=[IN_MATRIX, OUT_OF_MATRIX], requested_equipment=[],
                   approval_records=[approval("101"), approval("101", request_id=rid("103"), item="admin-console-access")])
    r = env.run(payload)
    out = item(r, "103")
    assert out.in_matrix is False and out.exception_required and out.exception_reason == "ITEM_NOT_IN_ROLE_MATRIX"
    assert out.state == ItemState.EXCEPTION_PENDING  # standard approval is present, exception approval is not
    assert env.submit(payload).submitted == [rid("101")]  # only the allowed, approved item is ever submitted
    assert [c["request_id"] for c in env.desk.calls] == [rid("101")]


def test_tc05_exception_approved_but_standard_missing_stays_pending(env):
    exc_only = approval("103", request_id=rid("103"), item="admin-console-access", role="exception_approver", typ="EXCEPTION")
    payload = make(requested_access=[OUT_OF_MATRIX], requested_equipment=[], approval_records=[exc_only])
    out = item(env.run(payload), "103")
    assert out.state == ItemState.APPROVAL_PENDING and out.exception_state.value == "RECEIVED"
    assert env.submit(payload).submitted == [] and env.desk.calls == []


def test_exception_with_both_approvals_is_eligible_and_names_both_approvers(env):
    std = approval("103", request_id=rid("103"), item="admin-console-access")
    exc = approval("103", request_id=rid("103"), item="admin-console-access", role="exception_approver", typ="EXCEPTION")
    payload = make(requested_access=[OUT_OF_MATRIX], requested_equipment=[], approval_records=[std, exc])
    out = item(env.run(payload), "103")
    assert out.state == ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION
    assert out.required_approver == "hiring_manager + exception_approver"


def test_tc06_all_approvals_make_items_eligible_only_nothing_is_submitted(env):
    payload = make(approval_records=approvals(ALL))
    r = env.run(payload)
    assert {i.state for i in r.items} == {ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION}
    assert r.overall_status == CaseStatus.IN_PROGRESS  # approval alone is never readiness
    assert env.desk.calls == [] and env.desk.tickets == {}
    assert env.desk.real_side_effects is False


def test_mock_approval_desk_issues_records_the_service_accepts(env):
    inp = Uc04Input.model_validate(make())
    desk = MockApprovalDesk()
    from datetime import date
    rec = desk.issue(inp, "SYN-REQ-201", item_id="laptop-standard", approver_role="hiring_manager", decided_on=date(2026, 10, 14))
    payload = make(approval_records=[rec.model_dump(mode="json")])
    assert item(env.run(payload), "201").state == ItemState.ELIGIBLE_FOR_SIMULATED_SUBMISSION


# ---- missing configuration fails closed
@pytest.mark.parametrize("block,kind,code", [
    ("role_access_matrix", "ACCESS", "CONFIG_MISSING:role_access_matrix"),
    ("role_equipment_matrix", "EQUIPMENT", "CONFIG_MISSING:role_equipment_matrix"),
])
def test_tc07_tc08_missing_matrix_blocks_affected_items_no_allow_all(env, block, kind, code):
    payload = make(approval_records=approvals(ALL), config={block: {"state": "MISSING"}})
    r = env.run(payload)
    assert r.overall_status == CaseStatus.BLOCKED and code in r.config_gaps
    affected = [i for i in r.items if i.item_kind.value == kind]
    assert affected and all(i.state == ItemState.BLOCKED and i.in_matrix is None for i in affected)
    env.submit(payload)
    assert not {i.request_id for i in affected} & {c["request_id"] for c in env.desk.calls}


def test_ec52_no_lists_for_role_stops_and_asks_hr(env):
    r = env.run(make(config={"role_access_matrix": {"state": "MISSING"}, "role_equipment_matrix": {"state": "MISSING"}}))
    assert r.overall_status == CaseStatus.BLOCKED
    assert {"CONFIG_MISSING:role_access_matrix", "CONFIG_MISSING:role_equipment_matrix"} <= set(r.config_gaps)
    assert {i.state for i in r.items} == {ItemState.BLOCKED}
    assert all(b.owner == "HR_OFFICER" for b in r.blockers)


def test_verified_empty_matrix_is_not_the_same_as_missing(env):
    r = env.run(make(approval_records=[], config={"role_access_matrix": {"state": "VERIFIED_EMPTY"}}))
    access = [i for i in r.items if i.item_kind.value == "ACCESS"]
    assert all(i.in_matrix is False and i.exception_required for i in access)  # nothing allowed, so every item is an exception
    assert "CONFIG_MISSING:role_access_matrix" not in r.config_gaps


def test_config_block_rejects_inconsistent_state():
    with pytest.raises(ValueError):
        ConfigBlock[list[str]](state=ConfigState.MISSING, value=["x"])
    with pytest.raises(ValueError):
        ConfigBlock[list[str]](state=ConfigState.VERIFIED)
    with pytest.raises(ValueError):
        ConfigBlock[list[str]](state=ConfigState.VERIFIED_EMPTY, value=["x"])


def test_tc09_missing_approver_config_blocks_and_owner_is_explicitly_unresolved(env):
    r = env.run(make(config={"approver_config": {"state": "MISSING"}}))
    assert r.overall_status == CaseStatus.BLOCKED and "CONFIG_MISSING:approver_config" in r.config_gaps
    assert {i.state for i in r.items} == {ItemState.BLOCKED}
    assert {i.required_approver for i in r.items} == {"OWNER_CONFIG_MISSING"}
    assert all(b.owner for b in r.blockers)  # BR-19


def test_missing_approver_class_blocks_only_that_class(env):
    cfg = {"approver_config": {"state": "VERIFIED", "value": {
        "ACCESS": {"role": "hiring_manager", "delegate_role": None},
        "EXCEPTION": {"role": "exception_approver", "delegate_role": None}}}}
    r = env.run(make(config=cfg))
    assert {i.state for i in r.items if i.item_kind.value == "EQUIPMENT"} == {ItemState.BLOCKED}
    assert {i.state for i in r.items if i.item_kind.value == "ACCESS"} == {ItemState.APPROVAL_PENDING}
    assert "CONFIG_MISSING:approver_config.EQUIPMENT" in codes(r.blockers)


@pytest.mark.parametrize("lead_times,expected", [
    ({"state": "MISSING"}, "CONFIG_MISSING:lead_times"),
    ({"state": "VERIFIED", "value": {"EQUIPMENT": -1, "ACCESS": 5, "CLIENT_SITE_CLEARANCE": 15}}, "CONFIG_INVALID:lead_times.EQUIPMENT"),
    ({"state": "VERIFIED", "value": {"EQUIPMENT": 10, "CLIENT_SITE_CLEARANCE": 15}}, "CONFIG_MISSING:lead_times.ACCESS"),
    ({"state": "VERIFIED", "value": {"EQUIPMENT": 10, "ACCESS": 9999, "CLIENT_SITE_CLEARANCE": 15}}, "CONFIG_INVALID:lead_times.ACCESS"),
])
def test_tc27_missing_or_invalid_lead_time_is_a_blocker_never_defaulted(env, lead_times, expected):
    r = env.run(make(config={"lead_times": lead_times}))
    assert r.overall_status == CaseStatus.BLOCKED and expected in r.config_gaps
    assert any(i.due_date is None for i in r.items)  # no invented default date


def test_missing_work_calendar_blocks(env):
    r = env.run(make(config={"work_calendar": {"state": "MISSING"}}))
    assert r.overall_status == CaseStatus.BLOCKED and "CONFIG_MISSING:work_calendar" in r.config_gaps


@pytest.mark.parametrize("mode", ["REMOTE", "OVERSEAS"])
def test_tc16_remote_or_overseas_without_rules_blocks_the_equipment_plan(env, mode):
    r = env.run(make(work_mode=mode))
    assert r.overall_status == CaseStatus.BLOCKED and "CONFIG_MISSING:country_rules" in r.config_gaps
    assert {i.state for i in r.items if i.item_kind.value == "EQUIPMENT"} == {ItemState.BLOCKED}


def test_remote_with_rules_adds_courier_days_to_the_equipment_lead_time(env):
    rules = {"state": "VERIFIED", "value": {"country_code": "ZZ", "courier_extra_days": 3, "remote_first_day_required": True}}
    r = env.run(make(work_mode="OVERSEAS", config={"country_rules": rules}))
    laptop = item(r, "201")
    assert laptop.lead_time_days == 13 and laptop.due_date.isoformat() == "2026-10-28"
    assert laptop.lead_time_source.endswith("+country_rules.courier_extra_days")
    assert r.remote_plan.courier_extra_days == 3 and "No courier" in r.remote_plan.note
    assert env.desk.calls == []  # a plan only: nothing is dispatched


# ---- client isolation
def test_tc19_configuration_of_another_client_is_never_used(env):
    from sub_agent.fixtures import load
    bravo = load("client_bravo_config.json")
    r = env.run(make(config=bravo))  # an alpha case carrying bravo's configuration
    assert r.overall_status == CaseStatus.BLOCKED and r.config_gaps == ["CONFIG_SCOPE_MISMATCH"]
    assert all(i.in_matrix is None for i in r.items)


def test_ac10_same_role_gets_different_plans_under_two_clients(env):
    extra = [{"request_key": "SYN-REQ-301", "item_id": "monitor-27", "item_kind": "EQUIPMENT", "requested_by": "SYN-USER-201"}]
    alpha = env.run(make("alpha", 1, requested_equipment=extra, requested_access=[]))
    bravo = env.run(make("bravo", 2, requested_equipment=extra, requested_access=[]))
    assert item(alpha, "301").exception_required is True and item(bravo, "301", 2).exception_required is False
    assert item(alpha, "301").lead_time_days == 10 and item(bravo, "301", 2).lead_time_days == 7
    assert item(alpha, "301").required_approver.startswith("hiring_manager")
    assert item(bravo, "301", 2).required_approver == "line_manager"
    assert alpha.config_version == "alpha-v1" and bravo.config_version == "bravo-v1"


def test_new_client_needs_only_configuration_not_code(env):
    from sub_agent.adapters.case_context_mock import MockCaseContext
    from sub_agent.fixtures import load
    from sub_agent.ports import ParentCase
    cfg = load("client_alpha_config.json")
    cfg.update(client_id="client_charlie", version="charlie-v1")
    cases = MockCaseContext([ParentCase(case_id="SYN-CASE-010", employee_ref="SYN-EMP-010", client_id="client_charlie")])
    from helpers import Env
    e = Env(cases=cases)
    data = make(n=10, client_id="client_charlie", config=cfg)
    assert e.run(data).overall_status == CaseStatus.AWAITING_APPROVAL
