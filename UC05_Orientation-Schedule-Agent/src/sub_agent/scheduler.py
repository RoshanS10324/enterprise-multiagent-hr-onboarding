"""Joining-day scheduler: runs the workflow's joining_day event (stage 11 trigger) for every tenant
once per local date, with no human having to call POST /v1/onboarding/workflow/joining-day.

Run it as its own process next to the API, against the same persistent stores:

    HR_AGENT_ENV=staging HR_AGENT_PERSIST=1 python -m sub_agent.scheduler          # loop forever
    HR_AGENT_ENV=staging HR_AGENT_PERSIST=1 python -m sub_agent.scheduler --once   # one tick, then exit (cron)

It is separate from the workflow: it only decides WHEN a (tenant, date) run is due and claims it;
what the run does is OnboardingAgent.run_event(JOINING_DAY), exactly as the API route does.

Restart safety and duplicates. Each (tenant, local date) is one JoiningDayJob in the job store
(SqliteJobStore in onboarding.db). A tick claims a job in one transaction before running it:
  - DONE jobs are never run again, so a restart or a second scheduler process cannot repeat a date;
  - a RUNNING job is skipped while its lease is live; if its worker died, the lease expires and a
    later tick reclaims it, and the run resumes from its workflow checkpoint;
  - a failed run is retried on later ticks, up to SCHEDULER_MAX_ATTEMPTS;
  - each tick looks back SCHEDULER_CATCH_UP_DAYS local days, so dates missed while the scheduler
    was down are still run (late) rather than skipped.
Beneath the job store, the welcome service skips anyone already drafted, so even a repeated run
would not draft a second welcome. A date counts as due from SCHEDULER_RUN_AT local time (policy
settings.timezone). The scheduler acts as a system actor (SYSTEM_ACTOR_ID, role system_scheduler)
scoped to one tenant per run; it drafts welcomes for HR approval and never sends anything itself.

Not covered: an HR user calling the joining-day route manually for the same tenant and date while
the scheduler runs it is serialized only within one process (see agent.py); run the two in the same
process, or leave the joining day to the scheduler.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from hr_onboarding_common.config import load_environment
from hr_onboarding_common.contracts import Actor, Purpose
from hr_onboarding_common.observability.logging import get_logger
from sub_agent.agent import OnboardingAgent, OnboardingWorkflowInput, WorkflowEvent
from sub_agent.models import JoiningDayJob

log = get_logger("scheduler")
SYSTEM_ACTOR_ID = "system:joining-day-scheduler"
SYSTEM_ROLE = "system_scheduler"


class JobStore(Protocol):
    def get(self, client_id: str, run_date: date) -> JoiningDayJob | None: ...
    def claim(self, client_id: str, run_date: date, *, now: datetime, lease: timedelta,
              max_attempts: int) -> JoiningDayJob | None: ...
    def complete(self, client_id: str, run_date: date, *, now: datetime) -> None: ...
    def fail(self, client_id: str, run_date: date, *, now: datetime, error: str) -> None: ...


@dataclass(frozen=True)
class SchedulerSettings:
    run_at: time = time(6, 0)  # local time from which a date's joining-day run is due
    catch_up_days: int = 2  # earlier local dates still run if they were missed
    lease: timedelta = timedelta(minutes=15)
    max_attempts: int = 5
    interval: timedelta = timedelta(minutes=5)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SchedulerSettings":
        env = os.environ if environ is None else environ
        return cls(run_at=time.fromisoformat(env.get("SCHEDULER_RUN_AT", "06:00")),
                   catch_up_days=int(env.get("SCHEDULER_CATCH_UP_DAYS", "2")),
                   lease=timedelta(seconds=int(env.get("SCHEDULER_LEASE_S", "900"))),
                   max_attempts=int(env.get("SCHEDULER_MAX_ATTEMPTS", "5")),
                   interval=timedelta(seconds=int(env.get("SCHEDULER_INTERVAL_S", "300"))))


@dataclass
class JoiningDayScheduler:
    agent: OnboardingAgent
    jobs: JobStore
    clock: Callable[[], datetime]
    settings: SchedulerSettings = field(default_factory=SchedulerSettings)

    def tenants(self) -> list[str]:
        repository = self.agent.deps.repository
        list_client_ids = getattr(repository, "list_client_ids", None)  # supabase mode: active client_configs
        if callable(list_client_ids):
            return list_client_ids()
        return sorted({r.client_id for r in repository.list_onboarding_records()})

    def due_dates(self, now: datetime) -> list[date]:
        """Local dates whose run is due at `now`, oldest first."""
        local = now.astimezone(ZoneInfo(self.agent.policy.settings.get("timezone", "Asia/Dubai")))
        latest = local.date() if local.time() >= self.settings.run_at else local.date() - timedelta(days=1)
        return [latest - timedelta(days=n) for n in range(self.settings.catch_up_days, -1, -1)]

    def tick(self) -> list[dict[str, Any]]:
        """Claims and runs every due (tenant, date) job not already done. Returns what it ran."""
        now = self.clock()
        results = []
        for client_id in self.tenants():
            for run_date in self.due_dates(now):
                job = self.jobs.claim(client_id, run_date, now=now, lease=self.settings.lease,
                                      max_attempts=self.settings.max_attempts)
                if job is not None:
                    results.append(self._run(job))
        return results

    def _run(self, job: JoiningDayJob) -> dict[str, Any]:
        actor = Actor(on_behalf_of=SYSTEM_ACTOR_ID, roles=[SYSTEM_ROLE], purpose_of_use=Purpose.ONBOARDING,
                      client_id=job.client_id)
        result: dict[str, Any] = {"client_id": job.client_id, "run_date": job.run_date.isoformat(),
                                  "attempt": job.attempts}
        try:
            response, summary = self.agent.run_event(
                OnboardingWorkflowInput(event=WorkflowEvent.JOINING_DAY, as_of=job.run_date), actor=actor,
                instance=f"joining-day:{job.run_date.isoformat()}")
            error = ",".join(e.code for e in response.exceptions) or None
            if summary.get("pending_nodes"):
                error = error or "run_incomplete"
        except Exception as e:  # noqa: BLE001 -- any failure is recorded and retried on a later tick
            error = type(e).__name__
            summary = {}
        if error is None:
            self.jobs.complete(job.client_id, job.run_date, now=self.clock())
            log.info("scheduler.joining_day_done", extra={"job": {**result, "stage": summary.get("stage")}})
            return {**result, "status": "done", "stage": summary.get("stage")}
        self.jobs.fail(job.client_id, job.run_date, now=self.clock(), error=error)
        log.warning("scheduler.joining_day_failed", extra={"job": {**result, "error": error}})
        return {**result, "status": "failed", "error": error}

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 -- e.g. the database is briefly locked; try again next tick
                log.exception("scheduler.tick_failed")
            stop.wait(self.settings.interval.total_seconds())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sub_agent.scheduler", description=__doc__.split("\n\n")[0])
    parser.add_argument("--once", action="store_true", help="run one tick and exit (for cron)")
    args = parser.parse_args(argv)
    load_environment()  # the same .env as the API (and the orchestrator), before a local one
    load_dotenv()
    if os.environ.get("HR_AGENT_PERSIST", "0") != "1":
        parser.error("the scheduler needs HR_AGENT_PERSIST=1: its job store and the API's records must be the same "
                     "files, and an in-memory job store cannot prevent a re-run after a restart")
    from sub_agent.runtime import build_context  # deferred: builds the stores from the environment

    ctx, jobs = build_context()
    scheduler = JoiningDayScheduler(OnboardingAgent(ctx), jobs, clock=ctx.connector.clock,
                                    settings=SchedulerSettings.from_env())
    if args.once:
        print(json.dumps(scheduler.tick(), indent=1))
        return 0
    log.info("scheduler.started", extra={"scheduler": {"interval_s": scheduler.settings.interval.total_seconds()}})
    scheduler.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
