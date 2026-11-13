"""The internal onboarding form's approval (orchestrator gate `onboarding_form_approval`).

A person submitting the form never approves it. The orchestrator raises an approval request for the role the
policy names (hr_lead: a role UC02 accepts as the form's approver), with separation of duties. UC01
opens the employee record only from an approved form (UC01 start_gate), so UC01 -- and therefore UC02 -- wait
for an authorized decision. The decision's facts (who, which verified role, when) are what UC01 and UC02 receive.

Agent: scripts/run_workflow_demo.build_synthetic_agent (the real graph, gates, HITL inbox and UC03; contract-shaped
stand-ins for UC01/UC02/UC04/UC05). approval_requests: InMemoryApprovalRequests (SupabaseApprovalRequests'
contract)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import run_workflow_demo as demo  # noqa: E402

from hr_onboarding_common.orchestration.api import create_orchestrator_app  # noqa: E402
from hr_onboarding_common.orchestration.approval_requests import InMemoryApprovalRequests, row_of  # noqa: E402
from hr_onboarding_common.platform.hitl import Decision  # noqa: E402
from hr_onboarding_common.platform.identity import InsecureHeaderIdentityProvider  # noqa: E402

BASE = "/v1/orchestration/cases"
HR, MANAGER = demo.HR, demo.HR_LEAD
HR_OPS = {"X-User-Id": "user:hr-ops-01", "X-Client-Id": demo.CLIENT, "X-User-Roles": "hr_ops"}
REQUESTER_WITH_ROLE = {**HR, "X-User-Roles": "hr_admin,hr_lead"}
OTHER_MANAGER = {"X-User-Id": "user:hr-lead-b", "X-Client-Id": "CLIENT-TEST-B", "X-User-Roles": "hr_lead"}
BODY = dict(demo.START_BODY)


class RecordingUc01:
    """The demo's UC01 stand-in, recording the form UC01 was given."""

    def __init__(self, inner):
        self.inner, self.payloads, self.is_alive = inner, [], True

    def call(self, op, payload, **kwargs):
        self.payloads.append(payload)
        return self.inner.call(op, payload, **kwargs)


def build(persist_dir: Path | None = None, *, known_tenants=None):
    agent = demo.build_synthetic_agent(persist_dir)
    uc01 = RecordingUc01(agent.deps.worker_pool.workers["uc01"])
    agent.deps.worker_pool.workers["uc01"] = uc01
    agent.deps.approval_mirror = InMemoryApprovalRequests(known_tenants)
    client = TestClient(create_orchestrator_app(agent=agent, identity_provider=InsecureHeaderIdentityProvider(enabled=True)))
    return agent, client, uc01


def decide(client, view, headers, decision="approved", **extra):
    approval_id = view["form_approval"]["approval_id"]
    return client.post(f"{BASE}/{view['case_id']}/approvals/{approval_id}/decision",
                       json={"decision": decision, **extra}, headers=headers)


@pytest.fixture
def submitted():
    agent, client, uc01 = build()
    response = client.post(BASE, json=BODY, headers=HR)
    assert response.status_code == 201, response.text
    return agent, client, uc01, response.json()


def test_submitting_the_form_raises_a_pending_request_and_holds_uc01_and_uc02(submitted):
    agent, _, uc01, view = submitted
    fa = view["form_approval"]
    assert fa["status"] == "pending" and fa["approval_type"] == "INTERNAL_ONBOARDING_FORM"
    assert fa["required_approver_role"] == "hr_lead" and fa["separation_of_duties"] is True
    assert fa["decision"] is None and fa["requested_at"]
    assert view["stage_statuses"]["intake"] == "awaiting_approval"
    assert view["stage_statuses"]["plan_tasks"] == "pending" and not view.get("task_plan")
    assert view["workflow_status"] == "waiting_for_approval" and view["current_stage"] == "intake"
    assert uc01.payloads == []  # UC01 is not called before the form is approved
    assert [(a["action"], a["stage"], a["owner"]) for a in view["pending_actions"]] == \
        [("decide_approval", "intake", "hr_lead")]
    (item,) = agent.ctx.hitl.list(status=Decision.PENDING, gate="onboarding_form_approval", client_id=demo.CLIENT)
    assert item.requested_by == "user:hr-admin-01" and item.approver is None and item.decided_at is None


def test_the_request_is_written_to_approval_requests_without_any_decision(submitted):
    agent, _, _, view = submitted
    row = agent.deps.approval_mirror.rows[view["form_approval"]["approval_id"]]
    assert row["outcome"] == "PENDING" and row["gate"] == "onboarding_form_approval"
    assert row["approver_role"] == "hr_lead" and row["requested_by"] == "user:hr-admin-01"
    assert row["decided_by"] is row["decided_at"] is row["decision_reason_code"] is row["decision_notes"] is None
    assert "candidate_email" not in str(row["payload"]) and "ID-DEMO-001" not in str(row["payload"])
    assert view["form_approval"]["persistence"]["persisted"] is True


