from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .audit_scope import audit_client_scope
from .case_events import publish_case_opened
from .duplicate_check import DuplicateCheckRequest, DuplicateOutcome, check_duplicates
from .llm import LlmClient
from .models import ActorType, AuditEntry, Case, CasePlacement, OnboardingForm, VariantFlags
from .open_case import create_case
from .ports import (
    ApprovalRepository,
    AuditLog,
    CaseRepository,
    DocumentStore,
    EmployeeNumberGenerator,
    EventPublisher,
    FormRepository,
    PersonDirectory,
)
from .start_gate import StartDecision, StartRequest, evaluate_start_gate


class OpenCaseOutcome(StrEnum):
    CASE_OPENED = "CASE_OPENED"
    HELD = "HELD"
    PAUSED = "PAUSED"
    LINKED_EXISTING = "LINKED_EXISTING"


class OpenCaseRequest(BaseModel):
    request_id: str
    client_id: str
    hr_officer_id: str
    offer_accepted: bool
    form: Optional[OnboardingForm] = None
    hiring_freeze: bool = False
    id_number: Optional[str] = None
    declared_variants: Optional[VariantFlags] = None
    # Placement attributes (approved 2026-11-04). A case cannot be
    # opened without a location_name - but a caller that does not supply
    # one gets a HELD outcome (reason PLACEMENT_REQUIRED_REASON), not a
    # validation crash, so a request with every other condition met is
    # parked for HR instead of failing. Nothing is ever invented here.
    placement: Optional[CasePlacement] = None


#: Reason returned (and audited) when a request has no placement.
PLACEMENT_REQUIRED_REASON = "location_name_required"


@dataclass
class OnboardingDeps:
    case_repo: CaseRepository
    form_repo: FormRepository
    approval_repo: ApprovalRepository
    person_directory: PersonDirectory
    document_store: DocumentStore
    number_generator: EmployeeNumberGenerator
    event_publisher: EventPublisher
    audit_log: AuditLog
    # Advisory only, off by default. Drafts an explanation of a
    # variant/duplicate match for the HR manager deciding HC_12.
    # Never decides, never changes the match itself.
    llm: Optional[LlmClient] = None


class OpenCaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: OpenCaseOutcome
    case: Optional[Case] = None
    reasons: list[str] = Field(default_factory=list)
    approvals_created: list[str] = Field(default_factory=list)
    event_id: Optional[str] = None
    existing_case_id: Optional[str] = None


def open_onboarding_case(request: OpenCaseRequest, deps: OnboardingDeps) -> OpenCaseResult:
    # Request-level audit entries (start gate, duplicate check) precede any
    # case; this scope attributes them to the request's client.
    with audit_client_scope(request.client_id):
        return _open_onboarding_case(request, deps)


def _open_onboarding_case(request: OpenCaseRequest, deps: OnboardingDeps) -> OpenCaseResult:
    start_request = StartRequest(
        request_id=request.request_id,
        client_id=request.client_id,
        candidate_email=request.form.candidate_email if request.form else "",
        offer_accepted=request.offer_accepted,
        form=request.form,
        hiring_freeze=request.hiring_freeze,
    )
    start_result = evaluate_start_gate(start_request, deps.audit_log)

    if start_result.decision == StartDecision.PAUSED:
        return OpenCaseResult(outcome=OpenCaseOutcome.PAUSED, reasons=start_result.reasons)
    if start_result.decision == StartDecision.HELD:
        reasons = list(start_result.reasons)
        if request.placement is None:
            reasons.append(PLACEMENT_REQUIRED_REASON)
        return OpenCaseResult(outcome=OpenCaseOutcome.HELD, reasons=reasons)

    # STARTED implies evaluate_start_gate already confirmed an APPROVED form
    # exists for this client, so request.form is guaranteed non-None here.
    form = request.form
    assert form is not None

    dup_request = DuplicateCheckRequest(
        request_id=request.request_id,
        client_id=request.client_id,
        candidate_name=form.candidate_name,
        candidate_email=form.candidate_email,
        id_number=request.id_number,
    )
    dup_result = check_duplicates(dup_request, deps.person_directory, deps.audit_log)

    if dup_result.outcome == DuplicateOutcome.DUPLICATE_OPEN_CASE:
        deps.audit_log.append(
            AuditEntry(
                case_id=dup_result.existing_case_id,
                request_id=request.request_id,
                actor_type=ActorType.AGENT,
                actor_id="uc01-onboarding-entry",
                action="REQUEST_LINKED_TO_EXISTING_CASE",
                after={"existing_case_id": dup_result.existing_case_id},
                reference=request.request_id,
            )
        )
        return OpenCaseResult(
            outcome=OpenCaseOutcome.LINKED_EXISTING,
            existing_case_id=dup_result.existing_case_id,
            reasons=dup_result.reasons,
        )

    if request.placement is None:
        # Offer and form are fine and nobody is onboarded yet, but
        # location_name is mandatory and UC-01 has no source it may derive
        # it from (decision D3). Hold - never invent a location. Placed
        # after the duplicate check so a repeat request for an existing
        # case still links to it.
        deps.audit_log.append(
            AuditEntry(
                request_id=request.request_id,
                actor_type=ActorType.AGENT,
                actor_id="uc01-onboarding-entry",
                action="CASE_OPEN_HELD_PLACEMENT_MISSING",
                after={"reasons": [PLACEMENT_REQUIRED_REASON]},
                reference=request.request_id,
            )
        )
        return OpenCaseResult(outcome=OpenCaseOutcome.HELD, reasons=[PLACEMENT_REQUIRED_REASON])

    case, approvals_created = create_case(
        form=form,
        hr_officer_id=request.hr_officer_id,
        id_number=request.id_number,
        duplicate_result=dup_result,
        declared_variants=request.declared_variants,
        placement=request.placement,
        case_repo=deps.case_repo,
        form_repo=deps.form_repo,
        approval_repo=deps.approval_repo,
        person_directory=deps.person_directory,
        folder_store=deps.document_store,
        number_generator=deps.number_generator,
        audit_log=deps.audit_log,
        llm=deps.llm,
    )

    event = publish_case_opened(case, deps.event_publisher, deps.audit_log)

    return OpenCaseResult(
        outcome=OpenCaseOutcome.CASE_OPENED,
        case=case,
        approvals_created=approvals_created,
        event_id=event.event_id,
    )
