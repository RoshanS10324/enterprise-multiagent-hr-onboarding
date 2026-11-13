"""Approval routing and validation (BR-05, BR-16). An approval only counts when approver role, request ID, item and
the approved scope all match what is being requested now."""

from dataclasses import dataclass, field

from sub_agent.contracts import ApprovalRecord, CaseConfig, RejectedApproval, Uc04Input
from sub_agent.states import ApprovalClass, ApprovalDecision, ApprovalState, ApprovalType, Owner


@dataclass
class Resolution:
    """Who may approve one request class right now."""

    accepted_roles: set[str] = field(default_factory=set)
    display: str = Owner.OWNER_CONFIG_MISSING
    owner: str = Owner.OWNER_CONFIG_MISSING
    config_missing: bool = False
    escalate_to_hr: bool = False  # primary approver unavailable and no delegate configured (EC-37)


def resolve(cls: ApprovalClass, cfg: CaseConfig, unavailable_roles: list[str]) -> Resolution:
    block = cfg.approver_config
    if not block.is_usable or cls not in (block.value or {}):
        return Resolution(config_missing=True)
    entry = block.value[cls]
    if entry.role not in unavailable_roles:
        return Resolution({entry.role}, entry.role, entry.role)
    if entry.delegate_role and entry.delegate_role not in unavailable_roles:
        return Resolution({entry.delegate_role}, entry.delegate_role, entry.delegate_role)
    return Resolution(set(), entry.role, Owner.HR_OFFICER, escalate_to_hr=True)


@dataclass
class ApprovalOutcome:
    standard: ApprovalState
    exception: ApprovalState
    rejected_evidence: list[RejectedApproval] = field(default_factory=list)


def _invalid_reason(r: ApprovalRecord, inp: Uc04Input, item_id: str, needed: Resolution | None) -> str | None:
    if needed is None:
        return "EXCEPTION_NOT_REQUIRED"
    if needed.config_missing:
        return "APPROVER_CONFIG_MISSING"
    if r.approver_role not in needed.accepted_roles:
        return "WRONG_APPROVER_ROLE"
    if r.item_id != item_id:
        return "ITEM_MISMATCH"
    if (r.client_id, r.role_id, r.client_site_required) != (inp.client_id, inp.role_id, inp.client_site_required):
        return "STALE_SCOPE"  # approved for a different client, role or site requirement
    if r.decided_on > inp.as_of_date:
        return "FUTURE_DATED"
    return None


def _state(valid: list[ApprovalRecord]) -> ApprovalState:
    if not valid:
        return ApprovalState.PENDING
    latest = max(r.decided_on for r in valid)
    newest = [r for r in valid if r.decided_on == latest]
    if any(r.decision == ApprovalDecision.REJECTED for r in newest):  # ties fail safe
        return ApprovalState.REJECTED
    return ApprovalState.RECEIVED


def assess(inp: Uc04Input, request_id: str, item_id: str, std: Resolution, exc: Resolution | None) -> ApprovalOutcome:
    valid_std: list[ApprovalRecord] = []
    valid_exc: list[ApprovalRecord] = []
    rejected: list[RejectedApproval] = []
    for r in (x for x in inp.approval_records if x.request_id == request_id):
        reason = _invalid_reason(r, inp, item_id, std if r.approval_type == ApprovalType.STANDARD else exc)
        if reason:
            rejected.append(RejectedApproval(approval_id=r.approval_id, request_id=request_id, reason_code=reason))
        elif r.approval_type == ApprovalType.STANDARD:
            valid_std.append(r)
        else:
            valid_exc.append(r)
    exception_state = ApprovalState.NOT_REQUIRED if exc is None else _state(valid_exc)
    return ApprovalOutcome(_state(valid_std), exception_state, rejected)
