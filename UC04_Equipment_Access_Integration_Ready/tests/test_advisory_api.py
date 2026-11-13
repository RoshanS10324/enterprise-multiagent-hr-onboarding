"""The advisory over HTTP: `POST /api/v1/sub_agent/evaluate-with-advisory`.

Regression for a gap found in review: the response model and an advisor were wired into the API, but no route returned the advisory,
so it was unreachable over HTTP. The advisory is read-only: the authoritative result must equal the plain evaluate result."""

import copy
import socket

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sub_agent.advisor import ADVISORY_BANNER  # noqa: E402
from sub_agent.api.app import create_app  # noqa: E402
from sub_agent.api.scenarios import BY_KEY  # noqa: E402
from sub_agent.contracts import Uc04Result  # noqa: E402
from sub_agent.policy import Policy  # noqa: E402

ADVISORY = "/api/v1/sub_agent/evaluate-with-advisory"
EVALUATE = "/api/v1/sub_agent/evaluate"
SECTIONS = ["#### 1. Status & Findings", "#### 2. Approvals", "#### 3. Risks & Blockers", "#### 4. Readiness", "#### 5. Recommended Actions"]


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded(self, address):
        assert (address[0] if isinstance(address, tuple) else address) in {"127.0.0.1", "::1", "localhost"}, address
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("DNS lookup")))


def client(**kw):
    return TestClient(create_app(**kw), raise_server_exceptions=False)


def body(key="01_valid_awaiting_approval", **patch):
    return {**copy.deepcopy(BY_KEY[key].body), **patch}


def test_the_route_returns_the_result_and_a_complete_advisory():
    response = client().post(ADVISORY, json=body())
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"result", "advisory"}
    advisory = data["advisory"]
    assert advisory["is_simulated"] is True and advisory["banner"] == ADVISORY_BANNER
    assert advisory["overall_status"] == data["result"]["overall_status"] == "AWAITING_APPROVAL"
    text = advisory["formatted_explanation"]
    assert text.splitlines()[0] == ADVISORY_BANNER and all(s in text for s in SECTIONS) and len(text) > 400


def test_the_authoritative_result_is_the_same_as_plain_evaluate():
    for key in ("01_valid_awaiting_approval", "01_fully_evidenced_ready", "04_laptop_delayed_beyond_joining", "04_offer_not_accepted"):
        plain = Uc04Result.model_validate(client().post(EVALUATE, json=body(key)).json())
        with_advice = Uc04Result.model_validate(client().post(ADVISORY, json=body(key)).json()["result"])
        assert with_advice.business_decision() == plain.business_decision(), key


def test_asking_for_advice_changes_nothing():
    c = client()
    first = c.post(ADVISORY, json=body(dev_reset_first=True))
    version = c.app.state.env.service.store.version
    again = c.post(ADVISORY, json=body(dev_reset_first=False))
    assert again.json() == first.json()                                   # idempotent replay, and the advisory is deterministic
    assert c.app.state.env.service.store.version == version and c.app.state.env.desk.calls == []


def test_the_route_obeys_the_kill_switch_the_role_allowlist_and_strict_input():
    disabled = client(policy=Policy.load().model_copy(update={"enabled": False}))
    refused = disabled.post(ADVISORY, json=body())
    assert refused.status_code == 403 and "kill switch" in refused.json()["detail"]
    wrong_role = client().post(ADVISORY, json={**body(), "roles": ["intern"]})
    assert wrong_role.status_code == 403 and wrong_role.json()["error"] == "CALLER_NOT_PERMITTED"
    bad = client().post(ADVISORY, json={**body(), "dev_reset_first": "yes"})
    assert bad.status_code == 422 and bad.json()["fields"] == ["dev_reset_first"]


def test_a_blocked_case_still_gets_a_complete_advisory():
    data = client().post(ADVISORY, json=body("04_offer_not_accepted")).json()
    assert data["result"]["overall_status"] == "BLOCKED" and "BLOCKED" in data["advisory"]["formatted_explanation"]


def test_the_route_is_documented_in_openapi():
    spec = client().get("/openapi.json").json()
    operation = spec["paths"][ADVISORY]["post"]
    assert operation["tags"] and "advisory" in operation["summary"].lower()
    assert {"200", "403", "422"} <= set(operation["responses"])
    assert "AdvisoryReport" in spec["components"]["schemas"] and "EvaluateWithAdvisoryResponse" in spec["components"]["schemas"]
