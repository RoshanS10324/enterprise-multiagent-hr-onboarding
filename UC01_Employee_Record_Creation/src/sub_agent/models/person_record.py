from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, Field

from .common import new_id


class PersonRecordKind(StrEnum):
    OPEN_CASE = "OPEN_CASE"
    PAST_EMPLOYEE = "PAST_EMPLOYEE"
    CURRENT_EMPLOYEE = "CURRENT_EMPLOYEE"


class PersonRecord(BaseModel):
    record_id: str = Field(default_factory=new_id)
    client_id: str
    email: str
    id_number: Optional[str] = None
    full_name: str
    kind: PersonRecordKind
    case_id: Optional[str] = None
    # For a PAST_EMPLOYEE or CURRENT_EMPLOYEE: how the target HRIS
    # already identifies this person. A rehire or internal transfer
    # reactivates that employee instead of creating a second one
    # (EC-43). None when the person is not in the HRIS yet.
    hris_external_ref: Optional[str] = None
