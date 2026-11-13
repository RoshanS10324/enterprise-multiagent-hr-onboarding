"""Onboarding cancellation behind its approval gate."""

from __future__ import annotations

import unittest
from datetime import date

from onboarding_task_management.contracts import (
    DecisionOutcome,
    GateName,
    OnboardingStatus,
    TaskStatus,
)
from onboarding_task_management.errors import (
    ApprovalRequiredError,
    InvalidStateError,
    UnauthorizedActionError,
)
from tests.support import (
    HR_OPS,
    HR_OPS_APPROVER,
    IT_MANAGER,
    ServiceTestCase,
)


class CancellationTests(ServiceTestCase):
    def _request(self, reason: str = "candidate withdrew"):
        return self.service.request_cancellation(HR_OPS, self.onboarding_id, reason)

    def _cancel(self):
        request = self._request()
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        return self.service.apply_cancellation(HR_OPS, request.request_id)

    def test_cancellation_uses_its_own_gate(self) -> None:
        request = self._request()
        self.assertEqual(request.gate, GateName.CANCELLATION)
        self.assertEqual(request.approver_role.value, "hr_ops")
        self.assertEqual(request.payload["reason"], "candidate withdrew")

    def test_request_alone_does_not_cancel(self) -> None:
        self._request()
        self.assertEqual(self.record.status, OnboardingStatus.ACTIVE)
        self.assertEqual(self.task("hr_record_create").status, TaskStatus.READY)

    def test_cancellation_without_approval_is_refused(self) -> None:
        request = self._request()
        with self.assertRaises(ApprovalRequiredError):
            self.service.apply_cancellation(HR_OPS, request.request_id)
        self.assertEqual(self.record.status, OnboardingStatus.ACTIVE)

    def test_rejected_cancellation_leaves_the_onboarding_active(self) -> None:
        request = self._request()
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.REJECTED
        )
        with self.assertRaises(ApprovalRequiredError):
            self.service.apply_cancellation(HR_OPS, request.request_id)
        self.assertEqual(self.record.status, OnboardingStatus.ACTIVE)
        self.assertEqual(self.task("hr_record_create").status, TaskStatus.READY)

    def test_approved_cancellation_cancels_all_open_tasks(self) -> None:
        record = self._cancel()
        self.assertEqual(record.status, OnboardingStatus.CANCELLED)
        self.assertTrue(
            all(t.status is TaskStatus.CANCELLED for t in record.tasks),
            msg=[(t.template_key, t.status) for t in record.tasks],
        )
        self.assertTrue(all(t.cancelled_at is not None for t in record.tasks))

    def test_completed_tasks_are_not_reverted_by_cancellation(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(IT_MANAGER, "it_laptop")

        record = self._cancel()

        self.assertEqual(self.task("hr_record_create").status, TaskStatus.COMPLETE)
        self.assertEqual(self.task("it_laptop").status, TaskStatus.COMPLETE)
        self.assertIsNone(self.task("it_laptop").cancelled_at)
        cancelled = [t for t in record.tasks if t.status is TaskStatus.CANCELLED]
        self.assertEqual(len(cancelled), len(record.tasks) - 2)

    def test_in_progress_task_is_cancelled(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.service.start_task(
            IT_MANAGER, self.onboarding_id, self.task_id("it_laptop")
        )
        self._cancel()
        self.assertEqual(self.task("it_laptop").status, TaskStatus.CANCELLED)

    def test_no_work_may_proceed_after_cancellation(self) -> None:
        self._cancel()
        with self.assertRaises(InvalidStateError) as ctx:
            self.complete(HR_OPS, "hr_record_create")
        self.assertIn("cancelled", str(ctx.exception))

    def test_no_further_changes_may_be_requested(self) -> None:
        self._cancel()
        with self.assertRaises(InvalidStateError) as ctx:
            self.service.request_change(
                HR_OPS, self.onboarding_id, joining_date=date(2026, 12, 7)
            )
        self.assertIn("not active", str(ctx.exception))

    def test_cancelling_twice_is_rejected(self) -> None:
        self._cancel()
        with self.assertRaises(InvalidStateError):
            self._request()

    def test_wrong_role_may_not_approve_cancellation(self) -> None:
        request = self._request()
        with self.assertRaises(UnauthorizedActionError):
            self.service.decide_change(
                IT_MANAGER, request.request_id, DecisionOutcome.APPROVED
            )

    def test_cancellation_is_audited_with_a_count(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self._cancel()
        events = [
            e
            for e in self.service.audit.events_for(HR_OPS)
            if e.action == "onboarding.cancel"
        ]
        self.assertEqual(events[-1].outcome, "cancelled")
        self.assertEqual(events[-1].detail["cancelled_tasks"], "9")

    def test_cancelled_tasks_are_excluded_from_parallel_batches(self) -> None:
        self._cancel()
        batches = self.service.parallel_batches(HR_OPS, self.onboarding_id)
        self.assertEqual(batches, [])

    def test_summary_reports_cancelled_counts(self) -> None:
        self._cancel()
        summary = self.service.status(HR_OPS, self.onboarding_id, date(2026, 11, 5))
        self.assertEqual(summary.cancelled, summary.total_tasks)
        self.assertEqual(summary.overdue_task_ids, [])
        self.assertTrue(summary.is_on_track)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
