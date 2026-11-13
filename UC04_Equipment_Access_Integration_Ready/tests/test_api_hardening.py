"""API and entry-point hardening found during the pre-integration audit (D4). Synthetic data, in-process, no network."""

import copy
import socket

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from helpers import Env, make  # noqa: E402
from sub_agent.entrypoints.orchestrated import Actor, OrchestratorRequest, WorkflowRef  # noqa: E402
from sub_agent.service import Uc04ValidationError  # noqa: E402
from sub_agent.api.app import create_app  # noqa: E402
from sub_agent.api.scenarios import BY_KEY, PATHS  # noqa: E402


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded(self, address):
        assert (address[0] if isinstance(address, tuple) else address) in {"127.0.0.1", "::1", "localhost"}, address
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("DNS lookup")))


@pytest.fixture
def client():
    return TestClient(create_app(), raise_server_exceptions=False)


def orchestrated_body(**patch):
    return {**copy.deepcopy(BY_KEY["06_orchestrated_valid"].body), **patch}


# ---------------------------------------------------------------- the orchestrator envelope version is checked
@pytest.mark.parametrize("version", ["9.9", "", "1.0.1", "2"])
def test_an_unsupported_contract_version_is_refused_before_any_work(client, version):
    response = client.post(PATHS["orchestrated"], json=orchestrated_body(contract_version=version))
    assert response.status_code == 422 and response.json()["fields"] == ["contract_version"]
    assert client.app.state.env.service.store.case_ids == []                       # nothing was evaluated or stored


def test_the_supported_contract_version_still_works(client):
    assert client.post(PATHS["orchestrated"], json=orchestrated_body(contract_version="1.0")).status_code == 200


def test_the_entry_point_refuses_an_unsupported_version_without_side_effects():
    env = Env()
    request = OrchestratorRequest(contract_version="9.9", run_id="run-1", idempotency_key="k", input=make(),
                                  workflow=WorkflowRef(id="onboarding", instance="SYN-CASE-001", step="sub_agent"),
                                  actor=Actor(on_behalf_of="SYN-USER-100", roles=["onboarding_orchestrator"]))
    with pytest.raises(Uc04ValidationError) as caught:
        env.orch.evaluate(request)
    assert caught.value.fields == ["contract_version"] and env.cases.created == [] and env.service.store.case_ids == []
    assert env.desk.calls == [] and env.service.audit.events == []


# ---------------------------------------------------------------- the DEV ONLY flag is a strict boolean
@pytest.mark.parametrize("value", ["yes", "true", 1, "1", "on"])
def test_dev_reset_first_accepts_only_a_real_boolean(client, value):
    body = {**copy.deepcopy(BY_KEY["01_valid_awaiting_approval"].body), "dev_reset_first": value}
    response = client.post(PATHS["evaluate"], json=body)
    assert response.status_code == 422 and response.json()["fields"] == ["dev_reset_first"]


def test_dev_reset_first_false_and_true_still_work(client):
    body = copy.deepcopy(BY_KEY["01_valid_awaiting_approval"].body)
    assert client.post(PATHS["evaluate"], json={**body, "dev_reset_first": False}).status_code == 200
    assert client.post(PATHS["evaluate"], json={**body, "dev_reset_first": True}).status_code == 200


def test_the_orchestrated_endpoint_applies_the_same_strictness(client):
    response = client.post(PATHS["orchestrated"], json=orchestrated_body(dev_reset_first="yes"))
    assert response.status_code == 422 and response.json()["fields"] == ["dev_reset_first"]


# ---------------------------------------------------------------- future external-service failures never look like success
def test_a_failing_desk_never_produces_a_false_success_and_blocks_blind_retries(client):
    from sub_agent.ports import AdapterTimeout
    body = copy.deepcopy(BY_KEY["05_submit_all_approved"].body)
    body["dev_reset_first"] = False
    client.app.state.env.desk.fail_with = AdapterTimeout("simulated")
    first = client.post(PATHS["submit"], json=body).json()
    assert first["submitted"] == [] and [f["error"] for f in first["failed"]] == ["AdapterTimeout"] * 4
    assert first["result"]["overall_status"] == "NEEDS_HUMAN_REVIEW" and "ADAPTER_ERROR" in {b["code"] for b in first["result"]["blockers"]}
    assert all(i["ticket_id"] is None for i in first["result"]["items"])
    client.app.state.env.desk.fail_with = None
    retry = client.post(PATHS["submit"], json={**body, "idempotency_key": "retry-after-failure"}).json()
    assert retry["submitted"] == [] and {s["reason"] for s in retry["skipped"]} == {"PREVIOUS_ADAPTER_ERROR_UNRESOLVED"}


def test_idempotent_replay_through_the_api_follows_a_parent_status_change(client):
    body = {**copy.deepcopy(BY_KEY["08_replay_first_call"].body), "dev_reset_first": True}
    assert client.post(PATHS["evaluate"], json=body).json()["overall_status"] == "AWAITING_APPROVAL"
    client.put("/api/v1/dev/mock-parent-cases/SYN-CASE-001/status", json={"status": "WITHDRAWN"})
    replay = client.post(PATHS["evaluate"], json={**body, "dev_reset_first": False})
    assert replay.status_code == 200 and replay.json()["overall_status"] == "CANCELLED"
