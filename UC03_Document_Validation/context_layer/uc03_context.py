from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from knowledge.checklists.loader import DocumentRequirement, load_checklist, resolve_applicable_documents
from src.state.enums import OnboardingStatus
from src.state.models import AuditEvent, OnboardingCase
from src.state.store import AuditStore, CaseStore

# Maximum recent audit events surfaced to the agent per step
_MAX_AUDIT_EVENTS = 10


@dataclass
class DocumentContext:
    """What the agent knows about one document at the current step."""
    document_type: str
    label: str
    required: bool
    submitted: bool
    validations: dict[str, Any]


@dataclass
class UC03Context:
    """
    Full context assembled for one UC-03 validation step.
    This is what the agent sees — no raw personal data, no document bytes.
    """
    case_id: str
    employee_id: str
    client_id: str
    employee_name_form: str
    onboarding_status: OnboardingStatus
    offer_accepted: bool
    onboarding_form_approved: bool
    location: str
    nationality: str
    sponsorship_applicable: bool
    visa_required: bool

    applicable_documents: list[DocumentContext]
    outstanding_document_ids: list[str]
    submitted_document_ids: list[str]

    recent_audit_events: list[dict[str, Any]]
    retrieved_passages: list[str]       # from Azure AI Search (empty until wired)

    assembled_at: datetime = field(default_factory=datetime.utcnow)
    assembly_warnings: list[str] = field(default_factory=list)


class UC03ContextAssembler:
    """
    Assembles the context that the UC-03 agent sees at each validation step.

    Pulls from:
    - CaseStore    → current onboarding case state
    - AuditStore   → recent audit history for the case
    - Checklist    → applicable document requirements
    - Knowledge    → retrieved policy/regulation passages (stub — needs Azure AI Search)
    """

    def __init__(
        self,
        case_store: CaseStore,
        audit_store: AuditStore,
        checklist_name: str = "default",
    ) -> None:
        self._case_store = case_store
        self._audit_store = audit_store
        self._checklist_name = checklist_name

    async def assemble(
        self,
        case_id: str,
        submitted_document_ids: list[str] | None = None,
        query: str | None = None,
    ) -> UC03Context:
        """
        Assemble full context for a validation step.

        Args:
            case_id: The onboarding case to assemble context for.
            submitted_document_ids: Documents already received for this case.
            query: Optional search query for knowledge retrieval (unused until Azure AI Search wired).
        """
        submitted_document_ids = submitted_document_ids or []
        warnings: list[str] = []

        # 1. Load case state
        case = await self._case_store.get_case(case_id)
        if case is None:
            warnings.append(f"Case '{case_id}' not found in store — using empty defaults.")
            case = _empty_case(case_id)

        # 2. Load recent audit events (capped — agent does not need full history)
        all_events = await self._audit_store.get_events(case_id)
        recent_events = _serialise_events(all_events[-_MAX_AUDIT_EVENTS:])

        # 3. Load applicable checklist
        checklist_ctx = _build_checklist_context(case)
        try:
            checklist = load_checklist(self._checklist_name)
            applicable = resolve_applicable_documents(checklist, checklist_ctx)
        except FileNotFoundError:
            warnings.append(
                f"Checklist '{self._checklist_name}' not found. "
                "Falling back to empty checklist — request HR clarification."
            )
            applicable = []

        doc_contexts = [
            DocumentContext(
                document_type=d.id,
                label=d.label,
                required=d.required,
                submitted=d.id in submitted_document_ids,
                validations=d.validations,
            )
            for d in applicable
        ]

        outstanding = [d.id for d in applicable if d.id not in submitted_document_ids]

        # 4. Knowledge retrieval (stub — returns empty until Azure AI Search is wired)
        passages = await _retrieve_passages(query=query)
        if query and not passages:
            warnings.append(
                "Azure AI Search not configured — knowledge retrieval skipped. "
                "Set AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_API_KEY to enable."
            )

        return UC03Context(
            case_id=case_id,
            employee_id=case.employee_id,
            client_id=case.client_id,
            employee_name_form=case.employee_name_form,
            onboarding_status=case.status,
            offer_accepted=case.offer_accepted,
            onboarding_form_approved=case.onboarding_form_approved,
            location=case.location,
            nationality=case.nationality,
            sponsorship_applicable=getattr(case, "sponsorship_applicable", False),
            visa_required=getattr(case, "visa_required", False),
            applicable_documents=doc_contexts,
            outstanding_document_ids=outstanding,
            submitted_document_ids=submitted_document_ids,
            recent_audit_events=recent_events,
            retrieved_passages=passages,
            assembly_warnings=warnings,
        )

    def format_for_llm(self, ctx: UC03Context) -> str:
        """
        Format the assembled context as a plain-text block suitable for
        inclusion in an LLM prompt. Excludes raw personal data.
        """
        lines = [
            f"Case ID: {ctx.case_id}",
            f"Onboarding status: {ctx.onboarding_status.value}",
            f"Offer accepted: {ctx.offer_accepted}",
            f"Form approved: {ctx.onboarding_form_approved}",
            f"Location: {ctx.location}",
            "",
            "Applicable documents:",
        ]
        for doc in ctx.applicable_documents:
            status = "submitted" if doc.submitted else "outstanding"
            req = "required" if doc.required else "optional"
            lines.append(f"  - {doc.label} [{req}] — {status}")

        lines += [
            "",
            f"Outstanding: {', '.join(ctx.outstanding_document_ids) or 'none'}",
            "",
            "Recent activity:",
        ]
        for evt in ctx.recent_audit_events[-5:]:
            lines.append(f"  - {evt.get('timestamp', '')} {evt.get('action', '')}")

        if ctx.retrieved_passages:
            lines += ["", "Relevant policy passages:"]
            for passage in ctx.retrieved_passages:
                lines.append(f"  > {passage}")

        if ctx.assembly_warnings:
            lines += ["", "Warnings:"]
            for w in ctx.assembly_warnings:
                lines.append(f"  ! {w}")

        return "\n".join(lines)


# ─── Private helpers ──────────────────────────────────────────────────────────

def _empty_case(case_id: str) -> OnboardingCase:
    from src.state.enums import OnboardingStatus
    return OnboardingCase(
        case_id=case_id,
        employee_id="unknown",
        client_id="default",
        role="",
        grade="",
        nationality="",
        location="",
        offer_accepted=False,
        onboarding_form_approved=False,
        employee_name_form="",
        status=OnboardingStatus.INCOMPLETE,
    )


def _build_checklist_context(case: OnboardingCase) -> dict:
    return {
        "location": case.location,
        "sponsorship": getattr(case, "sponsorship_applicable", False),
        "visa_required": getattr(case, "visa_required", False),
        "nationality": case.nationality,
    }


def _serialise_events(events: list[AuditEvent]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": e.event_id,
            "action": e.action,
            "actor_id": e.actor_id,
            "actor_type": e.actor_type.value,
            "timestamp": e.timestamp.isoformat(),
            # detail logged but sensitive fields stripped
            "detail_keys": list(e.detail.keys()),
        }
        for e in events
    ]


async def _retrieve_passages(query: str | None) -> list[str]:
    """
    Retrieve relevant passages from Azure AI Search.

    STUB: Returns empty list until Azure AI Search credentials are configured.
    Replace this function body with the real Azure AI Search client call.

    Required env vars when wiring:
        AZURE_SEARCH_ENDPOINT
        AZURE_SEARCH_API_KEY
        AZURE_SEARCH_INDEX_NAME
    """
    return []
