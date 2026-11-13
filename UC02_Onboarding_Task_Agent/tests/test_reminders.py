"""Overdue detection, joining-date risk, and reminder drafts."""

from __future__ import annotations

import unittest
from datetime import date

from onboarding_task_management.contracts import DecisionOutcome, Role, TaskStatus
from onboarding_task_management.sub_agent import reminders
from onboarding_task_management.sub_agent.tracker import is_overdue
from tests.support import HR_OPS, HR_OPS_APPROVER, IT_MANAGER, ServiceTestCase

#: Thu 2026-11-05. hr_record_create (Nov 2) and collect_documents (Nov 4) are past due.
AS_OF_MID = date(2026, 11, 5)
#: Thu 2026-11-12 — two business days before the Mon 2026-11-16 joining date.
AS_OF_NEAR_JOINING = date(2026, 11, 12)
#: Fri 2026-10-16 — before any task is due.
AS_OF_EARLY = date(2026, 10, 16)


class OverdueDetectionTests(ServiceTestCase):
    def test_open_task_past_its_due_date_is_overdue(self) -> None:
        self.assertTrue(is_overdue(self.task("hr_record_create"), AS_OF_MID))

    def test_task_due_today_is_not_overdue(self) -> None:
        self.assertEqual(self.task("visa_application").due_date, date(2026, 11, 5))
        self.assertFalse(is_overdue(self.task("visa_application"), AS_OF_MID))

    def test_completed_task_is_never_overdue(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.assertFalse(is_overdue(self.task("hr_record_create"), AS_OF_MID))

    def test_nothing_is_overdue_before_any_due_date(self) -> None:
        summary = self.service.status(HR_OPS, self.onboarding_id, AS_OF_EARLY)
        self.assertEqual(summary.overdue_task_ids, [])
        self.assertTrue(summary.is_on_track)

    def test_summary_lists_overdue_tasks_and_flags_off_track(self) -> None:
        summary = self.service.status(HR_OPS, self.onboarding_id, AS_OF_MID)
        self.assertEqual(
            set(summary.overdue_task_ids),
            {self.task_id("hr_record_create"), self.task_id("collect_documents")},
        )
        self.assertFalse(summary.is_on_track)
        self.assertEqual(summary.total_tasks, 10)

    def test_summary_counts_match_the_task_states(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.service.start_task(
            IT_MANAGER, self.onboarding_id, self.task_id("it_laptop")
        )
        summary = self.service.status(HR_OPS, self.onboarding_id, AS_OF_MID)
        self.assertEqual(summary.complete, 1)
        self.assertEqual(summary.in_progress, 1)
        self.assertEqual(
            summary.blocked + summary.ready + summary.in_progress + summary.complete,
            summary.total_tasks,
        )

    def test_days_to_joining_is_counted_in_business_days(self) -> None:
        summary = self.service.status(HR_OPS, self.onboarding_id, AS_OF_NEAR_JOINING)
        self.assertEqual(summary.days_to_joining, 2)


class JoiningRiskTests(ServiceTestCase):
    def test_overdue_tasks_raise_a_risk(self) -> None:
        risk = self.service.joining_risk(HR_OPS, self.onboarding_id, AS_OF_MID)
        self.assertTrue(risk.at_risk)
        self.assertIn("overdue", " ".join(risk.reasons))

    def test_critical_tasks_open_near_joining_raise_a_risk(self) -> None:
        risk = self.service.joining_risk(
            HR_OPS, self.onboarding_id, AS_OF_NEAR_JOINING
        )
        self.assertTrue(risk.at_risk)
        self.assertEqual(risk.days_to_joining, 2)
        self.assertIn(self.task_id("it_laptop"), risk.critical_open_task_ids)
        # Orientation is not a day-1 critical category.
        self.assertNotIn(
            self.task_id("orientation_schedule"), risk.critical_open_task_ids
        )

    def test_no_risk_when_nothing_is_overdue_and_joining_is_far_off(self) -> None:
        risk = self.service.joining_risk(HR_OPS, self.onboarding_id, AS_OF_EARLY)
        self.assertFalse(risk.at_risk)
        self.assertEqual(risk.reasons, [])
        self.assertEqual(risk.critical_open_task_ids, [])

    def test_risk_detection_is_audited(self) -> None:
        self.service.joining_risk(HR_OPS, self.onboarding_id, AS_OF_MID)
        actions = self.service.audit.actions_for(HR_OPS)
        self.assertIn("risk.detected", actions)


class ReminderDraftTests(ServiceTestCase):
    def drafts(self, as_of: date):
        return self.service.reminder_drafts(HR_OPS, self.onboarding_id, as_of)

    def test_overdue_tasks_are_grouped_into_one_draft_per_owner(self) -> None:
        overdue = [d for d in self.drafts(AS_OF_MID) if d.reason == "overdue"]
        self.assertEqual(len(overdue), 1)
        self.assertEqual(overdue[0].recipient_role, Role.HR_OPS)
        self.assertEqual(
            set(overdue[0].task_ids),
            {self.task_id("hr_record_create"), self.task_id("collect_documents")},
        )

    def test_due_soon_tasks_get_their_own_drafts_per_owner(self) -> None:
        due_soon = [d for d in self.drafts(AS_OF_MID) if d.reason == "due_soon"]
        self.assertEqual(
            {d.recipient_role for d in due_soon},
            {Role.HR_OPS, Role.IT_MANAGER, Role.FINANCE},
        )

    def test_a_task_is_never_in_both_an_overdue_and_a_due_soon_draft(self) -> None:
        drafts = self.drafts(AS_OF_MID)
        seen: set[str] = set()
        for draft in drafts:
            for task_id in draft.task_ids:
                self.assertNotIn(task_id, seen, msg=task_id)
                seen.add(task_id)

    def test_drafts_are_never_sent(self) -> None:
        for draft in self.drafts(AS_OF_MID):
            self.assertEqual(draft.delivery_status, "draft_not_sent")

    def test_reminders_module_exposes_no_transport(self) -> None:
        exported = [name for name in dir(reminders) if not name.startswith("_")]
        for name in exported:
            self.assertNotIn("send", name.lower(), msg=name)
            self.assertNotIn("dispatch", name.lower(), msg=name)
            self.assertNotIn("email", name.lower(), msg=name)

    def test_drafts_are_bilingual_and_carry_no_personal_data(self) -> None:
        for draft in self.drafts(AS_OF_MID):
            self.assertTrue(draft.subject_en.strip())
            self.assertTrue(draft.subject_ar.strip())
            self.assertTrue(draft.body_en.strip())
            self.assertTrue(draft.body_ar.strip())
            # Addressed by role, never by a person.
            self.assertIsInstance(draft.recipient_role, Role)

    def test_draft_ids_are_deterministic(self) -> None:
        first = [d.draft_id for d in self.drafts(AS_OF_MID)]
        second = [d.draft_id for d in self.drafts(AS_OF_MID)]
        self.assertEqual(first, second)
        self.assertEqual(len(set(first)), len(first))

    def test_no_drafts_when_nothing_is_due_or_overdue(self) -> None:
        self.assertEqual(self.drafts(AS_OF_EARLY), [])

    def test_completed_tasks_produce_no_drafts(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(HR_OPS, "collect_documents")
        overdue = [d for d in self.drafts(AS_OF_MID) if d.reason == "overdue"]
        self.assertEqual(overdue, [])

    def test_draft_preparation_is_audited_as_not_sent(self) -> None:
        self.drafts(AS_OF_MID)
        events = [
            e
            for e in self.service.audit.events_for(HR_OPS)
            if e.action == "reminder.draft"
        ]
        self.assertEqual(events[-1].outcome, "prepared_not_sent")

    def test_cancelled_tasks_produce_no_drafts(self) -> None:
        # Cancellation must go through the approved service API — mutating a
        # record returned by the service no longer affects stored state (H1).
        request = self.service.request_cancellation(
            HR_OPS, self.onboarding_id, "candidate withdrew"
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_cancellation(HR_OPS, request.request_id)

        self.assertTrue(
            all(t.status is TaskStatus.CANCELLED for t in self.record.tasks)
        )
        self.assertEqual(self.drafts(AS_OF_MID), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
