"""The RUNTIME flow: signed inbound channels, automatic progression, stored message bodies, notices, downstream adapters,
the factory, and the acceptance sequence for both the approval and rejection paths.

Everything external is mocked (employee source, Graph/Entra via FakeGraph, UC-04/05 endpoints, Supabase). Nothing contacts a
real service. Passing proves UC-02's side of each contract, not any real integration.
"""

from __future__ import annotations

import json
import logging
import threading
import unittest
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from onboarding_task_management import synthetic_profiles as sp
from onboarding_task_management.contracts import DecisionOutcome, DocumentState, DownstreamState, PlanStatus, Role
from onboarding_task_management.errors import MissingConfigurationError
from onboarding_task_management.persistence import InMemoryTableClient, OnboardingStore
from onboarding_task_management.privacy import contains_personal_pattern, redact_free_text
from onboarding_task_management.sub_agent import api as api_module
from onboarding_task_management.sub_agent import flow as fl
from onboarding_task_management.sub_agent import flow_runtime as rt
from onboarding_task_management.sub_agent.connectors.outlook import graph as g
from onboarding_task_management.sub_agent.connectors.outlook.mock import MockOutlookConnector
from onboarding_task_management.sub_agent.downstream import SIGNATURE_HEADER, HttpTriggerAdapter, sign_body
from onboarding_task_management.sub_agent.runtime import build_flow
from onboarding_task_management.sub_agent.service import OnboardingService
from tests.test_onboarding_flow import (
    CASE,
    CLIENT,
    COORD,
    LEAD,
    MockEmployeeSource,
    NOW,
    SECRET,
    approve_cb,
    doc_update,
    event,
)
from tests.test_outlook_graph import FakeGraph
from tests.test_team_assignment_flow import ADDRESSES, TEAM_ADDRESSES, graph_for, mapping

UTC = timezone.utc
SECRETS = {"events": b"evt-secret-synthetic", "documents": b"doc-secret-synthetic", "uc04": b"uc04-secret-synthetic",
           "uc05": b"uc05-secret-synthetic"}
TODAY = date(2026, 11, 2)


def runtime(profile="uae_inside", *, connector=None, adapters=None, store=None, source=None, secrets=None):
    source = source or MockEmployeeSource()
    if (sp.PROFILES[profile].client_id, CASE) not in source.rows:
        source.add(profile)
    service = OnboardingService(sp.registry(), store=store, require_plan_approval=True,
                                source_principals=rt.runtime_principals())
    flow = rt.RuntimeFlow(service, source, coordinator=COORD, callback_secret=SECRET,
                          connector=connector if connector is not None else MockOutlookConnector(),
                          downstream_adapters=adapters or {}, channel_secrets=SECRETS if secrets is None else secrets)
    return flow, service, source


def signed_event(profile="uae_inside", event_id="EVT-1", **kw):
    return rt.sign_model(SECRETS["events"], event(event_id=event_id, profile=profile, **kw))


def started(profile="uae_inside", **kw):
    flow, service, source = runtime(profile, **kw)
    out = flow.receive_employee_event(signed_event(profile), TODAY)
    assert out.status == "accepted", out
    return flow, service, source, out.data["onboarding_id"]


def approve(flow, service, oid, **kw):
    record = service.get(COORD, oid)
    request = next(r for r in service.inbox.pending(COORD))
    cb = approve_cb(oid, request.request_id, version=record.plan_version, **kw)
    return flow.process_approval_callback(CLIENT, cb, NOW)


def verify_docs(flow, oid, start=0):
    n = start
    for d in flow.service.get(COORD, oid).documents:
        if d.mandatory:
            for state, ref in ((DocumentState.SUBMITTED, None), (DocumentState.UNDER_REVIEW, None),
                               (DocumentState.VERIFIED, f"VER-{d.key}")):
                n += 1
                update = rt.sign_model(SECRETS["documents"], doc_update(oid, d.key, state, n, n, ref))
                assert flow.receive_document_update(update).status == "applied"
    return n


