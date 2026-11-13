from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .deps import UC01Deps
from .models import (
    ActorType,
    Approval,
    ApprovalStatus,
    AuditEntry,
    Case,
    Checkpoint,
    DiscrepancyStatus,
    FormChangeRequest,
    FormChangedEvent,
    FormStatus,
    HrisRecordStatus,
    OnboardingForm,
)
from .models.common import utc_now

COMPARABLE_FIELDS = (
    "candidate_name",
    "designation",
    "joining_date",
    "work_location_type",
    "client_site_id",
    "salary_and_terms",
)


def _diff_fields(old: OnboardingForm, new: OnboardingForm) -> list[str]:
    changed = []
    for name in COMPARABLE_FIELDS:
        if name == "salary_and_terms":
            old_value: Any = old.salary_and_terms.get_secret_value()
            new_value: Any = new.salary_and_terms.get_secret_value()
        else:
            old_value = getattr(old, name)
            new_value = getattr(new, name)
        if old_value != new_value:
            changed.append(name)
    return changed


class ProposeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    new_form_id: str
    new_version: int
    approval_id: str
    changed_fields: list[str]
    warnings: list[str] = Field(default_factory=list)


def _require_approval(approval_id: str, deps: UC01Deps) -> Approval:
    approval = deps.approval_repo.get(approval_id)
    if approval is None:
        raise ValueError(f"no approval with id {approval_id!r}")
    if approval.status != ApprovalStatus.PENDING:
        raise ValueError(f"approval {approval_id!r} is not PENDING (status {approval.status})")
    if approval.checkpoint != Checkpoint.HC_01_FORM:
        raise ValueError(f"approval {approval_id!r} is not an HC_01_FORM approval")
    if not approval.subject_ref.startswith("form:"):
        raise ValueError(f"approval {approval_id!r} has an unexpected subject_ref")
    return approval


def _find_form(approval: Approval, deps: UC01Deps) -> OnboardingForm:
    form_id = approval.subject_ref.split(":", 1)[1]
    form = next(
        (f for f in deps.form_repo.list_versions(approval.case_id) if f.form_id == form_id),
        None,
    )
    if form is None:
        raise ValueError(f"no form with id {form_id!r} for case {approval.case_id!r}")
    return form


