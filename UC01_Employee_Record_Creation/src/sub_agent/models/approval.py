from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .common import new_id, utc_now
from .enums import ApprovalStatus, Checkpoint


class Approval(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    approval_id: str = Field(default_factory=new_id)
    case_id: str
    checkpoint: Checkpoint
    subject_ref: str
    requested_to: str
    created_at: datetime = Field(default_factory=utc_now)
    # Who asked for this approval, when known (e.g. the HR officer who
    # proposed a form change). Enforcing "approver must differ from
    # requester" is deferred to the policy step; see docs/known-gaps.md.
    requested_by: Optional[str] = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    reason: Optional[str] = None
    # Free-text note attached at decision time (e.g. a rejection reason).
    # Distinct from `reason`, which describes why the approval was raised.
    decision_note: Optional[str] = None
