from datetime import date, datetime
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from .common import new_id, utc_now
from .enums import WorkLocationType
from .offer import OfferAcceptedEvent
from .variant_flags import VariantFlags


class CaseOpenedEvent(BaseModel):
    # Append-only, published fact about a case being opened. Deliberately
    # carries no PII and no salary/bank/loan data: only what other systems
    # need to react to a case existing.
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=new_id)
    case_id: str
    client_id: str
    form_version: int
    joining_date: date
    work_location_type: WorkLocationType
    variants: VariantFlags
    occurred_at: datetime = Field(default_factory=utc_now)


class EmployeeRecordCreatedEvent(BaseModel):
    # Published when every HRIS record for a case/form-version has been
    # written successfully. No PII, no salary - just enough for downstream
    # use cases to react to the employee record now existing.
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=new_id)
    case_id: str
    client_id: str
    employee_no: Optional[str] = None
    form_version: int
    occurred_at: datetime = Field(default_factory=utc_now)


class FormChangedEvent(BaseModel):
    # Published when a proposed form change (U8) is approved and becomes
    # the new in-force version. Carries only field NAMES that changed, no
    # values, no PII, no salary.
    model_config = ConfigDict(frozen=True)

    event_id: str = Field(default_factory=new_id)
    case_id: str
    client_id: str
    old_version: int
    new_version: int
    changed_fields: list[str]
    joining_date_changed: bool
    occurred_at: datetime = Field(default_factory=utc_now)


# EventPublisher.publish() accepts any of these. A plain Union (rather than
# a shared base class) because the two events have no real fields in
# common beyond bookkeeping (event_id/occurred_at) and forcing a shared
# base would mean either a near-empty abstract parent or fields that only
# make sense for one of the two subclasses.
DomainEvent = Union[CaseOpenedEvent, FormChangedEvent, EmployeeRecordCreatedEvent, OfferAcceptedEvent]