def propose_form_change(
    request: FormChangeRequest,
    deps: UC01Deps,
    approver_id: str,
) -> ProposeResult:
    case = deps.case_repo.get(request.case_id)
    if case is None:
        raise ValueError(f"no case with id {request.case_id!r}")

    in_force = deps.form_repo.latest_approved(request.case_id)
    if in_force is None:
        raise ValueError(f"no approved form in force for case {request.case_id!r}")

    # Only one pending change per case.
    if any(
        f.status == FormStatus.AWAITING_APPROVAL for f in deps.form_repo.list_versions(case.case_id)
    ):
        raise ValueError(f"case {case.case_id!r} already has a change awaiting approval")

    field_updates: dict[str, Any] = {"change_reason": request.change_reason}
    for name in ("candidate_name", "designation", "joining_date", "work_location_type", "client_site_id"):
        value = getattr(request, name)
        if value is not None:
            field_updates[name] = value
    if request.salary_and_terms is not None:
        field_updates["salary_and_terms"] = request.salary_and_terms

    new_form = in_force.next_version(**field_updates).submit_for_approval()

    changed_fields = _diff_fields(in_force, new_form)
    if not changed_fields:
        raise ValueError("no change: every requested value matches the in-force form")

    if request.source_discrepancy_id is not None:
        discrepancy = deps.discrepancy_repo.get(request.source_discrepancy_id)
        if discrepancy is None or discrepancy.case_id != case.case_id:
            raise ValueError(
                f"source_discrepancy_id {request.source_discrepancy_id!r} does not exist "
                f"or does not belong to case {case.case_id!r}"
            )
        if discrepancy.status != DiscrepancyStatus.ACCEPTED_AS_CHANGE:
            raise ValueError(
                f"source_discrepancy_id {request.source_discrepancy_id!r} must have status "
                f"ACCEPTED_AS_CHANGE (got {discrepancy.status})"
            )
        if discrepancy.field not in changed_fields:
            raise ValueError(
                f"source_discrepancy_id {request.source_discrepancy_id!r} field "
                f"{discrepancy.field!r} is not among the changed fields {changed_fields}"
            )

    deps.form_repo.save(new_form)

    approval = Approval(
        case_id=case.case_id,
        checkpoint=Checkpoint.HC_01_FORM,
        subject_ref=f"form:{new_form.form_id}",
        requested_to=approver_id,
        requested_by=request.requested_by,
        reason=f"form change proposed: {', '.join(changed_fields)}",
    )
    deps.approval_repo.add(approval)

    warnings: list[str] = []
    if "joining_date" in changed_fields:
        new_date = new_form.joining_date
        if new_date.weekday() >= 5:
            warnings.append("joining date falls on a weekend")
        if deps.holiday_calendar.is_holiday(case.client_id, new_date):
            warnings.append("joining date falls on a holiday")
        if new_date < deps.today():
            warnings.append("joining date is in the past")

    deps.audit_log.append(
        AuditEntry(
            case_id=case.case_id,
            actor_type=ActorType.HUMAN,
            actor_id=request.requested_by,
            action="FORM_CHANGE_PROPOSED",
            after={
                "new_version": new_form.version,
                "changed_fields": changed_fields,
                "warnings": warnings,
            },
            reference=case.case_id,
        )
    )

    return ProposeResult(
        new_form_id=new_form.form_id,
        new_version=new_form.version,
        approval_id=approval.approval_id,
        changed_fields=changed_fields,
        warnings=warnings,
    )


