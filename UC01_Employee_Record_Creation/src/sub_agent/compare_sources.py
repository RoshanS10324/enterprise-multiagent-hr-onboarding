import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, Field

from .models import (
    ActorType,
    Approval,
    ApprovalStatus,
    AuditEntry,
    Case,
    Checkpoint,
    Discrepancy,
    DiscrepancyStatus,
    FormStatus,
    OnboardingForm,
    SourceSnapshot,
)
from .llm import LlmClient, LlmError, LlmRequest, audit_action_for, draft_fingerprint
from .models.common import utc_now
from .ports import ApprovalRepository, AuditLog, DiscrepancyRepository


class DiscrepancyDecision(StrEnum):
    KEEP_FORM_VALUE = "KEEP_FORM_VALUE"
    ACCEPT_AS_CHANGE = "ACCEPT_AS_CHANGE"


class CompareResult(BaseModel):
    new_discrepancies: list[str] = Field(default_factory=list)
    already_known: int = 0
    approvals_created: list[str] = Field(default_factory=list)


@dataclass
class CompareDeps:
    discrepancy_repo: DiscrepancyRepository
    approval_repo: ApprovalRepository
    audit_log: AuditLog
    # Required, not defaulted: a de-duplication fingerprint key must be
    # supplied explicitly rather than silently falling back to something
    # insecure. In production this comes from a secret store, never from
    # .env (see docs/known-gaps.md).
    fingerprint_key: bytes
    # Advisory only, off by default. Drafts a plain-language summary
    # of what differs, for HR to read. Never decides anything, and
    # never sees a conflicting VALUE -- only field and source names.
    llm: Optional[LlmClient] = None


def _fingerprint(value: str, key: bytes) -> str:
    # HMAC rather than a plain hash: a plain sha256 digest of a small,
    # guessable value space (e.g. salary figures) could be brute-forced
    # offline; keying it closes that off.
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def _normalize_work_location(value: str) -> str:
    # Accepts "client site", "Client-Site", "CLIENT_SITE", etc. as the same
    # value: collapse whitespace/hyphens to underscores, then uppercase.
    collapsed = re.sub(r"[\s\-]+", "_", value.strip())
    return collapsed.upper()


def _compare_field(field: str, other_value: str, form: OnboardingForm) -> tuple[bool, str, str]:
    """Returns (differs, value_form, value_other) for the discrepancy record."""
    if field == "candidate_name":
        differs = _normalize_name(other_value) != _normalize_name(form.candidate_name)
        return differs, form.candidate_name, other_value

    if field == "designation":
        differs = other_value.strip().lower() != form.designation.strip().lower()
        return differs, form.designation, other_value

    if field == "work_location_type":
        differs = _normalize_work_location(other_value) != _normalize_work_location(
            form.work_location_type.value
        )
        return differs, form.work_location_type.value, other_value

    if field == "joining_date":
        try:
            parsed = date.fromisoformat(other_value.strip())
        except ValueError:
            # Unparseable is itself a discrepancy, not an error.
            return True, form.joining_date.isoformat(), other_value
        return parsed != form.joining_date, form.joining_date.isoformat(), other_value

    if field == "salary_and_terms":
        differs = other_value != form.salary_and_terms.get_secret_value()
        if differs:
            return True, "[REDACTED]", "[REDACTED]"
        return False, "", ""

    # Unreachable: SourceSnapshot only allows the fields handled above.
    raise ValueError(f"unsupported comparison field: {field}")


def _build_reason(field: str, source: str) -> str:
    # One generic reason for every source. UC-01 does not know or care
    # what kind of record `source` names: identity-document checks and
    # their rules belong to the document-validation use case, not here.
    # `source` is free text supplied by the caller and is echoed as-is.
    return f"{field} differs between the approved form and {source}"


def _draft_discrepancy_summary(
    case_id: str,
    form_version: int,
    fields: list[str],
    other_source: str,
    deps: CompareDeps,
) -> None:
    """Draft a note for HR about what differs. DRAFT_FOR_HUMAN only: the
    discrepancy records remain the record of what differs, and nothing
    here changes a case.

    Field names and the source name only -- never the conflicting
    values, so a salary discrepancy can be summarised without either
    figure reaching the model.
    """
    if deps.llm is None or not fields:
        return

    try:
        response = deps.llm.complete(
            LlmRequest(
                task="discrepancy_summary",
                inputs={
                    "case_id": case_id,
                    "fields_in_conflict": ", ".join(sorted(fields)),
                    "other_source": other_source,
                    "form_version": str(form_version),
                },
            )
        )
    except LlmError as exc:
        # A model outage or refusal must never block flagging a discrepancy.
        deps.audit_log.append(
            AuditEntry(
                case_id=case_id,
                actor_type=ActorType.SYSTEM,
                actor_id="uc01-compare-sources",
                action=audit_action_for(exc),
                after={"task": "discrepancy_summary"},
                reference=case_id,
            )
        )
        return

    deps.audit_log.append(
        AuditEntry(
            case_id=case_id,
            actor_type=ActorType.AGENT,
            actor_id="uc01-compare-sources",
            action="DISCREPANCY_SUMMARY_DRAFTED",
            after={
                "task": "discrepancy_summary",
                "task_version": response.task_version,
                "model": response.model,
                "fields": sorted(fields),
                "other_source": other_source,
                # Provenance, not content (llm.audited_without_content).
                **draft_fingerprint(response.text),
            },
            reference=case_id,
        )
    )


