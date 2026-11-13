"""UC-02 business-rule and edge-case coverage.

Covers the activities and rules added in the alignment pass: background
verification, visa, medical, biometrics, insurance, travel, accommodation,
food, banking letters, HR-system activation, grade filtering, owner
validation, completion evidence, calendar authority, and the UC-02/UC-06
responsibility boundary.

Every assertion reflects behaviour supported by an approved business rule.
Where a rule depends on an unresolved decision, the test asserts only the
supported part and says so in a comment.
"""

from __future__ import annotations

import unittest
from datetime import date

from onboarding_task_management.calendar_uae import (
    add_business_days,
    calendar_status,
    is_business_day,
)
from onboarding_task_management.contracts import (
    DecisionOutcome,
    OnboardingStatus,
    Role,
    TaskCategory,
    TaskStatus,
)
from onboarding_task_management.errors import (
    DataIsolationError,
    EvidenceRequiredError,
    PrerequisiteNotMetError,
)
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import (
    CLIENT_ALPHA,
    COHORT_EMPLOYEE_IDS,
    GRADE_L4,
    build_registry,
    make_trigger,
    persona,
)
from tests.support import (
    FACILITIES,
    HR_LEAD,
    HR_OPS,
    HR_OPS_APPROVER,
    HR_OPS_BETA,
    ServiceTestCase,
)

AS_OF = date(2026, 10, 26)
JOINING = date(2026, 11, 16)


class ExtendedPlanTestCase(ServiceTestCase):
    """Base: EMP-007 is the relocation hire with every extended activity."""

    def setUp(self) -> None:
        super().setUp()
        self.ext = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-007"))
        )
        self.ext_id = self.ext.onboarding_id

    def ext_record(self):
        return self.service.get(HR_OPS, self.ext_id)

    def ext_task(self, key: str):
        task = self.ext_record().task_by_key(key)
        assert task is not None, f"no task {key!r}"
        return task

    def ext_complete(self, actor, key: str, evidence: str | None = "EVD-001"):
        return self.service.complete_task(
            actor, self.ext_id, f"{self.ext_id}#{key}", evidence
        )


# ---------------------------------------------------------------------------
# UC02-BR-02 — grade filtering (nationality is a documented blocker)
# ---------------------------------------------------------------------------


class GradeFilteringTests(unittest.TestCase):
    def _config(self) -> ClientOnboardingConfig:
        base = dict(
            category=TaskCategory.HR_RECORD,
            title="T",
            title_ar="ت",
            owner=Role.HR_OPS,
            offset_days=-1,
        )
        return ClientOnboardingConfig(
            approval_status="approved",  # test fixture
            client_id="C",
            templates=(
                TaskTemplate(key="all_grades", **base),
                TaskTemplate(key="senior_only", grades=(GRADE_L4,), **base),
            ),
        )

    def test_grade_filters_templates(self) -> None:
        config = self._config()
        junior = config.applicable(location="x", project="y", role_id="z", grade="L2")
        senior = config.applicable(location="x", project="y", role_id="z", grade="L4")
        self.assertEqual({t.key for t in junior}, {"all_grades"})
        self.assertEqual({t.key for t in senior}, {"all_grades", "senior_only"})

    def test_blank_grade_excludes_grade_restricted_templates(self) -> None:
        config = self._config()
        none = config.applicable(location="x", project="y", role_id="z")
        self.assertEqual({t.key for t in none}, {"all_grades"})

    def test_grade_reaches_the_generated_plan(self) -> None:
        service = OnboardingService(build_registry())
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-007")))
        self.assertEqual(record.grade, GRADE_L4)

    def test_nationality_is_not_a_filter_dimension(self) -> None:
        # UC02-BR-02 names nationality, but filtering onboarding activities by
        # nationality requires an approved governance policy that does not
        # exist. The blocker is recorded; no filter is implemented.
        self.assertNotIn("nationality", TaskTemplate.model_fields)
        self.assertNotIn("nationality", ClientOnboardingConfig.model_fields)


# ---------------------------------------------------------------------------
# UC02-BR-03 — owner validation
# ---------------------------------------------------------------------------


