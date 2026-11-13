"""[Proposed] Dev simulator only; see
src/sub_agent/intake/email_intake.py and open question OQ-01/Q-11.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .common import new_id, utc_now


class PendingStartStatus(StrEnum):
    WAITING = "WAITING"
    STARTED = "STARTED"
    CANCELLED = "CANCELLED"


class PendingStart(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    pending_id: str = Field(default_factory=new_id)
    client_id: str
    offer_ref: str
    reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    status: PendingStartStatus = PendingStartStatus.WAITING