def test_a_person_cannot_declare_the_form_approved():
    _, client, uc01 = build()
    claimed = {**BODY, "form_approved_at": "2026-10-21T10:00:00Z", "form_approved_by_role": "hr_ops"}
    response = client.post(BASE, json=claimed, headers=HR)
    assert response.status_code == 403 and "form_approval_attestor_roles" in response.json()["detail"]
    assert client.get(BASE, headers=HR).json()["cases"] == [] and uc01.payloads == []


def test_the_case_view_names_nobody_and_the_approval_route_names_the_people_for_identity_readers(submitted):
    _, client, _, view = submitted
    assert "user:hr-admin-01" not in str(view["form_approval"]) and demo.CANDIDATE not in str(view["form_approval"])
    assert view["form_approval"]["assignment"]["status"] == "assignment_pending"
    assert view["form_approval"]["assignment"]["assigned_approver_id"] is None  # no approver is invented
    full = client.get(f"{BASE}/{view['case_id']}/form-approval", headers=HR).json()["form_approval"]
    assert full["requested_by"] == "user:hr-admin-01" and full["employee_name"] == demo.CANDIDATE
    reader = client.get(f"{BASE}/{view['case_id']}/form-approval", headers=HR_OPS).json()["form_approval"]
    assert "requested_by" not in reader and "employee_name" not in reader


def test_the_requester_cannot_approve_even_holding_the_role(submitted):
    agent, client, uc01, view = submitted
    assert decide(client, view, HR).status_code == 403  # lacks hr_lead
    response = decide(client, view, REQUESTER_WITH_ROLE)
    assert response.status_code == 403 and "separation of duties" in response.json()["detail"]
    assert client.get(f"{BASE}/{view['case_id']}", headers=HR).json()["form_approval"]["status"] == "pending"
    assert uc01.payloads == []


@pytest.mark.parametrize("headers,status", [(HR_OPS, 403), (OTHER_MANAGER, 404)], ids=["no-role", "other-tenant"])
def test_unauthorized_or_foreign_approvers_are_refused(submitted, headers, status):
    _, client, uc01, view = submitted
    assert decide(client, view, headers).status_code == status
    other_reader = {**OTHER_MANAGER, "X-User-Roles": "hr_admin"}
    assert client.get(f"{BASE}/{view['case_id']}/form-approval", headers=other_reader).status_code == 404
    assert uc01.payloads == []


def test_an_authorized_manager_approves_and_uc01_and_uc02_receive_the_actual_decision(submitted):
    agent, client, uc01, view = submitted
    response = decide(client, view, MANAGER, note="form reviewed")
    assert response.status_code == 200, response.text
    after = response.json()
    decision = after["form_approval"]["decision"]
    assert after["form_approval"]["status"] == "approved" and decision["decided_by_role"] == "hr_lead"
    assert "decided_by" not in decision  # the case view names nobody
    full = client.get(f"{BASE}/{after['case_id']}/form-approval", headers=HR).json()["form_approval"]
    assert full["decision"]["decided_by"] == "user:hr-lead-01"
    (form,) = [p["form"] for p in uc01.payloads]
    assert form["status"] == "APPROVED" and form["approved_by"] == "user:hr-lead-01"
    assert form["approved_at"] == full["decision"]["decided_at"]
    state = agent.get_case(after["case_id"], client_id=demo.CLIENT)
    assert state["uc02_role_context"]["approved_by_role"] == "hr_lead"
    assert state["uc02_role_context"]["form_approved_at"] == full["decision"]["decided_at"]
    assert after["stage_statuses"]["intake"] == "complete" and after["stage_statuses"]["plan_tasks"] == "complete"
    row = agent.deps.approval_mirror.rows[view["form_approval"]["approval_id"]]
    assert row["outcome"] == "APPROVED" and row["decided_by"] == "user:hr-lead-01" and row["decision_notes"] == "form reviewed"
    decided = [e for e in agent.ctx.audit.events if e["event"] == "hitl.decided"]
    assert decided and decided[-1]["actor"] == "user:hr-lead-01"


