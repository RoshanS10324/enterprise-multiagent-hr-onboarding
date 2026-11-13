from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .common import new_id
from .enums import FormStatus, WorkLocationType


class OnboardingForm(BaseModel):
    # The model is always frozen: once constructed, a form never mutates in
    # place. Lifecycle transitions (submit_for_approval/approve/supersede)
    # build and validate a brand new instance instead, so there's no need
    # for the status-dependent mutability this type used to implement via
    # a custom __setattr__.
    model_config = ConfigDict(frozen=True)

    form_id: str = Field(default_factory=new_id)
    client_id: str
    case_id: Optional[str] = None
    version: int = Field(ge=1)
    candidate_name: str
    candidate_email: str
    designation: str
    joining_date: date
    work_location_type: WorkLocationType
    client_site_id: Optional[str] = None
    # SecretStr keeps the verbatim salary/terms text out of repr(),
    # model_dump() and model_dump_json() (all render "**********"). The
    # exact original string is only reachable via get_secret_value().
    salary_and_terms: SecretStr
    status: FormStatus = FormStatus.DRAFT
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    change_reason: Optional[str] = None

    @field_validator("approved_at")
    @classmethod
    def _approved_at_must_be_tz_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and v.tzinfo is None:
            raise ValueError("approved_at must be a timezone-aware UTC datetime")
        return v

    @model_validator(mode="after")
    def _check_rules(self) -> "OnboardingForm":
        if self.status == FormStatus.APPROVED and (
            self.approved_by is None or self.approved_at is None
        ):
            raise ValueError("APPROVED form requires approved_by and approved_at")
        if self.work_location_type == WorkLocationType.CLIENT_SITE and self.client_site_id is None:
            raise ValueError("CLIENT_SITE work location requires client_site_id")
        return self

    def _dump_with_secret(self) -> dict[str, Any]:
        """model_dump(), but with the real salary string restored.

        Used only as the starting point for building the next lifecycle
        instance (approve/supersede/submit_for_approval); never exposed.
        """
        data = self.model_dump(exclude={"salary_and_terms"})
        data["salary_and_terms"] = self.salary_and_terms.get_secret_value()
        return data

    def submit_for_approval(self) -> "OnboardingForm":
        if self.status != FormStatus.DRAFT:
            raise ValueError(f"cannot submit for approval from status {self.status}")
        data = self._dump_with_secret()
        data["status"] = FormStatus.AWAITING_APPROVAL
        return OnboardingForm(**data)

    def approve(self, approved_by: str, approved_at: datetime) -> "OnboardingForm":
        if self.status not in (FormStatus.DRAFT, FormStatus.AWAITING_APPROVAL):
            raise ValueError(f"cannot approve a form in status {self.status}")
        data = self._dump_with_secret()
        data.update(
            status=FormStatus.APPROVED,
            approved_by=approved_by,
            approved_at=approved_at,
        )
        return OnboardingForm(**data)

    def supersede(self) -> "OnboardingForm":
        if self.status != FormStatus.APPROVED:
            raise ValueError(f"cannot supersede a form in status {self.status}")
        data = self._dump_with_secret()
        data["status"] = FormStatus.SUPERSEDED
        return OnboardingForm(**data)

    def with_case(self, case_id: str) -> "OnboardingForm":
        if self.case_id is not None and self.case_id != case_id:
            raise ValueError(
                f"form is already linked to case {self.case_id!r}, cannot relink to {case_id!r}"
            )
        data = self._dump_with_secret()
        data["case_id"] = case_id
        return OnboardingForm(**data)

    def reject(self) -> "OnboardingForm":
        if self.status != FormStatus.AWAITING_APPROVAL:
            raise ValueError(f"cannot reject a form in status {self.status}")
        data = self._dump_with_secret()
        data["status"] = FormStatus.REJECTED
        return OnboardingForm(**data)

    def next_version(self, **field_updates: Any) -> "OnboardingForm":
        """Build the next DRAFT version, copying every field (including the
        real salary) except those given in field_updates, and resetting the
        approval bookkeeping (approved_by/approved_at/change_reason) for the
        new version's own lifecycle.
        """
        data = self._dump_with_secret()
        data.pop("form_id", None)  # the next version is a new form row
        data["version"] = self.version + 1
        data["status"] = FormStatus.DRAFT
        data["approved_by"] = None
        data["approved_at"] = None
        data["change_reason"] = None
        if "salary_and_terms" in field_updates and isinstance(
            field_updates["salary_and_terms"], SecretStr
        ):
            field_updates = {
                **field_updates,
                "salary_and_terms": field_updates["salary_and_terms"].get_secret_value(),
            }
        data.update(field_updates)
        return OnboardingForm(**data)
