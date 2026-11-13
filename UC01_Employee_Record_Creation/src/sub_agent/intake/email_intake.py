"""[Proposed] Dev trigger simulator for UC-01, end to end: an offer-
acceptance email in -> a case opened (or held waiting on the form) ->
eventually an HRIS write. Deterministic Python only (stdlib `email`
parsing, no AI). Everything in this module is a stand-in: the real
trigger, its transport, and who creates/approves the onboarding form are
all open question OQ-01/Q-11. None of this is meant to survive contact
with a real event source.
"""

from dataclasses import dataclass
from email import message_from_string
from email.utils import parseaddr
from enum import StrEnum
from re import compile as re_compile
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..access import WORKLIST_ROLES, record_access_denied, require_role
from ..errors import AccessDeniedError
from ..llm import LlmClient, LlmError, LlmRequest, audit_action_for
from ..models import (
    ActorType,
    AuditEntry,
    CasePlacement,
    PendingStart,
    PendingStartStatus,
    Viewer,
)
from ..models.common import new_id, utc_now
from ..models.offer import OfferAcceptedEvent, OfferStatus
from ..onboarding_entry import OnboardingDeps, OpenCaseOutcome, OpenCaseRequest, open_onboarding_case
from ..ports import AuditLog, OfferRepository, OnboardingFormSource, PendingStartRepository

_OFFER_REF_RE = re_compile(r"\[(OFFER-\d+)\]")


class IntakeRejectionCode(StrEnum):
    NOT_AN_EMAIL = "NOT_AN_EMAIL"
    NO_OFFER_REF = "NO_OFFER_REF"
    MULTIPLE_OFFER_REFS = "MULTIPLE_OFFER_REFS"
    UNKNOWN_OFFER = "UNKNOWN_OFFER"
    AMBIGUOUS_OFFER_REF = "AMBIGUOUS_OFFER_REF"
    SENDER_MISMATCH = "SENDER_MISMATCH"
    OFFER_WITHDRAWN = "OFFER_WITHDRAWN"


class IntakeReviewCode(StrEnum):
    POSSIBLE_DECLINE_OR_NEGOTIATION = "POSSIBLE_DECLINE_OR_NEGOTIATION"
    NO_ACCEPTANCE_SIGNAL = "NO_ACCEPTANCE_SIGNAL"
    # Raised only by the advisory model, and only when the deterministic
    # rules already found the subject acceptable. See _llm_second_opinion.
    MODEL_FLAGGED_FOR_REVIEW = "MODEL_FLAGGED_FOR_REVIEW"


# The one label that lets an acceptance through. Anything else from the
# model -- including UNSURE and any label a future task file adds -- is
# treated as "a person should look at this".
_MODEL_ACCEPTANCE_LABEL = "LOOKS_LIKE_ACCEPTANCE"


# [Proposed] Subject-line heuristics only, never the body. In production,
# acceptance should come from the recruitment system or an e-signature
# flow, not a free-text email subject (OQ-01).
_DECLINE_KEYWORDS = (
    "decline",
    "reject",
    "not accept",
    "cannot accept",
    "can't accept",
    "withdraw",
    "counter",
    "negotiat",
    # "unacceptable"/"not acceptable" contain "accept" but say the
    # opposite; they must never read as an acceptance.
    "unacceptable",
    "not acceptable",
)

# An acceptance signal is the WORD accept (or its inflections), not the
# substring: "acceptable" and "unacceptable" are not acceptances.
_ACCEPT_WORD_RE = re_compile(r"\baccept(?:s|ed|ing|ance)?\b")


def _classify_intent(subject: str) -> Optional[IntakeReviewCode]:
    lowered = subject.lower()
    if any(keyword in lowered for keyword in _DECLINE_KEYWORDS):
        return IntakeReviewCode.POSSIBLE_DECLINE_OR_NEGOTIATION
    if _ACCEPT_WORD_RE.search(lowered) is None:
        return IntakeReviewCode.NO_ACCEPTANCE_SIGNAL
    return None


class ParsedAcceptance(BaseModel):
    model_config = ConfigDict(frozen=True)

    sender_email: str
    offer_ref: str
    subject: str


