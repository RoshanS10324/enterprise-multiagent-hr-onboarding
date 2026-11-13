"""Executes every documented Swagger scenario (src/sub_agent.api/scenarios.py) through the real service and asserts its stated
expectation, so the Swagger examples, the tag descriptions and docs/SWAGGER_TEST_CATALOG.md are all backed by behaviour that
actually ran. In-process, synthetic data only; a fixture fails the test on any non-loopback network use."""

import socket

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sub_agent.contracts import Uc04Result  # noqa: E402
from sub_agent.policy import Policy  # noqa: E402
from sub_agent.api.app import create_app  # noqa: E402
from sub_agent.api.catalog import CATALOG_PATH, how_to_steps, render_catalog  # noqa: E402
from sub_agent.api.examples import EVALUATE_EXAMPLES, ORCHESTRATED_EXAMPLES, STATUS_EXAMPLES, SUBMIT_EXAMPLES  # noqa: E402
from sub_agent.api.scenarios import (BY_KEY, PATHS, REDACTED_MARKER, RESET_PATH, SCENARIOS, STATUS_PATH, TAGS, chain,  # noqa: E402
                                operation_tags)

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
REQUIRED_TAGS = ["01 - Basic Agent Evaluation", "02 - Approvals & Exceptions", "03 - Equipment & Access Rules",
                 "04 - Lifecycle & Readiness", "05 - IT Service Desk Simulation", "06 - Orchestration",
                 "07 - Security & Policy", "08 - State & Idempotency"]


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        assert host in LOOPBACK, f"outbound network connection attempted: {host}"
        return real_connect(self, address)

    def refuse_dns(host, *args, **kwargs):
        raise AssertionError(f"DNS lookup attempted: {host}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", refuse_dns)


def new_client(**kwargs):
    return TestClient(create_app(**kwargs), raise_server_exceptions=False)


def execute(client, scenario):
    """Runs the scenario's whole chain as a person would in Swagger. Returns (final response, previous step's response)."""
    previous = response = None
    for step_scenario in chain(scenario):
        for step in step_scenario.prep:
            if step.kind == "reset":
                assert client.post(RESET_PATH).status_code == 200
            else:
                done = client.put(STATUS_PATH.format(case_id=step.case_id), json={"status": step.status})
                assert done.status_code == 200 and done.json()["status"] == step.status
        previous, response = response, client.post(PATHS[step_scenario.endpoint], json=step_scenario.body)
    return response, previous


def assert_expect(response, expect, previous=None):
    body = response.json()
    if "error" in expect:
        assert body["error"] == expect["error"], body
        if "fields" in expect:
            assert body["fields"] == sorted(expect["fields"])
    else:
        result = body.get("result", body)
        codes = lambda rows: {r["code"] for r in rows}  # noqa: E731
        if "overall" in expect:
            assert result["overall_status"] == expect["overall"]
        if "blockers" in expect:
            assert codes(result["blockers"]) == set(expect["blockers"])
        if "risks" in expect:
            assert codes(result["risks"]) == set(expect["risks"])
        if "rejected" in expect:
            assert [r["reason_code"] for r in result["rejected_approvals"]] == expect["rejected"]
        if "items" in expect:
            states = {i["request_key"][-3:]: i["state"] for i in result["items"]}
            if expect["items"]:
                assert {k: states.get(k) for k in expect["items"]} == expect["items"]
            else:
                assert result["items"] == []
        if "exceptions" in expect:
            assert sorted(i["request_key"][-3:] for i in result["items"] if i["exception_required"]) == sorted(expect["exceptions"])
        if "duplicates" in expect:
            assert [d["reason_code"] for d in result["duplicates_ignored"]] == expect["duplicates"]
        if "proposals" in expect:
            assert {p["proposal_type"] for p in result["proposals"]} == set(expect["proposals"])
            assert all(p["executed"] is False for p in result["proposals"])
        if "drafts" in expect:
            assert {d["proposal_type"] for d in result["cancellation_drafts"]} == set(expect["drafts"])
            assert all(d["executed"] is False for d in result["cancellation_drafts"])
        if "submitted" in expect:
            assert len(body["submitted"]) == expect["submitted"] and body["failed"] == []
        if "api_status" in expect:
            assert body["status"] == expect["api_status"]
        if "api_exceptions" in expect:
            assert [e["code"] for e in body["exceptions"]] == expect["api_exceptions"]
        if "api_proposals" in expect:
            assert [p["type"] for p in body["proposals"]] == expect["api_proposals"]
            assert all(p["requires_approval_by"] for p in body["proposals"])
        if "reassessment" in expect:
            assert result["reassessment"]["required"] is True and result["reassessment"]["changed_fields"] == expect["reassessment"]
        if "redacted" in expect:
            assert any(REDACTED_MARKER in b["reason"] for b in result["blockers"])
        if "audit_has" in expect:
            assert set(expect["audit_has"]) <= {e["event_type"] for e in result["audit_events"]}
        if "next_owner" in expect:
            assert expect["next_owner"] in {a["owner"] for a in result["next_actions"]}
        if "remote_country" in expect:
            assert result["remote_plan"]["country_code"] == expect["remote_country"]
    if expect.get("same_as_previous"):
        assert previous is not None and response.json() == previous.json()
    for text in expect.get("absent", []):
        assert text not in response.text.lower()


