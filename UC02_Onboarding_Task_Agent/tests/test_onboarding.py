"""Normal onboarding flow, preconditions, missing configuration, duplicates."""

from __future__ import annotations

import unittest
from datetime import date

from onboarding_task_management.contracts import (
    Actor,
    OfferAcceptance,
    OnboardingForm,
    OnboardingStatus,
    Role,
    TaskCategory,
    TaskStatus,
)
from onboarding_task_management.errors import (
    DuplicateOnboardingError,
    MissingConfigurationError,
    PreconditionError,
)
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.service import (
    OnboardingService,
    OnboardingTrigger,
)
from onboarding_task_management.synthetic import (
    CLIENT_ALPHA,
    CLIENT_EMPTY,
    DEFAULT_JOINING_DATE,
    LOCATION_REMOTE,
    accepted_offer,
    approved_form,
    build_registry,
    make_trigger,
    persona,
)
from tests.support import HR_OPS, ServiceTestCase


class CreateOnboardingTests(ServiceTestCase):
    def test_record_is_created_with_deterministic_ids(self) -> None:
        self.assertEqual(self.onboarding_id, f"ONB-{CLIENT_ALPHA}-EMP-001")
        self.assertEqual(self.record.status, OnboardingStatus.ACTIVE)
        self.assertEqual(self.record.revision, 0)
        self.assertEqual(
            self.task("hr_record_create").task_id,
            f"ONB-{CLIENT_ALPHA}-EMP-001#hr_record_create",
        )

    def test_tasks_come_only_from_client_configuration(self) -> None:
        # Dubai + apollo: the Zephyr access task and the remote kit task are
        # filtered out, everything else applies.
        self.assertEqual(
            self.template_keys(),
            {
                "hr_record_create",
                "collect_documents",
                "visa_application",
                "it_laptop",
                "it_accounts",
                "access_apollo",
                "desk_allocation",
                "payroll_setup",
                "orientation_schedule",
                "policy_ack",
            },
        )

    def test_tasks_carry_owner_status_due_date_and_dependencies(self) -> None:
        task = self.task("collect_documents")
        self.assertEqual(task.owner, Role.HR_OPS)
        self.assertEqual(task.category, TaskCategory.DOCUMENT_COLLECTION)
        self.assertEqual(task.status, TaskStatus.BLOCKED)
        self.assertEqual(task.due_date, date(2026, 11, 4))
        self.assertEqual(task.depends_on, [self.task_id("hr_record_create")])

    def test_due_dates_are_business_days_before_joining(self) -> None:
        self.assertEqual(self.task("hr_record_create").due_date, date(2026, 11, 2))
        self.assertEqual(self.task("it_laptop").due_date, date(2026, 11, 9))
        self.assertEqual(self.task("orientation_schedule").due_date, DEFAULT_JOINING_DATE)

    def test_root_task_is_ready_and_dependents_are_blocked(self) -> None:
        self.assertEqual(self.task("hr_record_create").status, TaskStatus.READY)
        self.assertEqual(self.task("collect_documents").status, TaskStatus.BLOCKED)

    def test_bilingual_titles_are_populated(self) -> None:
        for task in self.record.tasks:
            self.assertTrue(task.title.strip(), msg=task.task_id)
            self.assertTrue(task.title_ar.strip(), msg=task.task_id)

    def test_generation_is_deterministic_across_services(self) -> None:
        other = OnboardingService(build_registry())
        again = other.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        self.assertEqual(
            [(t.task_id, t.due_date, t.status) for t in again.tasks],
            [(t.task_id, t.due_date, t.status) for t in self.record.tasks],
        )

    def test_happy_path_completes_every_task(self) -> None:
        from tests.support import EMPLOYEE, FACILITIES, FINANCE, IT_MANAGER

        owners = {
            Role.HR_OPS: HR_OPS,
            Role.IT_MANAGER: IT_MANAGER,
            Role.FACILITIES: FACILITIES,
            Role.FINANCE: FINANCE,
            Role.EMPLOYEE: EMPLOYEE,
        }
        guard = 0
        while any(t.status is not TaskStatus.COMPLETE for t in self.record.tasks):
            guard += 1
            self.assertLess(guard, 50, "task graph did not drain")
            for task in self.service.ready_tasks(HR_OPS, self.onboarding_id):
                self.service.complete_task(
                    owners[task.owner], self.onboarding_id, task.task_id
                )

        summary = self.service.status(HR_OPS, self.onboarding_id, date(2026, 11, 16))
        self.assertEqual(summary.complete, summary.total_tasks)
        self.assertEqual(summary.overdue_task_ids, [])
        self.assertTrue(summary.is_on_track)

    def test_remote_persona_gets_location_specific_tasks(self) -> None:
        record = self.service.create_onboarding(HR_OPS, make_trigger(persona("EMP-003")))
        keys = {t.template_key for t in record.tasks}
        self.assertIn("remote_kit_shipping", keys)
        self.assertNotIn("visa_application", keys)
        self.assertNotIn("desk_allocation", keys)
        self.assertEqual(record.location, LOCATION_REMOTE)


