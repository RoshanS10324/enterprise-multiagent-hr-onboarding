from dataclasses import dataclass

from .access import (
    AUDIT_INTEGRITY_ROLES,
    AUDIT_TRAIL_ROLES,
    CANDIDATE_NAME_ROLES,
    STATUS_VIEW_ROLES,
    WORKLIST_ROLES,
    require_role,
    require_visible_case,
)
from .audit_integrity import ChainViolation, verify_chain
from .models import (
    ActorType,
    ApprovalStatus,
    AuditEntry,
    CaseStage,
    CaseStatus,
    CaseStatusView,
    Checkpoint,
    DiscrepancyStatus,
    FormStatus,
    HrisRecordStatus,
    HrisStatusView,
    OpenDiscrepancyView,
    PendingApprovalView,
    Viewer,
)
from .models.common import new_id
from .ports import (
    ApprovalRepository,
    AuditLog,
    CaseRepository,
    DiscrepancyRepository,
    FormRepository,
    HrisRecordRepository,
)


@dataclass
class U9Deps:
    # A dedicated, slim bundle rather than reusing UC01Deps: these
    # functions are read-only views (plus one audit-trail-viewed write) and
    # have no business touching a gateway, event publisher or holiday
    # calendar, so those ports are deliberately left out.
    case_repo: CaseRepository
    form_repo: FormRepository
    approval_repo: ApprovalRepository
    discrepancy_repo: DiscrepancyRepository
    hris_record_repo: HrisRecordRepository
    audit_log: AuditLog


def derive_stage(
    *,
    case_status: CaseStatus,
    hris_statuses: list[HrisRecordStatus],
    pending_form_change: bool,
    open_discrepancy_count: int,
    pending_variant_decision: bool,
    written_at_older_version: bool = False,
) -> CaseStage:
    # Precedence, first match wins. Only HRIS records of the form version
    # in force are passed in (STALE records are filtered out by the
    # caller before this function ever sees them):
    #   0  CASE_INACTIVE:              case.status is ON_HOLD, WITHDRAWN
    #                                  or CLOSED - outranks everything else
    #   1  RECORD_CREATED:            both HRIS records WRITTEN
    #   2  WRITE_FAILED:               any record FAILED
    #   3  HRIS_REJECTED:              records REJECTED (and none newer prepared)
    #   4  HRIS_APPROVED_NOT_WRITTEN:  records APPROVED
    #   5  HRIS_APPROVAL_PENDING:      records PREPARED
    #   6  FORM_CHANGE_PENDING:        a form version AWAITING_APPROVAL
    #   7  HRIS_UPDATE_NEEDED:         a record from an older form version
    #                                  was already WRITTEN (no update flow
    #                                  exists yet - see docs/known-gaps.md)
    #   8  DISCREPANCIES_OPEN:         any OPEN discrepancy
    #   9  VARIANT_DECISION_PENDING:   a PENDING HC_12_VARIANT approval
    #                                  other than the "possible_duplicate" notice
    #   10 READY_TO_PREPARE:          otherwise
    if case_status in (CaseStatus.ON_HOLD, CaseStatus.WITHDRAWN, CaseStatus.CLOSED):
        return CaseStage.CASE_INACTIVE
    if hris_statuses and all(s == HrisRecordStatus.WRITTEN for s in hris_statuses):
        return CaseStage.RECORD_CREATED
    if any(s == HrisRecordStatus.FAILED for s in hris_statuses):
        return CaseStage.WRITE_FAILED
    if hris_statuses and all(s == HrisRecordStatus.REJECTED for s in hris_statuses):
        return CaseStage.HRIS_REJECTED
    if any(s == HrisRecordStatus.APPROVED for s in hris_statuses):
        return CaseStage.HRIS_APPROVED_NOT_WRITTEN
    if any(s == HrisRecordStatus.PREPARED for s in hris_statuses):
        return CaseStage.HRIS_APPROVAL_PENDING
    if pending_form_change:
        return CaseStage.FORM_CHANGE_PENDING
    if written_at_older_version:
        return CaseStage.HRIS_UPDATE_NEEDED
    if open_discrepancy_count > 0:
        return CaseStage.DISCREPANCIES_OPEN
    if pending_variant_decision:
        return CaseStage.VARIANT_DECISION_PENDING
    return CaseStage.READY_TO_PREPARE


def _subject_ref_kind(subject_ref: str) -> str:
    return subject_ref.split(":", 1)[0] if ":" in subject_ref else subject_ref


