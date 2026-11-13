"""Persistent storage: round trips, idempotency, restart, isolation, failure.

Every test here runs against :class:`InMemoryTableClient`, which enforces the
same primary keys, append-only audit rule and personal-data rejection as the
migration. No database, credential or network is involved.

``SchemaParityTests`` is the guard that keeps this honest: it reads the SQL
migration and asserts the Python mapping and the table definitions agree, so a
column added on one side and not the other fails the suite.
"""

from __future__ import annotations

import re
import unittest
from datetime import date
from pathlib import Path

from onboarding_task_management import persistence
from onboarding_task_management.contracts import (
    Actor,
    AuditEvent,
    DecisionOutcome,
    OnboardingStatus,
    Role,
    TaskStatus,
)
from onboarding_task_management.errors import (
    DataIsolationError,
    DuplicateOnboardingError,
)
from onboarding_task_management.persistence import (
    InMemoryTableClient,
    OnboardingStore,
    PersistenceError,
    config_hash,
)
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import (
    ALPHA_CONFIG,
    ANCHOR_TODAY,
    BETA_CONFIG,
    CLIENT_ALPHA,
    CLIENT_BETA,
    build_registry,
    make_trigger,
    persona,
)
from tests.support import HR_OPS, HR_OPS_APPROVER, HR_OPS_BETA

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
INIT_SQL = MIGRATIONS / "20261027090000_uc02_init.sql"
RLS_SQL = MIGRATIONS / "20261027090100_uc02_rls.sql"


def stored_service(**kwargs) -> tuple[OnboardingService, InMemoryTableClient]:
    client = InMemoryTableClient()
    service = OnboardingService(
        build_registry(), store=OnboardingStore(client), **kwargs
    )
    return service, client


