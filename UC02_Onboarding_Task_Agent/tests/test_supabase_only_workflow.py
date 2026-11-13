"""The complete synthetic workflow on the Supabase-only runtime, over a MOCKED Supabase transport.

The stand-in database enforces the live schema's columns, NOT NULLs, foreign keys, unique keys and CHECK rules (captured from the
live schema), and ``InMemoryTableClient`` is made to fail if anything constructs it, so these tests prove the whole path uses the
Supabase adapter. They do not prove the real database accepts every write: that needs the approved live run described in
docs/UC02_RUNTIME_AND_ACTIVATION.md and the demo's ``preflight`` / ``setup`` / ``run`` stages.
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from fastapi.testclient import TestClient

from onboarding_task_management.errors import MissingConfigurationError, PreconditionError
from onboarding_task_management.persistence import InMemoryTableClient, PersistenceError
from onboarding_task_management.sub_agent import api as api_module
from onboarding_task_management.sub_agent import supabase_demo as demo
from onboarding_task_management.supabase_rest import SupabaseRestClient
from onboarding_task_management.supabase_store import SupabaseTableClient
from onboarding_task_management.supabase_synthetic import FixtureSpec, build_rows
from tests.test_supabase_store import SCHEMA, FakePostgrest

HOST = "example.supabase.co"
SPEC = FixtureSpec(joining_date=date(2026, 12, 14))
ENV = {
    "UC02_STORE_MODE": "supabase", "SUPABASE_URL": f"https://{HOST}", "SUPABASE_SERVICE_ROLE_KEY": "synthetic-key",
    "UC02_ENV": "development", "UC02_ALLOW_FIXTURE_WRITES": "1", "UC02_APPROVAL_CALLBACK_SECRET": "cb-secret-synthetic",
    "UC02_EVENT_SECRET": "evt-secret-synthetic", "UC02_DOCUMENTS_SECRET": "doc-secret-synthetic",
    "UC02_UC04_STATUS_SECRET": "uc04-secret-synthetic", "UC02_UC05_STATUS_SECRET": "uc05-secret-synthetic",
}


def fresh():
    db = FakePostgrest()
    rest = SupabaseRestClient(f"https://{HOST}", "synthetic-key", transport=db)
    return db, rest


def prepared(approve=True, mock_downstream=True):
    """A database with the synthetic fixture, the event processed, and (optionally) HR's approval recorded."""
    db, rest = fresh()
    demo.setup(ENV, rest=rest, spec=SPEC, confirm_target=HOST)
    demo.run(ENV, rest=rest, spec=SPEC, mock_downstream=mock_downstream)
    if approve:
        demo.decide(ENV, rest=rest, spec=SPEC, decision="approved", mock_downstream=mock_downstream)
    return db, rest


class NoMemoryTestCase(unittest.TestCase):
    """Fails any test in which the in-memory table client is constructed."""

    def setUp(self) -> None:
        patcher = mock.patch.object(InMemoryTableClient, "__init__", side_effect=AssertionError("InMemoryTableClient used"))
        patcher.start()
        self.addCleanup(patcher.stop)


