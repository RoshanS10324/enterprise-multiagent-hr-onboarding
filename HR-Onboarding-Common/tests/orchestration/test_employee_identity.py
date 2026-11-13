"""Gap 2 -- the canonical employee identifier. Pure tests of the tenant-scoped resolution
(tenant_config.TenantConfig.resolve_employee), and the real UC05 worker driven through a whole
lifecycle where UC01's employee_no is NOT the HRMS id, to show every UC05 operation uses the
resolved one."""

from __future__ import annotations

import pytest
from _lifecycle import (
    BUDDY_CANDIDATE,
    CLIENT,
    EMPLOYEE,
    HR,
    HR_2,
    JOINING,
    SCHEDULER,
    TEAM_LEAD,
    FakeUc01Worker,
    World,
    start_uc05_worker,
    status,
)

from hr_onboarding_common.orchestration.state import Stage, StageStatus
from hr_onboarding_common.orchestration.tenant_config import (
    EmployeeIdentityError,
    TenantConfig,
)

MAPPED = TenantConfig.model_validate({"tenants": {
    "CLIENT-TEST-A": {"employee_identity": {"scheme": "mapped", "employee_no_map": [
        {"employee_no": "E-900", "employee_id": "EMP-TEST-001"},
        {"employee_no": "E-DUP", "employee_id": "EMP-TEST-002"},
        {"employee_no": "E-DUP", "employee_id": "EMP-TEST-003"},
        {"employee_no": "E-TWIN-1", "employee_id": "EMP-TEST-004"},
        {"employee_no": "E-TWIN-2", "employee_id": "EMP-TEST-004"}]}},
    "CLIENT-TEST-B": {"employee_identity": {"scheme": "mapped", "employee_no_map": [
        {"employee_no": "E-900", "employee_id": "EMP-TEST-010"}]}},
    "CLIENT-NONE": {"employee_identity": {"scheme": "none"}},
}})


def _resolve(config=MAPPED, client_id=CLIENT, employee_no=None, employee_id=None):
    return config.resolve_employee(client_id=client_id, employee_no=employee_no, employee_id=employee_id)


def _code(**kwargs):
    with pytest.raises(EmployeeIdentityError) as err:
        _resolve(**kwargs)
    return err.value.code


def test_employee_no_is_mapped_to_the_canonical_id_of_its_own_tenant():
    ref = _resolve(employee_no="E-900")
    assert (ref.employee_id, ref.employee_no, ref.source) == ("EMP-TEST-001", "E-900", "mapped")
    # the same employee_no in another tenant resolves through THAT tenant's table
    assert _resolve(client_id="CLIENT-TEST-B", employee_no="E-900").employee_id == "EMP-TEST-010"


def test_shared_scheme_is_the_default_and_records_its_source():
    ref = TenantConfig().resolve_employee(client_id="ANY", employee_no="EMP-TEST-001", employee_id=None)
    assert (ref.employee_id, ref.source, ref.scheme) == ("EMP-TEST-001", "shared_scheme", "shared")


def test_a_caller_supplying_employee_id_keeps_working():
    ref = _resolve(employee_id="EMP-TEST-001")
    assert (ref.employee_id, ref.source) == ("EMP-TEST-001", "explicit")
    assert _resolve(employee_no="E-900", employee_id="EMP-TEST-001").source == "explicit"  # agrees with the map
    assert _resolve(client_id="CLIENT-NONE", employee_id="EMP-X").employee_id == "EMP-X"


@pytest.mark.parametrize("kwargs, expected", [
    ({}, "EMPLOYEE_ID_MISSING"),
    ({"employee_no": "E-UNLISTED"}, "EMPLOYEE_ID_UNRESOLVED"),
    ({"client_id": "CLIENT-NONE", "employee_no": "EMP-TEST-001"}, "EMPLOYEE_ID_UNRESOLVED"),
    ({"employee_no": "E 900; DROP"}, "EMPLOYEE_ID_MALFORMED"),
    ({"employee_id": "../etc/passwd"}, "EMPLOYEE_ID_MALFORMED"),
    ({"employee_no": "E-DUP"}, "EMPLOYEE_ID_AMBIGUOUS"),  # one employee_no, two ids
    ({"employee_no": "E-TWIN-1"}, "EMPLOYEE_ID_AMBIGUOUS"),  # one id, two employee_nos
    ({"employee_no": "E-900", "employee_id": "EMP-TEST-002"}, "EMPLOYEE_ID_CONFLICT"),
])
def test_missing_malformed_ambiguous_and_conflicting_identifiers_are_refused(kwargs, expected):
    assert _code(**kwargs) == expected


def test_conflict_under_the_shared_scheme_too():
    with pytest.raises(EmployeeIdentityError) as err:
        TenantConfig().resolve_employee(client_id="ANY", employee_no="EMP-1", employee_id="EMP-2")
    assert err.value.code == "EMPLOYEE_ID_CONFLICT"


def test_unknown_timezone_in_configuration_is_refused():
    with pytest.raises(ValueError):
        TenantConfig.model_validate({"tenants": {"X": {"timezone": "Mars/Olympus"}}})


# ------------------------------------------------------------------ the real UC05 worker


@pytest.fixture(scope="module")
def uc05():
    worker = start_uc05_worker()
    yield worker
    worker.stop()


