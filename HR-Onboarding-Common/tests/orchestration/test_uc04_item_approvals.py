"""UC04 item approvals decided in the Control Center.

UC04 holds each requested item for the approver its configuration names (hiring_manager for CLIENT-ALPHA) and takes
approvals as approval records -- input facts from the platform's approval system -- which it validates itself
(approver role, request, item, client/role/site scope, not future-dated). The orchestrator is that approval system: each
held item is a pending approval for that role; a person's decision becomes an approval record and UC04 re-evaluates.
UC04's ADVICE (owner codes such as HR_OFFICER: a loan device, another joining date) is kept as advice, never an approval.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from _helpers import UC_ROOTS, require_uc, venv_python
from test_uc04_client_role_config import _deps, _Pool, _state

from hr_onboarding_common.orchestration.adapters._shared import OrchestratorAuthorizationError
from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import (UC04_ITEM_APPROVAL,
                                                                               call_uc04_equipment_access,
                                                                               uc04_approval_record)
from hr_onboarding_common.orchestration.state import Stage, StageStatus

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import run_workflow_demo as demo  # noqa: E402
from hr_onboarding_common.orchestration.api import create_orchestrator_app  # noqa: E402
from hr_onboarding_common.orchestration.orchestrator_agent import WorkflowRequestError  # noqa: E402
from hr_onboarding_common.platform.identity import RegistryIdentityProvider  # noqa: E402

HIRING_MANAGER = "user:hiring-manager-01"


@pytest.fixture
def platform_uc04(monkeypatch):
    """The real UC04 worker with its PLATFORM identity profile (the platform's uuid case ids)."""
    require_uc("uc04")
    from hr_onboarding_common.orchestration.workers.config import WorkerConfig
    from hr_onboarding_common.orchestration.workers.uc_worker_client import UcWorker

    monkeypatch.setenv("UC04_IDENTITY_PROFILE", "PLATFORM")
    worker = UcWorker(WorkerConfig(uc_id="uc04", python_executable=venv_python(UC_ROOTS["uc04"]),
                                   src_dir=UC_ROOTS["uc04"] / "src"))
    worker.start()
    yield worker
    worker.stop()


def _awaiting(platform_uc04):
    state = _state("Developer")
    state["joining_date"] = "2026-11-15"
    state["equipment_access_requests"] = {"recorded_by": "user:hr-admin-01"}
    state["uc02_role_context"].update(requested_equipment=["laptop-standard", "headset-basic"],
                                      requested_access=["email-teams-account"])
    return call_uc04_equipment_access(state, _deps(_Pool(platform_uc04)))


def test_each_held_item_is_an_approval_for_the_configured_role_and_advice_is_not(platform_uc04):
    result = _awaiting(platform_uc04)
    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.AWAITING_APPROVAL.value
    approvals = [p for p in result["pending_approvals"] if p.get("proposal_type") == UC04_ITEM_APPROVAL]
    assert {p["role"] for p in approvals} == {"hiring_manager"} and len(approvals) == 3
    assert all(p["reference"].endswith(":STANDARD") and p["stage"] == "equipment_access" for p in approvals)
    assert not [p for p in result["pending_approvals"] if p["role"].isupper()]  # no HR_OFFICER "approvals"
    for advice in result["equipment_access_result"]["advisories"]:
        assert advice["owner"].isupper() and advice["rationale"]


def test_real_uc04_accepts_the_recorded_approvals_and_the_stage_completes(platform_uc04):
    result = _awaiting(platform_uc04)
    entries = [p for p in result["pending_approvals"] if p.get("proposal_type") == UC04_ITEM_APPROVAL]
    approved = copy.deepcopy(result)
    approved["uc02_role_context"]["approval_records"] = [
        uc04_approval_record(result, p, decision="approved", approver_id=HIRING_MANAGER) for p in entries]
    approved["blockers"], approved["pending_approvals"] = [], []
    after = call_uc04_equipment_access(approved, _deps(_Pool(platform_uc04)))
    items = after["equipment_access_result"]["items"]
    assert items and not [i for i in items if i["state"] in ("APPROVAL_PENDING", "EXCEPTION_PENDING")], items
    assert after["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.COMPLETE.value, after["blockers"]

    rejected = copy.deepcopy(result)
    rejected["uc02_role_context"]["approval_records"] = [
        uc04_approval_record(result, entries[0], decision="rejected", approver_id=HIRING_MANAGER)]
    rejected["blockers"], rejected["pending_approvals"] = [], []
    after = call_uc04_equipment_access(rejected, _deps(_Pool(platform_uc04)))
    assert after["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] != StageStatus.COMPLETE.value  # never auto-approved


def _case_with_pending_item(agent):
    state = _state("Developer")
    state["equipment_access_requests"] = {"recorded_by": "user:hr-admin-01"}
    state["equipment_access_result"] = {"role_code": "developer", "items": [
        {"request_id": f"{state['case_id']}:EQ-laptop-standard", "item_id": "laptop-standard",
         "state": "APPROVAL_PENDING"}]}
    entry = {"agent": "dact-uc04-equipment-access", "reference": f"{state['case_id']}:EQ-laptop-standard:STANDARD",
             "role": "hiring_manager", "stage": "equipment_access", "description": "UC04 standard approval",
             "proposal_type": UC04_ITEM_APPROVAL, "record_id": f"{state['case_id']}:EQ-laptop-standard"}
    state["pending_approvals"] = [entry]
    state["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] = StageStatus.AWAITING_APPROVAL.value
    agent.deps.case_states[state["case_id"]] = state
    return state, entry["reference"]


def _actor(user, roles):
    return {"on_behalf_of": user, "roles": roles, "client_id": "CLIENT-ALPHA"}


def test_the_decision_is_recorded_for_uc04_only_from_the_configured_role_and_not_the_requester(monkeypatch):
    agent = demo.build_synthetic_agent(None)
    monkeypatch.setattr(agent, "_invoke", lambda state, actor: state)  # the re-evaluation itself is tested above
    state, ref = _case_with_pending_item(agent)
    case_id = state["case_id"]

    with pytest.raises(OrchestratorAuthorizationError, match="hiring_manager"):
        agent.decide_approval(case_id, ref, decision="approved", actor=_actor("user:hr-admin-01", ["hr_admin"]))
    with pytest.raises(OrchestratorAuthorizationError, match="another person"):  # who recorded the requests
        agent.decide_approval(case_id, ref, decision="approved",
                              actor=_actor("user:hr-admin-01", ["hr_admin", "hiring_manager"]))
    with pytest.raises(WorkflowRequestError, match="reason"):
        agent.decide_approval(case_id, ref, decision="rejected", actor=_actor(HIRING_MANAGER, ["hiring_manager"]))

    after = agent.decide_approval(case_id, ref, decision="approved", actor=_actor(HIRING_MANAGER, ["hiring_manager"]))
    (record,) = after["uc02_role_context"]["approval_records"]
    assert (record["approver_id"], record["approver_role"], record["decision"]) == (HIRING_MANAGER, "hiring_manager",
                                                                                  "APPROVED")
    assert (record["item_id"], record["role_id"], record["client_id"], record["approval_type"]) == (
        "laptop-standard", "developer", "CLIENT-ALPHA", "STANDARD")
    assert not after["pending_approvals"]
    assert after["approval_decisions"][-1]["agent"] == "dact-uc04-equipment-access"
    assert after["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.IN_PROGRESS.value  # UC04 re-evaluates


def test_the_hiring_manager_sees_the_items_waiting_for_their_role_in_the_inbox():
    agent = demo.build_synthetic_agent(None)
    _case_with_pending_item(agent)
    registry = {
        "user:hr-admin-01": {"label": "HR", "roles": ["hr_admin"], "clients": ["CLIENT-ALPHA"]},
        HIRING_MANAGER: {"label": "Hiring manager", "roles": ["hiring_manager"], "clients": ["CLIENT-ALPHA"]},
    }
    client = TestClient(create_orchestrator_app(agent=agent, identity_provider=RegistryIdentityProvider(registry)))

    def inbox(user):
        r = client.get("/v1/orchestration/approvals", headers={"X-User-Id": user, "X-Client-Id": "CLIENT-ALPHA"})
        assert r.status_code == 200, r.text
        return r.json()["approvals"]

    (mine,) = inbox(HIRING_MANAGER)
    assert mine["required_role"] == "hiring_manager" and mine["can_decide"] is True
    (hr,) = inbox("user:hr-admin-01")
    assert hr["can_decide"] is False and "hiring_manager" in hr["reason"]
