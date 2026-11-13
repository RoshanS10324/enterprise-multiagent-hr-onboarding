"""Orientation/induction/team-intro/buddy-intro scheduling: availability check -> calendar
booking -> OnboardingSession record -> HITL approval -> confirm or cancel.

Uses the real HITLInbox (hr_onboarding_common.platform.hitl), the same approval primitive the
welcome communications go through -- not a bespoke scheduling-only approval status. Calendar
access is only ever through CalendarPort; only the simulated MockCalendar
(connectors/calendar/mock.py) exists until a real provider and credentials are approved (see
connectors/calendar/base.py).

Ordering matters for correctness: the calendar is booked BEFORE the OnboardingSession record is
created, so a provider failure never leaves an orphaned session; and if the repository then
rejects the session as a duplicate, the tentative calendar hold is cancelled rather than left
dangling.

Holds, confirmations and invitations are three different things:
  - a TENTATIVE hold (session SCHEDULED) reserves the slot while the approver decides;
  - a confirmed meeting (session CONFIRMED) exists only after a real HITL approval;
  - an invitation is what attendees receive. By default (settings.invitations: on_approval) the
    hold is created silently -- its Invitation rows are PENDING -- and attendees are invited only
    when the approval confirms it. settings.invitations: on_hold invites with the hold, for a
    client whose business rule allows pre-approval invitations.

Idempotency: every booking is recorded in the operation ledger (OperationRecord) under
booking_key(...) BEFORE the provider is called, and the ledger's operation_id is the idempotency
key sent to the provider on every attempt. A timeout after the provider created the event is
UNCERTAIN; the retry reuses the same key, so a provider that honours it returns the same event
instead of a second one. A provider that cannot (CalendarPort.idempotent_booking False) is never
retried blindly: BookingOutcomeUncertainError routes it to HR. Cancelling a hold marks its
operation RELEASED, so re-booking the same slot later starts a fresh operation.

Called through OnboardingToolRegistry by the onboarding workflow (agent.py) and the
/v1/onboarding/* routes.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from connectors.calendar.base import CalendarBooking, CalendarBookingError, CalendarConflictError, CalendarPort
from connectors.notifications.base import (NotificationChannel, NotificationMessage, NotificationPort,
                                           NotificationRejectedError)
from hr_onboarding_common.contracts import new_id, now_utc
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import ApprovalItem, Decision, HITLInbox
from sub_agent.models import (DuplicateSessionError, InvitationStatus, OnboardingRecord, OnboardingSession,
                              OperationRecord, OperationStatus, ParticipantResponse, ResponseStatus, SessionStatus,
                              SessionType)
from sub_agent.repository import BuddyAssignmentLedger, OnboardingPort
from sub_agent.services.delivery import deliver_once

AGENT_ID = "employee-onboarding"
GATE = "scheduling_approval"
ATTENDANCE_GATE = "attendance_decision"
BUDDY_GATE = "buddy_assignment"

# How long each session type runs. A business decision made in code, never the model's to choose.
SESSION_DURATION_MINUTES: dict[SessionType, int] = {
    SessionType.ORIENTATION: 60, SessionType.INDUCTION: 90, SessionType.TEAM_INTRO: 30,
    SessionType.BUDDY_INTRO: 30,
}

# Step 3: defaults for direct/standalone calls when no policy.yaml settings are supplied. UAE
# default weekend is Friday/Saturday (Python weekday(): Mon=0 ... Fri=4, Sat=5, Sun=6), matching
# this project's established UAE context (hr-recruitment-common's own CLAUDE.md/docs).
DEFAULT_TIMEZONE = "Asia/Dubai"
DEFAULT_OFFICE_HOURS = {"start": "09:00", "end": "18:00"}
DEFAULT_WEEKEND_WEEKDAYS: tuple[int, ...] = (4, 5)

# Step 7: who may approve each session type, by ROLE (hr_admin / team_lead / buddy) -- resolved
# to an actual policy.yaml setting by the caller (see tool_registry.py); these are just the
# built-in defaults for direct/standalone calls, so Phase 7's original tests keep working.
DEFAULT_APPROVER_ROLE: dict[SessionType, str] = {
    SessionType.ORIENTATION: "hr_admin", SessionType.INDUCTION: "hr_admin",
    SessionType.TEAM_INTRO: "team_lead", SessionType.BUDDY_INTRO: "buddy",
}

# When attendees are invited (see the module docstring). on_approval is the default.
INVITATION_TIMINGS = ("on_approval", "on_hold")
BOOKING_KIND, REPLAN_KIND = "calendar_booking", "replan"

# For a role that names a *specific* person (team_lead/buddy, as opposed to a general hr_admin
# pool), which OnboardingRecord field holds the one person actually authorized for that role.
EXPECTED_APPROVER_FIELD: dict[str, str] = {"team_lead": "team_lead_id", "buddy": "buddy_id"}

# Which participant roles are resolved to which attendee id, for attendance-requirement checks.
ROLE_PARTICIPANT_RESOLVERS = {
    "employee": lambda record, session: session.employee_id,
    "team_lead": lambda record, session: record.team_lead_id,
    "buddy": lambda record, session: record.buddy_id,
}


class SchedulingError(RuntimeError):
    pass


class NoBuddyCandidatesError(SchedulingError):
    pass


class BuddyNotAssignedError(SchedulingError):
    """A buddy-intro session was requested before a buddy was approved and assigned."""


class UnauthorizedApproverError(PermissionError):
    """The person who decided a team_lead/buddy-gated approval is not the specific team lead or
    buddy on record for this employee. The HITL item is still recorded (HITLInbox has no concept
    of per-instance identity, only role membership) -- but no side effect is ever applied for it."""


class InvalidMeetingTimeError(SchedulingError):
    pass


class IneligibleBuddyError(SchedulingError):
    """An approved (possibly HR-edited) buddy is not an active candidate in the employee's own
    client's pool. Never assigned: an edit cannot widen the authorized source of buddies."""