class OwnerValidationTests(unittest.TestCase):
    def test_unknown_owner_role_is_rejected_at_configuration_load(self) -> None:
        with self.assertRaises(ValueError):
            TaskTemplate(
                key="a",
                category=TaskCategory.HR_RECORD,
                title="T",
                title_ar="ت",
                owner="not_a_role",  # type: ignore[arg-type]
                offset_days=-1,
            )

    def test_system_pseudo_role_owner_is_flagged(self) -> None:
        config = ClientOnboardingConfig(
            approval_status="approved",  # test fixture
            client_id="C",
            templates=(
                TaskTemplate(
                    key="orphan",
                    category=TaskCategory.HR_RECORD,
                    title="T",
                    title_ar="ت",
                    owner=Role.SYSTEM,
                    offset_days=-1,
                ),
            ),
        )
        issues = config.owner_issues()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].template_key, "orphan")
        self.assertEqual(issues[0].configured_owner, "system")

    def test_no_default_owner_is_ever_substituted(self) -> None:
        config = ClientOnboardingConfig(
            approval_status="approved",  # test fixture
            client_id="C",
            templates=(
                TaskTemplate(
                    key="orphan",
                    category=TaskCategory.HR_RECORD,
                    title="T",
                    title_ar="ت",
                    owner=Role.SYSTEM,
                    offset_days=-1,
                ),
            ),
        )
        # The task keeps the configured (invalid) owner; it is reported, not
        # silently reassigned to HR Ops.
        self.assertEqual(config.templates[0].owner, Role.SYSTEM)

    def test_shipped_config_has_no_owner_issues(self) -> None:
        self.assertEqual(build_registry().get(CLIENT_ALPHA).owner_issues(), [])


# ---------------------------------------------------------------------------
# UC02-BR-06 — working calendar
# ---------------------------------------------------------------------------


class CalendarAuthorityTests(unittest.TestCase):
    def test_builtin_holiday_data_is_not_authoritative(self) -> None:
        status = calendar_status()
        self.assertFalse(status.approved)
        self.assertIn("provisional", " ".join(status.warnings))
        self.assertIn("not Legal/HR approved", status.source)

    def test_approved_data_can_be_supplied(self) -> None:
        approved = frozenset({date(2026, 12, 2)})
        status = calendar_status(approved, approved=True, source="Legal sign-off 2026")
        self.assertTrue(status.approved)
        self.assertEqual(status.warnings, [])

    def test_service_reports_its_calendar_status(self) -> None:
        self.assertFalse(OnboardingService(build_registry()).calendar_status().approved)

    def test_joining_on_a_weekend_moves_same_day_tasks_to_a_business_day(self) -> None:
        saturday = date(2026, 11, 14)
        self.assertFalse(is_business_day(saturday))
        self.assertEqual(add_business_days(saturday, 0), date(2026, 11, 16))

    def test_joining_on_a_public_holiday_moves_same_day_tasks(self) -> None:
        national_day = date(2026, 12, 2)
        self.assertFalse(is_business_day(national_day))
        self.assertEqual(add_business_days(national_day, 0), date(2026, 12, 4))

    def test_plan_for_a_weekend_joining_date_lands_on_business_days(self) -> None:
        service = OnboardingService(build_registry())
        record = service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-008"), joining_date=date(2026, 11, 14))
        )
        for task in record.tasks:
            with self.subTest(task=task.template_key):
                self.assertTrue(is_business_day(task.due_date))


# ---------------------------------------------------------------------------
# UC02-BR-07 — background verification, visa, medical, biometrics, insurance
# ---------------------------------------------------------------------------


