"""The joining-day scheduler (Phase 4, finding E): one run per tenant and local date, restart-safe,
retried on failure, catching up on missed dates -- driven by FakeClock, never by wall time."""

import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.platform.hitl import Decision
from sub_agent.agent import AGENT_HOME, OnboardingAgent
from sub_agent.fakes import FakeClock, InMemoryJobStore, build_fake_dependencies, offline_llm
from sub_agent.models import JobStatus
from sub_agent.persistence import SqliteJobStore, SqliteOnboardingRepository
from sub_agent.scheduler import SYSTEM_ACTOR_ID, JoiningDayScheduler, SchedulerSettings

ROOT = Path(__file__).resolve().parents[1]
ACME, GLOBEX = "CLIENT-ACME", "CLIENT-GLOBEX"
# 2026-11-02 07:00 in Dubai (UTC+4): EMP-001 (ACME) joins today
MORNING = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock():
    return FakeClock(MORNING)


@pytest.fixture
def agent(clock):
    ctx = AgentContext.build(agent_home=AGENT_HOME, llm=offline_llm(), connector=build_fake_dependencies(clock=clock))
    return OnboardingAgent(ctx)


def scheduler(agent, jobs, clock, **settings) -> JoiningDayScheduler:
    return JoiningDayScheduler(agent, jobs, clock=clock, settings=SchedulerSettings(catch_up_days=0, **settings))


def welcome_items(agent, client_id=ACME):
    return [i for i in agent.ctx.hitl.list(status=None, client_id=client_id) if i.proposal_type == "welcome_message"]


def test_each_tenant_date_runs_once_and_drafts_for_hr_approval_only(agent, clock):
    jobs = InMemoryJobStore()
    s = scheduler(agent, jobs, clock)

    first = s.tick()
    second = s.tick()

    assert sorted((r["client_id"], r["run_date"], r["status"]) for r in first) == [
        (ACME, "2026-11-02", "done"), (GLOBEX, "2026-11-02", "done")]
    assert second == []
    (item,) = welcome_items(agent)
    assert item.subject_id == "EMP-001" and item.status == Decision.PENDING  # drafted, never sent
    assert agent.deps.notifier.sent_to("EMP-001") == []
    assert welcome_items(agent, GLOBEX) == []  # each run is scoped to its own tenant
    started = [e for e in agent.ctx.audit.find("agent.started") if e.get("actor") == SYSTEM_ACTOR_ID]
    assert {e["client_id"] for e in started} == {ACME, GLOBEX}


def test_a_date_is_not_due_before_the_configured_local_run_time(agent, clock):
    clock.now = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)  # 05:00 Dubai, before 06:00
    results = scheduler(agent, InMemoryJobStore(), clock).tick()
    assert {r["run_date"] for r in results} == {"2026-11-01"}
    assert welcome_items(agent) == []


def test_dates_missed_while_the_scheduler_was_down_are_caught_up(agent, clock):
    clock.now = MORNING + timedelta(days=2)  # down for two days
    s = JoiningDayScheduler(agent, InMemoryJobStore(), clock=clock, settings=SchedulerSettings(catch_up_days=2))

    results = s.tick()

    assert sorted({r["run_date"] for r in results}) == ["2026-11-02", "2026-11-03", "2026-11-04"]
    (item,) = welcome_items(agent)  # EMP-001's 2026-11-02 joining day was not skipped
    assert item.subject_id == "EMP-001"


def test_a_restart_does_not_run_a_completed_date_again(agent, clock, tmp_path):
    SqliteOnboardingRepository(tmp_path / "onboarding.db")  # schema
    scheduler(agent, SqliteJobStore(tmp_path / "onboarding.db"), clock).tick()

    restarted = scheduler(agent, SqliteJobStore(tmp_path / "onboarding.db"), clock)  # a new process
    assert restarted.tick() == []
    assert SqliteJobStore(tmp_path / "onboarding.db").get(ACME, date(2026, 11, 2)).status == JobStatus.DONE
    assert len(welcome_items(agent)) == 1


