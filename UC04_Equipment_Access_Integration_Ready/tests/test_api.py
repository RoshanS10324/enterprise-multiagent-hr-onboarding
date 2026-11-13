"""Tests for the LOCAL development API (src/sub_agent.api). They run in-process through the ASGI test client, use only synthetic
fixtures, and a fixture fails the test if anything tries to open a non-loopback network connection. The business decisions
themselves are covered by the existing suites; here we check that the API passes the contract through unchanged, maps
errors safely and never echoes input values. Skipped automatically when fastapi/httpx are not installed."""

import socket

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from helpers import ALL, Env, approval, approvals, make, ready_payload, ticket  # noqa: E402
from sub_agent.contracts import Uc04Result  # noqa: E402
from sub_agent.guardrails import PrivacyViolation  # noqa: E402
from sub_agent.policy import Policy  # noqa: E402
from sub_agent.api.app import create_app  # noqa: E402
from sub_agent.api.examples import EVALUATE_EXAMPLES, EXTRA_103, IN_101, ORCHESTRATED_EXAMPLES, SUBMIT_EXAMPLES  # noqa: E402

EVALUATE = "/api/v1/sub_agent/evaluate"
SUBMIT = "/api/v1/sub_agent/submit-eligible"
ORCHESTRATED = "/api/v1/sub_agent/orchestrated/evaluate"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    """Fail on any connection to a non-loopback host. (The event loop's own internal loopback socket pair is allowed.)"""
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        assert host in LOOPBACK, f"outbound network connection attempted: {host}"
        return real_connect(self, address)

    def refuse_dns(host, *args, **kwargs):
        raise AssertionError(f"DNS lookup attempted: {host}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", refuse_dns)


@pytest.fixture
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


def body(payload, key="api-key-1", roles=None, actor="SYN-USER-100"):
    return {"actor_id": actor, "roles": roles or ["hr_onboarding_officer"], "idempotency_key": key, "input": payload}


def env_of(client):
    return client.app.state.env


# --------------------------------------------------------------------------- info, health, docs
def test_health_root_docs_and_openapi_are_served(client):
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["implementation_mode"] == "LOCAL_SIMULATION"
    assert health["policy_enabled"] is True and health["autonomy"] == "L1"
    info = client.get("/").json()
    assert info["docs"] == "/docs" and info["openapi"] == "/openapi.json" and "Not production-ready" in info["notice"]
    docs = client.get("/docs")
    assert docs.status_code == 200 and "swagger-ui" in docs.text.lower()
    assert client.get("/openapi.json").status_code == 200


def test_openapi_documents_endpoints_examples_and_error_responses(client):
    spec = client.get("/openapi.json").json()
    assert "Not production-ready" in spec["info"]["description"]
    assert {EVALUATE, SUBMIT, ORCHESTRATED, "/health", "/"} <= set(spec["paths"])
    operation = spec["paths"][EVALUATE]["post"]
    assert set(EVALUATE_EXAMPLES) == set(operation["requestBody"]["content"]["application/json"]["examples"])
    assert {"200", "403", "404", "409", "422", "500"} <= set(operation["responses"])
    schemas = spec["components"]["schemas"]
    assert "Uc04Input" in schemas and "Uc04Result" in schemas and "ErrorResponse" in schemas


# --------------------------------------------------------------------------- every documented example
EXPECTED = {
    "01_valid_awaiting_approval": (200, "AWAITING_APPROVAL"),
    "01_fully_evidenced_ready": (200, "READY"),
    "01_missing_required_field": (422, None),
    "02_wrong_approver_role": (200, "AWAITING_APPROVAL"),
    "03_out_of_matrix_access": (200, "AWAITING_APPROVAL"),
    "04_offer_not_accepted": (200, "BLOCKED"),
    "08_duplicate_request_key": (200, "AWAITING_APPROVAL"),
    "05_it_rejection_needs_review": (200, "NEEDS_HUMAN_REVIEW"),
    "07_caller_role_not_allowed": (403, None),
}


def test_every_stated_expectation_refers_to_a_real_example():
    assert set(EXPECTED) <= set(EVALUATE_EXAMPLES)  # the full scenario set is asserted in tests/test_api_scenarios.py


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_swagger_example_gives_the_outcome_its_summary_states(client, name):
    response = client.post(EVALUATE, json=EVALUATE_EXAMPLES[name]["value"])
    status, overall = EXPECTED[name]
    assert response.status_code == status
    if overall:
        assert response.json()["overall_status"] == overall