class InvalidParticipantResponseError(SchedulingError):
    """An RSVP for someone who was not invited, or for a session that is no longer live."""


class BookingOutcomeUncertainError(SchedulingError):
    """An earlier booking attempt may have created the event, and the calendar provider cannot
    deduplicate a retry. Routed to HR, who check the calendar and resolve the operation."""


class StaleParticipantResponseError(SchedulingError):
    """An RSVP older (by the provider's timestamp) than the response already recorded for that
    participant. Ignored, so a late-delivered message can never roll an RSVP back."""


class NoAvailableSlotError(SchedulingError):
    """No slot inside office hours, on a working day, with every participant free, was found
    within the search window. Routed to HR -- never resolved by booking outside the rules."""


# How far ahead find_available_slot searches, and at what granularity, when the workflow drafts
# a meeting itself. Overridable via policy.yaml settings.workflow.
DEFAULT_SLOT_SEARCH_DAYS = 14
DEFAULT_SLOT_STEP_MINUTES = 30


class OutsideSchedulingWindowError(InvalidMeetingTimeError):
    """The session's local date is outside the window allowed around the employee's joining date."""


# Every session must fall on a local date from the joining date to joining date + 2 calendar days
# (business decision, 2026-11-04). Overridable per deployment via policy.yaml settings.scheduling_window.
DEFAULT_SCHEDULING_WINDOW = {"days_before_joining": 0, "days_after_joining": 2}


