"""In-memory calendar adapter for development and tests. Deterministically simulates all the
outcomes the scheduling service must handle:

  - success: an open slot books normally and returns a confirmed event id once approved.
  - conflict: pre-seed a busy slot (via `busy=`) and book the same attendee/time -- raises
    CalendarConflictError, exactly like a real provider reporting a double-booking.
  - failure: book any event that includes the FAILING_ATTENDEE sentinel -- raises
    CalendarBookingError, simulating a provider outage unrelated to availability.
  - partial invitation failure: book any event that includes an attendee listed in
    `invite_failures=` -- the booking still succeeds, but that attendee's id is returned in
    CalendarBooking.invitation_failures, simulating a bounced/undeliverable invite.
"""

from __future__ import annotations

from datetime import datetime

from hr_onboarding_common.contracts import new_id
from onboarding_agent.calendar.base import (BookingStatus, CalendarBooking, CalendarBookingError,
                                            CalendarConflictError)

FAILING_ATTENDEE = "CALENDAR-OUTAGE"  # booking any event with this attendee simulates a provider failure


class FakeCalendar:
    def __init__(self, *, busy: list[tuple[str, datetime, datetime]] | None = None,
                invite_failures: set[str] | None = None):
        self._busy: list[tuple[str, datetime, datetime]] = list(busy or [])
        self._bookings: dict[str, CalendarBooking] = {}
        self._invite_failures: set[str] = set(invite_failures or set())

    def _conflicting(self, attendees: list[str], start: datetime, end: datetime) -> bool:
        return any(attendee in attendees and existing_start < end and start < existing_end
                  for attendee, existing_start, existing_end in self._busy)

    def check_availability(self, attendees: list[str], start: datetime, end: datetime) -> bool:
        return not self._conflicting(attendees, start, end)

    def book_event(self, *, attendees: list[str], start: datetime, end: datetime, title: str) -> CalendarBooking:
        if FAILING_ATTENDEE in attendees:
            raise CalendarBookingError(f"calendar provider request failed while booking '{title}' "
                                       f"(simulated outage)")
        if self._conflicting(attendees, start, end):
            raise CalendarConflictError(f"{attendees} already have a booking overlapping {start}-{end}")
        failed_invites = [a for a in attendees if a in self._invite_failures]
        booking = CalendarBooking(event_ref=new_id("evt"), status=BookingStatus.TENTATIVE,
                                  attendees=attendees, start=start, end=end, invitation_failures=failed_invites)
        self._bookings[booking.event_ref] = booking
        self._busy.extend((a, start, end) for a in attendees)
        return booking

    def confirm_event(self, event_ref: str) -> CalendarBooking:
        updated = self._bookings[event_ref].model_copy(update={"status": BookingStatus.CONFIRMED})
        self._bookings[event_ref] = updated
        return updated

    def cancel_event(self, event_ref: str) -> None:
        booking = self._bookings.pop(event_ref, None)
        if booking is None:
            return
        booking = booking.model_copy(update={"status": BookingStatus.CANCELLED})
        self._busy = [(a, s, e) for a, s, e in self._busy
                     if not (a in booking.attendees and s == booking.start and e == booking.end)]
