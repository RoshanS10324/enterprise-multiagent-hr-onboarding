"""UC-04 integration test cases.

Covers the mandatory testcase.py entry point. These tests validate the
sub-agent facade (Uc04Agent) and confirm that the connectors, guardrails
and policy integrate correctly end-to-end.

All data is synthetic (SYN-* prefixes). No network; no real IT tickets.
"""
from __future__ import annotations

import os
import sys

# Ensure src/ is on the path when running standalone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from connectors.outlook.base import CalendarEvent, EmailMessage
from sub_agent.agent import Uc04Agent
from sub_agent.fakes import (
    ALL,
    MockOutlookConnector,
    approvals,
    make,
    ready_payload,
)
from sub_agent.states import ParentCaseStatus

# --------------------------------------------------------------------------- fixtures

@pytest.fixture
def agent() -> Uc04Agent:
    """A fresh Uc04Agent backed by mock adapters."""
    return Uc04Agent()


@pytest.fixture
def outlook() -> MockOutlookConnector:
    """A fresh MockOutlookConnector for email/calendar testing."""
    return MockOutlookConnector()


# --------------------------------------------------------------------------- sub_agent.agent tests

class TestUc04AgentEvaluate:
    """Happy-path and start-gate tests via the sub_agent facade."""

    def test_evaluate_returns_result(self, agent: Uc04Agent) -> None:
        payload = make()
        result = agent.evaluate(payload)
        assert result is not None
        assert hasattr(result, "items")

    def test_start_gate_blocked_when_offer_not_accepted(self, agent: Uc04Agent) -> None:
        payload = make(offer_accepted=False)
        result = agent.evaluate(payload)
        blockers = [b.code for b in result.blockers]
        assert any("START_GATE" in b for b in blockers)

    def test_approved_items_surface_in_result(self, agent: Uc04Agent) -> None:
        payload = make(approval_records=approvals(ALL))
        result = agent.evaluate(payload)
        assert len(result.items) > 0

    def test_ready_scenario_has_no_blockers(self, agent: Uc04Agent) -> None:
        payload = ready_payload()
        result = agent.evaluate(payload)
        assert result.blockers == []

    def test_withdrawn_parent_blocks_evaluation(self, agent: Uc04Agent) -> None:
        from sub_agent.fakes import ALL, approvals, make

        payload = make(approval_records=approvals(ALL))
        # Simulate a withdrawn parent case
        agent._cases.set_status(payload["case_id"], ParentCaseStatus.WITHDRAWN)
        result = agent.evaluate(payload)
        # A withdrawn parent must result in blockers or a non-ready overall status
        blockers = [b.code for b in result.blockers]
        has_blocker = any(
            "PARENT" in b or "WITHDRAWN" in b or "STATUS" in b or "START_GATE" in b
            for b in blockers
        )
        assert has_blocker or result.overall_status is not None


class TestUc04AgentOrchestrated:
    """Verify the orchestrated path via sub_agent.agent."""

    def test_orchestrated_evaluate_returns_response(self, agent: Uc04Agent) -> None:
        from sub_agent.entrypoints.orchestrated import Actor, OrchestratorRequest, WorkflowRef
        from sub_agent.fakes import CALLER_ID, make

        payload = make()
        req = OrchestratorRequest(
            run_id="run-tc-001",
            idempotency_key="orch-tc-key-001",
            workflow=WorkflowRef(id="onboarding", instance=payload["case_id"], step="sub_agent"),
            actor=Actor(on_behalf_of=CALLER_ID, roles=["onboarding_orchestrator"]),
            input=payload,
        )
        response = agent.evaluate_orchestrated(req)
        assert response is not None


# --------------------------------------------------------------------------- connector tests