def approve_form_change(approval_id: str, decided_by: str, deps: UC01Deps) -> Case:
    approval = _require_approval(approval_id, deps)
    new_form = _find_form(approval, deps)

    case = deps.case_repo.get(approval.case_id)
    if case is None:
        raise ValueError(f"no case with id {approval.case_id!r}")

    in_force = deps.form_repo.latest_approved(case.case_id)
    if in_force is None:
        raise ValueError(f"no approved form in force for case {case.case_id!r}")

    if new_form.status != FormStatus.AWAITING_APPROVAL:
        raise ValueError(f"new form is not AWAITING_APPROVAL (status {new_form.status})")
    if new_form.client_id != case.client_id:
        raise ValueError("new form client_id does not match case client_id")
    if new_form.version != in_force.version + 1:
        raise ValueError(
            f"new form version {new_form.version} is not in-force version "
            f"{in_force.version} + 1"
        )

    decided_at = utc_now()
    approved_form = new_form.approve(decided_by, decided_at)
    superseded_form = in_force.supersede()
    changed_fields = _diff_fields(in_force, approved_form)

    # Order of saves: new approved form, old superseded form, approval,
    # case, discrepancies, event (see docs/known-gaps.md: not atomic).
    deps.form_repo.save(approved_form)
    deps.form_repo.save(superseded_form)

    approval.status = ApprovalStatus.APPROVED
    approval.decided_by = decided_by
    approval.decided_at = decided_at
    deps.approval_repo.update(approval)

    case.current_form_version = approved_form.version
    case.joining_date = approved_form.joining_date
    case.work_location_type = approved_form.work_location_type
    case.candidate_name = approved_form.candidate_name
    deps.case_repo.save(case)

    # Discrepancies raised against the old version, on a field that just
    # changed, no longer need an HR decision: the new form may already
    # match (or no longer matter). HR re-runs compare_sources afterwards.
    for discrepancy in deps.discrepancy_repo.list_for_case(case.case_id):
        if discrepancy.status != DiscrepancyStatus.OPEN or discrepancy.field not in changed_fields:
            continue
        discrepancy.status = DiscrepancyStatus.STALE
        deps.discrepancy_repo.update(discrepancy)

        subject_ref = f"discrepancy:{discrepancy.discrepancy_id}"
        pending_approval = next(
            (
                a
                for a in deps.approval_repo.list_for_case(case.case_id)
                if a.subject_ref == subject_ref and a.status == ApprovalStatus.PENDING
            ),
            None,
        )
        if pending_approval is not None:
            pending_approval.status = ApprovalStatus.CANCELLED
            deps.approval_repo.update(pending_approval)

    # HRIS records prepared against an older, now-superseded form version
    # that haven't been written yet are stale: the data they were built
    # from no longer matches the form. Records already WRITTEN stay WRITTEN
    # (updating a written record needs its own flow - see docs/known-gaps.md).
    staled_count = 0
    for hris_record in deps.hris_record_repo.list_for_case(case.case_id):
        if (
            hris_record.form_version < approved_form.version
            and hris_record.status not in (HrisRecordStatus.WRITTEN, HrisRecordStatus.STALE)
        ):
            hris_record.status = HrisRecordStatus.STALE
            deps.hris_record_repo.update(hris_record)
            staled_count += 1

    cancelled_hc05_count = 0
    if staled_count:
        for pending_hc05 in deps.approval_repo.list_for_case(case.case_id):
            if (
                pending_hc05.status == ApprovalStatus.PENDING
                and pending_hc05.checkpoint == Checkpoint.HC_05_HRIS_WRITE
            ):
                pending_hc05.status = ApprovalStatus.CANCELLED
                deps.approval_repo.update(pending_hc05)
                cancelled_hc05_count += 1

        deps.audit_log.append(
            AuditEntry(
                case_id=case.case_id,
                actor_type=ActorType.AGENT,
                actor_id="uc01-form-changes",
                action="HRIS_RECORDS_STALED",
                after={"staled_count": staled_count, "cancelled_approvals": cancelled_hc05_count},
                reference=case.case_id,
            )
        )

    event = FormChangedEvent(
        case_id=case.case_id,
        client_id=case.client_id,
        old_version=in_force.version,
        new_version=approved_form.version,
        changed_fields=changed_fields,
        joining_date_changed="joining_date" in changed_fields,
    )
    deps.publisher.publish(event)

    deps.audit_log.append(
        AuditEntry(
            case_id=case.case_id,
            actor_type=ActorType.HUMAN,
            actor_id=decided_by,
            action="FORM_CHANGE_APPROVED",
            after={"new_version": approved_form.version},
            reference=case.case_id,
        )
    )
    deps.audit_log.append(
        AuditEntry(
            case_id=case.case_id,
            actor_type=ActorType.AGENT,
            actor_id="uc01-form-changes",
            action="FORM_SUPERSEDED",
            after={"old_version": in_force.version},
            reference=case.case_id,
        )
    )

    return case


def reject_form_change(
    approval_id: str,
    decided_by: str,
    reason: str,
    deps: UC01Deps,
) -> OnboardingForm:
    approval = _require_approval(approval_id, deps)
    new_form = _find_form(approval, deps)

    if new_form.status != FormStatus.AWAITING_APPROVAL:
        raise ValueError(f"new form is not AWAITING_APPROVAL (status {new_form.status})")

    rejected_form = new_form.reject()
    deps.form_repo.save(rejected_form)

    decided_at = utc_now()
    approval.status = ApprovalStatus.REJECTED
    approval.decided_by = decided_by
    approval.decided_at = decided_at
    # The reject reason lives on the Approval's decision_note only (`reason`
    # keeps describing why the approval was originally raised); the audit
    # entry below carries no reason text, only the rejected version number.
    approval.decision_note = reason
    deps.approval_repo.update(approval)

    deps.audit_log.append(
        AuditEntry(
            case_id=approval.case_id,
            actor_type=ActorType.HUMAN,
            actor_id=decided_by,
            action="FORM_CHANGE_REJECTED",
            after={"rejected_version": rejected_form.version},
            reference=approval.case_id,
        )
    )

    return rejected_form
