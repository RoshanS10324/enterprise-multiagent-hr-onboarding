"""AC-07 (Framework section 12): "Due dates respect lead times, items allowed after joining, and the working calendar in 100% of plans."

What the sources support, and what these tests pin down:
- Lead times and the working calendar are fully specified by configuration: due date = joining date minus the configured lead time in
  WORKING days (weekends and holidays from the configured calendar). Boundaries are tested below.
- "Items allowed after joining": the Framework allows this only for DOCUMENTS (EC-08 degree attestation; the client example
  "collect education certificates only after day one"). For IT, equipment and access it says the opposite: "Must all be ready by day one"
  and requests are raised "in time for day one". No source defines any equipment or access item that may complete after joining, so
  UC-04 does not model one. Open items at or after the joining date are flagged, never allowed. The business owner's answer is
  recorded in specs/UC04_SPEC.md section 12; nothing is invented here."""

from datetime import date, timedelta

import pytest

from helpers import ALL, approvals, codes, item, make
from sub_agent.calendar import is_working_day, next_working_day, subtract_working_days
from sub_agent.contracts import WorkCalendar
from sub_agent.states import CaseStatus

ALPHA = WorkCalendar(weekend_days=[5, 6], holidays=[date(2026, 12, 2), date(2026, 12, 3)])   # the synthetic client_alpha calendar


def lead(equipment: int, access: int, site: int = 15) -> dict:
    return {"lead_times": {"state": "VERIFIED", "value": {"EQUIPMENT": equipment, "ACCESS": access, "CLIENT_SITE_CLEARANCE": site}}}


def access_only(**patch):
    return make(requested_equipment=[], **patch)


# ---------------------------------------------------------------------------------------------------- pure calendar arithmetic
def test_zero_working_days_is_the_same_date():
    assert subtract_working_days(date(2026, 11, 16), 0, ALPHA) == date(2026, 11, 16)


def test_weekends_are_skipped():
    assert subtract_working_days(date(2026, 11, 16), 1, ALPHA) == date(2026, 11, 13)       # Monday -> previous Friday
    assert subtract_working_days(date(2026, 11, 16), 5, ALPHA) == date(2026, 11, 9)


def test_configured_holidays_are_skipped_and_nothing_else():
    assert subtract_working_days(date(2026, 12, 4), 1, ALPHA) == date(2026, 12, 1)           # Dec 3 and Dec 2 are holidays
    assert subtract_working_days(date(2026, 12, 4), 2, ALPHA) == date(2026, 11, 30)
    assert subtract_working_days(date(2026, 12, 4), 1, WorkCalendar(weekend_days=[5, 6])) == date(2026, 12, 3)   # no holiday configured


def test_next_working_day_skips_consecutive_holidays():
    assert next_working_day(date(2026, 12, 2), ALPHA) == date(2026, 12, 4)


def test_a_positive_lead_time_always_lands_on_an_earlier_working_day():
    start = date(2026, 10, 1)
    for offset in range(0, 150):
        joining = start + timedelta(days=offset)
        for n in (1, 2, 5, 10, 30):
            due = subtract_working_days(joining, n, ALPHA)
            assert due < joining and is_working_day(due, ALPHA), (joining, n, due)


def test_a_different_weekend_is_honoured():
    gulf = WorkCalendar(weekend_days=[4, 5])                                                  # Friday and Saturday off
    assert subtract_working_days(date(2026, 11, 15), 1, gulf) == date(2026, 11, 12)           # Sunday -> previous Thursday


# ---------------------------------------------------------------------------------------------------- service-level due dates
def test_due_dates_use_the_configured_lead_times_in_working_days(env):
    r = env.run(make())
    assert item(r, "101").due_date.isoformat() == "2026-11-09" and item(r, "201").due_date.isoformat() == "2026-11-02"
    assert (item(r, "101").lead_time_days, item(r, "201").lead_time_days) == (5, 10)


def test_a_lead_time_of_zero_makes_the_due_date_the_joining_date(env):
    r = env.run(make(config=lead(0, 0)))
    assert {i.due_date.isoformat() for i in r.items} == {"2026-11-16"}


def test_holidays_inside_the_window_shift_the_due_date(env):
    r = env.run(make(joining_date="2026-12-04", config=lead(1, 2)))
    assert item(r, "201").due_date.isoformat() == "2026-12-01" and item(r, "101").due_date.isoformat() == "2026-11-30"