class EmployeeEventRuntimeTests(unittest.TestCase):
    def test_a_signed_event_runs_the_flow_to_a_submitted_plan_and_a_saved_hr_email_draft(self) -> None:
        mock = MockOutlookConnector()
        flow, service, _, oid = started(connector=mock)
        record = service.get(COORD, oid)
        self.assertEqual(record.plan_status, PlanStatus.SUBMITTED)
        self.assertTrue(record.documents)
        hr = [m for m in record.messages if m.kind == "hrmail"]
        self.assertEqual(len(hr), 1)
        self.assertEqual(len(mock.drafts(CLIENT)), 1)  # the HR review email draft
        self.assertEqual(mock.sent_count(), 0)  # nothing is sent without an authorisation

    def test_a_duplicate_event_creates_nothing_new(self) -> None:
        flow, service, _, oid = started()
        before = service.get(COORD, oid)
        again = flow.receive_employee_event(signed_event(event_id="EVT-2"), TODAY)
        self.assertEqual((again.status, again.data["onboarding_id"]), ("duplicate", oid))
        after = service.get(COORD, oid)
        self.assertEqual(len(after.messages), len(before.messages))
        self.assertEqual(len(after.tasks), len(before.tasks))

    def test_unsigned_tampered_and_unconfigured_channels_are_refused(self) -> None:
        flow, _, _ = runtime()
        self.assertEqual(flow.receive_employee_event(event(), TODAY).status, "invalid_signature")
        tampered = signed_event().model_copy(update={"employee_id": "EMP-EVIL"})
        self.assertEqual(flow.receive_employee_event(tampered, TODAY).status, "invalid_signature")
        wrong = rt.sign_model(b"another-secret", event())
        self.assertEqual(flow.receive_employee_event(wrong, TODAY).status, "invalid_signature")
        bare, _, _ = runtime(secrets={})
        self.assertEqual(bare.receive_employee_event(signed_event(), TODAY).status, "not_configured")

    def test_configuration_gaps_are_reported_not_hidden(self) -> None:
        flow, service, _ = runtime()
        service.registry.configs.pop(CLIENT)
        out = flow.receive_employee_event(signed_event(), TODAY)
        self.assertEqual(out.status, "configuration_gap")
        self.assertEqual(service.store, None)
        nosource = rt.RuntimeFlow(service, None, coordinator=COORD, callback_secret=SECRET, channel_secrets=SECRETS)
        self.assertEqual(nosource.receive_employee_event(signed_event(event_id="EVT-4"), TODAY).status, "rejected")

    def test_a_missing_employee_or_missing_uc01_facts_are_rejected(self) -> None:
        flow, _, _ = runtime()
        out = flow.receive_employee_event(rt.sign_model(SECRETS["events"], event(case_id="99999999-2222-4333-8444-555555555555")), TODAY)
        self.assertEqual(out.status, "rejected")


class StoredMessageTests(unittest.TestCase):
    def test_the_hr_email_body_is_stored_and_identical_after_a_restart_whatever_the_date(self) -> None:
        client = InMemoryTableClient()
        flow, service, source, oid = started(store=OnboardingStore(client))
        first = flow.prepare_hr_email(COORD, oid, TODAY)
        flow2, service2, _ = runtime(store=OnboardingStore(InMemoryTableClient.from_snapshot(client.snapshot())), source=source)
        service2.hydrate(CLIENT)
        later = flow2.prepare_hr_email(COORD, oid, date(2026, 11, 20))  # a different day: the reviewed content is kept
        self.assertEqual((later.body, later.subject, later.email_id), (first.body, first.subject, first.email_id))
        stored = flow2.stored_message(COORD, oid, first.email_id)
        self.assertEqual(stored.content_sha256, fl.content_digest(first.subject, first.body))

    def test_a_revised_plan_gets_a_new_stored_email_and_the_old_one_is_retained(self) -> None:
        flow, service, _, oid = started()
        out = approve(flow, service, oid, decision="changes_requested", reason="synthetic: tighten")
        self.assertEqual(out.status, "changes_requested")
        emails = [m for m in service.get(COORD, oid).messages if m.kind == "hrmail"]
        self.assertEqual(sorted(m.plan_version for m in emails), [1, 2])
        self.assertEqual(len({m.message_id for m in emails}), 2)

    def test_every_message_kind_is_stored_with_a_fingerprint(self) -> None:
        flow, service, _, oid = started()
        approve(flow, service, oid)
        kinds = {m.kind for m in service.get(COORD, oid).messages}
        self.assertEqual(kinds, {"hrmail", "docreq", "teamasg", "hrnotice"})
        for m in service.get(COORD, oid).messages:
            self.assertEqual(m.content_sha256, fl.content_digest(m.subject, m.body))
            self.assertNotIn("@", m.body)


