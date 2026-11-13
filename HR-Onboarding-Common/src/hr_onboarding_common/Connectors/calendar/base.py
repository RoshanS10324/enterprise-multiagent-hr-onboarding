"""CalendarPort: the only way the scheduling service touches a calendar. The read/write split and
narrow-write-method style mirror hr_recruitment_common.connectors.ats.base.ATSPort.

No real calendar provider is integrated here. Only the in-memory FakeCalendar (fake.py) exists
until a provider and credentials are approved -- the same "fake adapter first" rule the resume
and recruitment agents follow for the ATS.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field


class BookingStatus(StrEnum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class CalendarConflictError(ValueError):
    """At least one attendee already has an overlapping booking. Never treated as a provider failure."""


class CalendarBookingError(RuntimeError):
    """The calendar provider itself failed (outage, timeout, auth error, ...). Never treated as a
    conflict -- callers must not silently reinterpret a provider failure as 'slot unavailable'."""


class CalendarBooking(BaseModel):
    event_ref: str
    status: BookingStatus
    attendees: list[str] = Field(default_factory=list)
    start: datetime
    end: datetime
    invitation_failures: list[str] = Field(default_factory=list)  # attendees whose invite failed to send,
    # even though the booking itself succeeded (UC step 4: "invitation status")


class CalendarPort(Protocol):
    def check_availability(self, attendees: list[str], start: datetime, end: datetime) -> bool: ...
    def book_event(self, *, attendees: list[str], start: datetime, end: datetime,
                   title: str) -> CalendarBooking: ...
    def confirm_event(self, event_ref: str) -> CalendarBooking: ...
    def cancel_event(self, event_ref: str) -> None: ...
