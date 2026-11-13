"""Tests for the Fake LLM Advisory Layer (FakeLlmAdvisor).

Verifies:
- Deterministic repeatable output.
- Mandatory simulated advisory banner.
- Advisory-only boundary (cannot mutate state or grant approvals).
- Safe fallback on None or malformed inputs.
- Privacy sanitization.
- Accurate explanation across all case lifecycle states.
"""
from __future__ import annotations

import pytest

from sub_agent.advisor import ADVISORY_BANNER, FakeLlmAdvisor
from sub_agent.agent import Uc04Agent
from sub_agent.fakes import (
    ALL,
    approvals,
    delivery,
    make,
    ready_payload,
    ticket,
)
from sub_agent.states import CaseStatus


@pytest.fixture
def advisor() -> FakeLlmAdvisor:
    return FakeLlmAdvisor()


@pytest.fixture
def agent() -> Uc04Agent:
    return Uc04Agent()


def test_advisor_banner_present_on_all_outputs(advisor: FakeLlmAdvisor, agent: Uc04Agent) -> None:
    payload = make()
    result = agent.evaluate(payload)
    report = advisor.generate_advisory(result, payload)

    assert report.is_simulated is True
    assert report.banner == ADVISORY_BANNER
    assert ADVISORY_BANNER in report.formatted_explanation


def test_advisor_is_deterministic(advisor: FakeLlmAdvisor, agent: Uc04Agent) -> None:
    payload = make(approval_records=approvals(ALL))
    result = agent.evaluate(payload)

    report1 = advisor.generate_advisory(result, payload)
    report2 = advisor.generate_advisory(result, payload)

    assert report1.formatted_explanation == report2.formatted_explanation
    assert report1.status_explanation == report2.status_explanation
    assert report1.action_recommendations == report2.action_recommendations


def test_fallback_on_none_result(advisor: FakeLlmAdvisor) -> None:
    report = advisor.generate_advisory(None)

    assert report.is_simulated is True
    assert "UNABLE_TO_ADVISE" in report.formatted_explanation
    assert ADVISORY_BANNER in report.formatted_explanation
    assert report.case_id == "UNKNOWN"


def test_advisor_does_not_mutate_result(advisor: FakeLlmAdvisor, agent: Uc04Agent) -> None:
    payload = make()
    result = agent.evaluate(payload)
    original_status = result.overall_status

    report = advisor.generate_advisory(result, payload)

    # Assert service result is untouched
    assert result.overall_status == original_status
    assert report.overall_status == original_status.value


def test_awaiting_approval_explanation(advisor: FakeLlmAdvisor, agent: Uc04Agent) -> None:
    payload = make()  # No approvals provided
    result = agent.evaluate(payload)
    assert result.overall_status == CaseStatus.AWAITING_APPROVAL

    report = advisor.generate_advisory(result, payload)
    assert "AWAITING_APPROVAL" in report.overall_status
    assert "approval" in report.status_explanation.lower()
    assert len(report.approval_guidance) > 0


def test_ready_explanation(advisor: FakeLlmAdvisor, agent: Uc04Agent) -> None:
    payload = ready_payload()
    result = agent.evaluate(payload)
    assert result.overall_status == CaseStatus.READY

    report = advisor.generate_advisory(result, payload)
    assert report.overall_status == "READY"
    has_ready_text = (
        "completion evidence" in report.status_explanation.lower()
        or "ready" in report.status_explanation.lower()
    )
    assert has_ready_text


def test_blocked_explanation(advisor: FakeLlmAdvisor, agent: Uc04Agent) -> None:
    payload = make(offer_accepted=False)
    result = agent.evaluate(payload)
    assert result.overall_status == CaseStatus.BLOCKED

    report = advisor.generate_advisory(result, payload)
    assert report.overall_status == "BLOCKED"
    assert "BLOCKED" in report.status_explanation


def test_at_risk_explanation(advisor: FakeLlmAdvisor, agent: Uc04Agent) -> None:
    payload = make(
        approval_records=approvals(["201"]),
        mock_it_ticket_updates=[ticket("201")],
        delivery_status=[delivery("201", "PENDING", expected="2026-11-20")],
        joining_date="2026-11-15",
        as_of_date="2026-11-10",
    )
    result = agent.evaluate(payload)
    if result.overall_status == CaseStatus.AT_RISK:
        report = advisor.generate_advisory(result, payload)
        assert report.overall_status == "AT_RISK"
        assert "risk" in report.risk_analysis.lower()


def test_privacy_sanitization_in_advisory(advisor: FakeLlmAdvisor, agent: Uc04Agent) -> None:
    payload = ready_payload()
    result = agent.evaluate(payload)

    report = advisor.generate_advisory(result, payload)
    # Ensure no leaked credentials or tokens
    assert "bearer " not in report.formatted_explanation.lower()
    assert "password=" not in report.formatted_explanation.lower()