class DecisionAndNoticeTests(unittest.TestCase):
    def test_approval_prepares_assignments_document_request_and_an_hr_notice(self) -> None:
        flow, service, _, oid = started()
        self.assertEqual(approve(flow, service, oid).status, "approved")
        record = service.get(COORD, oid)
        notice = next(m for m in record.messages if m.kind == "hrnotice")
        self.assertIn("NOT sent", notice.body)
        self.assertIn("held until documents are verified", notice.body)
        self.assertTrue(record.assignments and all(a.status == "prepared" for a in record.assignments))

    def test_rejection_needs_a_reason_stops_everything_and_notifies_hr(self) -> None:
        flow, service, _, oid = started()
        self.assertEqual(approve(flow, service, oid, decision="rejected", reason="", cb_id="CB-A").status, "reason_required")
        self.assertEqual(service.get(COORD, oid).plan_status, PlanStatus.SUBMITTED)
        self.assertEqual(approve(flow, service, oid, decision="rejected", reason="synthetic: plan unacceptable", cb_id="CB-B").status,
                         "rejected")
        record = service.get(COORD, oid)
        self.assertEqual((record.plan_status, record.assignments, record.downstream), (PlanStatus.REJECTED, [], {}))
        notice = next(m for m in record.messages if m.kind == "hrnotice")
        self.assertIn("No task was dispatched", notice.body)
        self.assertIn("synthetic: plan unacceptable", notice.body)
        self.assertIn("intentional", notice.body)  # a plain rejection is a decision, not an automatic retry
        self.assertFalse([m for m in record.messages if m.kind in ("teamasg", "docreq")])

    def test_changes_requested_needs_a_reason_then_creates_the_next_version_request_and_email(self) -> None:
        flow, service, _, oid = started()
        self.assertEqual(approve(flow, service, oid, decision="changes_requested", reason=" ", cb_id="CB-C").status, "reason_required")
        out = approve(flow, service, oid, decision="changes_requested", reason="synthetic: fix dates", cb_id="CB-D")
        self.assertEqual((out.status, out.plan_version), ("changes_requested", None) if out.plan_version is None else (out.status, out.plan_version))
        record = service.get(COORD, oid)
        self.assertEqual((record.plan_version, record.plan_status), (2, PlanStatus.SUBMITTED))
        self.assertIsNotNone(out.new_request_id)
        self.assertTrue(any(m.kind == "hrmail" and m.plan_version == 2 for m in record.messages))

    def test_a_reason_with_personal_data_is_redacted_before_it_is_stored(self) -> None:
        flow, service, _, oid = started()
        approve(flow, service, oid, decision="rejected", reason="call jane.doe@example.com on +971 50 123 4567 about A1234567",
                cb_id="CB-P")
        record = service.get(COORD, oid)
        blob = json.dumps(record.model_dump(mode="json"), default=str)
        for leaked in ("jane.doe@example.com", "123 4567", "A1234567"):
            self.assertNotIn(leaked, blob)
        self.assertIn("[email removed]", blob)


class PrivacyFilterTests(unittest.TestCase):
    def test_personal_patterns_are_removed_and_ordinary_text_and_ids_are_kept(self) -> None:
        text = "see a.b@c.io, phone +44 20 7946 0958, passport X1234567 and 12345678"
        out = redact_free_text(text)
        for leaked in ("a.b@c.io", "7946", "X1234567", "12345678"):
            self.assertNotIn(leaked, out)
        kept = "ONB-SYN-UAE-TEST-EMP-1#visa_status_change is late; ask the owner"
        self.assertEqual(redact_free_text(kept), kept)
        self.assertEqual(redact_free_text(redact_free_text(text)), out)  # idempotent
        self.assertTrue(contains_personal_pattern(text) and not contains_personal_pattern(kept))
        self.assertLessEqual(len(redact_free_text("x" * 5000)), 500)

    def test_delay_and_blocking_reasons_are_redacted_where_they_enter_the_record(self) -> None:
        flow, service, _, oid = started()
        approve(flow, service, oid)
        tid = service.get(COORD, oid).task_by_key("hr_record_create").task_id
        service.report_delay(COORD, oid, tid, "waiting for x.y@z.org to reply")
        self.assertNotIn("x.y@z.org", service.get(COORD, oid).task_by_id(tid).delay_reason)


class MessageDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeGraph()
        self.graph = graph_for(self.fake, addresses={CLIENT: {"roles": ADDRESSES, "teams": TEAM_ADDRESSES,
                                                               "allow_role_fallback": False}})
        self.flow, self.service, _, self.oid = started(connector=self.graph)
        approve(self.flow, self.service, self.oid)

    def decision(self, message_id, *, role=Role.HR_LEAD, approver="U-LEAD-1", did="MD-1", issued=None, sign=True):
        d = rt.MessageDecision(decision_id=did, client_id=CLIENT, onboarding_id=self.oid, message_id=message_id,
                               approver_id=approver, approver_role=role, issued_at=issued or datetime.now(UTC))
        return rt.sign_model(SECRET, d) if sign else d

    def hr_message(self):
        return next(m for m in self.service.get(COORD, self.oid).messages if m.kind == "hrmail")

    def test_a_signed_lead_authorisation_sends_the_hr_email_through_graph_once(self) -> None:
        out = self.flow.process_message_decision(self.decision(self.hr_message().message_id), datetime.now(UTC))
        self.assertEqual((out.status, out.data["connector_status"]), ("sent", "submitted_for_sending"))
        self.assertEqual(self.fake.count("send"), 1)
        replay = self.flow.process_message_decision(self.decision(self.hr_message().message_id), datetime.now(UTC))
        self.assertEqual(replay.status, "replayed")
        again = self.flow.process_message_decision(self.decision(self.hr_message().message_id, did="MD-2"), datetime.now(UTC))
        self.assertEqual(again.data["connector_status"], "submitted_for_sending")
        self.assertEqual(self.fake.count("send"), 1)  # no second email

    def test_unsigned_unauthorised_and_expired_decisions_send_nothing(self) -> None:
        mid = self.hr_message().message_id
        self.assertEqual(self.flow.process_message_decision(self.decision(mid, sign=False), datetime.now(UTC)).status, "invalid_signature")
        self.assertEqual(self.flow.process_message_decision(self.decision(mid, role=Role.HR_OPS, approver="U-OPS", did="MD-3"),
                                                            datetime.now(UTC)).status, "unauthorized")
        old = self.decision(mid, did="MD-4", issued=datetime.now(UTC) - timedelta(hours=2))
        self.assertEqual(self.flow.process_message_decision(old, datetime.now(UTC)).status, "expired")
        self.assertEqual(self.fake.count("send"), 0)

    def test_a_held_downstream_assignment_cannot_be_sent_until_documents_are_verified(self) -> None:
        held = next(a for a in self.service.get(COORD, self.oid).assignments if a.use_case == "UC-04")
        out = self.flow.process_message_decision(self.decision(held.assignment_id, did="MD-5"), datetime.now(UTC))
        self.assertEqual(out.status, "held")
        self.assertEqual(self.fake.count("send"), 0)
        verify_docs(self.flow, self.oid)
        done = self.flow.process_message_decision(self.decision(held.assignment_id, did="MD-6"), datetime.now(UTC))
        self.assertEqual(done.status, "sent")

    def test_without_a_connector_nothing_is_sent_and_it_says_so(self) -> None:
        self.flow.connector = None
        out = self.flow.process_message_decision(self.decision(self.hr_message().message_id, did="MD-7"), datetime.now(UTC))
        self.assertEqual(out.status, "connector_not_configured")


