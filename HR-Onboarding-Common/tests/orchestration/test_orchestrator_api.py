"""HTTP boundary of the orchestrator (orchestration/api.py): identity, tenancy, roles, validation,
error mapping, and the UC05 lifecycle driven over HTTP against the real UC05 worker (UC01-UC04 as
contract-compatible doubles, see _lifecycle.py).

Identity here is InsecureHeaderIdentityProvider(enabled=True) -- the development/test provider; a
deployment puts a real identity provider behind the same IdentityProvider interface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from _lifecycle import (BUDDY_CANDIDATE, CLIENT, CLIENT_CONFIG, HR, HR_2, OTHER_TENANT_HR, SCHEDULER, TEAM_LEAD,
                        World, start_uc05_worker)
from hr_onboarding_common.orchestration.api import create_orchestrator_app
from hr_onboarding_common.platform.identity import InsecureHeaderIdentityProvider

START_BODY = {"offer_accepted": True, "offer_reference": "OFR-API-001", "offer_acceptance_event_id": "EVT-001",
              "candidate_name": "Alex Morgan", "candidate_email": "alex.morgan@example.com",
              "designation": "Software Engineer", "joining_date": "2026-12-01", "id_number": "ID-API-001",
              "role_id": "software_engineer", "department": "Engineering", "manager_id": "TL-TEST-001",
              "location": "Dubai", "project": "platform"}


def h(actor) -> dict:
    return {"X-User-Id": actor["on_behalf_of"], "X-Client-Id": actor["client_id"],
            "X-User-Roles": ",".join(actor["roles"])}


@pytest.fixture(scope="module")
def uc05():
    worker = start_uc05_worker()
    yield worker
    worker.stop()


@pytest.fixture
def api(uc05):
    world = World(uc05)
    world.hook("reset")
    world.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    world.hook("add_client_config", config=CLIENT_CONFIG)
    client = TestClient(create_orchestrator_app(agent=world.agent,
                                                identity_provider=InsecureHeaderIdentityProvider(enabled=True)))
    body = {**START_BODY, "uc04_client_config_ref": world.case_input().uc04_client_config_ref}
    return world, client, body


def _approval(view: dict, stage: str) -> str:
    (ref,) = [p["reference"] for p in view["pending_approvals"] if p.get("stage") == stage]
    return ref


def _decide(client, case_id, approval_id, actor, decision="approved", **body):
    return client.post(f"/v1/orchestration/cases/{case_id}/approvals/{approval_id}/decision",
                       json={"decision": decision, **body}, headers=h(actor))


def _documents(view: dict, tag: str = "1") -> dict:
    return {"documents": [{"document_type": o["document_type"], "document_ref": f"SYN-DOC-{o['document_type']}-{tag}",
                           "metadata": {"readable": True, "required_fields_present": True,
                                        "expiry_date": "2031-01-01", "evidence_stored": True,
                                        "attestation_confirmed": True}}
                          for o in view["document_collection"]["outstanding"]]}


HR_LEAD = {"on_behalf_of": "user:hr-lead-01", "roles": ["hr_lead"], "client_id": CLIENT}


def _approve_form(client, view: dict) -> dict:
    """A person's internal onboarding form waits for the onboarding_form_approval gate: an authorized HR
    lead who is not the requester approves it, and only then does UC01 open the case."""
    assert view["form_approval"]["status"] == "pending", view.get("form_approval")
    response = client.post(f"/v1/orchestration/cases/{view['case_id']}/approvals/"
                           f"{view['form_approval']['approval_id']}/decision",
                           json={"decision": "approved"}, headers=h(HR_LEAD))
    assert response.status_code == 200, response.text
    return response.json()


def _open(client, body, actor=HR) -> dict:
    return _approve_form(client, client.post("/v1/orchestration/cases", json=body, headers=h(actor)).json())


def _start(client, body, actor=HR) -> dict:
    """Start a case, then supply its documents over HTTP: the branches fan out after verification."""
    view = _open(client, body, actor)
    response = client.post(f"/v1/orchestration/cases/{view['case_id']}/documents", json=_documents(view),
                           headers=h(actor))
    assert response.status_code == 200, response.text
    return response.json()


def test_every_case_route_requires_a_verified_identity(api):
    _, client, body = api
    for method, path, payload in (("post", "/v1/orchestration/cases", body), ("get", "/v1/orchestration/cases/C", None),
                                  ("post", "/v1/orchestration/cases/C/resume", None),
                                  ("post", "/v1/orchestration/cases/C/joining-day", {"as_of": "2026-12-01"}),
                                  ("post", "/v1/orchestration/cases/C/approvals/A/decision", {"decision": "approved"})):
        response = getattr(client, method)(path, json=payload) if payload else getattr(client, method)(path)
        assert response.status_code == 401
    assert client.get("/healthz").json()["enabled"] is True


def test_start_takes_the_tenant_from_the_identity_and_returns_no_personal_data(api):
    _, client, body = api
    response = client.post("/v1/orchestration/cases", json=body, headers=h(HR))
    assert response.status_code == 201
    pending = response.json()
    assert pending["stage_statuses"]["intake"] == "awaiting_approval" and pending["form_approval"]["status"] == "pending"
    approved = client.post(f"/v1/orchestration/cases/{pending['case_id']}/approvals/"
                           f"{pending['form_approval']['approval_id']}/decision",
                           json={"decision": "approved"}, headers=h(HR_LEAD))
    view = approved.json()
    assert view["client_id"] == CLIENT and view["stage_statuses"]["document_submission"] == "waiting_for_input"
    assert view["workflow_status"] == "waiting_for_input" and view["data_origin"] == "synthetic"
    assert view["document_collection"]["notification"]["synthetic"] is True
    documents = client.post(f"/v1/orchestration/cases/{view['case_id']}/documents", json=_documents(view),
                            headers=h(HR))
    assert documents.json()["stage_statuses"]["orientation_scheduling"] == "awaiting_approval"
    for text in (response.text, approved.text, documents.text):
        for secret in ("alex.morgan@example.com", "ID-API-001", "intake_trigger", "user:hr-admin-01", "Alex Morgan",
                       "2031-01-01", "submitted_by"):
            assert secret not in text
    smuggled = client.post("/v1/orchestration/cases", json={**body, "client_id": "CLIENT-TEST-B"}, headers=h(HR))
    assert smuggled.status_code == 422  # a tenant can never come from the request body


def test_reading_a_case_needs_a_reader_role_and_the_same_tenant(api):
    _, client, body = api
    case_id = client.post("/v1/orchestration/cases", json=body, headers=h(HR)).json()["case_id"]
    assert client.get(f"/v1/orchestration/cases/{case_id}", headers=h(HR)).status_code == 200
    assert client.get(f"/v1/orchestration/cases/{case_id}", headers=h(TEAM_LEAD)).status_code == 403
    other = client.get(f"/v1/orchestration/cases/{case_id}", headers=h(OTHER_TENANT_HR))
    unknown = client.get("/v1/orchestration/cases/CASE-NOPE", headers=h(HR))
    assert other.status_code == unknown.status_code == 404 and other.json() == unknown.json()
    assert client.post(f"/v1/orchestration/cases/{case_id}/resume", headers=h(OTHER_TENANT_HR)).status_code == 404


def test_full_uc05_lifecycle_over_http(api):
    _, client, body = api
    view = _start(client, body)
    case_id = view["case_id"]
    view = _decide(client, case_id, _approval(view, "orientation_scheduling"), HR).json()
    view = _decide(client, case_id, _approval(view, "buddy_assignment"), HR).json()
    view = _decide(client, case_id, _approval(view, "team_introduction"), TEAM_LEAD).json()
    assert view["stage_statuses"]["day_one_readiness"] == "awaiting_approval"
    signoff = _approval(view, "day_one_readiness")  # the orchestrator's own gate is listed like any other
    view = _decide(client, case_id, signoff, HR_2).json()
    assert view["stage_statuses"]["joining_day_welcome"] == "waiting"

    early = client.post(f"/v1/orchestration/cases/{case_id}/joining-day", json={"as_of": "2026-11-30"},
                        headers=h(SCHEDULER))
    assert early.status_code == 409
    view = client.post(f"/v1/orchestration/cases/{case_id}/joining-day", json={"as_of": "2026-12-01"},
                       headers=h(SCHEDULER)).json()
    view = _decide(client, case_id, _approval(view, "joining_day_welcome"), HR).json()
    assert view["overall_status"] == "complete"
    assert [e["stage"] for e in view["stage_history"]].count("joining_day_welcome") >= 2


def test_decisions_are_checked_by_the_owning_inbox_and_mapped_to_http(api):
    _, client, body = api
    view = _start(client, body)
    case_id, induction = view["case_id"], _approval(view, "orientation_scheduling")
    employee = {"on_behalf_of": "user:someone", "roles": ["employee"], "client_id": CLIENT}
    assert _decide(client, case_id, induction, employee).status_code == 403  # UC05: lacks role hr_admin
    assert _decide(client, case_id, induction, HR, decision="rejected").status_code == 422  # reason_code required
    assert _decide(client, case_id, "apr-not-this-case", HR).status_code == 404
    assert _decide(client, case_id, induction, OTHER_TENANT_HR).status_code == 404
    assert _decide(client, case_id, induction, HR).status_code == 200
    again = _decide(client, case_id, induction, HR)
    assert again.status_code == 409 and "Traceback" not in again.text


def test_uc05_operations_validate_input_roles_and_prerequisites(api):
    _, client, body = api
    case_id = _start(client, body)["case_id"]
    url = f"/v1/orchestration/cases/{case_id}/orientation/operations"
    naive = client.post(url, json={"operation": "schedule_buddy_intro", "start": "2026-12-02T10:00:00"}, headers=h(HR))
    assert naive.status_code == 422  # a start time must carry its timezone
    assert client.post(url, json={"operation": "delete_everything"}, headers=h(HR)).status_code == 422
    early = client.post(url, json={"operation": "schedule_buddy_intro", "start": "2026-12-02T10:00:00+04:00"},
                        headers=h(HR))
    assert early.status_code == 409  # no buddy assigned yet
    denied = client.post(url, json={"operation": "propose_buddy"}, headers=h(TEAM_LEAD))
    assert denied.status_code == 403


def test_kill_switch_stops_every_write_but_not_reads(api):
    world, client, body = api
    case_id = client.post("/v1/orchestration/cases", json=body, headers=h(HR)).json()["case_id"]
    world.ctx.policies.override("onboarding-orchestrator", enabled=False)
    try:
        assert client.post(f"/v1/orchestration/cases/{case_id}/resume", headers=h(HR)).status_code == 403
        assert client.post("/v1/orchestration/cases", json=body, headers=h(HR)).status_code == 403
        for path, payload in ((f"/v1/orchestration/cases/{case_id}/documents", {"documents": [
                                   {"document_type": "passport", "document_ref": "SYN-1"}]}),
                              (f"/v1/orchestration/cases/{case_id}/documents/reviews", {"reviews": [
                                   {"document_type": "passport", "decision": "approved"}]}),
                              (f"/v1/orchestration/cases/{case_id}/stages/validate_documents/retry", None)):
            assert client.post(path, json=payload, headers=h(HR)).status_code == 403
        assert client.get(f"/v1/orchestration/cases/{case_id}/pending-actions", headers=h(HR)).status_code == 200
        assert client.get(f"/v1/orchestration/cases/{case_id}", headers=h(HR)).status_code == 200
        assert client.get("/healthz").json()["enabled"] is False
    finally:
        world.ctx.policies.override("onboarding-orchestrator", enabled=True)


def test_rsvp_route_forwards_to_uc05_and_surfaces_the_conflict(api):
    _, client, body = api
    view = _start(client, body)
    case_id, session_id = view["case_id"], view["orientation"]["session_id"]
    url = f"/v1/orchestration/cases/{case_id}/sessions/{session_id}/responses"
    lead = {"on_behalf_of": "TL-TEST-001", "roles": ["team_lead"], "client_id": CLIENT}
    r = client.post(url, json={"participant_id": "TL-TEST-001", "response": "declined"}, headers=h(lead))
    assert r.status_code == 200 and r.json()["outcome"]["outcome"] == "conflict_awaiting_hr_decision"
    assert [p["gate"] for p in r.json()["case"]["pending_approvals"]].count("attendance_decision") == 1
    assert client.post(url, json={"participant_id": "TL-TEST-001", "response": "maybe"},
                       headers=h(lead)).status_code == 422
    assert client.post(url, json={"participant_id": "TL-TEST-001", "response": "accepted",
                                  "responded_at": "2026-11-02T10:00:00"}, headers=h(lead)).status_code == 422
    unknown = f"/v1/orchestration/cases/{case_id}/sessions/session-nope/responses"
    assert client.post(unknown, json={"participant_id": "TL-TEST-001", "response": "accepted"},
                       headers=h(lead)).status_code == 404
    assert client.post(url, json={"participant_id": "TL-TEST-001", "response": "accepted"},
                       headers=h(OTHER_TENANT_HR)).status_code == 404


def test_welcome_operations_validate_their_input_over_http(api):
    _, client, body = api
    case_id = _start(client, body)["case_id"]
    url = f"/v1/orchestration/cases/{case_id}/orientation/operations"
    bad = client.post(url, json={"operation": "resolve_welcome_delivery", "resolution": "probably"}, headers=h(HR))
    assert bad.status_code == 422
    early = client.post(url, json={"operation": "retry_welcome_delivery", "reason": "x"}, headers=h(HR))
    assert early.status_code == 409 and "Traceback" not in early.text


def test_start_rejects_an_unknown_timezone_and_accepts_a_supplied_employee_id(api):
    _, client, body = api
    assert client.post("/v1/orchestration/cases", json={**body, "timezone": "Mars/Olympus"},
                       headers=h(HR)).status_code == 422
    view = _start(client, {**body, "employee_id": "EMP-TEST-001", "timezone": "Asia/Dubai"})
    assert view["employee_ref"]["employee_id"] == "EMP-TEST-001" and view["employee_ref"]["source"] == "explicit"


def test_document_routes_check_identity_role_tenant_and_content(api):
    _, client, body = api
    view = _open(client, body)
    case_id = view["case_id"]
    base = f"/v1/orchestration/cases/{case_id}"
    one = {"documents": [{"document_type": "passport", "document_ref": "SYN-DOC-passport-1"}]}
    for method, path, payload in (("post", f"{base}/documents", one), ("get", f"{base}/pending-actions", None),
                                  ("post", f"{base}/documents/reviews",
                                   {"reviews": [{"document_type": "passport", "decision": "approved"}]}),
                                  ("post", f"{base}/stages/validate_documents/retry", None)):
        response = getattr(client, method)(path, json=payload) if payload else getattr(client, method)(path)
        assert response.status_code == 401
    assert client.post(f"{base}/documents", json=one, headers=h(TEAM_LEAD)).status_code == 403
    assert client.post(f"{base}/documents", json=one, headers=h(OTHER_TENANT_HR)).status_code == 404
    unknown = {"documents": [{"document_type": "tax_return", "document_ref": "SYN-1"}]}
    assert client.post(f"{base}/documents", json=unknown, headers=h(HR)).status_code == 422
    content = {"documents": [{**one["documents"][0], "content": "JVBERi0xLjQK"}]}  # no field for contents
    assert client.post(f"{base}/documents", json=content, headers=h(HR)).status_code == 422
    actions = client.get(f"{base}/pending-actions", headers=h(HR)).json()
    assert actions["workflow_status"] == "waiting_for_input"
    assert [a["action"] for a in actions["pending_actions"]] == ["supply_documents"]
    assert client.get(f"{base}/pending-actions", headers=h(OTHER_TENANT_HR)).status_code == 404
    assert client.get(f"{base}/pending-actions", headers=h(TEAM_LEAD)).status_code == 403
    assert client.get(base, headers=h(HR)).json()["documents"] == {}  # nothing was recorded by a refusal


def test_review_and_retry_routes_refuse_what_they_cannot_decide(api):
    _, client, body = api
    view = _start(client, body)
    base = f"/v1/orchestration/cases/{view['case_id']}"
    officer = {"on_behalf_of": "user:hr-officer-01", "roles": ["hr_onboarding_officer"], "client_id": CLIENT}
    review = {"reviews": [{"document_type": "passport", "decision": "approved"}]}
    assert client.post(f"{base}/documents/reviews", json=review, headers=h(officer)).status_code == 403
    assert client.post(f"{base}/documents/reviews", json=review, headers=h(HR)).status_code == 409  # verified already
    assert client.post(f"{base}/stages/validate_documents/retry", headers=h(HR)).status_code == 409  # complete
    assert client.post(f"{base}/stages/no_such_stage/retry", headers=h(HR)).status_code == 422
    assert client.post(f"{base}/stages/validate_documents/retry", headers=h(TEAM_LEAD)).status_code == 403
    again = client.post(f"{base}/documents", json=_documents({"document_collection": {"outstanding": [
        {"document_type": "passport"}]}}, tag="2"), headers=h(HR))
    assert again.status_code == 409 and "Traceback" not in again.text  # verified documents are not re-opened