# --------------------------------------------------------------------------- 1. valid request, and parity with the service
def test_valid_request_returns_the_service_result(client):
    response = client.post(EVALUATE, json=body(make()))
    assert response.status_code == 200
    result = response.json()
    assert result["implementation_mode"] == "LOCAL_SIMULATION" and result["case_id"] == "SYN-CASE-001"
    assert [i["state"] for i in result["items"]] == ["APPROVAL_PENDING"] * 4
    assert all(p["executed"] is False for p in result["proposals"])
    assert result["caller"]["invocation_mode"] == "DIRECT"


@pytest.mark.parametrize("scenario", ["awaiting", "ready", "gate", "matrix"])
def test_api_decision_equals_the_direct_entry_point_decision(client, scenario):
    payloads = {
        "awaiting": lambda: make(),
        "ready": lambda: ready_payload(),
        "gate": lambda: make(offer_accepted=False),
        "matrix": lambda: make(requested_access=[IN_101, EXTRA_103], requested_equipment=[], approval_records=[approval("101")]),
    }
    api = Uc04Result.model_validate(client.post(EVALUATE, json=body(payloads[scenario]())).json())
    direct = Env().run(payloads[scenario]())
    assert api.business_decision() == direct.business_decision()


# --------------------------------------------------------------------------- 2. missing or invalid fields
def test_missing_required_field_is_422_with_field_names_only(client):
    payload = make()
    del payload["joining_date"]
    response = client.post(EVALUATE, json=body(payload))
    assert response.status_code == 422
    assert response.json() == {"error": "INVALID_REQUEST", "fields": ["input.joining_date"],
                               "detail": "The request does not match the UC-04 contract. Values are not echoed."}


def test_invalid_values_are_never_echoed(client):
    payload = make()
    payload["requested_access"][0]["note"] = "salary AED 99999 for this joiner"
    del payload["joining_date"]
    text = client.post(EVALUATE, json=body(payload)).text.lower()
    assert "salary" not in text and "99999" not in text


@pytest.mark.parametrize("mutate,field", [
    (lambda b: b.update(unexpected="x"), "unexpected"),
    (lambda b: b["input"].update(case_id="CASE-REAL-1"), "input.case_id"),
    (lambda b: b["input"].update(offer_accepted="yes"), "input.offer_accepted"),
    (lambda b: b["input"].update(work_mode="TELEPORT"), "input.work_mode"),
    (lambda b: b.update(actor_id="alice"), "actor_id"),
])
def test_invalid_requests_are_rejected_with_the_field_name(client, mutate, field):
    request = body(make())
    mutate(request)
    response = client.post(EVALUATE, json=request)
    assert response.status_code == 422 and field in response.json()["fields"]


def test_malformed_json_is_422(client):
    response = client.post(EVALUATE, content="{not json", headers={"content-type": "application/json"})
    assert response.status_code == 422 and response.json()["error"] == "INVALID_REQUEST"


# --------------------------------------------------------------------------- 3. unapproved or invalid approval
def test_wrong_role_approval_is_rejected_and_the_item_stays_pending(client):
    result = client.post(EVALUATE, json=body(make(approval_records=[approval("101", role="account_manager")]))).json()
    assert [r["reason_code"] for r in result["rejected_approvals"]] == ["WRONG_APPROVER_ROLE"]
    first = next(i for i in result["items"] if i["request_key"] == "SYN-REQ-101")
    assert first["state"] == "APPROVAL_PENDING" and first["approval_state"] == "PENDING"


def test_unapproved_items_are_never_submitted(client):
    report = client.post(SUBMIT, json=body(make())).json()
    assert report["submitted"] == [] and env_of(client).desk.calls == []


# --------------------------------------------------------------------------- 4. outside the role/client configuration
def test_out_of_matrix_access_needs_an_exception_and_is_not_submitted(client):
    payload = make(requested_access=[IN_101, EXTRA_103], requested_equipment=[],
                   approval_records=[approval("101"), approval("103", request_id="SYN-CASE-001:SYN-REQ-103",
                                                                 item="admin-console-access")])
    result = client.post(EVALUATE, json=body(payload)).json()
    extra = next(i for i in result["items"] if i["request_key"] == "SYN-REQ-103")
    assert extra["in_matrix"] is False and extra["exception_required"] is True and extra["state"] == "EXCEPTION_PENDING"
    submitted = client.post(SUBMIT, json=body(payload, key="api-key-2")).json()["submitted"]
    assert submitted == ["SYN-CASE-001:SYN-REQ-101"]