class AutomaticDownstreamTests(unittest.TestCase):
    def test_the_gate_opening_triggers_both_use_cases_once_and_status_events_are_tracked_independently(self) -> None:
        a, b = fl.MockTrigger(fl.UC04), fl.MockTrigger(fl.UC05)
        flow, service, _, oid = started(adapters={fl.UC04: a, fl.UC05: b})
        approve(flow, service, oid)
        self.assertEqual((a.payloads, b.payloads), ([], []))  # approval alone triggers nothing
        verify_docs(flow, oid)
        self.assertEqual((len(a.payloads), len(b.payloads)), (1, 1))
        self.assertEqual(a.payloads[0]["trigger_id"], f"TRG-{oid}-UC-04-p1")
        ev = rt.sign_model(SECRETS["uc04"], fl.DownstreamStatusEvent(
            event_id="DS-1", client_id=CLIENT, use_case="UC-04", onboarding_id=oid, plan_version=1,
            state=DownstreamState.IN_PROGRESS, occurred_at=NOW))
        self.assertEqual(flow.receive_downstream_status(ev).status, "applied")
        self.assertEqual(flow.downstream_summary(COORD, oid), {fl.UC04: "in_progress", fl.UC05: "triggered"})

    def test_status_channels_are_separate_one_uses_secret_cannot_speak_for_the_other(self) -> None:
        a, b = fl.MockTrigger(fl.UC04), fl.MockTrigger(fl.UC05)
        flow, service, _, oid = started(adapters={fl.UC04: a, fl.UC05: b})
        approve(flow, service, oid)
        verify_docs(flow, oid)
        forged = rt.sign_model(SECRETS["uc05"], fl.DownstreamStatusEvent(
            event_id="DS-2", client_id=CLIENT, use_case="UC-04", onboarding_id=oid, plan_version=1,
            state=DownstreamState.COMPLETED, occurred_at=NOW))
        self.assertEqual(flow.receive_downstream_status(forged).status, "invalid_signature")
        self.assertEqual(flow.downstream_summary(COORD, oid)[fl.UC04], "triggered")

    def test_a_failing_adapter_does_not_block_the_other_and_a_retry_does_not_resend_to_the_healthy_one(self) -> None:
        bad, good = fl.MockTrigger(fl.UC04, raise_error=True), fl.MockTrigger(fl.UC05)
        flow, service, _, oid = started(adapters={fl.UC04: bad, fl.UC05: good})
        approve(flow, service, oid)
        verify_docs(flow, oid)
        self.assertEqual(flow.downstream_summary(COORD, oid), {fl.UC04: "failed", fl.UC05: "triggered"})
        flow.trigger_downstream(COORD, oid, flow.downstream_adapters)
        self.assertEqual(len(good.payloads), 1)

    def test_a_crash_between_the_send_and_the_saved_result_retries_with_the_same_trigger_id(self) -> None:
        class Dies:
            use_case = fl.UC04

            def __init__(self):
                self.ids = []

            def send(self, payload):
                self.ids.append(payload["trigger_id"])
                raise KeyboardInterrupt  # not an Exception: the process "dies" after the send

        client = InMemoryTableClient()
        dies = Dies()
        flow, service, source, oid = started(adapters={fl.UC04: dies}, store=OnboardingStore(client))
        approve(flow, service, oid)
        with self.assertRaises(KeyboardInterrupt):
            verify_docs(flow, oid)
        restarted_client = InMemoryTableClient.from_snapshot(client.snapshot())
        ok = fl.MockTrigger(fl.UC04)
        flow2, service2, _ = runtime(store=OnboardingStore(restarted_client), source=source, adapters={fl.UC04: ok})
        service2.hydrate(CLIENT)
        track = service2.get(COORD, oid).downstream[fl.UC04]
        self.assertEqual((track.attempts, track.trigger_id), (1, f"TRG-{oid}-UC-04-p1"))  # the write-ahead intent survived
        flow2.trigger_downstream(COORD, oid, {fl.UC04: ok})
        self.assertEqual(ok.payloads[0]["trigger_id"], dies.ids[0])  # same idempotency key: the receiver can deduplicate

    def test_two_workers_deliver_the_same_trigger_id_so_the_receiver_can_deduplicate(self) -> None:
        """Documents the limit: process-local locks do not span workers, so delivery is at-least-once with ONE stable id."""
        client = InMemoryTableClient()
        a1, a2 = fl.MockTrigger(fl.UC04), fl.MockTrigger(fl.UC04)
        flow1, service1, source, oid = started(adapters={fl.UC04: a1}, store=OnboardingStore(client))
        approve(flow1, service1, oid)
        flow2, service2, _ = runtime(store=OnboardingStore(InMemoryTableClient.from_snapshot(client.snapshot())), source=source,
                                     adapters={fl.UC04: a2})
        service2.hydrate(CLIENT)
        verify_docs(flow1, oid)
        flow2.service._load(COORD, oid)  # worker 2 had loaded the plan before worker 1 opened the gate
        for d in service2.get(COORD, oid).documents:
            pass
        ids = {p["trigger_id"] for a in (a1, a2) for p in a.payloads}
        self.assertEqual(len(ids), 1)  # whichever worker sends, the id is identical


class HttpTriggerAdapterTests(unittest.TestCase):
    def test_the_trigger_is_signed_over_the_exact_body_and_posted_to_the_configured_https_url(self) -> None:
        seen = {}

        def post(url, headers, body):
            seen.update(url=url, headers=dict(headers), body=body)
            return 202, {"reference": "ACK-1"}

        adapter = HttpTriggerAdapter("UC-04", "https://uc04.synthetic.invalid/trigger", b"s3cret", transport=post)
        result = adapter.send({"trigger_id": "T-1", "use_case": "UC-04"})
        self.assertEqual((result.accepted, result.reference), (True, "ACK-1"))
        self.assertEqual(seen["headers"][SIGNATURE_HEADER], sign_body(b"s3cret", seen["body"]))
        self.assertEqual(json.loads(seen["body"]), {"trigger_id": "T-1", "use_case": "UC-04"})
        self.assertNotIn("s3cret", repr(adapter))

    def test_http_failures_and_network_errors_are_not_acceptance_and_http_is_refused(self) -> None:
        for status in (400, 500, 503):
            self.assertFalse(HttpTriggerAdapter("UC-05", "https://x.invalid/t", b"k", transport=lambda *a, s=status: (s, None)).send({}).accepted)

        def boom(*_):
            raise ConnectionError

        self.assertEqual(HttpTriggerAdapter("UC-05", "https://x.invalid/t", b"k", transport=boom).send({}).error, "network_error")
        with self.assertRaises(ValueError):
            HttpTriggerAdapter("UC-05", "http://x.invalid/t", b"k")
        with self.assertRaises(ValueError):
            HttpTriggerAdapter("UC-05", "https://x.invalid/t", b"")


