"""Document requirements on the plan and replanning on UC-03's validation feedback
(``OnboardingService.define_document_requirements`` / ``replan_documents``)."""

from __future__ import annotations

from onboarding_task_management.contracts import DocumentFeedback, DocumentSpec
from onboarding_task_management.errors import InvalidStateError, StalePlanVersionError, UnauthorizedActionError
from tests.support import EMPLOYEE, HR_OPS, ServiceTestCase

INITIAL = [DocumentSpec(document_type="passport"), DocumentSpec(document_type="degree_certificate")]


class DocumentRequirementTests(ServiceTestCase):
    def test_initial_requirements_are_recorded_once_and_idempotently(self) -> None:
        record = self.service.define_document_requirements(HR_OPS, self.onboarding_id, INITIAL)
        again = self.service.define_document_requirements(HR_OPS, self.onboarding_id, INITIAL)
        self.assertEqual([d.key for d in record.documents], ["passport", "degree_certificate"])
        self.assertEqual(again.plan_version, record.plan_version)
        with self.assertRaises(InvalidStateError):
            self.service.define_document_requirements(HR_OPS, self.onboarding_id, INITIAL[:1])

    def test_only_hr_may_define_requirements(self) -> None:
        with self.assertRaises(UnauthorizedActionError):
            self.service.define_document_requirements(EMPLOYEE, self.onboarding_id, INITIAL)


class DocumentReplanTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.define_document_requirements(HR_OPS, self.onboarding_id, INITIAL)
        self.v1 = self.record.plan_version

    def feedback(self, **over) -> DocumentFeedback:
        base = dict(feedback_id="FB-1", plan_version=self.v1,
                    add_documents=[DocumentSpec(document_type="visa_documents", reason="visa required")],
                    add_attributes=["visa_required"], reasons=["NEW_MANDATORY_DOCUMENT"])
        base.update(over)
        return DocumentFeedback(**base)

    def test_replan_versions_the_plan_adds_the_document_and_the_visa_tasks(self) -> None:
        self.assertNotIn("visa_processing", self.template_keys())
        outcome = self.service.replan_documents(HR_OPS, self.onboarding_id, self.feedback())
        self.assertEqual((outcome.previous_plan_version, outcome.plan_version), (self.v1, self.v1 + 1))
        self.assertEqual(outcome.added_documents, ["visa_documents"])
        self.assertIn(self.task_id("visa_processing"), outcome.added_task_ids)
        self.assertEqual(self.record.plan_version, self.v1 + 1)
        self.assertIn("visa_documents", [d.key for d in self.record.documents])
        self.assertIn("visa_required", self.record.attributes)
        self.assertEqual(self.record.plan_events[-1].action, "replanned_documents")

    def test_the_same_feedback_twice_changes_the_plan_once(self) -> None:
        first = self.service.replan_documents(HR_OPS, self.onboarding_id, self.feedback())
        replay = self.service.replan_documents(HR_OPS, self.onboarding_id, self.feedback())
        self.assertTrue(replay.duplicate_replay)
        self.assertEqual(replay.plan_version, first.plan_version)
        self.assertEqual(self.record.plan_version, self.v1 + 1)

    def test_stale_feedback_is_refused_and_changes_nothing(self) -> None:
        self.service.replan_documents(HR_OPS, self.onboarding_id, self.feedback())
        with self.assertRaises(StalePlanVersionError):
            self.service.replan_documents(HR_OPS, self.onboarding_id, self.feedback(feedback_id="FB-2"))
        self.assertEqual(self.record.plan_version, self.v1 + 1)

    def test_feedback_that_changes_nothing_is_refused(self) -> None:
        with self.assertRaises(InvalidStateError):
            self.service.replan_documents(HR_OPS, self.onboarding_id, self.feedback(
                add_documents=[DocumentSpec(document_type="passport")], add_attributes=[]))
        self.assertEqual(self.record.plan_version, self.v1)

    def test_a_document_can_be_dropped(self) -> None:
        outcome = self.service.replan_documents(HR_OPS, self.onboarding_id, self.feedback(
            add_documents=[], add_attributes=[], remove_documents=["degree_certificate"]))
        self.assertEqual(outcome.removed_documents, ["degree_certificate"])
        self.assertEqual([d.key for d in self.record.documents], ["passport"])