def test_another_clients_configuration_is_refused(client):
    response = client.post(EVALUATE, json=body(make(client="bravo", n=1)))  # parent case 001 belongs to client_alpha
    assert response.status_code == 409 and response.json()["error"] == "CASE_MISMATCH"


# --------------------------------------------------------------------------- 5. blocked, on-hold, withdrawn
def test_start_gate_blocks_and_prepares_nothing(client):
    result = client.post(EVALUATE, json=body(make(offer_accepted=False))).json()
    assert result["overall_status"] == "BLOCKED" and result["items"] == []
    assert {b["code"] for b in result["blockers"]} == {"START_GATE_OFFER_NOT_ACCEPTED"}


def test_parent_case_on_hold_blocks_and_withdrawn_cancels(client):
    on_hold = client.put("/api/v1/dev/mock-parent-cases/SYN-CASE-001/status", json={"status": "ON_HOLD"})
    assert on_hold.status_code == 200 and on_hold.json()["status"] == "ON_HOLD"
    held = client.post(EVALUATE, json=body(make())).json()
    assert held["overall_status"] == "BLOCKED" and {b["code"] for b in held["blockers"]} == {"CASE_ON_HOLD"}
    client.put("/api/v1/dev/mock-parent-cases/SYN-CASE-001/status", json={"status": "WITHDRAWN"})
    cancelled = client.post(EVALUATE, json=body(make(), key="api-key-2")).json()
    assert cancelled["overall_status"] == "CANCELLED" and cancelled["items"] == []


def test_dev_status_endpoint_validates_case_and_status(client):
    assert client.put("/api/v1/dev/mock-parent-cases/SYN-CASE-999/status", json={"status": "ON_HOLD"}).status_code == 404
    assert client.put("/api/v1/dev/mock-parent-cases/SYN-CASE-001/status", json={"status": "NOPE"}).status_code == 422
    assert len(client.get("/api/v1/dev/mock-parent-cases").json()) == 4


def test_unknown_case_is_404_and_never_created(client):
    response = client.post(EVALUATE, json=body(make(n=9)))
    assert response.status_code == 404 and response.json()["error"] == "UNKNOWN_CASE"
    assert env_of(client).cases.created == [] and len(client.get("/api/v1/dev/mock-parent-cases").json()) == 4


# --------------------------------------------------------------------------- 6. duplicates and idempotency
def test_duplicate_request_key_creates_one_item(client):
    result = client.post(EVALUATE, json=body(make(requested_access=[IN_101, dict(IN_101)], requested_equipment=[]))).json()
    assert len(result["items"]) == 1
    assert [d["reason_code"] for d in result["duplicates_ignored"]] == ["DUPLICATE_REQUEST_KEY"]


def test_idempotent_replay_returns_the_same_stored_result(client):
    first = client.post(EVALUATE, json=body(make(), key="replay-1"))
    second = client.post(EVALUATE, json=body(make(), key="replay-1"))
    assert first.status_code == second.status_code == 200 and first.json() == second.json()


def test_same_key_with_a_different_payload_is_a_conflict_until_reset(client):
    assert client.post(EVALUATE, json=body(make(), key="reuse-1")).status_code == 200
    different = body(make(offer_accepted=False), key="reuse-1")
    conflict = client.post(EVALUATE, json=different)
    assert conflict.status_code == 409 and conflict.json()["error"] == "IDEMPOTENCY_CONFLICT"
    assert client.post("/api/v1/dev/reset").json()["status"] == "reset"
    assert client.post(EVALUATE, json=different).status_code == 200


# --------------------------------------------------------------------------- 7. policy and kill switch
def test_caller_without_an_allowed_role_is_forbidden(client):
    response = client.post(EVALUATE, json=body(make(), roles=["intern"]))
    assert response.status_code == 403 and response.json()["error"] == "CALLER_NOT_PERMITTED"