def test_remote_equipment_adds_courier_days_to_the_working_day_count(env):
    country = {"state": "VERIFIED", "value": {"country_code": "AE", "courier_extra_days": 3, "remote_first_day_required": True}}
    r = env.run(make(work_mode="REMOTE", config={"country_rules": country}))
    assert item(r, "201").due_date.isoformat() == "2026-10-28" and item(r, "101").due_date.isoformat() == "2026-11-09"   # access unaffected


def test_no_due_date_is_ever_after_the_joining_date(env):
    for joining in ("2026-11-16", "2026-12-04", "2027-01-04"):
        r = env.run(make(joining_date=joining), idempotency_key=f"j-{joining}")
        assert all(i.due_date <= date.fromisoformat(joining) for i in r.items)


# ---------------------------------------------------------------------------------------------------- configuration boundaries
@pytest.mark.parametrize("days,expected_gap", [(365, None), (366, "CONFIG_INVALID:lead_times.ACCESS"), (-1, "CONFIG_INVALID:lead_times.ACCESS")])
def test_lead_time_range_boundaries(env, days, expected_gap):
    r = env.run(access_only(config=lead(10, days)))
    assert ("CONFIG_INVALID:lead_times.ACCESS" in codes(r.blockers)) == bool(expected_gap)


def test_a_missing_lead_time_for_a_requested_kind_blocks_only_that_kind(env):
    only_equipment = {"lead_times": {"state": "VERIFIED", "value": {"EQUIPMENT": 10}}}
    r = env.run(make(config=only_equipment))
    assert "CONFIG_MISSING:lead_times.ACCESS" in codes(r.blockers) and item(r, "201").due_date is not None and item(r, "101").due_date is None


def test_a_missing_or_empty_calendar_never_falls_back_to_a_default(env):
    r = env.run(make(config={"work_calendar": {"state": "MISSING"}}))
    assert "CONFIG_MISSING:work_calendar" in codes(r.blockers) and r.overall_status == CaseStatus.BLOCKED
    assert all(i.due_date is None for i in r.items)


# ---------------------------------------------------------------------------------------------------- timing risks at the boundaries
def test_an_approval_is_not_stalled_on_its_due_date_but_is_the_day_after(env):
    on_due = env.run(access_only(as_of_date="2026-11-09"), idempotency_key="a")            # access due 2026-11-09
    after = env.run(access_only(as_of_date="2026-11-10"), idempotency_key="b")
    assert "APPROVAL_STALLED" not in {x.code for x in on_due.risks} and "APPROVAL_STALLED" in {x.code for x in after.risks}


def test_open_items_are_flagged_from_the_joining_date_and_not_before(env):
    day_before = env.run(access_only(as_of_date="2026-11-15"), idempotency_key="a")
    on_day = env.run(access_only(as_of_date="2026-11-16"), idempotency_key="b")
    assert "JOINING_DATE_REACHED_INCOMPLETE" not in {x.code for x in day_before.risks}
    flagged = next(x for x in on_day.risks if x.code == "JOINING_DATE_REACHED_INCOMPLETE")
    assert flagged.owner == "HR_OFFICER" and on_day.overall_status == CaseStatus.AT_RISK


def test_no_source_allows_post_joining_completion_so_an_open_item_after_joining_is_a_risk_not_an_allowance(env):
    r = env.run(make(approval_records=approvals(ALL), as_of_date="2026-11-20"))             # four days after joining, nothing complete
    assert r.overall_status == CaseStatus.AT_RISK and "JOINING_DATE_REACHED_INCOMPLETE" in {x.code for x in r.risks}
    assert not any(i.completed for i in r.items) and r.overall_status != CaseStatus.READY


def test_a_joining_date_on_consecutive_holidays_suggests_the_next_working_day_only(env):
    r = env.run(make(joining_date="2026-12-02"))
    assert "JOINING_DATE_NON_WORKING_DAY" in {x.code for x in r.risks}
    suggestion = next(p for p in r.proposals if p.proposal_type == "NEXT_WORKING_DAY_SUGGESTION")
    assert "2026-12-04" in suggestion.description and suggestion.executed is False
