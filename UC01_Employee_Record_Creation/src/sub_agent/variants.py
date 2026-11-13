"""U-step: deciding an HC_12_VARIANT approval (rehire/transfer/minor/
contractor/possible_duplicate notices).
"""

from .deps import UC01Deps
from .models import ActorType, Approval, ApprovalStatus, AuditEntry, CaseStatus, Checkpoint
from .models.common import utc_now


def decide_variant_approval(
    approval_id: str,
    decided_by: str,
    approve: bool,
    note: str,
    deps: UC01Deps,
) -> Approval:
    approval = deps.approval_repo.get(approval_id)
    if approval is None:
        raise ValueError(f"no approval with id {approval_id!r}")
    if approval.status != ApprovalStatus.PENDING:
        raise ValueError(f"approval {approval_id!r} is not PENDING (status {approval.status})")
    if approval.checkpoint != Checkpoint.HC_12_VARIANT:
        raise ValueError(f"approval {approval_id!r} is not an HC_12_VARIANT approval")

    decided_at = utc_now()
    approval.decided_by = decided_by
    approval.decided_at = decided_at
    approval.decision_note = note

    if approve:
        # For the "possible_duplicate" notice specifically: approved means
        # HR confirmed this is NOT a duplicate after all.
        approval.status = ApprovalStatus.APPROVED
        deps.approval_repo.update(approval)

        deps.audit_log.append(
            AuditEntry(
                case_id=approval.case_id,
                actor_type=ActorType.HUMAN,
                actor_id=decided_by,
                action="VARIANT_APPROVED",
                after={"approval_id": approval.approval_id},
                reference=approval.case_id,
            )
        )
        return approval

    # Rejected: for "possible_duplicate" this means HR confirmed it IS a
    # duplicate. Either way, a rejected variant decision puts the case on
    # hold [Proposed, confirm with SME] - it cannot proceed until someone
    # resolves it through another channel.
    approval.status = ApprovalStatus.REJECTED
    deps.approval_repo.update(approval)

    case = deps.case_repo.get(approval.case_id)
    if case is None:
        raise ValueError(f"no case with id {approval.case_id!r}")
    case.status = CaseStatus.ON_HOLD
    deps.case_repo.save(case)

    deps.audit_log.append(
        AuditEntry(
            case_id=approval.case_id,
            actor_type=ActorType.HUMAN,
            actor_id=decided_by,
            action="VARIANT_REJECTED",
            after={"approval_id": approval.approval_id},
            reference=approval.case_id,
        )
    )
    deps.audit_log.append(
        AuditEntry(
            case_id=approval.case_id,
            actor_type=ActorType.AGENT,
            actor_id="uc01-variants",
            action="CASE_PUT_ON_HOLD",
            after={"reason_code": "VARIANT_REJECTED"},
            reference=approval.case_id,
        )
    )

    return approval
