"""A UC05 approval that only one person may decide -- the team introduction (the team lead on record) -- carries that
person (`approver_id`), so the inbox and the case view can say who decides instead of letting a click fail.

UC05 still enforces it: anyone else is refused (UnauthorizedApproverError). The approvals inbox shows a team lead or
buddy only the approvals naming them, and they list no cases; HR sees every approval with the reason it is not theirs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from _lifecycle import BUDDY_CANDIDATE, CLIENT, CLIENT_CONFIG, HR, TEAM_LEAD, TEAM_LEAD_ID, World, start_uc05_worker, status
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import run_workflow_demo as demo  # noqa: E402
from hr_onboarding_common.orchestration.api import create_orchestrator_app  # noqa: E402
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state  # noqa: E402
from hr_onboarding_common.platform.identity import RegistryIdentityProvider  # noqa: E402

S = Stage


@pytest.fixture
def world():
    worker = start_uc05_worker()
    w = World(worker)
    w.hook("reset")
    w.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    w.hook("add_client_config", config=CLIENT_CONFIG)
    yield w
    worker.stop()


def test_the_team_introduction_names_the_team_lead_on_record_and_only_they_decide_it(world):
    state = world.to_team_intro_pending()
    (approval,) = [p for p in state["pending_approvals"] if p.get("stage") == S.TEAM_INTRODUCTION.value]
    assert approval["role"] == "team_lead" and approval["approver_id"] == TEAM_LEAD_ID
    assert TEAM_LEAD_ID in approval["description"]

    with pytest.raises(Exception, match="team_lead on record"):  # HR is refused by UC05 itself
        world.decide(state, S.TEAM_INTRODUCTION, actor=HR)

    state = world.decide(state, S.TEAM_INTRODUCTION, actor=TEAM_LEAD)
    assert status(state, S.TEAM_INTRODUCTION) == StageStatus.COMPLETE.value


REGISTRY = {
    "user:hr-admin-01": {"label": "HR", "roles": ["hr_admin", "hr_onboarding_officer"], "clients": [CLIENT]},
    TEAM_LEAD_ID: {"label": "Team lead on record", "roles": ["team_lead"], "clients": [CLIENT]},
    "TL-OTHER-002": {"label": "Another team lead", "roles": ["team_lead"], "clients": [CLIENT]},
}


def _inbox(client, user):
    response = client.get("/v1/orchestration/approvals", headers={"X-User-Id": user, "X-Client-Id": CLIENT})
    assert response.status_code == 200, response.text
    return {i["approval_id"]: i for i in response.json()["approvals"]}


def test_the_inbox_shows_each_person_what_they_decide_and_why_not_otherwise():
    agent = demo.build_synthetic_agent(None)
    state = new_state(case_id="CASE-PERSON-1", client_id=CLIENT, correlation_id="corr",
                      actor={"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": CLIENT},
                      intake_trigger={})
    state["pending_approvals"] = [
        {"agent": "employee-onboarding", "reference": "apr-ti", "role": "team_lead", "stage": "team_introduction",
         "description": "UC05 team_intro booking", "approver_id": TEAM_LEAD_ID},
        {"agent": "employee-onboarding", "reference": "apr-bud", "role": "hr_admin", "stage": "buddy_assignment",
         "description": "UC05 buddy proposal"},
    ]
    agent.deps.case_states["CASE-PERSON-1"] = state
    client = TestClient(create_orchestrator_app(agent=agent, identity_provider=RegistryIdentityProvider(REGISTRY)))

    hr = _inbox(client, "user:hr-admin-01")
    assert set(hr) == {"apr-ti", "apr-bud"}
    assert hr["apr-ti"]["can_decide"] is False and TEAM_LEAD_ID in hr["apr-ti"]["reason"]
    assert hr["apr-ti"]["approver_id"] == TEAM_LEAD_ID and hr["apr-bud"]["can_decide"] is True

    lead = _inbox(client, TEAM_LEAD_ID)
    assert set(lead) == {"apr-ti"} and lead["apr-ti"]["can_decide"] is True  # only what names them
    assert _inbox(client, "TL-OTHER-002") == {}  # another team lead sees nothing of this case

    # A team lead lists no cases and reads no case.
    headers = {"X-User-Id": TEAM_LEAD_ID, "X-Client-Id": CLIENT}
    assert client.get("/v1/orchestration/cases", headers=headers).status_code == 403
    assert client.get("/v1/orchestration/cases/CASE-PERSON-1", headers=headers).status_code == 403


def test_an_approval_raised_before_the_person_was_recorded_still_names_them_only_when_the_case_says_so():
    from hr_onboarding_common.orchestration.adapters.uc05_orientation import approver_on_record

    state = new_state(case_id="CASE-OLD-1", client_id=CLIENT, correlation_id="corr", actor={"on_behalf_of": "u"},
                      intake_trigger={}, uc02_role_context={"manager_id": TEAM_LEAD_ID})
    pending = {"agent": "employee-onboarding", "reference": "apr-ti", "role": "team_lead",
               "stage": "team_introduction"}
    state["orientation_schedule_result"] = {"uc05_case": True, "offer_event": {"event_id": "CASE-OLD-1"}}
    assert approver_on_record(state, pending) is None  # not known to be case-backed: no guess

    state["joiner_contact"] = {"email": "x@example.com"}  # UC05 read the joiner from this case (orchestrator_case)
    assert approver_on_record(state, pending) == TEAM_LEAD_ID

    state["orientation_schedule_result"]["team_lead_id"] = "TL-FROM-UC05"  # what UC05 reported wins
    assert approver_on_record(state, pending) == "TL-FROM-UC05"
    assert approver_on_record(state, {**pending, "approver_id": "TL-ON-APPROVAL"}) == "TL-ON-APPROVAL"
    assert approver_on_record(state, {**pending, "role": "hr_admin"}) is None  # role-wide: anyone with the role
