"""Re-planning on joining-date, location and project changes."""

from __future__ import annotations

import unittest
from datetime import date

from onboarding_task_management.contracts import (
    ChangeDriver,
    DecisionOutcome,
    GateName,
    TaskStatus,
)
from onboarding_task_management.errors import InvalidStateError
from onboarding_task_management.synthetic import (
    LOCATION_REMOTE,
    PROJECT_ZEPHYR,
)
from tests.support import (
    HR_OPS,
    HR_OPS_APPROVER,
    IT_MANAGER,
    MANAGER,
    ServiceTestCase,
)


class ReplanTestCase(ServiceTestCase):
    """Helper: push a change through its gate and apply it."""

    def change(self, approver, **kwargs):
        request = self.service.request_change(HR_OPS, self.onboarding_id, **kwargs)
        self.service.decide_change(approver, request.request_id, DecisionOutcome.APPROVED)
        return self.service.apply_change(HR_OPS, request.request_id)


class JoiningDateChangeTests(ReplanTestCase):
    NEW_JOINING = date(2026, 11, 30)

    def test_date_change_reschedules_open_tasks(self) -> None:
        outcome = self.change(HR_OPS_APPROVER, joining_date=self.NEW_JOINING)

        self.assertEqual(outcome.drivers, [ChangeDriver.JOINING_DATE])
        self.assertEqual(self.record.joining_date, self.NEW_JOINING)
        # Ten business days before Mon 2026-11-30 is Mon 2026-11-16.
        self.assertEqual(self.task("hr_record_create").due_date, date(2026, 11, 16))
        self.assertIn(self.task_id("hr_record_create"), outcome.rescheduled_task_ids)

    def test_date_change_bumps_the_revision(self) -> None:
        self.assertEqual(self.record.revision, 0)
        outcome = self.change(HR_OPS_APPROVER, joining_date=self.NEW_JOINING)
        self.assertEqual(self.record.revision, 1)
        self.assertEqual(outcome.revision, 1)

    def test_completed_tasks_are_not_rescheduled_or_reopened(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        original_due = self.task("hr_record_create").due_date

        outcome = self.change(HR_OPS_APPROVER, joining_date=self.NEW_JOINING)

        task = self.task("hr_record_create")
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertEqual(task.due_date, original_due)
        self.assertIn(task.task_id, outcome.retained_task_ids)
        self.assertNotIn(task.task_id, outcome.rescheduled_task_ids)

    def test_date_change_respects_public_holidays(self) -> None:
        # policy_ack is +2 business days after joining; 2026-12-01..03 are holidays.
        self.change(HR_OPS_APPROVER, joining_date=date(2026, 11, 30))
        self.assertEqual(self.task("policy_ack").due_date, date(2026, 12, 7))

    def test_earlier_joining_date_pulls_due_dates_forward(self) -> None:
        self.change(HR_OPS_APPROVER, joining_date=date(2026, 11, 2))
        self.assertEqual(self.task("hr_record_create").due_date, date(2026, 10, 19))

    def test_no_change_requested_is_rejected(self) -> None:
        with self.assertRaises(InvalidStateError):
            self.service.request_change(
                HR_OPS, self.onboarding_id, joining_date=self.record.joining_date
            )

    def test_multiple_attribute_changes_in_one_request_are_rejected(self) -> None:
        with self.assertRaises(InvalidStateError) as ctx:
            self.service.request_change(
                HR_OPS,
                self.onboarding_id,
                joining_date=self.NEW_JOINING,
                location=LOCATION_REMOTE,
            )
        self.assertIn("one attribute change at a time", str(ctx.exception))


class LocationChangeTests(ReplanTestCase):
    def test_location_change_swaps_location_specific_tasks(self) -> None:
        outcome = self.change(HR_OPS_APPROVER, location=LOCATION_REMOTE)

        self.assertEqual(outcome.drivers, [ChangeDriver.LOCATION])
        keys = self.template_keys()
        self.assertNotIn("visa_application", keys)
        self.assertNotIn("desk_allocation", keys)
        self.assertIn("remote_kit_shipping", keys)
        self.assertIn(self.task_id("remote_kit_shipping"), outcome.added_task_ids)
        self.assertIn(self.task_id("desk_allocation"), outcome.removed_task_ids)

    def test_location_insensitive_tasks_are_retained_unchanged(self) -> None:
        before = self.task("collect_documents").due_date
        outcome = self.change(HR_OPS_APPROVER, location=LOCATION_REMOTE)
        self.assertEqual(self.task("collect_documents").due_date, before)
        self.assertIn(self.task_id("collect_documents"), outcome.retained_task_ids)

    def test_completed_location_task_is_kept_for_the_audit_trail(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(HR_OPS, "collect_documents")
        self.complete(HR_OPS, "visa_application")

        outcome = self.change(HR_OPS_APPROVER, location=LOCATION_REMOTE)

        task = self.task("visa_application")
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertIn(task.task_id, outcome.retained_task_ids)
        self.assertNotIn(task.task_id, outcome.removed_task_ids)

    def test_new_task_is_wired_into_the_dependency_graph(self) -> None:
        self.change(HR_OPS_APPROVER, location=LOCATION_REMOTE)
        task = self.task("remote_kit_shipping")
        self.assertEqual(task.depends_on, [self.task_id("hr_record_create")])
        self.assertEqual(task.status, TaskStatus.BLOCKED)

    def test_removed_task_is_dropped_from_surviving_dependencies(self) -> None:
        self.change(HR_OPS_APPROVER, location=LOCATION_REMOTE)
        for task in self.record.tasks:
            for dependency in task.depends_on:
                self.assertIsNotNone(
                    self.record.task_by_id(dependency),
                    msg=f"{task.task_id} depends on missing {dependency}",
                )


class ProjectChangeTests(ReplanTestCase):
    def test_project_change_swaps_access_tasks(self) -> None:
        outcome = self.change(MANAGER, project=PROJECT_ZEPHYR)

        self.assertEqual(outcome.drivers, [ChangeDriver.PROJECT])
        self.assertEqual(self.record.project, PROJECT_ZEPHYR)
        keys = self.template_keys()
        self.assertIn("access_zephyr", keys)
        self.assertNotIn("access_apollo", keys)

    def test_project_change_is_gated_by_the_hiring_manager(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, project=PROJECT_ZEPHYR
        )
        self.assertEqual(request.gate, GateName.PROJECT_CHANGE)
        self.assertEqual(request.approver_role.value, "manager")

    def test_in_progress_task_on_a_removed_branch_is_removed(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(IT_MANAGER, "it_laptop")
        self.service.start_task(
            IT_MANAGER, self.onboarding_id, self.task_id("it_accounts")
        )

        outcome = self.change(MANAGER, project=PROJECT_ZEPHYR)

        self.assertIn(self.task_id("access_apollo"), outcome.removed_task_ids)
        # The in-progress task is unrelated to the project filter and survives.
        self.assertEqual(self.task("it_accounts").status, TaskStatus.IN_PROGRESS)

    def test_unrelated_tasks_keep_their_due_dates(self) -> None:
        before = {t.template_key: t.due_date for t in self.record.tasks}
        self.change(MANAGER, project=PROJECT_ZEPHYR)
        for task in self.record.tasks:
            if task.template_key in before and task.template_key != "access_apollo":
                self.assertEqual(task.due_date, before[task.template_key])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