class SchemaParityTests(unittest.TestCase):
    """The SQL migration and the Python mapping must not drift apart."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.init_sql = INIT_SQL.read_text(encoding="utf-8")
        cls.rls_sql = RLS_SQL.read_text(encoding="utf-8")

    def test_every_mapped_table_exists_in_the_migration(self) -> None:
        for table in persistence.PRIMARY_KEYS:
            with self.subTest(table=table):
                self.assertIn(f"create table uc02.{table}", self.init_sql)

    def test_every_created_table_is_mapped_in_python(self) -> None:
        created = set(re.findall(r"create table uc02\.(\w+)", self.init_sql))
        self.assertEqual(created, set(persistence.PRIMARY_KEYS))

    def test_milestones_are_not_stored(self) -> None:
        """Milestones are derived; a table would be a second source of truth."""
        self.assertNotIn("create table uc02.milestone", self.init_sql)

    def test_task_status_check_matches_the_python_enum(self) -> None:
        for status in TaskStatus:
            with self.subTest(status=status):
                self.assertIn(f"'{status.value}'", self.init_sql)

    def test_audit_trigger_mirrors_the_python_forbidden_keys(self) -> None:
        from onboarding_task_management.audit import FORBIDDEN_DETAIL_KEYS

        self.assertEqual(FORBIDDEN_DETAIL_KEYS, persistence.FORBIDDEN_DETAIL_KEYS)
        for key in FORBIDDEN_DETAIL_KEYS:
            with self.subTest(key=key):
                self.assertIn(f"'{key}'", self.init_sql)

    def test_no_personal_data_column_exists(self) -> None:
        for forbidden in ("email", "full_name", "passport", "phone", "address"):
            with self.subTest(column=forbidden):
                self.assertNotIn(f"\n  {forbidden} ", self.init_sql)

    def test_every_table_has_rls_enabled_and_forced(self) -> None:
        """Whitespace in the DDL is aligned for readability, so normalise it."""
        flat = " ".join(self.rls_sql.split())
        for table in persistence.PRIMARY_KEYS:
            with self.subTest(table=table):
                self.assertIn(
                    f"alter table uc02.{table} enable row level security", flat
                )
                self.assertIn(
                    f"alter table uc02.{table} force row level security", flat
                )

    def test_audit_has_no_update_or_delete_policy(self) -> None:
        self.assertIn("audit_event_tenant_read", self.rls_sql)
        self.assertIn("audit_event_tenant_insert", self.rls_sql)
        self.assertNotIn("audit_event_tenant_update", self.rls_sql)
        self.assertNotIn("audit_event_tenant_delete", self.rls_sql)

    def test_rls_fails_closed_when_no_client_is_set(self) -> None:
        self.assertIn("fail-closed", self.rls_sql.lower())
        # current_client_id() returns NULL when neither source supplies a
        # client, and `client_id = NULL` never matches a row.
        self.assertIn("nullif(current_setting", self.init_sql)
        self.assertIn("current_client_id()", self.rls_sql)


class RoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = OnboardingService(build_registry())
        self.record = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-007"))
        )
        self.client = InMemoryTableClient()
        self.store = OnboardingStore(self.client)

    def test_a_saved_record_reads_back_identically(self) -> None:
        self.store.save_onboarding(self.record, config=ALPHA_CONFIG)
        back = self.store.load_onboarding(CLIENT_ALPHA, self.record.onboarding_id)
        self.assertEqual(back.model_dump(), self.record.model_dump())

    def test_task_order_is_preserved(self) -> None:
        """Plan order is topological; alphabetical task_id order is not."""
        self.store.save_onboarding(self.record, config=ALPHA_CONFIG)
        back = self.store.load_onboarding(CLIENT_ALPHA, self.record.onboarding_id)
        self.assertEqual(
            [t.template_key for t in back.tasks],
            [t.template_key for t in self.record.tasks],
        )

    def test_dependencies_are_stored_as_edges(self) -> None:
        self.store.save_onboarding(self.record, config=ALPHA_CONFIG)
        expected = sum(len(t.depends_on) for t in self.record.tasks)
        self.assertEqual(self.client.row_count("task_dependency"), expected)

    def test_missing_record_returns_none(self) -> None:
        self.assertIsNone(self.store.load_onboarding(CLIENT_ALPHA, "ONB-NOPE"))

    def test_config_reference_is_recorded_not_the_config(self) -> None:
        self.store.save_onboarding(self.record, config=ALPHA_CONFIG)
        rows = self.client.select("client_config_ref", {"client_id": CLIENT_ALPHA})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["config_hash"], config_hash(ALPHA_CONFIG))
        self.assertEqual(rows[0]["template_count"], len(ALPHA_CONFIG.templates))
        # The task content itself is not duplicated into the reference row.
        self.assertNotIn("templates", rows[0])

    def test_config_hash_is_deterministic_and_distinguishes_configs(self) -> None:
        self.assertEqual(config_hash(ALPHA_CONFIG), config_hash(ALPHA_CONFIG))
        self.assertNotEqual(config_hash(ALPHA_CONFIG), config_hash(BETA_CONFIG))


class IdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.client = stored_service()
        self.record = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"))
        )
        self.store = self.service.store

    def test_saving_twice_creates_no_duplicates(self) -> None:
        before = (
            self.client.row_count("onboarding"),
            self.client.row_count("task"),
            self.client.row_count("task_dependency"),
        )
        self.store.save_onboarding(self.record, config=ALPHA_CONFIG)
        self.store.save_onboarding(self.record, config=ALPHA_CONFIG)
        after = (
            self.client.row_count("onboarding"),
            self.client.row_count("task"),
            self.client.row_count("task_dependency"),
        )
        self.assertEqual(before, after)

    def test_a_duplicate_onboarding_is_refused_against_stored_state(self) -> None:
        with self.assertRaises(DuplicateOnboardingError):
            self.service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))

    def test_repeated_task_updates_do_not_multiply_rows(self) -> None:
        task_id = f"{self.record.onboarding_id}#hr_record_create"
        self.service.start_task(HR_OPS, self.record.onboarding_id, task_id)
        count = self.client.row_count("task")
        self.service.complete_task(HR_OPS, self.record.onboarding_id, task_id)
        self.assertEqual(self.client.row_count("task"), count)

    def test_audit_append_is_idempotent_by_event_id(self) -> None:
        self.service._persist_audit(HR_OPS)
        first = self.client.row_count("audit_event")
        self.service._persist_audit(HR_OPS)
        self.assertEqual(self.client.row_count("audit_event"), first)


class RestartTests(unittest.TestCase):
    """Persistence is only real if a new process can continue from it."""

    def setUp(self) -> None:
        self.service, self.client = stored_service()
        self.record = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"))
        )
        self.onboarding_id = self.record.onboarding_id
        self.task_id = f"{self.onboarding_id}#hr_record_create"

    def restart(self) -> OnboardingService:
        """A brand-new service over the same stored rows."""
        rebuilt = InMemoryTableClient.from_snapshot(self.client.snapshot())
        service = OnboardingService(build_registry(), store=OnboardingStore(rebuilt))
        service.hydrate(CLIENT_ALPHA)
        return service

    def test_a_new_process_sees_the_case(self) -> None:
        service = self.restart()
        record = service.get(HR_OPS, self.onboarding_id)
        self.assertEqual(record.onboarding_id, self.onboarding_id)
        self.assertEqual(len(record.tasks), len(self.record.tasks))

    def test_task_progress_survives_a_restart(self) -> None:
        self.service.complete_task(HR_OPS, self.onboarding_id, self.task_id)

        service = self.restart()
        task = service.get(HR_OPS, self.onboarding_id).task_by_id(self.task_id)
        self.assertIs(task.status, TaskStatus.COMPLETE)
        self.assertIsNotNone(task.completed_at)

    def test_readiness_derived_after_restart_matches(self) -> None:
        self.service.complete_task(HR_OPS, self.onboarding_id, self.task_id)
        before = {t.template_key for t in self.service.ready_tasks(HR_OPS, self.onboarding_id)}

        service = self.restart()
        after = {t.template_key for t in service.ready_tasks(HR_OPS, self.onboarding_id)}
        self.assertEqual(before, after)

    def test_evidence_reference_survives_a_restart(self) -> None:
        service, client = stored_service()
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-007")))
        task_id = f"{record.onboarding_id}#medical_test"
        service.complete_task(HR_OPS, record.onboarding_id, task_id, "EVID-001")

        rebuilt = OnboardingService(
            build_registry(),
            store=OnboardingStore(InMemoryTableClient.from_snapshot(client.snapshot())),
        )
        rebuilt.hydrate(CLIENT_ALPHA)
        task = rebuilt.get(HR_OPS, record.onboarding_id).task_by_id(task_id)
        self.assertEqual(task.evidence_ref, "EVID-001")

    def test_pending_approval_survives_a_restart(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=date(2026, 11, 30)
        )
        service = self.restart()
        restored = service.inbox.get(HR_OPS, request.request_id)
        self.assertIs(restored.outcome, DecisionOutcome.PENDING)

    def test_a_restored_inbox_does_not_reissue_an_existing_request_id(self) -> None:
        first = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=date(2026, 11, 30)
        )
        service = self.restart()
        second = service.request_change(
            HR_OPS, self.onboarding_id, location="Abu Dhabi"
        )
        self.assertNotEqual(first.request_id, second.request_id)

    def test_replan_preserves_completed_work_across_a_restart(self) -> None:
        self.service.complete_task(HR_OPS, self.onboarding_id, self.task_id)
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=date(2026, 11, 30)
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_change(HR_OPS, request.request_id)

        service = self.restart()
        record = service.get(HR_OPS, self.onboarding_id)
        self.assertIs(record.task_by_id(self.task_id).status, TaskStatus.COMPLETE)
        self.assertEqual(record.revision, 1)

    def test_plan_revisions_are_recorded(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=date(2026, 11, 30)
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_change(HR_OPS, request.request_id)

        revisions = self.service.store.plan_revisions(CLIENT_ALPHA, self.onboarding_id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["revision"], 1)
        self.assertEqual(revisions[0]["drivers"], ["joining_date"])
        self.assertEqual(revisions[0]["approval_request_id"], request.request_id)

    def test_cancellation_survives_a_restart(self) -> None:
        request = self.service.request_cancellation(
            HR_OPS, self.onboarding_id, reason="withdrawn"
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_cancellation(HR_OPS, request.request_id)

        service = self.restart()
        record = service.get(HR_OPS, self.onboarding_id)
        self.assertIs(record.status, OnboardingStatus.CANCELLED)

    def test_removed_tasks_do_not_linger_in_storage(self) -> None:
        """A re-plan that drops a task must drop its row, not orphan it."""
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, location="Remote-IN"
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_change(HR_OPS, request.request_id)

        stored = {
            row["template_key"]
            for row in self.client.select("task", {"onboarding_id": self.onboarding_id})
        }
        self.assertNotIn("desk_allocation", stored)
        self.assertIn("remote_kit_shipping", stored)


class IsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = InMemoryTableClient()
        self.store = OnboardingStore(self.client)
        alpha = OnboardingService(build_registry(), store=self.store)
        self.alpha_record = alpha.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"))
        )

    def test_another_client_cannot_load_the_record(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.store.load_onboarding(CLIENT_BETA, self.alpha_record.onboarding_id)

    def test_listing_is_scoped_to_the_client(self) -> None:
        self.assertEqual(len(self.store.list_onboardings(CLIENT_ALPHA)), 1)
        self.assertEqual(self.store.list_onboardings(CLIENT_BETA), [])

    def test_hydrate_only_loads_the_requested_client(self) -> None:
        beta = OnboardingService(build_registry(), store=self.store)
        beta.create_onboarding(HR_OPS_BETA, make_trigger(persona("EMP-006")))

        fresh = OnboardingService(build_registry(), store=self.store)
        self.assertEqual(fresh.hydrate(CLIENT_BETA), 1)
        self.assertEqual(len(fresh._client_records(CLIENT_ALPHA)), 0)

    def test_audit_is_scoped_to_the_client(self) -> None:
        self.store.append_audit(
            [
                AuditEvent(
                    event_id="AUD-X1",
                    client_id=CLIENT_BETA,
                    actor_id="U",
                    actor_role=Role.HR_OPS,
                    action="test",
                    outcome="ok",
                )
            ]
        )
        self.assertEqual(
            [e.event_id for e in self.store.load_audit(CLIENT_BETA)], ["AUD-X1"]
        )
        # Another client's event must not leak. (The alpha service created in setUp now writes
        # its own audit events through to the store, so alpha's list is no longer empty.)
        self.assertNotIn("AUD-X1", [e.event_id for e in self.store.load_audit(CLIENT_ALPHA)])
        self.assertTrue(all(e.client_id == CLIENT_ALPHA for e in self.store.load_audit(CLIENT_ALPHA)))


class FailureHandlingTests(unittest.TestCase):
    """A storage failure must never be reported as success."""

    class ExplodingClient(InMemoryTableClient):
        def __init__(self, fail_on: str) -> None:
            super().__init__()
            self.fail_on = fail_on

        def upsert(self, table, rows):
            if table == self.fail_on:
                raise PersistenceError("backend unavailable", table=table)
            return super().upsert(table, rows)

    def test_a_failed_write_aborts_creation_and_leaves_no_record(self) -> None:
        client = self.ExplodingClient(fail_on="onboarding")
        service = OnboardingService(build_registry(), store=OnboardingStore(client))
        with self.assertRaises(PersistenceError):
            service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        # Not in memory either: the in-memory copy is rolled back.
        self.assertEqual(service._client_records(CLIENT_ALPHA), {})

    def test_the_failure_is_audited(self) -> None:
        client = self.ExplodingClient(fail_on="onboarding")
        service = OnboardingService(build_registry(), store=OnboardingStore(client))
        with self.assertRaises(PersistenceError):
            service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        self.assertIn("failed_persist", [e.outcome for e in service.audit.events_for(HR_OPS)])

    def test_audit_rows_cannot_be_overwritten(self) -> None:
        client = InMemoryTableClient()
        store = OnboardingStore(client)
        event = AuditEvent(
            event_id="AUD-1",
            client_id=CLIENT_ALPHA,
            actor_id="U",
            actor_role=Role.HR_OPS,
            action="a",
            outcome="ok",
        )
        store.append_audit([event])
        with self.assertRaises(PersistenceError):
            store.append_audit([event])

    def test_audit_rows_cannot_be_deleted(self) -> None:
        client = InMemoryTableClient()
        client.upsert(
            "audit_event",
            [
                {
                    "event_id": "AUD-1",
                    "client_id": CLIENT_ALPHA,
                    "actor_id": "U",
                    "actor_role": "hr_ops",
                    "action": "a",
                    "outcome": "ok",
                    "detail": {},
                }
            ],
        )
        with self.assertRaises(PersistenceError):
            client.delete("audit_event", {"event_id": "AUD-1"})

    def test_personal_data_in_audit_detail_is_refused(self) -> None:
        client = InMemoryTableClient()
        with self.assertRaises(PersistenceError):
            client.upsert(
                "audit_event",
                [
                    {
                        "event_id": "AUD-2",
                        "client_id": CLIENT_ALPHA,
                        "actor_id": "U",
                        "actor_role": "hr_ops",
                        "action": "a",
                        "outcome": "ok",
                        "detail": {"email": "someone@example.com"},
                    }
                ],
            )

    def test_an_unknown_table_is_refused(self) -> None:
        with self.assertRaises(PersistenceError):
            InMemoryTableClient().upsert("not_a_table", [{"x": 1}])

    def test_a_row_without_its_primary_key_is_refused(self) -> None:
        with self.assertRaises(PersistenceError):
            InMemoryTableClient().upsert("onboarding", [{"client_id": CLIENT_ALPHA}])


class NoStoreBehaviourTests(unittest.TestCase):
    """Without a store the service must behave exactly as it always did."""

    def test_service_without_a_store_still_works(self) -> None:
        service = OnboardingService(build_registry())
        self.assertIsNone(service.store)
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        self.assertEqual(len(record.tasks), 10)

    def test_hydrate_without_a_store_is_a_no_op(self) -> None:
        self.assertEqual(OnboardingService(build_registry()).hydrate(CLIENT_ALPHA), 0)

    def test_checklist_is_identical_with_and_without_a_store(self) -> None:
        plain = OnboardingService(build_registry())
        stored, _ = stored_service()
        a = plain.create_onboarding(HR_OPS, make_trigger(persona("EMP-007")))
        b = stored.create_onboarding(HR_OPS, make_trigger(persona("EMP-007")))
        self.assertEqual(
            plain.checklist(HR_OPS, a.onboarding_id, ANCHOR_TODAY).model_dump(),
            stored.checklist(HR_OPS, b.onboarding_id, ANCHOR_TODAY).model_dump(),
        )


class SeedTests(unittest.TestCase):
    """Seeding runs fixtures through the real service path and never duplicates."""

    FIXTURES = Path(__file__).resolve().parents[1] / "data" / "mock"

    def setUp(self) -> None:
        from onboarding_task_management.persistence import OnboardingStore

        self.client = InMemoryTableClient()
        self.store = OnboardingStore(self.client)

    def seed(self):
        from onboarding_task_management.seed import build_seeded_service

        return build_seeded_service(build_registry(), self.store, self.FIXTURES)

    def test_seeding_creates_the_default_fixtures(self) -> None:
        _, report = self.seed()
        self.assertEqual(len(report.created), 5)
        self.assertEqual(report.rejected, [])
        self.assertEqual(self.client.row_count("onboarding"), 5)

    def test_seeding_is_idempotent(self) -> None:
        self.seed()
        rows = (
            self.client.row_count("onboarding"),
            self.client.row_count("task"),
            self.client.row_count("task_dependency"),
        )
        _, report = self.seed()
        self.assertEqual(report.created, [])
        self.assertEqual(len(report.skipped), 5)
        self.assertEqual(
            rows,
            (
                self.client.row_count("onboarding"),
                self.client.row_count("task"),
                self.client.row_count("task_dependency"),
            ),
        )

    def test_reseeding_does_not_reset_progress(self) -> None:
        service, _ = self.seed()
        record = self.store.list_onboardings(CLIENT_ALPHA)[0]
        task_id = f"{record.onboarding_id}#hr_record_create"
        service.complete_task(
            Actor(actor_id="seed", role=Role.HR_OPS, client_id=CLIENT_ALPHA),
            record.onboarding_id,
            task_id,
        )

        self.seed()
        reloaded = self.store.load_onboarding(CLIENT_ALPHA, record.onboarding_id)
        self.assertIs(reloaded.task_by_id(task_id).status, TaskStatus.COMPLETE)

    def test_a_rejected_fixture_is_reported_with_its_reason(self) -> None:
        from onboarding_task_management.seed import seed_from_fixtures

        service = OnboardingService(build_registry(), store=self.store)
        report = seed_from_fixtures(
            service, self.FIXTURES, filenames=("02_offer_not_accepted.json",)
        )
        self.assertEqual(report.created, [])
        self.assertEqual(len(report.rejected), 1)
        self.assertIn("MISSING_DATA", report.rejected[0][1])

    def test_a_missing_fixture_is_reported_not_ignored(self) -> None:
        from onboarding_task_management.seed import seed_from_fixtures

        service = OnboardingService(build_registry(), store=self.store)
        report = seed_from_fixtures(
            service, self.FIXTURES, filenames=("no_such_file.json",)
        )
        self.assertEqual(report.rejected[0][1], "fixture file not found")

    def test_only_a_config_reference_is_seeded_never_the_config(self) -> None:
        self.seed()
        for row in self.client.select("client_config_ref", {}):
            self.assertEqual(
                set(row),
                {
                    "client_id",
                    "config_hash",
                    "template_count",
                    "milestone_count",
                },
            )


class LiveDatabaseIntegrationTests(unittest.TestCase):
    """Opt-in test against a real Supabase/Postgres project.

    Skipped unless ``UC02_RUN_DB_INTEGRATION_TESTS=1`` **and** a connection
    string is configured. No Supabase project has been created for UC-02, so
    this currently always skips — and says so rather than silently passing.
    """

    def test_live_database_round_trip(self) -> None:
        import os

        if os.environ.get("UC02_RUN_DB_INTEGRATION_TESTS") != "1":
            self.skipTest(
                "UC02_RUN_DB_INTEGRATION_TESTS is not 1; no live database is configured"
            )
        if not os.environ.get("UC02_DATABASE_URL"):  # pragma: no cover
            self.skipTest("UC02_DATABASE_URL is not set")
        raise self.failureException(  # pragma: no cover
            "a live database driver is not installed; run the migrations with "
            "`supabase db push` and add the adapter before enabling this test"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
