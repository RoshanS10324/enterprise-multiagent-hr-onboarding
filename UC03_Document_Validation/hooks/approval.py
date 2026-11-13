from __future__ import annotations

from src.state.enums import ApprovalStatus
from src.state.models import ApprovalRecord
from src.state.store import CaseStore


class ApprovalRequiredError(Exception):
    def __init__(self, case_id: str, action_type: str) -> None:
        self.case_id = case_id
        self.action_type = action_type
        super().__init__(
            f"Action '{action_type}' on case '{case_id}' requires HR approval that is not yet granted"
        )


async def require_approval(
    case_store: CaseStore,
    case_id: str,
    action_type: str,
) -> ApprovalRecord:
    """Return the approval record if approved; raise ApprovalRequiredError otherwise."""
    record = await case_store.get_approval(case_id, action_type)
    if record is None or record.status != ApprovalStatus.APPROVED:
        raise ApprovalRequiredError(case_id=case_id, action_type=action_type)
    return record