class RuntimeSelectionTests(NoMemoryTestCase):
    def test_every_stage_refuses_without_supabase_mode(self) -> None:
        db, rest = fresh()
        for mode in (None, "memory", "sqlite", ""):
            env = {k: v for k, v in ENV.items() if k != "UC02_STORE_MODE"}
            if mode is not None:
                env["UC02_STORE_MODE"] = mode
            for stage in (demo.preflight, demo.run, demo.verify):
                with self.subTest(mode=mode, stage=stage.__name__):
                    with self.assertRaises(MissingConfigurationError) as ctx:
                        stage(env, rest=rest, spec=SPEC)
                    self.assertIn("UC02_STORE_MODE=supabase", ctx.exception.message)
        self.assertEqual(db.calls, [])  # nothing was even attempted

    def test_missing_credentials_unreachable_database_and_incomplete_schema_fail_immediately(self) -> None:
        no_creds = {k: v for k, v in ENV.items() if k not in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")}
        with self.assertRaises(PersistenceError):
            demo.run(no_creds, spec=SPEC)
        db, rest = fresh()
        db.down = True
        with self.assertRaises(PersistenceError):
            demo.run(ENV, rest=rest, spec=SPEC)
        db2, rest2 = fresh()
        original = SCHEMA["onboarding_tasks"]["columns"].pop("status_summary")
        try:
            with self.assertRaises(PersistenceError) as ctx:
                demo.run(ENV, rest=rest2, spec=SPEC)
            self.assertIn("status_summary", str(ctx.exception.__dict__))
        finally:
            SCHEMA["onboarding_tasks"]["columns"]["status_summary"] = original

    def test_a_failed_database_write_stops_the_workflow_and_is_not_reported_as_success(self) -> None:
        db, rest = fresh()
        demo.setup(ENV, rest=rest, spec=SPEC, confirm_target=HOST)
        db.fail_tables = {"onboarding_tasks"}
        with self.assertRaises(PersistenceError):
            demo.run(ENV, rest=rest, spec=SPEC)
        stored = [r for r in db.tables["onboarding_tasks"] if (r.get("status_summary") or {}).get("uc02") is not None]
        self.assertEqual(stored, [])

    def test_the_flow_is_built_on_the_supabase_adapter_and_never_on_memory(self) -> None:
        db, rest = fresh()
        flow = demo.make_flow(ENV, rest=rest)
        self.assertIsInstance(flow.service.store.client, SupabaseTableClient)
        self.assertIsNotNone(flow.source)

    def test_the_production_runtime_refuses_the_synthetic_demo_and_synthetic_configuration(self) -> None:
        db, rest = fresh()
        from onboarding_task_management.config_safety import UnsafeConfigurationError

        with self.assertRaises(UnsafeConfigurationError):  # production + the demo's synthetic opt-in is an unsafe combination, refused at startup
            demo.make_flow({**ENV, "UC02_ENV": "production"}, rest=rest)
        demo.setup(ENV, rest=rest, spec=SPEC, confirm_target=HOST)
        from onboarding_task_management.sub_agent.runtime import build_flow

        prod = build_flow({**{k: v for k, v in ENV.items() if k != "UC02_ALLOW_SYNTHETIC_CONFIG"}, "UC02_ENV": "production"},
                          rest=rest)
        from onboarding_task_management.supabase_config import SupabaseConfigSource

        with self.assertRaises(MissingConfigurationError):  # a runtime that did not opt in rejects the synthetic client
            SupabaseConfigSource(rest).load(SPEC.client_id, SPEC.joining_date)
        self.assertFalse(prod.service.registry.source.allow_synthetic)


class PreflightAndSetupTests(NoMemoryTestCase):
    def test_preflight_only_reads_and_reports_the_backend_target_and_fixture(self) -> None:
        db, rest = fresh()
        report = demo.preflight(ENV, rest=rest, spec=SPEC)
        self.assertEqual((report["backend"], report["target_host"], report["ready_to_run"]), ("supabase", HOST, True))
        self.assertTrue(all(v["present"] == 0 and v["missing"] > 0 for v in report["fixture"].values()))
        self.assertEqual({m for m, _ in db.calls}, {"GET"})

    def test_setup_needs_the_target_host_and_the_development_opt_ins(self) -> None:
        db, rest = fresh()
        with self.assertRaises(PersistenceError):
            demo.setup(ENV, rest=rest, spec=SPEC, confirm_target="")
        with self.assertRaises(PersistenceError):
            demo.setup(ENV, rest=rest, spec=SPEC, confirm_target="somewhere-else.supabase.co")
        no_opt_in = {k: v for k, v in ENV.items() if k != "UC02_ALLOW_FIXTURE_WRITES"}
        with self.assertRaises(PreconditionError):
            demo.setup(no_opt_in, rest=rest, spec=SPEC, confirm_target=HOST)
        self.assertEqual([m for m, _ in db.calls if m != "GET"], [])

    def test_setup_inserts_missing_rows_only_marks_them_synthetic_and_never_updates_or_deletes(self) -> None:
        db, rest = fresh()
        first = demo.setup(ENV, rest=rest, spec=SPEC, confirm_target=HOST)
        self.assertTrue(all(v["inserted"] > 0 for v in first["fixture"].values()))
        again = demo.setup(ENV, rest=rest, spec=SPEC, confirm_target=HOST)
        self.assertTrue(all(v["inserted"] == 0 for v in again["fixture"].values()))
        self.assertEqual({m for m, _ in db.calls} - {"GET", "POST"}, set())  # no PATCH, no DELETE
        client = db.tables["client_configs"][0]
        self.assertEqual(client["settings_payload"]["data_origin"], "synthetic")
        self.assertIn("SYNTHETIC", client["company_name"])
        self.assertEqual(db.tables["client_config_versions"][0]["status"], "approved")

    def test_an_existing_record_is_left_untouched(self) -> None:
        db, rest = fresh()
        demo.setup(ENV, rest=rest, spec=SPEC, confirm_target=HOST)
        db.tables["client_configs"][0]["company_name"] = "CHANGED BY SOMEONE ELSE"
        demo.setup(ENV, rest=rest, spec=SPEC, confirm_target=HOST)
        self.assertEqual(db.tables["client_configs"][0]["company_name"], "CHANGED BY SOMEONE ELSE")

    def test_the_fixture_rows_fit_the_live_schema(self) -> None:
        db, rest = fresh()
        for table, rows in build_rows(SPEC).items():
            for row in rows:
                db.check(table, dict(row), full=False)  # no unknown columns


class WorkflowOnSupabaseTests(NoMemoryTestCase):
    def test_run_stores_the_whole_review_stage_in_supabase_and_marks_it_synthetic(self) -> None:
        db, rest = prepared(approve=False)
        report = demo.verify(ENV, rest=rest, spec=SPEC)
        self.assertEqual((report["backend"], report["data_origin"], report["plan_status"]), ("supabase", "synthetic", "submitted"))
        self.assertEqual(report["config_version"], "1")  # read from client_config_versions, not an in-process registry
        self.assertGreater(report["tasks_stored"], 5)
        self.assertGreater(report["dependencies_stored"], 3)
        self.assertEqual(report["approval_requests"], [("plan_approval", "PENDING")])
        self.assertGreaterEqual(report["agent_runs"], 1)
        self.assertGreater(report["audit_events"], 3)
        self.assertEqual(report["reviews_stored"], ["p1"])
        self.assertIn(("hrmail", 1), report["messages"])
        self.assertTrue(any(k.startswith("ev:") for k in report["event_ledger"]))
        self.assertEqual(report["uc01_tables_written_by_uc02"], 0)

    def test_uc02_writes_nothing_to_uc01s_tables_outside_the_setup_stage(self) -> None:
        db, rest = fresh()
        demo.setup(ENV, rest=rest, spec=SPEC, confirm_target=HOST)
        mark = len(db.calls)
        demo.run(ENV, rest=rest, spec=SPEC)
        demo.decide(ENV, rest=rest, spec=SPEC, decision="approved")
        demo.documents(ENV, rest=rest, spec=SPEC)
        writes = [(m, p) for m, p in db.calls[mark:] if m != "GET"]
        touched = {p.split("/")[-1].split("?")[0] for _, p in writes}
        for uc01 in ("onboarding_cases", "onboarding_forms", "offers", "offer_forms"):
            self.assertNotIn(uc01, touched)
        self.assertTrue({"onboarding_tasks", "approval_requests", "audit_events", "agent_runs"} <= touched)

    def test_the_fake_llm_review_is_stored_labelled_and_never_approves(self) -> None:
        db, rest = prepared(approve=False)
        flow = demo.make_flow(ENV, rest=rest)
        flow.service.hydrate(SPEC.client_id)
        record = flow.service.get(flow._actor(SPEC.client_id), SPEC.case_id)
        analysis = record.analyses["p1"]
        self.assertTrue(analysis["source"].startswith("fake_llm:"))
        self.assertTrue(analysis["advisory_only"])
        self.assertEqual(record.plan_status.value, "submitted")  # the review changed nothing and approved nothing

    def test_the_checklist_comes_from_the_supabase_configuration_and_differs_by_profile(self) -> None:
        db, rest = prepared(approve=False)
        inside = {t["template_key"] for t in db.tables["onboarding_tasks"] if (t.get("status_summary") or {}).get("uc02") is not None}
        self.assertIn("visa_status_change", inside)
        self.assertNotIn("entry_permit", inside)  # the in-country route, chosen by the stored attribute
        assignments = [r for r in db.tables["client_service_policies"] if r["service_category"] == "uc02_assignment"]
        self.assertTrue(assignments)  # the team mapping the config reader used

    def test_the_hr_email_is_a_labelled_simulated_draft_and_nothing_is_sent(self) -> None:
        db, rest = prepared(approve=False)
        flow = demo.make_flow(ENV, rest=rest)
        flow.service.hydrate(SPEC.client_id)
        actor = flow._actor(SPEC.client_id)
        events = [e for e in flow.service.audit.events_for(actor) if e.action in ("hrmail.prepared", "hrmail.draft_saved")]
        self.assertEqual({e.action for e in events}, {"hrmail.prepared", "hrmail.draft_saved"})
        saved = next(e for e in events if e.action == "hrmail.draft_saved")
        self.assertEqual((saved.detail["real"], saved.detail["connector"]), ("False", "MockOutlookConnector"))
        self.assertFalse([e for e in flow.service.audit.events_for(actor) if e.action == "hrmail.result"])

    def test_approval_prepares_assignments_and_documents_and_changes_nothing_downstream(self) -> None:
        db, rest = prepared()
        report = demo.verify(ENV, rest=rest, spec=SPEC)
        self.assertEqual(report["plan_status"], "approved")
        self.assertGreater(report["assignments"], 3)
        self.assertTrue(report["documents"] and set(report["documents"].values()) == {"pending"})
        self.assertEqual(set(report["downstream"].values()), {"waiting_for_documents"})
        self.assertEqual([a[1] for a in report["approval_requests"]], ["APPROVED"])

    def test_rejection_needs_a_reason_and_stops_everything(self) -> None:
        db, rest = prepared(approve=False)
        refused = demo.decide(ENV, rest=rest, spec=SPEC, decision="rejected", reason="")
        self.assertEqual(refused["outcome"], "reason_required")
        done = demo.decide(ENV, rest=rest, spec=SPEC, decision="rejected", reason="synthetic: not acceptable")
        self.assertEqual((done["outcome"], done["workflow"]["stage"]), ("rejected", "rejected"))
        report = demo.verify(ENV, rest=rest, spec=SPEC)
        self.assertEqual((report["plan_status"], report["assignments"], report["downstream"]), ("rejected", 0, {}))
        later = demo.documents(ENV, rest=rest, spec=SPEC, mock_downstream=True)
        self.assertTrue(all(v != "applied" for v in later["updates"].values()))
        self.assertFalse(db.tables["onboarding_tasks"][0:0])  # (placeholder to keep the structure explicit)

    def test_a_change_request_creates_the_next_version_a_new_request_and_a_new_review_email(self) -> None:
        db, rest = prepared(approve=False)
        out = demo.decide(ENV, rest=rest, spec=SPEC, decision="changes_requested", reason="synthetic: adjust dates")
        self.assertEqual(out["outcome"], "changes_requested")
        report = demo.verify(ENV, rest=rest, spec=SPEC)
        self.assertEqual((report["plan_version"], report["plan_status"]), (2, "submitted"))
        self.assertEqual(sorted(v for k, v in report["messages"] if k == "hrmail"), [1, 2])
        self.assertEqual(sorted(a[1] for a in report["approval_requests"]), ["PENDING", "REJECTED"])
        approved = demo.decide(ENV, rest=rest, spec=SPEC, decision="approved")
        self.assertEqual(approved["outcome"], "approved")

    def test_the_mandatory_document_gate_keeps_downstream_blocked_until_every_document_is_verified(self) -> None:
        db, rest = prepared()
        rejected = demo.documents(ENV, rest=rest, spec=SPEC, mock_downstream=True, verify_all=False,
                                  reject="identity")
        self.assertFalse(rejected["gate"]["open"])
        self.assertEqual(set(rejected["workflow"]["downstream"].values()), {"waiting_for_documents"})
        done = demo.documents(ENV, rest=rest, spec=SPEC, mock_downstream=True)
        self.assertEqual(done["gate"]["rejected"], ["identity"])  # a rejected mandatory document still blocks
        self.assertFalse(done["gate"]["open"])

    def test_verifying_every_mandatory_document_opens_the_gate_and_triggers_both_use_cases_once(self) -> None:
        db, rest = prepared()
        done = demo.documents(ENV, rest=rest, spec=SPEC, mock_downstream=True)
        self.assertTrue(done["gate"]["open"])
        self.assertEqual(done["workflow"]["downstream"], {"UC-04": "triggered", "UC-05": "triggered"})
        flow = demo.make_flow(ENV, rest=rest, mock_downstream=True)
        flow.service.hydrate(SPEC.client_id)
        flow.trigger_downstream(flow._actor(SPEC.client_id), SPEC.case_id, flow.downstream_adapters)
        for adapter in flow.downstream_adapters.values():
            self.assertEqual(adapter.payloads, [])  # already triggered: a later pass sends nothing

    def test_without_configured_downstream_endpoints_the_use_cases_are_not_configured_never_triggered(self) -> None:
        db, rest = prepared(mock_downstream=False)
        done = demo.documents(ENV, rest=rest, spec=SPEC, mock_downstream=False)
        self.assertTrue(done["gate"]["open"])
        self.assertEqual(done["workflow"]["downstream"], {"UC-04": "not_configured", "UC-05": "not_configured"})
        self.assertEqual(done["workflow"]["stage"], "gate_open")
        self.assertTrue(any("not_configured" in b for b in done["workflow"]["blocked_because"]))

    def test_authorised_messages_go_through_the_connector_and_are_labelled_simulated(self) -> None:
        db, rest = prepared()
        sent = demo.send_authorised(ENV, rest=rest, spec=SPEC)
        kinds = {m["kind"] for m in sent["messages"].values()}
        self.assertTrue({"hrmail", "docreq", "teamasg", "hrnotice"} <= kinds)
        held = [m for m in sent["messages"].values() if m["status"] == "held"]
        self.assertTrue(held)  # UC-04 / UC-05 assignments wait for the document gate
        for m in sent["messages"].values():
            self.assertFalse(m["real"])
            self.assertEqual(m["connector"], "MockOutlookConnector")
        flow = demo.make_flow(ENV, rest=rest)
        flow.service.hydrate(SPEC.client_id)
        outcomes = {e.outcome for e in flow.service.audit.events_for(flow._actor(SPEC.client_id)) if e.action.endswith(".result")}
        self.assertEqual(outcomes, {"simulated_sent"})


class RecoveryAndIdempotencyTests(NoMemoryTestCase):
    def test_a_new_runtime_recovers_the_same_workflow_and_a_replayed_event_duplicates_nothing(self) -> None:
        db, rest = prepared()
        demo.documents(ENV, rest=rest, spec=SPEC, mock_downstream=True)
        before = demo.verify(ENV, rest=rest, spec=SPEC)
        report = demo.recover(ENV, rest=rest, spec=SPEC, mock_downstream=True)
        self.assertEqual(report["event_replay"], "duplicate")
        self.assertFalse(report["duplicate_tasks"])
        self.assertEqual(report["tasks_before_after"][0], report["tasks_before_after"][1])
        self.assertEqual(report["approvals_before_after"][0], report["approvals_before_after"][1])
        self.assertEqual(report["assignments_before_after"][0], report["assignments_before_after"][1])
        self.assertEqual(report["recovered_workflow"]["downstream"], {"UC-04": "triggered", "UC-05": "triggered"})
        self.assertEqual(report["recovered_workflow"]["plan_status"], "approved")
        after = demo.verify(ENV, rest=rest, spec=SPEC)
        self.assertEqual(after["documents"], before["documents"])
        self.assertEqual(after["messages"], before["messages"])

    def test_running_the_event_stage_twice_creates_one_workflow(self) -> None:
        db, rest = fresh()
        demo.setup(ENV, rest=rest, spec=SPEC, confirm_target=HOST)
        first = demo.run(ENV, rest=rest, spec=SPEC)
        tasks = len([r for r in db.tables["onboarding_tasks"] if (r.get("status_summary") or {}).get("uc02") is not None])
        second = demo.run(ENV, rest=rest, spec=SPEC)
        self.assertEqual((first["event"], second["event"]), ("accepted", "duplicate"))
        self.assertEqual(len([r for r in db.tables["onboarding_tasks"] if (r.get("status_summary") or {}).get("uc02") is not None]), tasks)
        self.assertEqual(len([r for r in db.tables["approval_requests"]]), 1)

    def test_a_missing_fixture_fails_clearly(self) -> None:
        db, rest = fresh()
        with self.assertRaises(PersistenceError) as ctx:
            demo.run(ENV, rest=rest, spec=SPEC)
        self.assertNotEqual(str(ctx.exception), "")

    def test_a_missing_event_secret_stops_the_run(self) -> None:
        db, rest = fresh()
        demo.setup(ENV, rest=rest, spec=SPEC, confirm_target=HOST)
        env = {k: v for k, v in ENV.items() if k != "UC02_EVENT_SECRET"}
        with self.assertRaises(PersistenceError):
            demo.run(env, rest=rest, spec=SPEC)


class ApiRuntimeSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = api_module._sub_agent
        api_module._sub_agent = None
        api_module.configure_flow(None)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        api_module._sub_agent = self._saved
        api_module.configure_flow(None)

    def test_the_api_refuses_to_run_a_workflow_without_supabase_and_health_says_so(self) -> None:
        import os

        saved = {k: os.environ.pop(k, None) for k in ("UC02_STORE_MODE", "PERSISTENCE_BACKEND")}
        try:
            http = TestClient(api_module.app)
            response = http.post("/v1/onboarding/workflow", json={"fixture": "01_valid_onboarding.json"})
            self.assertEqual(response.status_code, 503)
            self.assertIn("UC02_STORE_MODE=supabase", response.text)
            health = http.get("/health").json()["database"]
            self.assertFalse(health["configured"])
            self.assertIn("NOT RUNNING", health["note"])
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_with_supabase_selected_every_route_shares_the_one_supabase_backed_service(self) -> None:
        db, rest = fresh()
        with mock.patch.object(api_module, "build_runtime_service") as factory:
            from onboarding_task_management.sub_agent.runtime import build_runtime_service

            factory.side_effect = lambda env, **kw: build_runtime_service(ENV, rest=rest, **kw)
            first = api_module.get_sub_agent()
            self.assertIs(first, api_module.get_sub_agent())
            self.assertIsInstance(first.service.store.client, SupabaseTableClient)
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(TestClient(api_module.app).get("/health").json()["database"]["engine"], "supabase")


if __name__ == "__main__":
    unittest.main()
