from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .common import new_id
from .enums import DiscrepancyStatus


class Discrepancy(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    discrepancy_id: str = Field(default_factory=new_id)
    case_id: str
    field: str
    value_form: str
    value_other: str
    other_source: str
    # Version of the in-force form this discrepancy was raised against.
    # Part of the de-duplication key: a later form version may legitimately
    # match an other-source value that an earlier version did not.
    form_version: int
    status: DiscrepancyStatus = DiscrepancyStatus.OPEN
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    # De-duplication key only (sha256 of the other-source value); never
    # logged or shown, and left out of dumps so it never leaks a value by
    # being hashed from something sensitive.
    other_fingerprint: Optional[str] = Field(default=None, exclude=True, repr=False)
