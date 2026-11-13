from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import new_id
from .enums import HrisRecordStatus, HrisSystem


class HrisRecord(BaseModel):
    # Mutable (e.g. status progresses as prepare/approve/write happens), but
    # every assignment is re-validated so a bad status or a non-string
    # payload value can't be smuggled in after construction.
    model_config = ConfigDict(validate_assignment=True)

    record_id: str = Field(default_factory=new_id)
    case_id: str
    client_id: str
    employee_no: Optional[str] = None
    system: HrisSystem
    form_version: int = Field(ge=1)
    # Non-sensitive, verbatim strings only: never recalculated or
    # reformatted on the way to the target system, per the "copy exactly"
    # rule. Sensitive values (salary) never live here.
    prepared_payload: dict[str, str] = Field(default_factory=dict)
    # Target field NAMES only (e.g. "compensation_terms"), never values.
    # The record never stores the salary itself: the sensitive payload is
    # rebuilt from the in-force form at write time (see hris_mapping and
    # execute_hris_write), so there is nothing sensitive to redact here.
    sensitive_field_names: list[str] = Field(default_factory=list)
    comparison_result: Optional[dict] = None
    external_ref: Optional[str] = None
    # Set when this record updates an employee that already exists in
    # the target system, rather than creating one: the external
    # reference of the record it supersedes. Drives gateway.update()
    # instead of gateway.write(), so a post-write form change reaches
    # the target system without making a second employee.
    updates_external_ref: Optional[str] = None
    # The exception CLASS name or a short code only (e.g. "HrisUnavailableError"),
    # never an exception message, which could carry request/response data.
    last_error: Optional[str] = None
    status: HrisRecordStatus = HrisRecordStatus.NOT_PREPARED
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    # Derived from case_id + system + form_version so retrying a prepare for
    # the same case/system/form-version is naturally idempotent downstream.
    idempotency_key: Optional[str] = None

    @model_validator(mode="after")
    def _derive_idempotency_key(self) -> "HrisRecord":
        if self.idempotency_key is None:
            self.idempotency_key = f"{self.case_id}:{self.system.value}:{self.form_version}"
        return self