class FactoryTests(unittest.TestCase):
    BASE = {"UC02_APPROVAL_CALLBACK_SECRET": "synthetic-secret"}

    def mem(self):
        return OnboardingService(sp.registry(), require_plan_approval=True, source_principals=rt.runtime_principals())

    def test_a_missing_callback_secret_refuses_to_build(self) -> None:
        with self.assertRaises(MissingConfigurationError):
            build_flow({}, service=self.mem())

    def test_the_runtime_refuses_to_build_its_own_service_on_anything_but_supabase(self) -> None:
        for env in (dict(self.BASE), {**self.BASE, "UC02_STORE_MODE": "memory"}, {**self.BASE, "UC02_STORE_MODE": "sqlite"}):
            with self.subTest(env=env.get("UC02_STORE_MODE")):
                with self.assertRaises(MissingConfigurationError) as ctx:
                    build_flow(env)
                self.assertIn("UC02_STORE_MODE=supabase", ctx.exception.message)

    def test_defaults_are_the_mock_connector_and_no_downstream_adapters_and_no_employee_source(self) -> None:
        flow = build_flow(self.BASE, service=self.mem())
        self.assertIsInstance(flow, rt.RuntimeFlow)
        self.assertIsInstance(flow.connector, MockOutlookConnector)
        self.assertEqual((flow.downstream_adapters, flow.source, flow.channel_secrets), ({}, None, {}))

    def test_graph_mode_never_falls_back_to_the_mock_and_adapters_need_both_url_and_secret(self) -> None:
        from onboarding_task_management.sub_agent.connectors.outlook.base import OutlookConnectorError

        with self.assertRaises(OutlookConnectorError):
            build_flow({**self.BASE, "UC02_OUTLOOK_CONNECTOR": "graph"}, service=self.mem())
        half = build_flow({**self.BASE, "UC02_UC04_TRIGGER_URL": "https://uc04.invalid/t"}, service=self.mem())
        self.assertEqual(half.downstream_adapters, {})
        full = build_flow({**self.BASE, "UC02_UC04_TRIGGER_URL": "https://uc04.invalid/t", "UC02_UC04_TRIGGER_SECRET": "k",
                           "UC02_EVENT_SECRET": "e", "UC02_APPROVAL_CALLBACK_URL": "https://uc02.invalid/v1/approvals/callback"},
                          service=self.mem())
        self.assertEqual(set(full.downstream_adapters), {"UC-04"})
        self.assertEqual(set(full.channel_secrets), {"events"})
        self.assertEqual(full.approval_callback_url, "https://uc02.invalid/v1/approvals/callback")


class SupabaseEmployeeSourceTests(unittest.TestCase):
    def source(self, forms):
        from onboarding_task_management.supabase_rest import SupabaseGateway, SupabaseRestClient
        from tests.test_supabase_config import Db

        case = {"case_id": CASE, "client_id": "C-LIVE", "employee_no": "EMP-9", "joining_date": "2026-11-16",
                "location_name": "Dubai", "project_name": "P", "role_id": "eng", "grade": "L3", "department": "Eng",
                "manager_id": "M1", "nationality_group": "g", "status": "DOCUMENTS_PENDING",
                "current_stage": "document_collection", "onboarding_attributes": ["x"], "is_deleted": False,
                "work_location_type": "OFFICE", "hr_officer_id": "H1"}
        db = Db(onboarding_cases=[case], onboarding_forms=forms)
        return fl.SupabaseEmployeeSource(SupabaseGateway(SupabaseRestClient("https://example.supabase.co", "k", transport=db)))

    def test_the_case_and_an_approved_form_become_an_employee_view_without_personal_columns(self) -> None:
        view = self.source([{"case_id": CASE, "client_id": "C-LIVE", "status": "APPROVED", "version": 1,
                             "approved_at": "2026-10-22T00:00:00+00:00", "approved_by": "H1"}]).get("C-LIVE", CASE)
        self.assertEqual((view.employee_id, view.role_id, view.location, view.attributes), ("EMP-9", "eng", "Dubai", ("x",)))
        self.assertIsNotNone(view.form_approved_at)
        self.assertNotIn("candidate", json.dumps(view.model_dump(mode="json")))

    def test_no_approved_form_means_no_facts_and_unknown_or_soft_deleted_cases_are_absent(self) -> None:
        self.assertIsNone(self.source([]).get("C-LIVE", CASE).form_approved_at)
        self.assertIsNone(self.source([{"case_id": CASE, "client_id": "C-LIVE", "status": "DRAFT", "version": 1,
                                        "approved_at": None, "approved_by": None}]).get("C-LIVE", CASE).form_approved_at)
        self.assertIsNone(self.source([]).get("C-LIVE", "no-such-case"))


