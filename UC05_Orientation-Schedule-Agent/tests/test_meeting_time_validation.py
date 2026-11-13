"""UC step 3: a proposed meeting time must fall within configured office hours, on a working day,
in the configured timezone -- checked before the calendar or repository are ever touched.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from context_layer.knowledge.policy_index import PolicyKnowledgeBase
from hr_onboarding_common.contracts import Actor, Purpose
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import HITLInbox
from hr_onboarding_common.platform.policy import PolicyStore
from sub_agent.agent import AGENT_HOME
from sub_agent.fakes import build_mock_onboarding, FakeCalendar, offline_llm
from sub_agent.models import SessionType
from sub_agent.services.scheduling import InvalidMeetingTimeError, schedule_session, validate_meeting_time
from sub_agent.tool_registry import OnboardingToolRegistry

DUBAI = ZoneInfo("Asia/Dubai")
CUSTOM_SETTINGS = {"timezone": "Asia/Dubai", "office_hours": {"start": "09:00", "end": "18:00"},
                   "weekend_weekdays": [4, 5], "holidays": ["2026-12-02"]}


def _next_weekday(start: datetime, target_weekdays: tuple[int, ...]) -> datetime:
    d = start
    while d.weekday() not in target_weekdays:
        d += timedelta(days=1)
    return d


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


# --------------------------------------------------------------------------- validate_meeting_time directly

def test_time_within_office_hours_on_a_working_day_passes():
    start = _next_weekday(datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI), (0, 1, 2, 3))  # Mon-Thu
    validate_meeting_time(CUSTOM_SETTINGS, start, start + timedelta(hours=1))  # no exception


def test_time_before_office_hours_is_rejected():
    start = _next_weekday(datetime(2026, 11, 2, 7, 0, tzinfo=DUBAI), (0, 1, 2, 3))
    with pytest.raises(InvalidMeetingTimeError, match="office hours"):
        validate_meeting_time(CUSTOM_SETTINGS, start, start + timedelta(hours=1))


def test_meeting_ending_after_office_hours_is_rejected():
    start = _next_weekday(datetime(2026, 11, 2, 17, 30, tzinfo=DUBAI), (0, 1, 2, 3))
    with pytest.raises(InvalidMeetingTimeError, match="office hours"):
        validate_meeting_time(CUSTOM_SETTINGS, start, start + timedelta(hours=1))  # ends 18:30


def test_weekend_day_is_rejected():
    weekend_start = _next_weekday(datetime(2026, 11, 2, 10, 0, tzinfo=DUBAI), (4, 5))  # Fri/Sat
    assert weekend_start.weekday() in (4, 5)  # sanity-check the fixture is actually a weekend day
    with pytest.raises(InvalidMeetingTimeError, match="weekend"):
        validate_meeting_time(CUSTOM_SETTINGS, weekend_start, weekend_start + timedelta(hours=1))


def test_configured_holiday_is_rejected():
    holiday = datetime(2026, 12, 2, 10, 0, tzinfo=DUBAI)  # matches CUSTOM_SETTINGS["holidays"]
    with pytest.raises(InvalidMeetingTimeError, match="holiday"):
        validate_meeting_time(CUSTOM_SETTINGS, holiday, holiday + timedelta(hours=1))


def test_validation_is_timezone_aware_not_based_on_the_raw_utc_clock_time():
    # 05:00 UTC is 09:00 Dubai (+4) -- valid in Dubai even though 05:00 looks "too early" in UTC
    start = _next_weekday(datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc), (0, 1, 2, 3))
    validate_meeting_time(CUSTOM_SETTINGS, start, start + timedelta(hours=1))  # no exception

    # 04:00 UTC is 08:00 Dubai -- invalid, before the 09:00 office start
    too_early = _next_weekday(datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc), (0, 1, 2, 3))
    with pytest.raises(InvalidMeetingTimeError):
        validate_meeting_time(CUSTOM_SETTINGS, too_early, too_early + timedelta(hours=1))


# --------------------------------------------------------------------------- enforced inside schedule_session

def test_schedule_session_rejects_an_out_of_hours_time_and_creates_nothing(port, calendar, hitl, audit):
    out_of_hours = _next_weekday(datetime(2026, 11, 2, 20, 0, tzinfo=DUBAI), (0, 1, 2, 3))

    with pytest.raises(InvalidMeetingTimeError):
        schedule_session(port, calendar, hitl, audit, employee_id="EMP-001", session_type=SessionType.ORIENTATION,
                         start=out_of_hours, attendees=["HM-01"], workflow_instance="EMP-001",
                         settings=CUSTOM_SETTINGS)

    assert port.list_sessions("EMP-001") == []
    assert hitl.list(status=None) == []
    assert calendar._bookings == {}  # the calendar was never even consulted


def test_registry_enforces_office_hours_from_the_real_policy_yaml():
    reg = OnboardingToolRegistry(port=build_mock_onboarding(), calendar=FakeCalendar(), kb=PolicyKnowledgeBase(),
                                 llm=offline_llm(), policy=PolicyStore(AGENT_HOME / "guardrails").for_agent(
                                     "employee-onboarding"), hitl=HITLInbox(), audit=AuditLog(),
                                 actor=Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"],
                                            purpose_of_use=Purpose.ONBOARDING, client_id="CLIENT-ACME"))
    out_of_hours = _next_weekday(datetime(2026, 11, 2, 20, 0, tzinfo=DUBAI), (0, 1, 2, 3))

    with pytest.raises(InvalidMeetingTimeError):
        reg.schedule_session("EMP-001", session_type=SessionType.ORIENTATION, start=out_of_hours,
                             attendees=["HM-01"], workflow_instance="EMP-001")
    assert reg.port.list_sessions("EMP-001") == []
