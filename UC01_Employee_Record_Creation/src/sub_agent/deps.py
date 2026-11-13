"""The single dependency bundle for UC-01's write-side use cases (U2-U8
duplicate/compare/form-change/HRIS flows). Read-side U9 uses the slimmer
case_status.U9Deps instead.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from .policy import Policy
from .ports import (
    ApprovalRepository,
    AuditLog,
    CaseRepository,
    DiscrepancyRepository,
    EventPublisher,
    FormRepository,
    HolidayCalendar,
    HrisGateway,
    HrisRecordRepository,
)


@dataclass
class UC01Deps:
    case_repo: CaseRepository
    form_repo: FormRepository
    approval_repo: ApprovalRepository
    discrepancy_repo: DiscrepancyRepository
    hris_record_repo: HrisRecordRepository
    hris_gateway: HrisGateway
    audit_log: AuditLog
    publisher: EventPublisher
    holiday_calendar: HolidayCalendar
    # Injectable clock so "joining date is in the past" is deterministic in
    # tests. Defaults to the real date.today for normal use.
    today: Callable[[], date] = field(default=date.today)
    # Kill switch for HRIS writes (checked by hooks.before_hris_write).
    # Defaults to always-on; wire to UC01_HRIS_WRITES_ENABLED in .env for
    # real deployments.
    hris_writes_enabled: Callable[[], bool] = field(default=lambda: True)
    # Needed only for the SELF_APPROVED invariant check; everything else
    # in hooks/invariants works without it.
    policy: Optional[Policy] = None