def test_a_rejection_keeps_uc01_and_uc02_held_and_only_a_corrected_form_raises_a_new_request(submitted):
    agent, client, uc01, view = submitted
    rejected = decide(client, view, MANAGER, decision="rejected", reason_code="policy", note="grade missing").json()
    assert rejected["form_approval"]["status"] == "rejected"
    assert rejected["stage_statuses"]["intake"] == "blocked" and rejected["stage_statuses"]["plan_tasks"] == "pending"
    assert rejected["stage_errors"]["intake"]["code"] == "ONBOARDING_FORM_APPROVAL_REJECTED" and uc01.payloads == []
    assert agent.deps.approval_mirror.rows[view["form_approval"]["approval_id"]]["outcome"] == "REJECTED"
    same = client.post(BASE, json=BODY, headers=HR).json()  # the identical form again: no new request
    assert same["form_approval"]["approval_id"] == view["form_approval"]["approval_id"]
    assert same["form_approval"]["status"] == "rejected"
    corrected = client.post(BASE, json={**BODY, "grade": "G7"}, headers=HR).json()
    assert corrected["form_approval"]["status"] == "pending"
    assert corrected["form_approval"]["approval_id"] != view["form_approval"]["approval_id"]


def test_a_corrected_form_withdraws_the_undecided_request_and_the_old_id_cannot_be_decided(submitted):
    agent, client, _, view = submitted
    corrected = client.post(BASE, json={**BODY, "designation": "Senior Software Engineer"}, headers=HR).json()
    old_id, new_id = view["form_approval"]["approval_id"], corrected["form_approval"]["approval_id"]
    assert new_id != old_id and agent.ctx.hitl.get(old_id).status == Decision.WITHDRAWN
    assert agent.deps.approval_mirror.rows[old_id]["outcome"] == "WITHDRAWN"
    assert decide(client, view, MANAGER).status_code in (409, 422)
    assert len(agent.ctx.hitl.list(status=Decision.PENDING, gate="onboarding_form_approval")) == 1


def test_a_duplicate_submission_creates_no_second_case_or_request(submitted):
    agent, client, _, view = submitted
    again = client.post(BASE, json=BODY, headers=HR).json()
    assert again["case_id"] == view["case_id"]
    assert again["form_approval"]["approval_id"] == view["form_approval"]["approval_id"]
    assert len(agent.ctx.hitl.list(status=None, gate="onboarding_form_approval")) == 1
    assert len(client.get(BASE, headers=HR).json()["cases"]) == 1


def test_a_reload_shows_the_same_pending_state(submitted):
    _, client, _, view = submitted
    reread = client.get(f"{BASE}/{view['case_id']}", headers=HR).json()
    assert reread["form_approval"] == view["form_approval"] and reread["stage_statuses"]["intake"] == "awaiting_approval"


def test_the_request_and_state_survive_a_restart_and_the_decision_still_applies(tmp_path):
    _, client, _ = build(tmp_path)
    view = client.post(BASE, json=BODY, headers=HR).json()
    agent2, client2, uc01 = build(tmp_path)  # a new process over the same stores
    reread = client2.get(f"{BASE}/{view['case_id']}", headers=HR).json()
    assert reread["form_approval"]["status"] == "pending" and reread["stage_statuses"]["intake"] == "awaiting_approval"
    assert agent2.ctx.hitl.get(view["form_approval"]["approval_id"]).status == Decision.PENDING
    approved = decide(client2, view, MANAGER).json()
    # The email and id number are held in memory only (by design): after a restart HR resubmits the same form,
    # which keeps the approval (same content) and lets UC01 run.
    assert approved["form_approval"]["status"] == "approved"
    assert approved["stage_errors"]["intake"]["code"] == "INTAKE_INPUT_UNAVAILABLE" and uc01.payloads == []
    resumed = client2.post(BASE, json=BODY, headers=HR).json()
    assert resumed["stage_statuses"]["intake"] == "complete" and uc01.payloads[0]["form"]["approved_by"] == "user:hr-lead-01"


def test_a_tenant_missing_from_client_configs_is_reported_not_hidden():
    _, client, _ = build(known_tenants={"CLIENT_DUBAI"})
    view = client.post(BASE, json=BODY, headers=HR).json()
    assert view["form_approval"]["persistence"] == {**view["form_approval"]["persistence"], "persisted": False,
                                                    "reason": f"TENANT_NOT_IN_CLIENT_CONFIGS ({demo.CLIENT})"}


def test_an_attested_in_process_start_keeps_the_existing_path():
    """A trusted integration relaying UC01's own decision (in process, form_approved_* given) is unchanged."""
    agent, _, uc01 = build()
    from hr_onboarding_common.orchestration.contracts import OnboardingCaseInput

    inp = OnboardingCaseInput(client_id=demo.CLIENT, hr_officer_id="svc:uc01-events",
                              **{k: v for k, v in BODY.items()}, form_approved_at="2026-10-21T10:00:00Z",
                              form_approved_by_role="hr_ops")
    state = agent.advance_case(inp, case_id=None, actor={"on_behalf_of": "svc:uc01-events", "roles": ["hr_admin"],
                                                         "client_id": demo.CLIENT})
    assert state["form_approval"] == {"required": False} and state["stage_statuses"]["intake"] == "complete"