def compare_sources(
    case: Case,
    form: OnboardingForm,
    snapshots: list[SourceSnapshot],
    deps: CompareDeps,
    actor_id: Optional[str] = None,
) -> CompareResult:
    # When a human triggers this (via CaseService), actor_id is their
    # viewer.user_id and audit entries say so; otherwise it's an
    # unattributed agent/system run.
    audit_actor_type = ActorType.HUMAN if actor_id else ActorType.AGENT
    audit_actor_id = actor_id or "uc01-compare-sources"

    if form.status != FormStatus.APPROVED:
        raise ValueError("form must be APPROVED to compare sources")
    if form.case_id != case.case_id:
        raise ValueError(
            f"form is linked to case {form.case_id!r}, not the given case {case.case_id!r}"
        )
    if form.client_id != case.client_id:
        raise ValueError("form.client_id does not match case.client_id")

    # The approved form is the source of truth and is never written to by
    # this function, nor is the case or any other record: only new
    # Discrepancy/Approval rows and audit entries are created.
    existing_for_case = deps.discrepancy_repo.list_for_case(case.case_id)

    new_discrepancies: list[str] = []
    approvals_created: list[str] = []
    already_known = 0
    flagged_by_source: dict[str, list[str]] = {}

    for snapshot in snapshots:
        for field, other_value in snapshot.values.items():
            differs, value_form, value_other = _compare_field(field, other_value, form)
            if not differs:
                continue

            fingerprint = _fingerprint(other_value, deps.fingerprint_key)
            is_duplicate = any(
                d.field == field
                and d.other_source == snapshot.source
                and d.other_fingerprint == fingerprint
                and d.form_version == form.version
                for d in existing_for_case
            )
            if is_duplicate:
                already_known += 1
                continue

            discrepancy = Discrepancy(
                case_id=case.case_id,
                field=field,
                value_form=value_form,
                value_other=value_other,
                other_source=snapshot.source,
                form_version=form.version,
                status=DiscrepancyStatus.OPEN,
                other_fingerprint=fingerprint,
            )
            deps.discrepancy_repo.add(discrepancy)
            # So a second difference in the same call against the same
            # other-value is deduped too, not just across separate calls.
            existing_for_case.append(discrepancy)
            new_discrepancies.append(discrepancy.discrepancy_id)

            approval = Approval(
                case_id=case.case_id,
                checkpoint=Checkpoint.HC_04_DATA,
                subject_ref=f"discrepancy:{discrepancy.discrepancy_id}",
                requested_to=case.hr_officer_id,
                reason=_build_reason(field, snapshot.source),
            )
            deps.approval_repo.add(approval)
            approvals_created.append(approval.approval_id)
            flagged_by_source.setdefault(snapshot.source, []).append(field)

            deps.audit_log.append(
                AuditEntry(
                    case_id=case.case_id,
                    actor_type=audit_actor_type,
                    actor_id=audit_actor_id,
                    action="DISCREPANCY_FLAGGED",
                    after={
                        "discrepancy_id": discrepancy.discrepancy_id,
                        "field": field,
                        "other_source": snapshot.source,
                    },
                    reference=case.case_id,
                )
            )

    # Advisory draft for HR, after the facts are recorded. Nothing above
    # depends on it, so an outage changes nothing.
    for source, fields in sorted(flagged_by_source.items()):
        _draft_discrepancy_summary(case.case_id, form.version, fields, source, deps)

    deps.audit_log.append(
        AuditEntry(
            case_id=case.case_id,
            actor_type=audit_actor_type,
            actor_id=audit_actor_id,
            action="COMPARE_COMPLETED",
            after={
                "new_discrepancies": len(new_discrepancies),
                "already_known": already_known,
                "approvals_created": len(approvals_created),
            },
            reference=case.case_id,
        )
    )

    return CompareResult(
        new_discrepancies=new_discrepancies,
        already_known=already_known,
        approvals_created=approvals_created,
    )


def resolve_discrepancy(
    discrepancy_id: str,
    decision: DiscrepancyDecision,
    decided_by: str,
    deps: CompareDeps,
) -> Discrepancy:
    discrepancy = deps.discrepancy_repo.get(discrepancy_id)
    if discrepancy is None:
        raise ValueError(f"no discrepancy with id {discrepancy_id!r}")
    if discrepancy.status != DiscrepancyStatus.OPEN:
        raise ValueError(
            f"discrepancy {discrepancy_id!r} is already decided (status {discrepancy.status})"
        )

    decided_at = utc_now()
    new_status = (
        DiscrepancyStatus.RESOLVED
        if decision == DiscrepancyDecision.KEEP_FORM_VALUE
        else DiscrepancyStatus.ACCEPTED_AS_CHANGE
    )

    # ACCEPT_AS_CHANGE only records the decision here; it never edits the
    # form. The actual change goes through the form-versioning flow (U8).
    discrepancy.status = new_status
    discrepancy.decided_by = decided_by
    discrepancy.decided_at = decided_at
    deps.discrepancy_repo.update(discrepancy)

    subject_ref = f"discrepancy:{discrepancy_id}"
    matching_approval = next(
        (
            a
            for a in deps.approval_repo.list_for_case(discrepancy.case_id)
            if a.subject_ref == subject_ref
        ),
        None,
    )
    if matching_approval is not None:
        matching_approval.status = ApprovalStatus.APPROVED
        matching_approval.decided_by = decided_by
        matching_approval.decided_at = decided_at
        deps.approval_repo.update(matching_approval)

    deps.audit_log.append(
        AuditEntry(
            case_id=discrepancy.case_id,
            actor_type=ActorType.HUMAN,
            actor_id=decided_by,
            action="DISCREPANCY_DECIDED",
            after={"discrepancy_id": discrepancy_id, "decision": decision.value},
            reference=discrepancy.case_id,
        )
    )

    return discrepancy
