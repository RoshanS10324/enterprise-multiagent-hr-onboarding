"""The full UC-02 flow over SYNTHETIC configuration and MOCK connectors: UC-01 event to independent UC-04/UC-05 tracking.

Everything external is a mock (employee source, Outlook, approval connector, document source, UC-04, UC-05). Passing
these tests proves UC-02's side of the contracts, not any real integration.
"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone

from onboarding_task_management import synthetic_profiles as sp
from onboarding_task_management.contracts import (
    Actor,
    DocumentState,
    DownstreamState,
    PlanStatus,
    Role,
    SourceUseCase,
)
from onboarding_task_management.errors import (
    InvalidStateError,
    MissingConfigurationError,
    PreconditionError,
    UnauthorizedActionError,
)
from onboarding_task_management.persistence import InMemoryTableClient, OnboardingStore
from onboarding_task_management.sub_agent import flow as fl
from onboarding_task_management.sub_agent.connectors.outlook.mock import MockOutlookConnector
from onboarding_task_management.sub_agent.service import OnboardingService

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
TODAY = date(2026, 11, 2)
SECRET = b"synthetic-test-secret-not-real"
CLIENT = sp.UAE_CLIENT
CASE = "11111111-2222-4333-8444-555555555555"
DOC_SOURCE, UC04_SOURCE, UC05_SOURCE = "svc-docs-mock", "svc-uc04-mock", "svc-uc05-mock"
PRINCIPALS = {SourceUseCase.UC03_DOCUMENT_VALIDATION: frozenset({DOC_SOURCE}),
              SourceUseCase.UC04_EQUIPMENT_ACCESS: frozenset({UC04_SOURCE}),
              SourceUseCase.UC05_ORIENTATION: frozenset({UC05_SOURCE})}


class MockEmployeeSource:
    """MOCK of the UC-01 record. A real source reads the shared case; this one holds a dict."""

    def __init__(self):
        self.rows: dict[tuple[str, str], fl.EmployeeView] = {}

    def add(self, profile_key="uae_inside", case_id=CASE, **kw) -> fl.EmployeeView:
        p = sp.PROFILES[profile_key]
        view = fl.EmployeeView(
            client_id=p.client_id, employee_id=p.employee_id, case_id=case_id, role_id=p.role_id, grade=p.grade,
            manager_id="MGR-1", location=p.location, project="synthetic-project", nationality_group=p.nationality_group,
            attributes=p.attributes, joining_date=p.joining_date, current_location_status=p.current_location_status,
            offer_accepted_at=datetime(2026, 10, 20, tzinfo=UTC), form_approved_at=datetime(2026, 10, 22, tzinfo=UTC),
            form_approved_by_role=Role.HR_OPS)
        view = view.model_copy(update=kw)
        self.rows[(view.client_id, case_id)] = view
        return view

    def get(self, client_id, case_id):
        return self.rows.get((client_id, case_id))


def actor(role, ident, client=CLIENT):
    return Actor(actor_id=ident, role=role, client_id=client)


COORD = actor(Role.HR_OPS, "uc02-coordinator")
LEAD = actor(Role.HR_LEAD, "U-LEAD-1")
DOCS = actor(Role.SYSTEM, DOC_SOURCE)
UC04_A = actor(Role.SYSTEM, UC04_SOURCE)
UC05_A = actor(Role.SYSTEM, UC05_SOURCE)


def event(event_id="EVT-1", case_id=CASE, profile="uae_inside", **kw) -> fl.EmployeeCreatedEvent:
    p = sp.PROFILES[profile]
    base = dict(event_id=event_id, client_id=p.client_id, employee_id=p.employee_id, case_id=case_id,
                joining_date=p.joining_date, occurred_at=NOW, correlation_id="CORR-1")
    base.update(kw)
    return fl.EmployeeCreatedEvent(**base)


def make(profile="uae_inside", store=None, source=None):
    source = source or MockEmployeeSource()
    if (sp.PROFILES[profile].client_id, CASE) not in source.rows:
        source.add(profile)
    service = OnboardingService(sp.registry(), store=store, require_plan_approval=True, source_principals=PRINCIPALS)
    return fl.OnboardingFlow(service, source, coordinator=COORD, callback_secret=SECRET), service, source


def approve_cb(oid, request_id, version=1, decision="approved", cb_id="CB-1", approver="U-HR-APPR", role=Role.HR_OPS,
               issued=NOW, reason="", sign=True):
    cb = fl.ApprovalCallback(callback_id=cb_id, onboarding_id=oid, request_id=request_id, plan_version=version,
                             decision=decision, approver_id=approver, approver_role=role, issued_at=issued, reason=reason)
    return fl.sign_callback(SECRET, cb) if sign else cb


def to_review(flow, service, profile="uae_inside"):
    oid = flow.handle_employee_created(event(profile=profile), TODAY).onboarding_id
    flow.generate_review(COORD, oid, TODAY)
    request = service.submit_plan(COORD, oid)
    return oid, request


def approved(flow, service, profile="uae_inside"):
    oid, request = to_review(flow, service, profile)
    out = flow.process_approval_callback(CLIENT, approve_cb(oid, request.request_id), NOW)
    assert out.status == "approved", out
    return oid


def doc_update(oid, key, state, n, ts_minutes=0, ref=None, version=1):
    return fl.DocumentUpdate(update_id=f"DU-{n}", client_id=CLIENT, onboarding_id=oid, plan_version=version,
                             document_key=key, state=state, occurred_at=NOW + timedelta(minutes=ts_minutes),
                             reference=ref)


def verify_all(flow, oid, start=0):
    n = start
    for d in flow.service.get(COORD, oid).documents:
        if d.mandatory:
            for state, ref in ((DocumentState.SUBMITTED, None), (DocumentState.UNDER_REVIEW, None),
                               (DocumentState.VERIFIED, f"VER-{d.key}")):
                n += 1
                assert flow.apply_document_update(DOCS, doc_update(oid, d.key, state, n, n, ref)).accepted
    return n


def event_status(oid, uc, state, n, minutes=0):
    return fl.DownstreamStatusEvent(event_id=f"DS-{uc}-{n}", client_id=CLIENT, use_case=uc, onboarding_id=oid,
                                    plan_version=1, state=state, occurred_at=NOW + timedelta(minutes=minutes))


class TriggerTests(unittest.TestCase):
    def test_a_valid_event_creates_the_onboarding_from_the_retrieved_employee(self) -> None:
        flow, service, _ = make()
        result = flow.handle_employee_created(event(), TODAY)
        self.assertTrue(result.created)
        record = service.get(COORD, result.onboarding_id)
        self.assertEqual((record.case_id, record.client_id, record.correlation_id), (CASE, CLIENT, "CORR-1"))
        self.assertEqual(record.config_version, sp.uae_config().version)

    def test_a_duplicate_event_returns_the_same_onboarding_and_creates_nothing_new(self) -> None:
        flow, service, _ = make()
        first = flow.handle_employee_created(event(), TODAY)
        again = flow.handle_employee_created(event(event_id="EVT-2"), TODAY)
        self.assertEqual((again.created, again.onboarding_id), (False, first.onboarding_id))
        self.assertEqual(len([t for t in service.get(COORD, first.onboarding_id).tasks]), len(set(
            t.task_id for t in service.get(COORD, first.onboarding_id).tasks)))

    def test_missing_employee_invalid_event_and_mismatch_are_refused(self) -> None:
        flow, *_ = make()
        with self.assertRaises(PreconditionError):
            flow.handle_employee_created(event(case_id="99999999-2222-4333-8444-555555555555"), TODAY)
        with self.assertRaises(PreconditionError):
            flow.handle_employee_created(event(event_id=" "), TODAY)
        with self.assertRaises(PreconditionError):
            flow.handle_employee_created(event(joining_date=date(2026, 11, 20)), TODAY)  # differs from the record
        with self.assertRaises(PreconditionError):
            flow.handle_employee_created(event(joining_date=date(2026, 10, 1)), TODAY)  # already passed

    def test_uc01_facts_that_are_missing_are_not_invented(self) -> None:
        source = MockEmployeeSource()
        source.add(form_approved_at=None)
        flow, *_ = make(source=source)
        with self.assertRaises(PreconditionError):
            flow.handle_employee_created(event(), TODAY)

    def test_an_inactive_record_is_refused(self) -> None:
        source = MockEmployeeSource()
        source.add(active=False)
        flow, *_ = make(source=source)
        with self.assertRaises(PreconditionError):
            flow.handle_employee_created(event(), TODAY)

    def test_a_delayed_event_is_processed_and_the_lag_is_audited(self) -> None:
        flow, service, _ = make()
        result = flow.handle_employee_created(event(occurred_at=NOW - timedelta(days=5)), TODAY)
        self.assertTrue(result.created and result.notes)
        self.assertTrue([e for e in service.audit.events_for(COORD) if e.action == "flow.event_delayed"])

    def test_an_unconfigured_client_is_a_configuration_gap_not_a_default(self) -> None:
        source = MockEmployeeSource()
        source.add()
        flow, *_ = make(source=source)
        flow.service.registry.configs.pop(CLIENT)
        with self.assertRaises(MissingConfigurationError):
            flow.handle_employee_created(event(), TODAY)


class ReviewAndEmailTests(unittest.TestCase):
    def test_the_hr_email_contains_every_required_section_and_nothing_personal(self) -> None:
        flow, service, _ = make()
        oid, _ = to_review(flow, service)
        email = flow.prepare_hr_email(COORD, oid, TODAY)
        for needle in ("1. EMPLOYEE", "2. CLIENT AND JOINING", "3. CHECKLIST", "4. MANDATORY DOCUMENTS",
                       "5. CONDITIONAL", "6. EXCLUDED DOCUMENTS", "7. EXCLUDED TASKS", "8. OWNERS AND TEAMS",
                       "9. FAKE LLM REVIEW", "10. RISKS AND MISSING", "11. HOW TO RESPOND", "visa_status_change",
                       "synthetic_identity", "NOT an approval", "simulated, offline"):
            self.assertIn(needle, email.body)
        self.assertNotIn("@", email.body)
        self.assertEqual(email.delivery_status, "draft_not_sent")

    def test_mandatory_conditional_and_excluded_documents_are_listed_with_reasons(self) -> None:
        flow, service, _ = make()
        oid, _ = to_review(flow, service)
        body = flow.prepare_hr_email(COORD, oid, TODAY).body
        mandatory = body.split("4. MANDATORY DOCUMENTS")[1].split("5. CONDITIONAL")[0]
        self.assertIn("status_change_pack", mandatory)
        self.assertNotIn("entry_permit_pack", mandatory)
        self.assertIn("entry_permit_pack", body.split("6. EXCLUDED DOCUMENTS")[1].split("7. EXCLUDED TASKS")[0])
        self.assertIn("family_cover_form", body.split("5. CONDITIONAL")[1].split("6. EXCLUDED")[0])

    def test_different_profiles_give_different_document_lists_and_emails(self) -> None:
        bodies = {}
        for profile in ("uae_inside", "uae_overseas", "india_full"):
            source = MockEmployeeSource()
            source.add(profile)
            flow, service, _ = make(profile, source=source)
            oid = flow.handle_employee_created(event(profile=profile), TODAY).onboarding_id
            who = actor(Role.HR_OPS, "uc02-coordinator", sp.PROFILES[profile].client_id)
            flow.generate_review(who, oid, TODAY)
            bodies[profile] = {d.key for d in service.get(who, oid).documents}
        self.assertEqual(len({frozenset(v) for v in bodies.values()}), 3)
        self.assertIn("entry_permit_pack", bodies["uae_overseas"])
        self.assertIn("address_proof", bodies["india_full"])

    def test_sending_needs_a_different_hr_lead_and_reports_only_what_the_connector_says(self) -> None:
        flow, service, _ = make()
        oid, _ = to_review(flow, service)
        email = flow.prepare_hr_email(COORD, oid, TODAY)
        mock = MockOutlookConnector()
        with self.assertRaises(InvalidStateError):
            flow.send_message(COORD, oid, "hrmail", email.email_id, email.subject, email.body, email.recipient_role, mock)
        with self.assertRaises(UnauthorizedActionError):
            flow.authorise_message(COORD, oid, "hrmail", email.email_id)  # the preparer
        flow.authorise_message(LEAD, oid, "hrmail", email.email_id)
        outcome = flow.send_message(COORD, oid, "hrmail", email.email_id, email.subject, email.body,
                                    email.recipient_role, mock)
        self.assertEqual(outcome, "simulated_sent")  # never "delivered"
        again = flow.send_message(COORD, oid, "hrmail", email.email_id, email.subject, email.body,
                                  email.recipient_role, mock)
        self.assertEqual((again, len(mock.outbox(CLIENT))), ("simulated_sent", 1))

    def test_a_connector_failure_is_recorded_as_failed_not_hidden(self) -> None:
        flow, service, _ = make()
        oid, _ = to_review(flow, service)
        email = flow.prepare_hr_email(COORD, oid, TODAY)
        flow.authorise_message(LEAD, oid, "hrmail", email.email_id)
        mock = MockOutlookConnector(fail_keys={email.email_id})
        self.assertEqual(flow.send_message(COORD, oid, "hrmail", email.email_id, email.subject, email.body,
                                           email.recipient_role, mock), "failed")

    def test_sending_an_email_is_not_an_approval(self) -> None:
        flow, service, _ = make()
        oid, _ = to_review(flow, service)
        email = flow.prepare_hr_email(COORD, oid, TODAY)
        flow.authorise_message(LEAD, oid, "hrmail", email.email_id)
        flow.send_message(COORD, oid, "hrmail", email.email_id, email.subject, email.body, email.recipient_role,
                          MockOutlookConnector())
        self.assertEqual(service.get(COORD, oid).plan_status, PlanStatus.SUBMITTED)

    def test_the_review_is_fake_llm_labelled_and_never_approves(self) -> None:
        flow, service, _ = make()
        oid, _ = to_review(flow, service)
        pkg = flow.generate_review(COORD, oid, TODAY)
        self.assertTrue(pkg["analysis"].source.startswith("fake_llm:") and pkg["analysis"].advisory_only)
        self.assertEqual(pkg["record"].plan_status, PlanStatus.SUBMITTED)


class ApprovalCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow, self.service, _ = make()
        self.oid, self.request = to_review(self.flow, self.service)

    def go(self, **kw):
        return self.flow.process_approval_callback(CLIENT, approve_cb(self.oid, self.request.request_id, **kw), NOW)

    def status(self) -> PlanStatus:
        return self.service.get(COORD, self.oid).plan_status

    def test_a_valid_callback_approves_and_prepares_documents_and_waiting_tracks(self) -> None:
        self.assertEqual(self.go().status, "approved")
        record = self.service.get(COORD, self.oid)
        self.assertEqual(record.plan_status, PlanStatus.APPROVED)
        self.assertTrue(record.documents)
        self.assertEqual({uc: t.state for uc, t in record.downstream.items()},
                         {fl.UC04: DownstreamState.WAITING_FOR_DOCUMENTS, fl.UC05: DownstreamState.WAITING_FOR_DOCUMENTS})
        self.assertEqual(set(record.downstream[fl.UC04].task_ids) & set(record.downstream[fl.UC05].task_ids), set())

    def test_an_unsigned_or_tampered_callback_cannot_approve(self) -> None:
        self.assertEqual(self.go(sign=False).status, "invalid_signature")
        tampered = approve_cb(self.oid, self.request.request_id).model_copy(update={"decision": "approved", "approver_id": "U-EVIL"})
        out = self.flow.process_approval_callback(CLIENT, tampered, NOW)
        self.assertEqual(out.status, "invalid_signature")
        self.assertEqual(self.status(), PlanStatus.SUBMITTED)

    def test_the_wrong_secret_cannot_approve(self) -> None:
        cb = fl.sign_callback(b"another-secret", approve_cb(self.oid, self.request.request_id, sign=False))
        self.assertEqual(self.flow.process_approval_callback(CLIENT, cb, NOW).status, "invalid_signature")

    def test_an_unauthorised_approver_and_self_approval_are_refused(self) -> None:
        self.assertEqual(self.go(role=Role.EMPLOYEE, cb_id="CB-A", approver="U-EMP").status, "unauthorized")
        self.assertEqual(self.go(cb_id="CB-B", approver=COORD.actor_id).status, "unauthorized")  # the requester
        self.assertEqual(self.status(), PlanStatus.SUBMITTED)

    def test_a_replayed_callback_has_no_second_effect(self) -> None:
        self.assertEqual(self.go().status, "approved")
        self.assertEqual(self.status(), PlanStatus.APPROVED)
        events = len(self.service.audit.events_for(COORD))
        again = self.go()
        self.assertEqual((again.status, again.onboarding_id), ("replayed", self.oid))
        self.assertEqual(len(self.service.audit.events_for(COORD)), events)  # a replay leaves no trace of a second effect
        self.assertEqual(self.status(), PlanStatus.APPROVED)

    def test_a_stale_plan_version_cannot_approve_a_newer_revision(self) -> None:
        self.assertEqual(self.go(version=1, decision="changes_requested", cb_id="CB-C", reason="fix dates").status,
                         "changes_requested")
        record = self.service.get(COORD, self.oid)
        self.assertEqual((record.plan_version, record.plan_status), (2, PlanStatus.SUBMITTED))
        stale = self.flow.process_approval_callback(CLIENT, approve_cb(self.oid, self.request.request_id, version=1,
                                                                        cb_id="CB-D"), NOW)
        self.assertIn(stale.status, {"stale_version", "not_pending"})
        self.assertEqual(self.status(), PlanStatus.SUBMITTED)

    def test_changes_requested_revises_and_resubmits_a_new_version_for_approval(self) -> None:
        out = self.go(decision="changes_requested", reason="synthetic: tighten the dates")
        self.assertEqual(out.status, "changes_requested")
        new_request = out.new_request_id
        self.assertIsNotNone(new_request)
        record = self.service.get(COORD, self.oid)
        self.assertEqual(record.plan_status, PlanStatus.SUBMITTED)
        self.assertIn("rejected", [e.action for e in record.plan_events])
        self.assertIn("revised", [e.action for e in record.plan_events])
        final = self.flow.process_approval_callback(
            CLIENT, approve_cb(self.oid, new_request, version=record.plan_version, cb_id="CB-E"), NOW)
        self.assertEqual(final.status, "approved")

    def test_rejection_records_the_reason_and_executes_nothing(self) -> None:
        self.assertEqual(self.go(decision="rejected", reason="synthetic: not acceptable").status, "rejected")
        record = self.service.get(COORD, self.oid)
        self.assertEqual(record.plan_status, PlanStatus.REJECTED)
        self.assertEqual(record.downstream, {})
        self.assertEqual(record.plan_events[-1].reason, "synthetic: not acceptable")

    def test_an_expired_request_and_an_old_callback_cannot_approve(self) -> None:
        late = self.service.inbox.get(COORD, self.request.request_id).created_at + self.flow.approval_ttl + timedelta(hours=1)
        # (relative to the request's real creation time, so the test does not depend on the day it runs)
        cb = approve_cb(self.oid, self.request.request_id, issued=late, cb_id="CB-F")
        self.assertEqual(self.flow.process_approval_callback(CLIENT, cb, late).status, "expired")
        self.assertEqual(self.status(), PlanStatus.SUBMITTED)
        old = approve_cb(self.oid, self.request.request_id, issued=NOW - timedelta(hours=2), cb_id="CB-G")
        self.assertEqual(self.flow.process_approval_callback(CLIENT, old, NOW).status, "expired")

    def test_a_callback_for_another_case_or_an_unknown_request_is_refused(self) -> None:
        self.assertEqual(self.flow.process_approval_callback(
            CLIENT, approve_cb("ONB-NOPE", self.request.request_id, cb_id="CB-H"), NOW).status, "unknown_request")
        self.assertEqual(self.flow.process_approval_callback(
            CLIENT, approve_cb(self.oid, "REQ-NOPE", cb_id="CB-I"), NOW).status, "unknown_request")

    def test_another_client_cannot_use_the_callback_route(self) -> None:
        out = self.flow.process_approval_callback(sp.INDIA_CLIENT, approve_cb(self.oid, self.request.request_id, cb_id="CB-J"), NOW)
        self.assertEqual(out.status, "unknown_request")
        self.assertEqual(self.status(), PlanStatus.SUBMITTED)

    def test_no_post_approval_action_works_before_approval(self) -> None:
        with self.assertRaises(InvalidStateError):
            self.flow.prepare_document_request(COORD, self.oid)
        with self.assertRaises(InvalidStateError):
            self.flow.initialise_workflow(COORD, self.oid)
        self.assertFalse(self.flow.document_gate(COORD, self.oid).open)

    def test_a_secret_is_mandatory(self) -> None:
        with self.assertRaises(ValueError):
            fl.OnboardingFlow(self.service, MockEmployeeSource(), coordinator=COORD, callback_secret=b"")


class DocumentCoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow, self.service, _ = make()
        self.oid = approved(self.flow, self.service)

    def test_the_document_request_names_the_configured_team_and_the_exact_list(self) -> None:
        req = self.flow.prepare_document_request(COORD, self.oid)
        self.assertEqual((req.use_case, req.team), ("UC-03", "HR Operations (synthetic)"))
        keys = {d["key"] for d in req.documents}
        self.assertEqual(keys, {d.key for d in self.service.get(COORD, self.oid).documents})
        self.assertIn("MANDATORY", req.body)
        self.assertEqual(req.delivery_status, "draft_not_sent")
        self.assertEqual([d.draft_id if hasattr(d, "draft_id") else None for d in []], [])

    def test_a_missing_team_rule_is_an_assignment_gap_not_a_guess(self) -> None:
        cfg = self.service.registry.configs[CLIENT]
        self.service.registry.configs[CLIENT] = cfg.model_copy(update={"assignments": ()})
        with self.assertRaises(MissingConfigurationError):
            self.flow.prepare_document_request(COORD, self.oid)

    def test_document_updates_follow_the_state_machine(self) -> None:
        key = next(d.key for d in self.service.get(COORD, self.oid).documents if d.mandatory)
        bad = self.flow.apply_document_update(DOCS, doc_update(self.oid, key, DocumentState.VERIFIED, 1, 1, "REF"))
        self.assertEqual(bad.code, "rejected_invalid_transition")  # pending cannot jump to verified
        ok = self.flow.apply_document_update(DOCS, doc_update(self.oid, key, DocumentState.SUBMITTED, 2, 2))
        self.assertTrue(ok.accepted)
        self.assertEqual(self.flow.apply_document_update(
            DOCS, doc_update(self.oid, key, DocumentState.UNDER_REVIEW, 3, 3)).code, "applied")
        self.assertEqual(self.flow.apply_document_update(
            DOCS, doc_update(self.oid, key, DocumentState.VERIFIED, 4, 4)).code, "rejected_missing_evidence_reference")

    def test_untrusted_wrong_case_stale_version_and_unknown_documents_are_rejected(self) -> None:
        key = self.service.get(COORD, self.oid).documents[0].key
        outsider = actor(Role.SYSTEM, "svc-someone-else")
        self.assertEqual(self.flow.apply_document_update(outsider, doc_update(self.oid, key, DocumentState.SUBMITTED, 1)).code,
                         "rejected_unverified_source")
        self.assertEqual(self.flow.apply_document_update(DOCS, doc_update(self.oid, key, DocumentState.SUBMITTED, 2, version=9)).code,
                         "rejected_stale_plan_version")
        self.assertEqual(self.flow.apply_document_update(DOCS, doc_update(self.oid, "no_such_doc", DocumentState.SUBMITTED, 3)).code,
                         "rejected_unknown_document")
        self.assertEqual(self.flow.apply_document_update(DOCS, doc_update("ONB-NOPE", key, DocumentState.SUBMITTED, 4)).code,
                         "rejected_unknown_case")

    def test_duplicate_and_out_of_order_updates_are_safe(self) -> None:
        key = next(d.key for d in self.service.get(COORD, self.oid).documents if d.mandatory)
        first = doc_update(self.oid, key, DocumentState.SUBMITTED, 1, 10)
        self.assertEqual(self.flow.apply_document_update(DOCS, first).code, "applied")
        self.assertEqual(self.flow.apply_document_update(DOCS, first).code, "duplicate_replay")
        older = doc_update(self.oid, key, DocumentState.UNDER_REVIEW, 2, 5)
        self.assertEqual(self.flow.apply_document_update(DOCS, older).code, "stale_ignored")
        doc = next(d for d in self.service.get(COORD, self.oid).documents if d.key == key)
        self.assertEqual(doc.state, DocumentState.SUBMITTED)

    def test_not_applicable_only_where_an_approved_rule_allows_it(self) -> None:
        record = self.service.get(COORD, self.oid)
        allowed = next(d.key for d in record.documents if d.allow_not_applicable)
        refused = next(d.key for d in record.documents if d.mandatory and not d.allow_not_applicable)
        self.assertEqual(self.flow.apply_document_update(DOCS, doc_update(self.oid, allowed, DocumentState.NOT_APPLICABLE, 1)).code,
                         "applied")
        self.assertEqual(self.flow.apply_document_update(DOCS, doc_update(self.oid, refused, DocumentState.NOT_APPLICABLE, 2)).code,
                         "rejected_not_applicable_not_allowed")


class GateAndDownstreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow, self.service, _ = make()
        self.oid = approved(self.flow, self.service)
        self.uc04, self.uc05 = fl.MockTrigger(fl.UC04), fl.MockTrigger(fl.UC05)
        self.adapters = {fl.UC04: self.uc04, fl.UC05: self.uc05}

    def trigger(self):
        return self.flow.trigger_downstream(COORD, self.oid, self.adapters)

    def assert_blocked(self):
        result = self.trigger()
        self.assertEqual(result, {fl.UC04: "waiting_for_documents", fl.UC05: "waiting_for_documents"})
        self.assertEqual((self.uc04.payloads, self.uc05.payloads), ([], []))

    def test_nothing_collected_blocks_both(self) -> None:
        gate = self.flow.document_gate(COORD, self.oid)
        self.assertFalse(gate.open)
        self.assert_blocked()

    def test_partial_collection_blocks_both(self) -> None:
        keys = [d.key for d in self.service.get(COORD, self.oid).documents if d.mandatory]
        n = 0
        for state, ref in ((DocumentState.SUBMITTED, None), (DocumentState.UNDER_REVIEW, None), (DocumentState.VERIFIED, "V1")):
            n += 1
            self.flow.apply_document_update(DOCS, doc_update(self.oid, keys[0], state, n, n, ref))
        gate = self.flow.document_gate(COORD, self.oid)
        self.assertEqual((gate.open, gate.mandatory_verified < gate.mandatory_total), (False, True))
        self.assert_blocked()

    def test_a_rejected_mandatory_document_and_a_resubmission_block_both(self) -> None:
        keys = [d.key for d in self.service.get(COORD, self.oid).documents if d.mandatory]
        n = verify_all(self.flow, self.oid)
        # a verified one stays verified, so reject a different, still-open case: start over on a fresh onboarding
        flow, service, _ = make()
        oid = approved(flow, service)
        k = [d.key for d in service.get(COORD, oid).documents if d.mandatory][0]
        flow.apply_document_update(DOCS, doc_update(oid, k, DocumentState.SUBMITTED, 1, 1))
        flow.apply_document_update(DOCS, doc_update(oid, k, DocumentState.REJECTED, 2, 2))
        gate = flow.document_gate(COORD, oid)
        self.assertEqual((gate.open, gate.rejected), (False, [k]))
        flow.apply_document_update(DOCS, doc_update(oid, k, DocumentState.RESUBMISSION_REQUIRED, 3, 3))
        self.assertEqual(flow.document_gate(COORD, oid).resubmission_required, [k])
        a, b = fl.MockTrigger(fl.UC04), fl.MockTrigger(fl.UC05)
        flow.trigger_downstream(COORD, oid, {fl.UC04: a, fl.UC05: b})
        self.assertEqual((a.payloads, b.payloads), ([], []))

    def test_an_invalid_completion_event_does_not_open_the_gate(self) -> None:
        keys = [d.key for d in self.service.get(COORD, self.oid).documents if d.mandatory]
        for i, k in enumerate(keys):
            self.flow.apply_document_update(DOCS, doc_update(self.oid, k, DocumentState.VERIFIED, i, i, "R"))  # skips steps
            self.flow.apply_document_update(actor(Role.SYSTEM, "svc-evil"), doc_update(self.oid, k, DocumentState.SUBMITTED, 100 + i, 100 + i))
        self.assertFalse(self.flow.document_gate(COORD, self.oid).open)
        self.assert_blocked()

    def test_the_gate_cannot_open_on_an_empty_or_unapproved_plan(self) -> None:
        flow, service, _ = make()
        oid, _ = to_review(flow, service)
        gate = flow.document_gate(COORD, oid)
        self.assertFalse(gate.open)
        self.assertTrue(any("not approved" in r for r in gate.reasons))
        cfg = service.registry.configs[CLIENT]
        service.registry.configs[CLIENT] = cfg.model_copy(update={"document_requirements": ()})
        flow2, service2, _ = make()
        service2.registry.configs[CLIENT] = cfg.model_copy(update={"document_requirements": ()})
        oid2 = approved(flow2, service2)
        self.assertTrue(any("empty list" in r for r in flow2.document_gate(COORD, oid2).reasons))

    def test_all_mandatory_documents_verified_opens_the_gate_and_triggers_both_once(self) -> None:
        verify_all(self.flow, self.oid)
        gate = self.flow.document_gate(COORD, self.oid)
        self.assertTrue(gate.open, gate.reasons)
        result = self.trigger()
        self.assertEqual(result, {fl.UC04: "triggered", fl.UC05: "triggered"})
        p04, p05 = self.uc04.payloads[0], self.uc05.payloads[0]
        for payload, uc in ((p04, fl.UC04), (p05, fl.UC05)):
            self.assertEqual((payload["use_case"], payload["case_id"], payload["client_id"], payload["plan_version"]),
                             (uc, CASE, CLIENT, 1))
            self.assertTrue(payload["verified_documents"])
            self.assertEqual(payload["correlation_id"], "CORR-1")
        self.assertTrue({t["key"] for t in p04["tasks"]} >= {"laptop_request"})
        self.assertTrue({t["key"] for t in p05["tasks"]} >= {"orientation_slot"})
        self.assertFalse({t["task_id"] for t in p04["tasks"]} & {t["task_id"] for t in p05["tasks"]})

    def test_a_duplicate_completion_event_does_not_trigger_twice(self) -> None:
        verify_all(self.flow, self.oid)
        self.trigger()
        self.trigger()
        self.assertEqual((len(self.uc04.payloads), len(self.uc05.payloads)), (1, 1))
        key = next(d.key for d in self.service.get(COORD, self.oid).documents if d.mandatory)
        replay = self.flow.apply_document_update(DOCS, doc_update(self.oid, key, DocumentState.VERIFIED, 3, 3, "VER-x"))
        self.assertIn(replay.code, {"duplicate_replay", "rejected_invalid_transition"})
        self.trigger()
        self.assertEqual((len(self.uc04.payloads), len(self.uc05.payloads)), (1, 1))

    def test_the_two_triggers_run_in_parallel(self) -> None:
        verify_all(self.flow, self.oid)
        barrier = threading.Barrier(2, timeout=5)

        class Slow:
            def __init__(self, uc):
                self.use_case = uc

            def send(self, payload):
                barrier.wait()  # both must be inside send() at the same time
                return fl.TriggerResult(True, reference="ok")

        result = self.flow.trigger_downstream(COORD, self.oid, {fl.UC04: Slow(fl.UC04), fl.UC05: Slow(fl.UC05)})
        self.assertEqual(result, {fl.UC04: "triggered", fl.UC05: "triggered"})

    def test_uc04_failing_does_not_block_uc05_and_the_reverse(self) -> None:
        verify_all(self.flow, self.oid)
        bad04 = fl.MockTrigger(fl.UC04, raise_error=True)
        result = self.flow.trigger_downstream(COORD, self.oid, {fl.UC04: bad04, fl.UC05: self.uc05})
        self.assertEqual(result, {fl.UC04: "failed", fl.UC05: "triggered"})
        flow, service, _ = make()
        oid = approved(flow, service)
        verify_all(flow, oid)
        bad05 = fl.MockTrigger(fl.UC05, fail=99)
        good04 = fl.MockTrigger(fl.UC04)
        self.assertEqual(flow.trigger_downstream(COORD, oid, {fl.UC04: good04, fl.UC05: bad05}),
                         {fl.UC04: "triggered", fl.UC05: "failed"})

    def test_a_failed_trigger_is_retried_safely_up_to_the_limit_then_needs_intervention(self) -> None:
        verify_all(self.flow, self.oid)
        flaky = fl.MockTrigger(fl.UC04, fail=1)
        self.assertEqual(self.flow.trigger_downstream(COORD, self.oid, {fl.UC04: flaky, fl.UC05: self.uc05})[fl.UC04], "failed")
        self.assertEqual(self.flow.trigger_downstream(COORD, self.oid, {fl.UC04: flaky, fl.UC05: self.uc05})[fl.UC04], "triggered")
        self.assertEqual(len(self.uc05.payloads), 1)  # UC-05 was never re-triggered by UC-04's retry
        flow, service, _ = make()
        oid = approved(flow, service)
        verify_all(flow, oid)
        dead = fl.MockTrigger(fl.UC04, fail=99)
        for _ in range(fl.MAX_TRIGGER_ATTEMPTS + 2):
            flow.trigger_downstream(COORD, oid, {fl.UC04: dead})
        self.assertEqual(flow.downstream_summary(COORD, oid)[fl.UC04], "requires_intervention")
        self.assertEqual(len(dead.payloads), fl.MAX_TRIGGER_ATTEMPTS)

    def test_status_tracking_is_independent_validated_and_ordered(self) -> None:
        verify_all(self.flow, self.oid)
        self.trigger()
        ok = self.flow.apply_downstream_status(UC04_A, event_status(self.oid, fl.UC04, DownstreamState.IN_PROGRESS, 1, 5))
        self.assertTrue(ok.accepted)
        self.assertEqual(self.flow.downstream_summary(COORD, self.oid), {fl.UC04: "in_progress", fl.UC05: "triggered"})
        self.assertEqual(self.flow.apply_downstream_status(UC05_A, event_status(self.oid, fl.UC05, DownstreamState.BLOCKED, 2, 6)).code,
                         "applied")
        self.assertEqual(self.flow.downstream_summary(COORD, self.oid), {fl.UC04: "in_progress", fl.UC05: "blocked"})
        # an older event never replaces a newer one
        self.assertEqual(self.flow.apply_downstream_status(UC04_A, event_status(self.oid, fl.UC04, DownstreamState.ACCEPTED, 3, 1)).code,
                         "stale_ignored")
        # duplicates are a replay
        dup = event_status(self.oid, fl.UC04, DownstreamState.COMPLETED, 4, 9)
        self.assertTrue(self.flow.apply_downstream_status(UC04_A, dup).accepted)
        self.assertEqual(self.flow.apply_downstream_status(UC04_A, dup).code, "duplicate_replay")
        # completed is terminal
        self.assertEqual(self.flow.apply_downstream_status(UC04_A, event_status(self.oid, fl.UC04, DownstreamState.FAILED, 5, 20)).code,
                         "ignored_after_completion")
        self.assertEqual(self.flow.downstream_summary(COORD, self.oid)[fl.UC05], "blocked")  # UC-05 untouched

    def test_invalid_downstream_events_are_rejected(self) -> None:
        verify_all(self.flow, self.oid)
        self.assertEqual(self.flow.apply_downstream_status(UC04_A, event_status(self.oid, fl.UC04, DownstreamState.IN_PROGRESS, 1)).code,
                         "rejected_not_triggered")  # nothing was triggered yet
        self.trigger()
        self.assertEqual(self.flow.apply_downstream_status(UC05_A, event_status(self.oid, fl.UC04, DownstreamState.IN_PROGRESS, 2)).code,
                         "rejected_unverified_source")  # UC-05's identity cannot speak for UC-04
        self.assertEqual(self.flow.apply_downstream_status(UC04_A, event_status(self.oid, "UC-09", DownstreamState.IN_PROGRESS, 3)).code,
                         "rejected_unknown_use_case")
        self.assertEqual(self.flow.apply_downstream_status(UC04_A, event_status(self.oid, fl.UC04, DownstreamState.TRIGGERED, 4)).code,
                         "rejected_invalid_status")
        stale = event_status(self.oid, fl.UC04, DownstreamState.IN_PROGRESS, 5).model_copy(update={"plan_version": 7})
        self.assertEqual(self.flow.apply_downstream_status(UC04_A, stale).code, "rejected_stale_plan_version")
        other = event_status("ONB-NOPE", fl.UC04, DownstreamState.IN_PROGRESS, 6)
        self.assertEqual(self.flow.apply_downstream_status(UC04_A, other).code, "rejected_unknown_case")


class PersistenceAndRecoveryTests(unittest.TestCase):
    def test_documents_tracks_and_ledger_survive_a_restart_and_nothing_duplicates(self) -> None:
        client = InMemoryTableClient()
        flow, service, source = make(store=OnboardingStore(client))
        oid = approved(flow, service)
        verify_all(flow, oid)
        flow.trigger_downstream(COORD, oid, {fl.UC04: fl.MockTrigger(fl.UC04), fl.UC05: fl.MockTrigger(fl.UC05)})
        flow.apply_downstream_status(UC04_A, event_status(oid, fl.UC04, DownstreamState.IN_PROGRESS, 1, 3))
        restarted = InMemoryTableClient.from_snapshot(client.snapshot())
        flow2, service2, _ = make(store=OnboardingStore(restarted), source=source)
        service2.hydrate(CLIENT)
        record = service2.get(COORD, oid)
        self.assertEqual(record.plan_status, PlanStatus.APPROVED)
        self.assertTrue(all(d.state is DocumentState.VERIFIED for d in record.documents if d.mandatory))
        self.assertEqual(flow2.downstream_summary(COORD, oid), {fl.UC04: "in_progress", fl.UC05: "triggered"})
        again = fl.MockTrigger(fl.UC04)
        flow2.trigger_downstream(COORD, oid, {fl.UC04: again, fl.UC05: fl.MockTrigger(fl.UC05)})
        self.assertEqual(again.payloads, [])  # no second trigger after the restart
        dup = flow2.process_approval_callback(CLIENT, approve_cb(oid, "REQ-1", cb_id="CB-1"), NOW)
        self.assertIn(dup.status, {"replayed", "unknown_request"})
        replay = flow2.apply_downstream_status(UC04_A, event_status(oid, fl.UC04, DownstreamState.IN_PROGRESS, 1, 3))
        self.assertEqual(replay.code, "duplicate_replay")
        self.assertEqual(len({t.task_id for t in record.tasks}), len(record.tasks))

    def test_a_restart_while_approval_is_pending_resumes_with_the_callback(self) -> None:
        client = InMemoryTableClient()
        flow, service, source = make(store=OnboardingStore(client))
        oid, request = to_review(flow, service)
        flow2, service2, _ = make(store=OnboardingStore(InMemoryTableClient.from_snapshot(client.snapshot())), source=source)
        service2.hydrate(CLIENT)
        # hydrate recovers the pending approval request from the store, so the callback can still complete it
        out = flow2.process_approval_callback(CLIENT, approve_cb(oid, request.request_id, cb_id="CB-R"), NOW)
        self.assertEqual(out.status, "approved")

    def test_the_audit_trail_covers_the_important_transitions_and_holds_no_personal_data(self) -> None:
        flow, service, _ = make()
        oid = approved(flow, service)
        verify_all(flow, oid)
        flow.trigger_downstream(COORD, oid, {fl.UC04: fl.MockTrigger(fl.UC04), fl.UC05: fl.MockTrigger(fl.UC05)})
        actions = {e.action for e in service.audit.events_for(COORD)}
        for needed in ("flow.event_accepted", "plan.submit", "plan.decide", "approval.callback", "workflow.initialised",
                       "document.update", "downstream.trigger"):
            self.assertIn(needed, actions)
        text = " ".join(str(e.model_dump()) for e in service.audit.events_for(COORD)).lower()
        for word in ("passport", "salary", "@", "email", "candidate"):
            self.assertNotIn(word, text)


class EndToEndTests(unittest.TestCase):
    def test_the_complete_acceptance_sequence(self) -> None:
        """UC-01 event -> config -> checklist+docs -> Fake LLM -> HR email -> approval -> docs -> gate -> UC-04/05."""
        flow, service, _ = make("uae_overseas")
        source = flow.source
        oid = flow.handle_employee_created(event(profile="uae_overseas"), TODAY).onboarding_id
        self.assertEqual(service.get(COORD, oid).config_version, sp.uae_config().version)  # configuration retrieved and used
        self.assertIn("entry_permit", {t.template_key for t in service.get(COORD, oid).tasks})  # built from it
        email = flow.prepare_hr_email(COORD, oid, TODAY)
        self.assertIn("fake_llm:", email.body)
        request = service.submit_plan(COORD, oid)
        flow.authorise_message(LEAD, oid, "hrmail", email.email_id)
        mock = MockOutlookConnector()
        self.assertEqual(flow.send_message(COORD, oid, "hrmail", email.email_id, email.subject, email.body,
                                           email.recipient_role, mock), "simulated_sent")
        self.assertEqual(service.get(COORD, oid).plan_status, PlanStatus.SUBMITTED)  # email did not approve
        self.assertEqual(flow.trigger_downstream(COORD, oid, {}), {fl.UC04: "waiting_for_documents", fl.UC05: "waiting_for_documents"})
        out = flow.process_approval_callback(CLIENT, approve_cb(oid, request.request_id), NOW)
        self.assertEqual(out.status, "approved")
        doc_request = flow.prepare_document_request(COORD, oid)
        self.assertIn("entry_permit_pack", {d["key"] for d in doc_request.documents})
        self.assertEqual(flow.trigger_downstream(COORD, oid, {fl.UC04: fl.MockTrigger(fl.UC04)})[fl.UC04], "waiting_for_documents")
        verify_all(flow, oid)
        uc04, uc05 = fl.MockTrigger(fl.UC04), fl.MockTrigger(fl.UC05)
        self.assertEqual(flow.trigger_downstream(COORD, oid, {fl.UC04: uc04, fl.UC05: uc05}),
                         {fl.UC04: "triggered", fl.UC05: "triggered"})
        flow.apply_downstream_status(UC04_A, event_status(oid, fl.UC04, DownstreamState.COMPLETED, 1, 30))
        flow.apply_downstream_status(UC05_A, event_status(oid, fl.UC05, DownstreamState.DELAYED, 2, 31))
        self.assertEqual(flow.downstream_summary(COORD, oid), {fl.UC04: "completed", fl.UC05: "delayed"})  # independent


class ConcurrencyAndReplanSafetyTests(unittest.TestCase):
    """Defects found by the readiness audit: a concurrent duplicate trigger, and a gate that ignored a re-plan."""

    class Slow:
        def __init__(self, uc):
            self.use_case, self.sends = uc, 0

        def send(self, payload):
            self.sends += 1
            time.sleep(0.15)  # long enough for a second caller to arrive while the first is sending
            return fl.TriggerResult(True, reference="ok")

    def test_two_concurrent_callers_cannot_trigger_a_downstream_twice(self) -> None:
        flow, service, _ = make()
        oid = approved(flow, service)
        verify_all(flow, oid)
        a, b = self.Slow(fl.UC04), self.Slow(fl.UC05)
        threads = [threading.Thread(target=lambda: flow.trigger_downstream(COORD, oid, {fl.UC04: a, fl.UC05: b}))
                   for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual((a.sends, b.sends), (1, 1))
        self.assertEqual(flow.downstream_summary(COORD, oid), {fl.UC04: "triggered", fl.UC05: "triggered"})

    def test_a_document_update_delivered_twice_at_once_is_applied_once(self) -> None:
        flow, service, _ = make()
        oid = approved(flow, service)
        key = next(d.key for d in service.get(COORD, oid).documents if d.mandatory)
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            flow.apply_document_update(DOCS, doc_update(oid, key, DocumentState.SUBMITTED, 1, 1)).code)) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), ["applied"] + ["duplicate_replay"] * 4)

    def replanned(self, verify_first: bool):
        flow, service, _ = make()
        oid = approved(flow, service)
        if verify_first:
            verify_all(flow, oid)
        request = service.request_change(COORD, oid, location_status="overseas")
        service.decide_change(actor(Role.HR_OPS, "U-HR-APPR"), request.request_id,
                              __import__("onboarding_task_management.contracts", fromlist=["x"]).DecisionOutcome.APPROVED)
        service.apply_change(COORD, request.request_id)
        return flow, service, oid

    def test_a_replan_that_adds_a_required_document_closes_the_gate_until_it_is_verified(self) -> None:
        flow, service, oid = self.replanned(verify_first=True)
        self.assertNotIn("entry_permit_pack", {d.key for d in service.get(COORD, oid).documents})  # stale list
        gate = flow.document_gate(COORD, oid)
        self.assertFalse(gate.open)
        self.assertTrue(any("refresh needed" in r and "entry_permit_pack" in r for r in gate.reasons))
        a, b = fl.MockTrigger(fl.UC04), fl.MockTrigger(fl.UC05)
        self.assertEqual(flow.trigger_downstream(COORD, oid, {fl.UC04: a, fl.UC05: b}),
                         {fl.UC04: "waiting_for_documents", fl.UC05: "waiting_for_documents"})
        self.assertEqual((a.payloads, b.payloads), ([], []))
        record = service.get(COORD, oid)
        new = next(d for d in record.documents if d.key == "entry_permit_pack")
        self.assertEqual((new.state, new.mandatory), (DocumentState.PENDING, True))
        self.assertTrue(all(d.state is DocumentState.VERIFIED for d in record.documents if d.mandatory and d.key != new.key))
        for n, (state, ref) in enumerate(((DocumentState.SUBMITTED, None), (DocumentState.UNDER_REVIEW, None),
                                          (DocumentState.VERIFIED, "V-new")), start=1):
            update = doc_update(oid, "entry_permit_pack", state, 100 + n, 100 + n, ref, version=record.plan_version)
            self.assertTrue(flow.apply_document_update(DOCS, update).accepted)
        self.assertTrue(flow.document_gate(COORD, oid).open)

    def test_refresh_keeps_verified_documents_drops_unverified_ones_that_no_longer_apply_and_is_idempotent(self) -> None:
        flow, service, oid = self.replanned(verify_first=False)
        changed = flow.refresh_documents(COORD, oid)
        self.assertEqual(set(changed), {"entry_permit_pack", "status_change_pack"})
        keys = {d.key for d in service.get(COORD, oid).documents}
        self.assertIn("entry_permit_pack", keys)
        self.assertNotIn("status_change_pack", keys)
        self.assertEqual(flow.refresh_documents(COORD, oid), [])
        self.assertTrue([e for e in service.audit.events_for(COORD) if e.action == "documents.refreshed"])


if __name__ == "__main__":
    unittest.main()
