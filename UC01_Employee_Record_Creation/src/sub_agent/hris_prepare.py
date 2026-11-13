from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .deps import UC01Deps
from .hris_mapping import HRIS_FIELD_MAPPING, prepare_payloads, sensitive_field_names
from .models import (
    ActorType,
    Approval,
    ApprovalStatus,
    AuditEntry,
    CaseStatus,
    Checkpoint,
    DiscrepancyStatus,
    FormStatus,
    HrisRecord,
    HrisRecordStatus,
    HrisSystem,
)

# Order matters downstream (U7 writes people before payroll).
HRIS_SYSTEMS: tuple[HrisSystem, ...] = (HrisSystem.ZOHO_PEOPLE, HrisSystem.ZOHO_PAYROLL)


class PrepareOutcome(StrEnum):
    PREPARED = "PREPARED"
    ALREADY_PREPARED = "ALREADY_PREPARED"
    BLOCKED_CASE_NOT_ACTIVE = "BLOCKED_CASE_NOT_ACTIVE"
    PREPARED_UPDATE = "PREPARED_UPDATE"
    BLOCKED_UPDATE_NEEDED = "BLOCKED_UPDATE_NEEDED"
    BLOCKED_OPEN_DISCREPANCIES = "BLOCKED_OPEN_DISCREPANCIES"
    BLOCKED_PENDING_VARIANT_DECISION = "BLOCKED_PENDING_VARIANT_DECISION"
    BLOCKED_FORM_CHANGE_PENDING = "BLOCKED_FORM_CHANGE_PENDING"
    BLOCKED_HRIS_REJECTED = "BLOCKED_HRIS_REJECTED"


INACTIVE_CASE_STATUSES = (CaseStatus.ON_HOLD, CaseStatus.WITHDRAWN, CaseStatus.CLOSED)


class PrepareResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: PrepareOutcome
    record_ids: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    blockers: list[str] = Field(default_factory=list)


def _audit_blocked(deps: UC01Deps, case_id: str, outcome: PrepareOutcome, blockers: list[str]) -> None:
    deps.audit_log.append(
        AuditEntry(
            case_id=case_id,
            actor_type=ActorType.AGENT,
            actor_id="uc01-hris-prepare",
            action="HRIS_PREPARE_BLOCKED",
            after={"outcome": outcome.value, "ids": blockers},
            reference=case_id,
        )
    )


