"""All three brain tasks reached through the real application flow.

tests/test_llm.py proves each task is *invoked somewhere in the source*
by grepping. That is a static check: it would still pass if the call
site were unreachable in practice. These drive the HTTP API end to end
with the fake model enabled and assert the audit actions that only
appear when a task actually ran.

Nothing here contacts a provider. FakeLlm is the only client that
exists; it calls nothing and needs no key.
"""

import socket

import pytest
from fastapi.testclient import TestClient

from sub_agent.api.app import create_app
from sub_agent.api.container import build_container
from sub_agent.api.settings import Settings
from sub_agent.llm import FakeLlm

CLIENT_ID = "acme"
OFFICER = "officer-1"
SYSTEM_KEY = "test-system-key-not-for-prod"
SALARY = "Base: 118000 SYNTHETIC/yr"
OTHER_SALARY = "Base: 999999 OTHER/yr"
EMAIL = "asha.varma@example.invalid"

SYS = {"X-System-Key": SYSTEM_KEY}
OFF = {"X-User-Id": OFFICER, "X-User-Role": "HR_OFFICER", "X-Client-Ids": CLIENT_ID}
MGR = {"X-User-Id": "manager-1", "X-User-Role": "HR_MANAGER", "X-Client-Ids": CLIENT_ID}


def _app(llm_enabled: bool):
    container = build_container(
        Settings(
            app_env="dev",
            dedup_hmac_key="test-fixed-fingerprint-key",
            system_api_key=SYSTEM_KEY,
            llm_enabled=llm_enabled,
        )
    )
    return TestClient(create_app(container)), container


def _form_body(**overrides) -> dict:
    body = dict(
        client_id=CLIENT_ID,
        hr_officer_id=OFFICER,
        offer_accepted=True,
        candidate_name="Asha Varma",
        candidate_email=EMAIL,
        designation="Software Engineer",
        location_name="Synthetic Office, Dubai",
        joining_date="2026-04-01",
        work_location_type="OFFICE",
        salary_and_terms=SALARY,
        form_status="APPROVED",
        approved_by="manager-1",
        approved_at="2026-01-10T09:00:00Z",
    )
    body.update(overrides)
    return body


def _audit_actions(client, case_id) -> list[str]:
    response = client.get(f"/cases/{case_id}/audit", headers=MGR)
    assert response.status_code == 200
    return [e["action"] for e in response.json()]


def _open_case(client, **overrides) -> str:
    response = client.post("/internal/onboarding-requests", headers=SYS, json=_form_body(**overrides))
    assert response.status_code == 200, response.text
    return response.json()["case_id"]


# ---------------------------------------------------------------------
# intake_intent, through POST /internal/inbound-email
# ---------------------------------------------------------------------


