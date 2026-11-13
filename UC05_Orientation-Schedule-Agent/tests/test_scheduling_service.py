"""Phase 7: orientation/induction/team-intro/buddy-intro scheduling against FakeCalendar + the
real HITLInbox approval mechanism.

Covers successful booking, availability conflicts, provider failures, duplicate-booking
prevention, buddy assignment from the authorized candidate source, approval checks, and that a
confirmed event id only ever appears after a real HITL approval.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from connectors.calendar.base import BookingStatus, CalendarBookingError, CalendarConflictError
from connectors.notifications.base import NotificationRejectedError
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import Decision, HITLInbox, ReasonCode
from sub_agent.fakes import FAILING_ATTENDEE, build_mock_onboarding, FakeCalendar, FakeNotifier
from sub_agent.models import (DuplicateSessionError, OnboardingRecord, OnboardingSession, ResponseStatus,
                              SessionStatus, SessionType)
from sub_agent.services.scheduling import (BuddyNotAssignedError, IneligibleBuddyError, InvalidParticipantResponseError,
                                           NoAvailableSlotError, NoBuddyCandidatesError, apply_buddy_decision,
                                           apply_decision, find_available_slot, outstanding_sessions,
                                           participants_for, propose_buddy_assignment, record_response,
                                           schedule_buddy_intro, schedule_session)

ACME = "CLIENT-ACME"
START = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def port():
    return build_mock_onboarding()


@pytest.fixture
def calendar():
    return FakeCalendar()


@pytest.fixture
def hitl():
    return HITLInbox()


@pytest.fixture
def audit():
    return AuditLog()


# --------------------------------------------------------------------------- successful booking

def test_schedule_session_books_and_submits_for_approval(port, calendar, hitl, audit):
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance="EMP-001")

    assert session.status == SessionStatus.SCHEDULED
    assert session.external_event_ref is not None
    assert item.gate == "scheduling_approval" and item.status == Decision.PENDING


def test_confirmed_event_id_only_appears_after_approval(port, calendar, hitl, audit):
    hitl.subscribe(lambda item: apply_decision(port, calendar, audit, item))
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance="EMP-001")

    assert port.get_session(session.id).status == SessionStatus.SCHEDULED  # not yet confirmed
    hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"], client_id=ACME)

    confirmed = port.get_session(session.id)
    assert confirmed.status == SessionStatus.CONFIRMED
    assert calendar.confirm_event  # provider-side confirm happened via apply_decision, checked next:
    booking = calendar._bookings[confirmed.external_event_ref]
    assert booking.status == BookingStatus.CONFIRMED


# --------------------------------------------------------------------------- availability / conflicts

def test_unavailable_attendee_raises_conflict_before_any_session_is_created(port, calendar, hitl, audit):
    busy_calendar = FakeCalendar(busy=[("HM-01", START, START + timedelta(hours=1))])

    with pytest.raises(CalendarConflictError):
        schedule_session(port, busy_calendar, hitl, audit, employee_id="EMP-001",
                         session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                         workflow_instance="EMP-001")

    assert port.list_sessions("EMP-001") == []
    assert hitl.list(status=None) == []


def _same_day_hire(port, employee_id: str = "EMP-SAMEDAY"):
    """A second ACME hire joining on EMP-001's date (2026-11-02): sessions are confined to the
    joining-date window, so two employees only compete for a slot when their windows overlap."""
    port.create_onboarding_case(OnboardingRecord(
        employee_id=employee_id, full_name="Same Day Hire", email="same.day@example.com", role_title="Analyst",
        department="Engineering", team="Platform", team_lead_id="TL-001", client_id="CLIENT-ACME",
        start_date=date(2026, 11, 2)))
    return employee_id


def test_double_booking_the_same_attendee_and_time_conflicts(port, calendar, hitl, audit):
    other = _same_day_hire(port)
    schedule_session(port, calendar, hitl, audit, employee_id="EMP-001", session_type=SessionType.ORIENTATION,
                     start=START, attendees=["HM-01"], workflow_instance="EMP-001")

    with pytest.raises(CalendarConflictError):
        schedule_session(port, calendar, hitl, audit, employee_id=other, session_type=SessionType.ORIENTATION,
                         start=START, attendees=["HM-01"], workflow_instance=other)


# --------------------------------------------------------------------------- provider failures

def test_calendar_provider_failure_leaves_no_session_or_approval(port, calendar, hitl, audit):
    with pytest.raises(CalendarBookingError):
        schedule_session(port, calendar, hitl, audit, employee_id="EMP-001", session_type=SessionType.ORIENTATION,
                         start=START, attendees=[FAILING_ATTENDEE], workflow_instance="EMP-001")

    assert port.list_sessions("EMP-001") == []
    assert hitl.list(status=None) == []
    assert audit.find("scheduling.session_booked") == []


# --------------------------------------------------------------------------- duplicate prevention

def test_rebooking_the_same_attendee_and_slot_is_a_calendar_conflict(port, calendar, hitl, audit):
    """The calendar's own availability check is the first line of defense: the employee is the
    primary attendee of their own session, so a second attempt at the identical slot always
    conflicts before the repository is even consulted."""
    schedule_session(port, calendar, hitl, audit, employee_id="EMP-001", session_type=SessionType.ORIENTATION,
                     start=START, attendees=["HM-01"], workflow_instance="EMP-001")

    with pytest.raises(CalendarConflictError):
        schedule_session(port, calendar, hitl, audit, employee_id="EMP-001", session_type=SessionType.ORIENTATION,
                         start=START, attendees=["HM-02"], workflow_instance="EMP-001")

    assert len(port.list_sessions("EMP-001")) == 1


def test_repository_duplicate_rejects_a_session_the_calendar_did_not_know_about(port, calendar, hitl, audit):
    """A session already recorded in the repository by some other channel (migration, a different
    booking path) that the calendar has never seen: the calendar happily books it, the repository
    then rejects it as a duplicate, and the service releases the now-orphaned calendar hold."""
    end = START + timedelta(minutes=60)
    port.create_session(OnboardingSession(employee_id="EMP-001", client_id=ACME,
                                          session_type=SessionType.ORIENTATION,
                                          scheduled_start=START, scheduled_end=end))

    with pytest.raises(DuplicateSessionError):
        schedule_session(port, calendar, hitl, audit, employee_id="EMP-001", session_type=SessionType.ORIENTATION,
                         start=START, attendees=["HM-01"], workflow_instance="EMP-001")

    assert len(port.list_sessions("EMP-001")) == 1  # the duplicate attempt did not create a second record
    assert calendar._bookings == {}  # the orphaned calendar hold from the failed attempt was released


# --------------------------------------------------------------------------- buddy assignment

def test_propose_buddy_assignment_picks_from_the_authorized_candidate_pool(port, hitl, audit):
    buddy_id, item = propose_buddy_assignment(port, hitl, audit, employee_id="EMP-002",
                                              workflow_instance="EMP-002")  # EMP-002 starts with no buddy

    assert buddy_id == "EMP-010"
    assert item is not None and item.gate == "buddy_assignment" and item.status == Decision.PENDING
    assert port.get_onboarding_record("EMP-002").buddy_id is None  # not assigned until approved
    assert audit.find("scheduling.buddy_assignment_proposed")


def test_propose_buddy_assignment_prefers_a_same_team_candidate(port, hitl, audit):
    port.create_onboarding_case(OnboardingRecord(
        employee_id="EMP-TEST-TEAM", full_name="Casey Doe", email="casey.doe@example.com",
        role_title="Data Engineer", department="Data", team="Data", team_lead_id="TL-002",
        client_id=ACME, start_date=date(2026, 12, 1)))

    buddy_id, item = propose_buddy_assignment(port, hitl, audit, employee_id="EMP-TEST-TEAM",
                                              workflow_instance="EMP-TEST-TEAM")

    assert buddy_id == "EMP-011"  # the "Data" team candidate, not the alphabetically-first EMP-010
    assert item.confidence == 0.9  # higher confidence for a same-team match
    assert "same team" in item.rationale


def test_propose_buddy_assignment_is_idempotent_for_an_already_assigned_employee(port, hitl, audit):
    buddy_id, item = propose_buddy_assignment(port, hitl, audit, employee_id="EMP-001",
                                              workflow_instance="EMP-001")  # EMP-001 already has EMP-010

    assert buddy_id == "EMP-010" and item is None
    assert not audit.find("scheduling.buddy_assignment_proposed")


def test_propose_buddy_assignment_raises_when_client_has_no_candidates(port, hitl, audit):
    port.buddy_candidates = []
    with pytest.raises(NoBuddyCandidatesError):
        propose_buddy_assignment(port, hitl, audit, employee_id="EMP-002", workflow_instance="EMP-002")


def test_approved_buddy_assignment_sets_the_record_and_notifies_both_parties(port, hitl, audit):
    buddy_id, item = propose_buddy_assignment(port, hitl, audit, employee_id="EMP-002",
                                              workflow_instance="EMP-002")
    decided = hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"],
                          client_id=ACME)

    apply_buddy_decision(port, FakeNotifier(), audit, decided)

    assert port.get_onboarding_record("EMP-002").buddy_id == buddy_id
    assert {n.recipient_id for n in port.list_notifications("EMP-002")} == {"EMP-002", buddy_id}
    assert audit.find("scheduling.buddy_notified")


def test_rejected_buddy_assignment_leaves_no_buddy_assigned(port, hitl, audit):
    buddy_id, item = propose_buddy_assignment(port, hitl, audit, employee_id="EMP-002",
                                              workflow_instance="EMP-002")
    decided = hitl.decide(item.id, Decision.REJECTED, approver="hr-admin-01", approver_roles=["hr_admin"],
                          client_id=ACME, reason_code=ReasonCode.NOT_RELEVANT)

    apply_buddy_decision(port, FakeNotifier(), audit, decided)

    assert port.get_onboarding_record("EMP-002").buddy_id is None
    assert audit.find("scheduling.buddy_assignment_rejected")


def test_schedule_buddy_intro_requires_an_already_assigned_buddy(port, calendar, hitl, audit):
    with pytest.raises(BuddyNotAssignedError):
        schedule_buddy_intro(port, calendar, hitl, audit, employee_id="EMP-002", start=START,
                             workflow_instance="EMP-002")
    assert port.list_sessions("EMP-002") == []


def test_schedule_buddy_intro_books_a_session_with_the_approved_buddy(port, calendar, hitl, audit):
    buddy_id, item = propose_buddy_assignment(port, hitl, audit, employee_id="EMP-002",
                                              workflow_instance="EMP-002")
    apply_buddy_decision(port, FakeNotifier(), audit,
                        hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01",
                                   approver_roles=["hr_admin"], client_id=ACME))

    session, _ = schedule_buddy_intro(port, calendar, hitl, audit, employee_id="EMP-002",
                                      start=START + timedelta(days=7),  # EMP-002 joins 2026-11-09
                                      workflow_instance="EMP-002")

    assert session.session_type == SessionType.BUDDY_INTRO
    assert buddy_id in session.attendees


# --------------------------------------------------------------------------- outstanding activities (UC-05 #6)

def test_outstanding_sessions_excludes_confirmed_and_cancelled(port, calendar, hitl, audit):
    hitl.subscribe(lambda item: apply_decision(port, calendar, audit, item))
    booked, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                    session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                    workflow_instance="EMP-001")
    still_pending, _ = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                        session_type=SessionType.INDUCTION, start=START + timedelta(hours=2),
                                        attendees=["HM-01"], workflow_instance="EMP-001")

    assert {s.id for s in outstanding_sessions(port, "EMP-001")} == {booked.id, still_pending.id}

    hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"], client_id=ACME)
    assert {s.id for s in outstanding_sessions(port, "EMP-001")} == {still_pending.id}


def test_outstanding_sessions_is_empty_when_none_booked(port):
    assert outstanding_sessions(port, "EMP-001") == []


# --------------------------------------------------------------------------- approval rejection

def test_rejected_booking_cancels_the_calendar_hold_and_the_session(port, calendar, hitl, audit):
    hitl.subscribe(lambda item: apply_decision(port, calendar, audit, item))
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance="EMP-001")

    hitl.decide(item.id, Decision.REJECTED, approver="hr-admin-01", approver_roles=["hr_admin"],
               client_id=ACME, reason_code=ReasonCode.TIMING)

    assert port.get_session(session.id).status == SessionStatus.CANCELLED
    assert session.external_event_ref not in calendar._bookings  # the tentative hold was released

    # the slot is free again once cancelled (for another hire whose joining-date window includes it)
    other = _same_day_hire(port)
    reschedule, _ = schedule_session(port, calendar, hitl, audit, employee_id=other,
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance=other)
    assert reschedule.status == SessionStatus.SCHEDULED


# --------------------------------------------------------------------------- workflow-drafted meetings

SETTINGS = {"timezone": "Asia/Dubai", "office_hours": {"start": "09:00", "end": "18:00"},
            "weekend_weekdays": [4, 5], "holidays": ["2026-12-02"],
            "workflow": {"session_participants": {"induction": ["team_lead"], "team_intro": ["team_lead", "buddy"]}}}
DUBAI = ZoneInfo("Asia/Dubai")


def test_find_available_slot_takes_the_first_free_in_hours_slot(calendar):
    busy_until = datetime(2026, 12, 1, 10, 0, tzinfo=DUBAI)  # Tuesday: TL busy 09:00-10:00
    calendar = FakeCalendar(busy=[("TL-001", datetime(2026, 12, 1, 9, 0, tzinfo=DUBAI), busy_until)])
    slot = find_available_slot(calendar, SETTINGS, attendees=["EMP-001", "TL-001"],
                               session_type=SessionType.INDUCTION, earliest=date(2026, 12, 1))
    assert slot == busy_until


def test_find_available_slot_skips_weekends_and_holidays(calendar):
    slot = find_available_slot(calendar, SETTINGS, attendees=["EMP-001"], session_type=SessionType.TEAM_INTRO,
                               earliest=date(2026, 12, 2))  # Wed holiday, Thu ok
    assert slot == datetime(2026, 12, 3, 9, 0, tzinfo=DUBAI)
    friday = find_available_slot(calendar, SETTINGS, attendees=["EMP-001"], session_type=SessionType.TEAM_INTRO,
                                 earliest=date(2026, 12, 4))  # Fri + Sat are the UAE weekend
    assert friday == datetime(2026, 12, 6, 9, 0, tzinfo=DUBAI)


def test_find_available_slot_never_ends_after_office_hours():
    day = date(2026, 12, 1)
    calendar = FakeCalendar(busy=[("EMP-001", datetime(2026, 12, 1, 9, 0, tzinfo=DUBAI),
                                   datetime(2026, 12, 1, 16, 45, tzinfo=DUBAI))])
    slot = find_available_slot(calendar, SETTINGS, attendees=["EMP-001"], session_type=SessionType.INDUCTION,
                               earliest=day)  # 90 minutes no longer fits on the 1st
    assert slot == datetime(2026, 12, 3, 9, 0, tzinfo=DUBAI)


def test_find_available_slot_raises_when_the_window_is_full():
    start = datetime(2026, 12, 1, tzinfo=DUBAI)
    calendar = FakeCalendar(busy=[("EMP-001", start, start + timedelta(days=60))])
    with pytest.raises(NoAvailableSlotError):
        find_available_slot(calendar, SETTINGS, attendees=["EMP-001"], session_type=SessionType.INDUCTION,
                            earliest=date(2026, 12, 1))


def test_participants_for_resolves_roles_from_the_record_and_skips_unassigned(port):
    assert participants_for(SETTINGS, port.get_onboarding_record("EMP-001"), SessionType.TEAM_INTRO) == \
        ["TL-001", "EMP-010"]
    assert participants_for(SETTINGS, port.get_onboarding_record("EMP-002"), SessionType.TEAM_INTRO) == \
        ["TL-002"]  # no buddy assigned yet -> left out, never invented
    assert participants_for(SETTINGS, port.get_onboarding_record("EMP-002"), SessionType.ORIENTATION) == []


def test_propose_buddy_assignment_returns_the_pending_proposal_instead_of_a_second_one(port, hitl, audit):
    first_buddy, first = propose_buddy_assignment(port, hitl, audit, employee_id="EMP-002", workflow_instance="EMP-002")
    again_buddy, again = propose_buddy_assignment(port, hitl, audit, employee_id="EMP-002", workflow_instance="EMP-002")

    assert (again_buddy, again.id) == (first_buddy, first.id)
    assert len(hitl.list(status=Decision.PENDING, gate="buddy_assignment")) == 1
    assert audit.find("scheduling.buddy_assignment_already_pending")


def test_buddy_notification_records_the_delivery_and_a_retry_only_sends_what_is_missing(port, hitl, audit):
    _, item = propose_buddy_assignment(port, hitl, audit, employee_id="EMP-002", workflow_instance="EMP-002")
    decided = hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"],
                          client_id=ACME)
    flaky = FakeNotifier(fail_next=0)
    flaky.reject_recipients.add("EMP-002")  # the new hire's notice fails permanently the first time
    with pytest.raises(NotificationRejectedError):
        apply_buddy_decision(port, flaky, audit, decided)
    assert [n.recipient_id for n in port.list_notifications("EMP-002")] == ["EMP-010"]

    flaky.reject_recipients.clear()
    apply_buddy_decision(port, flaky, audit, decided)

    notes = port.list_notifications("EMP-002")
    assert sorted(n.recipient_id for n in notes) == ["EMP-002", "EMP-010"]
    assert all(n.detail["simulated"] is True and n.detail["delivery_ref"] for n in notes)
    assert len(flaky.sent_to("EMP-010")) == 1  # the buddy was not notified twice


# --------------------------------------------------------------------------- forged / stray callbacks

def test_rsvp_from_someone_who_was_not_invited_is_refused(port, calendar, hitl, audit):
    session, _ = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                  session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                  workflow_instance="EMP-001")
    with pytest.raises(InvalidParticipantResponseError):
        record_response(port, hitl, audit, session_id=session.id, participant_id="STRANGER-99",
                        response=ResponseStatus.DECLINED, workflow_instance="EMP-001")
    assert port.list_participant_responses(session.id) == []
    assert not [i for i in hitl.list(status=None) if i.proposal_type == "attendee_declined"]


def test_rsvp_for_a_cancelled_session_is_refused(port, calendar, hitl, audit):
    hitl.subscribe(lambda item: apply_decision(port, calendar, audit, item))
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance="EMP-001")
    hitl.decide(item.id, Decision.REJECTED, approver="hr-admin-01", approver_roles=["hr_admin"], client_id=ACME,
                reason_code=ReasonCode.TIMING)
    with pytest.raises(InvalidParticipantResponseError):
        record_response(port, hitl, audit, session_id=session.id, participant_id="HM-01",
                        response=ResponseStatus.DECLINED, workflow_instance="EMP-001")


def test_hr_edit_cannot_assign_a_buddy_outside_the_clients_authorized_pool(port, hitl, audit):
    _, item = propose_buddy_assignment(port, hitl, audit, employee_id="EMP-002", workflow_instance="EMP-002")
    decided = hitl.decide(item.id, Decision.EDITED, approver="hr-admin-01", approver_roles=["hr_admin"],
                          client_id=ACME, reason_code=ReasonCode.ACCURACY,
                          edited_payload={**item.payload, "buddy_id": "EMP-110"})  # a CLIENT-GLOBEX candidate
    notifier = FakeNotifier()

    with pytest.raises(IneligibleBuddyError):
        apply_buddy_decision(port, notifier, audit, decided)

    assert port.get_onboarding_record("EMP-002").buddy_id is None and notifier.sent == []
    assert audit.find("scheduling.buddy_rejected_ineligible")


def test_hr_edit_may_choose_another_eligible_buddy_from_the_same_pool(port, hitl, audit):
    _, item = propose_buddy_assignment(port, hitl, audit, employee_id="EMP-002", workflow_instance="EMP-002")
    decided = hitl.decide(item.id, Decision.EDITED, approver="hr-admin-01", approver_roles=["hr_admin"],
                          client_id=ACME, reason_code=ReasonCode.ACCURACY,
                          edited_payload={**item.payload, "buddy_id": "EMP-011"})
    apply_buddy_decision(port, FakeNotifier(), audit, decided)
    assert port.get_onboarding_record("EMP-002").buddy_id == "EMP-011"