def prepare_hris_records(
    case_id: str, deps: UC01Deps, requested_by: Optional[str] = None
) -> PrepareResult:
    case = deps.case_repo.get(case_id)
    if case is None:
        raise ValueError(f"no case with id {case_id!r}")

    form = deps.form_repo.latest_approved(case_id)
    if form is None:
        raise ValueError(f"no approved form in force for case {case_id!r}")

    # Checked FIRST, before every other blocker (including update-needed):
    # an inactive case (on hold / withdrawn / closed) is never a target
    # for a new HRIS write.
    if case.status in INACTIVE_CASE_STATUSES:
        blockers = [case.case_id]
        _audit_blocked(deps, case_id, PrepareOutcome.BLOCKED_CASE_NOT_ACTIVE, blockers)
        return PrepareResult(outcome=PrepareOutcome.BLOCKED_CASE_NOT_ACTIVE, blockers=blockers)

    # If a people/payroll record from an older form version was already
    # WRITTEN, the new version must UPDATE that employee rather than
    # create a second one. Those records are prepared further down with
    # `updates_external_ref` set; here we only reject the case we cannot
    # address: a written record with no external reference to update.
    written_at_older_version = [
        r
        for r in deps.hris_record_repo.list_for_case(case_id)
        if r.status == HrisRecordStatus.WRITTEN and r.form_version < form.version
    ]
    unaddressable = [r for r in written_at_older_version if not r.external_ref]
    if unaddressable:
        blockers = [r.record_id for r in unaddressable]
        _audit_blocked(deps, case_id, PrepareOutcome.BLOCKED_UPDATE_NEEDED, blockers)
        return PrepareResult(outcome=PrepareOutcome.BLOCKED_UPDATE_NEEDED, blockers=blockers)

    open_discrepancies = [
        d for d in deps.discrepancy_repo.list_for_case(case_id) if d.status == DiscrepancyStatus.OPEN
    ]
    if open_discrepancies:
        blockers = [d.discrepancy_id for d in open_discrepancies]
        _audit_blocked(deps, case_id, PrepareOutcome.BLOCKED_OPEN_DISCREPANCIES, blockers)
        return PrepareResult(outcome=PrepareOutcome.BLOCKED_OPEN_DISCREPANCIES, blockers=blockers)

    # A "possible_duplicate" notice is non-blocking (EC-39): it's an HR
    # notice, not a real variant decision. Every other PENDING HC_12
    # approval (rehire, internal transfer, declared minor/contractor, ...)
    # blocks until HR decides.
    pending_variant_approvals = [
        a
        for a in deps.approval_repo.list_for_case(case_id)
        if a.checkpoint == Checkpoint.HC_12_VARIANT
        and a.status == ApprovalStatus.PENDING
        and a.subject_ref != "possible_duplicate"
    ]
    if pending_variant_approvals:
        blockers = [a.approval_id for a in pending_variant_approvals]
        _audit_blocked(deps, case_id, PrepareOutcome.BLOCKED_PENDING_VARIANT_DECISION, blockers)
        return PrepareResult(outcome=PrepareOutcome.BLOCKED_PENDING_VARIANT_DECISION, blockers=blockers)

    pending_forms = [
        f for f in deps.form_repo.list_versions(case_id) if f.status == FormStatus.AWAITING_APPROVAL
    ]
    if pending_forms:
        blockers = [f.form_id for f in pending_forms]
        _audit_blocked(deps, case_id, PrepareOutcome.BLOCKED_FORM_CHANGE_PENDING, blockers)
        return PrepareResult(outcome=PrepareOutcome.BLOCKED_FORM_CHANGE_PENDING, blockers=blockers)

    # Rejection is terminal for a form version [Proposed, confirm with
    # SME]: once HR rejects a write, preparing again for the SAME version
    # is blocked. The only way forward is a form change (a new version),
    # whose records no longer carry this rejection.
    rejected_records = [
        r
        for r in deps.hris_record_repo.list_for_case(case_id)
        if r.form_version == form.version and r.status == HrisRecordStatus.REJECTED
    ]
    if rejected_records:
        blockers = [r.record_id for r in rejected_records]
        _audit_blocked(deps, case_id, PrepareOutcome.BLOCKED_HRIS_REJECTED, blockers)
        return PrepareResult(outcome=PrepareOutcome.BLOCKED_HRIS_REJECTED, blockers=blockers)

    existing = [
        r
        for r in deps.hris_record_repo.list_for_case(case_id)
        if r.form_version == form.version and r.status != HrisRecordStatus.STALE
    ]
    if existing:
        return PrepareResult(
            outcome=PrepareOutcome.ALREADY_PREPARED,
            record_ids=[r.record_id for r in existing],
        )

    # The employee already written for each system, if any. Its external
    # reference turns this batch from a create into an in-place update.
    superseded_by_system = {
        r.system: r for r in sorted(written_at_older_version, key=lambda r: r.form_version)
    }

    # A rehire or internal transfer reactivates the employee the person
    # already has in the target system (EC-43). HR has approved the
    # HC_12 variant by this point - a pending one blocks above.
    reactivation_ref = case.reactivates_external_ref

    record_ids: list[str] = []
    for system in HRIS_SYSTEMS:
        payload = prepare_payloads(system, case, form)
        superseded = superseded_by_system.get(system)
        record = HrisRecord(
            case_id=case_id,
            client_id=case.client_id,
            employee_no=case.employee_no,
            system=system,
            form_version=form.version,
            prepared_payload=payload,
            sensitive_field_names=sensitive_field_names(system),
            status=HrisRecordStatus.PREPARED,
            updates_external_ref=(superseded.external_ref if superseded else reactivation_ref),
        )
        deps.hris_record_repo.add(record)
        record_ids.append(record.record_id)

    field_names = sorted(
        {field.our_field for system in HRIS_SYSTEMS for field in HRIS_FIELD_MAPPING[system]}
    )
    approval = Approval(
        case_id=case_id,
        checkpoint=Checkpoint.HC_05_HRIS_WRITE,
        subject_ref=f"hris:{case_id}:v{form.version}",
        requested_to=case.hr_officer_id,
        requested_by=requested_by,
        reason=(
            f"prepare HRIS write for systems={[s.value for s in HRIS_SYSTEMS]}, "
            f"fields={field_names}"
        ),
    )
    deps.approval_repo.add(approval)

    deps.audit_log.append(
        AuditEntry(
            case_id=case_id,
            actor_type=ActorType.HUMAN if requested_by else ActorType.AGENT,
            actor_id=requested_by or "uc01-hris-prepare",
            action="HRIS_PREPARED",
            after={"form_version": form.version, "record_count": len(record_ids)},
            reference=case_id,
        )
    )

    is_update = bool(written_at_older_version) or reactivation_ref is not None
    outcome = PrepareOutcome.PREPARED_UPDATE if is_update else PrepareOutcome.PREPARED
    return PrepareResult(
        outcome=outcome,
        record_ids=record_ids,
        approval_id=approval.approval_id,
    )