def test_every_uc05_operation_uses_the_mapped_identity_end_to_end(uc05):
    world = World(uc05, uc01=FakeUc01Worker(employee_id="E-900"), tenants=MAPPED)  # UC01 reports E-900
    world.hook("reset")
    world.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    state = world.start()
    assert state["employee_id"] == "E-900"
    assert state["employee_ref"] == {"client_id": CLIENT, "employee_id": EMPLOYEE, "employee_no": "E-900",
                                     "source": "mapped", "scheme": "mapped"}
    state = world.decide(state, Stage.ORIENTATION_SCHEDULING)
    state = world.decide(state, Stage.BUDDY_ASSIGNMENT)
    state = world.decide(state, Stage.TEAM_INTRODUCTION, actor=TEAM_LEAD)
    state = world.agent.decide_approval(state["case_id"], world.gate_item(state["case_id"], "day_one_signoff"),
                                        decision="approved", actor=HR_2)
    state = world.agent.trigger_joining_day(state["case_id"], as_of=JOINING, actor=SCHEDULER)
    state = world.decide(state, Stage.JOINING_DAY_WELCOME)
    assert state["overall_status"] == "complete"

    snap = world.uc05_snapshot()  # everything UC05 did is about EMP-TEST-001
    assert {a["subject_id"] for a in snap["approvals"]} == {EMPLOYEE}
    assert len(snap["sessions"]) == 2 and snap["buddy_id"] == BUDDY_CANDIDATE["employee_id"]
    assert all(f":{EMPLOYEE}:" in o["key"] or not o["key"].startswith("calendar:") for o in snap["operations"])
    from hr_onboarding_common.orchestration.contracts import Actor
    unknown = uc05.call("case_snapshot", {"employee_id": "E-900"},
                        actor=Actor(on_behalf_of="t", roles=["hr_admin"], client_id=CLIENT))
    assert unknown.ok and unknown.result["found"] is False  # UC05 never saw the upstream id


def test_a_mapping_changed_mid_lifecycle_never_switches_uc05_to_another_employee(uc05):
    world = World(uc05, uc01=FakeUc01Worker(employee_id="E-900"), tenants=MAPPED)
    world.hook("reset")
    world.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    state = world.start()
    assert state["employee_ref"]["employee_id"] == EMPLOYEE
    world.deps.tenants = TenantConfig.model_validate({"tenants": {"CLIENT-TEST-A": {"employee_identity": {
        "scheme": "mapped", "employee_no_map": [{"employee_no": "E-900", "employee_id": "EMP-TEST-002"}]}}}})

    state = world.decide(state, Stage.ORIENTATION_SCHEDULING)  # UC05 holds a case for EMP-TEST-001: kept

    assert state["employee_ref"]["employee_id"] == EMPLOYEE
    assert status(state, Stage.BUDDY_ASSIGNMENT) == StageStatus.AWAITING_APPROVAL.value
    assert {a["subject_id"] for a in world.uc05_snapshot()["approvals"]} == {EMPLOYEE}


def test_an_unresolvable_identity_blocks_before_uc05_is_touched(uc05):
    world = World(uc05, uc01=FakeUc01Worker(employee_id="E-UNLISTED"), tenants=MAPPED)
    world.hook("reset")
    state = world.start()
    assert status(state, Stage.ORIENTATION_SCHEDULING) == StageStatus.BLOCKED.value
    assert state["stage_errors"][Stage.ORIENTATION_SCHEDULING.value]["code"] == "EMPLOYEE_ID_UNRESOLVED"
    assert world.hook("counts")["calendar_live"] == 0 and world.uc05_snapshot()["found"] is False


def test_conflicting_supplied_employee_id_blocks(uc05):
    world = World(uc05, uc01=FakeUc01Worker(employee_id="E-900"), tenants=MAPPED)
    world.hook("reset")
    state = world.start(employee_id="EMP-TEST-002")
    assert state["stage_errors"][Stage.ORIENTATION_SCHEDULING.value]["code"] == "EMPLOYEE_ID_CONFLICT"
    assert world.hook("counts")["calendar_live"] == 0


def test_a_duplicate_employee_no_in_another_tenant_never_resolves_to_this_tenants_employee(uc05):
    """CLIENT-TEST-B maps the same E-900 to EMP-TEST-010; a TEST-B case can never reach EMP-TEST-001
    (and UC05's directory refuses TEST-A's employee to a TEST-B actor anyway)."""
    world = World(uc05, uc01=FakeUc01Worker(employee_id="E-900"), tenants=MAPPED)
    world.hook("reset")
    hr_b = {**HR, "client_id": "CLIENT-TEST-B"}
    state = world.agent.advance_case(world.case_input(client_id="CLIENT-TEST-B", offer_reference="OFR-B-1",
                                                      offer_acceptance_event_id="EVT-001"),
                                     case_id=None, actor=hr_b)
    state = world.supply_documents(state, actor=hr_b)  # UC05 runs once the documents are verified
    assert state["employee_ref"]["employee_id"] == "EMP-TEST-010"
    # the offer event is TEST-A's: UC05's tenant-filtered directory does not show it to TEST-B
    assert state["stage_errors"][Stage.ORIENTATION_SCHEDULING.value]["code"] == "EMPLOYEE_NOT_IN_UC05_DIRECTORY"
    assert world.hook("counts")["calendar_live"] == 0