def _seed_offer(client, offer_ref: str, email: str) -> None:
    response = client.post(
        "/dev/seed",
        headers=SYS,
        json={**_form_body(candidate_email=email), "offer_ref": offer_ref},
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("llm_enabled,expected", [(True, "NEEDS_HR_REVIEW"), (False, "CASE_OPENED")])
def test_intake_intent_runs_over_http(llm_enabled, expected):
    """"accept, but timeline?" contains the word "accept", so the deterministic
    rules let it through. Only the advisory model escalates it - which
    makes the outcome a direct observation of whether it ran."""
    client, _ = _app(llm_enabled)
    _seed_offer(client, "OFFER-7001", EMAIL)

    response = client.post(
        "/internal/inbound-email",
        headers=SYS,
        json={
            "raw_email": f"From: {EMAIL}\nSubject: Re: offer [OFFER-7001] - accept, but timeline?\n\nbody\n"
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == expected
    if llm_enabled:
        assert body["review_code"] == "MODEL_FLAGGED_FOR_REVIEW"


# ---------------------------------------------------------------------
# discrepancy_summary, through POST /cases/{id}/compare
# ---------------------------------------------------------------------


def test_discrepancy_summary_is_drafted_and_audited_over_http():
    client, _ = _app(llm_enabled=True)
    case_id = _open_case(client)

    response = client.post(
        f"/cases/{case_id}/compare",
        headers=OFF,
        json={
            "snapshots": [
                {
                    "source": "hr_system",
                    "values": {"salary_and_terms": OTHER_SALARY, "designation": "Senior Engineer"},
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["new_discrepancies"]) == 2

    assert "DISCREPANCY_SUMMARY_DRAFTED" in _audit_actions(client, case_id)


def test_no_summary_is_drafted_when_the_model_is_disabled():
    client, _ = _app(llm_enabled=False)
    case_id = _open_case(client)

    client.post(
        f"/cases/{case_id}/compare",
        headers=OFF,
        json={"snapshots": [{"source": "hr_system", "values": {"designation": "Senior Engineer"}}]},
    )

    actions = _audit_actions(client, case_id)
    assert "DISCREPANCY_FLAGGED" in actions  # the business rule still runs
    assert "DISCREPANCY_SUMMARY_DRAFTED" not in actions


def test_the_drafted_summary_leaks_no_value_over_http():
    """The guardrail, observed through the API a caller actually reads."""
    client, _ = _app(llm_enabled=True)
    case_id = _open_case(client)

    client.post(
        f"/cases/{case_id}/compare",
        headers=OFF,
        json={"snapshots": [{"source": "hr_system", "values": {"salary_and_terms": OTHER_SALARY}}]},
    )

    raw = client.get(f"/cases/{case_id}/audit", headers=MGR).text
    assert "DISCREPANCY_SUMMARY_DRAFTED" in raw
    for secret in ("999999", "118000", "Base:", EMAIL):
        assert secret not in raw, f"{secret!r} leaked into the audit trail"


# ---------------------------------------------------------------------
# duplicate_explanation, through POST /internal/onboarding-requests
# ---------------------------------------------------------------------


def test_duplicate_explanation_is_drafted_and_audited_over_http():
    """A second joiner with the same name but a different email is a
    name-only match: an HC_12 notice for HR, which is what the
    explanation is drafted for."""
    client, _ = _app(llm_enabled=True)
    _open_case(client)
    second = _open_case(client, candidate_email="different.person@example.invalid")

    actions = _audit_actions(client, second)
    assert "APPROVAL_REQUESTED" in actions
    assert "VARIANT_EXPLANATION_DRAFTED" in actions


def test_no_explanation_is_drafted_when_the_model_is_disabled():
    client, _ = _app(llm_enabled=False)
    _open_case(client)
    second = _open_case(client, candidate_email="different.person@example.invalid")

    actions = _audit_actions(client, second)
    assert "APPROVAL_REQUESTED" in actions  # the notice still reaches HR
    assert "VARIANT_EXPLANATION_DRAFTED" not in actions


def test_the_explanation_names_no_person_over_http():
    client, _ = _app(llm_enabled=True)
    _open_case(client)
    second = _open_case(client, candidate_email="different.person@example.invalid")

    raw = client.get(f"/cases/{second}/audit", headers=MGR).text
    assert "VARIANT_EXPLANATION_DRAFTED" in raw
    for secret in ("different.person@example.invalid", "Asha Varma", "118000"):
        assert secret not in raw, f"{secret!r} leaked into the audit trail"


# ---------------------------------------------------------------------
# No provider, no key, no network
# ---------------------------------------------------------------------


def test_the_flow_runs_with_every_outbound_socket_blocked(monkeypatch):
    """FakeLlm is the development/testing implementation and must reach
    nothing. Any outbound connection attempt fails loudly here.

    Driven through the service layer rather than TestClient: TestClient
    opens its own loopback socketpair on Windows, which would trip the
    block for a reason that has nothing to do with the model.
    """

    def _blocked(*args, **kwargs):
        raise AssertionError("network access attempted with FakeLlm enabled")

    from sub_agent.compare_sources import DiscrepancyDecision
    from sub_agent.models import Role, SourceSnapshot, Viewer
    from sub_agent.onboarding_entry import OpenCaseRequest, open_onboarding_case

    client, container = _app(llm_enabled=True)
    case_id = _open_case(client)  # set up before the block goes on

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)

    viewer = Viewer(user_id=OFFICER, role=Role.HR_OFFICER, client_ids=frozenset({CLIENT_ID}))
    result = container.service.compare_sources(
        viewer,
        case_id,
        [SourceSnapshot(source="hr_system", values={"designation": "Senior Engineer"})],
    )

    assert len(result.new_discrepancies) == 1
    actions = [e.action for e in container.deps.audit_log.entries_for_case(case_id)]
    assert "DISCREPANCY_SUMMARY_DRAFTED" in actions
    assert "discrepancy_summary" in container.intake_deps.llm.calls


def test_the_model_needs_no_api_key_or_provider_setting():
    """Enabling the model must not require any new configuration beyond
    the flag itself."""
    settings = Settings(app_env="dev", llm_enabled=True)
    container = build_container(settings)

    assert isinstance(container.intake_deps.llm, FakeLlm)
    assert not any("key" in f or "token" in f for f in Settings.model_fields if "llm" in f)


def test_one_shared_client_is_injected_everywhere():
    """Intake, onboarding and comparison must share one instance, so a
    test or operator inspecting `calls` sees every task."""
    container = build_container(Settings(app_env="dev", llm_enabled=True))

    intake = container.intake_deps.llm
    assert intake is container.onboarding_deps.llm
    assert intake is container.service._compare_deps.llm
    assert sorted(intake._tasks) == [
        "discrepancy_summary",
        "duplicate_explanation",
        "intake_intent",
        "responsibilities_review",
    ]
    assert intake is container.documents._llm
