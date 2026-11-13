from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .common import new_id, utc_now
from .enums import CaseStatus, WorkLocationType
from .variant_flags import VariantFlags

# Default for Case.current_stage when the caller omits it.
#
# Deliberately a plain constant, NOT a member of the CaseStage enum.
# CaseStage is UC-01's *derived* view stage - case_status.py computes it
# from the case's current state and it is never persisted. The live
# onboarding_cases.current_stage column is a different, persisted,
# cross-use-case orchestration value, and 'INTAKE' is not in CaseStage.
# Conflating the two vocabularies would change what UC-01's status view
# reports.
DEFAULT_CURRENT_STAGE = "INTAKE"


def _require_location_name(v: str) -> str:
    """Required: reject missing, empty and whitespace-only.

    An explicit NULL is already rejected by the `str` annotation.
    Surrounding whitespace is trimmed, matching how the person directory
    normalises its lookup values (adapters/memory.py).
    """
    trimmed = v.strip()
    if not trimmed:
        raise ValueError("location_name is required and cannot be empty or whitespace")
    return trimmed


def _normalise_optional_text(v: Optional[str]) -> Optional[str]:
    """Optional, but a whitespace-only string is not a value.

    Normalised to None so "not known yet" has ONE representation in the
    database (SQL NULL) rather than two (NULL and '   ').
    """
    if v is None:
        return None
    return v.strip() or None


class CasePlacement(BaseModel):
    """The placement attributes a caller supplies at case creation.

    Carried as one object so create_case does not grow seven
    parameters, and so the same validation applies however a case is
    created. Mirrors the Case fields of the same names.
    """

    model_config = ConfigDict(frozen=True)

    location_name: str
    department: Optional[str] = None
    grade: Optional[str] = None
    nationality_group: Optional[str] = None
    project_name: Optional[str] = None
    current_stage: str = DEFAULT_CURRENT_STAGE
    onboarding_attributes: list[str] = Field(default_factory=list)

    _v_location = field_validator("location_name")(_require_location_name)
    _v_optional = field_validator(
        "department", "grade", "nationality_group", "project_name"
    )(_normalise_optional_text)

    @field_validator("current_stage", mode="before")
    @classmethod
    def _stage_defaults_when_omitted(cls, v: Optional[str]) -> str:
        """An omitted stage - which an API body renders as None - takes
        the approved default rather than failing. An explicitly supplied
        value is preserved verbatim, including one we do not recognise:
        current_stage is a cross-use-case vocabulary UC-01 does not own,
        so UC-01 must not police it."""
        if v is None:
            return DEFAULT_CURRENT_STAGE
        return v

    @field_validator("onboarding_attributes", mode="before")
    @classmethod
    def _attributes_default_when_omitted(cls, v: Optional[list[str]]) -> list[str]:
        return [] if v is None else v


class Case(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    case_id: str = Field(default_factory=new_id)
    client_id: str
    employee_no: Optional[str] = None
    candidate_name: str
    candidate_email: str
    hr_officer_id: str
    joining_date: date
    work_location_type: WorkLocationType
    variants: VariantFlags = Field(default_factory=VariantFlags)
    status: CaseStatus = CaseStatus.PLAN_PENDING
    current_form_version: int = Field(ge=1)
    opened_at: datetime = Field(default_factory=utc_now)
    folder_ref: Optional[str] = None
    # Set on a rehire or internal transfer: the PersonRecord this case
    # proposes to reactivate, and that person's existing HRIS
    # reference. UC-01 only proposes - HR decides at HC_12_VARIANT,
    # and nothing is written until HC_05 is approved as well.
    reactivates_record_id: Optional[str] = None
    reactivates_external_ref: Optional[str] = None

    # ------------------------------------------------------------------
    # Placement attributes (approved decisions, 2026-11-04).
    #
    # These exist as NOT NULL columns on the live onboarding_cases table
    # but had no UC-01 representation at all. The approved outcome is:
    # one required (location_name), four optional, two defaulted.
    #
    # The four optional ones are "populate later" fields: UC-01 does not
    # know them at intake and must not invent them. None persists as SQL
    # NULL once the proposed DROP NOT NULL lands - see
    # docs/schema/proposed/d4_d5_additive.sql (NOT APPLIED).
    # ------------------------------------------------------------------
    location_name: str
    department: Optional[str] = None
    grade: Optional[str] = None
    nationality_group: Optional[str] = None
    project_name: Optional[str] = None
    current_stage: str = DEFAULT_CURRENT_STAGE
    # default_factory, never a shared [] literal: a mutable default on a
    # model would be shared by every Case instance.
    #
    # TYPE NOTE: the live column is `text[]`, NOT jsonb. The approved
    # decision says "empty JSON object {}", but `{}` is also Postgres'
    # empty-ARRAY literal, which is what a text[] column defaults to.
    # Matching the live type is the safe reading; changing the column to
    # jsonb would be a shared-schema change. Flagged for DBA
    # confirmation rather than guessed - see the final report.
    onboarding_attributes: list[str] = Field(default_factory=list)

    # Same rules as CasePlacement: the Case is what gets persisted, so
    # it enforces them too rather than trusting its caller.
    _v_location = field_validator("location_name")(_require_location_name)
    _v_optional = field_validator(
        "department", "grade", "nationality_group", "project_name"
    )(_normalise_optional_text)
