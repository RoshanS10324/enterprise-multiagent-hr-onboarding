"""UC-02 task completion / failure, driven from the real orchestrator graph.

What is REAL here
    * The shared orchestrator graph (``OnboardingOrchestratorAgent``), its case store, stage
      routing, retry bookkeeping and the real ``call_uc02_plan_tasks`` adapter.
    * The real UC-02 package behind that adapter (planner, tracker, status updates, audit),
      with a synthetic client configuration registered for the orchestrator's test client.
    * The real UC-03 checklist behind the document-collection stage (read-only).

What is NOT real
    * UC-01 (``FakeUc01Worker``), UC-04 (``GatedUc04Worker``) and UC-05 (``Uc05Double``) are
      synthetic doubles from ``HR-Onboarding-Common/tests/orchestration``; UC-03 validation is
      a double (``Uc03Double``). The notifier is the mock outbox. Nothing touches a database.
    * The orchestrator has NO call into UC-02's status-update API (no contract exists), so the
      "task execution / status update" step is driven directly on the UC-02 service instance
      the orchestrator built. That step is real UC-02 code, but it is not orchestrated.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (
    ROOT / "src",
    ROOT.parent / "HR-Onboarding-Common" / "src",
    ROOT.parent / "HR-Onboarding-Common" / "tests" / "orchestration",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from _workflow import CLIENT, HR, Flow, code, status
    from hr_onboarding_common.orchestration.state import Stage, StageStatus
    from hr_onboarding_common.platform.policy import PolicyViolation  # noqa: F401
    from hr_onboarding_common.orchestration.api import PrerequisiteNotMetError  # noqa: F401
except Exception as exc:  # pragma: no cover - shared package not importable
    raise unittest.SkipTest(f"orchestrator test harness unavailable: {exc}")

from onboarding_task_management.contracts import (
    Actor,
    ExternalOutcome,
    Role,
    SourceUseCase,
    StatusUpdate,
    TaskCategory,
    TaskStatus,
)
from onboarding_task_management.errors import (
    DataIsolationError,
    EvidenceRequiredError,
    InvalidStateError,
    PrerequisiteNotMetError as Uc02PrerequisiteNotMetError,
    UnauthorizedActionError,
)
from onboarding_task_management.sub_agent.adapter import UC02SubAgent
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    ConfigRegistry,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.sub_agent.status_updates import (
    REJECT_SOURCE_UNAUTHENTICATED,
    REJECT_TASK_SETTLED,
)

OCCURRED = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
APPROVAL_FACTS = dict(
    offer_accepted_at="2026-10-20T10:00:00Z",
    form_approved_at="2026-10-21T10:00:00Z",
    form_approved_by_role="hr_ops",
)
PRINCIPALS = {
    SourceUseCase.UC03_DOCUMENT_VALIDATION: frozenset({"svc-uc03"}),
    SourceUseCase.UC04_EQUIPMENT_ACCESS: frozenset({"svc-uc04"}),
}
HR_OPS = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id=CLIENT)
IT_MGR = Actor(actor_id="U-IT-1", role=Role.IT_MANAGER, client_id=CLIENT)
EMPLOYEE = Actor(actor_id="U-EMP-1", role=Role.EMPLOYEE, client_id=CLIENT)
UC03 = Actor(actor_id="svc-uc03", role=Role.SYSTEM, client_id=CLIENT)
UC04 = Actor(actor_id="svc-uc04", role=Role.SYSTEM, client_id=CLIENT)
OTHER_CLIENT_HR = Actor(actor_id="U-HR-B", role=Role.HR_OPS, client_id="CLIENT-TEST-B")


def _t(key, category, owner, offset, *deps, evidence=False):
    return TaskTemplate(
        key=key, category=category, title=key, title_ar=key, owner=owner,
        offset_days=offset, depends_on=tuple(deps), evidence_required=evidence,
    )


def _registry() -> ConfigRegistry:
    """A synthetic client config for the orchestrator's test client. Synthetic, not approved."""
    registry = ConfigRegistry()
    registry.register(
        ClientOnboardingConfig(
            client_id=CLIENT,
            approval_status="approved",  # SYNTHETIC test fixture, not a real approval
            templates=(
                _t("hr_record_create", TaskCategory.HR_RECORD, Role.HR_OPS, -10),
                _t("collect_documents", TaskCategory.DOCUMENT_COLLECTION, Role.HR_OPS, -8, "hr_record_create"),
                _t("visa_application", TaskCategory.VISA, Role.HR_OPS, -4, "collect_documents"),
                _t("it_laptop", TaskCategory.IT_PROVISIONING, Role.IT_MANAGER, -5, "hr_record_create",
                   evidence=True),
                _t("it_accounts", TaskCategory.ACCESS_MANAGEMENT, Role.IT_MANAGER, -3, "it_laptop"),
                _t("access_platform", TaskCategory.ACCESS_MANAGEMENT, Role.IT_MANAGER, -1, "it_accounts"),
            ),
        )
    )
    return registry


