import pytest
from fastapi.testclient import TestClient

from sub_agent.api.app import create_app
from sub_agent.api.container import Container, build_container
from sub_agent.api.settings import Settings

CLIENT_ID = "client-api-001"
HR_OFFICER_ID = "hr-officer-api-001"
CANDIDATE_NAME = "Api Test Candidate"
CANDIDATE_EMAIL = "api.test.candidate@example.test"
SALARY = "Base: 125000 SYNTHETIC/yr; benefits: standard"
SYSTEM_KEY = "test-system-key-not-for-prod"


def _settings(**overrides) -> Settings:
    defaults = dict(
        app_env="dev",
        dedup_hmac_key="test-fixed-fingerprint-key",
        hris_writes_enabled=True,
        system_api_key=SYSTEM_KEY,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _client(
    container: Container | None = None, include_dev_routes: bool = True, **settings_overrides
) -> tuple[TestClient, Container]:
    container = container or build_container(_settings(**settings_overrides))
    app = create_app(container, include_dev_routes=include_dev_routes)
    return TestClient(app), container


def _headers(user_id: str, role: str, client_ids: str = CLIENT_ID) -> dict:
    return {"X-User-Id": user_id, "X-User-Role": role, "X-Client-Ids": client_ids}


def _onboarding_body(**overrides) -> dict:
    defaults = dict(
        client_id=CLIENT_ID,
        hr_officer_id=HR_OFFICER_ID,
        offer_accepted=True,
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
        designation="Software Engineer",
        location_name="Synthetic Office, Dubai",
        joining_date="2026-03-02",
        work_location_type="OFFICE",
        salary_and_terms=SALARY,
        form_status="APPROVED",
        approved_by="hr-officer-api-approver",
        approved_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return defaults


def _open_case(client: TestClient, **overrides) -> dict:
    resp = client.post(
        "/internal/onboarding-requests",
        json=_onboarding_body(**overrides),
        headers={"X-System-Key": SYSTEM_KEY},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_health_works_with_no_headers():
    client, _ = _client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_onboarding_wrong_or_missing_system_key_denied():
    client, _ = _client()

    resp = client.post("/internal/onboarding-requests", json=_onboarding_body())
    assert resp.status_code == 401

    resp = client.post(
        "/internal/onboarding-requests",
        json=_onboarding_body(),
        headers={"X-System-Key": "wrong-key"},
    )
    assert resp.status_code == 401

    resp = client.post(
        "/internal/onboarding-requests",
        json=_onboarding_body(),
        headers={"X-System-Key": SYSTEM_KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "CASE_OPENED"


def test_full_flow_over_http_only():
    client, _ = _client()
    case = _open_case(client)
    case_id = case["case_id"]
    assert case_id

    hr_officer_headers = _headers(HR_OFFICER_ID, "HR_OFFICER")
    hr_manager_headers = _headers("hr-manager-api-001", "HR_MANAGER")

    resp = client.post(
        f"/cases/{case_id}/compare",
        json={"snapshots": [{"source": "hr_system", "values": {"designation": "Senior Engineer"}}]},
        headers=hr_officer_headers,
    )
    assert resp.status_code == 200, resp.text
    discrepancy_id = resp.json()["new_discrepancies"][0]

    resp = client.post(
        f"/discrepancies/{discrepancy_id}/decision",
        json={"decision": "KEEP_FORM_VALUE"},
        headers=hr_officer_headers,
    )
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/cases/{case_id}/hris/prepare", headers=hr_officer_headers)
    assert resp.status_code == 200, resp.text
    approval_id = resp.json()["approval_id"]

    resp = client.post(
        f"/hris-approvals/{approval_id}/decision",
        json={"approve": True},
        headers=hr_manager_headers,
    )
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/cases/{case_id}/hris/write", headers=hr_manager_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["completed"] is True

    resp = client.get(f"/cases/{case_id}/status", headers=hr_manager_headers)
    assert resp.status_code == 200
    assert resp.json()["stage"] == "RECORD_CREATED"


def test_unknown_other_client_and_unassigned_officer_give_identical_404():
    client, _ = _client()
    case = _open_case(client)
    case_id = case["case_id"]

    unknown_resp = client.get(
        "/cases/not-a-real-case-id/status", headers=_headers("someone", "HR_MANAGER")
    )
    other_client_resp = client.get(
        f"/cases/{case_id}/status",
        headers=_headers("other-user", "HR_MANAGER", client_ids="client-other-999"),
    )
    unassigned_resp = client.get(
        f"/cases/{case_id}/status", headers=_headers("hr-officer-unassigned", "HR_OFFICER")
    )

    for resp in (unknown_resp, other_client_resp, unassigned_resp):
        assert resp.status_code == 404
        assert resp.json() == {"error": "case_not_found"}


def test_role_not_allowed_returns_403_with_policy_code():
    client, _ = _client()
    case = _open_case(client)
    case_id = case["case_id"]

    resp = client.post(
        f"/cases/{case_id}/hris/write", headers=_headers(HR_OFFICER_ID, "HR_OFFICER")
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"] == "policy_violation"
    assert body["code"] == "ROLE_NOT_ALLOWED"


def test_self_approval_of_hc05_returns_403():
    client, _ = _client()
    case = _open_case(client)
    case_id = case["case_id"]

    manager_headers = _headers("hr-manager-api-001", "HR_MANAGER")
    resp = client.post(f"/cases/{case_id}/hris/prepare", headers=manager_headers)
    approval_id = resp.json()["approval_id"]

    resp = client.post(
        f"/hris-approvals/{approval_id}/decision", json={"approve": True}, headers=manager_headers
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"] == "policy_violation"
    assert body["code"] == "SELF_APPROVAL"


def test_missing_identity_headers_returns_401():
    client, _ = _client()
    case = _open_case(client)
    case_id = case["case_id"]

    resp = client.get(f"/cases/{case_id}/status")
    assert resp.status_code == 401


def test_form_change_body_with_bad_field_and_salary_returns_422_without_salary_leak():
    client, _ = _client()
    case = _open_case(client)
    case_id = case["case_id"]

    resp = client.post(
        f"/cases/{case_id}/form-changes",
        json={
            "change_reason": "test",
            "approver_id": "hr-manager-api-001",
            "work_location_type": "NOT_A_REAL_LOCATION",
            "salary_and_terms": "Base: 999999 SYNTHETIC/yr; super secret bonus",
        },
        headers=_headers(HR_OFFICER_ID, "HR_OFFICER"),
    )
    assert resp.status_code == 422
    assert "Base: 999999" not in resp.text
    assert "super secret bonus" not in resp.text


def test_form_change_body_cannot_override_requested_by():
    client, _ = _client()
    case = _open_case(client)
    case_id = case["case_id"]

    resp = client.post(
        f"/cases/{case_id}/form-changes",
        json={
            "change_reason": "trying to spoof requester",
            "approver_id": "hr-manager-api-001",
            "designation": "Senior Engineer",
            "requested_by": "someone-else",
        },
        headers=_headers(HR_OFFICER_ID, "HR_OFFICER"),
    )
    # extra="forbid": the unknown field is rejected outright.
    assert resp.status_code == 422


def test_no_response_body_in_full_flow_contains_salary_email_or_discrepancy_value():
    client, _ = _client()
    case = _open_case(client)
    case_id = case["case_id"]
    assert SALARY not in str(case)
    assert CANDIDATE_EMAIL not in str(case)

    hr_officer_headers = _headers(HR_OFFICER_ID, "HR_OFFICER")
    hr_manager_headers = _headers("hr-manager-api-001", "HR_MANAGER")

    responses = []

    responses.append(
        client.post(
            f"/cases/{case_id}/compare",
            json={"snapshots": [{"source": "hr_system", "values": {"designation": "Senior Engineer"}}]},
            headers=hr_officer_headers,
        )
    )
    discrepancy_id = responses[-1].json()["new_discrepancies"][0]

    responses.append(
        client.post(
            f"/discrepancies/{discrepancy_id}/decision",
            json={"decision": "KEEP_FORM_VALUE"},
            headers=hr_officer_headers,
        )
    )
    responses.append(client.post(f"/cases/{case_id}/hris/prepare", headers=hr_officer_headers))
    approval_id = responses[-1].json()["approval_id"]
    responses.append(
        client.post(
            f"/hris-approvals/{approval_id}/decision",
            json={"approve": True},
            headers=hr_manager_headers,
        )
    )
    responses.append(client.post(f"/cases/{case_id}/hris/write", headers=hr_manager_headers))
    responses.append(client.get(f"/cases/{case_id}/status", headers=hr_manager_headers))
    responses.append(client.get(f"/cases/{case_id}/audit", headers=hr_manager_headers))
    responses.append(client.get("/approvals/pending", headers=hr_officer_headers))

    for resp in responses:
        assert resp.status_code == 200, resp.text
        assert SALARY not in resp.text
        assert CANDIDATE_EMAIL not in resp.text
        assert "Senior Engineer" not in resp.text


def test_onboarding_approved_with_approver_opens_case():
    client, _ = _client()
    resp = client.post(
        "/internal/onboarding-requests",
        json=_onboarding_body(),
        headers={"X-System-Key": SYSTEM_KEY},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "CASE_OPENED"
    assert body["case_id"]
    assert body["employee_no"]


def test_onboarding_approved_without_approver_returns_422():
    client, _ = _client()
    resp = client.post(
        "/internal/onboarding-requests",
        json=_onboarding_body(approved_by=None, approved_at=None),
        headers={"X-System-Key": SYSTEM_KEY},
    )
    assert resp.status_code == 422


def test_onboarding_awaiting_approval_is_held_with_no_case():
    client, _ = _client()
    resp = client.post(
        "/internal/onboarding-requests",
        json=_onboarding_body(form_status="AWAITING_APPROVAL", approved_by=None, approved_at=None),
        headers={"X-System-Key": SYSTEM_KEY},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "HELD"
    assert body["case_id"] is None
    assert body["employee_no"] is None


def test_onboarding_draft_is_held():
    client, _ = _client()
    resp = client.post(
        "/internal/onboarding-requests",
        json=_onboarding_body(form_status="DRAFT", approved_by=None, approved_at=None),
        headers={"X-System-Key": SYSTEM_KEY},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "HELD"
    assert body["case_id"] is None
    assert body["employee_no"] is None


def test_build_container_prod_raises():
    with pytest.raises(RuntimeError):
        build_container(_settings(app_env="prod"))


def test_kill_switch_write_returns_blocked_code():
    client, _ = _client(hris_writes_enabled=False)
    case = _open_case(client)
    case_id = case["case_id"]

    hr_officer_headers = _headers(HR_OFFICER_ID, "HR_OFFICER")
    hr_manager_headers = _headers("hr-manager-api-001", "HR_MANAGER")

    resp = client.post(f"/cases/{case_id}/hris/prepare", headers=hr_officer_headers)
    approval_id = resp.json()["approval_id"]
    client.post(
        f"/hris-approvals/{approval_id}/decision", json={"approve": True}, headers=hr_manager_headers
    )

    resp = client.post(f"/cases/{case_id}/hris/write", headers=hr_manager_headers)
    assert resp.status_code == 200
    assert resp.json()["blocked_code"] == "WRITES_DISABLED"


# --- [Proposed] dev trigger simulator (OQ-01/Q-11) --------------------------

INTAKE_CLIENT_ID = "client-intake-api-001"
INTAKE_HR_OFFICER_ID = "hr-officer-intake-api-1"
INTAKE_CANDIDATE_EMAIL = "devon.intake.api.candidate@example.test"
INTAKE_SALARY = "Base: 115000 SYNTHETIC/yr; benefits: standard"


def _dev_seed_body(offer_ref: str, **overrides) -> dict:
    defaults = dict(
        client_id=INTAKE_CLIENT_ID,
        offer_ref=offer_ref,
        hr_officer_id=INTAKE_HR_OFFICER_ID,
        candidate_name="Devon Intake Candidate",
        candidate_email=INTAKE_CANDIDATE_EMAIL,
        designation="Software Engineer",
        location_name="Synthetic Office, Dubai",
        joining_date="2026-04-01",
        work_location_type="OFFICE",
        salary_and_terms=INTAKE_SALARY,
        form_status="APPROVED",
        approved_by="hr-officer-intake-api-approver",
        approved_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return defaults


def test_dev_seed_and_inbound_email_opens_case():
    client, _ = _client()
    seed_resp = client.post(
        "/dev/seed", json=_dev_seed_body("OFFER-8001"), headers={"X-System-Key": SYSTEM_KEY}
    )
    assert seed_resp.status_code == 200, seed_resp.text
    assert seed_resp.json() == {"offer_ref": "OFFER-8001", "form_status": "APPROVED"}

    raw = f"From: {INTAKE_CANDIDATE_EMAIL}\nSubject: Re: offer [OFFER-8001] - I accept\n\nbody\n"
    resp = client.post(
        "/internal/inbound-email", json={"raw_email": raw}, headers={"X-System-Key": SYSTEM_KEY}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "CASE_OPENED"
    assert body["case_id"]
    assert INTAKE_SALARY not in resp.text
    assert INTAKE_CANDIDATE_EMAIL not in resp.text


def test_dev_seed_rejects_offer_ref_already_used_by_another_client():
    client, _ = _client()
    resp = client.post(
        "/dev/seed", json=_dev_seed_body("OFFER-8004"), headers={"X-System-Key": SYSTEM_KEY}
    )
    assert resp.status_code == 200, resp.text

    other_client_body = _dev_seed_body(
        "OFFER-8004", client_id="client-intake-api-002", candidate_email="other.candidate@example.test"
    )
    resp = client.post("/dev/seed", json=other_client_body, headers={"X-System-Key": SYSTEM_KEY})
    assert resp.status_code == 409


def test_dev_seed_draft_then_approve_form_opens_case():
    client, _ = _client()
    client.post(
        "/dev/seed",
        json=_dev_seed_body("OFFER-8002", form_status="DRAFT", approved_by=None, approved_at=None),
        headers={"X-System-Key": SYSTEM_KEY},
    )

    raw = f"From: {INTAKE_CANDIDATE_EMAIL}\nSubject: Re: offer [OFFER-8002] - I accept\n\nbody\n"
    resp = client.post(
        "/internal/inbound-email", json={"raw_email": raw}, headers={"X-System-Key": SYSTEM_KEY}
    )
    assert resp.status_code == 200, resp.text
    waiting_body = resp.json()
    assert waiting_body["outcome"] == "WAITING_FOR_FORM"
    assert waiting_body["case_id"] is None

    pending_resp = client.get(
        f"/internal/pending-starts?client_id={INTAKE_CLIENT_ID}",
        headers=_headers(INTAKE_HR_OFFICER_ID, "HR_OFFICER", client_ids=INTAKE_CLIENT_ID),
    )
    assert pending_resp.status_code == 200, pending_resp.text
    pending_list = pending_resp.json()
    assert len(pending_list) == 1
    assert pending_list[0]["offer_ref"] == "OFFER-8002"
    assert pending_list[0]["status"] == "WAITING"

    approve_resp = client.post(
        "/dev/approve-form",
        json={
            "client_id": INTAKE_CLIENT_ID,
            "offer_ref": "OFFER-8002",
            "approved_by": "hr-officer-intake-api-approver",
        },
        headers={"X-System-Key": SYSTEM_KEY},
    )
    assert approve_resp.status_code == 200, approve_resp.text
    approve_body = approve_resp.json()
    assert approve_body["outcome"] == "CASE_OPENED"
    assert approve_body["case_id"]


def test_inbound_email_and_form_approved_require_system_key():
    client, _ = _client()
    resp = client.post("/internal/inbound-email", json={"raw_email": "From: a@b.test\nSubject: x\n\n"})
    assert resp.status_code == 401

    resp = client.post(
        "/internal/form-approved", json={"client_id": INTAKE_CLIENT_ID, "offer_ref": "OFFER-9999"}
    )
    assert resp.status_code == 401


def test_pending_starts_requires_viewer_headers():
    client, _ = _client()
    resp = client.get(f"/internal/pending-starts?client_id={INTAKE_CLIENT_ID}")
    assert resp.status_code == 401


def test_dev_routes_return_404_when_not_included():
    client, _ = _client(include_dev_routes=False)

    resp = client.post(
        "/dev/seed", json=_dev_seed_body("OFFER-8003"), headers={"X-System-Key": SYSTEM_KEY}
    )
    assert resp.status_code == 404

    resp = client.post(
        "/dev/approve-form",
        json={"client_id": INTAKE_CLIENT_ID, "offer_ref": "OFFER-8003", "approved_by": "x"},
        headers={"X-System-Key": SYSTEM_KEY},
    )
    assert resp.status_code == 404

    # Non-dev routes stay registered regardless of include_dev_routes.
    resp = client.post(
        "/internal/inbound-email",
        json={"raw_email": "From: a@b.test\nSubject: x\n\n"},
        headers={"X-System-Key": SYSTEM_KEY},
    )
    assert resp.status_code == 200