class IntakeRejection(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: IntakeRejectionCode


def parse_acceptance_email(raw: str) -> ParsedAcceptance | IntakeRejection:
    """Parse with the stdlib `email` package only. The body is never read:
    only the From and Subject headers are inspected, so no body text is
    ever kept or logged.
    """
    if not raw or not raw.strip():
        return IntakeRejection(code=IntakeRejectionCode.NOT_AN_EMAIL)

    msg = message_from_string(raw)
    from_header = msg.get("From")
    _, sender_email = parseaddr(from_header or "")
    if not from_header or not sender_email:
        return IntakeRejection(code=IntakeRejectionCode.NOT_AN_EMAIL)

    subject = msg.get("Subject") or ""
    refs = sorted(set(_OFFER_REF_RE.findall(subject)))
    if not refs:
        return IntakeRejection(code=IntakeRejectionCode.NO_OFFER_REF)
    if len(refs) > 1:
        return IntakeRejection(code=IntakeRejectionCode.MULTIPLE_OFFER_REFS)

    return ParsedAcceptance(sender_email=sender_email.strip().lower(), offer_ref=refs[0], subject=subject)


def _llm_second_opinion(subject: str, deps: "IntakeDeps") -> Optional[IntakeReviewCode]:
    """Ask the advisory model whether a subject really reads as an
    acceptance. Escalate-only by construction, in three ways:

    - it is called only when the deterministic classifier already said
      "no review needed", so it can add a review but never clear one;
    - it returns either a review code or None -- there is no return
      value that approves anything;
    - a model outage returns None, so an outage is never able to block
      a case. The deterministic result stands on its own.

    Only the subject line is sent. The body is never read by UC-01.
    """
    if deps.llm is None:
        return None

    try:
        response = deps.llm.complete(LlmRequest(task="intake_intent", inputs={"subject": subject}))
    except LlmError as exc:
        # Code only: never the subject, never the model's output.
        _audit(deps, audit_action_for(exc), {"task": "intake_intent"}, "email-intake")
        return None

    # Label and confidence only. The subject is not logged, because the
    # audit log must not become a copy of inbound email.
    _audit(
        deps,
        "LLM_ADVISORY",
        {
            "task": "intake_intent",
            # Which wording produced this label. Without it an audited
            # advisory decision cannot be reproduced after the prompt
            # is edited.
            "task_version": response.task_version,
            "label": response.label,
            "model": response.model,
            "confidence": round(response.confidence, 3),
        },
        "email-intake",
    )

    if response.label == _MODEL_ACCEPTANCE_LABEL:
        return None
    return IntakeReviewCode.MODEL_FLAGGED_FOR_REVIEW


class IntakeOutcome(StrEnum):
    CASE_OPENED = "CASE_OPENED"
    LINKED_EXISTING = "LINKED_EXISTING"
    WAITING_FOR_FORM = "WAITING_FOR_FORM"
    PAUSED = "PAUSED"
    REJECTED = "REJECTED"
    DUPLICATE_ACCEPTANCE = "DUPLICATE_ACCEPTANCE"
    NOTHING_PENDING = "NOTHING_PENDING"
    NEEDS_HR_REVIEW = "NEEDS_HR_REVIEW"


class IntakeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: IntakeOutcome
    case_id: Optional[str] = None
    employee_no: Optional[str] = None
    rejection_code: Optional[str] = None
    review_code: Optional[str] = None
    pending_id: Optional[str] = None
    reasons: list[str] = Field(default_factory=list)


@dataclass
class IntakeDeps:
    offer_repo: OfferRepository
    form_source: OnboardingFormSource
    pending_repo: PendingStartRepository
    onboarding_deps: OnboardingDeps
    # A per-client hiring-freeze toggle, same shape as UC01Deps's
    # hris_writes_enabled: tests/demos can swap this to simulate EC-20
    # without a real hiring-freeze source.
    hiring_freeze: Callable[[str], bool] = lambda client_id: False
    # Advisory only, and off by default. When set, it runs AFTER the
    # deterministic classifier and can only add a review, never clear
    # one (see _llm_second_opinion). Leaving it None is the behaviour
    # the eval scenarios run against.
    llm: Optional[LlmClient] = None
    # Placement attributes for a case opened through this [Proposed]
    # dev simulator. location_name is a REQUIRED caller-supplied value
    # (approved 2026-11-04) and nothing in an offer or an onboarding
    # form carries it, so this simulator has no source for it. Left
    # None, try_start refuses to open a case and parks the offer as
    # WAITING with a reason - it does NOT invent a location. A real
    # intake trigger must carry the value end to end (see OQ-01/Q-11).
    placement: Optional[CasePlacement] = None


def _audit(deps: IntakeDeps, action: str, after: dict, reference: str) -> None:
    deps.onboarding_deps.audit_log.append(
        AuditEntry(
            request_id=new_id(),
            actor_type=ActorType.SYSTEM,
            actor_id="uc01-email-intake",
            action=action,
            after=after,
            reference=reference,
        )
    )


def _reject(deps: IntakeDeps, code: IntakeRejectionCode, reference: str) -> IntakeResult:
    # Code only: never the sender address, never any body text.
    _audit(deps, "INTAKE_REJECTED", {"code": code.value}, reference)
    return IntakeResult(outcome=IntakeOutcome.REJECTED, rejection_code=code.value)


def _ensure_waiting_pending_start(
    client_id: str, offer_ref: str, reasons: list[str], deps: IntakeDeps
) -> PendingStart:
    existing = deps.pending_repo.get_waiting(client_id, offer_ref)
    if existing is not None:
        return existing
    pending = PendingStart(client_id=client_id, offer_ref=offer_ref, reasons=reasons)
    deps.pending_repo.add(pending)
    return pending


def handle_acceptance_email(raw: str, deps: IntakeDeps) -> IntakeResult:
    """Parse and act on an inbound acceptance email. The body is never read
    or stored; only From and Subject are inspected. [Proposed]: in
    production, acceptance should come from the recruitment system or an
    e-signature flow, not free-text email (OQ-01).
    """
    parsed = parse_acceptance_email(raw)
    if isinstance(parsed, IntakeRejection):
        return _reject(deps, parsed.code, "email-intake")

    offers = deps.offer_repo.find_by_ref(parsed.offer_ref)
    if not offers:
        return _reject(deps, IntakeRejectionCode.UNKNOWN_OFFER, parsed.offer_ref)
    if len(offers) > 1:
        return _reject(deps, IntakeRejectionCode.AMBIGUOUS_OFFER_REF, parsed.offer_ref)
    offer = offers[0]

    if parsed.sender_email != offer.candidate_email.strip().lower():
        return _reject(deps, IntakeRejectionCode.SENDER_MISMATCH, offer.offer_ref)

    if offer.status == OfferStatus.WITHDRAWN:
        return _reject(deps, IntakeRejectionCode.OFFER_WITHDRAWN, offer.offer_ref)

    if offer.status == OfferStatus.ACCEPTED:
        _audit(deps, "DUPLICATE_ACCEPTANCE_IGNORED", {"offer_ref": offer.offer_ref}, offer.offer_ref)
        return IntakeResult(outcome=IntakeOutcome.DUPLICATE_ACCEPTANCE)

    # Deterministic first and decisive. The model is consulted only if
    # these rules found nothing, so it can never overturn them.
    review_code = _classify_intent(parsed.subject)
    if review_code is None:
        review_code = _llm_second_opinion(parsed.subject, deps)
    if review_code is not None:
        _audit(
            deps,
            "INTAKE_NEEDS_REVIEW",
            {"offer_ref": offer.offer_ref, "code": review_code.value},
            offer.offer_ref,
        )
        return IntakeResult(outcome=IntakeOutcome.NEEDS_HR_REVIEW, review_code=review_code.value)

    accepted_at = utc_now()
    accepted_offer = offer.model_copy(update={"status": OfferStatus.ACCEPTED, "accepted_at": accepted_at})
    deps.offer_repo.save(accepted_offer)

    deps.onboarding_deps.event_publisher.publish(
        OfferAcceptedEvent(
            client_id=offer.client_id, offer_ref=offer.offer_ref, accepted_at=accepted_at, source="email"
        )
    )
    _audit(deps, "OFFER_ACCEPTED", {"offer_ref": offer.offer_ref}, offer.offer_ref)

    return try_start(offer.client_id, offer.offer_ref, deps)


def try_start(client_id: str, offer_ref: str, deps: IntakeDeps) -> IntakeResult:
    offer = deps.offer_repo.get(client_id, offer_ref)
    if offer is None:
        raise ValueError(f"no offer {offer_ref!r} for client {client_id!r}")

    form = deps.form_source.latest_for_offer(client_id, offer_ref)

    if deps.placement is None:
        # No location_name available: a case cannot be opened, and
        # guessing one would put fabricated placement data on a real
        # employee record. Park it the same way a missing form is
        # parked, so the offer is picked up once the value arrives.
        reasons = ["location_name_required"]
        pending = _ensure_waiting_pending_start(client_id, offer_ref, reasons, deps)
        return IntakeResult(
            outcome=IntakeOutcome.WAITING_FOR_FORM,
            pending_id=pending.pending_id,
            reasons=reasons,
        )

    request = OpenCaseRequest(
        request_id=new_id(),
        client_id=client_id,
        hr_officer_id=offer.hr_officer_id,
        offer_accepted=offer.status == OfferStatus.ACCEPTED,
        form=form,
        hiring_freeze=deps.hiring_freeze(client_id),
        placement=deps.placement,
    )
    result = open_onboarding_case(request, deps.onboarding_deps)

    if result.outcome in (OpenCaseOutcome.CASE_OPENED, OpenCaseOutcome.LINKED_EXISTING):
        waiting = deps.pending_repo.get_waiting(client_id, offer_ref)
        if waiting is not None:
            deps.pending_repo.update(waiting.model_copy(update={"status": PendingStartStatus.STARTED}))

        if result.outcome == OpenCaseOutcome.CASE_OPENED:
            assert result.case is not None
            linked_case_id = result.case.case_id
        else:
            linked_case_id = result.existing_case_id

        deps.onboarding_deps.audit_log.append(
            AuditEntry(
                case_id=linked_case_id,
                actor_type=ActorType.SYSTEM,
                actor_id="uc01-email-intake",
                action="OFFER_LINKED",
                after={
                    "offer_ref": offer_ref,
                    "accepted_at": offer.accepted_at.isoformat() if offer.accepted_at else None,
                    "source": "email",
                },
                reference=linked_case_id,
            )
        )

        if result.outcome == OpenCaseOutcome.CASE_OPENED:
            return IntakeResult(
                outcome=IntakeOutcome.CASE_OPENED,
                case_id=result.case.case_id,
                employee_no=result.case.employee_no,
            )
        return IntakeResult(outcome=IntakeOutcome.LINKED_EXISTING, case_id=result.existing_case_id)

    if result.outcome == OpenCaseOutcome.PAUSED:
        pending = _ensure_waiting_pending_start(client_id, offer_ref, ["hiring_freeze"], deps)
        return IntakeResult(
            outcome=IntakeOutcome.PAUSED, pending_id=pending.pending_id, reasons=["hiring_freeze"]
        )

    # HELD
    pending = _ensure_waiting_pending_start(client_id, offer_ref, result.reasons, deps)
    return IntakeResult(
        outcome=IntakeOutcome.WAITING_FOR_FORM, pending_id=pending.pending_id, reasons=result.reasons
    )


def handle_form_approved(client_id: str, offer_ref: str, deps: IntakeDeps) -> IntakeResult:
    waiting = deps.pending_repo.get_waiting(client_id, offer_ref)
    if waiting is None:
        return IntakeResult(outcome=IntakeOutcome.NOTHING_PENDING)
    return try_start(client_id, offer_ref, deps)


def handle_freeze_lifted(client_id: str, deps: IntakeDeps) -> list[IntakeResult]:
    """Retry every start this client paused for a hiring freeze.

    Closes known gap #32: a PAUSED pending start had no trigger, so
    lifting a freeze left those joiners stuck with nothing to re-check
    them. Callers must have lifted the freeze first - `hiring_freeze`
    is re-read by `try_start`, so a start whose freeze is still on
    simply stays paused rather than slipping through.

    Idempotent: a start that has already begun is no longer WAITING and
    is not retried.
    """
    waiting = [
        p for p in deps.pending_repo.list_waiting(client_id) if "hiring_freeze" in p.reasons
    ]
    if not waiting:
        return []

    _audit(
        deps,
        "HIRING_FREEZE_LIFTED",
        {"client_id": client_id, "pending_count": len(waiting)},
        client_id,
    )
    return [try_start(client_id, pending.offer_ref, deps) for pending in waiting]


def list_pending_starts(
    viewer: Viewer, client_id: str, deps: IntakeDeps, audit_log: AuditLog
) -> list[PendingStart]:
    require_role(viewer, WORKLIST_ROLES, audit_log, "list_pending_starts")
    if client_id not in viewer.client_ids:
        record_access_denied(audit_log, viewer, "list_pending_starts")
        raise AccessDeniedError()
    return deps.pending_repo.list_waiting(client_id)
