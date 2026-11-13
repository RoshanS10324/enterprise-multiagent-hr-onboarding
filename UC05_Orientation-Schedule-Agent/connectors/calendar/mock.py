"""TEST / DEMO ONLY. MockCalendar: an in-memory, simulated CalendarPort.

Not Google Workspace and not Microsoft 365: nothing here makes a network call, holds a credential,
or creates a meeting anyone can see. Event references start with "sim-evt" so a simulated booking
is never mistaken for a real one. Production startup refuses fake connectors (sub_agent.runtime).

The mock only simulates provider behaviour. It does not decide WHEN a meeting may exist: a
tentative hold is created silently (book_event(notify_attendees=False)) and attendees are invited
only on confirm_event, and it is sub_agent.services.scheduling that calls confirm_event, after an
authorized HR approval. The calendar itself has no tenant concept (one provider per deployment);
tenant isolation is enforced before any calendar call, by OnboardingToolRegistry.

Outcomes it can simulate, deterministically:
  - success: an open slot books a TENTATIVE hold; confirm_event confirms it.
  - conflict: pre-seed a busy slot (`busy=`) and book the same attendee and time ->
    CalendarConflictError, like a provider reporting a double-booking.
  - failure: any booking that includes FAILING_ATTENDEE -> CalendarBookingError (an outage).
  - transient failure: `fail_next_bookings=n` -> the next n book_event calls raise
    CalendarBookingError before creating anything, then recover.
  - uncertain outcome: `lose_next_booking_responses=n` -> the next n book_event calls create the
    booking and THEN raise CalendarBookingError, like a timeout after the provider acted.
  - confirmation failure: `fail_next_confirms=n` -> the next n confirm_event calls raise.
  - partial invitation failure: attendees in `invite_failures=` come back in
    CalendarBooking.invitation_failures whenever attendees are actually invited.
  - idempotency: book_event with an idempotency_key already used returns that same booking (like
    Graph's transactionId); `idempotent_booking=False` simulates a provider that cannot, so a
    retry with the same key creates a second event.
Inspection helpers (not part of CalendarPort): get_booking, list_bookings, `invited` (every
attendee actually invited, as (event_ref, attendee)), and reset().
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from connectors.calendar.base import BookingStatus, CalendarBooking, CalendarBookingError, CalendarConflictError
from hr_onboarding_common.contracts import new_id

FAILING_ATTENDEE = "CALENDAR-OUTAGE"  # booking any event with this attendee simulates a provider failure


class MockCalendar:
    def __init__(self, *, busy: list[tuple[str, datetime, datetime]] | None = None,
                 invite_failures: set[str] | None = None, fail_next_bookings: int = 0,
                 lose_next_booking_responses: int = 0, fail_next_confirms: int = 0,
                 idempotent_booking: bool = True):
        self._initial: dict[str, Any] = dict(
            busy=list(busy or []), invite_failures=set(invite_failures or set()),
            fail_next_bookings=fail_next_bookings, lose_next_booking_responses=lose_next_booking_responses,
            fail_next_confirms=fail_next_confirms, idempotent_booking=idempotent_booking)
        self.reset()

    def reset(self) -> None:
        """Back to the state the mock was constructed with: no bookings, no invitations, the
        constructor's busy slots and failure settings."""
        init = self._initial
        self.idempotent_booking: bool = init["idempotent_booking"]
        self.invited: list[tuple[str, str]] = []
        self._by_key: dict[str, str] = {}
        self._busy: list[tuple[str, datetime, datetime]] = list(init["busy"])
        self._bookings: dict[str, CalendarBooking] = {}  # live (tentative or confirmed) bookings
        self._cancelled: dict[str, CalendarBooking] = {}
        self._invite_failures: set[str] = set(init["invite_failures"])
        self.fail_next_bookings: int = init["fail_next_bookings"]
        self.lose_next_booking_responses: int = init["lose_next_booking_responses"]
        self.fail_next_confirms: int = init["fail_next_confirms"]

    # ---- CalendarPort
    def check_availability(self, attendees: list[str], start: datetime, end: datetime) -> bool:
        self._validate(attendees, start, end)
        return not self._conflicting(attendees, start, end)

    def book_event(self, *, attendees: list[str], start: datetime, end: datetime, title: str,
                   idempotency_key: str | None = None, notify_attendees: bool = False) -> CalendarBooking:
        self._validate(attendees, start, end)
        if self.idempotent_booking and idempotency_key and idempotency_key in self._by_key:
            existing = self._bookings.get(self._by_key[idempotency_key])
            if existing is not None:
                return existing.model_copy(deep=True)  # the provider already holds it: create nothing
        if self.fail_next_bookings > 0:
            self.fail_next_bookings -= 1
            raise CalendarBookingError(f"calendar provider request failed while booking '{title}' "
                                       f"(simulated transient outage)")
        if FAILING_ATTENDEE in attendees:
            raise CalendarBookingError(f"calendar provider request failed while booking '{title}' "
                                       f"(simulated outage)")
        if self._conflicting(attendees, start, end):
            raise CalendarConflictError(f"{attendees} already have a booking overlapping {start}-{end}")
        booking = CalendarBooking(event_ref=new_id("sim-evt"), status=BookingStatus.TENTATIVE,
                                  attendees=list(attendees), start=start, end=end)
        if notify_attendees:
            booking = self._invite(booking)
        self._bookings[booking.event_ref] = booking
        if idempotency_key:
            self._by_key[idempotency_key] = booking.event_ref
        self._busy.extend((a, start, end) for a in attendees)
        if self.lose_next_booking_responses > 0:
            self.lose_next_booking_responses -= 1
            raise CalendarBookingError(f"calendar provider timed out while booking '{title}' (simulated; the "
                                       f"event was created)")
        return booking.model_copy(deep=True)

    def confirm_event(self, event_ref: str, *, notify_attendees: bool = True) -> CalendarBooking:
        """Raises KeyError for an event the provider does not hold (unknown or cancelled)."""
        if event_ref not in self._bookings:
            raise KeyError(event_ref)
        if self.fail_next_confirms > 0:
            self.fail_next_confirms -= 1
            raise CalendarBookingError(f"calendar provider request failed while confirming {event_ref} "
                                       f"(simulated outage)")
        updated = self._bookings[event_ref].model_copy(update={"status": BookingStatus.CONFIRMED})
        if notify_attendees and not updated.invitations_sent:
            updated = self._invite(updated)
        self._bookings[event_ref] = updated
        return updated.model_copy(deep=True)

    def cancel_event(self, event_ref: str) -> None:
        """Idempotent: cancelling an unknown or already-cancelled event changes nothing."""
        booking = self._bookings.pop(event_ref, None)
        if booking is None:
            return
        self._cancelled[event_ref] = booking.model_copy(update={"status": BookingStatus.CANCELLED})
        self._busy = [(a, s, e) for a, s, e in self._busy
                      if not (a in booking.attendees and s == booking.start and e == booking.end)]

    # ---- inspection (tests and the offline demo; not part of CalendarPort)
    def get_booking(self, event_ref: str) -> CalendarBooking:
        """A live or cancelled booking by reference; KeyError if the provider never held it."""
        booking = self._bookings.get(event_ref) or self._cancelled.get(event_ref)
        if booking is None:
            raise KeyError(event_ref)
        return booking.model_copy(deep=True)

    def list_bookings(self, *, attendee: str | None = None) -> list[CalendarBooking]:
        """Live bookings, oldest first, optionally only those including `attendee`."""
        return [b.model_copy(deep=True) for b in self._bookings.values() if attendee is None or attendee in b.attendees]

    # ---- helpers
    @staticmethod
    def _validate(attendees: list[str], start: datetime, end: datetime) -> None:
        if not attendees or any(not a or not a.strip() for a in attendees):
            raise ValueError("a calendar request needs at least one attendee, and no blank attendee ids")
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("calendar times must carry a timezone")
        if end <= start:
            raise ValueError(f"event end {end.isoformat()} must be after its start {start.isoformat()}")

    def _conflicting(self, attendees: list[str], start: datetime, end: datetime) -> bool:
        return any(attendee in attendees and existing_start < end and start < existing_end
                   for attendee, existing_start, existing_end in self._busy)

    def _invite(self, booking: CalendarBooking) -> CalendarBooking:
        self.invited.extend((booking.event_ref, a) for a in booking.attendees)
        return booking.model_copy(update={"invitations_sent": True,
                                          "invitation_failures": [a for a in booking.attendees
                                                                  if a in self._invite_failures]})