def get_case_status(case_id: str, viewer: Viewer, deps: U9Deps) -> CaseStatusView:
    require_role(viewer, STATUS_VIEW_ROLES, deps.audit_log, "get_case_status")
    case = require_visible_case(case_id, viewer, deps.case_repo, deps.audit_log, "get_case_status")

    form = deps.form_repo.latest_approved(case.case_id)
    if form is None:
        raise ValueError(f"no approved form in force for case {case.case_id!r}")

    open_discrepancies = [
        d for d in deps.discrepancy_repo.list_for_case(case.case_id) if d.status == DiscrepancyStatus.OPEN
    ]
    pending_approvals_all = [
        a for a in deps.approval_repo.list_for_case(case.case_id) if a.status == ApprovalStatus.PENDING
    ]
    pending_form_change = any(
        f.status == FormStatus.AWAITING_APPROVAL for f in deps.form_repo.list_versions(case.case_id)
    )
    pending_variant_decision = any(
        a.checkpoint == Checkpoint.HC_12_VARIANT and a.subject_ref != "possible_duplicate"
        for a in pending_approvals_all
    )
    possible_duplicate_pending = any(
        a.checkpoint == Checkpoint.HC_12_VARIANT and a.subject_ref == "possible_duplicate"
        for a in pending_approvals_all
    )

    all_hris_records = deps.hris_record_repo.list_for_case(case.case_id)
    hris_records = [
        r for r in all_hris_records if r.form_version == form.version and r.status != HrisRecordStatus.STALE
    ]
    written_at_older_version = any(
        r.status == HrisRecordStatus.WRITTEN and r.form_version < form.version for r in all_hris_records
    )

    stage = derive_stage(
        case_status=case.status,
        hris_statuses=[r.status for r in hris_records],
        pending_form_change=pending_form_change,
        open_discrepancy_count=len(open_discrepancies),
        pending_variant_decision=pending_variant_decision,
        written_at_older_version=written_at_older_version,
    )

    warnings: list[str] = []
    if possible_duplicate_pending:
        warnings.append("possible duplicate notice pending")

    # No audit entry here: status reads happen far more often than any
    # other U9 operation (polling, dashboards, ...), and auditing every
    # read would flood the trail without adding useful signal.
    return CaseStatusView(
        case_id=case.case_id,
        employee_no=case.employee_no,
        stage=stage,
        form_version_in_force=form.version,
        joining_date=form.joining_date,
        work_location_type=form.work_location_type,
        candidate_name=case.candidate_name if viewer.role in CANDIDATE_NAME_ROLES else None,
        open_discrepancies=[
            OpenDiscrepancyView(discrepancy_id=d.discrepancy_id, field=d.field, other_source=d.other_source)
            for d in open_discrepancies
        ],
        pending_approvals=[
            PendingApprovalView(
                approval_id=a.approval_id,
                checkpoint=a.checkpoint,
                requested_to=a.requested_to,
                subject_ref_kind=_subject_ref_kind(a.subject_ref),
                created_at=a.created_at,
            )
            for a in pending_approvals_all
        ],
        hris=[HrisStatusView(system=r.system, status=r.status) for r in hris_records],
        warnings=warnings,
    )


def get_audit_trail(case_id: str, viewer: Viewer, deps: U9Deps) -> list[AuditEntry]:
    # Role check first: a disallowed role (CLIENT_USER) must be refused
    # before even looking the case up, so the refusal never depends on
    # (and can't leak) whether the case exists.
    require_role(viewer, AUDIT_TRAIL_ROLES, deps.audit_log, "get_audit_trail")
    case = require_visible_case(case_id, viewer, deps.case_repo, deps.audit_log, "get_audit_trail")

    entries = deps.audit_log.entries_for_case(case.case_id)

    # Appended AFTER entries is captured, so this viewing itself is never
    # part of the list just returned to the caller.
    deps.audit_log.append(
        AuditEntry(
            case_id=case.case_id,
            actor_type=ActorType.HUMAN,
            actor_id=viewer.user_id,
            action="AUDIT_TRAIL_VIEWED",
            after={"role": viewer.role.value, "entry_count": len(entries)},
            reference=case.case_id,
        )
    )

    return entries


def list_pending_approvals(viewer: Viewer, deps: U9Deps) -> list[PendingApprovalView]:
    require_role(viewer, WORKLIST_ROLES, deps.audit_log, "list_pending_approvals")

    pending = deps.approval_repo.list_pending_for(viewer.user_id)

    views: list[PendingApprovalView] = []
    for approval in pending:
        case = deps.case_repo.get(approval.case_id)
        if case is None or case.client_id not in viewer.client_ids:
            continue
        views.append(
            PendingApprovalView(
                approval_id=approval.approval_id,
                checkpoint=approval.checkpoint,
                requested_to=approval.requested_to,
                subject_ref_kind=_subject_ref_kind(approval.subject_ref),
                created_at=approval.created_at,
            )
        )

    views.sort(key=lambda v: v.created_at)
    return views


def verify_audit_integrity(viewer: Viewer, deps: U9Deps) -> tuple[int, list[ChainViolation]]:
    """Verify the hash chain over the COMPLETE audit log.

    Deliberately not per-case. `verify_chain` needs every entry in append
    order: a filtered subset has gaps in `sequence` and broken
    `prev_hash` links by construction, and would report violations on a
    perfectly intact log.

    AUDITOR only - the whole log spans every client (see
    AUDIT_INTEGRITY_ROLES).
    """
    require_role(viewer, AUDIT_INTEGRITY_ROLES, deps.audit_log, "verify_audit_integrity")
    entries = deps.audit_log.entries()
    violations = verify_chain(entries)

    deps.audit_log.append(
        AuditEntry(
            request_id=new_id(),
            actor_type=ActorType.HUMAN,
            actor_id=viewer.user_id,
            action="AUDIT_INTEGRITY_VERIFIED",
            after={"entries_checked": len(entries), "violation_count": len(violations)},
            reference=viewer.user_id,
        )
    )

    return len(entries), violations