def _new_flow(**kw) -> tuple[Flow, OnboardingService]:
    flow = Flow(real_uc02=True, **kw)
    service = OnboardingService(_registry(), source_principals=PRINCIPALS)
    flow.deps._uc02_sub_agent = UC02SubAgent(service=service)
    return flow, service


def _plan(flow: Flow, **over):
    """UC-01 approved input -> orchestrator -> real plan_tasks adapter -> UC-02."""
    return flow.supply(flow.start(**{**APPROVAL_FACTS, "joining_date": "2026-11-30", **over}))


class OrchestratedPlanningTests(unittest.TestCase):
    """REAL orchestrator graph + REAL UC-02 adapter and package."""

    def test_uc01_approved_input_produces_a_uc02_task_plan(self) -> None:
        flow, service = _new_flow()
        state = _plan(flow)
        self.assertEqual(status(state, Stage.PLAN_TASKS), StageStatus.COMPLETE.value, state.get("error_detail"))
        plan = state["task_plan"]
        self.assertEqual(len(plan["tasks"]), 6)
        self.assertEqual(plan["case_id"], state["case_id"])
        self.assertEqual(plan["correlation_id"], state["correlation_id"])
        # Only the root is ready; everything else waits on a prerequisite.
        self.assertEqual(len(plan["ready"]), 1)

    def test_the_orchestrators_ids_reach_uc02_records_and_audit(self) -> None:
        flow, service = _new_flow()
        state = _plan(flow)
        record = service.get(HR_OPS, state["task_plan"]["onboarding_id"])
        self.assertEqual(record.case_id, state["case_id"])
        self.assertEqual(record.correlation_id, state["correlation_id"])
        events = [
            e for e in service.audit.events_for(HR_OPS) if e.onboarding_id == record.onboarding_id
        ]
        self.assertTrue(events)
        self.assertTrue(all(e.correlation_id == state["correlation_id"] for e in events))

    def test_plan_tasks_is_blocked_not_assumed_when_approval_facts_are_missing(self) -> None:
        flow, service = _new_flow()
        state = flow.supply(flow.start(joining_date="2026-11-30"))  # no approval facts supplied
        self.assertEqual(status(state, Stage.PLAN_TASKS), StageStatus.BLOCKED.value)
        self.assertEqual(state["error_code"], "MISSING_DATA")
        self.assertIsNone(state["task_plan"])
        self.assertEqual(service._client_records(CLIENT), {})  # nothing was created
        # An unrelated branch is unaffected by UC-02 blocking.
        self.assertEqual(status(state, Stage.EQUIPMENT_ACCESS), StageStatus.COMPLETE.value)

    def test_a_blocked_uc02_stage_is_not_retryable(self) -> None:
        flow, _ = _new_flow()
        state = flow.supply(flow.start(joining_date="2026-11-30"))
        with self.assertRaises(PrerequisiteNotMetError):
            flow.agent.retry_stage(state["case_id"], Stage.PLAN_TASKS.value, actor=HR)

    def test_orchestrator_restart_resumes_without_replanning_but_uc02_records_are_not_durable(self) -> None:
        flow, service = _new_flow()
        state = _plan(flow)
        self.assertEqual(state["stage_attempts"][Stage.PLAN_TASKS.value], 1)
        before = state["task_plan"]

        # "Restart": new orchestrator over the same case-state store, brand-new in-memory UC-02.
        flow2, service2 = _new_flow(case_states=flow.deps.case_states)
        resumed = flow2.agent.advance_case(None, case_id=state["case_id"], actor=HR)

        self.assertEqual(resumed["stage_attempts"][Stage.PLAN_TASKS.value], 1)  # not re-run
        self.assertEqual(resumed["task_plan"], before)  # orchestrator projection survived
        # KNOWN GAP (reported, not hidden): UC-02's own records were in memory and are gone.
        self.assertEqual(service2._client_records(CLIENT), {})


