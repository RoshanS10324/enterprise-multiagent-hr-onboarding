from enum import StrEnum
from typing import Optional

from pydantic import BaseModel

from .models import ActorType, AuditEntry, PersonRecord, PersonRecordKind, VariantFlags
from .ports import AuditLog, PersonDirectory

# Assumption: rehire/transfer are identified by email or id_number (open question Q-23).

_AMBIGUOUS_REASON = (
    "{n} existing employee records matched, so no single record can be "
    "reactivated; HR must choose, or the request proceeds as a new employee"
)


def _sole_match(matches: list[PersonRecord]) -> tuple[Optional[PersonRecord], bool]:
    """The one record to reactivate, or (None, True) when ambiguous.

    More than one match of the same kind means two distinct employees
    share an email or an id number. Reactivating `matches[0]` would be
    an arbitrary pick between two real people, and could attach a new
    joiner to the wrong person's HRIS record. The caller keeps the
    REHIRE / INTERNAL_TRANSFER outcome - HR is still gated on the
    variant - but is given nothing to reactivate.
    """
    if len(matches) == 1:
        return matches[0], False
    return None, True


class DuplicateOutcome(StrEnum):
    NEW = "NEW"
    DUPLICATE_OPEN_CASE = "DUPLICATE_OPEN_CASE"
    REHIRE = "REHIRE"
    INTERNAL_TRANSFER = "INTERNAL_TRANSFER"
    POSSIBLE_DUPLICATE_NEEDS_HR = "POSSIBLE_DUPLICATE_NEEDS_HR"


class DuplicateCheckRequest(BaseModel):
    request_id: str
    client_id: str
    candidate_name: str
    candidate_email: str
    id_number: Optional[str] = None


class DuplicateCheckResult(BaseModel):
    outcome: DuplicateOutcome
    existing_case_id: Optional[str] = None
    matched_record_ids: list[str]
    # The single existing person a rehire/internal transfer should
    # reactivate, chosen from matched_record_ids. None otherwise, and
    # None when the match is ambiguous (see reactivation_ambiguous).
    reactivation_record_id: Optional[str] = None
    reactivation_external_ref: Optional[str] = None
    # True when MORE THAN ONE existing employee of the deciding kind
    # matched, so there is no single record to reactivate. The outcome
    # stays REHIRE/INTERNAL_TRANSFER (HR is still gated on the variant),
    # but no reactivation is proposed: picking one of several real
    # employees arbitrarily could attach a new joiner to the wrong
    # person's HRIS record. HR chooses, or the request proceeds as a new
    # employee.
    reactivation_ambiguous: bool = False
    variants: VariantFlags
    needs_hr_decision: bool
    reasons: list[str]


def check_duplicates(
    request: DuplicateCheckRequest,
    directory: PersonDirectory,
    audit_log: AuditLog,
) -> DuplicateCheckResult:
    by_email = directory.find_by_email(request.client_id, request.candidate_email)
    by_id = (
        directory.find_by_id_number(request.client_id, request.id_number)
        if request.id_number
        else []
    )

    # Dedupe in case the same record matched on both email and id_number.
    matches_by_id = {}
    for record in (*by_email, *by_id):
        matches_by_id[record.record_id] = record
    matches = list(matches_by_id.values())

    variants = VariantFlags()
    reasons: list[str] = []
    existing_case_id: Optional[str] = None
    reactivation: Optional[PersonRecord] = None
    ambiguous = False

    if matches:
        matched_record_ids = [r.record_id for r in matches]

        # Priority among matched kinds: OPEN_CASE, then CURRENT_EMPLOYEE,
        # then PAST_EMPLOYEE.
        open_case_matches = [r for r in matches if r.kind == PersonRecordKind.OPEN_CASE]
        current_matches = [r for r in matches if r.kind == PersonRecordKind.CURRENT_EMPLOYEE]
        past_matches = [r for r in matches if r.kind == PersonRecordKind.PAST_EMPLOYEE]

        if open_case_matches:
            outcome = DuplicateOutcome.DUPLICATE_OPEN_CASE
            existing_case_id = open_case_matches[0].case_id
            needs_hr_decision = False
            reasons.append("matched an existing open case by email or id number")
        elif current_matches:
            outcome = DuplicateOutcome.INTERNAL_TRANSFER
            variants.is_internal_transfer = True
            needs_hr_decision = True
            reactivation, ambiguous = _sole_match(current_matches)
            reasons.append("matched a current employee by email or id number")
            if ambiguous:
                reasons.append(_AMBIGUOUS_REASON.format(n=len(current_matches)))
        elif past_matches:
            outcome = DuplicateOutcome.REHIRE
            variants.is_rehire = True
            needs_hr_decision = True
            reactivation, ambiguous = _sole_match(past_matches)
            reasons.append("matched a past employee by email or id number")
            if ambiguous:
                reasons.append(_AMBIGUOUS_REASON.format(n=len(past_matches)))
        else:
            # Unreachable with the current PersonRecordKind enum, kept so
            # new kinds fail loudly instead of silently falling through.
            raise ValueError(f"unhandled matched record kinds: {matches}")
    else:
        # A name match alone is never treated as a match: it only raises a
        # flag for HR review, it never drives an automatic outcome.
        name_matches = directory.find_by_name(request.client_id, request.candidate_name)
        if name_matches:
            outcome = DuplicateOutcome.POSSIBLE_DUPLICATE_NEEDS_HR
            needs_hr_decision = True
            matched_record_ids = [r.record_id for r in name_matches]
            reasons.append(
                "name matches an existing record but email/id number do not; needs HR review"
            )
        else:
            outcome = DuplicateOutcome.NEW
            needs_hr_decision = False
            matched_record_ids = []
            reasons.append("no matching record found")

    result = DuplicateCheckResult(
        outcome=outcome,
        existing_case_id=existing_case_id,
        matched_record_ids=matched_record_ids,
        reactivation_record_id=reactivation.record_id if reactivation else None,
        reactivation_external_ref=reactivation.hris_external_ref if reactivation else None,
        reactivation_ambiguous=ambiguous,
        variants=variants,
        needs_hr_decision=needs_hr_decision,
        reasons=reasons,
    )

    audit_log.append(
        AuditEntry(
            request_id=request.request_id,
            actor_type=ActorType.AGENT,
            actor_id="uc01-duplicate-check",
            action="DUPLICATE_CHECK_EVALUATED",
            after={
                "outcome": result.outcome.value,
                "matched_record_ids": result.matched_record_ids,
            },
            reference=request.request_id,
        )
    )

    return result