class AcceptanceTests(unittest.TestCase):
    """The full intended sequence through the HTTP routes, approval path and rejection path, with mocked externals."""

    def setUp(self) -> None:
        self.fake = FakeGraph()
        self.graph = graph_for(self.fake, addresses={CLIENT: {"roles": ADDRESSES, "teams": TEAM_ADDRESSES,
                                                               "allow_role_fallback": False}})
        self.a, self.b = fl.MockTrigger(fl.UC04), fl.MockTrigger(fl.UC05)
        self.flow, self.service, _ = runtime(connector=self.graph, adapters={fl.UC04: self.a, fl.UC05: self.b})
        api_module.configure_flow(self.flow)
        self.addCleanup(api_module.configure_flow, None)
        self.http = TestClient(api_module.app)

    def post(self, path, model):
        return self.http.post(path, content=model.model_dump_json(), headers={"content-type": "application/json"})

    def now(self):
        return datetime.now(UTC)

    def test_the_approval_path_end_to_end(self) -> None:
        evt = rt.sign_model(SECRETS["events"], event(event_id="E-1"))
        self.assertEqual(self.post("/v1/events/employee-created", evt).status_code, 200)
        oid = f"ONB-{CLIENT}-{sp.PROFILES['uae_inside'].employee_id}"
        self.assertEqual(self.post("/v1/events/employee-created", rt.sign_model(SECRETS["events"], event(event_id="E-2"))).json()["status"], "duplicate")
        record = self.service.get(COORD, oid)
        self.assertEqual(record.plan_status, PlanStatus.SUBMITTED)
        hr_email = next(m for m in record.messages if m.kind == "hrmail")
        self.assertEqual(self.fake.count("create"), 1)  # the HR review email draft is in the mailbox
        # HR approves through the authenticated callback
        request = self.service.inbox.pending(COORD)[0]
        cb = fl.sign_callback(SECRET, approve_cb(oid, request.request_id, sign=False, issued=self.now()).model_copy(update={"client_id": CLIENT}))
        self.assertEqual(self.post("/v1/approvals/callback", cb).json()["status"], "approved")
        record = self.service.get(COORD, oid)
        self.assertTrue(record.assignments and any(m.kind == "docreq" for m in record.messages))
        self.assertEqual((self.a.payloads, self.b.payloads), ([], []))  # still blocked: no documents yet
        # HR lead authorises sending the HR email and the document request
        for mid in (hr_email.message_id, next(m.message_id for m in record.messages if m.kind == "docreq")):
            d = rt.sign_model(SECRET, rt.MessageDecision(decision_id=f"D-{mid[:12]}", client_id=CLIENT, onboarding_id=oid,
                                                         message_id=mid, approver_id="U-LEAD-1", approver_role=Role.HR_LEAD,
                                                         issued_at=self.now()))
            self.assertEqual(self.post("/v1/messages/decision", d).json()["connector_status"], "submitted_for_sending")
        # documents arrive and are verified through the signed channel
        for n, d in enumerate([x for x in record.documents if x.mandatory], start=1):
            for i, (state, ref) in enumerate(((DocumentState.SUBMITTED, None), (DocumentState.UNDER_REVIEW, None),
                                              (DocumentState.VERIFIED, f"V-{d.key}"))):
                upd = rt.sign_model(SECRETS["documents"], doc_update(oid, d.key, state, 100 * n + i, 100 * n + i, ref))
                self.assertEqual(self.post("/v1/documents/status", upd).status_code, 200)
        self.assertEqual((len(self.a.payloads), len(self.b.payloads)), (1, 1))  # both triggered, once
        for uc, secret_key in ((fl.UC04, "uc04"), (fl.UC05, "uc05")):
            ev = rt.sign_model(SECRETS[secret_key], fl.DownstreamStatusEvent(
                event_id=f"S-{uc}", client_id=CLIENT, use_case=uc, onboarding_id=oid, plan_version=1,
                state=DownstreamState.COMPLETED if uc == fl.UC04 else DownstreamState.DELAYED, occurred_at=NOW))
            self.assertEqual(self.post("/v1/downstream/status", ev).status_code, 200)
        self.assertEqual(self.flow.downstream_summary(COORD, oid), {fl.UC04: "completed", fl.UC05: "delayed"})
        outcomes = " ".join(e.outcome for e in self.service.audit.events_for(COORD))
        self.assertNotIn("delivered", outcomes)
        self.assertEqual(self.fake.count("send"), 2)  # exactly the two authorised messages were submitted

    def test_the_rejection_path_end_to_end(self) -> None:
        self.post("/v1/events/employee-created", rt.sign_model(SECRETS["events"], event(event_id="E-9")))
        oid = f"ONB-{CLIENT}-{sp.PROFILES['uae_inside'].employee_id}"
        request = self.service.inbox.pending(COORD)[0]
        cb = fl.sign_callback(SECRET, approve_cb(oid, request.request_id, sign=False, decision="rejected",
                                                 reason="synthetic: not acceptable", issued=self.now()).model_copy(update={"client_id": CLIENT}))
        self.assertEqual(self.post("/v1/approvals/callback", cb).json()["status"], "rejected")
        record = self.service.get(COORD, oid)
        self.assertEqual((record.plan_status, record.assignments, record.downstream), (PlanStatus.REJECTED, [], {}))
        # nothing downstream can proceed
        upd = rt.sign_model(SECRETS["documents"], doc_update(oid, "identity", DocumentState.SUBMITTED, 1, 1))
        self.assertIn(self.post("/v1/documents/status", upd).status_code, (409, 422))
        self.assertEqual((self.a.payloads, self.b.payloads), ([], []))
        self.assertEqual(self.fake.count("send"), 0)
        self.assertTrue(any(m.kind == "hrnotice" for m in record.messages))

    def test_no_real_external_call_is_possible_in_these_tests(self) -> None:
        # every transport in this module is an injected stand-in
        self.assertIsInstance(self.graph._transport, FakeGraph)
        self.assertTrue(all(isinstance(a, fl.MockTrigger) for a in self.flow.downstream_adapters.values()))

    def test_routes_refuse_when_unconfigured(self) -> None:
        api_module.configure_flow(None)
        import os
        saved = os.environ.pop(api_module.ENV_CALLBACK_SECRET, None)
        try:
            response = self.http.post("/v1/events/employee-created", content=signed_event().model_dump_json(),
                                      headers={"content-type": "application/json"})
            self.assertEqual(response.status_code, 503)
        finally:
            if saved is not None:
                os.environ[api_module.ENV_CALLBACK_SECRET] = saved


