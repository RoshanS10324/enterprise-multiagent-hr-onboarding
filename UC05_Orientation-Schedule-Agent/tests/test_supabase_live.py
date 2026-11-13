"""OPT-IN live verification against the real Supabase project. Skipped unless ALL of these are set:

  UC05_SUPABASE_LIVE_TESTS=1
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY          (backend credentials; never printed)
  UC05_LIVE_TEST_CLIENT_ID                          an isolated, approved TEST tenant
  UC05_LIVE_TEST_EMPLOYEE_NO                        employee_no of an approved TEST case in that tenant

Read-only by default. The workflow test WRITES (orientation_sessions, session_participants,
buddy_assignments, communications, approval_requests, audit_events, agent_runs) only when
UC05_LIVE_TEST_ALLOW_WRITES=1 as well -- use it only on records the database owner approved for
testing. UC05 never creates or edits onboarding_cases; the test case must already exist (UC01).
Audit rows are append-only and are not deleted afterwards.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from sub_agent.db.config import load_supabase_settings
from sub_agent.db.factory import build_supabase_stores

REQUIRED = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "UC05_LIVE_TEST_CLIENT_ID", "UC05_LIVE_TEST_EMPLOYEE_NO")
pytestmark = pytest.mark.skipif(
    os.environ.get("UC05_SUPABASE_LIVE_TESTS") != "1" or not all(os.environ.get(v) for v in REQUIRED),
    reason="live Supabase tests are opt-in (UC05_SUPABASE_LIVE_TESTS=1 plus credentials and an approved test case)")


@pytest.fixture(scope="module")
def stores():
    return build_supabase_stores(load_supabase_settings())  # pings and verifies every table/column UC05 uses


@pytest.fixture(scope="module")
def repo(stores):
    return stores.repository.for_tenant(os.environ["UC05_LIVE_TEST_CLIENT_ID"])


def report(title: str, **facts) -> None:
    """What a live test read or wrote, shown with `pytest -v -s`. Ids, dates, statuses and counts only:
    never names, e-mail addresses, message text or credentials."""
    print(f"\n  -- {title}")
    for name, value in facts.items():
        print(f"     {name:<26} {value}")


def test_live_schema_and_credentials(stores):
    clients = stores.repository.list_client_ids()
    assert clients is not None
    report("credentials accepted; every UC05 table and column exists",
           active_clients=", ".join(clients) or "(none)")


def test_live_reads_the_test_case_configuration_and_buddies(repo):
    record = repo.get_onboarding_record(os.environ["UC05_LIVE_TEST_EMPLOYEE_NO"])
    assert record.client_id == repo.tenant and isinstance(record.start_date, date)
    report("onboarding case (onboarding_cases, read-only)", employee_no=record.employee_id,
           client_id=record.client_id, case_id=repo.case_id_for(record.employee_id),
           joining_date=record.start_date, case_status=record.case_status.value,
           department_set=bool(record.department), manager_set=bool(record.team_lead_id),
           confirmed_buddy=record.buddy_id or "(none yet)")
    calendar = repo.get_client_calendar(repo.tenant)
    assert calendar.working_days
    report("client calendar (client_configs + client_holidays + client_config_versions)",
           timezone=calendar.timezone, working_days_1_is_monday=calendar.working_days,
           weekend_python_weekdays=calendar.weekend_weekdays, observed_holidays=len(calendar.holidays),
           holiday_dates=", ".join(d.isoformat() for d in calendar.holidays) or "(none)",
           approved_config_version=calendar.config_version)
    try:
        welcome = repo.get_client_config(repo.tenant)
        report("welcome configuration (settings_payload.uc05_welcome)", languages=welcome.enabled_languages,
               default_language=welcome.default_language, templates=sorted(welcome.templates))
    except KeyError as exc:
        report("welcome configuration (settings_payload.uc05_welcome)", status=f"NOT USABLE: {exc}")
    buddies = repo.list_buddy_candidates(repo.tenant)
    report("eligible buddies (buddy_profiles: active, with capacity)", count=len(buddies),
           buddy_employee_ids=", ".join(b.employee_id for b in buddies) or "(none)",
           same_department_as_case=sum(1 for b in buddies if b.team == record.team))


@pytest.mark.skipif(os.environ.get("UC05_LIVE_TEST_ALLOW_WRITES") != "1",
                    reason="writes to the shared database need UC05_LIVE_TEST_ALLOW_WRITES=1 and approved test records")
def test_live_scheduler_books_inside_the_joining_window_and_round_trips(repo):
    """Uses UC05's own scheduling rules on the LIVE case -- never a hard-coded date:
    joining date from onboarding_cases, timezone/working days/holidays from the client calendar,
    the joining-date window from policy.yaml. Writes one deterministic test row per slot (reused, never
    duplicated), reads it back, then cancels it. Once the window is over it asserts the scheduler
    refuses and writes nothing."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    import yaml

    from sub_agent.db import mapping as m
    from sub_agent.fakes import FakeCalendar
    from sub_agent.models import DuplicateOnboardingCaseError, OnboardingSession, SessionStatus, SessionType
    from sub_agent.services.scheduling import (SESSION_DURATION_MINUTES, NoAvailableSlotError, find_available_slot,
                                               scheduling_window, validate_session_time)

    employee = os.environ["UC05_LIVE_TEST_EMPLOYEE_NO"]
    record = repo.get_onboarding_record(employee)  # the authoritative joining date, read fresh
    policy = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "guardrails", "policy.yaml"),
                                 encoding="utf-8"))
    settings = repo.get_client_calendar(repo.tenant).scheduling_settings(policy["settings"])  # as the registry does
    tz = ZoneInfo(settings["timezone"])
    first, last = scheduling_window(settings, record.start_date)
    now = datetime.now(timezone.utc)
    report("scheduling inputs (live)", joining_date=record.start_date, timezone=settings["timezone"],
           window_local_dates=f"{first} .. {last}", now_local=now.astimezone(tz).isoformat(timespec="minutes"))

    busy = [(employee, s.scheduled_start, s.scheduled_end) for s in repo.list_sessions(employee)
            if s.status != SessionStatus.CANCELLED]  # UC05's live sessions block their slots
    try:
        start = find_available_slot(FakeCalendar(busy=busy), settings, attendees=[employee],
                                    session_type=SessionType.ORIENTATION,
                                    earliest=max(first, now.astimezone(tz).date()), latest=last, not_before=now)
    except NoAvailableSlotError as exc:
        report("no valid slot: the scheduler refuses (nothing written)", reason=str(exc))
        return
    end = start + timedelta(minutes=SESSION_DURATION_MINUTES[SessionType.ORIENTATION])
    validate_session_time(settings, start, end, joining_date=record.start_date, now=now)
    assert first <= start.astimezone(tz).date() <= last

    try:
        repo.create_onboarding_case(record)
        link = "linked now (audit_events UC05_CASE_LINKED written)"
    except DuplicateOnboardingCaseError:
        link = "already linked by an earlier run (nothing written)"
    report("UC05 takes on the case", result=link)

    uc05_id = f"live-test:{employee}:{start.astimezone(timezone.utc).isoformat()}"
    session_id = m.stable_uuid("session", repo.tenant, uc05_id)
    existing = repo.client.select("orientation_sessions", filters=[("session_id", f"eq.{session_id}"),
                                                                   ("client_id", f"eq.{repo.tenant}")])
    if existing:  # this slot's test row from an earlier run: reuse it instead of adding another
        repo.update_session_status(session_id, SessionStatus.SCHEDULED)
        how = "reused this slot's test row from an earlier run"
    else:
        created = repo.create_session(OnboardingSession(
            id=uc05_id, employee_id=employee, client_id=repo.tenant, session_type=SessionType.ORIENTATION,
            scheduled_start=start, scheduled_end=end, attendees=[employee], external_event_ref="live-test"))
        assert created.id == session_id
        how = "created"
    row = repo.client.select("orientation_sessions", filters=[("session_id", f"eq.{session_id}"),
                                                              ("client_id", f"eq.{repo.tenant}")])[0]
    stored_start, stored_end = (datetime.fromisoformat(row["start_utc"].replace("Z", "+00:00")),
                                datetime.fromisoformat(row["end_utc"].replace("Z", "+00:00")))
    report(f"orientation_sessions row ({how})", session_id=session_id, case_id=row["case_id"],
           local_start=start.isoformat(), start_utc=row["start_utc"], end_utc=row["end_utc"],
           db_session_type=row["session_type"], db_status=row["status"],
           uc05_session_type=row["session_metadata"].get("uc05_session_type"),
           uc05_status=row["session_metadata"].get("uc05_status"),
           joining_date_basis=row["session_metadata"].get("joining_date_basis"))
    assert stored_start == start.astimezone(timezone.utc) and stored_end == end.astimezone(timezone.utc)
    assert stored_end > stored_start and row["status"] == "SCHEDULED"
    assert row["session_metadata"]["joining_date_basis"] == record.start_date.isoformat()
    assert repo.get_session(session_id).status == SessionStatus.SCHEDULED

    cancelled = repo.cancel_session(session_id)
    assert cancelled.status == SessionStatus.CANCELLED
    row = repo.client.select("orientation_sessions", columns="status,updated_at",
                             filters=[("session_id", f"eq.{session_id}"), ("client_id", f"eq.{repo.tenant}")])[0]
    assert row["status"] == "CANCELLED"
    report("same row after cancel_session", db_status=row["status"], updated_at=row["updated_at"],
           uc05_reads_back=cancelled.status.value)
