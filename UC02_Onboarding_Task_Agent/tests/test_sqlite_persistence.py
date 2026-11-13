"""Local SQLite persistence for UC-02, against REAL database files in a temp directory.

Synthetic data only. Nothing here touches Supabase, PostgreSQL or ``.env``. Process-restart
recovery is covered separately in ``test_restart_recovery.py`` (separate OS processes);
the tests below reopen files but share one process, so they do not count as restart tests.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from onboarding_task_management import persistence
from onboarding_task_management.contracts import (
    Actor,
    AuditEvent,
    DecisionOutcome,
    ExternalOutcome,
    OnboardingStatus,
    Role,
    SourceUseCase,
    StatusUpdate,
    TaskStatus,
)
from onboarding_task_management.errors import (
    DataIsolationError,
    DuplicateOnboardingError,
    InvalidStateError,
)
from onboarding_task_management.persistence import (
    InMemoryTableClient,
    OnboardingStore,
    PersistenceError,
    store_from_env,
)
from onboarding_task_management.sqlite_store import SqliteTableClient
from onboarding_task_management.sub_agent.runtime import build_service
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import (
    CLIENT_ALPHA,
    CLIENT_BETA,
    build_registry,
    make_trigger,
    persona,
)
from tests.support import HR_OPS, HR_OPS_APPROVER, HR_OPS_BETA

OCCURRED = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
UC03 = Actor(actor_id="svc-uc03-TEST-ONLY", role=Role.SYSTEM, client_id=CLIENT_ALPHA)
PRINCIPALS = {SourceUseCase.UC03_DOCUMENT_VALIDATION: frozenset({UC03.actor_id})}


class TempDbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="uc02-sqlite-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = self.dir / "uc02.sqlite3"
        self._open: list[SqliteTableClient] = []
        self.addCleanup(lambda: [c.close() for c in self._open])

    def client(self, path: Path | None = None) -> SqliteTableClient:
        client = SqliteTableClient(path or self.db)
        self._open.append(client)
        return client

    def service(self, client: SqliteTableClient | None = None, **kw) -> OnboardingService:
        client = client or self.client()
        return OnboardingService(
            build_registry(), store=OnboardingStore(client), source_principals=PRINCIPALS, **kw
        )

    @staticmethod
    def failure(oid, update_id="u-fail"):
        return StatusUpdate(
            update_id=update_id, source=SourceUseCase.UC03_DOCUMENT_VALIDATION,
            client_id=CLIENT_ALPHA, onboarding_id=oid, task_id=f"{oid}#collect_documents",
            outcome=ExternalOutcome.INVALID, occurred_at=OCCURRED, blocking_reason="synthetic")


# --------------------------------------------------------------------------- the client
class SqliteTableClientTests(TempDbCase):
    def test_upsert_select_delete_match_the_in_memory_client(self) -> None:
        sql, mem = self.client(), InMemoryTableClient()
        rows = [
            {"onboarding_id": "O1", "client_id": "C1", "employee_id": "E1", "status": "active", "n": 2},
            {"onboarding_id": "O2", "client_id": "C1", "employee_id": "E2", "status": "active", "n": 1},
            {"onboarding_id": "O3", "client_id": "C2", "employee_id": "E3", "status": "cancelled"},
        ]
        for client in (sql, mem):
            client.upsert("onboarding", rows)
            client.upsert("onboarding", [{**rows[0], "n": 9}])  # idempotent by primary key
        for filters, order in (({}, "onboarding_id"), ({"client_id": "C1"}, "n"),
                               ({"client_id": "C1", "employee_id": "E2"}, None), ({"n": 9}, None)):
            self.assertEqual(sql.select("onboarding", filters, order),
                             mem.select("onboarding", filters, order), (filters, order))
        self.assertEqual(sql.row_count("onboarding"), 3)
        self.assertEqual(sql.delete("onboarding", {"client_id": "C2"}), mem.delete("onboarding", {"client_id": "C2"}))
        self.assertEqual(sql.select("onboarding", {}), mem.select("onboarding", {}))

    def test_unknown_table_and_missing_primary_key_are_refused(self) -> None:
        client = self.client()
        with self.assertRaises(PersistenceError):
            client.upsert("nope", [{"a": 1}])
        with self.assertRaises(PersistenceError):
            client.select("nope", {})
        with self.assertRaises(PersistenceError):
            client.upsert("onboarding", [{"client_id": "C"}])

    def test_the_audit_table_is_append_only_and_rejects_personal_data(self) -> None:
        client = self.client()
        event = {"event_id": "AUD-1", "client_id": "C1", "onboarding_id": None, "detail": {}}
        client.upsert("audit_event", [event])
        with self.assertRaises(PersistenceError):
            client.upsert("audit_event", [event])  # repeated id
        with self.assertRaises(PersistenceError):
            client.delete("audit_event", {"event_id": "AUD-1"})
        with self.assertRaises(PersistenceError):
            client.upsert("audit_event", [{**event, "event_id": "AUD-2", "detail": {"email": "x@y.z"}}])
        # Even raw SQL cannot rewrite history: the trigger refuses it.
        raw = sqlite3.connect(self.db)
        self.addCleanup(raw.close)
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute("UPDATE audit_event SET client_id = 'X'")
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute("DELETE FROM audit_event")
        self.assertEqual(client.row_count("audit_event"), 1)

    def test_one_live_onboarding_per_employee_is_enforced_by_the_database(self) -> None:
        client = self.client()
        base = {"client_id": "C1", "employee_id": "E1", "status": "active"}
        client.upsert("onboarding", [{**base, "onboarding_id": "O1"}])
        with self.assertRaises(DuplicateOnboardingError):
            client.upsert("onboarding", [{**base, "onboarding_id": "O2"}])
        client.upsert("onboarding", [{**base, "onboarding_id": "O1", "status": "cancelled"}])
        client.upsert("onboarding", [{**base, "onboarding_id": "O2"}])  # allowed once O1 is cancelled
        self.assertEqual(client.row_count("onboarding"), 2)

    def test_foreign_keys_and_cascade(self) -> None:
        client = self.client()
        with self.assertRaises(PersistenceError):
            client.upsert("task", [{"task_id": "T1", "onboarding_id": "MISSING", "client_id": "C1"}])
        client.upsert("onboarding", [{"onboarding_id": "O1", "client_id": "C1", "employee_id": "E1", "status": "active"}])
        client.upsert("task", [{"task_id": "T1", "onboarding_id": "O1", "client_id": "C1"}])
        client.upsert("task_dependency", [{"task_id": "T1", "depends_on_id": "T0"}])
        client.delete("onboarding", {"onboarding_id": "O1"})
        self.assertEqual((client.row_count("task"), client.row_count("task_dependency")), (0, 0))

    def test_transactions_commit_roll_back_and_nest(self) -> None:
        client = self.client()
        row = lambda n: {"onboarding_id": n, "client_id": "C", "employee_id": n, "status": "active"}  # noqa: E731
        with client.transaction():
            client.upsert("onboarding", [row("A")])
            with client.transaction():
                client.upsert("onboarding", [row("B")])
        self.assertEqual(client.row_count("onboarding"), 2)
        with self.assertRaises(RuntimeError):
            with client.transaction():
                client.upsert("onboarding", [row("C1")])
                with client.transaction():
                    client.upsert("onboarding", [row("D")])
                raise RuntimeError("boom")
        self.assertEqual(client.row_count("onboarding"), 2)  # the whole group was rolled back

    def test_a_failing_multi_row_write_is_all_or_nothing(self) -> None:
        client = self.client()
        good = {"task_id": "T1", "onboarding_id": "O1", "client_id": "C"}
        client.upsert("onboarding", [{"onboarding_id": "O1", "client_id": "C", "employee_id": "E", "status": "active"}])
        with self.assertRaises(PersistenceError):
            client.upsert("task", [good, {"task_id": "T2", "onboarding_id": "NOPE", "client_id": "C"}])
        self.assertEqual(client.row_count("task"), 0)

    def test_database_errors_become_persistence_errors_without_leaking_paths(self) -> None:
        bad = self.dir / "garbage.sqlite3"
        bad.write_bytes(b"this is not a sqlite database" * 50)
        with self.assertRaises(PersistenceError) as ctx:
            SqliteTableClient(bad)
        self.assertNotIn(str(self.dir), str(ctx.exception))
        blocker = self.dir / "a_file"
        blocker.write_text("x")
        with self.assertRaises(PersistenceError):
            SqliteTableClient(blocker / "child.sqlite3")  # parent is a file, not a directory
        client = SqliteTableClient(self.dir / "closed.sqlite3")
        client.close()
        for call in (lambda: client.select("onboarding", {}), lambda: client.upsert("onboarding", []),
                     lambda: client.row_count("onboarding")):
            with self.assertRaises(PersistenceError):
                call()

    def test_a_newer_schema_version_is_refused(self) -> None:
        self.client().close()
        raw = sqlite3.connect(self.db)
        raw.execute("PRAGMA user_version = 99")
        raw.commit()
        raw.close()
        with self.assertRaises(PersistenceError):
            SqliteTableClient(self.db)


# --------------------------------------------------------------------------- local mode
class LocalModeConfigurationTests(TempDbCase):
    def test_default_is_in_memory_and_creates_no_file(self) -> None:
        self.assertIsNone(store_from_env({}))
        self.assertIsNone(store_from_env({"UC02_STORE_MODE": "memory"}))
        self.assertIsNone(build_service({}).store)

    def test_sqlite_mode_is_explicit_and_creates_the_database(self) -> None:
        store = store_from_env({"UC02_STORE_MODE": "sqlite", "UC02_SQLITE_PATH": str(self.db)})
        self.assertIsInstance(store.client, SqliteTableClient)
        self.addCleanup(store.client.close)
        self.assertTrue(self.db.exists())

    def test_unknown_mode_and_unopenable_database_raise_instead_of_falling_back(self) -> None:
        with self.assertRaises(PersistenceError):
            store_from_env({"UC02_STORE_MODE": "postgres"})
        blocker = self.dir / "file"
        blocker.write_text("x")
        with self.assertRaises(PersistenceError):  # no silent in-memory fallback
            build_service({"UC02_STORE_MODE": "sqlite", "UC02_SQLITE_PATH": str(blocker / "x.sqlite3")})

    def test_the_default_database_lives_in_a_git_ignored_development_directory(self) -> None:
        self.assertEqual(persistence.DEFAULT_SQLITE_PATH.parent.name, "local_db")
        gitignore = (Path(persistence.__file__).resolve().parents[2] / ".gitignore").read_text()
        for pattern in ("local_db/", "*.sqlite3", "*.sqlite3-wal"):
            self.assertIn(pattern, gitignore)


# --------------------------------------------------------------------------- the service
def _scenario(service: OnboardingService, assignee_task="hr_record_create"):
    oid = service.create_onboarding(
        HR_OPS, make_trigger(persona("EMP-007")).model_copy(update={"case_id": "CASE-S", "correlation_id": "corr-S"})
    ).onboarding_id
    t = lambda k: f"{oid}#{k}"  # noqa: E731
    service.assign_owner(HR_OPS, oid, t(assignee_task), "U-OPS-1", team="Ops")
    service.complete_task(HR_OPS, oid, t("hr_record_create"))
    service.complete_task(HR_OPS, oid, t("salary_transfer_letter"), "EVID-1")
    service.apply_status_update(UC03, oid, TempDbCase.failure(oid))
    request = service.request_change(HR_OPS, oid, joining_date=date(2026, 11, 30))
    service.decide_change(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
    service.apply_change(HR_OPS, request.request_id)
    return oid


def _normalised(record):
    data = record.model_dump(mode="json")
    for key in ("created_at", "updated_at"):
        data.pop(key)
    for event in data["plan_events"]:
        event["at"] = bool(event["at"])  # wall-clock stamps differ between two runs
    for task in data["tasks"]:
        for key in [k for k in task if k.endswith("_at")]:
            task[key] = bool(task[key])
    return data


class ServicePersistenceTests(TempDbCase):
    def test_business_logic_is_independent_of_the_storage_adapter(self) -> None:
        memory_client = InMemoryTableClient()
        mem = OnboardingService(build_registry(), store=OnboardingStore(memory_client), source_principals=PRINCIPALS)
        sql = self.service()
        mem_oid, sql_oid = _scenario(mem), _scenario(sql)
        self.assertEqual(mem_oid, sql_oid)
        self.assertEqual(_normalised(mem.get(HR_OPS, mem_oid)), _normalised(sql.get(HR_OPS, sql_oid)))
        self.assertEqual(_normalised(mem.store.load_onboarding(CLIENT_ALPHA, mem_oid)),
                         _normalised(sql.store.load_onboarding(CLIENT_ALPHA, sql_oid)))
        trail = lambda s: [(e.action, e.outcome, e.task_id, e.before_status, e.after_status)  # noqa: E731
                           for e in s.store.load_audit(CLIENT_ALPHA)]
        self.assertEqual(sorted(map(str, trail(mem))), sorted(map(str, trail(sql))))
        self.assertEqual(
            [(r["revision"], r["drivers"], r["rescheduled_task_ids"]) for r in mem.store.plan_revisions(CLIENT_ALPHA, mem_oid)],
            [(r["revision"], r["drivers"], r["rescheduled_task_ids"]) for r in sql.store.plan_revisions(CLIENT_ALPHA, sql_oid)])

    def test_every_supported_record_type_reaches_the_database(self) -> None:
        client = self.client()
        service = self.service(client)
        _scenario(service)
        counts = {t: client.row_count(t) for t in
                  ("onboarding", "task", "task_dependency", "plan_revision", "approval_request",
                   "audit_event", "client_config_ref")}
        self.assertTrue(all(v >= 1 for v in counts.values()), counts)
        self.assertEqual(counts["onboarding"], 1)
        stored = service.store.load_onboarding(CLIENT_ALPHA, "ONB-CLIENT-ALPHA-EMP-007")
        self.assertEqual((stored.case_id, stored.correlation_id), ("CASE-S", "corr-S"))
        self.assertIn("u-fail", stored.applied_updates)  # the idempotency record

    def test_agent_runs_are_persisted_when_the_adapter_uses_a_store(self) -> None:
        from onboarding_task_management.sub_agent.adapter import UC02SubAgent, WorkflowRequest

        client = self.client()
        agent = UC02SubAgent(service=self.service(client))
        trigger = make_trigger(persona("EMP-001")).model_dump(mode="json")
        result = agent.run(WorkflowRequest(trigger=trigger, llm_behaviour="offline"))
        runs = OnboardingStore(client).agent_runs(CLIENT_ALPHA)
        self.assertEqual([r["onboarding_id"] for r in runs], [result.onboarding_id])
        self.assertTrue(runs[0]["run_id"])

    def test_audit_event_ids_stay_unique_when_services_share_one_database(self) -> None:
        client = self.client()
        a, b = self.service(client), self.service(client)
        a.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        b.create_onboarding(HR_OPS_BETA, make_trigger(persona("EMP-006")))  # would collide on a bare counter
        ids = [e.event_id for e in OnboardingStore(client).load_audit(CLIENT_ALPHA)] + \
              [e.event_id for e in OnboardingStore(client).load_audit(CLIENT_BETA)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_second_service_cannot_create_a_duplicate_or_reuse_a_cancelled_id(self) -> None:
        first = self.service()
        oid = first.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).onboarding_id
        second = self.service(self.client())  # fresh, never hydrated
        with self.assertRaises(DuplicateOnboardingError):
            second.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        request = first.request_cancellation(HR_OPS, oid, "synthetic")
        first.decide_change(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        first.apply_cancellation(HR_OPS, request.request_id)
        third = self.service(self.client())
        new_oid = third.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).onboarding_id
        self.assertEqual(new_oid, f"{oid}-R2")  # history was loaded, so the cancelled case is not overwritten
        self.assertEqual(third.store.load_onboarding(CLIENT_ALPHA, oid).status, OnboardingStatus.CANCELLED)

    def test_replayed_status_update_is_idempotent_in_the_database(self) -> None:
        client = self.client()
        service = self.service(client)
        oid = _scenario(service)
        rows = client.row_count("audit_event")
        again = service.apply_status_update(UC03, oid, self.failure(oid))
        self.assertTrue(again.duplicate_replay)
        self.assertEqual(client.row_count("audit_event"), rows)

    def test_tenant_isolation_in_the_database(self) -> None:
        client = self.client()
        alpha = self.service(client)
        oid = alpha.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).onboarding_id
        beta = self.service(self.client())  # a different process's view: nothing loaded
        with self.assertRaises(DataIsolationError):
            beta.get(HR_OPS_BETA, oid)
        with self.assertRaises(DataIsolationError):
            beta.complete_task(HR_OPS_BETA, oid, f"{oid}#hr_record_create")
        self.assertEqual(beta.hydrate(CLIENT_BETA), 0)
        self.assertEqual(beta.store.list_onboardings(CLIENT_BETA), [])
        self.assertEqual(beta.store.load_audit(CLIENT_BETA)[-1].outcome, "denied_isolation")
        with self.assertRaises(DataIsolationError):
            OnboardingStore(client).load_onboarding(CLIENT_BETA, oid)
        self.assertEqual(alpha.get(HR_OPS, oid).onboarding_id, oid)  # the owner is unaffected


class PersistenceFailureTests(TempDbCase):
    """A storage failure is never reported as success and never leaves memory ahead of disk."""

    class Flaky(SqliteTableClient):
        fail_tables: set[str] = set()

        def upsert(self, table, rows):
            if table in self.fail_tables:
                raise PersistenceError("simulated storage failure", table=table)
            return super().upsert(table, rows)

    def flaky_service(self):
        client = self.Flaky(self.db)
        self._open.append(client)
        client.fail_tables = set()
        service = OnboardingService(build_registry(), store=OnboardingStore(client), source_principals=PRINCIPALS)
        return service, client

    def test_a_failed_completion_is_reported_and_not_kept_in_memory(self) -> None:
        service, client = self.flaky_service()
        oid = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).onboarding_id
        root = f"{oid}#hr_record_create"
        client.fail_tables = {"task"}
        with self.assertRaises(PersistenceError):
            service.complete_task(HR_OPS, oid, root)
        self.assertNotEqual(service.get(HR_OPS, oid).task_by_id(root).status, TaskStatus.COMPLETE)
        self.assertEqual(self.client(self.dir / "x.sqlite3").row_count("task"), 0)  # (separate file, sanity)
        self.assertFalse([e for e in service.audit.events_for(HR_OPS) if e.action == "task.complete" and e.outcome == "complete"])
        client.fail_tables = set()
        self.assertEqual(service.complete_task(HR_OPS, oid, root).status, TaskStatus.COMPLETE)  # retry works
        self.assertEqual(OnboardingStore(client).load_onboarding(CLIENT_ALPHA, oid).task_by_id(root).status,
                         TaskStatus.COMPLETE)

    def test_a_failed_status_update_leaves_no_idempotency_entry_so_a_retry_is_applied(self) -> None:
        service, client = self.flaky_service()
        oid = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).onboarding_id
        service.complete_task(HR_OPS, oid, f"{oid}#hr_record_create")
        client.fail_tables = {"onboarding"}
        with self.assertRaises(PersistenceError):
            service.apply_status_update(UC03, oid, self.failure(oid, "u-retry"))
        self.assertNotIn("u-retry", service.get(HR_OPS, oid).applied_updates)
        client.fail_tables = set()
        result = service.apply_status_update(UC03, oid, self.failure(oid, "u-retry"))
        self.assertTrue(result.accepted and not result.duplicate_replay)

    def test_a_failed_replan_changes_nothing_in_memory_or_on_disk(self) -> None:
        service, client = self.flaky_service()
        oid = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).onboarding_id
        before = service.get(HR_OPS, oid).model_dump(mode="json")
        request = service.request_change(HR_OPS, oid, joining_date=date(2026, 11, 30))
        service.decide_change(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        client.fail_tables = {"plan_revision"}
        with self.assertRaises(PersistenceError):
            service.apply_change(HR_OPS, request.request_id)
        after = service.get(HR_OPS, oid).model_dump(mode="json")
        self.assertEqual(after["joining_date"], before["joining_date"])
        self.assertEqual(after["revision"], 0)
        stored = OnboardingStore(client).load_onboarding(CLIENT_ALPHA, oid)
        self.assertEqual(stored.revision, 0)  # the transaction rolled the onboarding write back
        self.assertEqual(OnboardingStore(client).plan_revisions(CLIENT_ALPHA, oid), [])
        self.assertFalse([e for e in service.audit.events_for(HR_OPS) if e.action == "change.apply"])
        client.fail_tables = set()
        self.assertEqual(service.apply_change(HR_OPS, request.request_id).revision, 1)

    def test_a_failed_creation_leaves_nothing_behind(self) -> None:
        service, client = self.flaky_service()
        client.fail_tables = {"task"}
        with self.assertRaises(PersistenceError):
            service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        self.assertEqual(service._client_records(CLIENT_ALPHA), {})
        self.assertEqual(client.row_count("onboarding"), 0)  # rolled back with the tasks
        self.assertEqual(client.row_count("task"), 0)

    def test_an_audit_write_failure_is_not_swallowed(self) -> None:
        service, client = self.flaky_service()
        client.fail_tables = {"audit_event"}
        with self.assertRaises(PersistenceError):
            service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        self.assertEqual(service.audit.events_for(HR_OPS), [])  # an unsaved event is not reported as recorded


class ApprovalConsistencyTests(TempDbCase):
    """Approval requests and the records they gate stay consistent when a write fails, and survive a restart."""

    Flaky = PersistenceFailureTests.Flaky

    def make(self):
        client = self.Flaky(self.db)
        self._open.append(client)
        client.fail_tables = set()
        service = OnboardingService(build_registry(), store=OnboardingStore(client), source_principals=PRINCIPALS)
        return service, client

    def case(self):
        service, client = self.make()
        oid = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).onboarding_id
        return service, client, oid

    @staticmethod
    def pending(service):
        return [r.request_id for r in service.inbox.pending(HR_OPS)]

    def test_a_failed_change_request_leaves_no_request_in_memory_and_no_audit_claim(self) -> None:
        service, client, oid = self.case()
        client.fail_tables = {"approval_request"}
        with self.assertRaises(PersistenceError):
            service.request_change(HR_OPS, oid, joining_date=date(2026, 11, 30))
        self.assertEqual(self.pending(service), [])
        self.assertEqual(OnboardingStore(client).load_approvals(CLIENT_ALPHA), [])
        self.assertFalse([e for e in service.audit.events_for(HR_OPS) if e.action == "change.request"])
        client.fail_tables = set()
        request = service.request_change(HR_OPS, oid, joining_date=date(2026, 11, 30))  # a retry works
        self.assertEqual(self.pending(service), [request.request_id])

    def test_a_failed_cancellation_request_is_forgotten_too(self) -> None:
        service, client, oid = self.case()
        client.fail_tables = {"approval_request"}
        with self.assertRaises(PersistenceError):
            service.request_cancellation(HR_OPS, oid, "synthetic")
        self.assertEqual(self.pending(service), [])
        self.assertFalse([e for e in service.audit.events_for(HR_OPS) if e.action == "onboarding.cancel_request"])

    def test_a_failed_decision_is_not_kept_so_the_retry_is_not_refused_as_already_decided(self) -> None:
        service, client, oid = self.case()
        request = service.request_change(HR_OPS, oid, joining_date=date(2026, 11, 30))
        client.fail_tables = {"approval_request"}
        with self.assertRaises(PersistenceError):
            service.decide_change(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        still = service.inbox.get(HR_OPS, request.request_id)
        self.assertEqual((still.outcome, still.decided_by), (DecisionOutcome.PENDING, None))
        self.assertEqual(OnboardingStore(client).load_approvals(CLIENT_ALPHA)[0].outcome, DecisionOutcome.PENDING)
        self.assertFalse([e for e in service.audit.events_for(HR_OPS) if e.action == "change.decide" and e.outcome == "approved"])
        client.fail_tables = set()
        decided = service.decide_change(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        self.assertEqual(decided.outcome, DecisionOutcome.APPROVED)
        self.assertEqual(OnboardingStore(client).load_approvals(CLIENT_ALPHA)[0].outcome, DecisionOutcome.APPROVED)

    def test_plan_submission_and_decision_roll_back_together_with_their_records(self) -> None:
        service, client, oid = self.case()
        client.fail_tables = {"approval_request"}
        with self.assertRaises(PersistenceError):
            service.submit_plan(HR_OPS, oid)
        self.assertEqual(service.get(HR_OPS, oid).plan_status.value, "proposed")
        self.assertEqual(self.pending(service), [])
        self.assertEqual(OnboardingStore(client).load_onboarding(CLIENT_ALPHA, oid).plan_status.value, "proposed")
        client.fail_tables = set()
        request = service.submit_plan(HR_OPS, oid)
        client.fail_tables = {"approval_request"}
        with self.assertRaises(PersistenceError):
            service.decide_plan(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        self.assertEqual(service.get(HR_OPS, oid).plan_status.value, "submitted")
        self.assertEqual(service.inbox.get(HR_OPS, request.request_id).outcome, DecisionOutcome.PENDING)
        self.assertEqual(OnboardingStore(client).load_onboarding(CLIENT_ALPHA, oid).plan_status.value, "submitted")
        client.fail_tables = set()
        self.assertEqual(service.decide_plan(HR_OPS_APPROVER, request.request_id,
                                             DecisionOutcome.APPROVED).plan_status.value, "approved")

    def test_final_submission_and_decision_roll_back_when_the_approval_cannot_be_stored(self) -> None:
        service, client, oid = self.case()
        service.decide_plan(HR_OPS_APPROVER, service.submit_plan(HR_OPS, oid).request_id, DecisionOutcome.APPROVED)
        client.fail_tables = {"approval_request"}
        with self.assertRaises(PersistenceError):
            service.submit_final(HR_OPS, oid, date(2026, 11, 2))
        self.assertEqual(service.get(HR_OPS, oid).final_status.value, "not_submitted")
        self.assertEqual(service.get(HR_OPS, oid).final_submissions, [])
        client.fail_tables = set()
        final = service.submit_final(HR_OPS, oid, date(2026, 11, 2))
        client.fail_tables = {"approval_request"}
        with self.assertRaises(PersistenceError):
            service.decide_final(HR_OPS_APPROVER, final.request_id, DecisionOutcome.REJECTED, "synthetic")
        record = service.get(HR_OPS, oid)
        self.assertEqual((record.final_status.value, record.final_submissions[-1].decision), ("submitted", "pending"))
        self.assertEqual(service.inbox.get(HR_OPS, final.request_id).outcome, DecisionOutcome.PENDING)

    def test_approval_decisions_survive_a_restart_and_cannot_be_repeated(self) -> None:
        service, client, oid = self.case()
        request = service.submit_plan(HR_OPS, oid)
        service.decide_plan(HR_OPS_APPROVER, request.request_id, DecisionOutcome.REJECTED, "synthetic reason")
        change = service.request_change(HR_OPS, oid, joining_date=date(2026, 11, 30))
        service.decide_change(HR_OPS_APPROVER, change.request_id, DecisionOutcome.APPROVED)
        client.close()
        restarted, _ = self.make()
        self.assertEqual(restarted.hydrate(CLIENT_ALPHA), 1)
        plan = restarted.inbox.get(HR_OPS, request.request_id)
        self.assertEqual((plan.outcome, plan.decided_by, plan.reason),
                         (DecisionOutcome.REJECTED, HR_OPS_APPROVER.actor_id, "synthetic reason"))
        self.assertEqual(restarted.get(HR_OPS, oid).plan_status.value, "rejected")
        self.assertEqual(restarted.inbox.get(HR_OPS, change.request_id).outcome, DecisionOutcome.APPROVED)
        for outcome in (DecisionOutcome.APPROVED, DecisionOutcome.REJECTED):  # no second decision, before or after restart
            with self.assertRaises(InvalidStateError):
                restarted.decide_change(HR_OPS_APPROVER, change.request_id, outcome)
            with self.assertRaises(InvalidStateError):
                restarted.decide_plan(HR_OPS_APPROVER, request.request_id, outcome, "again")
        again = restarted.apply_change(HR_OPS, change.request_id)  # the approved change is still usable
        self.assertEqual(again.revision, 1)
        new_request = restarted.request_change(HR_OPS, oid, location="Remote")
        self.assertNotEqual(new_request.request_id, change.request_id)  # ids continue, never collide


if __name__ == "__main__":
    unittest.main()