# --------------------------------------------------------------------------- every scenario, each from a clean client
@pytest.mark.parametrize("key", [s.key for s in SCENARIOS])
def test_scenario_gives_its_documented_outcome(key):
    scenario = BY_KEY[key]
    response, previous = execute(new_client(), scenario)
    assert response.status_code == scenario.http, response.text
    assert_expect(response, scenario.expect, previous)


def test_examples_do_not_interfere_when_run_one_after_another_on_one_server():
    """The Swagger promise: any example can be run right after any other, on the same running server."""
    client = new_client()
    for scenario in SCENARIOS:
        response, previous = execute(client, scenario)
        assert response.status_code == scenario.http, (scenario.key, response.text)
        assert_expect(response, scenario.expect, previous)


def test_every_scenario_starts_from_a_clean_state():
    for scenario in SCENARIOS:
        first = chain(scenario)[0]
        assert first.body.get("dev_reset_first") is True or any(p.kind == "reset" for p in first.prep), first.key


# --------------------------------------------------------------------------- documentation completeness
def test_every_scenario_documents_all_required_fields():
    for s in SCENARIOS:
        for field in ("title", "purpose", "fixture", "result", "inspect"):
            assert getattr(s, field).strip(), (s.key, field)
        assert s.http in (200, 403, 404, 409, 422) and s.expect and s.endpoint in PATHS
        assert how_to_steps(s) and s.label.startswith(s.tag)


def test_each_swagger_example_carries_the_six_documentation_fields():
    for examples in (EVALUATE_EXAMPLES, SUBMIT_EXAMPLES, ORCHESTRATED_EXAMPLES):
        for example in examples.values():
            for heading in ("**Purpose:**", "**Input fixture:**", "**How to run:**", "**Expected HTTP:**",
                            "**Expected result:**", "**Inspect:**"):
                assert heading in example["description"], example["summary"]


def test_catalog_file_matches_the_registry():
    assert CATALOG_PATH.read_text(encoding="utf-8") == render_catalog(), "run: python -m sub_agent.api.catalog"


def test_every_capability_requested_has_a_scenario():
    titles = " | ".join(s.title.lower() for s in SCENARIOS)
    for needle in ("valid onboarding evaluation", "missing required field", "wrong role", "outside the role list",
                   "missing access matrix", "on_hold", "withdrawn", "delayed", "submit all approved", "idempotent replay",
                   "idempotency key reused", "without an allowed role", "redacted", "duplicate request key"):
        assert needle in titles, needle
    assert {s.endpoint for s in SCENARIOS} == {"evaluate", "submit", "orchestrated"}


# --------------------------------------------------------------------------- tags: organised, not duplicated
def test_swagger_has_exactly_the_eight_required_tags_in_order():
    spec = new_client().get("/openapi.json").json()
    assert [t["name"] for t in spec["tags"]] == REQUIRED_TAGS == [t["name"] for t in TAGS.values()]
    assert all(t["description"] for t in spec["tags"])
    used = {tag for ops in spec["paths"].values() for op in ops.values() for tag in op["tags"]}
    assert used == set(REQUIRED_TAGS)


def test_existing_endpoints_are_reused_not_duplicated():
    spec = new_client().get("/openapi.json").json()
    assert set(spec["paths"]) == {"/", "/health", PATHS["evaluate"], PATHS["submit"], PATHS["orchestrated"],
                                  "/api/v1/sub_agent/evaluate-with-advisory",     # the advisory route; behaviour tested in test_advisory_api.py
                                  "/api/v1/dev/mock-parent-cases", STATUS_PATH, RESET_PATH}
    assert operation_tags("evaluate") == [t for t in REQUIRED_TAGS if t[:2] in {"01", "02", "03", "04", "05", "07", "08"}]
    assert operation_tags("submit") == [REQUIRED_TAGS[4]] and operation_tags("orchestrated") == [REQUIRED_TAGS[5], REQUIRED_TAGS[6]]