class OrchestratedTaskExecutionTests(unittest.TestCase):
    """Execution after orchestrated planning. UC-02 code is real; the calls are not orchestrated."""

    def setUp(self) -> None:
        self.flow, self.service = _new_flow()
        self.state = _plan(self.flow)
        self.oid = self.state["task_plan"]["onboarding_id"]

    def tid(self, key):
        return f"{self.oid}#{key}"

    def task(self, key):
        return self.service.get(HR_OPS, self.oid).task_by_key(key)

    def update(self, key, source=SourceUseCase.UC03_DOCUMENT_VALIDATION,
               outcome=ExternalOutcome.VALID, **over):
        payload = dict(
            update_id=f"upd-{key}-{outcome.value}", source=source, client_id=CLIENT,
            onboarding_id=self.oid, task_id=self.tid(key), outcome=outcome, occurred_at=OCCURRED,
        )
        payload.update(over)
        return StatusUpdate(**payload)

    def summary(self):
        return self.service.status(HR_OPS, self.oid, datetime(2026, 11, 20).date())

    # 1. successful completion ------------------------------------------------
    def test_successful_completion_unblocks_dependents_and_updates_readiness(self) -> None:
        before = self.summary()
        # One root task is actionable: it is either still ready or the agent has already started it (the
        # adapter now evaluates as of today, so the agent may start it). Nothing is complete yet.
        self.assertEqual((before.complete, before.ready + before.in_progress), (0, 1))
        done = self.service.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        self.assertEqual(done.status, TaskStatus.COMPLETE)
        self.assertIsNotNone(done.completed_at)
        after = self.summary()
        self.assertEqual(after.complete, 1)
        self.assertEqual(after.ready, 2)  # collect_documents and it_laptop
        event = [e for e in self.service.audit.events_for(HR_OPS) if e.action == "task.complete"][-1]
        self.assertIn(event.before_status, ("ready", "in_progress"))  # the agent may have started it
        self.assertEqual(event.after_status, "complete")
        self.assertEqual(event.task_id, self.tid("hr_record_create"))

    # 2. evidence ---------------------------------------------------------------
    def test_completion_without_required_evidence_is_refused_then_accepted_with_it(self) -> None:
        self.service.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        with self.assertRaises(EvidenceRequiredError):
            self.service.complete_task(IT_MGR, self.oid, self.tid("it_laptop"))
        self.assertNotEqual(self.task("it_laptop").status, TaskStatus.COMPLETE)
        self.assertIsNone(self.task("it_laptop").completed_at)
        self.assertTrue(any(e.action == "task.complete" and e.outcome == "denied"
                            for e in self.service.audit.events_for(HR_OPS)))
        with self.assertRaises(EvidenceRequiredError):
            self.service.complete_task(IT_MGR, self.oid, self.tid("it_laptop"), "   ")
        ok = self.service.complete_task(IT_MGR, self.oid, self.tid("it_laptop"), "TCK-1001")
        self.assertEqual(ok.evidence_ref, "TCK-1001")

    # 3. failure ----------------------------------------------------------------
    def test_a_reported_failure_is_captured_and_is_not_confused_with_blocked(self) -> None:
        self.service.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        result = self.service.apply_status_update(
            UC03, self.oid,
            self.update("collect_documents", outcome=ExternalOutcome.INVALID,
                        blocking_reason="document unreadable"),
        )
        self.assertTrue(result.accepted)
        task = self.task("collect_documents")
        # No FAILED status exists in the contract: the task stays READY (prerequisites are met)...
        self.assertEqual(task.status, TaskStatus.READY)
        self.assertNotEqual(task.status, TaskStatus.BLOCKED)
        # ...and the failure is carried by the external outcome and its reason.
        self.assertEqual(task.external_outcome, ExternalOutcome.INVALID)
        self.assertEqual(task.external_blocking_reason, "document unreadable")
        self.assertTrue(task.is_externally_unresolved)
        # A follow-up flag is raised and the dependent stays blocked.
        flags = {f.code for f in self.service.follow_up_flags(HR_OPS, self.oid, OCCURRED.date())
                 if f.template_key == "collect_documents"}
        self.assertIn("external_unresolved", flags)
        self.assertEqual(self.task("visa_application").status, TaskStatus.BLOCKED)
        event = [e for e in self.service.audit.events_for(HR_OPS) if e.action == "task.status_update"][-1]
        self.assertEqual(event.detail["reported_outcome"], "invalid")
        self.assertEqual((event.before_status, event.after_status), ("ready", "ready"))

    def test_a_failure_does_not_unblock_dependents_but_a_later_success_does(self) -> None:
        self.service.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        self.service.apply_status_update(
            UC03, self.oid, self.update("collect_documents", outcome=ExternalOutcome.INVALID,
                                        blocking_reason="expired"))
        self.assertEqual(self.task("visa_application").status, TaskStatus.BLOCKED)
        ok = self.service.apply_status_update(UC03, self.oid, self.update("collect_documents"))
        self.assertTrue(ok.accepted)
        self.assertEqual(self.task("collect_documents").status, TaskStatus.COMPLETE)
        self.assertEqual(self.task("collect_documents").external_blocking_reason, "")  # cleared
        self.assertEqual(self.task("visa_application").status, TaskStatus.READY)
        self.assertEqual(ok.unblocked_task_ids, [self.tid("visa_application")])

    def test_a_failure_without_a_reason_is_rejected(self) -> None:
        self.service.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        result = self.service.apply_status_update(
            UC03, self.oid, self.update("collect_documents", outcome=ExternalOutcome.INVALID))
        self.assertFalse(result.accepted)
        self.assertIsNone(self.task("collect_documents").external_outcome)

    # 4 / 8. dependencies -------------------------------------------------------
    def test_dependent_chain_cannot_be_started_or_completed_out_of_order(self) -> None:
        for key in ("it_accounts", "access_platform", "visa_application"):
            with self.assertRaises(Uc02PrerequisiteNotMetError):
                self.service.start_task(HR_OPS, self.oid, self.tid(key))
            with self.assertRaises(Uc02PrerequisiteNotMetError):
                self.service.complete_task(HR_OPS, self.oid, self.tid(key), "ref")
            self.assertEqual(self.task(key).status, TaskStatus.BLOCKED)

    def test_multiple_dependent_tasks_unblock_in_order_and_independent_branches_run_in_parallel(self) -> None:
        self.service.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        batches = self.service.parallel_batches(HR_OPS, self.oid)
        flat = [k for batch in batches for k in batch]
        self.assertLess(flat.index(self.tid("it_laptop")), flat.index(self.tid("it_accounts")))
        self.assertLess(flat.index(self.tid("it_accounts")), flat.index(self.tid("access_platform")))
        ready = {t.template_key for t in self.service.ready_tasks(HR_OPS, self.oid)}
        self.assertEqual(ready, {"collect_documents", "it_laptop"})  # two branches at once
        self.service.complete_task(IT_MGR, self.oid, self.tid("it_laptop"), "TCK-1")
        self.service.complete_task(IT_MGR, self.oid, self.tid("it_accounts"))
        self.service.complete_task(IT_MGR, self.oid, self.tid("access_platform"))
        self.assertEqual(self.task("collect_documents").status, TaskStatus.READY)  # untouched branch

    # 5. invalid transitions ----------------------------------------------------
    def test_invalid_transitions_are_refused_without_changing_state(self) -> None:
        root = self.tid("hr_record_create")
        self.service.complete_task(HR_OPS, self.oid, root)
        completed_at = self.task("hr_record_create").completed_at
        with self.assertRaises(InvalidStateError):  # restart a finished task
            self.service.start_task(HR_OPS, self.oid, root)
        with self.assertRaises(InvalidStateError):  # complete it twice
            self.service.complete_task(HR_OPS, self.oid, root)
        with self.assertRaises(InvalidStateError):  # unknown task
            self.service.complete_task(HR_OPS, self.oid, self.tid("no_such_task"))
        self.assertEqual(self.task("hr_record_create").completed_at, completed_at)
        completes = [e for e in self.service.audit.events_for(HR_OPS)
                     if e.action == "task.complete" and e.outcome == "complete" and e.task_id == root]
        self.assertEqual(len(completes), 1)  # no second "complete" event

    def test_a_late_report_cannot_reopen_a_completed_task(self) -> None:
        self.service.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        self.service.complete_task(HR_OPS, self.oid, self.tid("collect_documents"))
        result = self.service.apply_status_update(
            UC03, self.oid, self.update("collect_documents", outcome=ExternalOutcome.INVALID,
                                        blocking_reason="late"))
        self.assertEqual(result.rejection_code, REJECT_TASK_SETTLED)
        self.assertEqual(self.task("collect_documents").status, TaskStatus.COMPLETE)

    # 6. duplicates -------------------------------------------------------------
    def test_duplicate_status_update_is_applied_and_audited_once(self) -> None:
        self.service.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        update = self.update("collect_documents", outcome=ExternalOutcome.INVALID, blocking_reason="blurry")
        first = self.service.apply_status_update(UC03, self.oid, update)
        events = len(self.service.audit)
        second = self.service.apply_status_update(UC03, self.oid, update)
        self.assertTrue(first.accepted and second.accepted)
        self.assertTrue(second.duplicate_replay)
        self.assertEqual(len(self.service.audit), events)  # no duplicate audit event
        flags = [f for f in self.service.follow_up_flags(HR_OPS, self.oid, OCCURRED.date())
                 if f.template_key == "collect_documents" and f.code == "external_unresolved"]
        self.assertEqual(len(flags), 1)

    # 7. authorization ----------------------------------------------------------
    def test_unauthorized_actors_cannot_complete_or_report(self) -> None:
        with self.assertRaises(UnauthorizedActionError):
            self.service.complete_task(EMPLOYEE, self.oid, self.tid("hr_record_create"))
        with self.assertRaises(UnauthorizedActionError):  # IT may not act on an HR task
            self.service.complete_task(IT_MGR, self.oid, self.tid("hr_record_create"))
        with self.assertRaises(DataIsolationError):
            self.service.complete_task(OTHER_CLIENT_HR, self.oid, self.tid("hr_record_create"))
        self.service.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        spoof = self.service.apply_status_update(HR_OPS, self.oid, self.update("collect_documents"))
        self.assertEqual(spoof.rejection_code, REJECT_SOURCE_UNAUTHENTICATED)
        wrong_source = self.service.apply_status_update(
            UC04, self.oid, self.update("collect_documents", source=SourceUseCase.UC03_DOCUMENT_VALIDATION))
        self.assertEqual(wrong_source.rejection_code, REJECT_SOURCE_UNAUTHENTICATED)
        self.assertEqual(self.task("collect_documents").status, TaskStatus.READY)
        self.assertEqual(self.task("hr_record_create").status, TaskStatus.COMPLETE)

    # 10. readiness after completion and failure ---------------------------------
    def test_readiness_and_risk_after_completion_and_failure(self) -> None:
        self.service.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        self.service.apply_status_update(
            UC03, self.oid, self.update("collect_documents", outcome=ExternalOutcome.REQUIRES_REVIEW,
                                        blocking_reason="needs HR eyes"))
        self.service.complete_task(IT_MGR, self.oid, self.tid("it_laptop"), "TCK-2")
        summary = self.summary()
        self.assertEqual(summary.total_tasks, 6)
        # done: hr_record_create, it_laptop. ready: collect_documents (failed report keeps it ready) and
        # it_accounts (unblocked by it_laptop). blocked: visa_application, access_platform.
        self.assertEqual((summary.complete, summary.ready, summary.blocked), (2, 2, 2))
        self.assertEqual(summary.complete + summary.ready + summary.in_progress + summary.blocked
                         + summary.cancelled, summary.total_tasks)
        outstanding = {a.template_key for a in
                       self.service.outstanding_activities(HR_OPS, self.oid, OCCURRED.date())}
        self.assertIn("collect_documents", outstanding)
        self.assertNotIn("hr_record_create", outstanding)
        risk = self.service.joining_risk(HR_OPS, self.oid, OCCURRED.date())
        self.assertEqual(risk.onboarding_id, self.oid)

    def test_overdue_failed_task_is_escalated_and_flags_joining_risk(self) -> None:
        self.service.complete_task(HR_OPS, self.oid, self.tid("hr_record_create"))
        self.service.apply_status_update(
            UC03, self.oid, self.update("collect_documents", outcome=ExternalOutcome.DELAYED,
                                        blocking_reason="courier delay"))
        late = datetime(2026, 12, 10).date()  # after the 30 Nov joining date
        self.assertTrue(self.service.joining_risk(HR_OPS, self.oid, late).at_risk)
        self.assertIn(self.tid("collect_documents"),
                      self.service.status(HR_OPS, self.oid, late).overdue_task_ids)
        self.assertTrue(self.service.escalations(HR_OPS, self.oid, late))