class PreconditionTests(ServiceTestCase):
    def _trigger(self, **overrides) -> OnboardingTrigger:
        return make_trigger(persona("EMP-002"), **overrides)

    def test_offer_not_accepted_is_rejected(self) -> None:
        offer = OfferAcceptance(employee_id="EMP-002", accepted=False)
        with self.assertRaises(PreconditionError) as ctx:
            self.service.create_onboarding(HR_OPS, self._trigger(offer=offer))
        self.assertIn("offer has not been accepted", str(ctx.exception))

    def test_accepted_offer_without_timestamp_is_rejected(self) -> None:
        offer = OfferAcceptance(employee_id="EMP-002", accepted=True, accepted_at=None)
        with self.assertRaises(PreconditionError):
            self.service.create_onboarding(HR_OPS, self._trigger(offer=offer))

    def test_unsubmitted_form_is_rejected(self) -> None:
        form = OnboardingForm(employee_id="EMP-002", submitted=False)
        with self.assertRaises(PreconditionError) as ctx:
            self.service.create_onboarding(HR_OPS, self._trigger(form=form))
        self.assertIn("not been submitted", str(ctx.exception))

    def test_unapproved_form_is_rejected(self) -> None:
        form = OnboardingForm(employee_id="EMP-002", submitted=True, approved=False)
        with self.assertRaises(PreconditionError) as ctx:
            self.service.create_onboarding(HR_OPS, self._trigger(form=form))
        self.assertIn("not been approved", str(ctx.exception))

    def test_incomplete_form_is_rejected(self) -> None:
        form = OnboardingForm(
            employee_id="EMP-002",
            submitted=True,
            approved=True,
            approved_by_role=Role.HR_OPS,
            missing_fields=["bank_details"],
        )
        with self.assertRaises(PreconditionError) as ctx:
            self.service.create_onboarding(HR_OPS, self._trigger(form=form))
        self.assertIn("incomplete", str(ctx.exception))

    def test_form_approved_by_non_hr_role_is_rejected(self) -> None:
        form = OnboardingForm(
            employee_id="EMP-002",
            submitted=True,
            approved=True,
            approved_by_role=Role.EMPLOYEE,
        )
        with self.assertRaises(PreconditionError) as ctx:
            self.service.create_onboarding(HR_OPS, self._trigger(form=form))
        self.assertIn("not approved by an HR role", str(ctx.exception))

    def test_mismatched_employee_on_offer_is_rejected(self) -> None:
        with self.assertRaises(PreconditionError) as ctx:
            self.service.create_onboarding(
                HR_OPS, self._trigger(offer=accepted_offer("EMP-999"))
            )
        self.assertIn("different employee", str(ctx.exception))

    def test_rejected_precondition_is_audited_and_creates_nothing(self) -> None:
        offer = OfferAcceptance(employee_id="EMP-002", accepted=False)
        with self.assertRaises(PreconditionError):
            self.service.create_onboarding(HR_OPS, self._trigger(offer=offer))
        self.assertIn("rejected_precondition", [
            e.outcome for e in self.service.audit.events_for(HR_OPS)
        ])


