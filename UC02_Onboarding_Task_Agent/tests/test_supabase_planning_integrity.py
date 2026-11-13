"""UC-02's planning data in the shared Supabase tables: tenant isolation, plan revisions, snapshot order,
commit-marker write order, idempotency and outage classification.

Runs against the in-process PostgREST stand-in of ``test_supabase_store.py`` (live schema: unknown columns,
NOT NULL, uuid columns, primary keys, the foreign keys and unique keys the owner's export showed). It proves the
mapping's behaviour, not the live database's CHECK constraints or RLS. Synthetic data only.
"""

from __future__ import annotations

import unittest
from urllib.parse import parse_qsl, urlparse

from onboarding_task_management.contracts import Actor, DecisionOutcome, DocumentFeedback, DocumentSpec, Role
from onboarding_task_management.errors import DataIsolationError, DuplicateOnboardingError
from onboarding_task_management.persistence import TABLE_TASK, TABLE_TASK_DEPENDENCY, PersistenceError
from onboarding_task_management.supabase_rest import SupabaseRestClient, SupabaseUnavailableError
from onboarding_task_management.supabase_store import (STATE_ACTION, CaseNotFoundError, SupabaseTableClient,
                                                       VERSION_EVENTS)
from tests.test_supabase_store import (APPROVER, CASE, EMPLOYEE_2, HR, LIVE_CLIENT, SYNTHETIC_CONFIG, FakePostgrest,
                                       make_service, trigger)

OTHER_CLIENT = "CLIENT_CANADA"  # another live client (the stand-in's client_configs foreign key accepts it)
OTHER_CONFIG = SYNTHETIC_CONFIG.model_copy(update={"client_id": OTHER_CLIENT})
OTHER_CASE = "5eed0000-0000-4000-8000-0000000000aa"
HR_OTHER = Actor(actor_id="U-UC02-OTHER", role=Role.HR_OPS, client_id=OTHER_CLIENT)
INITIAL = [DocumentSpec(document_type="passport"), DocumentSpec(document_type="degree_certificate")]
PLANNING_TABLES = ("onboarding_tasks", "task_dependencies", "plan_revisions")


def _requests(db: FakePostgrest, table: str) -> list[tuple[str, dict[str, str]]]:
    out = []
    for method, url in db.calls:
        parts = urlparse(url)
        if parts.path.rsplit("/rest/v1/", 1)[-1] == table:
            out.append((method, dict(parse_qsl(parts.query))))
    return out


def _snapshots(db: FakePostgrest, case: str = CASE) -> list[dict]:
    return [r for r in db.tables["audit_events"] if r.get("action") == STATE_ACTION and r.get("case_id") == case]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.db = FakePostgrest()
        self.db.seed_case()  # CASE belongs to LIVE_CLIENT
        self.db.seed_case(OTHER_CASE, OTHER_CLIENT, EMPLOYEE_2)
        self.service = make_service(self.db)
        self.oid = self.service.create_onboarding(HR, trigger()).onboarding_id

    def revisions(self, case: str = CASE) -> list[dict]:
        return sorted((r for r in self.db.tables["plan_revisions"] if r["case_id"] == case), key=lambda r: r["revision"])