def _client_tz(settings: dict) -> ZoneInfo:
    name = settings.get("timezone", DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        raise InvalidMeetingTimeError(f"the configured scheduling timezone {name!r} is not a valid IANA timezone") from None


def scheduling_window(settings: dict | None, joining_date: date) -> tuple[date, date]:
    """(first, last) local dates a session may fall on, inclusive, relative to the joining date."""
    window = {**DEFAULT_SCHEDULING_WINDOW, **((settings or {}).get("scheduling_window") or {})}
    before, after = int(window["days_before_joining"]), int(window["days_after_joining"])
    if before < 0 or after < 0:
        raise ValueError("settings.scheduling_window days must not be negative")
    return joining_date - timedelta(days=before), joining_date + timedelta(days=after)


def validate_session_time(settings: dict | None, start: datetime, end: datetime, *, joining_date: date | None,
                          now: datetime | None = None) -> None:
    """The one check every session must pass BEFORE anything is booked or written: a joining date
    exists; end after start; both timezone-aware; office hours, working day and holiday in the
    client's timezone (validate_meeting_time); the local date inside the joining-date window; and,
    when `now` is given, not in the past. Raises InvalidMeetingTimeError (or its subclass
    OutsideSchedulingWindowError) -- callers route that to HR, never write the session."""
    settings = settings or {}
    if joining_date is None:
        raise InvalidMeetingTimeError("the case has no joining date; a session cannot be scheduled")
    if start.tzinfo is None or end.tzinfo is None:
        raise InvalidMeetingTimeError(f"{start.isoformat()} has no timezone; send an offset, e.g. +04:00")
    if end <= start:
        raise InvalidMeetingTimeError(f"the session must end after it starts ({start.isoformat()} - {end.isoformat()})")
    validate_meeting_time(settings, start, end)
    local_day = start.astimezone(_client_tz(settings)).date()
    first, last = scheduling_window(settings, joining_date)
    if not first <= local_day <= last:
        raise OutsideSchedulingWindowError(
            f"{local_day} is outside the scheduling window {first} to {last} (joining date {joining_date})")
    if now is not None and start <= now:
        raise InvalidMeetingTimeError(f"{start.isoformat()} is in the past")


def validate_meeting_time(settings: dict, start: datetime, end: datetime) -> None:
    """Step 3: a proposed slot must fall within configured office hours, on a configured working
    day, and not on a configured holiday -- checked in the client's configured local timezone,
    never in whatever timezone the caller happened to pass start/end in. A time without a timezone
    is refused: it would otherwise be read in the server's own local timezone."""
    if start.tzinfo is None or end.tzinfo is None:
        raise InvalidMeetingTimeError(f"{start.isoformat()} has no timezone; send an offset, e.g. +04:00")
    tz = _client_tz(settings)
    local_start, local_end = start.astimezone(tz), end.astimezone(tz)
    holidays = set(settings.get("holidays", []))
    weekend_weekdays = set(settings.get("weekend_weekdays", DEFAULT_WEEKEND_WEEKDAYS))
    office_hours = settings.get("office_hours", DEFAULT_OFFICE_HOURS)
    office_start, office_end = time.fromisoformat(office_hours["start"]), time.fromisoformat(office_hours["end"])

    if local_start.date().isoformat() in holidays:
        raise InvalidMeetingTimeError(f"{local_start.date()} is a configured holiday")
    if local_start.weekday() in weekend_weekdays:
        raise InvalidMeetingTimeError(f"{local_start.date()} falls on a configured weekend day")
    # A meeting that ends on a later local date (e.g. 23:00-00:30) is outside office hours too:
    # comparing clock times alone would read its 00:30 end as "before 18:00".
    if (local_start.time() < office_start or local_end.time() > office_end
            or local_end.date() != local_start.date()):
        raise InvalidMeetingTimeError(
            f"{local_start.time()}-{local_end.time()} {tz} is outside configured office hours "
            f"({office_start}-{office_end})")


def participants_for(settings: dict, record: OnboardingRecord, session_type: SessionType) -> list[str]:
    """Who the workflow invites (besides the new hire, whom schedule_session always adds) when it
    drafts a session itself: the roles in settings.workflow.session_participants, resolved to the
    people on record. A role that resolves to nobody (e.g. no buddy yet) is left out, not invented."""
    roles = settings.get("workflow", {}).get("session_participants", {}).get(session_type.value, [])
    resolved = (ROLE_PARTICIPANT_RESOLVERS[role](record, None) if role != "employee" else None for role in roles)
    return [p for p in dict.fromkeys(resolved) if p and p != record.employee_id]


def find_available_slot(calendar: CalendarPort, settings: dict, *, attendees: list[str], session_type: SessionType,
                        earliest: date, not_before: datetime | None = None,
                        latest: date | None = None) -> datetime:
    """The first start time on/after `earliest` and on/before `latest` (local dates in the configured
    timezone), not before `not_before` (normally "now"), that passes validate_meeting_time and has
    every attendee free. Deterministic: same inputs, same slot. Raises NoAvailableSlotError if the
    search window holds no such slot -- including when `earliest` is already past `latest` (e.g. the
    joining-date window is over): the window is never extended."""
    tz = _client_tz(settings)
    workflow = settings.get("workflow", {})
    search_days = int(workflow.get("slot_search_days", DEFAULT_SLOT_SEARCH_DAYS))
    step = timedelta(minutes=int(workflow.get("slot_step_minutes", DEFAULT_SLOT_STEP_MINUTES)))
    duration = timedelta(minutes=SESSION_DURATION_MINUTES[session_type])
    office_hours = settings.get("office_hours", DEFAULT_OFFICE_HOURS)
    office_start, office_end = time.fromisoformat(office_hours["start"]), time.fromisoformat(office_hours["end"])
    if latest is not None:
        search_days = min(search_days, (latest - earliest).days + 1)
        if search_days <= 0:
            raise NoAvailableSlotError(f"no {session_type.value} slot: the scheduling window ended on {latest}, "
                                       f"before {earliest}")

    for offset in range(search_days):
        day = earliest + timedelta(days=offset)
        start = datetime.combine(day, office_start, tzinfo=tz)
        while (end := start + duration).date() == day and end.time() <= office_end:
            try:
                validate_meeting_time(settings, start, end)
            except InvalidMeetingTimeError:
                break  # inside office hours, so the whole day is out (weekend or holiday)
            if not_before is not None and start < not_before:
                start += step
                continue
            if calendar.check_availability(attendees, start, end):
                return start
            start += step
    raise NoAvailableSlotError(f"no {session_type.value} slot for {attendees} within {search_days} days "
                               f"of {earliest}")


def invitation_timing(settings: dict | None) -> str:
    timing = (settings or {}).get("invitations", "on_approval")
    if timing not in INVITATION_TIMINGS:
        raise ValueError(f"settings.invitations must be one of {INVITATION_TIMINGS}, got {timing!r}")
    return timing


def booking_key(client_id: str, employee_id: str, session_type: SessionType, start: datetime) -> str:
    """The logical identity of one booking in the operation ledger."""
    return f"calendar:{client_id}:{employee_id}:{session_type.value}:{start.astimezone(timezone.utc).isoformat()}"


def in_flight_booking(port: OnboardingPort, client_id: str, employee_id: str,
                      session_type: SessionType) -> OperationRecord | None:
    """A booking of this type whose outcome was never settled (PENDING/UNCERTAIN) -- e.g. the
    provider timed out after creating the event. Its slot must be retried, not searched for
    again: the event it may have created makes that slot look busy."""
    prefix = f"calendar:{client_id}:{employee_id}:{session_type.value}:"
    return next((op for op in port.list_operations(client_id=client_id,
                                                   statuses=(OperationStatus.PENDING, OperationStatus.UNCERTAIN))
                 if op.key.startswith(prefix)), None)


def _live_session(port: OnboardingPort, employee_id: str, session_type: SessionType,
                  start: datetime) -> OnboardingSession | None:
    return next((s for s in port.list_sessions(employee_id) if s.session_type == session_type
                 and s.scheduled_start == start and s.status != SessionStatus.CANCELLED), None)


def _book(port: OnboardingPort, calendar: CalendarPort, audit: AuditLog, op: OperationRecord | None, *,
          key: str, client_id: str, employee_id: str, session_type: SessionType, attendees: list[str],
          start: datetime, end: datetime, title: str, notify: bool) -> tuple[OperationRecord, CalendarBooking | None]:
    """Books the hold through the operation ledger (see the module docstring). `op` is the
    ledger's current record for `key`. Returns the settled operation and the provider's booking
    (None when the ledger already held a SUCCEEDED booking from an earlier attempt, which is reused
    without calling the provider)."""
    if op is not None and op.status == OperationStatus.SUCCEEDED:
        return op, None
    if op is None or op.status in (OperationStatus.REJECTED, OperationStatus.RELEASED):
        op = OperationRecord(key=key, kind=BOOKING_KIND, client_id=client_id,
                             detail={"employee_id": employee_id, "session_type": session_type.value,
                                     "start": start.isoformat()})
    elif not calendar.idempotent_booking:
        audit.record("scheduling.booking_uncertain", agent=AGENT_ID, subject_id=employee_id, operation_key=key)
        raise BookingOutcomeUncertainError(
            f"an earlier attempt to book this {session_type.value} may have created a calendar event, and the "
            f"calendar provider cannot deduplicate a retry; check the calendar, then resolve operation '{key}'")
    op = port.save_operation(op.model_copy(update={"attempts": op.attempts + 1, "status": OperationStatus.PENDING}))
    try:
        booking = calendar.book_event(attendees=attendees, start=start, end=end, title=title,
                                      idempotency_key=op.operation_id, notify_attendees=notify)
    except CalendarConflictError as e:
        port.save_operation(op.model_copy(update={"status": OperationStatus.REJECTED, "last_error": type(e).__name__}))
        raise
    except CalendarBookingError as e:
        port.save_operation(op.model_copy(update={"status": OperationStatus.UNCERTAIN, "last_error": type(e).__name__}))
        raise
    op = port.save_operation(op.model_copy(update={
        "status": OperationStatus.SUCCEEDED, "provider_ref": booking.event_ref, "last_error": None,
        "detail": {**op.detail, "invitations_sent": booking.invitations_sent,
                   "invitation_failures": list(booking.invitation_failures)}}))
    return op, booking


def _release(port: OnboardingPort, calendar: CalendarPort, session: OnboardingSession) -> OnboardingSession:
    """Cancels a session's calendar hold (or meeting) and marks its booking operation RELEASED."""
    if session.external_event_ref:
        calendar.cancel_event(session.external_event_ref)
    op = port.get_operation(session.booking_operation_key) if session.booking_operation_key else None
    if op is not None and op.status != OperationStatus.RELEASED:
        port.save_operation(op.model_copy(update={"status": OperationStatus.RELEASED}))
    return port.update_session_status(session.id, SessionStatus.CANCELLED)


def _record_invitations(port: OnboardingPort, audit: AuditLog, session: OnboardingSession, *, sent: bool,
                        failures: list[str]) -> None:
    """PENDING rows for a silent hold; SENT/FAILED rows once the provider actually invited."""
    for attendee in session.attendees:
        status = (InvitationStatus.FAILED if attendee in failures else InvitationStatus.SENT) if sent \
            else InvitationStatus.PENDING
        port.record_invitation(session.id, attendee, status)
    _audit_invitations(audit, session, sent=sent, failures=failures)


def _audit_invitations(audit: AuditLog, session: OnboardingSession, *, sent: bool, failures: list[str]) -> None:
    if not sent:
        audit.record("scheduling.invitations_pending_approval", agent=AGENT_ID, subject_id=session.employee_id,
                     session_id=session.id, attendees=len(session.attendees))
        return
    for attendee in failures:
        audit.record("scheduling.invitation_failed", agent=AGENT_ID, subject_id=session.employee_id,
                     session_id=session.id, participant_id=attendee)
    audit.record("scheduling.invitations_sent", agent=AGENT_ID, subject_id=session.employee_id, session_id=session.id,
                 sent=len(session.attendees) - len(failures), failed=len(failures))


def outstanding_sessions(port: OnboardingPort, employee_id: str) -> list[OnboardingSession]:
    """Orientation/induction/team-intro/buddy-intro activities booked but not yet confirmed by HR.
    Mirrors checklist.pending_tasks for the scheduling side of UC-05 requirement 6 ('track
    outstanding orientation activities')."""
    return [s for s in port.list_sessions(employee_id) if s.status == SessionStatus.SCHEDULED]


def propose_buddy_assignment(port: OnboardingPort, hitl: HITLInbox, audit: AuditLog, *, employee_id: str,
                             workflow_instance: str) -> tuple[str, ApprovalItem | None]:
    """Idempotent: an employee who already has a buddy keeps them (no new proposal), and an
    employee with a proposal still awaiting HR gets that same pending proposal back rather than a
    second one. Otherwise proposes one eligible candidate -- preferring the new hire's own team,
    falling back to any active candidate for the client -- for HR approval. The record is only
    updated once that proposal is approved (see apply_buddy_decision); never a name a model suggests.

    Returns (buddy_id, approval_item); approval_item is None when already assigned.
    """
    record = port.get_onboarding_record(employee_id)
    if record.buddy_id:
        return record.buddy_id, None
    pending = [i for i in hitl.list(status=Decision.PENDING, gate=BUDDY_GATE, client_id=record.client_id)
               if i.agent == AGENT_ID and i.subject_id == employee_id]
    if pending:
        audit.record("scheduling.buddy_assignment_already_pending", agent=AGENT_ID, subject_id=employee_id,
                     approval_id=pending[0].id)
        return pending[0].payload["buddy_id"], pending[0]
    candidates = port.list_buddy_candidates(record.client_id)
    if not candidates:
        raise NoBuddyCandidatesError(f"no buddy candidates are configured for {record.client_id}")
    same_team = [c for c in candidates if c.team == record.team]
    chosen = (same_team or candidates)[0]

    item = hitl.submit(ApprovalItem(
        gate=BUDDY_GATE, approver_role="hr_admin", workflow="employee-onboarding",
        workflow_instance=workflow_instance, agent=AGENT_ID, proposal_type="buddy_assignment",
        client_id=record.client_id, subject_id=employee_id,
        payload={"employee_id": employee_id, "buddy_id": chosen.employee_id},
        rationale=f"Assign {chosen.full_name} ({chosen.team}) as onboarding buddy for {employee_id}"
                 f"{' (same team)' if same_team else ' (no same-team candidate available)'}.",
        confidence=0.9 if same_team else 0.6, run_id=new_id("run")))
    if isinstance(port, BuddyAssignmentLedger):
        port.record_buddy_proposal(employee_id, chosen.employee_id, approval_id=item.id,
                                   match_score=item.confidence, same_team=bool(same_team))
    audit.record("scheduling.buddy_assignment_proposed", agent=AGENT_ID, subject_id=employee_id,
                 buddy_id=chosen.employee_id, same_team=bool(same_team), approval_id=item.id)
    return chosen.employee_id, item


def apply_buddy_decision(port: OnboardingPort, notifier: NotificationPort, audit: AuditLog, item: ApprovalItem, *,
                         channel: NotificationChannel = NotificationChannel.EMAIL) -> None:
    """HITLInbox decision -> OnboardingRecord.buddy_id + a notification to both the buddy and the
    new hire, delivered through NotificationPort and then recorded on the case.

    Safe to re-run for the same decision (HITLInbox.retry_apply after a notification outage):
    the assignment is not repeated, and only recipients without a recorded notification for this
    approval are notified. A provider failure (NotificationDeliveryError) propagates so the
    decision's apply_error is set and the remaining notifications can be retried."""
    if item.agent != AGENT_ID or item.proposal_type != "buddy_assignment":
        return
    employee_id = item.payload["employee_id"]
    record = port.get_onboarding_record(employee_id)
    approved = item.status in (Decision.APPROVED, Decision.EDITED)
    buddy_id = ((item.final_payload or {}).get("buddy_id", item.payload["buddy_id"])
                if item.status == Decision.EDITED else item.payload["buddy_id"])
    if record.buddy_id and not (approved and record.buddy_id == buddy_id):
        audit.record("scheduling.late_decision_ignored", agent=AGENT_ID, subject_id=employee_id,
                     decision=item.status)
        return  # already assigned (e.g. by an earlier decision) -- never reassign silently

    if approved:
        if not record.buddy_id:
            eligible = {c.employee_id for c in port.list_buddy_candidates(record.client_id)}
            if buddy_id not in eligible:
                audit.record("scheduling.buddy_rejected_ineligible", agent=AGENT_ID, subject_id=employee_id,
                             buddy_id=buddy_id, approval_id=item.id, decision=item.status)
                raise IneligibleBuddyError(f"{buddy_id} is not an active buddy candidate for {record.client_id}; "
                                           f"decision {item.id} was recorded but not applied")
            port.assign_buddy(employee_id, buddy_id)
            audit.record("scheduling.buddy_assigned", agent=AGENT_ID, subject_id=employee_id, buddy_id=buddy_id)
        already_notified = {n.recipient_id for n in port.list_notifications(employee_id)
                            if n.kind == "buddy_assignment" and n.detail.get("approval_id") == item.id}
        rejected: list[str] = []
        for recipient_id, role in ((buddy_id, "buddy"), (employee_id, "new_hire")):
            if recipient_id in already_notified:
                continue
            body = (f"You have been assigned as the onboarding buddy for {employee_id}." if role == "buddy"
                    else f"Your onboarding buddy is {buddy_id}.")
            try:
                receipt = deliver_once(port, notifier, NotificationMessage(
                    channel=channel, client_id=record.client_id, employee_id=employee_id, recipient_id=recipient_id,
                    recipient_address=record.email if role == "new_hire" else None, kind="buddy_assignment",
                    subject="Onboarding buddy assignment", body=body, metadata={"approval_id": item.id}),
                    key=f"buddy-notice:{record.client_id}:{item.id}:{recipient_id}")
            except NotificationRejectedError:
                rejected.append(recipient_id)  # permanent for this recipient; still notify the other one
                continue
            port.record_notification(employee_id, recipient_id, "buddy_assignment", role=role, buddy_id=buddy_id,
                                     approval_id=item.id, channel=receipt.channel,
                                     delivery_ref=receipt.delivery_ref, simulated=receipt.simulated)
        audit.record("scheduling.buddy_notified", agent=AGENT_ID, subject_id=employee_id, buddy_id=buddy_id,
                     rejected_recipients=rejected)
        if rejected:
            raise NotificationRejectedError(f"buddy assignment notice rejected for {', '.join(rejected)}")
    elif item.status == Decision.REJECTED:
        if isinstance(port, BuddyAssignmentLedger):
            port.release_buddy_proposal(employee_id, item.payload["buddy_id"])
        audit.record("scheduling.buddy_assignment_rejected", agent=AGENT_ID, subject_id=employee_id,
                     buddy_id=item.payload["buddy_id"], reason_code=item.reason_code)


def schedule_session(port: OnboardingPort, calendar: CalendarPort, hitl: HITLInbox, audit: AuditLog, *,
                     employee_id: str, session_type: SessionType, start: datetime, attendees: list[str],
                     workflow_instance: str, approver_role: str | None = None,
                     settings: dict | None = None,
                     now: datetime | None = None) -> tuple[OnboardingSession, ApprovalItem]:
    record = port.get_onboarding_record(employee_id)  # KeyError if unknown; read fresh: the current joining date
    end = start + timedelta(minutes=SESSION_DURATION_MINUTES[session_type])
    all_attendees = [employee_id, *attendees]
    approver_role = approver_role or DEFAULT_APPROVER_ROLE[session_type]

    # every creation path (workflow, HR-chosen time, buddy intro, replan, retry) passes here BEFORE
    # anything is booked or written
    validate_session_time(settings, start, end, joining_date=record.start_date, now=now)
    notify = invitation_timing(settings) == "on_hold"
    key = booking_key(record.client_id, employee_id, session_type, start)
    op = port.get_operation(key)
    live = _live_session(port, employee_id, session_type, start)
    # Resuming an earlier attempt whose outcome is unsettled (or whose session was never written)
    # skips the availability check: that attempt's own hold makes the slot look busy.
    resuming = live is None and op is not None and op.status in (
        OperationStatus.PENDING, OperationStatus.UNCERTAIN, OperationStatus.SUCCEEDED)
    if not resuming:
        if not calendar.check_availability(all_attendees, start, end):
            audit.record("scheduling.conflict", agent=AGENT_ID, subject_id=employee_id,
                         session_type=session_type)
            raise CalendarConflictError(f"{all_attendees} are not all available between {start} and {end}")
        if live is not None:  # recorded by another path the calendar does not know about
            raise DuplicateSessionError(f"a {session_type} session already exists for {employee_id} at {start}")

    title = f"{session_type.value.replace('_', ' ').title()} - {record.full_name}"
    op, booking = _book(port, calendar, audit, op, key=key, client_id=record.client_id, employee_id=employee_id,
                        session_type=session_type, attendees=all_attendees, start=start, end=end, title=title,
                        notify=notify)
    event_ref = op.provider_ref or ""
    try:
        session = port.create_session(OnboardingSession(
            employee_id=employee_id, client_id=record.client_id, session_type=session_type,
            scheduled_start=start, scheduled_end=end, attendees=all_attendees,
            external_event_ref=event_ref, booking_operation_key=key))
    except DuplicateSessionError:
        # A concurrent request created the session first. Its hold is ours too when the provider
        # deduplicated on the shared idempotency key; otherwise never leave an orphaned hold.
        live = _live_session(port, employee_id, session_type, start)
        if live is None or live.external_event_ref != event_ref:
            calendar.cancel_event(event_ref)
            port.save_operation(op.model_copy(update={"status": OperationStatus.RELEASED}))
        raise

    _record_invitations(port, audit, session, sent=bool(op.detail.get("invitations_sent")),
                        failures=list(op.detail.get("invitation_failures", [])))
    item = hitl.submit(ApprovalItem(
        gate=GATE, approver_role=approver_role, workflow="employee-onboarding", workflow_instance=workflow_instance,
        agent=AGENT_ID, proposal_type="session_booking", client_id=record.client_id, subject_id=employee_id,
        payload=session.model_dump(mode="json"),
        rationale=f"Confirm {session_type.value} booking ({event_ref}).", confidence=0.9,
        run_id=new_id("run")))
    audit.record("scheduling.session_booked", agent=AGENT_ID, subject_id=employee_id, session_id=session.id,
                 event_ref=event_ref, approval_id=item.id, approver_role=approver_role,
                 operation_id=op.operation_id, reused_booking=booking is None)
    return session, item


def schedule_buddy_intro(port: OnboardingPort, calendar: CalendarPort, hitl: HITLInbox, audit: AuditLog, *,
                         employee_id: str, start: datetime, workflow_instance: str,
                         approver_role: str | None = None, settings: dict | None = None,
                         now: datetime | None = None) -> tuple[OnboardingSession, ApprovalItem]:
    """Requires a buddy to already be assigned (via propose_buddy_assignment + an approved
    decision) -- scheduling can no longer silently auto-assign one, since assignment itself is
    now gated on HR approval and cannot complete synchronously within this call."""
    record = port.get_onboarding_record(employee_id)  # KeyError if unknown
    if not record.buddy_id:
        raise BuddyNotAssignedError(
            f"{employee_id} has no approved buddy yet; call propose_buddy_assignment first")
    return schedule_session(port, calendar, hitl, audit, employee_id=employee_id,
                            session_type=SessionType.BUDDY_INTRO, start=start, attendees=[record.buddy_id],
                            workflow_instance=workflow_instance, approver_role=approver_role, settings=settings,
                            now=now)


def check_decider(port: OnboardingPort, item: ApprovalItem, approver: str | None) -> None:
    """hr_admin is a pool (any hr_admin may decide); team_lead/buddy name one specific person.
    Callable BEFORE a decision is recorded (services/decisions.py), so the wrong person is refused
    without consuming the approval. The message names roles, not the person on record."""
    expected_field = EXPECTED_APPROVER_FIELD.get(item.approver_role)
    if expected_field is None:
        return
    record = port.get_onboarding_record(item.subject_id)
    if not approver or approver != getattr(record, expected_field):
        raise UnauthorizedApproverError(f"only the {item.approver_role} on record for this employee may decide "
                                        f"approval {item.id}")


def _check_instance_approver(port: OnboardingPort, item: ApprovalItem) -> None:
    """The same check on an already-recorded decision, inside the HITL listener -- the last line of
    defence for a decision recorded through a path that did not validate it first (HITLInbox itself
    only checks role membership): the decision stays recorded, but no action is applied."""
    try:
        check_decider(port, item, item.approver)
    except UnauthorizedApproverError as e:
        raise UnauthorizedApproverError(f"{e}; the decision was recorded but no action was applied") from None


def apply_decision(port: OnboardingPort, calendar: CalendarPort, audit: AuditLog, item: ApprovalItem, *,
                   settings: dict | None = None) -> None:
    """HITLInbox decision -> calendar confirm/cancel + OnboardingSession status. Register via
    hitl.subscribe(...); a session is only ever CONFIRMED in response to a real HITL decision.
    An approval of a hold that no longer fits the window around the CURRENT joining date (the
    joining date changed after it was drafted) releases the hold instead of confirming it."""
    if item.agent != AGENT_ID or item.proposal_type != "session_booking":
        return
    _check_instance_approver(port, item)
    session_id = item.payload["id"]
    session = port.get_session(session_id)
    event_ref = session.external_event_ref or item.payload["external_event_ref"]
    if session.status != SessionStatus.SCHEDULED:
        # already resolved -- most likely cancelled by check_approval_timeouts before this (late)
        # decision arrived. The HITL item is still recorded, but there is nothing left to apply.
        audit.record("scheduling.late_decision_ignored", agent=AGENT_ID, subject_id=item.subject_id,
                     session_id=session_id, current_status=session.status, decision=item.status)
        return
    if item.status in (Decision.APPROVED, Decision.EDITED):
        joining_date = port.get_onboarding_record(session.employee_id).start_date
        try:
            validate_session_time(settings, session.scheduled_start, session.scheduled_end, joining_date=joining_date)
        except OutsideSchedulingWindowError as exc:
            _release(port, calendar, session)
            audit.record("scheduling.stale_session_released", agent=AGENT_ID, subject_id=item.subject_id,
                         session_id=session_id, approval_id=item.id, joining_date=joining_date.isoformat(),
                         reason=str(exc)[:300])
            return
        # Confirming is what invites the attendees (unless the hold already did, settings.invitations:
        # on_hold). Safe to repeat after an outage: a confirmed, already-invited event is unchanged.
        confirmed = calendar.confirm_event(event_ref, notify_attendees=True)
        pending = [i for i in port.list_invitations(session_id) if i.status == InvitationStatus.PENDING]
        if pending and confirmed.invitations_sent:
            for invitation in pending:
                port.update_invitation_status(invitation.id, InvitationStatus.FAILED
                                              if invitation.participant_id in confirmed.invitation_failures
                                              else InvitationStatus.SENT)
            _audit_invitations(audit, session, sent=True, failures=[p for p in confirmed.invitation_failures
                                                                    if p in {i.participant_id for i in pending}])
        port.update_session_status(session_id, SessionStatus.CONFIRMED)
        audit.record("scheduling.confirmed", agent=AGENT_ID, subject_id=item.subject_id, session_id=session_id)
    elif item.status == Decision.REJECTED:
        _release(port, calendar, session)
        audit.record("scheduling.cancelled", agent=AGENT_ID, subject_id=item.subject_id, session_id=session_id,
                     reason_code=item.reason_code)


def check_approval_timeouts(port: OnboardingPort, calendar: CalendarPort, hitl: HITLInbox, audit: AuditLog, *,
                            as_of: datetime, client_id: str | None = None) -> list[dict]:
    """UC step 4: a session_booking approval that sits PENDING past its sla_hours is cancelled --
    the tentative calendar hold is released rather than blocking the slot indefinitely. The
    HITLInbox item itself is left PENDING (HITLInbox has no 'expired' decision and deciding it
    would need a human actor it doesn't have); apply_decision's late-decision guard makes it safe
    if HR decides on it anyway after the fact. Scope to client_id to sweep one tenant at a time;
    None sweeps every tenant's items the caller's HITLInbox holds.
    """
    results: list[dict] = []
    # Scoped at the query itself when a tenant is known (finding S1 posture: do not rely solely
    # on filtering returned results), not only by the per-session check below.
    for item in hitl.list(status=Decision.PENDING, client_id=client_id):
        if item.agent != AGENT_ID or item.proposal_type != "session_booking":
            continue
        if item.created_at + timedelta(hours=item.sla_hours) > as_of:
            continue
        session_id = item.payload["id"]
        session = port.get_session(session_id)
        if client_id is not None and session.client_id != client_id:
            continue
        if session.status != SessionStatus.SCHEDULED:
            continue  # already resolved by a real decision or an earlier sweep
        _release(port, calendar, session)
        audit.record("scheduling.approval_timed_out", agent=AGENT_ID, subject_id=item.subject_id,
                     session_id=session_id, approval_id=item.id, sla_hours=item.sla_hours)
        results.append({"session_id": session_id, "approval_id": item.id, "employee_id": item.subject_id})
    # A REJECTED booking whose apply failed (provider outage, kill switch) cannot be retried --
    # HITLInbox.retry_apply only replays approvals -- so the sweep releases its hold instead.
    for item in hitl.list(status=Decision.REJECTED, client_id=client_id):
        if item.agent != AGENT_ID or item.proposal_type != "session_booking":
            continue
        session = port.get_session(item.payload["id"])
        if (client_id is not None and session.client_id != client_id) or session.status != SessionStatus.SCHEDULED:
            continue
        _release(port, calendar, session)
        audit.record("scheduling.rejection_applied_by_sweep", agent=AGENT_ID, subject_id=item.subject_id,
                     session_id=session.id, approval_id=item.id)
        results.append({"session_id": session.id, "approval_id": item.id, "employee_id": item.subject_id})
    return results


# --------------------------------------------------------------------------- UC step 5: participant
# responses + HR decision (Don't Cancel / Cancel / Reschedule)

def attendance_requirements_for(settings: dict, session_type: SessionType) -> list[str]:
    return settings.get("attendance_requirements", {}).get(session_type.value, [])


def is_attendance_satisfied(port: OnboardingPort, settings: dict, session: OnboardingSession) -> bool:
    """True if every configured required role (e.g. 'team_lead' for a team_intro) has responded
    accepted or tentative. A role with no resolvable participant (e.g. no buddy assigned yet)
    counts as unsatisfied -- it is never silently skipped."""
    record = port.get_onboarding_record(session.employee_id)
    responses = {r.participant_id: r.response for r in port.list_participant_responses(session.id)}
    for role in attendance_requirements_for(settings, session.session_type):
        participant_id = ROLE_PARTICIPANT_RESOLVERS[role](record, session)
        if participant_id is None or responses.get(participant_id) not in (ResponseStatus.ACCEPTED,
                                                                            ResponseStatus.TENTATIVE):
            return False
    return True


def pending_conflicts(hitl: HITLInbox, session: OnboardingSession,
                      participant_id: str | None = None) -> list[ApprovalItem]:
    """Undecided attendee_declined items for this session (and participant, when given)."""
    return [i for i in hitl.list(status=Decision.PENDING, gate=ATTENDANCE_GATE, client_id=session.client_id)
            if i.agent == AGENT_ID and i.proposal_type == "attendee_declined"
            and i.payload.get("session_id") == session.id
            and (participant_id is None or i.payload.get("participant_id") == participant_id)]


def conflict_superseded_reason(port: OnboardingPort, item: ApprovalItem) -> str | None:
    """Why an attendee_declined decision no longer applies, derived from the current records at
    the moment it is used -- never from the state when the item was opened: the session was
    cancelled meanwhile, or the participant's latest RSVP (newest by provider timestamp) is no
    longer a decline. None while the decline still stands."""
    session = port.get_session(item.payload["session_id"])
    replan = port.get_operation(f"replan:{session.client_id}:{item.id}")
    if item.status == Decision.EDITED and replan is not None and replan.status == OperationStatus.PENDING:
        return None  # this decision's own replan stopped part-way (after cancelling); it resumes
    if session.status == SessionStatus.CANCELLED:
        return "session_cancelled"
    latest = next((r for r in port.list_participant_responses(session.id)
                   if r.participant_id == item.payload["participant_id"]), None)
    if latest is not None and latest.response != ResponseStatus.DECLINED:
        return f"participant_now_{latest.response.value}"
    return None


def record_response(port: OnboardingPort, hitl: HITLInbox, audit: AuditLog, *, session_id: str,
                    participant_id: str, response: ResponseStatus, workflow_instance: str,
                    responded_at: datetime | None = None) -> ParticipantResponse:
    """Records the response. A DECLINED response always surfaces an HR decision -- never silently
    treated as low-priority just because the role isn't in the configured requirements. Only an
    invited participant of a live (not cancelled) session can respond, so a stray or forged RSVP
    can neither be recorded nor open an HR conflict decision.

    Ordering: `responded_at` is the provider's timestamp for the response (the receipt time when
    the caller has none). A response older than the one already recorded raises
    StaleParticipantResponseError and changes nothing. A repeated decline while that
    participant's earlier decline is still undecided does not open a second HR decision."""
    session = port.get_session(session_id)  # KeyError if unknown
    if session.status == SessionStatus.CANCELLED:
        raise InvalidParticipantResponseError(f"session {session_id} is cancelled; responses are no longer accepted")
    if participant_id not in session.attendees:
        raise InvalidParticipantResponseError(f"{participant_id} is not an invited participant of session {session_id}")
    when = responded_at or now_utc()
    if when.tzinfo is None:
        raise InvalidParticipantResponseError("responded_at has no timezone; send an offset, e.g. +04:00")
    recorded = port.record_participant_response(session_id, participant_id, response, responded_at=when)
    if recorded.responded_at > when:
        audit.record("scheduling.stale_response_ignored", agent=AGENT_ID, subject_id=session.employee_id,
                     session_id=session_id, participant_id=participant_id, response=response,
                     responded_at=when.isoformat(), current_responded_at=recorded.responded_at.isoformat())
        raise StaleParticipantResponseError(f"a newer response from {participant_id} for session {session_id} is "
                                            f"already recorded; this one was ignored")
    audit.record("scheduling.participant_response_recorded", agent=AGENT_ID, subject_id=session.employee_id,
                 session_id=session_id, participant_id=participant_id, response=response)
    if response == ResponseStatus.DECLINED:
        if pending_conflicts(hitl, session, participant_id):
            audit.record("scheduling.attendance_decision_already_pending", agent=AGENT_ID,
                         subject_id=session.employee_id, session_id=session_id, participant_id=participant_id)
            return recorded
        item = hitl.submit(ApprovalItem(
            gate=ATTENDANCE_GATE, approver_role="hr_admin", workflow="employee-onboarding",
            workflow_instance=workflow_instance, agent=AGENT_ID, proposal_type="attendee_declined",
            client_id=session.client_id, subject_id=session.employee_id,
            payload={"session_id": session_id, "participant_id": participant_id,
                    "session_type": session.session_type.value},
            rationale=f"{participant_id} declined the {session.session_type.value} booking for "
                     f"{session.employee_id}. Choose: approve = don't cancel, reject = cancel, "
                     f"edit with a new_start = reschedule.",
            confidence=1.0, run_id=new_id("run")))
        audit.record("scheduling.attendance_decision_requested", agent=AGENT_ID, subject_id=session.employee_id,
                     session_id=session_id, participant_id=participant_id, approval_id=item.id)
    return recorded


def apply_attendance_decision(port: OnboardingPort, calendar: CalendarPort, hitl: HITLInbox, audit: AuditLog,
                              item: ApprovalItem, *, settings: dict | None = None,
                              approver_role: str | None = None,
                              now: datetime | None = None) -> OnboardingSession | None:
    """HR's Don't-Cancel/Cancel/Reschedule decision on a declined participant. Reuses the real
    Decision enum: APPROVED = don't cancel, REJECTED = cancel, EDITED (edited_payload={"new_start":
    timezone-aware iso datetime}) = cancel the old booking and book a fresh one, going through its
    own availability check and its own new scheduling_approval item -- never confirmed silently.

    A decision that no longer applies (conflict_superseded_reason: the session was cancelled
    meanwhile, or the participant has since accepted) is recorded and ignored, so a stale decision
    can neither cancel a meeting that is now fine nor book a second replacement. A replan's new
    start is parsed and checked against office hours BEFORE the original meeting is cancelled, so a
    malformed edit leaves the original meeting in place (raises ValueError, KeyError or
    InvalidMeetingTimeError). A replan that stopped after cancelling the original (an outage while
    booking the replacement) resumes the booking when the decision is retried."""
    if item.agent != AGENT_ID or item.proposal_type != "attendee_declined":
        return None
    session_id = item.payload["session_id"]
    session = port.get_session(session_id)
    replan_key = f"replan:{session.client_id}:{item.id}"
    replan = port.get_operation(replan_key)
    reason = conflict_superseded_reason(port, item)
    if reason is not None:
        audit.record("scheduling.late_decision_ignored", agent=AGENT_ID, subject_id=item.subject_id,
                     session_id=session_id, current_status=session.status, decision=item.status, reason=reason)
        return None

    new_start = None
    if item.status == Decision.EDITED:
        new_start = datetime.fromisoformat((item.final_payload or {})["new_start"])
        validate_session_time(settings, new_start,
                              new_start + timedelta(minutes=SESSION_DURATION_MINUTES[session.session_type]),
                              joining_date=port.get_onboarding_record(session.employee_id).start_date, now=now)

    if item.status == Decision.APPROVED:
        audit.record("scheduling.attendance_decision_applied", agent=AGENT_ID, subject_id=item.subject_id,
                     session_id=session_id, action="dont_cancel")
        return None

    if item.status == Decision.EDITED and replan is None:
        replan = port.save_operation(OperationRecord(key=replan_key, kind=REPLAN_KIND, client_id=session.client_id,
                                                     detail={"session_id": session_id,
                                                             "new_start": new_start.isoformat() if new_start else None}))
    if session.status != SessionStatus.CANCELLED:
        _release(port, calendar, session)

    if item.status == Decision.REJECTED:
        audit.record("scheduling.attendance_decision_applied", agent=AGENT_ID, subject_id=item.subject_id,
                     session_id=session_id, action="cancel")
        return None

    if item.status == Decision.EDITED and new_start is not None and replan is not None:
        other_attendees = [a for a in session.attendees if a != session.employee_id]
        try:
            rescheduled = _live_session(port, session.employee_id, session.session_type, new_start)
            if rescheduled is None:
                rescheduled, _ = schedule_session(port, calendar, hitl, audit, employee_id=session.employee_id,
                                                  session_type=session.session_type, start=new_start,
                                                  attendees=other_attendees, workflow_instance=item.workflow_instance,
                                                  approver_role=approver_role, settings=settings, now=now)
        except (CalendarConflictError, DuplicateSessionError, InvalidMeetingTimeError) as e:
            # definitive: the replacement cannot be booked; HR is told (replan_failed in the workflow).
            # An outage propagates instead and leaves the replan PENDING, so a retry resumes it.
            port.save_operation(replan.model_copy(update={"status": OperationStatus.REJECTED,
                                                          "last_error": type(e).__name__}))
            raise
        port.save_operation(replan.model_copy(update={"status": OperationStatus.SUCCEEDED,
                                                      "provider_ref": rescheduled.id}))
        audit.record("scheduling.attendance_decision_applied", agent=AGENT_ID, subject_id=item.subject_id,
                     session_id=session_id, action="reschedule", new_session_id=rescheduled.id)
        return rescheduled

    return None
