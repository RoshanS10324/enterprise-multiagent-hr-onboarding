"""The workflow state (documents, downstream tracks, ledger) persists through the Supabase adapter and recovers.

Uses the in-process PostgREST stand-in that enforces the live schema's keys and CHECKs. This is NOT a live database test.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from onboarding_task_management import synthetic_profiles as sp
from onboarding_task_management.contracts import Actor, DocumentState, PlanStatus, Role, SourceUseCase
from onboarding_task_management.persistence import OnboardingStore
from onboarding_task_management.sub_agent import flow as fl
from onboarding_task_management.sub_agent.checklist import ConfigRegistry
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.supabase_rest import SupabaseRestClient
from onboarding_task_management.supabase_store import STATE_ACTION, SupabaseTableClient
from tests.test_onboarding_flow import MockEmployeeSource, approve_cb, doc_update, event_status
from tests.test_supabase_store import CASE, LIVE_CLIENT, FakePostgrest, foreign_task

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
SECRET = b"synthetic-test-secret-not-real"
LIVE_EMP = "EMP-UC02-TEST-0001"
COORD = Actor(actor_id="uc02-coordinator", role=Role.HR_OPS, client_id=LIVE_CLIENT)
DOCS = Actor(actor_id="svc-docs-mock", role=Role.SYSTEM, client_id=LIVE_CLIENT)
UC04_A = Actor(actor_id="svc-uc04-mock", role=Role.SYSTEM, client_id=LIVE_CLIENT)
PRINCIPALS = {SourceUseCase.UC03_DOCUMENT_VALIDATION: frozenset({"svc-docs-mock"}),
              SourceUseCase.UC04_EQUIPMENT_ACCESS: frozenset({"svc-uc04-mock"}),
              SourceUseCase.UC05_ORIENTATION: frozenset({"svc-uc05-mock"})}


def build(db: FakePostgrest, source: MockEmployeeSource):
    store = OnboardingStore(SupabaseTableClient(SupabaseRestClient("https://example.supabase.co", "k", transport=db)))
    registry = ConfigRegistry()
    registry.register(sp.uae_config().model_copy(update={"client_id": LIVE_CLIENT}))  # SYNTHETIC, in-process only
    service = OnboardingService(registry, store=store, require_plan_approval=True, source_principals=PRINCIPALS)
    return fl.OnboardingFlow(service, source, coordinator=COORD, callback_secret=SECRET), service


class FlowOnSupabaseAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = FakePostgrest()
        self.db.seed_case(employee=LIVE_EMP)
        self.db.tables["onboarding_tasks"].append(foreign_task())  # another use case's task on the same case
        self.source = MockEmployeeSource()
        self.source.add("uae_inside", case_id=CASE, client_id=LIVE_CLIENT, employee_id=LIVE_EMP)
        self.flow, self.service = build(self.db, self.source)
        self.event = fl.EmployeeCreatedEvent(event_id="E1", client_id=LIVE_CLIENT, employee_id=LIVE_EMP, case_id=CASE,
                                             joining_date=sp.JOINING, occurred_at=NOW, correlation_id="C1")

    def run_to_gate(self):
        oid = self.flow.handle_employee_created(self.event, NOW.date()).onboarding_id
        self.flow.generate_review(COORD, oid, NOW.date())
        request = self.service.submit_plan(COORD, oid)
        out = self.flow.process_approval_callback(
            LIVE_CLIENT, approve_cb(oid, request.request_id, approver="U-APPR").model_copy(update={"signature": ""}), NOW)
        self.assertEqual(out.status, "invalid_signature")
        cb = fl.sign_callback(SECRET, approve_cb(oid, request.request_id, approver="U-APPR", sign=False))
        self.assertEqual(self.flow.process_approval_callback(LIVE_CLIENT, cb, NOW).status, "approved")
        return oid

    def test_the_onboarding_is_the_uc01_case_and_foreign_rows_are_untouched(self) -> None:
        cases_before = json.dumps(self.db.tables["onboarding_cases"], sort_keys=True)
        foreign_before = json.dumps(foreign_task(), sort_keys=True)
        oid = self.run_to_gate()
        self.assertEqual(oid, CASE)
        self.assertEqual(json.dumps(self.db.tables["onboarding_cases"], sort_keys=True), cases_before)
        mine = [r for r in self.db.tables["onboarding_tasks"] if r["template_key"] == "doc_passport_verification"]
        self.assertEqual(json.dumps(mine[0], sort_keys=True), foreign_before)
        self.assertFalse([c for c in self.db.calls if "onboarding_cases" in c[1] and c[0] != "GET"])

    def test_documents_approval_and_workflow_state_are_stored_and_recovered_after_a_restart(self) -> None:
        oid = self.run_to_gate()
        keys = [d.key for d in self.service.get(COORD, oid).documents if d.mandatory]
        n = 0
        for k in keys:
            for state, ref in ((DocumentState.SUBMITTED, None), (DocumentState.UNDER_REVIEW, None),
                               (DocumentState.VERIFIED, f"V-{k}")):
                n += 1
                self.assertTrue(self.flow.apply_document_update(DOCS, doc_update(oid, k, state, n, n, ref).model_copy(update={"client_id": LIVE_CLIENT})).accepted)
        uc04, uc05 = fl.MockTrigger(fl.UC04), fl.MockTrigger(fl.UC05)
        self.assertEqual(self.flow.trigger_downstream(COORD, oid, {fl.UC04: uc04, fl.UC05: uc05}),
                         {fl.UC04: "triggered", fl.UC05: "triggered"})
        self.flow.apply_downstream_status(UC04_A, event_status(oid, fl.UC04, fl.DownstreamState.IN_PROGRESS, 1, 3
                                                               ).model_copy(update={"client_id": LIVE_CLIENT}))
        # ---- a brand-new process over the same database
        flow2, service2 = build(self.db, self.source)
        service2.hydrate(LIVE_CLIENT)
        record = service2.get(COORD, oid)
        self.assertEqual(record.plan_status, PlanStatus.APPROVED)
        self.assertTrue(all(d.state is DocumentState.VERIFIED for d in record.documents if d.mandatory))
        self.assertEqual(flow2.downstream_summary(COORD, oid), {fl.UC04: "in_progress", fl.UC05: "triggered"})
        again = fl.MockTrigger(fl.UC04)
        flow2.trigger_downstream(COORD, oid, {fl.UC04: again})
        self.assertEqual(again.payloads, [])  # no duplicate trigger after the restart
        self.assertEqual(flow2.document_gate(COORD, oid).open, True)
        self.assertNotIn("doc_passport_verification", {t.template_key for t in record.tasks})  # foreign task not adopted

    def test_the_state_is_in_the_append_only_snapshot_and_audit_holds_no_personal_data(self) -> None:
        oid = self.run_to_gate()
        snapshots = [r for r in self.db.tables["audit_events"] if r["action"] == STATE_ACTION and r["case_id"] == oid]
        latest = max(snapshots, key=lambda r: r["after_state"]["updated_at"])["after_state"]
        self.assertTrue(latest["documents"] and "downstream" in latest and "flow_ledger" in latest)
        self.assertTrue(any(k.startswith("cb:") for k in latest["flow_ledger"]))
        text = json.dumps(self.db.tables["audit_events"]).lower()
        for word in ("passport_number", "salary", "candidate_name", "@"):
            self.assertNotIn(word, text)
        self.assertEqual([r for r in self.db.tables["approval_requests"] if r["outcome"] == "APPROVED"][0]["case_id"], oid)

    def test_a_replayed_trigger_event_after_restart_is_idempotent(self) -> None:
        oid = self.run_to_gate()
        flow2, service2 = build(self.db, self.source)
        service2.hydrate(LIVE_CLIENT)
        again = flow2.handle_employee_created(self.event.model_copy(update={"event_id": "E-REPLAY"}), NOW.date())
        self.assertEqual((again.created, again.onboarding_id), (False, oid))
        owned = [r for r in self.db.tables["onboarding_tasks"] if (r.get("status_summary") or {}).get("uc02") is not None]
        self.assertEqual(len(owned), len({r["task_id"] for r in owned}))


if __name__ == "__main__":
    unittest.main()
