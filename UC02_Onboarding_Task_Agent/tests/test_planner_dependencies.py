"""Dependency blocking and parallel execution readiness."""

from __future__ import annotations

import unittest

from onboarding_task_management.contracts import TaskStatus
from onboarding_task_management.errors import PrerequisiteNotMetError
from onboarding_task_management.sub_agent.planner import unmet_prerequisites
from tests.support import FACILITIES, FINANCE, HR_OPS, IT_MANAGER, ServiceTestCase


class BlockedPrerequisiteTests(ServiceTestCase):
    def test_only_the_root_task_is_initially_ready(self) -> None:
        self.assertEqual(self.ready_keys(), {"hr_record_create"})

    def test_dependent_task_cannot_start_before_its_prerequisite(self) -> None:
        with self.assertRaises(PrerequisiteNotMetError) as ctx:
            self.service.start_task(
                HR_OPS, self.onboarding_id, self.task_id("collect_documents")
            )
        self.assertIn("prerequisites", str(ctx.exception))

    def test_dependent_task_cannot_complete_before_its_prerequisite(self) -> None:
        with self.assertRaises(PrerequisiteNotMetError):
            self.complete(IT_MANAGER, "it_accounts")
        self.assertEqual(self.task("it_accounts").status, TaskStatus.BLOCKED)

    def test_transitive_prerequisite_stays_blocked(self) -> None:
        # access_apollo -> it_accounts -> it_laptop -> hr_record_create
        self.complete(HR_OPS, "hr_record_create")
        self.assertEqual(self.task("access_apollo").status, TaskStatus.BLOCKED)
        with self.assertRaises(PrerequisiteNotMetError):
            self.complete(IT_MANAGER, "access_apollo")

    def test_unmet_prerequisites_lists_the_blocking_task(self) -> None:
        task = self.task("collect_documents")
        self.assertEqual(
            unmet_prerequisites(self.record, task), [self.task_id("hr_record_create")]
        )

    def test_completing_a_prerequisite_unblocks_its_dependents(self) -> None:
        self.assertEqual(self.task("collect_documents").status, TaskStatus.BLOCKED)
        self.complete(HR_OPS, "hr_record_create")
        self.assertEqual(self.task("collect_documents").status, TaskStatus.READY)
        self.assertEqual(unmet_prerequisites(self.record, self.task("collect_documents")), [])

    def test_blocked_tasks_are_reported(self) -> None:
        blocked = {t.template_key for t in self.service.blocked_tasks(HR_OPS, self.onboarding_id)}
        self.assertNotIn("hr_record_create", blocked)
        self.assertIn("access_apollo", blocked)


class ParallelExecutionTests(ServiceTestCase):
    def test_independent_tasks_become_ready_together(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.assertEqual(
            self.ready_keys(),
            {"collect_documents", "it_laptop", "desk_allocation", "orientation_schedule"},
        )

    def test_starting_one_task_does_not_block_its_siblings(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.service.start_task(IT_MANAGER, self.onboarding_id, self.task_id("it_laptop"))

        # The sibling is untouched and still startable.
        self.assertEqual(self.task("desk_allocation").status, TaskStatus.READY)
        self.service.start_task(
            FACILITIES, self.onboarding_id, self.task_id("desk_allocation")
        )
        self.assertEqual(self.task("it_laptop").status, TaskStatus.IN_PROGRESS)
        self.assertEqual(self.task("desk_allocation").status, TaskStatus.IN_PROGRESS)

    def test_siblings_complete_in_any_order(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(FACILITIES, "desk_allocation")
        self.complete(IT_MANAGER, "it_laptop")
        self.complete(HR_OPS, "collect_documents")
        self.assertEqual(self.task("it_accounts").status, TaskStatus.READY)
        self.assertEqual(self.task("payroll_setup").status, TaskStatus.READY)

    def test_parallel_batches_group_by_dependency_depth(self) -> None:
        batches = self.service.parallel_batches(HR_OPS, self.onboarding_id)
        self.assertEqual(batches[0], [self.task_id("hr_record_create")])
        self.assertEqual(
            set(batches[1]),
            {
                self.task_id("collect_documents"),
                self.task_id("it_laptop"),
                self.task_id("desk_allocation"),
                self.task_id("orientation_schedule"),
            },
        )
        # Deepest chain: hr_record_create -> it_laptop -> it_accounts -> access_apollo
        self.assertEqual(batches[3], [self.task_id("access_apollo")])

    def test_every_task_appears_in_exactly_one_batch(self) -> None:
        batches = self.service.parallel_batches(HR_OPS, self.onboarding_id)
        flat = [task_id for batch in batches for task_id in batch]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertEqual(set(flat), {t.task_id for t in self.record.tasks})

    def test_a_batch_contains_no_task_depending_on_another_in_the_same_batch(self) -> None:
        batches = self.service.parallel_batches(HR_OPS, self.onboarding_id)
        for batch in batches:
            members = set(batch)
            for task_id in batch:
                task = self.record.task_by_id(task_id)
                assert task is not None
                self.assertFalse(members & set(task.depends_on), msg=task_id)

    def test_parallel_branches_do_not_interfere(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(HR_OPS, "collect_documents")
        # Finance branch and IT branch are independent of each other.
        self.complete(FINANCE, "payroll_setup")
        self.assertEqual(self.task("it_laptop").status, TaskStatus.READY)
        self.complete(IT_MANAGER, "it_laptop")
        self.assertEqual(self.task("payroll_setup").status, TaskStatus.COMPLETE)
        self.assertEqual(self.task("it_accounts").status, TaskStatus.READY)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
