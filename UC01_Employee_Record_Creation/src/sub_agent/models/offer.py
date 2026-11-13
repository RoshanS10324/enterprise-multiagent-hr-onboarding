"""[Proposed] Dev simulator only. UC-01's real offer-acceptance trigger and
event source are open question OQ-01/Q-11; see
src/sub_agent/intake/email_intake.py.
"""

from datetime import datetime
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .common import new_id


class OfferStatus(StrEnum):
    SENT = "SENT"
    ACCEPTED = "ACCEPTED"
    WITHDRAWN = "WITHDRAWN"


class OfferRecord(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    client_id: str
    offer_ref: str
    candidate_email: str
    hr_officer_id: str
    status: OfferStatus = OfferStatus.SENT
    accepted_at: Optional[datetime] = None


class OfferAcceptedEvent(BaseModel):
    # No name, no salary: only enough for a downstream system to react to
    # an offer having been accepted.
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=new_id)
    client_id: str
    offer_ref: str
    accepted_at: datetime
    source: str = "email"
