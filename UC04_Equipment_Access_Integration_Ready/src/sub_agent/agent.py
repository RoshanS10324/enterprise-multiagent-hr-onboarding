"""UC-04 sub-agent entry point.

This module is the single integration surface between the parent orchestrator
and the UC-04 Equipment & Approved Access Management agent. It wraps the
existing entry points (DirectEntry / OrchestratedEntry) so the parent can
call a uniform interface regardless of the invocation mode.

Usage (direct / standalone):
    from sub_agent.agent import Uc04Agent
    agent = Uc04Agent()
    result = agent.evaluate(payload)

Usage (parent-orchestrated):
    from sub_agent.agent import Uc04Agent
    agent = Uc04Agent()
    result = agent.evaluate_orchestrated(orchestrator_request)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from connectors.outlook.base import EmailMessage, SentMessage
from connectors.outlook.mock import MockOutlookConnector
from sub_agent.adapters.case_context_mock import MockCaseContext
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk
from sub_agent.advisor import AdvisoryReport, FakeLlmAdvisor
from sub_agent.advisory_providers import AdvisorPort
from sub_agent.cohort import CohortView, cohort_view
from sub_agent.audit import AuditLog, system_clock
from sub_agent.contracts import USER_REF, SubmissionReport, Uc04Input, Uc04Result
from sub_agent.direct import DirectEntry
from sub_agent.fixtures import new_environment
from sub_agent.knowledge import BrainKnowledgeLoader
from sub_agent.orchestrated import (AcknowledgementOut, CaseOperationRequest, OperationEntry, OrchestratedEntry, OrchestratorRequest,
                                    OrchestratorResponse)
from sub_agent.policy import PolicyViolation
from sub_agent.ports import AgentRunRecorderPort, AuditSinkPort
from sub_agent.service import Uc04Service
from sub_agent.skills import SkillRegistry
from sub_agent.store import CaseRepository


@dataclass
class AgentEvaluationResult:
    """Composite container holding deterministic Uc04Result and advisory Fake LLM report."""

    result: Uc04Result
    advisory: AdvisoryReport

    def __iter__(self):
        return iter((self.result, self.advisory))


class Uc04Agent:
    """Facade used by the parent orchestrator, API, and integration tests.

    Integrates:
    - Authoritative deterministic UC-04 service and validation logic
    - Fake LLM Advisory Layer (strictly advisory, read-only)
    - Brain Knowledge Loader for curated UC-04 knowledge
    - Skill Registry for modular onboarding reasoning skills
    - Mock Outlook Connector for synthetic readiness notifications
    """

    def __init__(
        self,
        *,
        cases: object | None = None,
        it_desk: object | None = None,
        clock=None,
        knowledge: BrainKnowledgeLoader | None = None,
        skills: SkillRegistry | None = None,
        advisor: AdvisorPort | None = None,
        outlook: MockOutlookConnector | None = None,
        store: CaseRepository | None = None,
        audit_sink: AuditSinkPort | None = None,
        run_recorder: AgentRunRecorderPort | None = None,
    ) -> None:
        # new_environment supplies the mock registry and desk; the service is built here so the audit clock is the real one, or the injected one
        _, self._cases, self._it_desk = new_environment(cases=cases, it_desk=it_desk)  # type: ignore[arg-type]
        # store, audit_sink and run_recorder are optional injection points (all default to today's in-memory behaviour)
        self._service = Uc04Service(cases=self._cases, it_desk=self._it_desk, audit=AuditLog(clock or system_clock, sink=audit_sink, defer_unattributed=audit_sink is not None), store=store)
        self._direct = DirectEntry(self._service, run_recorder=run_recorder)
        self._orchestrated = OrchestratedEntry(self._service, run_recorder=run_recorder)
        self._operations = OperationEntry(self._service)

        # Knowledge, Skills, Fake LLM Advisor, and Outlook Connector
        self._knowledge = knowledge or BrainKnowledgeLoader()
        self._skills = skills or SkillRegistry()
        self._advisor = advisor or FakeLlmAdvisor(
            knowledge_loader=self._knowledge,
            skill_registry=self._skills,
        )
        self._outlook = outlook or MockOutlookConnector()

    # ------------------------------------------------------------------ direct path
    def evaluate(
        self,
        payload: dict | Uc04Input,
        *,
        actor_id: str = "SYN-USER-100",
        roles: list[str] | None = None,
        run_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Uc04Result:
        """Run the full UC-04 evaluation via the direct entry point."""
        return self._direct.evaluate(
            payload,
            actor_id=actor_id,
            roles=roles or ["hr_onboarding_officer"],
            run_id=run_id,
            idempotency_key=idempotency_key,
        )

    def evaluate_with_advisory(
        self,
        payload: dict | Uc04Input,
        *,
        actor_id: str = "SYN-USER-100",
        roles: list[str] | None = None,
        run_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> AgentEvaluationResult:
        """Run evaluation and generate a deterministic advisory report.

        The evaluation result is authoritative. The advisory report is
        purely informational and synthesized via the local Fake LLM layer.
        """
        result = self.evaluate(
            payload,
            actor_id=actor_id,
            roles=roles,
            run_id=run_id,
            idempotency_key=idempotency_key,
        )
        advisory = self._advisor.generate_advisory(result, payload)
        return AgentEvaluationResult(result=result, advisory=advisory)

    def explain(
        self,
        result: Uc04Result,
        payload: dict | Uc04Input | None = None,
    ) -> AdvisoryReport:
        """Generate a Fake LLM advisory explanation for an already computed result."""
        return self._advisor.generate_advisory(result, payload)

    def evaluate_cohort(self, cohort_ref: str, payloads: list[dict], *, actor_id: str = "SYN-USER-100", roles: list[str] | None = None,
                        run_id: str | None = None) -> CohortView:
        """EC-40: evaluate each person's OWN case (never one shared case) and return one cohort view. A case that cannot be evaluated is reported
        by its error class and never hides or changes another person's result."""
        results: dict = {}
        failed: dict[str, str] = {}
        for payload in payloads:
            data = dict(payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload, cohort_ref=cohort_ref)
            case_id = data.get("case_id", "UNKNOWN")
            try:
                results[case_id] = self.evaluate(data, actor_id=actor_id, roles=roles, run_id=run_id,
                                                 idempotency_key=f"cohort.{cohort_ref}.{case_id}"[:80])
            except Exception as exc:  # noqa: BLE001 - one person's failure is reported, not propagated
                failed[case_id] = type(exc).__name__
        return cohort_view(cohort_ref, results, failed)

    def submit_eligible(
        self,
        payload: dict | Uc04Input,
        *,
        actor_id: str = "SYN-USER-100",
        roles: list[str] | None = None,
    ) -> SubmissionReport:
        """Submit fully approved items to the MOCK IT service desk. Only eligible items are sent (BR-08, GR-02)."""
        return self._direct.submit_eligible(
            payload,
            actor_id=actor_id,
            roles=roles or ["hr_onboarding_officer"],
        )

    def acknowledge_adapter_error(
        self,
        case_id: str,
        request_id: str,
        roles: list[str] | None = None,
    ) -> None:
        """A person confirms with IT that a retry is safe, unblocking resubmission for an item that failed an adapter call."""
        self._service.acknowledge_adapter_error(
            case_id=case_id,
            request_id=request_id,
            roles=roles or ["hr_onboarding_officer"],
        )

    # ------------------------------------------------------------------ notification
    def draft_readiness_notification(
        self,
        result: Uc04Result,
        recipient_email: str = "hr-onboarding@example.com",
    ) -> EmailMessage:
        """Prepare (never send) an advisory notification about the case status.
        A draft is always allowed.
        """
        status_val = (
            result.overall_status.value
            if hasattr(result.overall_status, "value")
            else str(result.overall_status)
        )
        subject = f"[SIMULATED NOTIFICATION] UC-04 Case {result.case_id} Status: {status_val}"
        body = (
            f"Advisory notification for case {result.case_id}:\n"
            f"Status: {status_val}\n"
            f"Readiness: {result.readiness_reason}\n\n"
            f"Notice: This notification was generated by the UC-04 sub-agent. "
            f"It does not constitute an approval, IT ticket update, or business decision."
        )
        return EmailMessage(
            to=[recipient_email],
            subject=subject,
            body=body,
        )

    def notify_readiness(
        self,
        result: Uc04Result,
        recipient_email: str = "hr-onboarding@example.com",
        *,
        approved_by: str | None = None,
    ) -> SentMessage:
        """Send the notification through the mock Outlook connector with human approval.

        UC-04 runs at autonomy L1: nothing is sent to a person without a person's approval
        (GR-01), and the policy kill switch stops every action (BR-18). `approved_by` is the
        approver's reference. Refusals and sends are audited without the recipient's address.
        Sending is informational only: it grants no approval and never changes case status.
        """
        policy, audit = self._service.policy, self._service.audit
        if not policy.enabled:
            audit.record(
                "notification.refused",
                case_id=result.case_id,
                rule_id="BR-18",
                reason="kill switch is off",
                owner="HR_OFFICER",
            )
            raise PolicyViolation(f"{policy.policy}: disabled by kill switch")
        approver = (approved_by or "").strip()
        if not re.match(USER_REF, approver):
            audit.record(
                "notification.refused",
                case_id=result.case_id,
                rule_id="GR-01",
                reason="no valid human approval for sending",
                owner="HR_OFFICER",
            )
            raise PolicyViolation(
                f"{policy.policy}: sending a notification needs an explicit "
                f"human approval (approved_by)"
            )
        sent = self._outlook.send_email(
            self.draft_readiness_notification(result, recipient_email)
        )
        audit.record(
            "notification.sent",
            case_id=result.case_id,
            rule_id="GR-01",
            reason=f"approved by {approver}",
            owner="HR_OFFICER",
        )
        return sent

    # ------------------------------------------------------------------ orchestrated path
    def evaluate_orchestrated(self, request: OrchestratorRequest) -> OrchestratorResponse:
        """Run the evaluation via the parent-orchestrated entry point."""
        return self._orchestrated.evaluate(request)

    def submit_orchestrated(self, request: OrchestratorRequest) -> OrchestratorResponse:
        """Raise the (mock) IT requests for the items that are fully approved. An uncertain outcome is never assumed to have succeeded."""
        return self._orchestrated.submit_eligible(request)

    def acknowledge_orchestrated(self, op: CaseOperationRequest) -> AcknowledgementOut:
        """A person's confirmation that a retry is safe after an adapter error. Authorised by the policy's acknowledgement roles."""
        return self._operations.acknowledge_adapter_error(op)

    def status_orchestrated(self, op: CaseOperationRequest):
        """Read-only: the stored state of the case, for reconciliation. Evaluates nothing."""
        return self._operations.case_status(op)

    # ------------------------------------------------------------------ inspection
    @property
    def service(self) -> Uc04Service:
        return self._service

    @property
    def cases(self) -> MockCaseContext:
        return self._cases  # type: ignore[return-value]

    @property
    def it_desk(self) -> MockITServiceDesk:
        return self._it_desk  # type: ignore[return-value]

    @property
    def knowledge(self) -> BrainKnowledgeLoader:
        return self._knowledge

    @property
    def skills(self) -> SkillRegistry:
        return self._skills

    @property
    def advisor(self) -> AdvisorPort:
        return self._advisor

    @property
    def advisor_provider(self) -> str:
        """Which advisory provider this agent uses (never a secret): shown in diagnostics."""
        return self._advisor.provider

    @property
    def outlook(self) -> MockOutlookConnector:
        return self._outlook
