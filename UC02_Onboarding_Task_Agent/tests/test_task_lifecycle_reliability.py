"""Task completion, failure, retry, evidence, cancellation and restart reliability.

Deterministic and offline: no LLM, no network. Persistence is a REAL SQLite file; a "restart" here
closes the first client and opens a second service over the same file in the same process. (Real
separate-process recovery is covered by ``test_restart_recovery.py`` and ``test_hr_workflow.py``.)

Design facts these tests pin, not invent:
  * UC-02 has no FAILED task status. A task's failure is another workflow's report
    (``external_outcome`` + reason) or a team's own delay report; the task keeps its open status.
  * UC-02 has NO per-task retry counter or retry limit. "Retry" is a corrected report under a new
    ``update_id``. Stage-level retry limits belong to the orchestrator (``stage_max_attempts``).
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from onboarding_task_management.contracts import (
    Actor, DecisionOutcome, ExternalOutcome, OnboardingStatus, Role, SourceUseCase, StatusUpdate, TaskStatus,
)
from onboarding_task_management.errors import (
    EvidenceRequiredError, InvalidStateError, PrerequisiteNotMetError, UnauthorizedActionError,
)
from onboarding_task_management.persistence import OnboardingStore
from onboarding_task_management.sqlite_store import SqliteTableClient
from onboarding_task_management.sub_agent import status_updates
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import CLIENT_ALPHA, build_registry, make_trigger, persona
from tests.support import HR_OPS, HR_OPS_APPROVER

OCCURRED = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
OPS = Actor(actor_id="svc-ops-TEST", role=Role.SYSTEM, client_id=CLIENT_ALPHA)
UC03 = Actor(actor_id="svc-uc03-TEST", role=Role.SYSTEM, client_id=CLIENT_ALPHA)
PRINCIPALS = {
    SourceUseCase.OPERATIONS: frozenset({OPS.actor_id}),
    SourceUseCase.UC03_DOCUMENT_VALIDATION: frozenset({UC03.actor_id}),
}
CLOCK = date(2026, 11, 2)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="uc02-lifecycle-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = self.dir / "uc02.sqlite3"
        self._clients: list[SqliteTableClient] = []
        self.addCleanup(lambda: [c.close() for c in self._clients])
        self.service = self.open()
        self.oid = self.service.create_onboarding(HR_OPS, make_trigger(persona("EMP-007"))).onboarding_id
        request = self.service.submit_plan(HR_OPS, self.oid)
        self.service.decide_plan(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)

    def open(self) -> OnboardingService:
        client = SqliteTableClient(self.db)
        self._clients.append(client)
        service = OnboardingService(
            build_registry(), store=OnboardingStore(client), source_principals=PRINCIPALS,
            require_plan_approval=True)
        return service

    def restart(self) -> OnboardingService:
        """Close the running service's database and open a brand-new service over the same file."""
        self._clients[-1].close()
        service = self.open()
        self.assertEqual(service.hydrate(CLIENT_ALPHA), 1)
        return service

    def tid(self, key: str) -> str:
        return f"{self.oid}#{key}"

    def task(self, service, key):
        return service.get(HR_OPS, self.oid).task_by_key(key)

    def report(self, service, key, outcome, *, source=SourceUseCase.OPERATIONS, who=OPS, uid=None, **extra):
        update = StatusUpdate(
            update_id=uid or f"u-{key}-{outcome.value}", source=source, client_id=CLIENT_ALPHA,
            onboarding_id=self.oid, task_id=self.tid(key), outcome=outcome, occurred_at=OCCURRED, **extra)
        return service.apply_status_update(who, self.oid, update)

    def done_events(self, service, key):
        return [e for e in service.audit.events_for(HR_OPS)
                if e.action == "task.complete" and e.task_id == self.tid(key) and e.outcome == "complete"]