class TenantIsolationTests(Base):
    def test_every_planning_read_of_a_restarted_process_carries_the_client(self) -> None:
        self.db.calls.clear()
        again = make_service(self.db)  # a restarted process: everything is read from the database
        again.hydrate(LIVE_CLIENT)
        self.assertTrue(again.get(HR, self.oid).tasks)
        for table in PLANNING_TABLES:
            for method, query in _requests(self.db, table):
                self.assertIn("client_id", query, f"{method} {table} without a client filter: {query}")
        snapshot_reads = [q for m, q in _requests(self.db, "audit_events") if m == "GET" and q.get("action") == f"eq.{STATE_ACTION}"]
        self.assertTrue(snapshot_reads)
        self.assertTrue(all("client_id" in q for q in snapshot_reads))

    def test_every_planning_update_and_delete_carries_the_client(self) -> None:
        self.db.calls.clear()
        self.service.define_document_requirements(HR, self.oid, INITIAL)
        self.service.replan_documents(HR, self.oid, DocumentFeedback(
            feedback_id="FB-T", plan_version=1, add_documents=[DocumentSpec(document_type="visa_documents")],
            add_attributes=["visa_required"], reasons=["NEW_MANDATORY_DOCUMENT:visa_documents"]))
        writes = [(t, m, q) for t in PLANNING_TABLES for m, q in _requests(self.db, t) if m in ("PATCH", "DELETE")]
        self.assertTrue(writes)
        for table, method, query in writes:
            self.assertIn("client_id", query, f"{method} {table} without a client filter: {query}")
        self.assertTrue(all(r["client_id"] == LIVE_CLIENT for t in PLANNING_TABLES for r in self.db.tables[t]
                            if r.get("task_id", "").startswith(CASE) or r.get("case_id") == CASE))

    def test_another_client_cannot_read_the_plan(self) -> None:
        with self.assertRaises(DataIsolationError):
            make_service(self.db).get(HR_OTHER, self.oid)
        client = SupabaseTableClient(SupabaseRestClient("https://example.supabase.co", "k", transport=self.db))
        self.assertEqual(client.select(TABLE_TASK, {"onboarding_id": self.oid, "client_id": OTHER_CLIENT}), [])
        self.assertTrue(client.select(TABLE_TASK, {"onboarding_id": self.oid}))  # its own tenant: the plan is there

    def test_another_client_cannot_plan_on_the_case_and_nothing_is_written(self) -> None:
        other = make_service(self.db, OTHER_CONFIG)  # the other client HAS a configuration: isolation is what refuses
        before = len(self.db.tables["onboarding_tasks"])
        foreign = trigger().model_copy(update={"client_id": OTHER_CLIENT})
        with self.assertRaises(DataIsolationError):
            other.create_onboarding(HR_OTHER, foreign)
        self.assertEqual(len(self.db.tables["onboarding_tasks"]), before)
        self.assertFalse([r for r in self.db.tables["onboarding_tasks"] if r["client_id"] == OTHER_CLIENT])
        self.assertFalse([r for r in _snapshots(self.db) if r["client_id"] == OTHER_CLIENT])

    def test_a_dependency_between_two_clients_is_refused(self) -> None:
        other = make_service(self.db, OTHER_CONFIG)
        other_oid = other.create_onboarding(HR_OTHER, trigger(OTHER_CASE, EMPLOYEE_2).model_copy(
            update={"client_id": OTHER_CLIENT})).onboarding_id
        mine = self.service.get(HR, self.oid).tasks[0].task_id
        theirs = other.get(HR_OTHER, other_oid).tasks[0].task_id
        client = self.service.store.client
        with self.assertRaises(DataIsolationError):
            client.upsert(TABLE_TASK_DEPENDENCY, [{"task_id": mine, "depends_on_id": theirs}])
        self.assertFalse([e for e in self.db.tables["task_dependencies"]
                          if (e["task_id"], e["depends_on_task_id"]) == (mine, theirs)])

    def test_a_missing_case_is_reported_as_such_and_nothing_is_created(self) -> None:
        missing = "11111111-1111-4111-8111-111111111111"
        with self.assertRaises(CaseNotFoundError):
            make_service(self.db).create_onboarding(HR, trigger(missing, "EMP-UC02-MISSING"))
        self.assertFalse([r for r in self.db.tables["onboarding_tasks"] if r["case_id"] == missing])
        self.assertFalse(_snapshots(self.db, missing))
        self.assertFalse(self.revisions(missing))