def test_a_failed_run_is_retried_on_a_later_tick_and_then_completes(agent, clock, monkeypatch):
    jobs = InMemoryJobStore()
    s = scheduler(agent, jobs, clock)
    real = agent.run_event
    calls = {"n": 0}

    def flaky(event, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("database briefly unavailable")
        return real(event, **kwargs)

    monkeypatch.setattr(agent, "run_event", flaky)

    first = s.tick()
    assert [r["status"] for r in first if r["client_id"] == ACME] == ["failed"]
    assert jobs.get(ACME, date(2026, 11, 2)).last_error == "ConnectionError"

    clock.advance(minutes=5)
    second = s.tick()

    assert [(r["client_id"], r["status"], r["attempt"]) for r in second] == [(ACME, "done", 2)]
    assert len(welcome_items(agent)) == 1


def test_a_run_that_keeps_failing_stops_after_max_attempts(agent, clock):
    agent.ctx.policies.override("employee-onboarding", enabled=False)  # kill switch: every run reports an exception
    jobs = InMemoryJobStore()
    s = scheduler(agent, jobs, clock, max_attempts=2)

    s.tick()
    s.tick()
    third = s.tick()

    assert third == []
    job = jobs.get(ACME, date(2026, 11, 2))
    assert job.status == JobStatus.FAILED and job.attempts == 2 and job.last_error == "AGENT_DISABLED"
    assert welcome_items(agent) == []


def test_a_live_lease_held_by_another_worker_is_respected_until_it_expires(agent, clock, tmp_path):
    SqliteOnboardingRepository(tmp_path / "onboarding.db")
    other_worker = SqliteJobStore(tmp_path / "onboarding.db")
    other_worker.claim(ACME, date(2026, 11, 2), now=clock(), lease=timedelta(minutes=15), max_attempts=5)
    s = scheduler(agent, SqliteJobStore(tmp_path / "onboarding.db"), clock, lease=timedelta(minutes=15))

    assert [r["client_id"] for r in s.tick()] == [GLOBEX]  # ACME is someone else's, for now

    clock.advance(minutes=16)  # that worker died; its lease expired
    resumed = s.tick()

    assert [(r["client_id"], r["status"], r["attempt"]) for r in resumed] == [(ACME, "done", 2)]
    assert len(welcome_items(agent)) == 1


def _scheduler_cli(*args, **env):
    full_env = {k: v for k, v in os.environ.items() if not k.startswith(("HR_AGENT_", "IDENTITY_", "SCHEDULER_"))}
    paths = [str(ROOT), str(ROOT / "src")]
    common_src = ROOT.parent / "HR-Onboarding-Common" / "src"
    if common_src.exists():
        paths.append(str(common_src))
    full_env.update(PYTHONPATH=os.pathsep.join(paths), LOG_LEVEL="ERROR", HR_AGENT_ENV="test",
                    **env)
    return subprocess.run([sys.executable, "-m", "sub_agent.scheduler", *args], capture_output=True, text=True,
                          cwd=ROOT, env=full_env, timeout=180)


def test_the_scheduler_process_refuses_to_run_without_persistent_storage():
    out = _scheduler_cli("--once", HR_AGENT_PERSIST="0")
    assert out.returncode == 2 and "needs HR_AGENT_PERSIST=1" in out.stderr


def test_the_scheduler_process_runs_one_tick_and_a_second_process_repeats_nothing(tmp_path):
    first = _scheduler_cli("--once", HR_AGENT_PERSIST="1", HR_AGENT_DATA_DIR=str(tmp_path))
    second = _scheduler_cli("--once", HR_AGENT_PERSIST="1", HR_AGENT_DATA_DIR=str(tmp_path))
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert '"status": "done"' in first.stdout and '"status": "failed"' not in first.stdout
    assert second.stdout.strip() == "[]"