class CompletionAndEvidenceTests(_Base):
    def test_evidence_task_cannot_complete_without_valid_evidence_through_either_path(self) -> None:
        s = self.service
        s.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        for evidence in (None, "", "   \t"):
            with self.assertRaises(EvidenceRequiredError):
                s.complete_task(HR_OPS, self.oid, self.tid("salary_transfer_letter"), evidence)
        for reference in (None, "  "):  # the external-report path applies the same rule
            result = self.report(s, "salary_transfer_letter", ExternalOutcome.COMPLETED, reference=reference)
            self.assertEqual(result.rejection_code, status_updates.REJECT_MISSING_EVIDENCE)
        self.assertNotEqual(self.task(s, "salary_transfer_letter").status, TaskStatus.COMPLETE)
        self.assertIsNone(self.task(s, "salary_transfer_letter").completed_at)
        ok = self.report(s, "salary_transfer_letter", ExternalOutcome.COMPLETED, reference="CONF-1")
        self.assertTrue(ok.accepted)
        self.assertEqual(self.task(s, "salary_transfer_letter").evidence_ref, "CONF-1")

    def test_evidence_and_completion_survive_a_restart_and_a_repeat_completion_is_refused(self) -> None:
        s = self.service
        s.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        s.complete_task(HR_OPS, self.oid, self.tid("salary_transfer_letter"), "EVID-9")
        original = self.task(s, "salary_transfer_letter").completed_at
        s2 = self.restart()
        stored = self.task(s2, "salary_transfer_letter")
        self.assertEqual((stored.status, stored.evidence_ref, stored.completed_at),
                         (TaskStatus.COMPLETE, "EVID-9", original))
        events_before = len(self.done_events(s2, "salary_transfer_letter"))
        for _ in range(2):
            with self.assertRaises(InvalidStateError):
                s2.complete_task(HR_OPS, self.oid, self.tid("salary_transfer_letter"), "EVID-9")
        self.assertEqual(self.task(s2, "salary_transfer_letter").completed_at, original)
        self.assertEqual(len(self.done_events(s2, "salary_transfer_letter")), events_before)  # no second "complete"

    def test_dependents_stay_blocked_then_unblock_and_the_unblock_survives_a_restart(self) -> None:
        s = self.service
        with self.assertRaises(PrerequisiteNotMetError):
            s.complete_task(HR_OPS, self.oid, self.tid("collect_documents"))
        self.assertEqual(self.task(s, "collect_documents").status, TaskStatus.BLOCKED)
        s.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        s2 = self.restart()
        self.assertEqual(self.task(s2, "collect_documents").status, TaskStatus.READY)
        self.assertEqual(self.task(s2, "it_laptop").status, TaskStatus.READY)

    def test_only_the_owner_or_hr_can_complete_and_the_refusal_is_audited(self) -> None:
        outsider = Actor(actor_id="U-FIN-1", role=Role.FINANCE, client_id=CLIENT_ALPHA)
        with self.assertRaises(UnauthorizedActionError):
            self.service.complete_task(outsider, self.oid, self.tid("hr_record_create"))
        denied = [e for e in self.service.audit.events_for(HR_OPS)
                  if e.action == "task.complete" and e.outcome == "denied"]
        self.assertTrue(denied)
        self.assertEqual(self.task(self.service, "hr_record_create").status, TaskStatus.READY)


class FailureAndRetryTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.service.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))

    def test_a_reported_failure_persists_and_a_corrected_report_retries_it_after_restart(self) -> None:
        s = self.service
        failed = self.report(s, "collect_documents", ExternalOutcome.INVALID, source=SourceUseCase.UC03_DOCUMENT_VALIDATION,
                             who=UC03, blocking_reason="synthetic: unreadable scan", uid="try-1")
        self.assertTrue(failed.accepted)
        s2 = self.restart()
        held = self.task(s2, "collect_documents")
        self.assertEqual((held.external_outcome, held.external_blocking_reason),
                         (ExternalOutcome.INVALID, "synthetic: unreadable scan"))
        self.assertNotEqual(held.status, TaskStatus.COMPLETE)
        self.assertEqual(self.task(s2, "payroll_setup").status, TaskStatus.BLOCKED)  # dependants still held
        # The failed attempt's id is a no-op if replayed; the retry is a NEW update.
        replay = self.report(s2, "collect_documents", ExternalOutcome.INVALID,
                             source=SourceUseCase.UC03_DOCUMENT_VALIDATION, who=UC03,
                             blocking_reason="synthetic: unreadable scan", uid="try-1")
        self.assertTrue(replay.duplicate_replay)
        retry = self.report(s2, "collect_documents", ExternalOutcome.VALID,
                            source=SourceUseCase.UC03_DOCUMENT_VALIDATION, who=UC03, uid="try-2")
        self.assertTrue(retry.accepted and not retry.duplicate_replay)
        self.assertEqual(self.task(s2, "collect_documents").status, TaskStatus.COMPLETE)
        self.assertEqual(self.task(s2, "collect_documents").external_blocking_reason, "")
        self.assertEqual(self.task(s2, "payroll_setup").status, TaskStatus.READY)
        s3 = self.restart()
        self.assertEqual(self.task(s3, "collect_documents").status, TaskStatus.COMPLETE)

    def test_repeated_failure_reports_do_not_multiply_audit_events(self) -> None:
        s = self.service
        kwargs = dict(source=SourceUseCase.UC03_DOCUMENT_VALIDATION, who=UC03,
                      blocking_reason="synthetic", uid="same-id")
        self.report(s, "collect_documents", ExternalOutcome.DELAYED, **kwargs)
        count = len(s.audit.events_for(HR_OPS))
        for _ in range(3):
            self.assertTrue(self.report(s, "collect_documents", ExternalOutcome.DELAYED, **kwargs).duplicate_replay)
        self.assertEqual(len(s.audit.events_for(HR_OPS)), count)
        flags = [f for f in s.follow_up_flags(HR_OPS, self.oid, CLOCK)
                 if f.template_key == "collect_documents" and f.code == "external_unresolved"]
        self.assertEqual(len(flags), 1)

    def test_a_delay_report_survives_a_restart_and_clears_on_completion(self) -> None:
        s = self.service
        s.report_delay(HR_OPS, self.oid, self.tid("medical_test"), "synthetic: no slot")
        s2 = self.restart()
        task = self.task(s2, "medical_test")
        self.assertEqual((task.delay_reason, task.delay_kind), ("synthetic: no slot", "delay"))
        self.assertIn(("medical_test", "delayed"),
                      {(f.template_key, f.code) for f in s2.follow_up_flags(HR_OPS, self.oid, CLOCK)})
        s2.complete_task(HR_OPS, self.oid, self.tid("medical_test"), "EVID")
        self.assertEqual(self.task(self.restart(), "medical_test").delay_reason, "")

    def test_there_is_no_per_task_retry_counter_in_uc02(self) -> None:
        """Pinned so nobody assumes one exists: the model carries no attempt/retry fields."""
        fields = set(type(self.task(self.service, "collect_documents")).model_fields)
        self.assertFalse({f for f in fields if "retry" in f or "attempt" in f})


