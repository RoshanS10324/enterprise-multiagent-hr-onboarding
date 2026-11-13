"""Gap 3 -- the joining-day scheduler (orchestration/joining_day_scheduler.py). Deterministic clock
throughout; no background process. Pure eligibility/boundary tests against a stub agent, then the
real orchestrator + real UC05 worker (UC01-UC04 doubles) for triggering, idempotency, tenants,
retries, restarts and uncertain deliveries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from _helpers import UC_ROOTS
from _lifecycle import (
    BUDDY_CANDIDATE,
    CLIENT,
    CLIENT_CONFIG,
    HR,
    World,
    start_uc05_worker,
    status,
)

from hr_onboarding_common.orchestration.joining_day_scheduler import (
    InMemoryJoiningDayJobStore,
    JobStatus,
    JoiningDayScheduler,
    JoiningDaySchedulerSettings,
    SqliteJoiningDayJobStore,
)
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state
from hr_onboarding_common.orchestration.tenant_config import TenantConfig

UTC = timezone.utc
TENANTS = TenantConfig.model_validate({"default": {"timezone": "Asia/Dubai", "joining_day_run_at": "06:00"},
                                       "tenants": {"CLIENT-TEST-B": {"timezone": "America/New_York"}}})
DUBAI_DUE = datetime(2026, 12, 1, 2, 0, tzinfo=UTC)  # 06:00 in Dubai (UTC+4) on the joining day


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


# ------------------------------------------------------------------ pure eligibility (stub agent)


def _case(case_id="C1", client_id=CLIENT, joining="2026-12-01", ready=True, **statuses):
    state = new_state(case_id=case_id, client_id=client_id, correlation_id="c",
                      actor={"on_behalf_of": "u", "roles": ["hr_admin"], "client_id": client_id}, intake_trigger={})
    state["joining_date"] = joining
    if ready:
        state["stage_statuses"][Stage.DAY_ONE_READINESS.value] = StageStatus.COMPLETE.value
    state["stage_statuses"].update(statuses)
    return state


def _stub(*states):
    return SimpleNamespace(deps=SimpleNamespace(tenants=TENANTS), list_case_states=lambda: list(states))


def _verdict(state, now):
    return JoiningDayScheduler(_stub(state), InMemoryJoiningDayJobStore()).eligibility(state, now)["status"]


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 11, 30, 12, 0, tzinfo=UTC), "not_due"),             # the day before
    (datetime(2026, 12, 1, 1, 59, tzinfo=UTC), "not_due"),              # 05:59 Dubai on the day
    (DUBAI_DUE, "due"),                                                  # 06:00 Dubai
    (datetime(2026, 12, 3, 10, 0, tzinfo=UTC), "due"),                   # two days late: within catch-up
    (datetime(2026, 12, 4, 10, 0, tzinfo=UTC), "outside_catch_up_window"),
])
def test_joining_day_boundary_in_the_tenants_timezone(now, expected):
    assert _verdict(_case(), now) == expected


def test_each_tenant_and_each_case_uses_its_own_timezone():
    new_york = _case(client_id="CLIENT-TEST-B")
    assert _verdict(new_york, DUBAI_DUE) == "not_due"  # 21:00 on Nov 30 in New York
    assert _verdict(new_york, datetime(2026, 12, 1, 11, 0, tzinfo=UTC)) == "due"  # 06:00 New York
    own_zone = _case() | {"case_timezone": "Pacific/Kiritimati"}  # UTC+14: the joining day starts earlier
    assert _verdict(own_zone, datetime(2026, 11, 30, 16, 0, tzinfo=UTC)) == "due"


@pytest.mark.parametrize("state, expected", [
    (_case(ready=False), "awaiting_day_one_signoff"),
    (_case(joining=None), "missing_joining_date"),
    # UC05 still holds the start date of before a joining-date change: never triggered on either date.
    (_case() | {"orientation_schedule_result": {"uc05_start_date": "2026-11-24"}}, "joining_date_unreconciled"),
    (_case(joining_day_welcome=StageStatus.UNCERTAIN.value), "uncertain_needs_review"),
    (_case(joining_day_welcome=StageStatus.COMPLETE.value), "already_complete"),
    (_case() | {"overall_status": "failed"}, "case_failed"),
])
def test_ineligible_cases_are_skipped_with_a_reason(state, expected):
    assert _verdict(state, DUBAI_DUE) == expected


def test_a_due_case_is_triggered_once_and_a_failed_job_is_retried_up_to_the_limit():
    triggered = []

    def trigger(case_id, *, as_of, actor):
        triggered.append((case_id, as_of, actor["roles"]))
        raise RuntimeError("boom")

    agent = _stub(_case())
    agent.trigger_joining_day = trigger
    jobs = InMemoryJoiningDayJobStore()
    scheduler = JoiningDayScheduler(agent, jobs, clock=Clock(DUBAI_DUE),
                                    settings=JoiningDaySchedulerSettings(max_attempts=2))
    assert [r["status"] for r in scheduler.tick()] == ["failed"]
    assert [r["status"] for r in scheduler.tick()] == ["failed"]
    assert [r["status"] for r in scheduler.tick()] == ["job_failed"]  # attempts exhausted: not run again
    assert len(triggered) == 2 and triggered[0][2] == ["onboarding_scheduler"]
    assert jobs.get("C1", "2026-12-01").attempts == 2


def test_a_job_whose_runner_died_is_reclaimed_after_its_lease(tmp_path):
    store = SqliteJoiningDayJobStore(tmp_path / "jobs.db")
    lease = timedelta(minutes=15)
    assert store.claim("C1", "2026-12-01", client_id=CLIENT, now=DUBAI_DUE, lease=lease, max_attempts=5)
    store.close()  # the runner "crashes" holding the lease

    reopened = SqliteJoiningDayJobStore(tmp_path / "jobs.db")  # a restarted scheduler
    assert reopened.claim("C1", "2026-12-01", client_id=CLIENT, now=DUBAI_DUE + timedelta(minutes=5), lease=lease,
                          max_attempts=5) is None  # still leased
    job = reopened.claim("C1", "2026-12-01", client_id=CLIENT, now=DUBAI_DUE + timedelta(minutes=16), lease=lease,
                         max_attempts=5)
    assert job.attempts == 2 and job.status == JobStatus.RUNNING
    reopened.complete("C1", "2026-12-01", now=DUBAI_DUE, outcome="awaiting_approval")
    assert reopened.claim("C1", "2026-12-01", client_id=CLIENT, now=DUBAI_DUE + timedelta(days=1), lease=lease,
                          max_attempts=5) is None  # DONE never runs again


# ------------------------------------------------------------------ the real orchestrator and UC05 worker


@pytest.fixture(scope="module")
def uc05():
    worker = start_uc05_worker()
    yield worker
    worker.stop()


@pytest.fixture
def world(uc05):
    w = World(uc05, tenants=TENANTS)
    w.hook("reset")
    w.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    return w


def _scheduler(world, now, jobs=None, **settings):
    return JoiningDayScheduler(world.agent, jobs or InMemoryJoiningDayJobStore(), clock=Clock(now),
                               settings=JoiningDaySchedulerSettings(**settings))


def test_a_due_case_gets_its_welcome_drafted_exactly_once(world):
    state = world.to_welcome_waiting()
    early = _scheduler(world, DUBAI_DUE - timedelta(minutes=1))
    assert [r["status"] for r in early.tick()] == ["not_due"]
    assert world.uc05_snapshot()["welcome_drafts"] == []  # never early

    jobs = InMemoryJoiningDayJobStore()
    scheduler = _scheduler(world, DUBAI_DUE, jobs)
    (run,) = scheduler.tick()
    assert run["status"] == "done" and run["outcome"] == StageStatus.AWAITING_APPROVAL.value
    case = world.agent.get_case(state["case_id"], client_id=CLIENT)
    assert case["joining_day_event"]["triggered_by"] == "system:orchestrator-joining-day-scheduler"
    assert case["actor"]["on_behalf_of"] == HR["on_behalf_of"]  # the case keeps its own actor

    for _ in range(3):  # repeated polling
        assert [r["status"] for r in scheduler.tick()] == ["awaiting_welcome_approval"]
    assert len(world.uc05_snapshot()["welcome_drafts"]) == 1 and jobs.get(state["case_id"], "2026-12-01").attempts == 1

    world.decide(case, Stage.JOINING_DAY_WELCOME)
    assert [r["status"] for r in scheduler.tick()] == ["already_complete"]
    assert world.hook("counts")["messages_by_kind"]["welcome"] == 1


def test_a_case_signed_off_after_an_earlier_run_is_picked_up_later(world):
    state = world.to_signoff_pending()
    scheduler = _scheduler(world, DUBAI_DUE)
    assert [r["status"] for r in scheduler.tick()] == ["awaiting_day_one_signoff"]
    assert world.uc05_snapshot()["welcome_drafts"] == []
    world.agent.decide_approval(state["case_id"], world.gate_item(state["case_id"], "day_one_signoff"),
                                decision="approved", actor=HR)
    scheduler.clock = Clock(DUBAI_DUE + timedelta(days=1))  # signed off a day late: still in the window
    (run,) = scheduler.tick()
    assert run["status"] == "done"
    case = world.agent.get_case(state["case_id"], client_id=CLIENT)
    assert status(case, Stage.JOINING_DAY_WELCOME) == StageStatus.AWAITING_APPROVAL.value
    assert case["joining_day_event"]["as_of"] == "2026-12-01"  # UC05 drafts for the actual joining date


def test_a_retryable_failure_is_retried_after_a_restart_until_it_succeeds(world, tmp_path):
    world.hook("remove_client_config", client_id=CLIENT)
    state = world.to_welcome_waiting()
    jobs = SqliteJoiningDayJobStore(tmp_path / "jobs.db")
    (run,) = _scheduler(world, DUBAI_DUE, jobs).tick()
    assert run["status"] == "failed" and run["error"] == "UC05_CLIENT_CONFIG_MISSING"
    jobs.close()

    world.hook("add_client_config", config=CLIENT_CONFIG)  # HR configures the tenant
    restarted = SqliteJoiningDayJobStore(tmp_path / "jobs.db")  # the scheduler process restarted
    (run,) = _scheduler(world, DUBAI_DUE + timedelta(minutes=5), restarted).tick()
    assert run["status"] == "done" and run["attempt"] == 2
    assert status(world.agent.get_case(state["case_id"], client_id=CLIENT),
                  Stage.JOINING_DAY_WELCOME) == StageStatus.AWAITING_APPROVAL.value
    assert len(world.uc05_snapshot()["welcome_drafts"]) == 1


def test_an_uncertain_delivery_is_never_triggered_again(world):
    state = world.to_welcome_pending()
    world.hook("set", target="notifier", attrs={"idempotent_delivery": False, "lose_next_responses": 1})
    state = world.decide(state, Stage.JOINING_DAY_WELCOME)
    assert status(state, Stage.JOINING_DAY_WELCOME) == StageStatus.UNCERTAIN.value
    scheduler = _scheduler(world, DUBAI_DUE)
    for _ in range(2):
        assert [r["status"] for r in scheduler.tick()] == ["uncertain_needs_review"]
    assert world.hook("counts")["messages_by_kind"]["welcome"] == 1


def test_tenants_are_processed_independently_in_their_own_timezones(world):
    a = world.to_welcome_waiting()
    b = _drive_tenant_b_to_welcome_waiting(world)
    scheduler = _scheduler(world, DUBAI_DUE)
    runs = {r["case_id"]: r for r in scheduler.tick()}
    assert runs[a["case_id"]]["status"] == "done"
    assert runs[b["case_id"]]["status"] == "not_due"  # still the evening before in New York
    scheduler.clock = Clock(datetime(2026, 12, 1, 11, 0, tzinfo=UTC))  # 06:00 in New York
    runs = {r["case_id"]: r for r in scheduler.tick()}
    assert runs[b["case_id"]]["status"] == "done" and runs[a["case_id"]]["status"] == "awaiting_welcome_approval"
    for case, client in ((a, CLIENT), (b, "CLIENT-TEST-B")):
        assert status(world.agent.get_case(case["case_id"], client_id=client),
                      Stage.JOINING_DAY_WELCOME) == StageStatus.AWAITING_APPROVAL.value


def _drive_tenant_b_to_welcome_waiting(world):
    """A second tenant's case (UC05's EMP-TEST-010 / EVT-010, team lead TL-TEST-003)."""
    hr_b = {**HR, "client_id": "CLIENT-TEST-B"}
    world.hook("add_buddy_candidate", candidate={"employee_id": "EMP-TEST-060", "full_name": "Sam Lee",
                                                 "client_id": "CLIENT-TEST-B", "team": "Tier 2"})
    world.hook("add_client_config", config={**CLIENT_CONFIG, "client_id": "CLIENT-TEST-B", "name": "Test Client B"})
    world.uc01.employee_id = "EMP-TEST-010"
    try:
        state = world.agent.advance_case(world.case_input(client_id="CLIENT-TEST-B", offer_reference="OFR-B-1",
                                                          offer_acceptance_event_id="EVT-010"), case_id=None, actor=hr_b)
    finally:
        world.uc01.employee_id = "EMP-TEST-001"
    state = world.supply_documents(state, actor=hr_b)  # UC05 runs once the documents are verified
    state = world.decide(state, Stage.ORIENTATION_SCHEDULING, actor=hr_b)
    state = world.decide(state, Stage.BUDDY_ASSIGNMENT, actor=hr_b)
    state = world.decide(state, Stage.TEAM_INTRODUCTION,
                         actor={"on_behalf_of": "TL-TEST-003", "roles": ["team_lead"], "client_id": "CLIENT-TEST-B"})
    (gate,) = [p["reference"] for p in state["pending_approvals"] if p.get("gate") == "day_one_signoff"]
    state = world.agent.decide_approval(state["case_id"], gate, decision="approved", actor=hr_b)
    assert status(state, Stage.JOINING_DAY_WELCOME) == StageStatus.WAITING.value
    return state


def test_the_command_line_entry_point_runs_one_tick(tmp_path, capsys):
    """`python -m hr_onboarding_common.orchestration.joining_day_scheduler --once` against an (empty)
    persistent orchestrator data directory: it starts the workers, ticks once, stops them."""
    from hr_onboarding_common.orchestration.joining_day_scheduler import main

    assert main(["--once", "--persist-dir", str(tmp_path), "--uc03-repo-root", str(UC_ROOTS["uc03"])]) == 0
    assert capsys.readouterr().out.strip() == "[]"
    assert (tmp_path / "joining_day_jobs.db").exists() and (tmp_path / "case_states.db").exists()
