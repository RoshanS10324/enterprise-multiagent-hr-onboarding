"""The joining-day scheduler's jobs in the shared `joining_day_jobs` table (SupabaseJoiningDayJobStore), over an
in-process stand-in for PostgREST that enforces the primary key, the status CHECK list, the client foreign key and the
`job_payload->>version` filter. No network, no database.

Proved: the same claim semantics as the SQLite store (one run per case and joining date, leases, bounded attempts),
the compare-and-swap that keeps two schedulers off the same job, the table's own status vocabulary, the tenant, and
that the store survives a restart (a new store on the same table)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest

from hr_onboarding_common.orchestration.joining_day_scheduler import (JobStatus, JobStoreUnavailable,
                                                                      SqliteJoiningDayJobStore,
                                                                      SupabaseJoiningDayJobStore, build_job_store)

NOW = datetime(2026, 12, 14, 6, 0, tzinfo=timezone.utc)
LEASE = timedelta(minutes=15)
CASE, DAY, CLIENT = "62e57066-c75e-46b0-9e15-61b3b2178dbe", "2026-12-14", "CLIENT_DUBAI"
STATUSES = {"PENDING", "RUNNING", "COMPLETED", "FAILED", "SKIPPED"}


class FakeJobsTable:
    def __init__(self, clients=(CLIENT,)):
        self.rows: dict[str, dict] = {}
        self.clients = set(clients)
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down")
        params = dict(parse_qsl(request.url.query.decode()))
        if request.method == "GET":
            job_id = params["job_id"].removeprefix("eq.")
            return httpx.Response(200, json=[dict(self.rows[job_id])] if job_id in self.rows else [])
        body = json.loads(request.content)
        if "status" in body and body["status"] not in STATUSES:
            return httpx.Response(400, json={"code": "23514"})
        if request.method == "POST":
            if body["client_id"] not in self.clients:
                return httpx.Response(409, json={"code": "23503"})
            if body["job_id"] in self.rows:
                return httpx.Response(409, json={"code": "23505"})
            self.rows[body["job_id"]] = dict(body)
            return httpx.Response(201)
        job_id = params["job_id"].removeprefix("eq.")
        row = self.rows.get(job_id)
        version = params.get("job_payload->>version")
        if row is None or (version and str(row["job_payload"]["version"]) != version.removeprefix("eq.")):
            return httpx.Response(200, json=[])
        row.update(body)
        return httpx.Response(200, json=[dict(row)])

    def store(self) -> SupabaseJoiningDayJobStore:
        return SupabaseJoiningDayJobStore(url="https://x.supabase.co", service_role_key="k",
                                          transport=httpx.MockTransport(self.handler))


def test_a_job_runs_once_and_is_recorded_in_the_tables_vocabulary():
    table = FakeJobsTable()
    store = table.store()
    first = store.claim(CASE, DAY, client_id=CLIENT, now=NOW, lease=LEASE, max_attempts=5)
    assert first.status == JobStatus.RUNNING and first.attempts == 1
    assert store.claim(CASE, DAY, client_id=CLIENT, now=NOW, lease=LEASE, max_attempts=5) is None  # leased
    store.complete(CASE, DAY, now=NOW, outcome="complete")
    (row,) = table.rows.values()
    assert row["status"] == "COMPLETED" and row["executed_cases_count"] == 1 and row["client_id"] == CLIENT
    assert row["run_date"] == DAY and row["job_payload"]["case_id"] == CASE and row["finished_at"]
    assert row["job_id"] == SupabaseJoiningDayJobStore.job_id(CASE, DAY)
    assert store.claim(CASE, DAY, client_id=CLIENT, now=NOW + timedelta(hours=1), lease=LEASE, max_attempts=5) is None


def test_a_failed_job_is_retried_after_its_lease_and_bounded_by_attempts():
    table = FakeJobsTable()
    store = table.store()
    for attempt in range(1, 3):
        job = store.claim(CASE, DAY, client_id=CLIENT, now=NOW + timedelta(minutes=attempt), lease=LEASE, max_attempts=2)
        assert job.attempts == attempt
        store.fail(CASE, DAY, now=NOW, error="WELCOME_NOT_DRAFTED")
    assert next(iter(table.rows.values()))["status"] == "FAILED"
    assert store.claim(CASE, DAY, client_id=CLIENT, now=NOW + timedelta(hours=2), lease=LEASE, max_attempts=2) is None


def test_two_schedulers_never_run_the_same_job():
    table = FakeJobsTable()
    a, b = table.store(), table.store()
    assert a.claim(CASE, DAY, client_id=CLIENT, now=NOW, lease=LEASE, max_attempts=5) is not None
    a.fail(CASE, DAY, now=NOW, error="x")
    stale = dict(table.rows[SupabaseJoiningDayJobStore.job_id(CASE, DAY)])
    assert b.claim(CASE, DAY, client_id=CLIENT, now=NOW, lease=LEASE, max_attempts=5) is not None
    # a scheduler that read the row before b's claim loses the compare-and-swap: nothing is written
    original = b._row
    b._row = lambda case_id, run_date: stale
    try:
        assert b.claim(CASE, DAY, client_id=CLIENT, now=NOW, lease=LEASE, max_attempts=5) is None
    finally:
        b._row = original
    assert table.rows[SupabaseJoiningDayJobStore.job_id(CASE, DAY)]["job_payload"]["attempts"] == 2


def test_a_first_claim_race_lands_on_the_same_primary_key():
    table = FakeJobsTable()
    store = table.store()
    original = store._row
    store._row = lambda case_id, run_date: None  # both schedulers saw no row
    try:
        assert store.claim(CASE, DAY, client_id=CLIENT, now=NOW, lease=LEASE, max_attempts=5) is not None
        assert store.claim(CASE, DAY, client_id=CLIENT, now=NOW, lease=LEASE, max_attempts=5) is None
    finally:
        store._row = original
    assert len(table.rows) == 1


def test_jobs_survive_a_restart_and_stay_in_their_tenant():
    table = FakeJobsTable()
    table.store().claim(CASE, DAY, client_id=CLIENT, now=NOW, lease=LEASE, max_attempts=5)
    restarted = table.store()
    assert restarted.get(CASE, DAY).attempts == 1 and restarted.get(CASE, DAY).client_id == CLIENT
    with pytest.raises(JobStoreUnavailable):
        restarted.claim(CASE, DAY, client_id="CLIENT_OTHER", now=NOW + timedelta(hours=1), lease=LEASE, max_attempts=5)


def test_a_tenant_without_a_client_configs_row_and_an_unreachable_database_are_errors_not_runs():
    assert_raises = pytest.raises(JobStoreUnavailable)
    with assert_raises:
        FakeJobsTable(clients=()).store().claim(CASE, DAY, client_id=CLIENT, now=NOW, lease=LEASE, max_attempts=5)
    table = FakeJobsTable()
    table.down = True
    with pytest.raises(JobStoreUnavailable):
        table.store().get(CASE, DAY)


def test_the_store_follows_the_case_state_store_and_never_falls_back(tmp_path: Path):
    assert isinstance(build_job_store({}, tmp_path), SqliteJoiningDayJobStore)
    with pytest.raises(ValueError, match="ORCHESTRATOR_SUPABASE_URL"):
        build_job_store({"ORCHESTRATOR_STATE_STORE": "supabase"}, tmp_path)  # Supabase-authoritative: no SQLite
    assert isinstance(build_job_store({"ORCHESTRATOR_STATE_STORE": "supabase", "ORCHESTRATOR_SUPABASE_URL": "https://x",
                                       "ORCHESTRATOR_SUPABASE_SERVICE_ROLE_KEY": "k"}, tmp_path),
                      SupabaseJoiningDayJobStore)
    with pytest.raises(ValueError):
        build_job_store({"ORCHESTRATOR_JOINING_DAY_STORE": "csv"}, tmp_path)
