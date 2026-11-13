"""Regression tests for joining-date-based scheduling (services/scheduling.py):

Rule (business decision 2026-11-04, policy.yaml settings.scheduling_window): every session's LOCAL
date, in the client's timezone, lies from the joining date to joining date + 2 calendar days, on a
working day, not a holiday, inside office hours, and not in the past. No valid slot -> an explicit
failure routed to HR; nothing is booked or written.

Deterministic: fixed clocks, the in-memory repository and calendar; no credentials, no network.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import Decision, HITLInbox, ReasonCode
from sub_agent.fakes import FakeCalendar, build_mock_onboarding
from sub_agent.models import OnboardingRecord, ResponseStatus, SessionStatus, SessionType
from sub_agent.services.decisions import InvalidDecisionError, validate_decision
from sub_agent.services.scheduling import (InvalidMeetingTimeError, NoAvailableSlotError,
                                           OutsideSchedulingWindowError, apply_attendance_decision, apply_decision,
                                           find_available_slot, record_response, schedule_session, scheduling_window,
                                           validate_session_time)

DUBAI, KOLKATA, NEW_YORK = ZoneInfo("Asia/Dubai"), ZoneInfo("Asia/Kolkata"), ZoneInfo("America/New_York")
SETTINGS = {"timezone": "Asia/Dubai", "office_hours": {"start": "09:00", "end": "18:00"},
            "weekend_weekdays": [4, 5], "holidays": [], "scheduling_window": {"days_before_joining": 0,
                                                                              "days_after_joining": 2}}
NOW = datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)
JOINING = date(2026, 11, 2)  # EMP-001 (CLIENT-ACME), a Monday


@pytest.fixture
def port():
    return build_mock_onboarding()


@pytest.fixture
def calendar():
    return FakeCalendar()


@pytest.fixture
def hitl():
    return HITLInbox()


def hire(port, employee_id: str, joining: date) -> str:
    port.create_onboarding_case(OnboardingRecord(
        employee_id=employee_id, full_name="Window Test", email="window.test@example.com", role_title="Analyst",
        department="Engineering", team="Platform", team_lead_id="TL-001", client_id="CLIENT-ACME",
        start_date=joining))
    return employee_id


def slot(calendar, settings, joining: date, *, now: datetime, session_type=SessionType.ORIENTATION) -> datetime:
    """The automatic search exactly as OnboardingToolRegistry.find_slot runs it."""
    first, last = scheduling_window(settings, joining)
    today = now.astimezone(ZoneInfo(settings["timezone"])).date()
    return find_available_slot(calendar, settings, attendees=["EMP-001"], session_type=session_type,
                               earliest=max(first, today), latest=last, not_before=now)


def book(port, calendar, hitl, employee_id, start, *, settings=SETTINGS, now=NOW, audit=None):
    return schedule_session(port, calendar, hitl, audit or AuditLog(), employee_id=employee_id,
                            session_type=SessionType.ORIENTATION, start=start, attendees=["HM-01"],
                            workflow_instance=employee_id, settings=settings, now=now)


def assert_nothing_written(port, calendar, hitl, employee_id):
    assert port.list_sessions(employee_id) == []
    assert hitl.list(status=None) == []
    assert calendar._bookings == {}


# ------------------------------------------------------------------- the window itself

def test_window_is_joining_date_to_two_days_after():
    assert scheduling_window(SETTINGS, JOINING) == (date(2026, 11, 2), date(2026, 11, 4))
    assert scheduling_window({}, JOINING) == (date(2026, 11, 2), date(2026, 11, 4))  # the built-in default
    with pytest.raises(ValueError):
        scheduling_window({"scheduling_window": {"days_before_joining": -1}}, JOINING)


# ------------------------------------------------------------------- automatic slot search

def test_future_joining_date_is_scheduled_within_the_window(calendar):
    start = slot(calendar, SETTINGS, JOINING, now=NOW)
    assert start == datetime(2026, 11, 2, 9, 0, tzinfo=DUBAI)  # the joining day, first office hour


def test_joining_date_today_schedules_only_a_future_slot_before_the_cutoff(calendar):
    noon = datetime(2026, 11, 2, 12, 10, tzinfo=DUBAI)
    start = slot(calendar, SETTINGS, JOINING, now=noon)
    assert start.date() == JOINING and start >= noon  # later the same day, never in the past
    evening = datetime(2026, 11, 4, 17, 30, tzinfo=DUBAI)  # last window day, after the last slot
    with pytest.raises(NoAvailableSlotError):
        slot(calendar, SETTINGS, JOINING, now=evening)


def test_joining_date_in_the_past_is_never_scheduled_automatically(calendar):
    with pytest.raises(NoAvailableSlotError, match="window ended"):
        slot(calendar, SETTINGS, JOINING, now=datetime(2026, 11, 10, 5, 0, tzinfo=timezone.utc))


def test_weekend_joining_date_moves_to_an_eligible_working_day_inside_the_window(calendar):
    friday = date(2026, 11, 6)  # Dubai weekend: Friday and Saturday
    assert slot(calendar, SETTINGS, friday, now=NOW) == datetime(2026, 11, 8, 9, 0, tzinfo=DUBAI)  # Sunday


def test_holiday_on_the_joining_date_moves_to_the_next_working_day(calendar):
    settings = {**SETTINGS, "holidays": ["2026-11-02"]}
    assert slot(calendar, settings, JOINING, now=NOW) == datetime(2026, 11, 3, 9, 0, tzinfo=DUBAI)


def test_window_with_no_working_day_fails_explicitly_rather_than_extending(calendar):
    settings = {**SETTINGS, "holidays": ["2026-11-02", "2026-11-03", "2026-11-04"]}
    with pytest.raises(NoAvailableSlotError):
        slot(calendar, settings, JOINING, now=NOW)


# ------------------------------------------------------------------- validation before any write

def test_session_after_the_window_is_rejected_and_nothing_is_written(port, calendar, hitl):
    with pytest.raises(OutsideSchedulingWindowError, match="2026-11-05 is outside the scheduling window"):
        book(port, calendar, hitl, "EMP-001", datetime(2026, 11, 5, 9, 0, tzinfo=DUBAI))
    assert_nothing_written(port, calendar, hitl, "EMP-001")


def test_session_before_the_joining_date_is_rejected(port, calendar, hitl):
    with pytest.raises(OutsideSchedulingWindowError):
        book(port, calendar, hitl, "EMP-001", datetime(2026, 11, 1, 9, 0, tzinfo=DUBAI))
    assert_nothing_written(port, calendar, hitl, "EMP-001")


def test_far_future_synthetic_date_like_2030_is_rejected(port, calendar, hitl):
    """The live test once wrote 2030-01-07 directly; no UC05 path may accept such a date."""
    with pytest.raises(OutsideSchedulingWindowError):
        book(port, calendar, hitl, "EMP-001", datetime(2030, 1, 7, 6, 0, tzinfo=timezone.utc))
    assert_nothing_written(port, calendar, hitl, "EMP-001")


def test_configured_window_without_days_after_rejects_the_next_day(port, calendar, hitl):
    settings = {**SETTINGS, "scheduling_window": {"days_before_joining": 0, "days_after_joining": 0}}
    with pytest.raises(OutsideSchedulingWindowError):
        book(port, calendar, hitl, "EMP-001", datetime(2026, 11, 3, 9, 0, tzinfo=DUBAI), settings=settings)
    session, _ = book(port, calendar, hitl, "EMP-001", datetime(2026, 11, 2, 9, 0, tzinfo=DUBAI), settings=settings)
    assert session.scheduled_start.astimezone(DUBAI).date() == JOINING


def test_session_in_the_past_is_rejected(port, calendar, hitl):
    with pytest.raises(InvalidMeetingTimeError, match="in the past"):
        book(port, calendar, hitl, "EMP-001", datetime(2026, 11, 2, 9, 0, tzinfo=DUBAI),
             now=datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI))
    assert_nothing_written(port, calendar, hitl, "EMP-001")


def test_missing_joining_date_is_never_scheduled():
    with pytest.raises(InvalidMeetingTimeError, match="no joining date"):
        validate_session_time(SETTINGS, datetime(2026, 11, 2, 9, 0, tzinfo=DUBAI),
                              datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI), joining_date=None)


def test_end_before_start_is_rejected():
    start = datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI)
    with pytest.raises(InvalidMeetingTimeError, match="must end after it starts"):
        validate_session_time(SETTINGS, start, start - timedelta(minutes=30), joining_date=JOINING)
    with pytest.raises(InvalidMeetingTimeError, match="must end after it starts"):
        validate_session_time(SETTINGS, start, start, joining_date=JOINING)


def test_naive_times_are_never_read_as_utc():
    with pytest.raises(InvalidMeetingTimeError, match="no timezone"):
        validate_session_time(SETTINGS, datetime(2026, 11, 2, 9, 0), datetime(2026, 11, 2, 10, 0),
                              joining_date=JOINING)


def test_invalid_timezone_fails_safely(port, calendar, hitl):
    settings = {**SETTINGS, "timezone": "Mars/Olympus_Mons"}
    with pytest.raises(InvalidMeetingTimeError, match="not a valid IANA timezone"):
        book(port, calendar, hitl, "EMP-001", datetime(2026, 11, 2, 9, 0, tzinfo=DUBAI), settings=settings)
    assert_nothing_written(port, calendar, hitl, "EMP-001")


# ------------------------------------------------------------------- timezone and UTC

def test_kolkata_local_time_is_stored_as_the_correct_utc_instant(port, calendar, hitl):
    settings = {**SETTINGS, "timezone": "Asia/Kolkata", "weekend_weekdays": [6]}
    joining = date(2026, 11, 16)  # CLIENT_INDIA's live case: a Monday
    hire(port, "EMP-INDIA", joining)
    start = slot(calendar, settings, joining, now=NOW)
    assert start == datetime(2026, 11, 16, 9, 0, tzinfo=KOLKATA)
    local_ten = datetime(2026, 11, 16, 10, 0, tzinfo=KOLKATA)
    session, _ = book(port, calendar, hitl, "EMP-INDIA", local_ten, settings=settings)
    assert session.scheduled_start.astimezone(timezone.utc) == datetime(2026, 11, 16, 4, 30, tzinfo=timezone.utc)
    assert session.scheduled_end - session.scheduled_start == timedelta(minutes=60)  # duration preserved


def test_a_utc_time_that_is_the_next_local_day_is_judged_by_the_local_date(port, calendar, hitl):
    """20:16Z on 4 Nov is 00:16 on 5 Nov in Dubai: outside the window AND outside office hours."""
    with pytest.raises(InvalidMeetingTimeError):
        book(port, calendar, hitl, "EMP-001", datetime(2026, 11, 4, 20, 16, tzinfo=timezone.utc))
    assert_nothing_written(port, calendar, hitl, "EMP-001")


def test_new_york_dst_offset_is_converted_correctly(port, calendar, hitl):
    settings = {**SETTINGS, "timezone": "America/New_York", "weekend_weekdays": [5, 6]}
    hire(port, "EMP-NY", date(2026, 11, 2))  # the first Monday after the US DST change (UTC-5)
    session, _ = book(port, calendar, hitl, "EMP-NY", datetime(2026, 11, 2, 9, 0, tzinfo=NEW_YORK), settings=settings)
    assert session.scheduled_start.astimezone(timezone.utc) == datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------- idempotency, rescheduling, revisions

def test_duplicate_scheduling_request_does_not_create_a_second_session(port, calendar, hitl):
    start = datetime(2026, 11, 2, 9, 0, tzinfo=DUBAI)
    book(port, calendar, hitl, "EMP-001", start)
    with pytest.raises(Exception):  # the slot is taken by the first booking (calendar conflict)
        book(port, calendar, hitl, "EMP-001", start)
    assert len([s for s in port.list_sessions("EMP-001") if s.status != SessionStatus.CANCELLED]) == 1


def test_rescheduling_is_revalidated_against_the_latest_joining_date(port, calendar, hitl):
    audit = AuditLog()
    session, _ = book(port, calendar, hitl, "EMP-001", datetime(2026, 11, 2, 9, 0, tzinfo=DUBAI), audit=audit)
    record_response(port, hitl, audit, session_id=session.id, participant_id="HM-01",
                    response=ResponseStatus.DECLINED,
                    workflow_instance="EMP-001")
    (conflict,) = [i for i in hitl.list() if i.proposal_type == "attendee_declined"]
    outside = {"new_start": datetime(2026, 11, 9, 9, 0, tzinfo=DUBAI).isoformat()}
    with pytest.raises(InvalidDecisionError, match="outside the scheduling window"):
        validate_decision(port, SETTINGS, conflict, decision=Decision.EDITED, approver="hr", edited_payload=outside)
    edited = hitl.decide(conflict.id, Decision.EDITED, approver="hr", approver_roles=["hr_admin"],
                         client_id="CLIENT-ACME", reason_code=ReasonCode.TIMING, edited_payload=outside)
    with pytest.raises(OutsideSchedulingWindowError):
        apply_attendance_decision(port, calendar, hitl, audit, edited, settings=SETTINGS, now=NOW)
    assert port.get_session(session.id).status == SessionStatus.SCHEDULED  # the original is kept


def test_a_hold_drafted_for_an_old_joining_date_is_released_not_confirmed(port, calendar, hitl):
    audit = AuditLog()
    session, item = book(port, calendar, hitl, "EMP-001", datetime(2026, 11, 2, 9, 0, tzinfo=DUBAI), audit=audit)
    port.records["EMP-001"] = port.records["EMP-001"].model_copy(update={"start_date": date(2026, 11, 16)})
    approved = hitl.decide(item.id, Decision.APPROVED, approver="hr", approver_roles=["hr_admin"],
                           client_id="CLIENT-ACME")
    apply_decision(port, calendar, audit, approved, settings=SETTINGS)
    assert port.get_session(session.id).status == SessionStatus.CANCELLED
    assert session.external_event_ref not in calendar._bookings  # the calendar hold is released too
    released = audit.find("scheduling.stale_session_released")[0]
    assert released["joining_date"] == "2026-11-16"


def test_a_hold_inside_the_current_window_is_confirmed_as_before(port, calendar, hitl):
    session, item = book(port, calendar, hitl, "EMP-001", datetime(2026, 11, 3, 9, 0, tzinfo=DUBAI))
    approved = hitl.decide(item.id, Decision.APPROVED, approver="hr", approver_roles=["hr_admin"],
                           client_id="CLIENT-ACME")
    apply_decision(port, calendar, AuditLog(), approved, settings=SETTINGS)
    assert port.get_session(session.id).status == SessionStatus.CONFIRMED