if __name__ == "__main__":
    unittest.main()


class OrchestratorDurabilityTests(unittest.TestCase):
    """Shows the orchestrator CAN drive a durable UC-02 once its sub-agent slot is given one.

    The shared adapter itself is unchanged: ``call_uc02_plan_tasks`` still builds an in-memory
    ``UC02SubAgent()`` when ``deps._uc02_sub_agent`` is empty. Here the slot is filled from
    outside with a SQLite-backed service (the smallest compatible change, proposed in the
    report); nothing in the shared package is modified. Real orchestrator graph, real UC-02
    package, real SQLite file; UC-01/03/04/05 are the harness's synthetic doubles.
    """

    def setUp(self) -> None:
        import shutil
        import tempfile

        self.dir = Path(tempfile.mkdtemp(prefix="uc02-orch-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.env = {"UC02_STORE_MODE": "sqlite", "UC02_SQLITE_PATH": str(self.dir / "uc02.sqlite3")}

    def durable_flow(self, **kw):
        from onboarding_task_management.sub_agent.runtime import build_service

        flow = Flow(real_uc02=True, **kw)
        service = build_service(self.env, registry=_registry(), source_principals=PRINCIPALS)
        self.addCleanup(service.store.client.close)
        flow.deps._uc02_sub_agent = UC02SubAgent(service=service)
        return flow, service

    def test_the_orchestrated_plan_survives_an_orchestrator_and_uc02_restart(self) -> None:
        flow, service = self.durable_flow()
        state = _plan(flow)
        self.assertEqual(status(state, Stage.PLAN_TASKS), StageStatus.COMPLETE.value, state.get("error_detail"))
        oid = state["task_plan"]["onboarding_id"]
        service.store.client.close()

        flow2, service2 = self.durable_flow(case_states=flow.deps.case_states)  # "restart"
        resumed = flow2.agent.advance_case(None, case_id=state["case_id"], actor=HR)
        self.assertEqual(resumed["stage_attempts"][Stage.PLAN_TASKS.value], 1)  # not re-planned
        self.assertEqual(service2.hydrate(CLIENT), 1)  # UC-02's own record survived, unlike before
        record = service2.get(HR_OPS, oid)
        self.assertEqual((record.case_id, record.correlation_id), (state["case_id"], state["correlation_id"]))
        self.assertEqual(len(record.tasks), 6)

    def test_the_gated_plan_waits_for_hr_before_teams_can_work(self) -> None:
        flow, service = self.durable_flow()
        state = _plan(flow)
        oid = state["task_plan"]["onboarding_id"]
        from onboarding_task_management.errors import PlanNotApprovedError

        with self.assertRaises(PlanNotApprovedError):
            service.complete_task(HR_OPS, oid, f"{oid}#hr_record_create")
        # The agent run inside the orchestrated stage must not have progressed anything either.
        self.assertTrue(all(t.status is not TaskStatus.IN_PROGRESS for t in service.get(HR_OPS, oid).tasks))