class WorkAuthorisationTests(ExtendedPlanTestCase):
    def test_all_named_milestone_activities_are_tracked(self) -> None:
        categories = {t.category for t in self.ext_record().tasks}
        for expected in (
            TaskCategory.BACKGROUND_VERIFICATION,
            TaskCategory.VISA,
            TaskCategory.MEDICAL,
            TaskCategory.BIOMETRICS,
            TaskCategory.INSURANCE,
        ):
            with self.subTest(category=expected):
                self.assertIn(expected, categories)

    def test_activities_are_absent_when_not_applicable(self) -> None:
        # EMP-008 is a local hire: no visa, no BV, no insurance flags.
        local = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-008"))
        )
        categories = {t.category for t in local.tasks}
        for absent in (
            TaskCategory.VISA,
            TaskCategory.BACKGROUND_VERIFICATION,
            TaskCategory.MEDICAL,
            TaskCategory.BIOMETRICS,
            TaskCategory.INSURANCE,
        ):
            with self.subTest(category=absent):
                self.assertNotIn(absent, categories)

    def test_visa_processing_waits_for_the_pack_handover_to_the_pro(self) -> None:
        self.assertEqual(self.ext_task("visa_processing").status, TaskStatus.BLOCKED)
        self.assertEqual(self.ext_task("visa_processing").owner, Role.PRO)
        self.assertIn(
            f"{self.ext_id}#visa_pack_handover",
            self.ext_task("visa_processing").depends_on,
        )

    def test_visa_status_is_tracked_after_handover(self) -> None:
        self.ext_complete(HR_OPS, "hr_record_create")
        self.ext_complete(HR_OPS, "collect_documents")
        self.ext_complete(HR_OPS, "visa_pack_handover", "PRO-RECEIPT-001")

        self.assertEqual(self.ext_task("visa_processing").status, TaskStatus.READY)
        self.assertEqual(
            self.ext_task("visa_pack_handover").evidence_ref, "PRO-RECEIPT-001"
        )
        # PRO then progresses it; status updates are tracked on the task.
        from onboarding_task_management.contracts import Actor

        pro = Actor(actor_id="U-PRO-1", role=Role.PRO, client_id=CLIENT_ALPHA)
        self.service.start_task(pro, self.ext_id, f"{self.ext_id}#visa_processing")
        self.assertEqual(
            self.ext_task("visa_processing").status, TaskStatus.IN_PROGRESS
        )

    def test_visa_does_not_assume_a_background_verification_dependency(self) -> None:
        # Business Framework OQ-02 is an OPEN QUESTION owned by the Process
        # SME. UC-02 must not assume the dependency in either direction.
        self.assertNotIn(
            f"{self.ext_id}#background_verification",
            self.ext_task("visa_processing").depends_on,
        )

    def test_background_verification_pending_blocks_its_milestone(self) -> None:
        states = {
            m.template_key: m
            for m in self.service.milestones(HR_OPS, self.ext_id, AS_OF)
        }
        self.assertIn("verification_cleared", states)
        self.assertNotEqual(
            states["verification_cleared"].status.value, "achieved"
        )
        # NOTE: a *failed* background verification has no distinct status.
        # The outcome taxonomy for a failed check is an unresolved HR Ops
        # decision; only "pending / not complete" is asserted here.

    def test_family_insurance_only_when_applicable(self) -> None:
        self.assertIsNotNone(self.ext_record().task_by_key("family_insurance_enrolment"))
        local = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-008"))
        )
        self.assertIsNone(local.task_by_key("family_insurance_enrolment"))

    def test_family_insurance_follows_employee_enrolment(self) -> None:
        self.assertIn(
            f"{self.ext_id}#insurance_enrolment",
            self.ext_task("family_insurance_enrolment").depends_on,
        )


# ---------------------------------------------------------------------------
# UC02-BR-08 — travel, accommodation, food, banking letters
# ---------------------------------------------------------------------------