def test_disabled_policy_kill_switch_refuses_every_call():
    disabled = TestClient(create_app(Policy.load().model_copy(update={"enabled": False})), raise_server_exceptions=False)
    assert disabled.get("/health").json()["policy_enabled"] is False
    for path in (EVALUATE, SUBMIT):
        response = disabled.post(path, json=body(make()))
        assert response.status_code == 403 and "kill switch" in response.json()["detail"]
    assert env_of(disabled).desk.calls == []


# --------------------------------------------------------------------------- 8. human review
def test_it_rejection_needs_human_review_with_an_owner(client):
    payload = make(approval_records=approvals(["201"]), mock_it_ticket_updates=[
        ticket("201", "CREATED"), ticket("201", "REJECTED", reason="Wrong model requested")])
    result = client.post(EVALUATE, json=body(payload)).json()
    assert result["overall_status"] == "NEEDS_HUMAN_REVIEW"
    blocker = next(b for b in result["blockers"] if b["code"] == "IT_REJECTED")
    assert blocker["owner"] == "HIRING_MANAGER" and blocker["severity"] == "REVIEW"


# --------------------------------------------------------------------------- submission (mock desk only)
def test_submit_eligible_uses_only_the_mock_desk(client):
    report = client.post(SUBMIT, json=body(make(approval_records=approvals(ALL)))).json()
    assert len(report["submitted"]) == 4 and report["failed"] == []
    desk = env_of(client).desk
    assert desk.real_side_effects is False and len(desk.calls) == 4
    assert all(i["ticket_id"].startswith("SYN-TKT-") for i in report["result"]["items"])


def test_evaluate_never_calls_the_desk(client):
    client.post(EVALUATE, json=body(make(approval_records=approvals(ALL))))
    assert env_of(client).desk.calls == []


@pytest.mark.parametrize("name", sorted(SUBMIT_EXAMPLES))
def test_submit_examples_run(client, name):
    assert client.post(SUBMIT, json=SUBMIT_EXAMPLES[name]["value"]).status_code == 200


# --------------------------------------------------------------------------- orchestrated path
def test_orchestrated_example_proposes_and_matches_the_direct_decision(client):
    example = ORCHESTRATED_EXAMPLES["06_orchestrated_valid"]["value"]
    response = client.post(ORCHESTRATED, json=example)
    assert response.status_code == 200 and response.json()["status"] == "proposed"
    api = Uc04Result.model_validate(response.json()["result"])
    assert api.caller["invocation_mode"] == "PARENT_ORCHESTRATED"
    assert api.business_decision() == Env().run(make()).business_decision()


def test_orchestrated_workflow_instance_must_match_the_case(client):
    example = dict(ORCHESTRATED_EXAMPLES["06_orchestrated_valid"]["value"])
    example["workflow"] = {"id": "onboarding", "instance": "SYN-CASE-002", "step": "sub_agent"}
    response = client.post(ORCHESTRATED, json=example)
    assert response.status_code == 422 and "workflow.instance" in response.json()["fields"]


def test_orchestrated_bad_input_names_fields_only(client):
    example = dict(ORCHESTRATED_EXAMPLES["06_orchestrated_valid"]["value"])
    example["input"] = {"case_id": "SYN-CASE-001"}
    response = client.post(ORCHESTRATED, json=example)
    assert response.status_code == 422 and response.json()["error"] == "INVALID_REQUEST"
    assert "SYN-CASE-001" not in response.text


# --------------------------------------------------------------------------- safe handling of service exceptions
def test_unexpected_service_error_is_500_without_details(client, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("simulated internal failure with detail")

    monkeypatch.setattr(env_of(client).service, "evaluate", explode)
    response = client.post(EVALUATE, json=body(make()))
    assert response.status_code == 500 and response.json()["error"] == "INTERNAL_ERROR"
    assert "simulated" not in response.text
    assert response.json()["detail"] == "Unexpected error. Details are not returned."


def test_privacy_guard_failure_is_422_with_labels_only(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise PrivacyViolation("result: sensitive content detected (['pay_or_compensation'])")

    monkeypatch.setattr(env_of(client).service, "evaluate", refuse)
    response = client.post(EVALUATE, json=body(make()))
    assert response.status_code == 422 and response.json()["error"] == "PRIVACY_GUARD"


def test_the_api_adds_no_unsafe_surface(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    assert not {p for p in paths if "file" in p or "url" in p or "exec" in p}
    assert client.get("/etc/passwd").status_code == 404
