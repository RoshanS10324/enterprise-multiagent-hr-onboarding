"""Joining-day scheduler for orchestrated cases: finds the cases whose joining day has come and
sends each one the orchestrator's joining-day event (OnboardingOrchestratorAgent.trigger_joining_day),
which runs UC05's own joining-day workflow for that employee -- no welcome logic lives here.

Why a second scheduler. UC05 has one (sub_agent/scheduler.py): per (tenant, local date) it drafts
a welcome for every active UC05 case starting that day. It runs inside UC05's process, sees UC05's
records only, and has no notion of the orchestrator's day-1 readiness signoff, so it would draft
welcomes the orchestrator has not cleared. This scheduler follows the same design -- a job store
with atomic claims, leases, bounded attempts, a catch-up window and a local "due from" time -- per
orchestrated case instead. In an orchestrated deployment do not run UC05's own scheduler against
the UC05 worker's data directory; this one replaces it.

Eligibility (each case, each tick; a skipped case is re-evaluated on the next tick):
  - the case is not complete, not failed, and its welcome is not already complete;
  - its welcome is not UNCERTAIN (a delivery with an unknown outcome is never re-triggered; an
    operator resolves it -- see the welcome-delivery operations);
  - it has a joining date (the case's authoritative date, joining_date.py), and UC05 does not hold a
    different start date for it (until a date change is reconciled: "joining_date_unreconciled");
  - the joining date has come in the employee's timezone: the case's own (OnboardingCaseInput.
    timezone) when given, else its tenant's (config/tenants.yaml), else HR_AGENT_TZ; due from the
    tenant's joining_day_run_at local time on that date (default 06:00, as UC05's scheduler);
  - it is not more than catch_up_days past the joining date (a late case is still picked up within
    the window -- after a missed run, a restart, or a signoff given after the joining day);
  - day-1 readiness is signed off (until then: "awaiting_day_one_signoff").

Restart safety and duplicates. One job per (case, joining date) in the job store. A claim is atomic
(SQLite: BEGIN IMMEDIATE); DONE jobs never run again; a RUNNING job whose lease expired (its runner
died) is reclaimed; FAILED jobs are retried on later ticks up to max_attempts. Beneath that,
trigger_joining_day is idempotent (the event is recorded once) and UC05 drafts at most one welcome
per employee and delivers it once through its ledger, so even a repeated run sends nothing twice.

Job outcome after the trigger, from the welcome stage: complete / awaiting_approval -> DONE;
uncertain -> DONE "needs_review" (not retried); blocked and retryable (e.g. the tenant's welcome
configuration is missing, UC05 unavailable) -> FAILED (retried); anything else -> DONE
"needs_attention" (an HR action is required, not a retry).

Runtime. Nothing starts this automatically. `python -m hr_onboarding_common.orchestration.
joining_day_scheduler --once` runs one tick (for cron / Windows Task Scheduler) against a persistent
orchestrator data directory; `JoiningDayScheduler.start_background()` runs it in-process next to
the orchestrator API, which is the arrangement to prefer: the UC workers' in-memory state is per
process, so a separate scheduler process sees the same cases only through persistent stores.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import httpx

from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.observability.logging import get_logger
from hr_onboarding_common.orchestration.joining_date import uc05_conflict
from hr_onboarding_common.orchestration.state import (
    OnboardingCaseState,
    OverallStatus,
    Stage,
    StageStatus,
)

log = get_logger("orchestration.joining_day_scheduler")
SYSTEM_ACTOR_ID = "system:orchestrator-joining-day-scheduler"
SYSTEM_ROLE = "onboarding_scheduler"  # policy.yaml settings.joining_day_trigger_roles


class JobStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class JoiningDayJob:
    case_id: str
    run_date: str  # the case's joining date (ISO)
    client_id: str
    status: JobStatus
    attempts: int = 0
    lease_until: str | None = None
    last_error: str | None = None
    outcome: str | None = None
    updated_at: str | None = None


class JoiningDayJobStore(Protocol):
    def get(self, case_id: str, run_date: str) -> JoiningDayJob | None: ...
    def claim(self, case_id: str, run_date: str, *, client_id: str, now: datetime, lease: timedelta,
              max_attempts: int) -> JoiningDayJob | None: ...
    def complete(self, case_id: str, run_date: str, *, now: datetime, outcome: str) -> None: ...
    def fail(self, case_id: str, run_date: str, *, now: datetime, error: str) -> None: ...


def _claimable(job: JoiningDayJob | None, now: datetime, max_attempts: int) -> bool:
    if job is None:
        return True
    if job.status == JobStatus.DONE or job.attempts >= max_attempts:
        return False
    return not (job.status == JobStatus.RUNNING and job.lease_until and datetime.fromisoformat(job.lease_until) > now)


class InMemoryJoiningDayJobStore:
    """Same claim semantics as the SQLite store; nothing survives a restart (tests, development)."""

    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], JoiningDayJob] = {}
        self._lock = threading.Lock()

    def get(self, case_id: str, run_date: str) -> JoiningDayJob | None:
        return self.jobs.get((case_id, run_date))

    def claim(self, case_id, run_date, *, client_id, now, lease, max_attempts):
        with self._lock:
            job = self.jobs.get((case_id, run_date))
            if not _claimable(job, now, max_attempts):
                return None
            claimed = JoiningDayJob(case_id=case_id, run_date=run_date, client_id=client_id, status=JobStatus.RUNNING,
                                    attempts=(job.attempts if job else 0) + 1, lease_until=(now + lease).isoformat(),
                                    last_error=job.last_error if job else None, updated_at=now.isoformat())
            self.jobs[(case_id, run_date)] = claimed
            return claimed

    def complete(self, case_id, run_date, *, now, outcome):
        with self._lock:
            self.jobs[(case_id, run_date)] = replace(self.jobs[(case_id, run_date)], status=JobStatus.DONE,
                                                     lease_until=None, outcome=outcome, updated_at=now.isoformat())

    def fail(self, case_id, run_date, *, now, error):
        with self._lock:
            self.jobs[(case_id, run_date)] = replace(self.jobs[(case_id, run_date)], status=JobStatus.FAILED,
                                                     lease_until=None, last_error=error[:500],
                                                     updated_at=now.isoformat())


class SqliteJoiningDayJobStore:
    """Jobs in one SQLite file. A claim is one IMMEDIATE transaction, so two scheduler processes on
    the same file never run the same job at once. Single host, like every SQLite store here."""

    def __init__(self, path: Path | str):
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS joining_day_jobs (case_id TEXT NOT NULL, run_date TEXT NOT NULL, "
                           "client_id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (case_id, run_date))")

    def _read(self, case_id: str, run_date: str) -> JoiningDayJob | None:
        row = self._conn.execute("SELECT body FROM joining_day_jobs WHERE case_id=? AND run_date=?",
                                 (case_id, run_date)).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        return JoiningDayJob(**{**data, "status": JobStatus(data["status"])})

    def _write(self, job: JoiningDayJob) -> None:
        self._conn.execute("INSERT OR REPLACE INTO joining_day_jobs VALUES (?,?,?,?)",
                           (job.case_id, job.run_date, job.client_id, json.dumps(asdict(job), default=str)))

    def get(self, case_id: str, run_date: str) -> JoiningDayJob | None:
        with self._lock:
            return self._read(case_id, run_date)

    def claim(self, case_id, run_date, *, client_id, now, lease, max_attempts):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                job = self._read(case_id, run_date)
                if not _claimable(job, now, max_attempts):
                    self._conn.execute("COMMIT")
                    return None
                claimed = JoiningDayJob(case_id=case_id, run_date=run_date, client_id=client_id,
                                        status=JobStatus.RUNNING, attempts=(job.attempts if job else 0) + 1,
                                        lease_until=(now + lease).isoformat(),
                                        last_error=job.last_error if job else None, updated_at=now.isoformat())
                self._write(claimed)
                self._conn.execute("COMMIT")
                return claimed
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _finish(self, case_id: str, run_date: str, **changes: Any) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            job = self._read(case_id, run_date)
            self._write(replace(job, lease_until=None, **changes))
            self._conn.execute("COMMIT")

    def complete(self, case_id, run_date, *, now, outcome):
        self._finish(case_id, run_date, status=JobStatus.DONE, outcome=outcome, updated_at=now.isoformat())

    def fail(self, case_id, run_date, *, now, error):
        self._finish(case_id, run_date, status=JobStatus.FAILED, last_error=error[:500], updated_at=now.isoformat())

    def close(self) -> None:
        self._conn.close()


class JobStoreUnavailable(RuntimeError):
    """The Supabase job store could not be read or written: the tick skips the case and retries on the next one."""


# JobStatus -> joining_day_jobs.status (its CHECK accepts PENDING, RUNNING, COMPLETED, FAILED, SKIPPED; verified with
# zero-write probes on 2026-11-06).
_DB_STATUS = {JobStatus.RUNNING: "RUNNING", JobStatus.DONE: "COMPLETED", JobStatus.FAILED: "FAILED"}
_FROM_DB = {v: k for k, v in _DB_STATUS.items()}
JOB_KIND = "orchestrator_case_joining_day"


class SupabaseJoiningDayJobStore:
    """The jobs in the shared `joining_day_jobs` table: one row per (case, joining date), with a job id derived from
    both (a retried first claim lands on the same primary key). Tenant-scoped (`client_id`, a foreign key to
    `client_configs`); executed_cases_count is 1 once done. The claim is a compare-and-swap on the job's own version
    (`job_payload->>version`): an insert for a new job, a conditional update otherwise -- two schedulers never run the
    same job at once, wherever they run. The orchestrator's scoped settings (ORCHESTRATOR_SUPABASE_*) are used."""

    TABLE = "joining_day_jobs"

    def __init__(self, *, url: str, service_role_key: str, schema: str = "public",
                 transport: httpx.BaseTransport | None = None, timeout_s: float = 15.0):
        if not url or not service_role_key:
            raise ValueError("the Supabase joining-day job store needs ORCHESTRATOR_SUPABASE_URL and "
                             "ORCHESTRATOR_SUPABASE_SERVICE_ROLE_KEY (or the plain SUPABASE_* names)")
        self._client = httpx.Client(base_url=url.rstrip("/") + "/rest/v1", timeout=timeout_s, transport=transport,
                                    headers={"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}",
                                             "Accept-Profile": schema, "Content-Profile": schema})

    @classmethod
    def from_env(cls, environ: Mapping[str, str], *, transport: httpx.BaseTransport | None = None):
        def setting(name: str) -> str:
            return (environ.get(f"ORCHESTRATOR_{name}") or environ.get(name) or "").strip()

        return cls(url=setting("SUPABASE_URL"), service_role_key=setting("SUPABASE_SERVICE_ROLE_KEY"),
                   schema=setting("SUPABASE_SCHEMA") or "public", transport=transport)

    @staticmethod
    def job_id(case_id: str, run_date: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"dact:orchestrator-joining-day:{case_id}:{run_date}"))

    def _send(self, method: str, *, params: dict[str, str] | None = None, body: Any = None,
              prefer: str | None = None) -> httpx.Response:
        headers = {"Prefer": prefer} if prefer else {}
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body, default=str)
        try:
            return self._client.request(method, f"/{self.TABLE}", params=params, headers=headers, content=content)
        except httpx.HTTPError:
            raise JobStoreUnavailable("joining_day_jobs unreachable") from None

    @staticmethod
    def _job(row: dict[str, Any]) -> JoiningDayJob:
        payload = row.get("job_payload") or {}
        return JoiningDayJob(case_id=payload["case_id"], run_date=str(row["run_date"]), client_id=row["client_id"],
                             status=_FROM_DB.get(row.get("status"), JobStatus.FAILED),
                             attempts=int(payload.get("attempts", 0)), lease_until=payload.get("lease_until"),
                             last_error=payload.get("last_error"), outcome=payload.get("outcome"),
                             updated_at=payload.get("updated_at"))

    def _row(self, case_id: str, run_date: str) -> dict[str, Any] | None:
        response = self._send("GET", params={"select": "*", "job_id": f"eq.{self.job_id(case_id, run_date)}"})
        if response.status_code != 200:
            raise JobStoreUnavailable(f"joining_day_jobs read failed (HTTP {response.status_code})")
        rows = response.json() or []
        return rows[0] if rows else None

    def get(self, case_id: str, run_date: str) -> JoiningDayJob | None:
        row = self._row(case_id, run_date)
        return self._job(row) if row else None

    @staticmethod
    def _payload(job: JoiningDayJob, version: int) -> dict[str, Any]:
        return {"kind": JOB_KIND, "case_id": job.case_id, "attempts": job.attempts, "lease_until": job.lease_until,
                "last_error": job.last_error, "outcome": job.outcome, "updated_at": job.updated_at, "version": version}

    def claim(self, case_id, run_date, *, client_id, now, lease, max_attempts):
        row = self._row(case_id, run_date)
        job = self._job(row) if row else None
        if job is not None and job.client_id != client_id:
            raise JobStoreUnavailable("a joining-day job cannot move to another tenant")
        if not _claimable(job, now, max_attempts):
            return None
        claimed = JoiningDayJob(case_id=case_id, run_date=run_date, client_id=client_id, status=JobStatus.RUNNING,
                                attempts=(job.attempts if job else 0) + 1, lease_until=(now + lease).isoformat(),
                                last_error=job.last_error if job else None, updated_at=now.isoformat())
        if row is None:
            response = self._send("POST", body={
                "job_id": self.job_id(case_id, run_date), "client_id": client_id, "run_date": run_date,
                "status": _DB_STATUS[JobStatus.RUNNING], "executed_cases_count": 0,
                "job_payload": self._payload(claimed, 1), "started_at": now.isoformat()}, prefer="return=minimal")
            if response.status_code in (200, 201, 204):
                return claimed
            code = ""
            try:
                code = (response.json() or {}).get("code") or ""
            except ValueError:
                pass
            if code == "23505":
                return None  # another scheduler claimed it first
            raise JobStoreUnavailable(f"joining_day_jobs insert failed (HTTP {response.status_code} {code})".strip())
        version = int((row.get("job_payload") or {}).get("version", 1))
        response = self._send("PATCH", params={"job_id": f"eq.{row['job_id']}", "job_payload->>version": f"eq.{version}"},
                              body={"status": _DB_STATUS[JobStatus.RUNNING], "started_at": now.isoformat(),
                                    "finished_at": None, "job_payload": self._payload(claimed, version + 1)},
                              prefer="return=representation")
        if response.status_code != 200:
            raise JobStoreUnavailable(f"joining_day_jobs claim failed (HTTP {response.status_code})")
        return claimed if response.json() else None  # zero rows: another scheduler moved it first

    def _finish(self, case_id: str, run_date: str, now: datetime, **changes: Any) -> None:
        row = self._row(case_id, run_date)
        if row is None:
            raise JobStoreUnavailable("the joining-day job to finish is not stored")
        job = replace(self._job(row), lease_until=None, updated_at=now.isoformat(), **changes)
        version = int((row.get("job_payload") or {}).get("version", 1))
        response = self._send("PATCH", params={"job_id": f"eq.{row['job_id']}"},
                              body={"status": _DB_STATUS[job.status], "finished_at": now.isoformat(),
                                    "executed_cases_count": 1 if job.status == JobStatus.DONE else 0,
                                    "job_payload": self._payload(job, version + 1)}, prefer="return=minimal")
        if response.status_code not in (200, 204):
            raise JobStoreUnavailable(f"joining_day_jobs update failed (HTTP {response.status_code})")

    def complete(self, case_id, run_date, *, now, outcome):
        self._finish(case_id, run_date, now, status=JobStatus.DONE, outcome=outcome)

    def fail(self, case_id, run_date, *, now, error):
        self._finish(case_id, run_date, now, status=JobStatus.FAILED, last_error=error[:500])


