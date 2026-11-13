"""HR plan approval, team views, delays, monitoring, final review and readiness.

In-memory unless noted. ``BusinessScenarioProcessTests`` runs the scenario script A-G in real
separate processes against SQLite files. Team members and events are SYNTHETIC.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from onboarding_task_management.contracts import (
    Actor, DecisionOutcome, ExternalOutcome, FinalStatus, PlanStatus, Role, SourceUseCase,
    StatusUpdate, TaskCategory, TaskStatus,
)
from onboarding_task_management.errors import (
    ApprovalRequiredError, DataIsolationError, InvalidStateError, PlanNotApprovedError,
    UnauthorizedActionError,
)
from onboarding_task_management.sub_agent import status_updates
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig, ConfigRegistry, TaskTemplate,
)
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import CLIENT_ALPHA, build_registry, make_trigger, persona
from tests.support import EMPLOYEE, HR_OPS, HR_OPS_APPROVER, HR_OPS_BETA, IT_MANAGER

CLOCK = date(2026, 11, 2)
UC03 = Actor(actor_id="svc-uc03-TEST", role=Role.SYSTEM, client_id=CLIENT_ALPHA)
PRINCIPALS = {SourceUseCase.UC03_DOCUMENT_VALIDATION: frozenset({UC03.actor_id})}
ROOT = Path(__file__).resolve().parents[1]


def gated_service(**kw) -> OnboardingService:
    return OnboardingService(build_registry(), require_plan_approval=True,
                             source_principals=PRINCIPALS, **kw)


def new_case(service, employee="EMP-001"):
    return service.create_onboarding(HR_OPS, make_trigger(persona(employee))).onboarding_id


def approve(service, oid):
    request = service.submit_plan(HR_OPS, oid)
    return service.decide_plan(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)


class PlanApprovalGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = gated_service()
        self.oid = new_case(self.service)
        self.root = f"{self.oid}#hr_record_create"

    def test_a_new_plan_is_proposed_and_blocks_all_team_execution(self) -> None:
        record = self.service.get(HR_OPS, self.oid)
        self.assertEqual((record.plan_status, record.plan_version), (PlanStatus.PROPOSED, 1))
        for call in (lambda: self.service.start_task(HR_OPS, self.oid, self.root),
                     lambda: self.service.complete_task(HR_OPS, self.oid, self.root),
                     lambda: self.service.report_delay(HR_OPS, self.oid, self.root, "late"),
                     lambda: self.service.monitoring_sweep(HR_OPS, self.oid, CLOCK)):
            with self.assertRaises(PlanNotApprovedError):
                call()
        self.assertEqual(self.service.get(HR_OPS, self.oid).task_by_key("hr_record_create").status,
                         TaskStatus.READY)
        self.assertTrue(any(e.outcome == "denied_plan_not_approved" for e in self.service.audit.events_for(HR_OPS)))

    def test_status_reports_are_refused_until_the_plan_is_approved(self) -> None:
        update = StatusUpdate(update_id="u1", source=SourceUseCase.UC03_DOCUMENT_VALIDATION,
                              client_id=CLIENT_ALPHA, onboarding_id=self.oid,
                              task_id=f"{self.oid}#collect_documents", outcome=ExternalOutcome.VALID,
                              occurred_at=datetime(2026, 11, 2, tzinfo=timezone.utc))
        result = self.service.apply_status_update(UC03, self.oid, update)
        self.assertEqual(result.rejection_code, status_updates.REJECT_PLAN_NOT_APPROVED)
        # An unverified caller learns nothing about the plan: it is refused as unverified first.
        stranger = Actor(actor_id="stranger", role=Role.SYSTEM, client_id=CLIENT_ALPHA)
        self.assertEqual(self.service.apply_status_update(stranger, self.oid, update).rejection_code,
                         status_updates.REJECT_SOURCE_UNAUTHENTICATED)

    def test_approval_releases_execution_and_records_who_when_and_which_version(self) -> None:
        record = approve(self.service, self.oid)
        self.assertEqual(record.plan_status, PlanStatus.APPROVED)
        done = self.service.complete_task(HR_OPS, self.oid, self.root)
        self.assertEqual(done.status, TaskStatus.COMPLETE)
        events = [(e.version, e.action, e.by) for e in record.plan_events]
        self.assertEqual(events, [(1, "proposed", HR_OPS.actor_id), (1, "submitted", HR_OPS.actor_id),
                                  (1, "approved", HR_OPS_APPROVER.actor_id)])
        decide = [e for e in self.service.audit.events_for(HR_OPS) if e.action == "plan.decide"][-1]
        self.assertEqual((decide.before_status, decide.after_status, decide.approver_id),
                         ("submitted", "approved", HR_OPS_APPROVER.actor_id))
        self.assertIsNotNone(decide.at.tzinfo)

    def test_only_an_authorised_hr_role_can_submit_or_decide(self) -> None:
        with self.assertRaises(UnauthorizedActionError):
            self.service.submit_plan(IT_MANAGER, self.oid)
        request = self.service.submit_plan(HR_OPS, self.oid)
        for who in (IT_MANAGER, EMPLOYEE):
            with self.assertRaises(UnauthorizedActionError):
                self.service.decide_plan(who, request.request_id, DecisionOutcome.APPROVED)
        with self.assertRaises(UnauthorizedActionError):  # the requester cannot approve their own plan
            self.service.decide_plan(HR_OPS, request.request_id, DecisionOutcome.APPROVED)
        with self.assertRaises(DataIsolationError):  # another client cannot decide it
            self.service.decide_plan(HR_OPS_BETA, request.request_id, DecisionOutcome.APPROVED)
        self.assertEqual(self.service.get(HR_OPS, self.oid).plan_status, PlanStatus.SUBMITTED)

    def test_rejection_needs_a_reason_and_a_decision_cannot_be_repeated(self) -> None:
        request = self.service.submit_plan(HR_OPS, self.oid)
        with self.assertRaises(InvalidStateError):
            self.service.decide_plan(HR_OPS_APPROVER, request.request_id, DecisionOutcome.REJECTED, "")
        self.assertEqual(self.service.get(HR_OPS, self.oid).plan_status, PlanStatus.SUBMITTED)
        self.service.decide_plan(HR_OPS_APPROVER, request.request_id, DecisionOutcome.REJECTED, "fix owners")
        for outcome in (DecisionOutcome.APPROVED, DecisionOutcome.REJECTED):
            with self.assertRaises(InvalidStateError):
                self.service.decide_plan(HR_OPS_APPROVER, request.request_id, outcome, "again")
        self.assertEqual(self.service.get(HR_OPS, self.oid).plan_status, PlanStatus.REJECTED)

    def test_a_rejected_plan_is_revised_to_a_new_version_and_resubmitted(self) -> None:
        r1 = self.service.submit_plan(HR_OPS, self.oid)
        self.service.decide_plan(HR_OPS_APPROVER, r1.request_id, DecisionOutcome.REJECTED, "owners")
        with self.assertRaises(InvalidStateError):
            self.service.submit_plan(HR_OPS, self.oid)  # not before it is revised
        with self.assertRaises(InvalidStateError):
            self.service.revise_plan(HR_OPS, self.oid, "  ")  # must say what changed
        revised = self.service.revise_plan(HR_OPS, self.oid, "owners assigned")
        self.assertEqual((revised.plan_version, revised.plan_status), (2, PlanStatus.PROPOSED))
        r2 = self.service.submit_plan(HR_OPS, self.oid)
        with self.assertRaises(InvalidStateError):  # the old request no longer matches the plan under review
            self.service.decide_plan(HR_OPS_APPROVER, r1.request_id, DecisionOutcome.APPROVED)
        self.assertEqual(self.service.decide_plan(HR_OPS_APPROVER, r2.request_id,
                                                  DecisionOutcome.APPROVED).plan_status, PlanStatus.APPROVED)
        self.assertEqual([e.action for e in self.service.get(HR_OPS, self.oid).plan_events],
                         ["proposed", "submitted", "rejected", "revised", "submitted", "approved"])

    def test_a_plan_cannot_be_revised_once_work_has_started(self) -> None:
        approve(self.service, self.oid)
        self.service.complete_task(HR_OPS, self.oid, self.root)
        with self.assertRaises(InvalidStateError):
            self.service.revise_plan(HR_OPS, self.oid, "late change")  # not rejected: use an approved change

    def test_the_gate_is_off_by_default_for_direct_service_use(self) -> None:
        legacy = OnboardingService(build_registry())
        oid = new_case(legacy)
        self.assertFalse(legacy.require_plan_approval)
        self.assertEqual(legacy.complete_task(HR_OPS, oid, f"{oid}#hr_record_create").status, TaskStatus.COMPLETE)

    def test_the_application_factory_turns_the_gate_on(self) -> None:
        from onboarding_task_management.sub_agent.runtime import build_service

        self.assertTrue(build_service({}).require_plan_approval)


class TeamViewsAndDelaysTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = gated_service()
        self.oid = new_case(self.service, "EMP-007")
        approve(self.service, self.oid)

    def test_team_summary_groups_tasks_by_configured_team(self) -> None:
        rows = {r.team: r for r in self.service.team_summary(HR_OPS, self.oid, CLOCK)}
        self.assertEqual(sum(r.total for r in rows.values()), 23)
        self.assertTrue({"hr_ops", "it_manager", "facilities", "finance", "pro"} <= set(rows))
        self.assertEqual(rows["pro"].total, 1)
        with self.assertRaises(UnauthorizedActionError):
            self.service.team_summary(IT_MANAGER, self.oid, CLOCK)

    def test_a_team_sees_only_its_own_tasks(self) -> None:
        mine = self.service.team_tasks(IT_MANAGER, self.oid)
        self.assertTrue(mine and all(t.owner is Role.IT_MANAGER for t in mine))
        with self.assertRaises(UnauthorizedActionError):
            self.service.team_tasks(IT_MANAGER, self.oid, Role.HR_OPS)
        self.assertGreater(len(self.service.team_tasks(HR_OPS, self.oid, Role.FACILITIES)), 0)

    def test_assignment_history_is_audited_with_previous_and_new_assignee(self) -> None:
        tid = f"{self.oid}#medical_test"
        self.service.assign_owner(HR_OPS, self.oid, tid, "U-A", team="Medical")
        self.service.assign_owner(HR_OPS, self.oid, tid, "U-B", team="Medical")
        events = [e for e in self.service.audit.events_for(HR_OPS)
                  if e.action == "task.assign_owner" and e.outcome == "assigned"]
        self.assertEqual([(e.previous_assignee_id, e.current_assignee_id) for e in events],
                         [(None, "U-A"), ("U-A", "U-B")])
        # An assignee is not a status: the status fields stay empty for an assignment.
        self.assertTrue(all(e.before_status is None and e.after_status is None for e in events))

    def test_delay_report_captures_the_reason_and_does_not_change_status(self) -> None:
        tid = f"{self.oid}#medical_test"
        before = self.service.get(HR_OPS, self.oid).task_by_id(tid).status
        task = self.service.report_delay(HR_OPS, self.oid, tid, "no clinic slot")
        self.assertEqual((task.status, task.delay_reason, task.delay_kind), (before, "no clinic slot", "delay"))
        self.assertIn(("medical_test", "delayed"),
                      {(f.template_key, f.code) for f in self.service.follow_up_flags(HR_OPS, self.oid, CLOCK)})
        blocker = self.service.report_delay(HR_OPS, self.oid, tid, "waiting on sponsor", blocker=True)
        self.assertEqual(blocker.delay_kind, "blocker")

    def test_delay_reports_need_a_reason_an_open_task_and_authority(self) -> None:
        tid = f"{self.oid}#medical_test"
        with self.assertRaises(InvalidStateError):
            self.service.report_delay(HR_OPS, self.oid, tid, " ")
        with self.assertRaises(UnauthorizedActionError):
            self.service.report_delay(EMPLOYEE, self.oid, tid, "not my task")
        with self.assertRaises(InvalidStateError):
            self.service.report_delay(HR_OPS, self.oid, f"{self.oid}#nope", "x")
        self.service.complete_task(HR_OPS, self.oid, tid, "EVID")
        with self.assertRaises(InvalidStateError):
            self.service.report_delay(HR_OPS, self.oid, tid, "too late")

    def test_priority_and_planned_start_are_populated_from_configuration(self) -> None:
        registry = ConfigRegistry()
        registry.register(ClientOnboardingConfig(
            client_id=CLIENT_ALPHA, approval_status="approved",
            templates=(
                TaskTemplate(key="a", category=TaskCategory.HR_RECORD, title="a", title_ar="a",
                             owner=Role.HR_OPS, offset_days=-5, priority="high", duration_days=2),
                TaskTemplate(key="b", category=TaskCategory.IT_PROVISIONING, title="b", title_ar="b",
                             owner=Role.IT_MANAGER, offset_days=-4, depends_on=("a",)),
            )))
        service = OnboardingService(registry)
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        a, b = record.task_by_key("a"), record.task_by_key("b")
        self.assertEqual((a.priority, b.priority), ("high", "normal"))
        self.assertLess(a.planned_start_date, a.due_date)  # 2 business days of work
        self.assertGreaterEqual(b.planned_start_date, a.due_date)  # cannot start before its prerequisite is due
        self.assertLessEqual(b.planned_start_date, b.due_date)
        with self.assertRaises(ValueError):
            TaskTemplate(key="x", category=TaskCategory.HR_RECORD, title="x", title_ar="x",
                         owner=Role.HR_OPS, offset_days=0, priority="urgent!")


class MonitoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = gated_service()
        self.oid = new_case(self.service, "EMP-007")
        approve(self.service, self.oid)

    def test_the_sweep_drafts_reminders_and_records_each_escalation_once(self) -> None:
        first = self.service.monitoring_sweep(HR_OPS, self.oid, date(2026, 11, 4))
        self.assertTrue(first.reminder_drafts)
        self.assertTrue(all(d.delivery_status == "draft_not_sent" for d in first.reminder_drafts))
        self.assertIn("nothing was sent", first.delivery)
        self.assertGreater(first.newly_recorded_escalations, 0)
        second = self.service.monitoring_sweep(HR_OPS, self.oid, date(2026, 11, 4))
        self.assertEqual((second.newly_recorded_escalations, second.newly_recorded_reminders), (0, 0))
        later = self.service.monitoring_sweep(HR_OPS, self.oid, date(2026, 11, 9))
        self.assertGreater(later.newly_recorded_escalations, 0)  # new level / new items on a later day

    def test_escalation_history_closes_when_work_completes(self) -> None:
        self.service.monitoring_sweep(HR_OPS, self.oid, date(2026, 11, 9))
        tid = f"{self.oid}#medical_test"
        before = [e for e in self.service.escalation_history(HR_OPS, self.oid) if e.subject_id == tid]
        self.assertTrue(before and all(e.status == "open" for e in before))
        self.service.complete_task(HR_OPS, self.oid, tid, "EVID")
        after = [e for e in self.service.escalation_history(HR_OPS, self.oid) if e.subject_id == tid]
        self.assertTrue(all(e.status == "resolved" for e in after))

    def test_escalation_routes_follow_the_configured_ladder_only(self) -> None:
        self.service.monitoring_sweep(HR_OPS, self.oid, date(2026, 11, 9))
        targets = {e.escalate_to for e in self.service.escalation_history(HR_OPS, self.oid)}
        self.assertTrue(targets <= {"hr_officer", "hr_lead"}, targets)  # no invented team-lead route

    def test_only_hr_can_run_the_sweep(self) -> None:
        with self.assertRaises(UnauthorizedActionError):
            self.service.monitoring_sweep(IT_MANAGER, self.oid, CLOCK)


class FinalReviewAndReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = gated_service()
        self.oid = new_case(self.service, "EMP-001")

    def complete_everything(self):
        approve(self.service, self.oid)
        roles = {Role.HR_OPS: HR_OPS, Role.IT_MANAGER: IT_MANAGER, Role.EMPLOYEE: EMPLOYEE,
                 Role.FACILITIES: Actor(actor_id="F", role=Role.FACILITIES, client_id=CLIENT_ALPHA),
                 Role.FINANCE: Actor(actor_id="N", role=Role.FINANCE, client_id=CLIENT_ALPHA)}
        while True:
            ready = self.service.ready_tasks(HR_OPS, self.oid)
            if not ready:
                return
            for t in ready:
                self.service.complete_task(roles[t.owner], self.oid, t.task_id,
                                           "EVID" if t.evidence_required else None)

    def test_readiness_is_not_granted_for_an_unapproved_plan(self) -> None:
        r = self.service.joining_readiness(HR_OPS, self.oid, CLOCK)
        self.assertEqual(r.status, "not_ready")
        self.assertTrue(any("not approved by HR" in reason for reason in r.reasons))
        self.assertEqual(r.total_tasks, 10)
        self.assertGreater(len(r.outstanding_hr_action_task_ids), 0)

    def test_a_final_submission_needs_an_approved_plan(self) -> None:
        with self.assertRaises(PlanNotApprovedError):
            self.service.submit_final(HR_OPS, self.oid, CLOCK)

    def test_readiness_needs_everything_done_and_the_final_approval(self) -> None:
        self.complete_everything()
        r = self.service.joining_readiness(HR_OPS, self.oid, CLOCK)
        self.assertEqual(r.status, "not_ready")
        self.assertEqual([x for x in r.reasons if "final" not in x], [])  # only the final approval is missing
        request = self.service.submit_final(HR_OPS, self.oid, CLOCK)
        self.assertEqual(self.service.joining_readiness(HR_OPS, self.oid, CLOCK).status, "not_ready")
        record = self.service.decide_final(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        self.assertEqual(record.final_status, FinalStatus.APPROVED)
        granted = self.service.joining_readiness(HR_OPS, self.oid, CLOCK)
        self.assertEqual((granted.status, granted.reasons), ("granted", []))
        self.assertEqual(record.status.value, "active")  # UC-02 does not close the case

    def test_final_review_authority_reason_and_duplicate_rules(self) -> None:
        self.complete_everything()
        request = self.service.submit_final(HR_OPS, self.oid, CLOCK)
        with self.assertRaises(InvalidStateError):
            self.service.submit_final(HR_OPS, self.oid, CLOCK)  # one under review at a time
        for who in (IT_MANAGER, EMPLOYEE, HR_OPS):
            with self.assertRaises(UnauthorizedActionError):
                self.service.decide_final(who, request.request_id, DecisionOutcome.APPROVED)
        with self.assertRaises(InvalidStateError):
            self.service.decide_final(HR_OPS_APPROVER, request.request_id, DecisionOutcome.REJECTED, "")
        self.service.decide_final(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        with self.assertRaises(InvalidStateError):
            self.service.decide_final(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)

    def test_wrong_gate_cannot_be_used_as_a_final_or_plan_decision(self) -> None:
        plan_request = self.service.submit_plan(HR_OPS, self.oid)
        with self.assertRaises(UnauthorizedActionError):
            self.service.decide_final(HR_OPS_APPROVER, plan_request.request_id, DecisionOutcome.APPROVED)
        self.service.decide_plan(HR_OPS_APPROVER, plan_request.request_id, DecisionOutcome.APPROVED)
        self.complete_everything_after_plan()
        final_request = self.service.submit_final(HR_OPS, self.oid, CLOCK)
        with self.assertRaises(UnauthorizedActionError):
            self.service.decide_plan(HR_OPS_APPROVER, final_request.request_id, DecisionOutcome.APPROVED)

    def complete_everything_after_plan(self):
        roles = {Role.HR_OPS: HR_OPS, Role.IT_MANAGER: IT_MANAGER, Role.EMPLOYEE: EMPLOYEE,
                 Role.FACILITIES: Actor(actor_id="F", role=Role.FACILITIES, client_id=CLIENT_ALPHA),
                 Role.FINANCE: Actor(actor_id="N", role=Role.FINANCE, client_id=CLIENT_ALPHA)}
        while self.service.ready_tasks(HR_OPS, self.oid):
            for t in self.service.ready_tasks(HR_OPS, self.oid):
                self.service.complete_task(roles[t.owner], self.oid, t.task_id, "EVID" if t.evidence_required else None)

    def test_a_rejected_final_submission_returns_items_and_keeps_history(self) -> None:
        approve(self.service, self.oid)
        self.service.complete_task(HR_OPS, self.oid, f"{self.oid}#hr_record_create")
        request = self.service.submit_final(HR_OPS, self.oid, CLOCK)
        with self.assertRaises(InvalidStateError):  # incomplete: cannot be approved
            self.service.decide_final(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        record = self.service.decide_final(HR_OPS_APPROVER, request.request_id, DecisionOutcome.REJECTED, "work remains")
        sub = record.final_submissions[0]
        self.assertEqual((sub.decision, sub.decision_reason, record.final_status),
                         ("rejected", "work remains", FinalStatus.REJECTED))
        self.assertGreater(len(sub.returned_task_ids), 0)
        self.service.submit_final(HR_OPS, self.oid, CLOCK)  # resubmission allowed after rejection
        self.assertEqual(len(self.service.get(HR_OPS, self.oid).final_submissions), 2)
        self.assertEqual(self.service.get(HR_OPS, self.oid).final_submissions[0].decision, "rejected")

    def test_readiness_lists_outstanding_requirements_by_category(self) -> None:
        approve(self.service, self.oid)
        r = self.service.joining_readiness(HR_OPS, self.oid, CLOCK)
        self.assertIn("it_provisioning", r.outstanding_by_category)
        self.assertEqual(r.complete + r.pending + r.blocked, r.total_tasks)


class BusinessScenarioProcessTests(unittest.TestCase):
    """The scenario script, A-G, in real separate processes on SQLite files."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="uc02-business-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def phase(self, phase: str):
        return subprocess.run(
            [sys.executable, "-I", str(ROOT / "demo" / "uc02_business_workflow.py"),
             "--dir", str(self.dir / "db"), "--phase", phase],
            capture_output=True, text=True, timeout=300, cwd=tempfile.gettempdir())

    def test_all_scenarios_pass_and_survive_a_restart(self) -> None:
        run = self.phase("run")
        self.assertEqual(run.returncode, 0, run.stdout[-3000:] + run.stderr[-2000:])
        self.assertIn("ALL SCENARIOS PASSED", run.stdout)
        self.assertNotIn("[FAIL]", run.stdout)
        for name in "ABCDEFG":
            self.assertIn(f"=== Scenario {name}:", run.stdout)
        self.assertGreaterEqual(run.stdout.count("[PASS]"), 40)
        verify = self.phase("verify")  # a new interpreter over the same files
        self.assertEqual(verify.returncode, 0, verify.stdout[-3000:] + verify.stderr[-2000:])
        self.assertIn("RESTART VERIFICATION PASSED", verify.stdout)
        self.assertNotIn("[FAIL]", verify.stdout)
        manifest = json.loads((self.dir / "db" / "manifest.json").read_text())
        self.assertEqual(sorted(manifest), list("ABCDEFG"))


if __name__ == "__main__":
    unittest.main()