class PlanRevisionTests(Base):
    def feedback(self, plan_version: int, feedback_id: str = "FB-UC02-1") -> DocumentFeedback:
        return DocumentFeedback(feedback_id=feedback_id, plan_version=plan_version,
                                add_documents=[DocumentSpec(document_type="visa_documents", reason="visa required")],
                                add_attributes=["visa_required"], reasons=["NEW_MANDATORY_DOCUMENT:visa_documents"])

    def test_the_initial_plan_is_revision_1(self) -> None:
        (first,) = self.revisions()
        self.assertEqual((first["revision"], first["client_id"]), (1, LIVE_CLIENT))
        self.assertEqual(first["change_summary"]["trigger_source"], VERSION_EVENTS["proposed"])
        self.assertIsNone(first["change_summary"]["previous_revision"])
        self.assertEqual(first["applied_by"], HR.actor_id)

    def test_a_document_replan_adds_revision_2_and_revision_1_stays_as_it_was(self) -> None:
        self.service.define_document_requirements(HR, self.oid, INITIAL)
        first = dict(self.revisions()[0])
        outcome = self.service.replan_documents(HR, self.oid, self.feedback(1))
        revisions = self.revisions()
        self.assertEqual([r["revision"] for r in revisions], [1, 2])
        self.assertEqual(revisions[0], first)  # auditable, unchanged
        second = revisions[1]
        summary = second["change_summary"]
        self.assertEqual((summary["trigger_source"], summary["previous_revision"], summary["feedback_id"]),
                         ("document_validation_feedback", 1, "FB-UC02-1"))
        self.assertEqual(summary["added_documents"], ["visa_documents"])
        self.assertEqual(second["added_task_ids"], outcome.added_task_ids)
        # revision 2 is authoritative: a restarted process reads plan version 2 and its tasks
        again = make_service(self.db)
        again.hydrate(LIVE_CLIENT)
        record = again.get(HR, self.oid)
        self.assertEqual(record.plan_version, 2)
        self.assertTrue({t for t in outcome.added_task_ids} <= {t.task_id for t in record.tasks})

    def test_replaying_the_same_feedback_adds_no_revision_no_task_and_no_snapshot(self) -> None:
        self.service.define_document_requirements(HR, self.oid, INITIAL)
        self.service.replan_documents(HR, self.oid, self.feedback(1))
        counts = (len(self.revisions()), len(self.db.tables["onboarding_tasks"]), len(_snapshots(self.db)),
                  len(self.db.tables["task_dependencies"]))
        replay = make_service(self.db)  # even from a restarted process
        replay.hydrate(LIVE_CLIENT)
        self.assertTrue(replay.replan_documents(HR, self.oid, self.feedback(1)).duplicate_replay)
        self.assertEqual((len(self.revisions()), len(self.db.tables["onboarding_tasks"]), len(_snapshots(self.db)),
                          len(self.db.tables["task_dependencies"])), counts)

    def test_an_approved_date_change_is_the_next_revision_with_its_drivers(self) -> None:
        from datetime import date

        self.service.decide_plan(APPROVER, self.service.submit_plan(HR, self.oid).request_id, DecisionOutcome.APPROVED)
        request = self.service.request_change(HR, self.oid, joining_date=date(2026, 11, 30))
        self.service.decide_change(APPROVER, request.request_id, DecisionOutcome.APPROVED)
        self.service.apply_change(HR, request.request_id)
        revisions = self.revisions()
        self.assertEqual([r["revision"] for r in revisions], [1, 2])
        change = revisions[1]
        self.assertEqual((change["drivers"], change["approval_request_id"]), (["joining_date"], request.request_id))
        self.assertEqual(change["change_summary"]["trigger_source"], "approved_change")
        self.assertEqual(change["change_summary"]["task_revision"], 1)  # UC-02's own task-replan counter


class SnapshotOrderAndCommitTests(Base):
    def test_the_newest_snapshot_wins_even_when_every_timestamp_ties(self) -> None:
        self.service.define_document_requirements(HR, self.oid, INITIAL)
        for row in _snapshots(self.db):
            row["occurred_at"] = "2026-11-05T00:00:00+00:00"  # a coarse clock: all equal
        seqs = sorted(r["after_state"]["snapshot_seq"] for r in _snapshots(self.db))
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        again = make_service(self.db)
        again.hydrate(LIVE_CLIENT)
        self.assertEqual([d.key for d in again.get(HR, self.oid).documents], ["passport", "degree_certificate"])

    def test_a_failed_task_write_leaves_the_previous_plan_in_force(self) -> None:
        self.service.define_document_requirements(HR, self.oid, INITIAL)
        before = len(_snapshots(self.db))
        self.db.fail_tables = {"onboarding_tasks"}
        with self.assertRaises(PersistenceError):
            self.service.replan_documents(HR, self.oid, DocumentFeedback(
                feedback_id="FB-FAIL", plan_version=1, add_documents=[DocumentSpec(document_type="visa_documents")],
                add_attributes=["visa_required"], reasons=["NEW_MANDATORY_DOCUMENT:visa_documents"]))
        self.db.fail_tables = set()
        self.assertEqual(len(_snapshots(self.db)), before)  # the commit marker was never written
        self.assertEqual([r["revision"] for r in self.revisions()], [1])
        again = make_service(self.db)
        again.hydrate(LIVE_CLIENT)
        record = again.get(HR, self.oid)
        self.assertEqual(record.plan_version, 1)
        self.assertNotIn("docfb:FB-FAIL", record.flow_ledger)  # the retry will apply it, not skip it as a replay

    def test_saving_an_unchanged_plan_writes_nothing_new(self) -> None:
        counts = (len(_snapshots(self.db)), len(self.revisions()), len(self.db.tables["onboarding_tasks"]))
        self.service._persist(self.service._load(HR, self.oid))  # the same save the service makes on every change
        self.assertEqual((len(_snapshots(self.db)), len(self.revisions()), len(self.db.tables["onboarding_tasks"])),
                         counts)

    def test_creating_the_same_onboarding_twice_creates_nothing_twice(self) -> None:
        counts = (len(_snapshots(self.db)), len(self.revisions()), len(self.db.tables["onboarding_tasks"]),
                  len(self.db.tables["task_dependencies"]))
        with self.assertRaises(DuplicateOnboardingError):
            make_service(self.db).create_onboarding(HR, trigger())
        self.assertEqual((len(_snapshots(self.db)), len(self.revisions()), len(self.db.tables["onboarding_tasks"]),
                          len(self.db.tables["task_dependencies"])), counts)