class MissingConfigurationTests(unittest.TestCase):
    def test_unregistered_client_has_no_configuration(self) -> None:
        service = OnboardingService(build_registry())
        actor = Actor(actor_id="U-X", role=Role.HR_OPS, client_id="CLIENT-UNKNOWN")
        trigger = OnboardingTrigger(
            client_id="CLIENT-UNKNOWN",
            employee_id="EMP-900",
            joining_date=DEFAULT_JOINING_DATE,
            role_id="backend_engineer",
            department="technology",
            manager_id="MGR-X",
            location="Dubai",
            project="apollo",
            offer=accepted_offer("EMP-900"),
            form=approved_form("EMP-900"),
        )
        with self.assertRaises(MissingConfigurationError) as ctx:
            service.create_onboarding(actor, trigger)
        self.assertIn("no onboarding configuration", str(ctx.exception))

    def test_registered_client_with_no_templates_is_missing_configuration(self) -> None:
        service = OnboardingService(build_registry())
        actor = Actor(actor_id="U-E", role=Role.HR_OPS, client_id=CLIENT_EMPTY)
        trigger = OnboardingTrigger(
            client_id=CLIENT_EMPTY,
            employee_id="EMP-901",
            joining_date=DEFAULT_JOINING_DATE,
            role_id="backend_engineer",
            department="technology",
            manager_id="MGR-E",
            location="Dubai",
            project="apollo",
            offer=accepted_offer("EMP-901"),
            form=approved_form("EMP-901"),
        )
        with self.assertRaises(MissingConfigurationError) as ctx:
            service.create_onboarding(actor, trigger)
        self.assertIn("no task templates", str(ctx.exception))

    def test_configuration_matching_no_tasks_is_rejected(self) -> None:
        registry = build_registry()
        registry.register(
            ClientOnboardingConfig(
                approval_status="approved",  # test fixture
                client_id="CLIENT-NARROW",
                templates=(
                    TaskTemplate(
                        key="site_only",
                        category=TaskCategory.WORKPLACE,
                        title="Site induction",
                        title_ar="تعريف بالموقع",
                        owner=Role.FACILITIES,
                        offset_days=-1,
                        locations=("Sharjah",),
                    ),
                ),
            )
        )
        service = OnboardingService(registry)
        actor = Actor(actor_id="U-N", role=Role.HR_OPS, client_id="CLIENT-NARROW")
        trigger = OnboardingTrigger(
            client_id="CLIENT-NARROW",
            employee_id="EMP-902",
            joining_date=DEFAULT_JOINING_DATE,
            role_id="backend_engineer",
            department="technology",
            manager_id="MGR-N",
            location="Dubai",
            project="apollo",
            offer=accepted_offer("EMP-902"),
            form=approved_form("EMP-902"),
        )
        with self.assertRaises(MissingConfigurationError) as ctx:
            service.create_onboarding(actor, trigger)
        self.assertIn("no applicable tasks", str(ctx.exception))

    def test_duplicate_template_key_is_rejected(self) -> None:
        template = dict(
            category=TaskCategory.HR_RECORD,
            title="t",
            title_ar="ت",
            owner=Role.HR_OPS,
            offset_days=-1,
        )
        with self.assertRaises(ValueError) as ctx:
            ClientOnboardingConfig(
                approval_status="approved",  # test fixture
                client_id="C",
                templates=(
                    TaskTemplate(key="dup", **template),
                    TaskTemplate(key="dup", **template),
                ),
            )
        # Pin the reason: templates now also validate title_ar, so a bare
        # assertRaises(ValueError) could pass without reaching this check.
        self.assertIn("duplicate template key", str(ctx.exception))

    def test_unknown_dependency_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ClientOnboardingConfig(
                approval_status="approved",  # test fixture
                client_id="C",
                templates=(
                    TaskTemplate(
                        key="a",
                        category=TaskCategory.HR_RECORD,
                        title="t",
                        title_ar="ت",
                        owner=Role.HR_OPS,
                        offset_days=-1,
                        depends_on=("missing",),
                    ),
                ),
            )
        self.assertIn("unknown key", str(ctx.exception))


class DuplicateOnboardingTests(ServiceTestCase):
    def test_second_create_for_same_employee_is_rejected(self) -> None:
        with self.assertRaises(DuplicateOnboardingError) as ctx:
            self.service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        self.assertIn(self.onboarding_id, str(ctx.exception))

    def test_duplicate_does_not_alter_the_existing_record(self) -> None:
        before = [(t.task_id, t.status) for t in self.record.tasks]
        with self.assertRaises(DuplicateOnboardingError):
            self.service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        after = [(t.task_id, t.status) for t in self.record.tasks]
        self.assertEqual(before, after)
        self.assertEqual(self.record.revision, 0)

    def test_duplicate_is_audited(self) -> None:
        with self.assertRaises(DuplicateOnboardingError):
            self.service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        outcomes = [e.outcome for e in self.service.audit.events_for(HR_OPS)]
        self.assertIn("rejected_duplicate", outcomes)

    def test_same_employee_id_under_a_different_client_is_not_a_duplicate(self) -> None:
        from tests.support import HR_OPS_BETA

        record = self.service.create_onboarding(
            HR_OPS_BETA, make_trigger(persona("EMP-006"))
        )
        self.assertNotEqual(record.onboarding_id, self.onboarding_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