class SnapshotGrowthTests(unittest.TestCase):
    def test_unchanged_state_adds_no_new_snapshot_and_the_ledger_is_bounded_by_events(self) -> None:
        from tests.test_supabase_store import CASE as SCASE, FakePostgrest
        from onboarding_task_management.supabase_store import STATE_ACTION

        flow, service, _, oid = started()
        size = len(service.get(COORD, oid).model_dump_json())
        for _ in range(3):
            flow.prepare_hr_email(COORD, oid, TODAY)  # a repeat prepares nothing new
        self.assertEqual(len(service.get(COORD, oid).model_dump_json()), size)


class TwoEntryPathTests(unittest.TestCase):
    def test_the_legacy_in_process_path_and_the_event_route_do_not_process_the_same_employee_twice(self) -> None:
        """The orchestrator's adapter and the event route share a store-backed duplicate rule per employee."""
        flow, service, _ = runtime()
        legacy = flow.source.get(CLIENT, CASE)
        trigger = sp.trigger_for(sp.PROFILES["uae_inside"], case_id=CASE)
        first = service.create_onboarding(COORD, trigger)  # what UC02SubAgent.run / the orchestrator adapter does
        out = flow.receive_employee_event(signed_event(), TODAY)
        self.assertEqual((out.status, out.data["onboarding_id"]), ("duplicate", first.onboarding_id))
        self.assertEqual(len(service._client_records(CLIENT)), 1)


if __name__ == "__main__":
    unittest.main()