class OrchestrationSupportTests(Base):
    """What UC-02 records about an execution the central orchestrator ran (sub_agent/orchestration_support.py)."""

    def test_a_blocked_execution_is_an_exception_run_with_its_code_and_tenant(self) -> None:
        from onboarding_task_management.sub_agent.orchestration_support import record_stage_event, record_stage_run

        record_stage_event(self.service, HR, stage="plan_tasks", outcome="blocked", case_id=CASE, record_exists=True,
                           execution_id="EXE-1", code="UC02_CONFIG_MISSING")
        self.assertTrue(record_stage_run(self.service, HR, run_id="UC02-EXE-1", stage="plan_tasks",
                                         operation="initial_task_planning", outcome="blocked", case_id=CASE,
                                         record_exists=True, started_at="2026-11-05T00:00:00+00:00",
                                         code="UC02_CONFIG_MISSING"))
        (run,) = [r for r in self.db.tables["agent_runs"] if r["run_id"] == "UC02-EXE-1"]
        self.assertEqual((run["status"], run["client_id"], run["case_id"], run["error_summary"]),
                         ("exception", LIVE_CLIENT, CASE, "plan_tasks:UC02_CONFIG_MISSING"))
        (event,) = [r for r in self.db.tables["audit_events"] if r["action"] == "orchestration.plan_tasks"]
        self.assertEqual((event["outcome"], event["client_id"], event["detail"]["code"]),
                         ("blocked", LIVE_CLIENT, "UC02_CONFIG_MISSING"))
        # the same execution recorded again is the same run row (idempotent by the execution id)
        record_stage_run(self.service, HR, run_id="UC02-EXE-1", stage="plan_tasks", operation="initial_task_planning",
                         outcome="blocked", case_id=CASE, record_exists=True, started_at="2026-11-05T00:00:00+00:00",
                         code="UC02_CONFIG_MISSING")
        self.assertEqual(len([r for r in self.db.tables["agent_runs"] if r["run_id"] == "UC02-EXE-1"]), 1)

    def test_a_case_the_database_does_not_hold_is_referenced_not_linked(self) -> None:
        from onboarding_task_management.sub_agent.orchestration_support import record_stage_event

        missing = "11111111-1111-4111-8111-111111111111"
        record_stage_event(self.service, HR, stage="plan_tasks", outcome="blocked", case_id=missing,
                           record_exists=False, code="UC02_CASE_NOT_FOUND")
        (event,) = [r for r in self.db.tables["audit_events"] if r["action"] == "orchestration.plan_tasks"]
        self.assertIsNone(event["case_id"])
        self.assertEqual(event["detail"]["case_ref"], missing)

    def test_the_projection_is_the_authoritative_record(self) -> None:
        from onboarding_task_management.sub_agent.orchestration_support import plan_projection, store_mode

        record = self.service.get(HR, self.oid)
        required = [record.tasks[0].task_id, "not-a-task-of-this-plan"]
        projection = plan_projection(record, required_task_ids=required, store=store_mode(self.service))
        self.assertEqual((projection["plan_version"], projection["task_count"], projection["store"]),
                         (1, len(record.tasks), "supabase"))
        self.assertEqual(projection["required_task_count"], 1)  # never counts a task the plan does not hold
        self.assertEqual(projection["dependency_count"], sum(len(t.depends_on) for t in record.tasks))
        stored = {(e["task_id"], e["depends_on_task_id"]) for e in self.db.tables["task_dependencies"]
                  if e["client_id"] == LIVE_CLIENT}
        self.assertEqual({(d["task_id"], d["depends_on_task_id"]) for d in projection["dependencies"]}, stored)


class OutageTests(unittest.TestCase):
    def test_a_server_error_or_an_unreachable_database_is_a_retryable_outage(self) -> None:
        for status in (500, 503, 429):
            rest = SupabaseRestClient("https://example.supabase.co", "k", transport=lambda *a, s=status: (s, {"message": "x"}))
            with self.assertRaises(SupabaseUnavailableError):
                rest.select("onboarding_tasks", {"client_id": LIVE_CLIENT})
        refused = SupabaseRestClient("https://example.supabase.co", "k", transport=lambda *a: (400, {"message": "bad"}))
        with self.assertRaises(PersistenceError) as caught:
            refused.select("onboarding_tasks", {"client_id": LIVE_CLIENT})
        self.assertNotIsInstance(caught.exception, SupabaseUnavailableError)  # a refusal is not an outage

    def test_the_error_never_carries_the_key(self) -> None:
        rest = SupabaseRestClient("https://example.supabase.co", "secret-key-value", transport=lambda *a: (503, None))
        with self.assertRaises(SupabaseUnavailableError) as caught:
            rest.select("plan_revisions", {"client_id": LIVE_CLIENT})
        self.assertNotIn("secret-key-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
