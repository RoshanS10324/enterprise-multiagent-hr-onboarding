"""Control Center data presentation: values UC05 and UC04 hold reach the case record and the API intact.

Real UC05 worker for the UC05 flow. The approvals inbox and employee numbers are tested at the API, with privacy and
tenant isolation. Nothing absent is filled in: a missing value stays missing.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest
from _lifecycle import BUDDY_CANDIDATE, BUDDY_ID, CLIENT, CLIENT_CONFIG, EMPLOYEE, TEAM_LEAD, TEAM_LEAD_ID, World
from _lifecycle import start_uc05_worker
from fastapi.testclient import TestClient

from hr_onboarding_common.orchestration.adapters._shared import add_pending_approval
from hr_onboarding_common.orchestration.state import Stage, new_state

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import run_workflow_demo as demo  # noqa: E402
from hr_onboarding_common.orchestration.api import case_summary, case_view, create_orchestrator_app  # noqa: E402
from hr_onboarding_common.platform.identity import RegistryIdentityProvider  # noqa: E402

S = Stage


@pytest.fixture(scope="module")
def uc05():
    worker = start_uc05_worker()
    yield worker
    worker.stop()


@pytest.fixture
def world(uc05):
    w = World(uc05)
    w.hook("reset")
    w.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    w.hook("add_client_config", config=CLIENT_CONFIG)
    return w


def test_orientation_start_end_and_timezone_survive_the_whole_flow(world):
    state = world.to_buddy_pending()  # induction confirmed
    o = case_view(world.agent.get_case(state["case_id"], client_id=CLIENT)).model_dump()["orientation"]
    start, end = datetime.fromisoformat(o["start_utc"].replace("Z", "+00:00")), \
        datetime.fromisoformat(o["end_utc"].replace("Z", "+00:00"))
    assert end > start and o["start_utc"].endswith("Z")  # both from UC05; the end is not reconstructed
    assert o["session_type"] == "induction" and o["session_status"] == "confirmed" and o["timezone"]
    assert o["calendar_event_id"].startswith("sim-evt")  # the simulated calendar's own reference
    # The full schedule carries link and location; UC05 holds neither, so both are None -- never invented.
    assert o.get("meeting_link") is None and o.get("location") is None and "room_location" not in o
    roles = {p["participant_id"]: p["role"] for p in o["participants"]}
    assert roles[EMPLOYEE] == "joiner"


def test_buddy_name_department_and_status_survive_the_whole_flow(world):
    state = world.to_team_intro_pending()  # buddy approved
    b = case_view(world.agent.get_case(state["case_id"], client_id=CLIENT)).model_dump()["buddy_assignment"]
    assert (b["buddy_id"], b["status"]) == (BUDDY_ID, "assigned")
    assert (b["buddy_name"], b["buddy_department"]) == (BUDDY_CANDIDATE["full_name"], BUDDY_CANDIDATE["team"])
    assert b["buddy_profile_valid"] is True and b["buddy_location"] is None  # UC05's candidates hold no location


def test_team_introduction_participants_and_their_invitations_and_replies_survive(world):
    state = world.to_team_intro_pending()
    ti = world.agent.get_case(state["case_id"], client_id=CLIENT)["team_introduction_result"]
    by_role = {p["role"]: p for p in ti["participants"]}
    assert by_role["joiner"]["participant_id"] == EMPLOYEE and by_role["team_lead"]["participant_id"] == TEAM_LEAD_ID
    assert all(p["invitation_status"] in ("pending", "sent") for p in ti["participants"])
    assert ti["start_utc"] and ti["end_utc"] and ti["session_type"] == "team_intro"

    state = world.decide(state, S.TEAM_INTRODUCTION, actor=TEAM_LEAD)  # confirmed: UC05 invites
    ti = world.agent.get_case(state["case_id"], client_id=CLIENT)["team_introduction_result"]
    assert {p["invitation_status"] for p in ti["participants"]} == {"sent"}
    # A reply UC05 records reaches its snapshot (the UI shows it next to the stored participants).
    world.agent.record_participant_response(state["case_id"], session_id=ti["session_id"], participant_id=TEAM_LEAD_ID,
                                            response="accepted", responded_at=None, actor=TEAM_LEAD)
    session = next(s for s in world.uc05_snapshot()["sessions"] if s["session_id"] == ti["session_id"])
    assert {"participant_id": TEAM_LEAD_ID, "response": "accepted"}.items() <= next(
        r for r in session["responses"] if r["participant_id"] == TEAM_LEAD_ID).items()


# ---------------------------------------------------------------------------------------------------- inbox / numbers
REGISTRY = {
    "user:hr-admin-01": {"label": "HR", "roles": ["hr_admin"], "clients": ["CLIENT-ALPHA", "CLIENT-OTHER"]},
    "user:hiring-manager-01": {"label": "HM", "roles": ["hiring_manager"], "clients": ["CLIENT-ALPHA"]},
}


def _case(agent, case_id, client="CLIENT-ALPHA"):
    state = new_state(case_id=case_id, client_id=client, correlation_id="c", actor={"on_behalf_of": "user:hr-admin-01"},
                      intake_trigger={"candidate_name": "Suvedha", "designation": "Software Developer",
                                      "employee_id": "EMP008"},
                      uc02_role_context={"department": "IT"})
    state["employee_id"], state["joining_date"] = "EMP-000001", "2026-11-24"
    state["employee_ref"] = {"employee_id": "EMP008", "employee_no": "EMP-000001"}
    state["equipment_access_requests"] = {"recorded_by": "user:hr-admin-01"}
    state["equipment_access_result"] = {"items": [
        {"request_id": f"{case_id}:EQ-laptop-standard", "item_id": "laptop-standard", "state": "APPROVAL_PENDING"},
        {"request_id": f"{case_id}:AC-email-teams-account", "item_id": "email-teams-account",
         "state": "APPROVAL_PENDING"}]}
    for item in state["equipment_access_result"]["items"]:
        add_pending_approval(state, agent="dact-uc04-equipment-access", reference=f"{item['request_id']}:STANDARD",
                             role="hiring_manager", description=f"UC04 standard approval: item {item['item_id']}",
                             stage=Stage.EQUIPMENT_ACCESS, proposal_type="uc04_item_approval",
                             record_id=item["request_id"])
    agent.deps.case_states[case_id] = state
    return state


@pytest.fixture
def client():
    agent = demo.build_synthetic_agent(None)
    _case(agent, "CASE-ALPHA-1")
    _case(agent, "CASE-OTHER-1", client="CLIENT-OTHER")  # another tenant's case
    return TestClient(create_orchestrator_app(agent=agent, identity_provider=RegistryIdentityProvider(REGISTRY)))


def _inbox(client, user, tenant="CLIENT-ALPHA"):
    r = client.get("/v1/orchestration/approvals", headers={"X-User-Id": user, "X-Client-Id": tenant})
    assert r.status_code == 200, r.text
    return r.json()["approvals"]


def test_uc04_item_approvals_are_distinguishable_and_say_what_they_approve(client):
    items = _inbox(client, "user:hr-admin-01")
    assert {i["case_id"] for i in items} == {"CASE-ALPHA-1"}  # tenant isolation: CLIENT-OTHER's case not listed
    requests = {i["request"]["item_id"]: i["request"] for i in items}
    assert set(requests) == {"laptop-standard", "email-teams-account"}
    assert requests["laptop-standard"]["item_kind"] == "EQUIPMENT"
    assert requests["email-teams-account"]["item_kind"] == "ACCESS"
    assert all(r["approval_type"] == "STANDARD" for r in requests.values())
    assert all(i["description"] and i["raised_by"] == "UC04 Equipment & Access" and i["requested_at"] for i in items)


def test_both_employee_numbers_are_named_and_privacy_masking_still_holds(client):
    (hr, *_) = _inbox(client, "user:hr-admin-01")
    assert (hr["hrms_employee_no"], hr["uc01_employee_no"]) == ("EMP008", "EMP-000001")
    assert hr["employee_name"] == "Suvedha" and hr["requested_by"] == "user:hr-admin-01"
    (hm, *_) = _inbox(client, "user:hiring-manager-01")
    assert hm["employee_name"] is None and hm["requested_by"] is None  # not an identity reader: still masked
    assert hm["hrms_employee_no"] == "EMP008" and hm["request"]["item_id"]  # but what it decides is identifiable
    other = _inbox(client, "user:hr-admin-01", tenant="CLIENT-OTHER")
    assert {i["case_id"] for i in other} == {"CASE-OTHER-1"}


def test_case_views_name_both_numbers_and_never_invent_one():
    state = new_state(case_id="C1", client_id="CLIENT-ALPHA", correlation_id="c", actor={}, intake_trigger={})
    state["employee_id"] = "EMP-000001"
    summary, view = case_summary(state, include_identity=False), case_view(state)
    assert (summary.hrms_employee_no, summary.uc01_employee_no) == (None, "EMP-000001")  # not resolved: stays None
    assert (view.hrms_employee_no, view.uc01_employee_no) == (None, "EMP-000001")
    state["employee_ref"] = {"employee_id": "EMP008", "employee_no": "EMP-000001"}
    assert case_view(state).hrms_employee_no == "EMP008" and case_view(state).employee_id == "EMP-000001"


def test_hr_refreshes_details_of_results_recorded_before_uc05_reported_them_without_changing_any_status(world):
    import copy as _copy

    state = world.to_team_intro_pending()
    case_id = state["case_id"]
    old = world.agent.get_case(case_id, client_id=CLIENT)
    for key, fields in (("orientation_schedule_result", ("start_utc", "end_utc", "participants", "session_type",
                                                         "calendar_event_id", "timezone")),
                        ("buddy_assignment_result", ("buddy_name", "buddy_department", "buddy_location",
                                                     "buddy_profile_found", "buddy_profile_valid",
                                                     "buddy_profile_source"))):
        for f in fields:
            old[key].pop(f, None)  # as recorded before this change
    world.deps.case_states[case_id] = old
    before = _copy.deepcopy(old)

    after, outcome = world.agent.refresh_uc05_details(case_id, actor={"on_behalf_of": "user:hr-admin-01",
                                                                       "roles": ["hr_admin"], "client_id": CLIENT})
    assert set(outcome["refreshed"]) >= {"orientation_schedule_result", "buddy_assignment_result"}
    assert after["orientation_schedule_result"]["end_utc"] and after["orientation_schedule_result"]["participants"]
    assert after["buddy_assignment_result"]["buddy_name"] == BUDDY_CANDIDATE["full_name"]
    for key in ("stage_statuses", "pending_approvals", "approval_decisions", "blockers"):
        assert after[key] == before[key], key  # display details only
