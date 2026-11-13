"""CalendarPort: the only way the scheduling service touches a calendar. The read/write split and
narrow-write-method style mirror hr_recruitment_common.connectors.ats.base.ATSPort.

No real calendar provider is integrated here. Only the simulated MockCalendar
(connectors/calendar/mock.py) exists until a provider (Google Workspace / Microsoft 365 / other)
and credentials are approved -- the same "fake adapter first" rule the resume and recruitment
agents follow for the ATS.

Participant RSVPs are recorded through the onboarding API today (POST .../responses), not read
back from a provider; a real adapter would feed the same workflow event, passing the provider's
response timestamp so out-of-order deliveries can be detected.

Contract an adapter must honour:
  - book_event(..., idempotency_key=K): calling it again with the same K returns the SAME booking
    instead of creating a second one, even if the first call's response was lost. Advertise that
    with `idempotent_booking = True` (e.g. Microsoft Graph `transactionId`, Google Calendar
    client-supplied event ids). An adapter that cannot guarantee it must set False; the scheduling
    service then refuses to retry an uncertain booking blindly and routes it to HR instead.
  - book_event(..., notify_attendees=False) creates a TENTATIVE hold without sending invitations;
    confirm_event(..., notify_attendees=True) confirms it and only then invites the attendees.
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
    invitations_sent: bool = False  # whether this call actually invited the attendees


class CalendarPort(Protocol):
    idempotent_booking: bool  # True only if book_event honours idempotency_key (see module docstring)

    def check_availability(self, attendees: list[str], start: datetime, end: datetime) -> bool: ...
    def book_event(self, *, attendees: list[str], start: datetime, end: datetime, title: str,
                   idempotency_key: str | None = None, notify_attendees: bool = False) -> CalendarBooking: ...
    def confirm_event(self, event_ref: str, *, notify_attendees: bool = True) -> CalendarBooking: ...
    def cancel_event(self, event_ref: str) -> None: ...