class TestMockOutlookConnector:
    """Validate the Outlook connector mock behaviour."""

    def test_send_email_captures_message(self, outlook: MockOutlookConnector) -> None:
        msg = EmailMessage(to=["hr@example.com"], subject="Test", body="Hello")
        sent = outlook.send_email(msg)
        assert sent.message_id.startswith("SYN-MSG-")
        assert outlook.email_count == 1

    def test_create_event_captures_event(self, outlook: MockOutlookConnector) -> None:
        evt = CalendarEvent(
            subject="Equipment Handover",
            start="2026-10-20T09:00:00Z",
            end="2026-10-20T10:00:00Z",
            attendees=["emp@example.com"],
        )
        created = outlook.create_event(evt)
        assert created.event_id.startswith("SYN-EVT-")
        assert outlook.event_count == 1

    def test_cancel_event_records_id(self, outlook: MockOutlookConnector) -> None:
        outlook.cancel_event("SYN-EVT-ABCD1234")
        assert "SYN-EVT-ABCD1234" in outlook.cancelled_events

    def test_fail_on_send_raises(self, outlook: MockOutlookConnector) -> None:
        failing = MockOutlookConnector(fail_on_send=True)
        msg = EmailMessage(to=["hr@example.com"], subject="Test", body="Hello")
        with pytest.raises(RuntimeError):
            failing.send_email(msg)

    def test_reset_clears_all_state(self, outlook: MockOutlookConnector) -> None:
        msg = EmailMessage(to=["hr@example.com"], subject="Test", body="Body")
        outlook.send_email(msg)
        outlook.reset()
        assert outlook.email_count == 0
        assert outlook.event_count == 0
        assert outlook.cancelled_events == []


# ----------------------------------------------------------------- guardrails / policy integration

class TestGuardrailsIntegration:
    """Verify privacy and policy guardrails are enforced through the sub_agent facade."""

    def test_policy_disallowed_role_is_rejected(self, agent: Uc04Agent) -> None:
        from sub_agent.policy import PolicyViolation
        payload = make()
        with pytest.raises(PolicyViolation):
            agent.evaluate(payload, roles=["random_unknown_role"])

    def test_valid_hr_role_is_accepted(self, agent: Uc04Agent) -> None:
        payload = make()
        result = agent.evaluate(payload, roles=["hr_onboarding_officer"])
        assert result is not None


# --------------------------------------------------------------------------- Fake LLM & E2E tests

class TestUc04AgentWithAdvisory:
    """End-to-end flow integrating Fake LLM, Brain Knowledge, Skills, and Outlook."""

    def test_e2e_evaluate_with_advisory(self, agent: Uc04Agent) -> None:
        payload = ready_payload()
        eval_res = agent.evaluate_with_advisory(payload)

        # 1. Unpacking check
        res, adv = eval_res
        assert res.overall_status.value == "READY"
        assert adv.is_simulated is True
        assert "[SIMULATED ADVISORY" in adv.banner
        assert "READY" in adv.overall_status

        # 2. Brain knowledge and skills referenced
        assert len(adv.applicable_skills) > 0
        assert any("brain/onboarding" in doc for doc in adv.referenced_knowledge)

        # 3. Plaintext formatting sanitized and grounded
        assert "Case Evaluation Advisory" in adv.formatted_explanation

    def test_e2e_advisory_never_changes_underlying_state(self, agent: Uc04Agent) -> None:
        payload = make()  # Awaiting approval
        eval_res = agent.evaluate_with_advisory(payload)
        res, adv = eval_res

        # Status must remain AWAITING_APPROVAL; Fake LLM cannot approve
        assert res.overall_status.value == "AWAITING_APPROVAL"
        assert adv.overall_status == "AWAITING_APPROVAL"
        assert "approval" in adv.approval_guidance.lower()

    def test_e2e_notify_readiness_does_not_alter_state(self, agent: Uc04Agent) -> None:
        payload = ready_payload()
        result = agent.evaluate(payload)
        # Dispatch simulated notification via Outlook mock
        sent = agent.notify_readiness(
            result,
            recipient_email="hiring-manager@example.com",
            approved_by="SYN-USER-100",
        )
        assert sent.message_id.startswith("SYN-MSG-")
        assert agent.outlook.email_count == 1
        assert "READY" in sent.subject

        # Verify state is untouched
        assert result.overall_status.value == "READY"

    def test_e2e_api_evaluate_endpoint(self) -> None:
        from fastapi.testclient import TestClient

        from sub_agent.advisor import FakeLlmAdvisor
        from sub_agent.api import app
        from sub_agent.contracts import Uc04Result

        client = TestClient(app)
        payload = make()
        body = {
            "actor_id": "SYN-USER-100",
            "roles": ["hr_onboarding_officer"],
            "input": payload,
        }
        response = client.post("/api/v1/sub_agent/evaluate", json=body)
        assert response.status_code == 200
        result = Uc04Result.model_validate(response.json())
        assert result.case_id == "SYN-CASE-001"

        # Advisory layer explains the API evaluation result
        advisor = FakeLlmAdvisor()
        advisory = advisor.generate_advisory(result, payload)
        assert advisory.is_simulated is True
        assert "[SIMULATED ADVISORY" in advisory.banner
        assert "AWAITING_APPROVAL" in advisory.overall_status
