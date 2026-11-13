from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .enums import CaseStage, Checkpoint, HrisRecordStatus, HrisSystem, WorkLocationType


class PendingApprovalView(BaseModel):
    model_config = ConfigDict(frozen=True)

    approval_id: str
    checkpoint: Checkpoint
    requested_to: str
    # The part of subject_ref before the first colon (e.g. "form",
    # "discrepancy", "hris"), or the raw subject_ref for variant notices
    # that carry no colon (e.g. "is_rehire", "possible_duplicate").
    subject_ref_kind: str
    created_at: datetime


class OpenDiscrepancyView(BaseModel):
    model_config = ConfigDict(frozen=True)

    discrepancy_id: str
    field: str
    other_source: str


class HrisStatusView(BaseModel):
    model_config = ConfigDict(frozen=True)

    system: HrisSystem
    status: HrisRecordStatus


class CaseStatusView(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    employee_no: Optional[str] = None
    stage: CaseStage
    form_version_in_force: int
    joining_date: date
    work_location_type: WorkLocationType
    # Filled only for roles allowed to see it; None otherwise (e.g. AUDITOR).
    candidate_name: Optional[str] = None
    open_discrepancies: list[OpenDiscrepancyView] = Field(default_factory=list)
    pending_approvals: list[PendingApprovalView] = Field(default_factory=list)
    hris: list[HrisStatusView] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
