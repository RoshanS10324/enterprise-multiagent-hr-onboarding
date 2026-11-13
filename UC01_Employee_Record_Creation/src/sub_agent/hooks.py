"""Pre/post write hooks for execute_hris_write (AC-15 field-for-field
check, kill switch, and post-write invariant auditing).
"""

from typing import Optional

from .deps import UC01Deps
from .hris_mapping import prepare_payloads, sensitive_field_names
from .invariants import check_case_invariants
from .models import (
    ActorType,
    Approval,
    ApprovalStatus,
    AuditEntry,
    CaseStatus,
    Checkpoint,
    HrisRecord,
    OnboardingForm,
)
from .policy import Policy

INACTIVE_CASE_STATUSES = (CaseStatus.ON_HOLD, CaseStatus.WITHDRAWN, CaseStatus.CLOSED)


def before_hris_write(
    record: HrisRecord,
    form: OnboardingForm,
    approval: Optional[Approval],
    deps: UC01Deps,
) -> Optional[str]:
    """Returns a block code, or None to proceed. Checked in this order:

    WRITES_DISABLED, APPROVAL_NOT_APPROVED, APPROVAL_SUBJECT_MISMATCH,
    VERSION_NOT_IN_FORCE, PAYLOAD_MISMATCH (the AC-15 field-for-field
    check), CASE_NOT_ACTIVE.
    """
    if not deps.hris_writes_enabled():
        return "WRITES_DISABLED"

    if approval is None or approval.status != ApprovalStatus.APPROVED or approval.decided_by is None:
        return "APPROVAL_NOT_APPROVED"

    # Re-derive the binding from the record itself rather than trusting
    # the approval the caller handed us. execute_hris_write looks the
    # approval up by subject_ref, so today these always agree; checking
    # here means a future caller cannot pass an approval for a different
    # case, a different form version, or a different checkpoint and have
    # it honoured.
    if (
        approval.checkpoint != Checkpoint.HC_05_HRIS_WRITE
        or approval.subject_ref != f"hris:{record.case_id}:v{record.form_version}"
    ):
        return "APPROVAL_SUBJECT_MISMATCH"

    if record.form_version != form.version:
        return "VERSION_NOT_IN_FORCE"

    case = deps.case_repo.get(record.case_id)
    rebuilt_payload = prepare_payloads(record.system, case, form) if case is not None else None
    if (
        case is None
        or rebuilt_payload != record.prepared_payload
        or sensitive_field_names(record.system) != record.sensitive_field_names
    ):
        return "PAYLOAD_MISMATCH"

    if case.status in INACTIVE_CASE_STATUSES:
        return "CASE_NOT_ACTIVE"

    return None


def after_hris_write(case_id: str, deps: UC01Deps, policy: Optional[Policy]) -> None:
    """Runs check_case_invariants and audits every violation found. Never
    raises: a broken invariant is a signal to investigate, not a reason to
    fail the write that already happened.
    """
    violations = check_case_invariants(case_id, deps, policy=policy)
    for violation in violations:
        deps.audit_log.append(
            AuditEntry(
                case_id=case_id,
                actor_type=ActorType.AGENT,
                actor_id="uc01-invariants",
                action="INVARIANT_VIOLATION",
                after={"code": violation.code.value, "detail": violation.detail},
                reference=case_id,
            )
        )