def build_job_store(environ: Mapping[str, str], persist_dir: Path) -> JoiningDayJobStore:
    """ORCHESTRATOR_JOINING_DAY_STORE: sqlite (persist_dir/joining_day_jobs.db) or supabase (joining_day_jobs). Default:
    supabase when the case state is in Supabase (ORCHESTRATOR_STATE_STORE=supabase), sqlite otherwise. Never a silent
    fallback: a Supabase store without its settings refuses to start."""
    store = (environ.get("ORCHESTRATOR_JOINING_DAY_STORE") or "").strip().lower()
    store = store or ("supabase" if (environ.get("ORCHESTRATOR_STATE_STORE") or "").strip().lower() == "supabase"
                      else "sqlite")
    if store == "supabase":
        return SupabaseJoiningDayJobStore.from_env(environ)
    if store == "sqlite":
        return SqliteJoiningDayJobStore(persist_dir / "joining_day_jobs.db")
    raise ValueError(f"ORCHESTRATOR_JOINING_DAY_STORE={store!r} is not one of sqlite, supabase")


@dataclass(frozen=True)
class JoiningDaySchedulerSettings:
    catch_up_days: int = 2  # as UC05's SCHEDULER_CATCH_UP_DAYS
    lease: timedelta = timedelta(minutes=15)
    max_attempts: int = 5
    interval: timedelta = timedelta(minutes=5)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> JoiningDaySchedulerSettings:
        env = os.environ if environ is None else environ
        return cls(catch_up_days=int(env.get("ORCH_SCHEDULER_CATCH_UP_DAYS", "2")),
                   lease=timedelta(seconds=int(env.get("ORCH_SCHEDULER_LEASE_S", "900"))),
                   max_attempts=int(env.get("ORCH_SCHEDULER_MAX_ATTEMPTS", "5")),
                   interval=timedelta(seconds=int(env.get("ORCH_SCHEDULER_INTERVAL_S", "300"))))


