"""Deterministic Fake LLM Advisory Layer for UC-04.

Provides human-readable explanations and advisory summaries of UC-04 case
evaluations, synthesizing verified service results, brain knowledge, and
registered reasoning skills.

CRITICAL ARCHITECTURAL BOUNDARY:
  The Fake LLM is strictly read-only and advisory. It CANNOT:
  - Approve or reject requests
  - Alter case or item states
  - Create IT tickets or bypass guardrails
  - Fabricate approvals, evidence, or delivery records
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sub_agent.contracts import Uc04Input, Uc04Result
from sub_agent.guardrails import sanitize_free_text
from sub_agent.knowledge import BrainKnowledgeLoader
from sub_agent.skills import SkillRegistry
from sub_agent.states import ApprovalState, CaseStatus

ADVISORY_BANNER = "[SIMULATED ADVISORY - NOT AN AUTHORITATIVE APPROVAL OR DECISION]"
FALLBACK_ADVISORY = (
    f"{ADVISORY_BANNER}\n"
    "Status: UNABLE_TO_ADVISE\n"
    "Reason: The provided input or evaluation result is missing, incomplete, or malformed.\n"
    "Recommended Action: Defer to the designated HR Onboarding Officer for manual review."
)


class AdvisoryReport(BaseModel):
    """Structured output from the Fake LLM Advisory Layer."""

    is_simulated: bool = True
    banner: str = ADVISORY_BANNER
    case_id: str
    overall_status: str
    status_explanation: str
    approval_guidance: str
    risk_analysis: str
    readiness_summary: str
    action_recommendations: list[str] = Field(default_factory=list)
    applicable_skills: list[str] = Field(default_factory=list)
    referenced_knowledge: list[str] = Field(default_factory=list)
    formatted_explanation: str
    # Which provider produced this report (see advisory_providers.py). Additive; the defaults describe the offline template advisor.
    provider: str = "fake-llm"
    model: str | None = None
    narrative: str | None = None  # model-written plain-language summary; never a decision
    advisory_error: str | None = None  # an error class name only, never a message
    fallback_from: str | None = None  # set only when a fallback was explicitly configured and used
    # True when a model advisory was requested but could not be produced (failure, refusal, rejected output, missing model). The decision
    # fields above are unaffected either way: they always come from the deterministic template.
    degraded: bool = False
    degraded_reason: str | None = None  # an error class name only, the same value as advisory_error


class FakeLlmAdvisor:
    """Local deterministic advisory layer for UC-04 Equipment & Access Management."""

    provider = "fake-llm"

    def __init__(
        self,
        knowledge_loader: BrainKnowledgeLoader | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.knowledge = knowledge_loader or BrainKnowledgeLoader()
        self.skills = skill_registry or SkillRegistry()

    def generate_advisory(
        self,
        result: Uc04Result | None,
        input_data: Uc04Input | dict[str, Any] | None = None,
    ) -> AdvisoryReport:
        """Generate a deterministic advisory explanation from a verified Uc04Result."""
        if result is None:
            return self._build_fallback("Missing evaluation result")

        case_id = result.case_id or "UNKNOWN-CASE"
        status_val = (
            result.overall_status.value
            if hasattr(result.overall_status, "value")
            else str(result.overall_status)
        )

        # 1. Match applicable skills and brain knowledge
        matched_skills = self.skills.match_skills(result=result, status=status_val)
        skill_names = [s.name for s in matched_skills]

        referenced_docs = ["brain/onboarding/uc04_equipment_access.md"]
        status_guidance = self.knowledge.get_status_guidance(status_val)

        # 2-6. Every section passes the privacy guard
        status_exp = self._clean(self._explain_status(result, status_guidance))
        approval_exp = self._clean(self._explain_approvals(result))
        risk_exp = self._clean(self._explain_risks(result))
        readiness_exp = self._clean(self._explain_readiness(result))
        actions = [self._clean(a) for a in self._build_actions(result)]

        # 7. Formatted Plain-text Output
        context = self._clean(self._context(input_data) or "") or None
        formatted = self._compose_markdown(
            case_id=case_id,
            status=status_val,
            status_exp=status_exp,
            approval_exp=approval_exp,
            risk_exp=risk_exp,
            readiness_exp=readiness_exp,
            actions=actions,
            skills=skill_names,
            context=context,
        )

        # 8. Privacy guardrail on finished text (idempotent: clean lines returned unchanged)
        clean_formatted = self._clean(formatted)

        return AdvisoryReport(
            is_simulated=True,
            banner=ADVISORY_BANNER,
            case_id=case_id,
            overall_status=status_val,
            status_explanation=status_exp,
            approval_guidance=approval_exp,
            risk_analysis=risk_exp,
            readiness_summary=readiness_exp,
            action_recommendations=actions,
            applicable_skills=skill_names,
            referenced_knowledge=referenced_docs,
            formatted_explanation=clean_formatted,
        )

    @staticmethod
    def _context(input_data: Uc04Input | dict[str, Any] | None) -> str | None:
        """One line of case context (client, role, joining date, work mode) from the input, when it is given."""
        if input_data is None:
            return None
        get = input_data.get if isinstance(input_data, dict) else (lambda key: getattr(input_data, key, None))
        parts = []
        for label, key in (("client", "client_id"), ("role", "role_id"), ("joining", "joining_date"), ("work mode", "work_mode")):
            value = get(key)
            if value not in (None, ""):
                parts.append(f"{label} {getattr(value, 'value', value)}")
        return " | ".join(parts) or None

    @staticmethod
    def _clean(text: str) -> str:
        """Privacy guard applied line-by-line: sensitive lines are replaced by the neutral marker.

        `sanitize_free_text` flattens text to one line and cuts it to 200 characters, so it must
        never be applied to a whole multi-line report.
        """
        return "\n".join(
            sanitize_free_text(line, max_len=max(len(line), 1)) or ""
            for line in text.splitlines()
        )

    def _explain_status(self, result: Uc04Result, guidance: str) -> str:
        status = result.overall_status
        if status == CaseStatus.BLOCKED:
            reasons = [b.reason for b in result.blockers]
            reasons_str = (
                ", ".join(reasons)
                if reasons
                else "Start gate or essential configuration not satisfied."
            )
            return f"Case is BLOCKED from progression. Active blockers: {reasons_str}."
        if status == CaseStatus.AWAITING_APPROVAL:
            pending_items = [
                it
                for it in result.items
                if it.approval_state == ApprovalState.PENDING
                or it.exception_state == ApprovalState.PENDING
            ]
            pending_count = len(pending_items)
            return (
                f"Case is AWAITING_APPROVAL. {pending_count} item(s) require designated approver "
                f"signoff before simulated dispatch to IT."
            )
        if status == CaseStatus.IN_PROGRESS:
            return (
                "Case is IN_PROGRESS. Approvals have been recorded and items are currently "
                "progressing through simulated IT fulfillment."
            )
        if status == CaseStatus.AT_RISK:
            return (
                "Case is AT_RISK. An equipment delivery or security clearance delay threatens "
                "readiness prior to the joining date."
            )
        if status == CaseStatus.READY:
            return (
                "Case is READY. Every required access and equipment item has authoritative "
                "completion evidence on file."
            )
        if status == CaseStatus.NEEDS_HUMAN_REVIEW:
            return (
                "Case NEEDS_HUMAN_REVIEW. One or more IT service desk requests were rejected "
                "or require manual HR intervention."
            )
        if status == CaseStatus.CANCELLED:
            return (
                "Case is CANCELLED. The parent onboarding process was withdrawn; all sub-agent "
                "actions are halted."
            )
        return f"Case is currently in status {status.value}. {guidance}"

    def _explain_approvals(self, result: Uc04Result) -> str:
        pending_items = [
            it
            for it in result.items
            if it.approval_state == ApprovalState.PENDING
            or it.exception_state == ApprovalState.PENDING
        ]
        if not pending_items:
            return "No outstanding approvals required at this time."
        lines = []
        for item in pending_items:
            kind_str = (
                item.item_kind.value
                if hasattr(item.item_kind, "value")
                else str(item.item_kind)
            )
            lines.append(
                f"- Item `{item.item_id}` ({kind_str}): "
                f"requires signoff from `{item.required_approver}`."
            )
        return "The following item approvals must be submitted:\n" + "\n".join(lines)

    def _explain_risks(self, result: Uc04Result) -> str:
        if not result.risks:
            return "No active risks identified for this case."
        lines = []
        for r in result.risks:
            code_str = r.code.value if hasattr(r.code, "value") else str(r.code)
            guidance = self.knowledge.get_risk_guidance(code_str)
            lines.append(
                f"- Risk `{code_str}`: {r.reason} (Owner: {r.owner}). "
                f"Mitigation: {guidance}"
            )
        return "Active risks detected:\n" + "\n".join(lines)

    def _explain_readiness(self, result: Uc04Result) -> str:
        return f"Readiness Assessment: {result.readiness_reason}"

    def _build_actions(self, result: Uc04Result) -> list[str]:
        actions: list[str] = []
        for next_act in result.next_actions:
            actions.append(f"[{next_act.owner}] {next_act.action}")
        if not actions and result.overall_status == CaseStatus.READY:
            actions.append("[HR_OFFICER] Confirm joiner readiness with hiring manager.")
        return actions

    def _compose_markdown(
        self,
        case_id: str,
        status: str,
        status_exp: str,
        approval_exp: str,
        risk_exp: str,
        readiness_exp: str,
        actions: list[str],
        skills: list[str],
        context: str | None = None,
    ) -> str:
        lines = [
            ADVISORY_BANNER,
            f"### Case Evaluation Advisory: {case_id}",
            f"**Current Status**: `{status}`",
            *([f"**Context**: {context}"] if context else []),
            "",
            "#### 1. Status & Findings",
            status_exp,
            "",
            "#### 2. Approvals",
            approval_exp,
            "",
            "#### 3. Risks & Blockers",
            risk_exp,
            "",
            "#### 4. Readiness",
            readiness_exp,
            "",
            "#### 5. Recommended Actions",
        ]
        if actions:
            for act in actions:
                lines.append(f"- {act}")
        else:
            lines.append("- No further actions required.")

        lines.extend([
            "",
            f"*(Skills applied: {', '.join(skills) if skills else 'none'})*",
        ])
        return "\n".join(lines)

    def _build_fallback(self, reason: str) -> AdvisoryReport:
        return AdvisoryReport(
            is_simulated=True,
            banner=ADVISORY_BANNER,
            case_id="UNKNOWN",
            overall_status="UNKNOWN",
            status_explanation=f"{FALLBACK_ADVISORY} (Detail: {reason})",
            approval_guidance="None",
            risk_analysis="None",
            readiness_summary="None",
            action_recommendations=["Manual HR assessment required."],
            applicable_skills=[],
            referenced_knowledge=[],
            formatted_explanation=FALLBACK_ADVISORY,
        )