def test_row_of_never_fills_decision_fields_for_an_undecided_request(submitted):
    agent, _, _, view = submitted
    row = row_of(agent.ctx.hitl.get(view["form_approval"]["approval_id"]))
    assert row["outcome"] == "PENDING" and row["decided_by"] is None and row["decided_at"] is None


# ------------------------------------------------------------------ the approvals inbox (GET /approvals)


def test_the_inbox_lists_every_case_awaiting_approval_with_who_may_decide():
    _, client, _ = build()
    first = client.post(BASE, json=BODY, headers=HR).json()
    second = client.post(BASE, json={**BODY, "offer_reference": "OFR-DEMO-002", "candidate_name": "Sam Lee"},
                         headers=HR).json()
    client.post(BASE, json={**BODY, "offer_reference": "OFR-B-9"},
                headers={**OTHER_MANAGER, "X-User-Roles": "hr_admin"})  # another tenant's case
    as_requester = client.get("/v1/orchestration/approvals", headers=HR).json()["approvals"]
    assert {a["case_id"] for a in as_requester} == {first["case_id"], second["case_id"]}
    assert all(not a["can_decide"] and "requires the 'hr_lead' role" in a["reason"] for a in as_requester)
    as_requester_with_role = client.get("/v1/orchestration/approvals", headers=REQUESTER_WITH_ROLE).json()["approvals"]
    assert all(not a["can_decide"] and "separation of duties" in a["reason"] for a in as_requester_with_role)
    as_manager = client.get("/v1/orchestration/approvals", headers=MANAGER).json()["approvals"]
    assert all(a["can_decide"] and a["reason"] is None for a in as_manager)
    item = next(a for a in as_manager if a["case_id"] == second["case_id"])
    assert item["approval_type"] == "INTERNAL_ONBOARDING_FORM" and item["required_role"] == "hr_lead"
    assert item["employee_name"] == "Sam Lee" and item["requested_by"] == "user:hr-admin-01"
    assert item["approval_id"] == second["form_approval"]["approval_id"]


def test_approving_from_the_inbox_lets_uc01_run_and_removes_the_item(submitted):
    _, client, uc01, view = submitted
    (item,) = client.get("/v1/orchestration/approvals", headers=MANAGER).json()["approvals"]
    response = client.post(f"{BASE}/{item['case_id']}/approvals/{item['approval_id']}/decision",
                           json={"decision": "approved"}, headers=MANAGER)
    assert response.status_code == 200 and response.json()["stage_statuses"]["intake"] == "complete"
    assert len(uc01.payloads) == 1
    remaining = [a for a in client.get("/v1/orchestration/approvals", headers=MANAGER).json()["approvals"]
                 if a["gate"] == "onboarding_form_approval"]
    assert remaining == []


def test_the_inbox_needs_a_reader_role():
    _, client, _ = build()
    client.post(BASE, json=BODY, headers=HR)
    assert client.get("/v1/orchestration/approvals",
                      headers={**HR, "X-User-Roles": "random_unaffiliated_role"}).status_code == 403
    # A team lead (approval_inbox_roles) sees only approvals naming them as the person on record: never the form.
    lead = client.get("/v1/orchestration/approvals", headers={**HR, "X-User-Roles": "team_lead"})
    assert lead.status_code == 200 and lead.json()["approvals"] == []


def test_the_form_approver_role_is_one_uc02_accepts():
    """UC02 receives the decision's role as the form's approver and refuses to plan for any role outside its
    ADMIN_ROLES. The policy's gate role must stay inside that set."""
    from onboarding_task_management.contracts import ADMIN_ROLES

    agent, _, _ = build()
    assert agent.policy.gate("onboarding_form_approval").approver_role in {str(r) for r in ADMIN_ROLES}


def test_me_reports_what_the_policy_allows_each_identity():
    _, client, _ = build()
    requester = client.get("/v1/orchestration/me", headers=HR).json()
    assert requester["permissions"]["resume_case"] and requester["permissions"]["start_case"]
    assert not requester["permissions"]["approve_forms"] and requester["required_roles"]["approve_forms"] == ["hr_lead"]
    lead = client.get("/v1/orchestration/me", headers=MANAGER).json()
    assert lead["permissions"]["approve_forms"] and lead["permissions"]["read_cases"]
    assert not lead["permissions"]["resume_case"]  # the route answers 403 for this identity, as reported
    assert set(lead["required_roles"]["resume_case"]) == {"hr_admin", "hr_onboarding_officer"}