class TravelAndBankingTests(ExtendedPlanTestCase):
    def test_travel_accommodation_and_food_are_tracked_when_applicable(self) -> None:
        categories = {t.category for t in self.ext_record().tasks}
        for expected in (
            TaskCategory.TRAVEL,
            TaskCategory.ACCOMMODATION,
            TaskCategory.FOOD,
        ):
            with self.subTest(category=expected):
                self.assertIn(expected, categories)

    def test_not_every_employee_gets_travel_or_accommodation(self) -> None:
        local = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-008"))
        )
        categories = {t.category for t in local.tasks}
        self.assertNotIn(TaskCategory.TRAVEL, categories)
        self.assertNotIn(TaskCategory.ACCOMMODATION, categories)
        self.assertNotIn(TaskCategory.FOOD, categories)

    def test_accommodation_not_ready_is_overdue_and_raises_joining_risk(self) -> None:
        # accommodation_booking is due 2026-11-06; evaluate after that date.
        late = date(2026, 11, 10)
        outstanding = {
            a.template_key: a
            for a in self.service.outstanding_activities(HR_OPS, self.ext_id, late)
        }
        self.assertIn("accommodation_booking", outstanding)
        activity = outstanding["accommodation_booking"]
        self.assertTrue(activity.is_overdue)
        self.assertEqual(activity.owner, Role.FACILITIES)
        self.assertIn("OVERDUE", activity.required_follow_up)

        risk = self.service.joining_risk(HR_OPS, self.ext_id, late)
        self.assertTrue(risk.at_risk)
        self.assertIn(f"{self.ext_id}#accommodation_booking", risk.overdue_task_ids)

    def test_accommodation_not_ready_puts_its_milestone_at_risk(self) -> None:
        late = date(2026, 11, 10)
        states = {
            m.template_key: m
            for m in self.service.milestones(HR_OPS, self.ext_id, late)
        }
        self.assertIn("relocation_ready", states)
        self.assertIn(
            f"{self.ext_id}#accommodation_booking",
            states["relocation_ready"].blocking_task_ids,
        )

    def test_banking_letters_are_tracked_when_required(self) -> None:
        keys = {t.template_key for t in self.ext_record().tasks}
        self.assertIn("salary_transfer_letter", keys)
        self.assertIn("non_liability_letter", keys)

    def test_banking_letters_absent_when_not_required(self) -> None:
        local = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-008"))
        )
        keys = {t.template_key for t in local.tasks}
        self.assertNotIn("salary_transfer_letter", keys)
        self.assertNotIn("non_liability_letter", keys)

    def test_uc02_never_contacts_a_bank(self) -> None:
        # Banking letters are issued by an HR role; there is no bank
        # integration, transport or transaction anywhere in UC-02.
        self.assertEqual(
            self.ext_task("salary_transfer_letter").owner, Role.HR_OFFICER
        )
        import pathlib

        import onboarding_task_management.sub_agent.service as svc

        source = pathlib.Path(svc.__file__).read_text(encoding="utf-8").lower()
        for forbidden in ("bank_api", "swift", "iban_transfer", "payment"):
            self.assertNotIn(forbidden, source)


# ---------------------------------------------------------------------------
# HR-system activation
# ---------------------------------------------------------------------------


class HrSystemActivationTests(ExtendedPlanTestCase):
    def test_activation_is_blocked_until_the_hr_record_exists(self) -> None:
        task = self.ext_task("hr_system_activation")
        self.assertEqual(task.status, TaskStatus.BLOCKED)
        self.assertIn(f"{self.ext_id}#hr_record_create", task.depends_on)
        with self.assertRaises(PrerequisiteNotMetError):
            self.ext_complete(HR_OPS, "hr_system_activation")

    def test_activation_is_scheduled_on_the_joining_date(self) -> None:
        self.assertEqual(self.ext_task("hr_system_activation").due_date, JOINING)

    def test_activation_requires_confirmation_evidence(self) -> None:
        self.ext_complete(HR_OPS, "hr_record_create")
        with self.assertRaises(EvidenceRequiredError):
            self.ext_complete(HR_OPS, "hr_system_activation", None)
        self.ext_complete(HR_OPS, "hr_system_activation", "ZOHO-ACT-001")
        self.assertEqual(
            self.ext_task("hr_system_activation").evidence_ref, "ZOHO-ACT-001"
        )

    def test_no_live_hr_system_integration_exists(self) -> None:
        import pathlib

        import onboarding_task_management.sub_agent.service as svc

        source = pathlib.Path(svc.__file__).read_text(encoding="utf-8").lower()
        for forbidden in ("zoho", "requests.", "httpx", "api_key"):
            self.assertNotIn(forbidden, source)


# ---------------------------------------------------------------------------
# UC02-BR-12 — completion evidence
# ---------------------------------------------------------------------------


