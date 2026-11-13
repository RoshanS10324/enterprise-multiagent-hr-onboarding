from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator

from .enums import WorkLocationType

CHANGEABLE_FIELDS = (
    "candidate_name",
    "designation",
    "joining_date",
    "work_location_type",
    "client_site_id",
    "salary_and_terms",
)


class FormChangeRequest(BaseModel):
    # candidate_email is deliberately not changeable here: it is the
    # identity key duplicate_check (U2) matches on. See docs/known-gaps.md.
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    requested_by: str
    change_reason: str
    source_discrepancy_id: Optional[str] = None

    candidate_name: Optional[str] = None
    designation: Optional[str] = None
    joining_date: Optional[date] = None
    work_location_type: Optional[WorkLocationType] = None
    client_site_id: Optional[str] = None
    salary_and_terms: Optional[SecretStr] = None

    @field_validator("change_reason")
    @classmethod
    def _change_reason_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("change_reason must not be blank")
        return v

    @model_validator(mode="after")
    def _at_least_one_change_field(self) -> "FormChangeRequest":
        if not any(getattr(self, name) is not None for name in CHANGEABLE_FIELDS):
            raise ValueError("at least one change field must be set")
        return self