def test_dev_only_operations_are_labelled_and_the_status_endpoint_has_examples():
    spec = new_client().get("/openapi.json").json()
    for path in ("/api/v1/dev/mock-parent-cases", STATUS_PATH, RESET_PATH):
        for operation in spec["paths"][path].values():
            assert operation["summary"].startswith("DEV ONLY - ")
    examples = spec["paths"][STATUS_PATH]["put"]["requestBody"]["content"]["application/json"]["examples"]
    assert set(examples) == set(STATUS_EXAMPLES) == {"on_hold", "withdrawn", "cancelled", "active"}
    assert "DEV ONLY" in spec["components"]["schemas"]["EvaluateRequest"]["properties"]["dev_reset_first"]["description"]


# --------------------------------------------------------------------------- direct versus orchestrated, same facts
@pytest.mark.parametrize("key", [s.key for s in SCENARIOS if s.compare_with])
def test_direct_and_orchestrated_examples_decide_the_same(key):
    orchestrated, other = BY_KEY[key], BY_KEY[BY_KEY[key].compare_with]
    a = Uc04Result.model_validate(execute(new_client(), orchestrated)[0].json()["result"])
    b = Uc04Result.model_validate(execute(new_client(), other)[0].json())
    assert a.business_decision() == b.business_decision()
    assert a.caller["invocation_mode"] == "PARENT_ORCHESTRATED" and b.caller["invocation_mode"] == "DIRECT"


# --------------------------------------------------------------------------- DEV ONLY controls
def test_dev_reset_first_clears_state_only_when_asked():
    client = new_client()
    first = BY_KEY["08_replay_first_call"].body
    assert client.post(PATHS["evaluate"], json=first).status_code == 200
    changed = {**first, "input": {**first["input"], "offer_accepted": False}, "dev_reset_first": False}
    assert client.post(PATHS["evaluate"], json=changed).status_code == 409           # state kept: key conflict
    assert client.post(PATHS["evaluate"], json={**changed, "dev_reset_first": True}).status_code == 200   # state cleared


def test_dev_reset_flag_also_resets_parent_statuses():
    client = new_client()
    client.put(STATUS_PATH.format(case_id="SYN-CASE-001"), json={"status": "ON_HOLD"})
    result = client.post(PATHS["evaluate"], json=BY_KEY["01_valid_awaiting_approval"].body).json()
    assert result["overall_status"] == "AWAITING_APPROVAL"          # the example's reset restored ACTIVE
    assert client.get("/api/v1/dev/mock-parent-cases").json()[0]["status"] == "ACTIVE"


def test_dev_tools_can_be_switched_off():
    client = new_client(dev_tools=False)
    spec = client.get("/openapi.json").json()
    assert not [p for p in spec["paths"] if "/dev/" in p]
    assert client.post(RESET_PATH).status_code == 404
    assert client.put(STATUS_PATH.format(case_id="SYN-CASE-001"), json={"status": "ON_HOLD"}).status_code == 404
    refused = client.post(PATHS["evaluate"], json=BY_KEY["01_valid_awaiting_approval"].body)   # has dev_reset_first = true
    assert refused.status_code == 403 and refused.json()["error"] == "DEV_TOOLS_DISABLED"
    plain = {**BY_KEY["01_valid_awaiting_approval"].body, "dev_reset_first": False}
    assert client.post(PATHS["evaluate"], json=plain).status_code == 200


def test_the_orchestrator_envelope_accepts_the_reset_flag_but_stays_unchanged_without_it():
    client = new_client()
    plain = {k: v for k, v in BY_KEY["06_orchestrated_valid"].body.items() if k != "dev_reset_first"}
    assert client.post(PATHS["orchestrated"], json=plain).status_code == 200     # the original envelope shape still works


# --------------------------------------------------------------------------- limitations are real, not fabricated
def test_documented_limitations_hold():
    """The kill switch cannot be toggled over HTTP: it is configured at start-up, so it is tested here and documented, not faked."""
    assert all(TAGS[t]["limits"] for t in ("05", "07"))
    disabled = new_client(policy=Policy.load().model_copy(update={"enabled": False}))
    assert disabled.get("/health").json()["policy_enabled"] is False
    response = disabled.post(PATHS["evaluate"], json=BY_KEY["01_valid_awaiting_approval"].body)
    assert response.status_code == 403 and "kill switch" in response.json()["detail"]
    assert not [s for s in SCENARIOS if "kill" in s.title.lower()]          # no HTTP scenario pretends to switch it