class CompletionEvidenceTests(ExtendedPlanTestCase):
    def test_evidence_required_task_cannot_complete_without_evidence(self) -> None:
        self.ext_complete(HR_OPS, "hr_record_create")
        self.ext_complete(HR_OPS, "collect_documents")
        with self.assertRaises(EvidenceRequiredError) as ctx:
            self.ext_complete(HR_OPS, "visa_pack_handover", None)
        self.assertIn("evidence", str(ctx.exception))

    def test_blank_evidence_is_refused(self) -> None:
        self.ext_complete(HR_OPS, "hr_record_create")
        with self.assertRaises(EvidenceRequiredError):
            self.ext_complete(HR_OPS, "salary_transfer_letter", "   ")

    def test_task_stays_open_when_evidence_is_missing(self) -> None:
        self.ext_complete(HR_OPS, "hr_record_create")
        with self.assertRaises(EvidenceRequiredError):
            self.ext_complete(HR_OPS, "salary_transfer_letter", None)
        self.assertEqual(
            self.ext_task("salary_transfer_letter").status, TaskStatus.READY
        )

    def test_evidence_is_recorded_on_completion(self) -> None:
        self.ext_complete(HR_OPS, "hr_record_create")
        self.ext_complete(HR_OPS, "salary_transfer_letter", "LTR-001")
        task = self.ext_task("salary_transfer_letter")
        self.assertEqual(task.evidence_ref, "LTR-001")
        self.assertIsNotNone(task.evidence_recorded_at)

    def test_tasks_without_the_requirement_complete_as_before(self) -> None:
        # Regression: the baseline plan has no evidence requirements.
        self.complete(HR_OPS, "hr_record_create")
        self.assertEqual(self.task("hr_record_create").status, TaskStatus.COMPLETE)

    def test_outstanding_report_flags_the_evidence_requirement(self) -> None:
        activities = {
            a.template_key: a
            for a in self.service.outstanding_activities(HR_OPS, self.ext_id, AS_OF)
        }
        self.assertIn(
            "evidence required", activities["medical_test"].required_follow_up
        )


# ---------------------------------------------------------------------------
# UC02-BR-11 — explicit approvers
# ---------------------------------------------------------------------------


class ExplicitApproverTests(ExtendedPlanTestCase):
    def test_spend_related_actions_name_the_hr_manager(self) -> None:
        for key in ("travel_booking", "accommodation_booking", "insurance_enrolment"):
            with self.subTest(task=key):
                self.assertEqual(self.ext_task(key).approval_role, Role.HR_MANAGER)

    def test_background_verification_names_the_hr_officer(self) -> None:
        self.assertEqual(
            self.ext_task("background_verification").approval_role, Role.HR_OFFICER
        )

    def test_visa_actions_name_hr_operations(self) -> None:
        self.assertEqual(self.ext_task("visa_processing").approval_role, Role.HR_OPS)

    def test_not_every_action_carries_an_approver(self) -> None:
        # Only actions with a documented approver have one; none was invented.
        self.assertIsNone(self.task("hr_record_create").approval_role)


# ---------------------------------------------------------------------------
# UC02-BR-09 — approval-controlled re-planning
# ---------------------------------------------------------------------------