@dataclass
class JoiningDayScheduler:
    agent: Any  # OnboardingOrchestratorAgent
    jobs: JoiningDayJobStore
    clock: Callable[[], datetime] = now_utc
    settings: JoiningDaySchedulerSettings = field(default_factory=JoiningDaySchedulerSettings)

    def eligibility(self, state: OnboardingCaseState, now: datetime) -> dict[str, Any]:
        """{"status": "due" | <skip reason>, ...} for one case at `now` -- see the module docstring."""
        statuses = state["stage_statuses"]
        base = {"case_id": state["case_id"], "client_id": state["client_id"]}
        welcome = statuses.get(Stage.JOINING_DAY_WELCOME.value)
        if statuses.get(Stage.COMPLETE.value) == StageStatus.COMPLETE.value or welcome == StageStatus.COMPLETE.value:
            return {**base, "status": "already_complete"}
        if state.get("overall_status") == OverallStatus.FAILED.value:
            return {**base, "status": "case_failed"}
        if welcome == StageStatus.UNCERTAIN.value:
            return {**base, "status": "uncertain_needs_review"}
        joining = state.get("joining_date")
        if not joining:
            return {**base, "status": "missing_joining_date"}
        if uc05_conflict(state):
            return {**base, "status": "joining_date_unreconciled"}
        joining_date = date.fromisoformat(str(joining)[:10])
        tenants = self.agent.deps.tenants
        tz = tenants.timezone_for(state["client_id"], state.get("case_timezone"))
        local = now.astimezone(tz)
        due_from = datetime.combine(joining_date, tenants.run_at_for(state["client_id"]), tzinfo=tz)
        base.update(joining_date=joining_date.isoformat(), timezone=str(tz.key), local_date=local.date().isoformat())
        if local < due_from:
            return {**base, "status": "not_due", "due_from": due_from.isoformat()}
        if (local.date() - joining_date).days > self.settings.catch_up_days:
            return {**base, "status": "outside_catch_up_window"}
        if statuses.get(Stage.DAY_ONE_READINESS.value) != StageStatus.COMPLETE.value:
            return {**base, "status": "awaiting_day_one_signoff"}
        if state.get("joining_day_event") and welcome == StageStatus.AWAITING_APPROVAL.value:
            return {**base, "status": "awaiting_welcome_approval"}
        return {**base, "status": "due", "as_of": local.date().isoformat()}

    def tick(self) -> list[dict[str, Any]]:
        """Evaluates every case once; claims and runs each due one. Returns one entry per case."""
        now = self.clock()
        results = []
        for state in self.agent.list_case_states():
            verdict = self.eligibility(state, now)
            if verdict["status"] != "due":
                results.append(verdict)
                continue
            job = self.jobs.claim(state["case_id"], verdict["joining_date"], client_id=state["client_id"], now=now,
                                  lease=self.settings.lease, max_attempts=self.settings.max_attempts)
            if job is None:
                existing = self.jobs.get(state["case_id"], verdict["joining_date"])
                results.append({**verdict, "status": f"job_{existing.status if existing else 'unavailable'}",
                                "attempts": existing.attempts if existing else 0})
                continue
            results.append(self._run(verdict, job))
        return results

    def _run(self, verdict: dict[str, Any], job: JoiningDayJob) -> dict[str, Any]:
        actor = {"on_behalf_of": SYSTEM_ACTOR_ID, "roles": [SYSTEM_ROLE], "client_id": job.client_id}
        result = {**verdict, "attempt": job.attempts}
        try:
            state = self.agent.trigger_joining_day(job.case_id, as_of=date.fromisoformat(verdict["as_of"]), actor=actor)
        except Exception as exc:
            self.jobs.fail(job.case_id, job.run_date, now=self.clock(), error=type(exc).__name__)
            log.warning("scheduler.joining_day_failed", extra={"job": {**result, "error": type(exc).__name__}})
            return {**result, "status": "failed", "error": type(exc).__name__}
        welcome = state["stage_statuses"].get(Stage.JOINING_DAY_WELCOME.value)
        error = (state.get("stage_errors") or {}).get(Stage.JOINING_DAY_WELCOME.value) or {}
        if welcome in (StageStatus.COMPLETE.value, StageStatus.AWAITING_APPROVAL.value):
            outcome, retry = welcome, False
        elif welcome == StageStatus.UNCERTAIN.value:
            outcome, retry = "needs_review", False
        elif welcome == StageStatus.BLOCKED.value and error.get("retryable"):
            outcome, retry = error.get("code", "blocked"), True
        else:
            outcome, retry = f"needs_attention:{error.get('code', welcome)}", False
        if retry:
            self.jobs.fail(job.case_id, job.run_date, now=self.clock(), error=outcome)
            return {**result, "status": "failed", "error": outcome, "welcome": welcome}
        self.jobs.complete(job.case_id, job.run_date, now=self.clock(), outcome=outcome)
        log.info("scheduler.joining_day_done", extra={"job": {**result, "outcome": outcome}})
        return {**result, "status": "done", "outcome": outcome, "welcome": welcome}

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler.tick_failed")
            stop.wait(self.settings.interval.total_seconds())

    def start_background(self) -> tuple[threading.Thread, threading.Event]:
        """Runs in this process (next to the orchestrator API, sharing its UC workers). Set the
        returned event to stop it."""
        stop = threading.Event()
        thread = threading.Thread(target=self.run_forever, args=(stop,), name="joining-day-scheduler", daemon=True)
        thread.start()
        return thread, stop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hr_onboarding_common.orchestration.joining_day_scheduler",
                                     description="Send the joining-day event to every due orchestrated case.")
    parser.add_argument("--once", action="store_true", help="run one tick and exit (cron / Task Scheduler)")
    parser.add_argument("--persist-dir", default=os.environ.get("ORCHESTRATOR_DATA_DIR"),
                        help="the orchestrator's data directory (case_states.db, checkpoints.db, hitl.db)")
    parser.add_argument("--uc03-repo-root", default=os.environ.get("UC03_REPO_ROOT"))
    parser.add_argument("--worker-config", default=os.environ.get("ORCHESTRATOR_WORKER_CONFIG"))
    parser.add_argument("--agent-home", default=os.environ.get("ORCHESTRATOR_HOME",
                                                               str(Path(__file__).resolve().parents[3])))
    args = parser.parse_args(argv)
    if not args.persist_dir:
        parser.error("--persist-dir (or ORCHESTRATOR_DATA_DIR) is required: the scheduler reads the cases the "
                     "orchestrator persisted, and its job store must survive restarts")
    from hr_onboarding_common.orchestration.bootstrap import build_deps
    from hr_onboarding_common.orchestration.orchestrator_agent import (
        OnboardingOrchestratorAgent,
    )

    persist_dir = Path(args.persist_dir)
    ctx, deps = build_deps(agent_home=Path(args.agent_home), uc03_repo_root=Path(args.uc03_repo_root or "."),
                           worker_config_path=Path(args.worker_config) if args.worker_config else None,
                           persist_dir=persist_dir)
    try:
        scheduler = JoiningDayScheduler(OnboardingOrchestratorAgent(ctx), build_job_store(os.environ, persist_dir),
                                        settings=JoiningDaySchedulerSettings.from_env())
        if args.once:
            print(json.dumps(scheduler.tick(), indent=1, default=str))
            return 0
        scheduler.run_forever()
        return 0
    finally:
        deps.worker_pool.stop_all()


if __name__ == "__main__":
    raise SystemExit(main())