class CancellationTests(_Base):
    def cancel(self, service):
        request = service.request_cancellation(HR_OPS, self.oid, "synthetic: offer withdrawn")
        service.decide_change(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        return service.apply_cancellation(HR_OPS, request.request_id)

    def test_cancellation_needs_approval_and_a_date_change_approval_cannot_cancel(self) -> None:
        s = self.service
        pending = s.request_cancellation(HR_OPS, self.oid, "synthetic")
        with self.assertRaises(Exception):
            s.apply_cancellation(HR_OPS, pending.request_id)  # not approved yet
        change = s.request_change(HR_OPS, self.oid, joining_date=date(2026, 11, 30))
        s.decide_change(HR_OPS_APPROVER, change.request_id, DecisionOutcome.APPROVED)
        with self.assertRaises(UnauthorizedActionError):
            s.apply_cancellation(HR_OPS, change.request_id)  # wrong gate
        self.assertEqual(s.get(HR_OPS, self.oid).status, OnboardingStatus.ACTIVE)

    def test_cancelled_work_cannot_be_completed_or_reported_and_it_survives_a_restart(self) -> None:
        s = self.service
        s.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        self.cancel(s)
        s2 = self.restart()
        record = s2.get(HR_OPS, self.oid)
        self.assertEqual(record.status, OnboardingStatus.CANCELLED)
        self.assertEqual(self.task(s2, "hr_record_create").status, TaskStatus.COMPLETE)  # done work stands
        self.assertEqual(self.task(s2, "collect_documents").status, TaskStatus.CANCELLED)
        self.assertIsNotNone(self.task(s2, "collect_documents").cancelled_at)
        with self.assertRaises(InvalidStateError):
            s2.complete_task(HR_OPS, self.oid, self.tid("collect_documents"))
        with self.assertRaises(InvalidStateError):
            s2.start_task(HR_OPS, self.oid, self.tid("it_laptop"))
        result = self.report(s2, "it_laptop", ExternalOutcome.COMPLETED, reference="X")
        self.assertEqual(result.rejection_code, status_updates.REJECT_NOT_ACTIVE)
        with self.assertRaises(InvalidStateError):  # an applied cancellation cannot be replayed
            s2.apply_cancellation(HR_OPS, s2.store.load_approvals(CLIENT_ALPHA)[-1].request_id)


class ReplanPreservesStateTests(_Base):
    def test_replan_after_restart_keeps_completed_and_failed_state(self) -> None:
        s = self.service
        s.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        s.complete_task(HR_OPS, self.oid, self.tid("salary_transfer_letter"), "EVID-1")
        self.report(s, "collect_documents", ExternalOutcome.REQUIRES_REVIEW,
                    source=SourceUseCase.UC03_DOCUMENT_VALIDATION, who=UC03, blocking_reason="synthetic")
        s2 = self.restart()
        request = s2.request_change(HR_OPS, self.oid, joining_date=date(2026, 11, 30))
        with self.assertRaises(Exception):
            s2.apply_change(HR_OPS, request.request_id)  # no approval yet
        s2.decide_change(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        outcome = s2.apply_change(HR_OPS, request.request_id)
        s3 = self.restart()
        self.assertEqual(self.task(s3, "hr_record_create").status, TaskStatus.COMPLETE)
        self.assertEqual(self.task(s3, "salary_transfer_letter").evidence_ref, "EVID-1")
        self.assertEqual(self.task(s3, "collect_documents").external_outcome, ExternalOutcome.REQUIRES_REVIEW)
        self.assertNotIn(self.tid("hr_record_create"), outcome.rescheduled_task_ids)
        self.assertEqual(s3.get(HR_OPS, self.oid).revision, 1)
        self.assertEqual(len(s3.store.plan_revisions(CLIENT_ALPHA, self.oid)), 1)


class PerTaskRetryCounterTests(unittest.TestCase):
    """The per-task failed-report counter and the optional, client-configured limit.

    No limit is approved anywhere in the repository, so the default is NO limit; these tests use a
    synthetic limit of 2 set on a synthetic template. They do not state a policy.
    """

    def setUp(self) -> None:
        from onboarding_task_management.contracts import TaskCategory
        from onboarding_task_management.sub_agent.checklist import (
            ClientOnboardingConfig, ConfigRegistry, TaskTemplate)

        self.dir = Path(tempfile.mkdtemp(prefix="uc02-retry-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = self.dir / "uc02.sqlite3"
        self._clients: list[SqliteTableClient] = []
        self.addCleanup(lambda: [c.close() for c in self._clients])

        def registry(limit):
            reg = ConfigRegistry()
            reg.register(ClientOnboardingConfig(
                client_id=CLIENT_ALPHA, approval_status="approved",
                templates=(
                    TaskTemplate(key="root", category=TaskCategory.HR_RECORD, title="r", title_ar="r",
                                 owner=Role.HR_OPS, offset_days=-5),
                    TaskTemplate(key="docs", category=TaskCategory.DOCUMENT_COLLECTION, title="d", title_ar="d",
                                 owner=Role.HR_OPS, offset_days=-3, depends_on=("root",),
                                 max_failed_reports=limit),
                )))
            return reg

        self.registry = registry
        self.service = self.open(limit=2)
        self.oid = self.service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).onboarding_id
        self.service.complete_task(HR_OPS, self.oid, f"{self.oid}#root")

    def open(self, limit):
        client = SqliteTableClient(self.db)
        self._clients.append(client)
        return OnboardingService(self.registry(limit), store=OnboardingStore(client),
                                 source_principals=PRINCIPALS)

    def send(self, outcome, uid, reason="synthetic"):
        needs_reason = outcome in (ExternalOutcome.INVALID, ExternalOutcome.REQUIRES_REVIEW,
                                   ExternalOutcome.SUSPECTED_FRAUD, ExternalOutcome.DELAYED)
        update = StatusUpdate(
            update_id=uid, source=SourceUseCase.UC03_DOCUMENT_VALIDATION, client_id=CLIENT_ALPHA,
            onboarding_id=self.oid, task_id=f"{self.oid}#docs", outcome=outcome, occurred_at=OCCURRED,
            blocking_reason=reason if needs_reason else "")
        return self.service.apply_status_update(UC03, self.oid, update)

    def docs(self, service=None):
        return (service or self.service).get(HR_OPS, self.oid).task_by_key("docs")

    def test_each_failed_report_is_counted_once_and_only_failures_count(self) -> None:
        self.assertEqual(self.docs().failed_report_count, 0)
        self.send(ExternalOutcome.DELAYED, "d1")  # late is not failed
        self.send(ExternalOutcome.IN_PROGRESS, "p1")
        self.assertEqual(self.docs().failed_report_count, 0)
        self.send(ExternalOutcome.INVALID, "f1")
        self.send(ExternalOutcome.INVALID, "f1")  # replay of the same report: not another attempt
        self.assertEqual(self.docs().failed_report_count, 1)
        self.send(ExternalOutcome.REQUIRES_REVIEW, "f2")
        self.assertEqual(self.docs().failed_report_count, 2)
        refused = self.send(ExternalOutcome.INVALID, "f3", reason="")  # no reason: refused, not an attempt
        self.assertFalse(refused.accepted)
        self.assertEqual(self.docs().failed_report_count, 2)

    def test_the_counter_and_the_limit_survive_a_restart_and_success_does_not_reset_the_history(self) -> None:
        self.send(ExternalOutcome.INVALID, "f1")
        self._clients[-1].close()
        restarted = self.open(limit=2)
        restarted.hydrate(CLIENT_ALPHA)
        task = self.docs(restarted)
        self.assertEqual((task.failed_report_count, task.max_failed_reports), (1, 2))
        self.service = restarted
        self.assertTrue(self.send(ExternalOutcome.VALID, "ok").accepted)
        self.assertEqual(self.docs().status, TaskStatus.COMPLETE)
        self.assertEqual(self.docs().failed_report_count, 1)  # history kept

    def test_no_limit_is_applied_unless_the_client_configured_one(self) -> None:
        self._clients[-1].close()
        self.service = self.open(limit=None)
        self.oid = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-002"))).onboarding_id  # a case planned with no limit
        self.service.complete_task(HR_OPS, self.oid, f"{self.oid}#root")
        for n in range(5):
            self.send(ExternalOutcome.INVALID, f"u{n}")
        task = self.docs()
        self.assertEqual(task.failed_report_count, 5)
        self.assertIsNone(task.max_failed_reports)
        self.assertFalse(task.retry_limit_reached)
        codes = {f.code for f in self.service.follow_up_flags(HR_OPS, self.oid, CLOCK)}
        self.assertNotIn("retry_limit_reached", codes)

    def test_reaching_the_configured_limit_flags_the_task_and_escalates_to_the_hr_lead_once(self) -> None:
        self.send(ExternalOutcome.INVALID, "f1")
        self.assertNotIn("retry_limit_reached",
                         {f.code for f in self.service.follow_up_flags(HR_OPS, self.oid, CLOCK)})
        self.send(ExternalOutcome.REQUIRES_REVIEW, "f2")
        self.assertTrue(self.docs().retry_limit_reached)
        flags = [f for f in self.service.follow_up_flags(HR_OPS, self.oid, CLOCK)
                 if f.code == "retry_limit_reached"]
        self.assertEqual(len(flags), 1)
        self.assertIn("limit 2", flags[0].reason)
        first = self.service.monitoring_sweep(HR_OPS, self.oid, CLOCK)
        retry_items = [e for e in first.escalations if e.subject_type == "task_retry_limit"]
        self.assertEqual(len(retry_items), 1)
        self.assertEqual((retry_items[0].level.value, retry_items[0].escalate_to), ("hr_ops_lead", Role.HR_LEAD))
        second = self.service.monitoring_sweep(HR_OPS, self.oid, CLOCK)
        self.assertEqual(second.newly_recorded_escalations, 0)  # recorded once, not once per sweep
        history = [e for e in self.service.escalation_history(HR_OPS, self.oid)
                   if e.subject_type == "task_retry_limit"]
        self.assertEqual([e.status for e in history], ["open"])

    def test_a_resolving_report_is_still_accepted_after_the_limit_and_closes_the_escalation(self) -> None:
        self.send(ExternalOutcome.INVALID, "f1")
        self.send(ExternalOutcome.INVALID, "f2")
        self.service.monitoring_sweep(HR_OPS, self.oid, CLOCK)
        self.assertTrue(self.send(ExternalOutcome.VALID, "fix").accepted)  # correcting is the goal
        self.assertFalse(self.docs().retry_limit_reached)  # no longer unresolved
        history = [e for e in self.service.escalation_history(HR_OPS, self.oid)
                   if e.subject_type == "task_retry_limit"]
        self.assertTrue(history and all(e.status == "resolved" for e in history))

    def test_the_limit_comes_from_the_template_and_must_be_positive(self) -> None:
        from onboarding_task_management.contracts import TaskCategory
        from onboarding_task_management.sub_agent.checklist import TaskTemplate

        self.assertEqual(self.docs().max_failed_reports, 2)
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                TaskTemplate(key="x", category=TaskCategory.HR_RECORD, title="x", title_ar="x",
                             owner=Role.HR_OPS, offset_days=0, max_failed_reports=bad)


class AuditAccuracyTests(_Base):
    """Every audit event says what really happened, in the right field, with no personal data."""

    STATUS_VOCABULARY = {
        *(s.value for s in TaskStatus),
        "proposed", "submitted", "approved", "rejected", "not_submitted",
    }

    def test_assignment_events_use_assignee_fields_and_never_the_status_fields(self) -> None:
        s = self.service
        s.assign_owner(HR_OPS, self.oid, self.tid("hr_record_create"), "U-A")
        s.assign_owner(HR_OPS, self.oid, self.tid("hr_record_create"), "U-B")
        events = [e for e in s.audit.events_for(HR_OPS) if e.action == "task.assign_owner" and e.outcome == "assigned"]
        self.assertEqual([(e.previous_assignee_id, e.current_assignee_id) for e in events],
                         [(None, "U-A"), ("U-A", "U-B")])
        self.assertTrue(all(e.before_status is None and e.after_status is None for e in events))

    def test_every_before_and_after_status_is_a_real_status_never_an_identifier(self) -> None:
        s = self.service
        t = self.tid
        s.assign_owner(HR_OPS, self.oid, t("hr_record_create"), "U-A")
        s.start_task(HR_OPS, self.oid, t("hr_record_create"))
        s.complete_task(HR_OPS, self.oid, t("hr_record_create"))
        s.report_delay(HR_OPS, self.oid, t("medical_test"), "synthetic")
        self.report(s, "collect_documents", ExternalOutcome.INVALID, source=SourceUseCase.UC03_DOCUMENT_VALIDATION,
                    who=UC03, blocking_reason="synthetic")
        s.decide_final(HR_OPS_APPROVER, s.submit_final(HR_OPS, self.oid, CLOCK).request_id,
                       DecisionOutcome.REJECTED, "synthetic")
        seen = [(e.action, e.before_status, e.after_status) for e in s.audit.events_for(HR_OPS)
                if e.before_status is not None or e.after_status is not None]
        self.assertGreaterEqual(len(seen), 6)
        for action, before, after in seen:
            for value in (before, after):
                self.assertTrue(value is None or value in self.STATUS_VOCABULARY, (action, before, after))

    def test_no_audit_event_carries_a_reason_text_or_personal_keys(self) -> None:
        s = self.service
        request = s.submit_plan(HR_OPS, self.oid) if s.get(HR_OPS, self.oid).plan_status.value == "proposed" else None
        s.report_delay(HR_OPS, self.oid, self.tid("medical_test"), "free text that must stay out of the log")
        text = " ".join(str(e.model_dump()) for e in s.audit.events_for(HR_OPS))
        self.assertNotIn("free text that must stay out of the log", text)
        for e in s.audit.events_for(HR_OPS):
            self.assertFalse({"name", "email", "phone", "nationality", "passport", "address"} & set(e.detail))


class MonitoringReliabilityTests(_Base):
    """Due-soon, overdue and at-risk are told apart; repeat sweeps (also across a restart) add nothing."""

    def test_due_soon_overdue_and_at_risk_are_distinguished_by_the_test_clock(self) -> None:
        s = self.service
        soon = s.monitoring_sweep(HR_OPS, self.oid, date(2026, 11, 2))
        reasons_soon = {d.reason for d in soon.reminder_drafts}
        self.assertIn("due_soon", reasons_soon)
        self.assertNotIn("overdue", {d.reason for d in soon.reminder_drafts if any("medical_test" in t for t in d.task_ids)})
        late = s.monitoring_sweep(HR_OPS, self.oid, date(2026, 11, 9))
        self.assertIn("overdue", {d.reason for d in late.reminder_drafts})
        self.assertTrue(any("overdue" in e.reason for e in late.escalations))
        near = s.monitoring_sweep(HR_OPS, self.oid, date(2026, 11, 13))  # inside the joining-risk window
        self.assertTrue(any("day-1 critical" in e.reason or "overdue" in e.reason for e in near.escalations))
        self.assertTrue(s.joining_risk(HR_OPS, self.oid, date(2026, 11, 13)).at_risk)

    def test_reminders_are_drafts_and_nothing_claims_delivery(self) -> None:
        s = self.service
        result = s.monitoring_sweep(HR_OPS, self.oid, date(2026, 11, 9))
        self.assertTrue(result.reminder_drafts)
        self.assertTrue(all(d.delivery_status == "draft_not_sent" for d in result.reminder_drafts))
        self.assertIn("nothing was sent", result.delivery)
        outcomes = {e.outcome for e in s.audit.events_for(HR_OPS) if e.action == "monitor.reminder_draft"}
        self.assertEqual(outcomes, {"drafted_not_sent"})
        from onboarding_task_management.sub_agent import reminders as reminders_module

        self.assertFalse([n for n in dir(reminders_module) if n.lower() in {"send", "deliver", "dispatch"}])  # no transport

    def test_repeat_sweeps_add_no_records_even_across_a_restart(self) -> None:
        s = self.service
        first = s.monitoring_sweep(HR_OPS, self.oid, date(2026, 11, 9))
        self.assertGreater(first.newly_recorded_escalations + first.newly_recorded_reminders, 0)
        again = s.monitoring_sweep(HR_OPS, self.oid, date(2026, 11, 9))
        self.assertEqual((again.newly_recorded_escalations, again.newly_recorded_reminders), (0, 0))
        events = len([e for e in s.audit.events_for(HR_OPS) if e.action.startswith("monitor.")])
        s2 = self.restart()
        after = s2.monitoring_sweep(HR_OPS, self.oid, date(2026, 11, 9))
        self.assertEqual((after.newly_recorded_escalations, after.newly_recorded_reminders), (0, 0))
        self.assertEqual(len([e for e in s2.audit.events_for(HR_OPS) if e.action.startswith("monitor.")]), events)
        self.assertTrue(s2.escalation_history(HR_OPS, self.oid))  # the history itself survived

    def test_the_sweep_is_refused_before_hr_releases_the_plan_and_for_non_hr_roles(self) -> None:
        fresh = OnboardingService(build_registry(), require_plan_approval=True)
        oid = fresh.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).onboarding_id
        from onboarding_task_management.errors import PlanNotApprovedError

        with self.assertRaises(PlanNotApprovedError):
            fresh.monitoring_sweep(HR_OPS, oid, CLOCK)
        outsider = Actor(actor_id="U-FIN-1", role=Role.FINANCE, client_id=CLIENT_ALPHA)
        with self.assertRaises(UnauthorizedActionError):
            self.service.monitoring_sweep(outsider, self.oid, CLOCK)


if __name__ == "__main__":
    unittest.main()