class ApprovalControlledReplanTests(ExtendedPlanTestCase):
    def test_joining_date_change_requires_approval(self) -> None:
        from onboarding_task_management.errors import ApprovalRequiredError

        request = self.service.request_change(
            HR_OPS, self.ext_id, joining_date=date(2026, 11, 30)
        )
        with self.assertRaises(ApprovalRequiredError):
            self.service.apply_change(HR_OPS, request.request_id)
        self.assertEqual(self.ext_record().joining_date, JOINING)

    def test_rejected_approval_cannot_authorise_replanning(self) -> None:
        from onboarding_task_management.errors import ApprovalRequiredError

        request = self.service.request_change(
            HR_OPS, self.ext_id, joining_date=date(2026, 11, 30)
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.REJECTED
        )
        with self.assertRaises(ApprovalRequiredError):
            self.service.apply_change(HR_OPS, request.request_id)
        self.assertEqual(self.ext_record().revision, 0)

    def test_stale_approval_cannot_authorise_replanning(self) -> None:
        from onboarding_task_management.errors import InvalidStateError

        change = self.service.request_change(
            HR_OPS, self.ext_id, joining_date=date(2026, 11, 30)
        )
        cancel = self.service.request_cancellation(HR_OPS, self.ext_id, "withdrew")
        self.service.decide_change(
            HR_OPS_APPROVER, cancel.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_cancellation(HR_OPS, cancel.request_id)
        self.service.decide_change(
            HR_OPS_APPROVER, change.request_id, DecisionOutcome.APPROVED
        )
        with self.assertRaises(InvalidStateError):
            self.service.apply_change(HR_OPS, change.request_id)

    def test_approved_date_change_replans_extended_activities(self) -> None:
        before = self.ext_task("visa_processing").due_date
        request = self.service.request_change(
            HR_OPS, self.ext_id, joining_date=date(2026, 11, 30)
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_change(HR_OPS, request.request_id)
        self.assertNotEqual(self.ext_task("visa_processing").due_date, before)

    def test_location_change_replans_location_sensitive_activities(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.ext_id, location="Abu Dhabi"
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        outcome = self.service.apply_change(HR_OPS, request.request_id)
        self.assertEqual(self.ext_record().location, "Abu Dhabi")
        self.assertTrue(outcome.rescheduled_task_ids or outcome.retained_task_ids)


# ---------------------------------------------------------------------------
# Cohort and owner-availability scenarios
# ---------------------------------------------------------------------------


class CohortTests(unittest.TestCase):
    def test_cohort_members_each_get_an_isolated_plan(self) -> None:
        service = OnboardingService(build_registry())
        records = [
            service.create_onboarding(HR_OPS, make_trigger(persona(eid)))
            for eid in COHORT_EMPLOYEE_IDS
        ]
        self.assertEqual(len(records), 3)
        ids = {r.onboarding_id for r in records}
        self.assertEqual(len(ids), 3)
        for record in records:
            with self.subTest(employee=record.employee_id):
                self.assertEqual(record.joining_date, JOINING)
                self.assertEqual(len(record.tasks), 10)

    def test_cohort_task_ids_do_not_collide(self) -> None:
        service = OnboardingService(build_registry())
        all_ids: list[str] = []
        for eid in COHORT_EMPLOYEE_IDS:
            record = service.create_onboarding(HR_OPS, make_trigger(persona(eid)))
            all_ids += [t.task_id for t in record.tasks]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_completing_one_cohort_member_does_not_affect_another(self) -> None:
        service = OnboardingService(build_registry())
        first = service.create_onboarding(
            HR_OPS, make_trigger(persona(COHORT_EMPLOYEE_IDS[0]))
        )
        second = service.create_onboarding(
            HR_OPS, make_trigger(persona(COHORT_EMPLOYEE_IDS[1]))
        )
        service.complete_task(
            HR_OPS, first.onboarding_id, f"{first.onboarding_id}#hr_record_create"
        )
        refreshed = service.get(HR_OPS, second.onboarding_id)
        self.assertEqual(
            refreshed.task_by_key("hr_record_create").status, TaskStatus.READY
        )


class OwnerAvailabilityTests(ServiceTestCase):
    def test_an_admin_role_can_act_when_the_owner_is_unavailable(self) -> None:
        # UC-02 has no leave calendar. What IS supported is that an admin role
        # may act on any task, which is the documented cover path. Leave-aware
        # re-assignment needs an HR Ops decision and is not implemented.
        self.complete(HR_OPS, "hr_record_create")
        self.complete(HR_LEAD, "it_laptop")
        self.assertEqual(self.task("it_laptop").status, TaskStatus.COMPLETE)

    def test_a_non_owner_non_admin_still_cannot_act(self) -> None:
        from onboarding_task_management.errors import UnauthorizedActionError

        self.complete(HR_OPS, "hr_record_create")
        with self.assertRaises(UnauthorizedActionError):
            self.complete(FACILITIES, "it_laptop")

    def test_leave_status_is_not_modelled(self) -> None:
        from onboarding_task_management.contracts import TaskRecord

        self.assertNotIn("owner_on_leave", TaskRecord.model_fields)


# ---------------------------------------------------------------------------
# UC-02 / UC-06 responsibility boundary
# ---------------------------------------------------------------------------


class ResponsibilityBoundaryTests(ExtendedPlanTestCase):
    def test_handoff_carries_planning_and_risk_information(self) -> None:
        handoff = self.service.joining_handoff(HR_OPS, self.ext_id, AS_OF)
        self.assertEqual(handoff.onboarding_id, self.ext_id)
        self.assertTrue(handoff.outstanding)
        self.assertTrue(handoff.milestones)
        self.assertIsNotNone(handoff.risk)
        self.assertIsNotNone(handoff.calendar)

    def test_uc02_never_closes_the_case(self) -> None:
        handoff = self.service.joining_handoff(HR_OPS, self.ext_id, AS_OF)
        self.assertFalse(handoff.case_closed_by_uc02)
        self.assertEqual(self.ext_record().status, OnboardingStatus.ACTIVE)

    def test_handoff_states_the_boundary(self) -> None:
        note = self.service.joining_handoff(HR_OPS, self.ext_id, AS_OF).handoff_note
        self.assertIn("UC-06", note)
        self.assertIn("case closure", note)

    def test_uc02_retains_planning_time_risk(self) -> None:
        # The boundary does not remove UC-02's planning-time risk calculation.
        risk = self.service.joining_risk(HR_OPS, self.ext_id, date(2026, 11, 13))
        self.assertIsNotNone(risk)

    def test_uc02_does_not_deliver_reminders(self) -> None:
        drafts = self.service.reminder_drafts(HR_OPS, self.ext_id, date(2026, 11, 10))
        for draft in drafts:
            with self.subTest(draft=draft.draft_id):
                self.assertEqual(draft.delivery_status, "draft_not_sent")

    def test_outstanding_activities_name_owner_and_follow_up(self) -> None:
        activities = self.service.outstanding_activities(HR_OPS, self.ext_id, AS_OF)
        self.assertTrue(activities)
        for activity in activities:
            with self.subTest(task=activity.template_key):
                self.assertIsInstance(activity.owner, Role)
                self.assertTrue(activity.required_follow_up)


# ---------------------------------------------------------------------------
# UC02-BR-13 / BR-14 for the new features
# ---------------------------------------------------------------------------


class IsolationAndAuditForNewFeaturesTests(ExtendedPlanTestCase):
    def test_another_client_cannot_read_outstanding_activities(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.outstanding_activities(HR_OPS_BETA, self.ext_id, AS_OF)

    def test_another_client_cannot_request_the_handoff(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.joining_handoff(HR_OPS_BETA, self.ext_id, AS_OF)

    def test_another_client_cannot_inspect_owner_issues(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.owner_issues(HR_OPS_BETA, CLIENT_ALPHA)

    def test_handoff_is_audited(self) -> None:
        self.service.joining_handoff(HR_OPS, self.ext_id, AS_OF)
        self.assertIn("handoff.uc06", self.service.audit.actions_for(HR_OPS))

    def test_evidence_completion_is_audited_without_leaking_the_reference(self) -> None:
        self.ext_complete(HR_OPS, "hr_record_create")
        self.ext_complete(HR_OPS, "salary_transfer_letter", "LTR-SECRET-001")
        events = [
            e
            for e in self.service.audit.events_for(HR_OPS)
            if e.action == "task.complete"
        ]
        self.assertEqual(events[-1].detail["evidence"], "True")
        self.assertNotIn("LTR-SECRET-001", str(events[-1].detail))

    def test_new_activities_carry_no_personal_data(self) -> None:
        for task in self.ext_record().tasks:
            with self.subTest(task=task.template_key):
                fields = set(type(task).model_fields)
                self.assertFalse(
                    fields & {"name", "email", "nationality", "passport"}
                )

    def test_extended_activities_are_bilingual(self) -> None:
        for task in self.ext_record().tasks:
            with self.subTest(task=task.template_key):
                self.assertTrue(
                    any("؀" <= c <= "ۿ" for c in task.title_ar)
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
