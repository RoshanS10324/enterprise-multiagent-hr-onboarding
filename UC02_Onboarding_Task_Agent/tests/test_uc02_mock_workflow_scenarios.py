"""Deterministic UC-02 mock workflow scenarios.

What this module is for
-----------------------

The existing suite tests each rule in isolation against CLIENT-ALPHA, whose
configuration grew organically and whose dependency graph is wide but shallow.
What it does not provide is a **purpose-built scenario fixture** with an
unambiguous dependency shape, so that a transition can be asserted precisely:
"completing exactly this prerequisite unlocked exactly those tasks, and
nothing else moved".

That is what CLIENT-MOCK below is for. It is a test fixture, so it lives with
the tests. Nothing here re-implements a rule: every plan, transition,
reminder, escalation and recovery is produced by the shipped
``OnboardingService``, ``planner``, ``tracker``, ``reminders``,
``MockOutlookConnector`` and ``OnboardingStore``.

The dependency graph
--------------------

::

    mock_hr_record            (-10, hr_ops)      <- the ONLY initially READY task
      |- mock_documents       ( -8, hr_ops)      evidence
      |    |- mock_visa       ( -6, pro)         evidence
      |    \\- mock_payroll    ( -5, finance)
      |         \\- mock_bank_letter (-4, hr_officer)  evidence
      |- mock_laptop          ( -7, it_manager)
      |    \\- mock_accounts   ( -3, it_manager)
      \\- mock_welcome_pack    ( -6, facilities)  independent leaf

Four dependency levels; three independent branches at level 1; six distinct
owners, so reminder grouping per owner is observable.

Determinism
-----------

Every date is explicit and was hand-counted on a Monday-to-Friday calendar
from the joining date 2026-11-16, then confirmed against
``calendar_uae.add_business_days``. No clock is read, no network is touched,
no database is used: storage is ``InMemoryTableClient`` and delivery is
``MockOutlookConnector``.

A note on "failed"
------------------

``TaskStatus`` has no ``FAILED`` member -- see
:meth:`SupportedStateTests.test_there_is_no_failed_task_status`. The failure
channels UC-02 actually supports are tested instead: a refused tool call, a
refused delivery, a transient delivery failure followed by a successful
retry, and a persistence write that is rolled back.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from onboarding_task_management.contracts import (
    Actor,
    DecisionOutcome,
    EscalationLevel,
    MilestoneStatus,
    OnboardingStatus,
    Role,
    TaskCategory,
    TaskStatus,
)
from onboarding_task_management.errors import (
    ApprovalRequiredError,
    DataIsolationError,
    EvidenceRequiredError,
    InvalidStateError,
    PrerequisiteNotMetError,
    UnauthorizedActionError,
)
from onboarding_task_management.persistence import (
    InMemoryTableClient,
    OnboardingStore,
)
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    ConfigRegistry,
    MilestoneTemplate,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.connectors.outlook.base import (
    DeliveryStatus,
)
from onboarding_task_management.sub_agent.fakes import (
    make_mock_outlook,
    message_from_draft,
)
from onboarding_task_management.sub_agent.service import (
    OnboardingService,
    OnboardingTrigger,
)
from onboarding_task_management.synthetic import accepted_offer, approved_form

# ---------------------------------------------------------------------------
# Fixture: clients, dates, actors
# ---------------------------------------------------------------------------

CLIENT_MOCK = "CLIENT-MOCK"
CLIENT_MOCK_B = "CLIENT-MOCK-B"

JOINING = date(2026, 11, 16)          # Monday

#: Hand-counted business days back from Monday 2026-11-16. No UAE public
#: holiday falls between 2026-10-26 and 2026-11-16, so only weekends apply.
DUE = {
    "mock_hr_record": date(2026, 11, 2),     # -10
    "mock_documents": date(2026, 11, 4),     # -8
    "mock_laptop": date(2026, 11, 5),        # -7
    "mock_visa": date(2026, 11, 6),          # -6
    "mock_welcome_pack": date(2026, 11, 16),  # 0 (day one)
    "mock_payroll": date(2026, 11, 9),       # -5
    "mock_bank_letter": date(2026, 11, 10),  # -4
    "mock_accounts": date(2026, 11, 11),     # -3
    "mock_access_grant": date(2026, 11, 13),  # -1
}

#: Nothing is due or overdue yet.
AS_OF_PLANNING = date(2026, 10, 26)
#: Exactly REMINDER_LEAD_DAYS (3) business days before mock_hr_record is due.
AS_OF_DUE_SOON = date(2026, 10, 28)
#: mock_hr_record is 1 business day overdue -> first responder.
AS_OF_OVERDUE_EARLY = date(2026, 11, 3)
#: mock_hr_record is 3 business days overdue -> operations lead.
AS_OF_OVERDUE_LATE = date(2026, 11, 5)
#: Exactly JOINING_RISK_DAYS_BEFORE (2) business days from joining.
AS_OF_NEAR_JOINING = date(2026, 11, 12)

HR_OPS = Actor(actor_id="U-MOCK-HR-1", role=Role.HR_OPS, client_id=CLIENT_MOCK)
HR_OPS_APPROVER = Actor(actor_id="U-MOCK-HR-2", role=Role.HR_OPS,
                        client_id=CLIENT_MOCK)
IT_MANAGER = Actor(actor_id="U-MOCK-IT-1", role=Role.IT_MANAGER,
                   client_id=CLIENT_MOCK)
FACILITIES = Actor(actor_id="U-MOCK-FAC-1", role=Role.FACILITIES,
                   client_id=CLIENT_MOCK)
FINANCE = Actor(actor_id="U-MOCK-FIN-1", role=Role.FINANCE,
                client_id=CLIENT_MOCK)
HR_OFFICER = Actor(actor_id="U-MOCK-OFF-1", role=Role.HR_OFFICER,
                   client_id=CLIENT_MOCK)
PRO = Actor(actor_id="U-MOCK-PRO-1", role=Role.PRO, client_id=CLIENT_MOCK)
HR_OPS_B = Actor(actor_id="U-MOCKB-HR-1", role=Role.HR_OPS,
                 client_id=CLIENT_MOCK_B)


MOCK_CONFIG = ClientOnboardingConfig(
    approval_status="approved",  # test fixture
    client_id=CLIENT_MOCK,
    templates=(
        TaskTemplate(
            key="mock_hr_record", category=TaskCategory.HR_RECORD,
            title="Create employee record", title_ar="إنشاء سجل الموظف",
            owner=Role.HR_OPS, offset_days=-10,
        ),
        TaskTemplate(
            key="mock_documents", category=TaskCategory.DOCUMENT_COLLECTION,
            title="Collect joining documents", title_ar="جمع مستندات الالتحاق",
            owner=Role.HR_OPS, offset_days=-8,
            depends_on=("mock_hr_record",), evidence_required=True,
        ),
        TaskTemplate(
            key="mock_laptop", category=TaskCategory.IT_PROVISIONING,
            title="Issue laptop", title_ar="تسليم الحاسوب المحمول",
            owner=Role.IT_MANAGER, offset_days=-7,
            depends_on=("mock_hr_record",),
        ),
        # Due on day 1 and NOT a day-1-critical category, so it is the
        # control for the proximity escalation rule: non-overdue and inside
        # the joining window, yet it must not escalate.
        TaskTemplate(
            key="mock_welcome_pack", category=TaskCategory.WORKPLACE,
            title="Prepare welcome pack", title_ar="تجهيز حزمة الترحيب",
            owner=Role.FACILITIES, offset_days=0,
            depends_on=("mock_hr_record",),
        ),
        TaskTemplate(
            key="mock_visa", category=TaskCategory.VISA,
            title="Submit visa application", title_ar="تقديم طلب التأشيرة",
            owner=Role.PRO, offset_days=-6,
            depends_on=("mock_documents",), evidence_required=True,
        ),
        TaskTemplate(
            key="mock_payroll", category=TaskCategory.PAYROLL,
            title="Set up payroll", title_ar="إعداد الرواتب",
            owner=Role.FINANCE, offset_days=-5,
            depends_on=("mock_documents",),
        ),
        TaskTemplate(
            key="mock_bank_letter", category=TaskCategory.BANKING_LETTER,
            title="Issue salary transfer letter",
            title_ar="إصدار خطاب تحويل الراتب",
            owner=Role.HR_OFFICER, offset_days=-4,
            depends_on=("mock_payroll",), evidence_required=True,
        ),
        TaskTemplate(
            key="mock_accounts", category=TaskCategory.IT_PROVISIONING,
            title="Create system accounts", title_ar="إنشاء حسابات الأنظمة",
            owner=Role.IT_MANAGER, offset_days=-3,
            depends_on=("mock_laptop",),
        ),
        # Day-1 critical and due inside the joining-risk window, so the
        # "critical work still open close to day 1" escalation trigger can be
        # observed on its own, without the task also being overdue.
        TaskTemplate(
            key="mock_access_grant", category=TaskCategory.ACCESS_MANAGEMENT,
            title="Grant system access", title_ar="منح صلاحيات الأنظمة",
            owner=Role.IT_MANAGER, offset_days=-1,
            depends_on=("mock_accounts",),
        ),
    ),
    milestones=(
        MilestoneTemplate(
            key="mock_day_one_ready", title="Day one ready",
            title_ar="جاهز لليوم الأول", offset_days=-1,
            requires=("mock_documents", "mock_laptop", "mock_accounts"),
        ),
    ),
)

#: A second client, deliberately sharing one template KEY with CLIENT-MOCK but
#: configuring a different owner and lead time, so a leak would be visible.
MOCK_B_CONFIG = ClientOnboardingConfig(
    approval_status="approved",  # test fixture
    client_id=CLIENT_MOCK_B,
    templates=(
        TaskTemplate(
            key="mock_hr_record", category=TaskCategory.HR_RECORD,
            title="Create employee record", title_ar="إنشاء سجل الموظف",
            owner=Role.HR_OFFICER, offset_days=-4,
        ),
    ),
)

ALL_KEYS = frozenset(t.key for t in MOCK_CONFIG.templates)
EVIDENCE_KEYS = frozenset({"mock_documents", "mock_visa", "mock_bank_letter"})
OWNER = {t.key: t.owner for t in MOCK_CONFIG.templates}

#: Hand-derived dependency layers. Everything in a layer may run at once.
EXPECTED_BATCH_KEYS = (
    {"mock_hr_record"},
    {"mock_documents", "mock_laptop", "mock_welcome_pack"},
    {"mock_visa", "mock_payroll", "mock_accounts"},
    {"mock_bank_letter", "mock_access_grant"},
)


def mock_registry() -> ConfigRegistry:
    registry = ConfigRegistry()
    registry.register(MOCK_CONFIG)
    registry.register(MOCK_B_CONFIG)
    return registry


def mock_trigger(employee_id: str, *, client_id: str = CLIENT_MOCK,
                 joining_date: date = JOINING) -> OnboardingTrigger:
    """A valid, approved trigger. Synthetic identifiers only."""
    return OnboardingTrigger(
        client_id=client_id,
        employee_id=employee_id,
        joining_date=joining_date,
        role_id="mock_engineer",
        department="technology",
        manager_id=f"MGR-{client_id}",
        location="Dubai",
        project="apollo",
        grade="L3",
        offer=accepted_offer(employee_id),
        form=approved_form(employee_id),
    )


class MockScenarioTestCase(unittest.TestCase):
    """MOCK-001 on CLIENT-MOCK, freshly planned for each test."""

    employee_id = "MOCK-001"

    def setUp(self) -> None:
        self.service = OnboardingService(mock_registry())
        record = self.service.create_onboarding(
            HR_OPS, mock_trigger(self.employee_id)
        )
        self.onboarding_id = record.onboarding_id

    # -- helpers -----------------------------------------------------------

    @property
    def record(self):
        """A fresh snapshot. The service returns deep copies, so re-read."""
        return self.service.get(HR_OPS, self.onboarding_id)

    def task_id(self, key: str) -> str:
        return f"{self.onboarding_id}#{key}"

    def status_of(self, key: str) -> TaskStatus:
        task = self.record.task_by_key(key)
        assert task is not None, key
        return task.status

    def statuses(self) -> dict[str, TaskStatus]:
        return {t.template_key: t.status for t in self.record.tasks}

    def ready_keys(self) -> set[str]:
        return {
            t.template_key
            for t in self.service.ready_tasks(HR_OPS, self.onboarding_id)
        }

    def blocked_keys(self) -> set[str]:
        return {
            t.template_key
            for t in self.service.blocked_tasks(HR_OPS, self.onboarding_id)
        }

    def finish(self, actor: Actor, key: str) -> None:
        """Complete a task, supplying evidence only where it is required."""
        evidence = f"DOC-{key.upper()}" if key in EVIDENCE_KEYS else None
        self.service.complete_task(
            actor, self.onboarding_id, self.task_id(key), evidence
        )


# ---------------------------------------------------------------------------
# Scenario 1 - the initial plan
# ---------------------------------------------------------------------------


class InitialPlanTests(MockScenarioTestCase):
    def test_exactly_one_task_is_initially_ready(self) -> None:
        self.assertEqual(self.ready_keys(), {"mock_hr_record"})

    def test_every_other_task_is_blocked(self) -> None:
        self.assertEqual(self.blocked_keys(), set(ALL_KEYS) - {"mock_hr_record"})

    def test_the_plan_contains_exactly_the_configured_tasks(self) -> None:
        self.assertEqual({t.template_key for t in self.record.tasks},
                         set(ALL_KEYS))

    def test_due_dates_match_the_hand_counted_calendar(self) -> None:
        for task in self.record.tasks:
            with self.subTest(key=task.template_key):
                self.assertEqual(task.due_date, DUE[task.template_key])

    def test_owners_match_the_configuration(self) -> None:
        for task in self.record.tasks:
            with self.subTest(key=task.template_key):
                self.assertIs(task.owner, OWNER[task.template_key])

    def test_dependency_layers_match_the_hand_derived_graph(self) -> None:
        batches = self.service.parallel_batches(HR_OPS, self.onboarding_id)
        as_keys = [{tid.split("#", 1)[1] for tid in batch} for batch in batches]
        self.assertEqual(as_keys, list(EXPECTED_BATCH_KEYS))

    def test_nothing_is_overdue_and_the_case_is_on_track(self) -> None:
        summary = self.service.status(HR_OPS, self.onboarding_id,
                                      AS_OF_PLANNING)
        self.assertEqual(summary.overdue_task_ids, [])
        self.assertTrue(summary.is_on_track)
        self.assertEqual(summary.total_tasks, len(ALL_KEYS))
        self.assertEqual(summary.ready, 1)
        self.assertEqual(summary.blocked, len(ALL_KEYS) - 1)


# ---------------------------------------------------------------------------
# Scenario 2 - dependency recalculation
# ---------------------------------------------------------------------------


class DependencyUnlockTests(MockScenarioTestCase):
    def test_completing_the_root_unlocks_exactly_its_three_dependants(self) -> None:
        before = self.statuses()
        self.finish(HR_OPS, "mock_hr_record")
        after = self.statuses()

        self.assertIs(after["mock_hr_record"], TaskStatus.COMPLETE)
        self.assertEqual(
            self.ready_keys(),
            {"mock_documents", "mock_laptop", "mock_welcome_pack"},
        )
        # Nothing deeper in the graph moved.
        for key in ("mock_visa", "mock_payroll", "mock_bank_letter",
                    "mock_accounts"):
            with self.subTest(key=key):
                self.assertIs(after[key], TaskStatus.BLOCKED)
                self.assertIs(before[key], TaskStatus.BLOCKED)

    def test_completing_one_branch_does_not_unlock_another(self) -> None:
        self.finish(HR_OPS, "mock_hr_record")
        self.finish(IT_MANAGER, "mock_laptop")

        self.assertIs(self.status_of("mock_accounts"), TaskStatus.READY)
        # The documents branch is untouched.
        self.assertIs(self.status_of("mock_visa"), TaskStatus.BLOCKED)
        self.assertIs(self.status_of("mock_payroll"), TaskStatus.BLOCKED)
        self.assertIs(self.status_of("mock_bank_letter"), TaskStatus.BLOCKED)

    def test_a_three_level_chain_unlocks_one_level_at_a_time(self) -> None:
        self.assertIs(self.status_of("mock_bank_letter"), TaskStatus.BLOCKED)

        self.finish(HR_OPS, "mock_hr_record")
        self.assertIs(self.status_of("mock_payroll"), TaskStatus.BLOCKED)

        self.finish(HR_OPS, "mock_documents")
        self.assertIs(self.status_of("mock_payroll"), TaskStatus.READY)
        self.assertIs(self.status_of("mock_bank_letter"), TaskStatus.BLOCKED)

        self.finish(FINANCE, "mock_payroll")
        self.assertIs(self.status_of("mock_bank_letter"), TaskStatus.READY)

    def test_independent_tasks_are_simultaneously_ready(self) -> None:
        self.finish(HR_OPS, "mock_hr_record")
        ready = self.ready_keys()
        self.assertEqual(len(ready), 3)
        # And they can be progressed in any order, concurrently.
        self.service.start_task(FACILITIES, self.onboarding_id,
                                self.task_id("mock_welcome_pack"))
        self.service.start_task(IT_MANAGER, self.onboarding_id,
                                self.task_id("mock_laptop"))
        self.assertIs(self.status_of("mock_welcome_pack"),
                      TaskStatus.IN_PROGRESS)
        self.assertIs(self.status_of("mock_laptop"), TaskStatus.IN_PROGRESS)
        self.assertIs(self.status_of("mock_documents"), TaskStatus.READY)

    def test_a_blocked_task_cannot_be_started(self) -> None:
        with self.assertRaises(PrerequisiteNotMetError) as caught:
            self.service.start_task(FINANCE, self.onboarding_id,
                                    self.task_id("mock_payroll"))
        self.assertIn("mock_documents", str(caught.exception))
        self.assertIs(self.status_of("mock_payroll"), TaskStatus.BLOCKED)

    def test_a_blocked_task_cannot_be_completed(self) -> None:
        with self.assertRaises(PrerequisiteNotMetError):
            self.service.complete_task(PRO, self.onboarding_id,
                                       self.task_id("mock_visa"), "DOC-X")
        self.assertIs(self.status_of("mock_visa"), TaskStatus.BLOCKED)

    def test_a_refused_start_is_audited_as_denied(self) -> None:
        with self.assertRaises(PrerequisiteNotMetError):
            self.service.start_task(FINANCE, self.onboarding_id,
                                    self.task_id("mock_payroll"))
        denied = [
            e for e in self.service.audit.events_for(HR_OPS)
            if e.action == "task.start" and e.outcome == "denied"
        ]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].detail["error"], "PrerequisiteNotMetError")


# ---------------------------------------------------------------------------
# Scenario 3 - supported task states
# ---------------------------------------------------------------------------


class SupportedStateTests(MockScenarioTestCase):
    def test_there_is_no_failed_task_status(self) -> None:
        """UC-02 models no FAILED state; this pins that so it cannot be assumed.

        The failure channels that *are* supported are exercised by
        :class:`FailureAndRetryTests` and :class:`AuthorizationTests`.
        """
        self.assertEqual(
            {s.value for s in TaskStatus},
            {"blocked", "ready", "in_progress", "complete", "cancelled"},
        )
        self.assertFalse(hasattr(TaskStatus, "FAILED"))

    def test_the_full_blocked_ready_in_progress_complete_path(self) -> None:
        self.assertIs(self.status_of("mock_documents"), TaskStatus.BLOCKED)
        self.finish(HR_OPS, "mock_hr_record")
        self.assertIs(self.status_of("mock_documents"), TaskStatus.READY)
        self.service.start_task(HR_OPS, self.onboarding_id,
                                self.task_id("mock_documents"))
        self.assertIs(self.status_of("mock_documents"), TaskStatus.IN_PROGRESS)
        self.finish(HR_OPS, "mock_documents")
        self.assertIs(self.status_of("mock_documents"), TaskStatus.COMPLETE)

    def test_start_and_complete_stamp_their_timestamps(self) -> None:
        self.service.start_task(HR_OPS, self.onboarding_id,
                                self.task_id("mock_hr_record"))
        started = self.record.task_by_key("mock_hr_record")
        assert started is not None
        self.assertIsNotNone(started.started_at)
        self.finish(HR_OPS, "mock_hr_record")
        done = self.record.task_by_key("mock_hr_record")
        assert done is not None
        self.assertIsNotNone(done.completed_at)

    def test_a_task_cannot_be_completed_twice(self) -> None:
        self.finish(HR_OPS, "mock_hr_record")
        with self.assertRaises(InvalidStateError):
            self.finish(HR_OPS, "mock_hr_record")

    def test_evidence_is_required_where_configured_and_recorded_when_given(self) -> None:
        self.finish(HR_OPS, "mock_hr_record")
        with self.assertRaises(EvidenceRequiredError):
            self.service.complete_task(HR_OPS, self.onboarding_id,
                                       self.task_id("mock_documents"))
        self.assertIs(self.status_of("mock_documents"), TaskStatus.READY)

        self.service.complete_task(HR_OPS, self.onboarding_id,
                                   self.task_id("mock_documents"),
                                   "DOC-PASSPORT-001")
        task = self.record.task_by_key("mock_documents")
        assert task is not None
        self.assertIs(task.status, TaskStatus.COMPLETE)
        self.assertEqual(task.evidence_ref, "DOC-PASSPORT-001")
        self.assertIsNotNone(task.evidence_recorded_at)

    def test_blank_evidence_is_refused(self) -> None:
        self.finish(HR_OPS, "mock_hr_record")
        with self.assertRaises(EvidenceRequiredError):
            self.service.complete_task(HR_OPS, self.onboarding_id,
                                       self.task_id("mock_documents"), "   ")

    def test_cancellation_cancels_open_work_and_preserves_completed_work(self) -> None:
        self.finish(HR_OPS, "mock_hr_record")
        request = self.service.request_cancellation(
            HR_OPS, self.onboarding_id, "role withdrawn"
        )
        self.service.decide_change(HR_OPS_APPROVER, request.request_id,
                                   DecisionOutcome.APPROVED)
        record = self.service.apply_cancellation(HR_OPS, request.request_id)

        self.assertIs(record.status, OnboardingStatus.CANCELLED)
        done = record.task_by_key("mock_hr_record")
        assert done is not None
        self.assertIs(done.status, TaskStatus.COMPLETE)
        for key in ALL_KEYS - {"mock_hr_record"}:
            task = record.task_by_key(key)
            assert task is not None
            with self.subTest(key=key):
                self.assertIs(task.status, TaskStatus.CANCELLED)
                self.assertIsNotNone(task.cancelled_at)

    def test_overdue_is_derived_not_stored(self) -> None:
        """The same stored record yields different overdue sets per as_of."""
        early = self.service.status(HR_OPS, self.onboarding_id, AS_OF_PLANNING)
        late = self.service.status(HR_OPS, self.onboarding_id,
                                   AS_OF_OVERDUE_LATE)
        self.assertEqual(early.overdue_task_ids, [])
        self.assertEqual(
            {tid.split("#", 1)[1] for tid in late.overdue_task_ids},
            {"mock_hr_record", "mock_documents"},
        )
        # No task carries a stored "overdue" status.
        self.assertNotIn("overdue",
                         {t.status.value for t in self.record.tasks})


# ---------------------------------------------------------------------------
# Scenario 4 - due-date change and rescheduling
# ---------------------------------------------------------------------------


class RescheduleTests(MockScenarioTestCase):
    NEW_JOINING = date(2026, 11, 30)   # two weeks later, a Monday

    def approved_date_change(self) -> str:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=self.NEW_JOINING
        )
        self.service.decide_change(HR_OPS_APPROVER, request.request_id,
                                   DecisionOutcome.APPROVED)
        return request.request_id

    def test_a_date_change_cannot_be_applied_without_approval(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=self.NEW_JOINING
        )
        with self.assertRaises(ApprovalRequiredError):
            self.service.apply_change(HR_OPS, request.request_id)
        self.assertEqual(self.record.joining_date, JOINING)
        self.assertEqual(self.record.revision, 0)

    def test_the_requester_may_not_approve_their_own_change(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=self.NEW_JOINING
        )
        with self.assertRaises(UnauthorizedActionError):
            self.service.decide_change(HR_OPS, request.request_id,
                                       DecisionOutcome.APPROVED)

    def test_a_rejected_change_leaves_the_plan_untouched(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=self.NEW_JOINING
        )
        self.service.decide_change(HR_OPS_APPROVER, request.request_id,
                                   DecisionOutcome.REJECTED)
        with self.assertRaises(ApprovalRequiredError):
            self.service.apply_change(HR_OPS, request.request_id)
        self.assertEqual(self.record.joining_date, JOINING)

    def test_an_approved_change_reschedules_every_open_task(self) -> None:
        outcome = self.service.apply_change(HR_OPS,
                                            self.approved_date_change())
        self.assertEqual(outcome.revision, 1)
        self.assertEqual(len(outcome.rescheduled_task_ids), len(ALL_KEYS))
        self.assertEqual(outcome.added_task_ids, [])
        self.assertEqual(outcome.removed_task_ids, [])
        self.assertEqual(self.record.joining_date, self.NEW_JOINING)
        # Every due date moved by exactly ten calendar days (two weeks of
        # business days, no holidays in the window).
        for task in self.record.tasks:
            with self.subTest(key=task.template_key):
                self.assertEqual(
                    (task.due_date - DUE[task.template_key]).days, 14
                )

    def test_rescheduling_clears_a_previously_overdue_task(self) -> None:
        before = self.service.status(HR_OPS, self.onboarding_id,
                                     AS_OF_OVERDUE_LATE)
        self.assertTrue(before.overdue_task_ids)
        self.service.apply_change(HR_OPS, self.approved_date_change())
        after = self.service.status(HR_OPS, self.onboarding_id,
                                    AS_OF_OVERDUE_LATE)
        self.assertEqual(after.overdue_task_ids, [])
        self.assertTrue(after.is_on_track)

    def test_completed_work_is_never_rescheduled_or_reopened(self) -> None:
        self.finish(HR_OPS, "mock_hr_record")
        original_due = DUE["mock_hr_record"]
        outcome = self.service.apply_change(HR_OPS,
                                            self.approved_date_change())
        done = self.record.task_by_key("mock_hr_record")
        assert done is not None
        self.assertIs(done.status, TaskStatus.COMPLETE)
        self.assertEqual(done.due_date, original_due)
        self.assertIn(self.task_id("mock_hr_record"), outcome.retained_task_ids)
        self.assertNotIn(self.task_id("mock_hr_record"),
                         outcome.rescheduled_task_ids)

    def test_readiness_survives_a_reschedule(self) -> None:
        self.finish(HR_OPS, "mock_hr_record")
        self.service.apply_change(HR_OPS, self.approved_date_change())
        self.assertEqual(
            self.ready_keys(),
            {"mock_documents", "mock_laptop", "mock_welcome_pack"},
        )

    def test_an_unschedulable_date_is_refused_at_request_time(self) -> None:
        from onboarding_task_management.errors import CalendarRangeError

        with self.assertRaises(CalendarRangeError):
            self.service.request_change(HR_OPS, self.onboarding_id,
                                        joining_date=date(2028, 5, 1))
        self.assertEqual(self.record.joining_date, JOINING)
        self.assertEqual(self.record.revision, 0)

    def test_the_replan_is_audited_with_counts_only(self) -> None:
        self.service.apply_change(HR_OPS, self.approved_date_change())
        applied = [e for e in self.service.audit.events_for(HR_OPS)
                   if e.action == "change.apply" and e.outcome == "replanned"]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].detail["rescheduled"], str(len(ALL_KEYS)))
        self.assertEqual(applied[0].detail["revision"], "1")


# ---------------------------------------------------------------------------
# Scenario 5 - reminders
# ---------------------------------------------------------------------------


class ReminderTests(MockScenarioTestCase):
    def drafts(self, as_of: date):
        return self.service.reminder_drafts(HR_OPS, self.onboarding_id, as_of)

    def test_no_drafts_while_nothing_is_due_or_overdue(self) -> None:
        self.assertEqual(self.drafts(AS_OF_PLANNING), [])

    def test_a_task_due_within_the_lead_window_gets_a_due_soon_draft(self) -> None:
        drafts = self.drafts(AS_OF_DUE_SOON)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].reason, "due_soon")
        self.assertEqual(drafts[0].task_ids, [self.task_id("mock_hr_record")])
        self.assertIs(drafts[0].recipient_role, Role.HR_OPS)

    def test_drafts_are_grouped_one_per_owner_per_reason(self) -> None:
        drafts = self.drafts(AS_OF_OVERDUE_LATE)
        pairs = [(d.recipient_role, d.reason) for d in drafts]
        self.assertEqual(len(pairs), len(set(pairs)))
        # hr_ops owns both overdue tasks, so they share one draft.
        overdue = [d for d in drafts if d.reason == "overdue"]
        self.assertEqual(len(overdue), 1)
        self.assertIs(overdue[0].recipient_role, Role.HR_OPS)
        self.assertEqual(
            {tid.split("#", 1)[1] for tid in overdue[0].task_ids},
            {"mock_hr_record", "mock_documents"},
        )

    def test_a_task_is_never_in_both_an_overdue_and_a_due_soon_draft(self) -> None:
        drafts = self.drafts(AS_OF_OVERDUE_LATE)
        overdue = {t for d in drafts if d.reason == "overdue" for t in d.task_ids}
        soon = {t for d in drafts if d.reason == "due_soon" for t in d.task_ids}
        self.assertTrue(overdue)
        self.assertTrue(soon)
        self.assertEqual(overdue & soon, set())

    def test_draft_ids_are_deterministic_so_repeats_do_not_duplicate(self) -> None:
        first = self.drafts(AS_OF_OVERDUE_LATE)
        second = self.drafts(AS_OF_OVERDUE_LATE)
        self.assertEqual([d.draft_id for d in first],
                         [d.draft_id for d in second])
        self.assertEqual(len(first), len({d.draft_id for d in first}))

    def test_completing_a_task_removes_it_from_the_next_reminder(self) -> None:
        before = self.drafts(AS_OF_OVERDUE_LATE)
        self.assertIn(self.task_id("mock_hr_record"),
                      {t for d in before for t in d.task_ids})
        self.finish(HR_OPS, "mock_hr_record")
        after = self.drafts(AS_OF_OVERDUE_LATE)
        self.assertNotIn(self.task_id("mock_hr_record"),
                         {t for d in after for t in d.task_ids})

    def test_every_draft_is_bilingual_and_marked_not_sent(self) -> None:
        for draft in self.drafts(AS_OF_OVERDUE_LATE):
            with self.subTest(draft=draft.draft_id):
                self.assertEqual(draft.delivery_status, "draft_not_sent")
                self.assertTrue(draft.subject_en.strip())
                self.assertTrue(draft.subject_ar.strip())
                self.assertTrue(draft.body_en.strip())
                self.assertTrue(draft.body_ar.strip())
                # Arabic body must actually contain Arabic script.
                self.assertTrue(any("؀" <= c <= "ۿ"
                                    for c in draft.body_ar))

    def test_drafts_carry_identifiers_only(self) -> None:
        for draft in self.drafts(AS_OF_OVERDUE_LATE):
            with self.subTest(draft=draft.draft_id):
                self.assertEqual(draft.employee_id, self.employee_id)
                for field in ("name", "email", "phone", "@"):
                    self.assertNotIn(field, draft.body_en.lower())

    def test_preparing_drafts_is_audited_as_not_sent(self) -> None:
        self.drafts(AS_OF_OVERDUE_LATE)
        events = [e for e in self.service.audit.events_for(HR_OPS)
                  if e.action == "reminder.draft"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].outcome, "prepared_not_sent")


# ---------------------------------------------------------------------------
# Scenario 6 - escalation thresholds
# ---------------------------------------------------------------------------


class EscalationThresholdTests(MockScenarioTestCase):
    def items(self, as_of: date):
        return self.service.escalations(HR_OPS, self.onboarding_id, as_of)

    def test_nothing_escalates_while_the_plan_is_on_track(self) -> None:
        self.assertEqual(self.items(AS_OF_PLANNING), [])

    def test_nothing_escalates_on_the_due_date_itself(self) -> None:
        """A task due today is not yet overdue."""
        self.assertEqual(self.items(DUE["mock_hr_record"]), [])

    def test_one_day_overdue_escalates_to_the_first_responder(self) -> None:
        items = self.items(AS_OF_OVERDUE_EARLY)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.subject_type, "task")
        self.assertEqual(item.subject_id, self.task_id("mock_hr_record"))
        self.assertIs(item.level, EscalationLevel.HR_OFFICER)
        self.assertIs(item.escalate_to, Role.HR_OFFICER)
        self.assertIs(item.owner, Role.HR_OPS)
        self.assertEqual(item.days_waiting, 1)
        self.assertEqual(item.source_rule, "GR-12")

    def test_past_the_response_window_it_escalates_to_the_operations_lead(self) -> None:
        tasks = [i for i in self.items(AS_OF_OVERDUE_LATE)
                 if i.subject_id == self.task_id("mock_hr_record")]
        self.assertEqual(len(tasks), 1)
        self.assertIs(tasks[0].level, EscalationLevel.HR_OPS_LEAD)
        self.assertIs(tasks[0].escalate_to, Role.HR_LEAD)
        self.assertEqual(tasks[0].days_waiting, 3)

    def test_a_delayed_milestone_escalates_once_a_required_task_is_overdue(self) -> None:
        early = [i for i in self.items(AS_OF_OVERDUE_EARLY)
                 if i.subject_type == "milestone"]
        self.assertEqual(early, [])
        late = [i for i in self.items(AS_OF_OVERDUE_LATE)
                if i.subject_type == "milestone"]
        self.assertEqual(len(late), 1)
        self.assertIn("mock_day_one_ready", late[0].subject_id)

    def test_completing_the_overdue_task_stops_the_escalation(self) -> None:
        self.assertTrue(self.items(AS_OF_OVERDUE_EARLY))
        self.finish(HR_OPS, "mock_hr_record")
        self.assertEqual(self.items(AS_OF_OVERDUE_EARLY), [])

    def test_day_one_critical_work_escalates_even_when_not_overdue(self) -> None:
        """The second GR-12 trigger, isolated.

        ``mock_access_grant`` is due 2026-11-13 and is still in the future at
        AS_OF_NEAR_JOINING (2026-11-12), so it is NOT overdue. It escalates
        anyway, because it is day-1 critical and joining is two business days
        away -- which is exactly ``JOINING_RISK_DAYS_BEFORE``.
        """
        task = self.record.task_by_key("mock_access_grant")
        assert task is not None
        self.assertFalse(task.due_date < AS_OF_NEAR_JOINING)

        items = {i.subject_id: i for i in self.items(AS_OF_NEAR_JOINING)}
        grant = items.get(self.task_id("mock_access_grant"))
        self.assertIsNotNone(grant, "day-1 critical task did not escalate")
        assert grant is not None
        self.assertEqual(grant.days_waiting, 0)        # not overdue
        self.assertIs(grant.level, EscalationLevel.HR_OFFICER)
        self.assertIn("day-1 critical", grant.reason)

    def test_a_non_critical_task_does_not_escalate_merely_for_being_close(self) -> None:
        """The control for the proximity rule.

        At AS_OF_NEAR_JOINING both ``mock_access_grant`` (due 11-13) and
        ``mock_welcome_pack`` (due 11-16) are open and not yet overdue, and
        joining is two business days away. Only the day-1-critical one may
        escalate; ``workplace`` is not a critical category.
        """
        for key in ("mock_access_grant", "mock_welcome_pack"):
            task = self.record.task_by_key(key)
            assert task is not None
            with self.subTest(key=key):
                self.assertFalse(task.due_date < AS_OF_NEAR_JOINING)

        subjects = {i.subject_id for i in self.items(AS_OF_NEAR_JOINING)}
        self.assertIn(self.task_id("mock_access_grant"), subjects)
        self.assertNotIn(self.task_id("mock_welcome_pack"), subjects)

    def test_a_stalled_approval_escalates_past_its_gate_sla(self) -> None:
        request = self.service.request_change(HR_OPS, self.onboarding_id,
                                              joining_date=date(2026, 11, 30))
        # The request records the real clock; pin it to the planning date this scenario is written around, so the test
        # does not depend on the day it runs (it passed only while "today" was before the fixed as-of date).
        self.service.inbox.get(HR_OPS, request.request_id).created_at = datetime(2026, 10, 26, tzinfo=timezone.utc)
        approvals = [i for i in self.items(AS_OF_OVERDUE_LATE)
                     if i.subject_type == "approval"]
        self.assertEqual(len(approvals), 1)
        self.assertIn("date_change_approval", approvals[0].reason)

    def test_escalation_detection_is_audited(self) -> None:
        self.items(AS_OF_OVERDUE_EARLY)
        events = [e for e in self.service.audit.events_for(HR_OPS)
                  if e.action == "escalation.detected"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].detail["count"], "1")


# ---------------------------------------------------------------------------
# Scenario 7 - monitoring views
# ---------------------------------------------------------------------------


class MonitoringTests(MockScenarioTestCase):
    def test_outstanding_activities_name_the_owner_and_the_follow_up(self) -> None:
        activities = self.service.outstanding_activities(
            HR_OPS, self.onboarding_id, AS_OF_OVERDUE_EARLY
        )
        self.assertEqual(len(activities), len(ALL_KEYS))
        by_key = {a.template_key: a for a in activities}

        root = by_key["mock_hr_record"]
        self.assertTrue(root.is_overdue)
        self.assertEqual(root.days_overdue, 1)
        self.assertIs(root.owner, Role.HR_OPS)
        self.assertIn("OVERDUE", root.required_follow_up)

        payroll = by_key["mock_payroll"]
        self.assertFalse(payroll.is_overdue)
        self.assertEqual(payroll.blocked_by_task_ids,
                         [self.task_id("mock_documents")])
        self.assertIn("prerequisite", payroll.required_follow_up)

        self.assertIn("evidence", by_key["mock_visa"].required_follow_up)

    def test_joining_risk_is_raised_by_an_overdue_task(self) -> None:
        safe = self.service.joining_risk(HR_OPS, self.onboarding_id,
                                         AS_OF_PLANNING)
        self.assertFalse(safe.at_risk)
        risky = self.service.joining_risk(HR_OPS, self.onboarding_id,
                                          AS_OF_OVERDUE_EARLY)
        self.assertTrue(risky.at_risk)
        self.assertEqual(risky.overdue_task_ids,
                         [self.task_id("mock_hr_record")])

    def test_milestone_state_tracks_the_plan(self) -> None:
        [pending] = self.service.milestones(HR_OPS, self.onboarding_id,
                                            AS_OF_PLANNING)
        self.assertIs(pending.status, MilestoneStatus.PENDING)
        self.assertEqual(len(pending.required_task_ids), 3)

        [at_risk] = self.service.milestones(HR_OPS, self.onboarding_id,
                                            AS_OF_OVERDUE_LATE)
        self.assertIs(at_risk.status, MilestoneStatus.AT_RISK)

        for key in ("mock_hr_record", "mock_documents", "mock_laptop",
                    "mock_accounts"):
            self.finish(HR_OPS if key in ("mock_hr_record", "mock_documents")
                        else IT_MANAGER, key)
        [achieved] = self.service.milestones(HR_OPS, self.onboarding_id,
                                             AS_OF_OVERDUE_LATE)
        self.assertIs(achieved.status, MilestoneStatus.ACHIEVED)
        self.assertEqual(achieved.outstanding_task_ids, [])

    def test_the_checklist_view_explains_every_blocker(self) -> None:
        view = self.service.checklist(HR_OPS, self.onboarding_id,
                                      AS_OF_OVERDUE_EARLY)
        entries = {e.template_key: e for e in view.entries}
        self.assertEqual(entries["mock_payroll"].blocked_by_keys,
                         ["mock_documents"])
        self.assertTrue(entries["mock_hr_record"].is_overdue)
        self.assertEqual(entries["mock_hr_record"].days_overdue, 1)
        self.assertEqual(
            {tid.split("#", 1)[1] for tid in view.ready_task_ids},
            {"mock_hr_record"},
        )


# ---------------------------------------------------------------------------
# Scenario 8 - authorization and human approval
# ---------------------------------------------------------------------------


class AuthorizationTests(MockScenarioTestCase):
    def test_a_non_owner_non_admin_may_not_act_on_a_task(self) -> None:
        self.finish(HR_OPS, "mock_hr_record")
        with self.assertRaises(UnauthorizedActionError):
            self.service.start_task(FINANCE, self.onboarding_id,
                                    self.task_id("mock_laptop"))
        self.assertIs(self.status_of("mock_laptop"), TaskStatus.READY)

    def test_the_configured_owner_may_act(self) -> None:
        self.finish(HR_OPS, "mock_hr_record")
        self.service.start_task(IT_MANAGER, self.onboarding_id,
                                self.task_id("mock_laptop"))
        self.assertIs(self.status_of("mock_laptop"), TaskStatus.IN_PROGRESS)

    def test_an_admin_role_may_act_on_any_task(self) -> None:
        self.finish(HR_OPS, "mock_hr_record")
        self.service.start_task(HR_OPS, self.onboarding_id,
                                self.task_id("mock_laptop"))
        self.assertIs(self.status_of("mock_laptop"), TaskStatus.IN_PROGRESS)

    def test_a_non_hr_role_may_not_create_an_onboarding(self) -> None:
        with self.assertRaises(UnauthorizedActionError):
            self.service.create_onboarding(IT_MANAGER,
                                           mock_trigger("MOCK-099"))

    def test_an_approval_for_one_gate_cannot_drive_another_operation(self) -> None:
        """A date-change approval must not be replayable as a cancellation."""
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=date(2026, 11, 30)
        )
        self.service.decide_change(HR_OPS_APPROVER, request.request_id,
                                   DecisionOutcome.APPROVED)
        with self.assertRaises(UnauthorizedActionError):
            self.service.apply_cancellation(HR_OPS, request.request_id)
        self.assertIs(self.record.status, OnboardingStatus.ACTIVE)

    def test_a_refused_action_is_audited_and_changes_nothing(self) -> None:
        before = self.statuses()
        with self.assertRaises(UnauthorizedActionError):
            self.service.start_task(FINANCE, self.onboarding_id,
                                    self.task_id("mock_hr_record"))
        self.assertEqual(self.statuses(), before)
        denied = [e for e in self.service.audit.events_for(HR_OPS)
                  if e.outcome == "denied"]
        self.assertTrue(denied)


# ---------------------------------------------------------------------------
# Scenario 9 - multi-client isolation
# ---------------------------------------------------------------------------


class MultiClientIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = OnboardingService(mock_registry())
        self.a = self.service.create_onboarding(HR_OPS,
                                                mock_trigger("MOCK-002"))
        self.b = self.service.create_onboarding(
            HR_OPS_B, mock_trigger("MOCK-003", client_id=CLIENT_MOCK_B)
        )

    def test_each_client_gets_its_own_configuration(self) -> None:
        self.assertEqual(len(self.a.tasks), len(ALL_KEYS))
        self.assertEqual(len(self.b.tasks), 1)

    def test_a_shared_template_key_uses_each_clients_own_owner_and_lead_time(self) -> None:
        left = self.a.task_by_key("mock_hr_record")
        right = self.b.task_by_key("mock_hr_record")
        assert left is not None and right is not None
        self.assertIs(left.owner, Role.HR_OPS)
        self.assertIs(right.owner, Role.HR_OFFICER)
        self.assertEqual(left.due_date, date(2026, 11, 2))    # -10
        self.assertEqual(right.due_date, date(2026, 11, 10))  # -4

    def test_an_actor_cannot_read_another_clients_case(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.get(HR_OPS_B, self.a.onboarding_id)
        with self.assertRaises(DataIsolationError):
            self.service.get(HR_OPS, self.b.onboarding_id)

    def test_an_actor_cannot_act_on_another_clients_task(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.start_task(HR_OPS_B, self.a.onboarding_id,
                                    f"{self.a.onboarding_id}#mock_hr_record")

    def test_an_actor_cannot_create_for_another_client(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.create_onboarding(
                HR_OPS_B, mock_trigger("MOCK-004", client_id=CLIENT_MOCK)
            )

    def test_audit_is_partitioned_by_client(self) -> None:
        left = self.service.audit.events_for(HR_OPS)
        right = self.service.audit.events_for(HR_OPS_B)
        self.assertTrue(left and right)
        self.assertEqual({e.client_id for e in left}, {CLIENT_MOCK})
        self.assertEqual({e.client_id for e in right}, {CLIENT_MOCK_B})
        self.assertEqual(
            {e.event_id for e in left} & {e.event_id for e in right}, set()
        )

    def test_a_denied_cross_client_read_is_audited_against_the_caller(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.get(HR_OPS_B, self.a.onboarding_id)
        denied = [e for e in self.service.audit.events_for(HR_OPS_B)
                  if e.outcome == "denied_isolation"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].client_id, CLIENT_MOCK_B)

    def test_progress_on_one_client_does_not_affect_the_other(self) -> None:
        self.service.complete_task(HR_OPS, self.a.onboarding_id,
                                   f"{self.a.onboarding_id}#mock_hr_record")
        other = self.service.get(HR_OPS_B, self.b.onboarding_id)
        task = other.task_by_key("mock_hr_record")
        assert task is not None
        self.assertIs(task.status, TaskStatus.READY)


# ---------------------------------------------------------------------------
# Scenario 10 - failure handling, retries, and recovery
# ---------------------------------------------------------------------------


class FailureAndRetryTests(MockScenarioTestCase):
    def test_a_tool_refusal_is_reported_as_a_failed_result_not_an_exception(self) -> None:
        from onboarding_task_management.agent.tools import UC02Tools

        tools = UC02Tools(self.service, FINANCE)
        result = tools.update_task_state(
            self.onboarding_id, self.task_id("mock_payroll"), action="start"
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.failed)
        self.assertEqual(result.error_code, "POLICY_BREACH")
        self.assertIs(self.status_of("mock_payroll"), TaskStatus.BLOCKED)

    def test_an_unsupported_tool_action_is_refused_without_touching_state(self) -> None:
        from onboarding_task_management.agent.tools import UC02Tools

        tools = UC02Tools(self.service, HR_OPS)
        result = tools.update_task_state(
            self.onboarding_id, self.task_id("mock_hr_record"),
            action="delete",
        )
        self.assertFalse(result.ok)
        self.assertIn("unsupported action", result.error)
        self.assertIs(self.status_of("mock_hr_record"), TaskStatus.READY)

    def test_delivery_without_an_approval_is_blocked_and_not_queued(self) -> None:
        outlook = make_mock_outlook()
        drafts = self.service.reminder_drafts(HR_OPS, self.onboarding_id,
                                              AS_OF_OVERDUE_LATE)
        self.assertTrue(drafts)
        receipts = [outlook.send(message_from_draft(d)) for d in drafts]
        self.assertTrue(all(r.status is DeliveryStatus.BLOCKED_NO_APPROVAL
                            for r in receipts))
        self.assertEqual(outlook.sent_count(), 0)

    def test_a_transient_delivery_failure_succeeds_on_retry_without_duplicating(self) -> None:
        drafts = self.service.reminder_drafts(HR_OPS, self.onboarding_id,
                                              AS_OF_OVERDUE_LATE)
        draft = drafts[0]
        outlook = make_mock_outlook(fail_times={draft.draft_id: 2})
        message = message_from_draft(draft, approval_id="APR-MOCK-1")

        first = outlook.send(message)
        second = outlook.send(message)
        self.assertIs(first.status, DeliveryStatus.FAILED)
        self.assertIs(second.status, DeliveryStatus.FAILED)
        self.assertEqual(outlook.sent_count(), 0)

        third = outlook.send(message)
        self.assertIs(third.status, DeliveryStatus.SIMULATED_SENT)
        self.assertEqual(outlook.sent_count(), 1)

        # A fourth attempt must not queue a second copy.
        fourth = outlook.send(message)
        self.assertIs(fourth.status, DeliveryStatus.DUPLICATE_IGNORED)
        self.assertEqual(outlook.sent_count(), 1)
        self.assertEqual(outlook.attempts_for(draft.draft_id), 4)

    def test_a_permanently_failing_recipient_never_enters_the_outbox(self) -> None:
        drafts = self.service.reminder_drafts(HR_OPS, self.onboarding_id,
                                              AS_OF_OVERDUE_LATE)
        draft = drafts[0]
        outlook = make_mock_outlook(fail_keys={draft.draft_id})
        message = message_from_draft(draft, approval_id="APR-MOCK-1")
        for _ in range(3):
            self.assertIs(outlook.send(message).status, DeliveryStatus.FAILED)
        self.assertEqual(outlook.sent_count(), 0)

    def test_uc02_itself_never_marks_a_draft_as_sent(self) -> None:
        drafts = self.service.reminder_drafts(HR_OPS, self.onboarding_id,
                                              AS_OF_OVERDUE_LATE)
        self.assertEqual({d.delivery_status for d in drafts},
                         {"draft_not_sent"})


class WorkflowRecoveryTests(unittest.TestCase):
    """A workflow interrupted mid-flight must resume from stored state."""

    def setUp(self) -> None:
        self.client = InMemoryTableClient()
        self.store = OnboardingStore(self.client)
        self.service = OnboardingService(mock_registry(), store=self.store)
        self.record = self.service.create_onboarding(
            HR_OPS, mock_trigger("MOCK-001")
        )
        self.onboarding_id = self.record.onboarding_id

    def task_id(self, key: str) -> str:
        return f"{self.onboarding_id}#{key}"

    def restart(self) -> OnboardingService:
        """A new process reading only what was written."""
        rebuilt = OnboardingService(
            mock_registry(),
            store=OnboardingStore(
                InMemoryTableClient.from_snapshot(self.client.snapshot())
            ),
        )
        self.assertEqual(rebuilt.hydrate(CLIENT_MOCK), 1)
        return rebuilt

    def test_the_plan_is_written_through_on_creation(self) -> None:
        self.assertEqual(self.client.row_count("onboarding"), 1)
        self.assertEqual(self.client.row_count("task"), len(ALL_KEYS))
        # Seven dependency edges: every task except the root has one parent.
        self.assertEqual(self.client.row_count("task_dependency"),
                         len(ALL_KEYS) - 1)

    def test_mid_workflow_state_survives_a_restart(self) -> None:
        self.service.complete_task(HR_OPS, self.onboarding_id,
                                   self.task_id("mock_hr_record"))
        self.service.start_task(IT_MANAGER, self.onboarding_id,
                                self.task_id("mock_laptop"))
        self.service.complete_task(HR_OPS, self.onboarding_id,
                                   self.task_id("mock_documents"),
                                   "DOC-PASSPORT-001")

        rebuilt = self.restart()
        record = rebuilt.get(HR_OPS, self.onboarding_id)
        statuses = {t.template_key: t.status for t in record.tasks}

        self.assertIs(statuses["mock_hr_record"], TaskStatus.COMPLETE)
        self.assertIs(statuses["mock_laptop"], TaskStatus.IN_PROGRESS)
        self.assertIs(statuses["mock_documents"], TaskStatus.COMPLETE)
        self.assertIs(statuses["mock_payroll"], TaskStatus.READY)
        self.assertIs(statuses["mock_visa"], TaskStatus.READY)
        self.assertIs(statuses["mock_bank_letter"], TaskStatus.BLOCKED)

        evidence = record.task_by_key("mock_documents")
        assert evidence is not None
        self.assertEqual(evidence.evidence_ref, "DOC-PASSPORT-001")

    def test_the_workflow_continues_correctly_after_a_restart(self) -> None:
        self.service.complete_task(HR_OPS, self.onboarding_id,
                                   self.task_id("mock_hr_record"))
        rebuilt = self.restart()
        rebuilt.complete_task(IT_MANAGER, self.onboarding_id,
                              self.task_id("mock_laptop"))
        after = rebuilt.get(HR_OPS, self.onboarding_id)
        accounts = after.task_by_key("mock_accounts")
        assert accounts is not None
        self.assertIs(accounts.status, TaskStatus.READY)

    def test_dependency_edges_survive_a_restart(self) -> None:
        rebuilt = self.restart()
        record = rebuilt.get(HR_OPS, self.onboarding_id)
        letter = record.task_by_key("mock_bank_letter")
        assert letter is not None
        self.assertEqual(letter.depends_on, [self.task_id("mock_payroll")])

    def test_a_pending_approval_survives_a_restart_and_can_be_decided(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=date(2026, 11, 30)
        )
        rebuilt = self.restart()
        rebuilt.decide_change(HR_OPS_APPROVER, request.request_id,
                              DecisionOutcome.APPROVED)
        outcome = rebuilt.apply_change(HR_OPS, request.request_id)
        self.assertEqual(outcome.revision, 1)
        self.assertEqual(
            rebuilt.get(HR_OPS, self.onboarding_id).joining_date,
            date(2026, 11, 30),
        )

    def test_a_replan_survives_a_restart_with_its_revision(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=date(2026, 11, 30)
        )
        self.service.decide_change(HR_OPS_APPROVER, request.request_id,
                                   DecisionOutcome.APPROVED)
        self.service.apply_change(HR_OPS, request.request_id)

        rebuilt = self.restart()
        record = rebuilt.get(HR_OPS, self.onboarding_id)
        self.assertEqual(record.revision, 1)
        self.assertEqual(record.joining_date, date(2026, 11, 30))
        store = rebuilt.store
        assert store is not None  # narrow for the type checker
        self.assertEqual(
            store.plan_revisions(CLIENT_MOCK, self.onboarding_id)[0]["revision"],
            1,
        )

    def test_repeated_writes_do_not_multiply_rows(self) -> None:
        self.service.start_task(HR_OPS, self.onboarding_id,
                                self.task_id("mock_hr_record"))
        self.service.complete_task(HR_OPS, self.onboarding_id,
                                   self.task_id("mock_hr_record"))
        self.assertEqual(self.client.row_count("task"), len(ALL_KEYS))
        self.assertEqual(self.client.row_count("onboarding"), 1)

    def test_a_storage_failure_rolls_back_the_in_memory_case(self) -> None:
        """A write that did not happen must never be reported as success."""
        from onboarding_task_management.persistence import PersistenceError

        class BrokenStore(OnboardingStore):
            def save_onboarding(self, record, *, config=None):  # type: ignore[override]
                raise PersistenceError("backend unavailable")

        service = OnboardingService(mock_registry(),
                                    store=BrokenStore(InMemoryTableClient()))
        with self.assertRaises(PersistenceError):
            service.create_onboarding(HR_OPS, mock_trigger("MOCK-002"))
        self.assertEqual(service._client_records(CLIENT_MOCK), {})
        failed = [e for e in service.audit.events_for(HR_OPS)
                  if e.outcome == "failed_persist"]
        self.assertEqual(len(failed), 1)

    def test_the_audit_trail_is_persisted_and_survives_a_restart(self) -> None:
        """The former "known gap" test, updated now that the gap is closed.

        A store-backed service writes every audit event through to the store as it
        is recorded, and ``hydrate`` restores them, so the trail survives a restart.
        (Converging with the shared harness's AuditLog stays open blocker 5 in
        ``docs/UC02_ARCHITECTURE.md`` section 9.)
        """
        self.service.complete_task(HR_OPS, self.onboarding_id,
                                   self.task_id("mock_hr_record"))
        live = self.service.audit.events_for(HR_OPS)
        self.assertTrue(live)
        self.assertEqual(self.client.row_count("audit_event"), len(live))
        rebuilt = self.restart()
        self.assertEqual([e.event_id for e in rebuilt.audit.events_for(HR_OPS)],
                         [e.event_id for e in live])
        # The seam stays idempotent when called explicitly: nothing is added twice.
        before = self.client.row_count("audit_event")
        self.service._persist_audit(HR_OPS)
        self.assertEqual(self.client.row_count("audit_event"), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
