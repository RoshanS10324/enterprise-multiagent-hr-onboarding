"""Approval gates: enforcement, rejection, and approver authorisation."""

from __future__ import annotations

import unittest
from datetime import date

from onboarding_task_management.contracts import DecisionOutcome, GateName
from onboarding_task_management.errors import (
    ApprovalRequiredError,
    DataIsolationError,
    InvalidStateError,
    UnauthorizedActionError,
)
from onboarding_task_management.synthetic import PROJECT_ZEPHYR
from tests.support import (
    HR_OPS,
    HR_OPS_APPROVER,
    HR_OPS_BETA,
    IT_MANAGER,
    MANAGER,
    ServiceTestCase,
)

NEW_JOINING = date(2026, 11, 30)


class GateEnforcementTests(ServiceTestCase):
    def test_change_request_does_not_apply_anything_yet(self) -> None:
        before = self.task("hr_record_create").due_date
        self.service.request_change(HR_OPS, self.onboarding_id, joining_date=NEW_JOINING)
        self.assertEqual(self.record.joining_date, self.trigger.joining_date)
        self.assertEqual(self.task("hr_record_create").due_date, before)
        self.assertEqual(self.record.revision, 0)

    def test_apply_without_a_decision_is_refused(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )
        with self.assertRaises(ApprovalRequiredError) as ctx:
            self.service.apply_change(HR_OPS, request.request_id)
        self.assertIn("requires an approved decision", str(ctx.exception))

    def test_request_is_pending_until_decided(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )
        self.assertEqual(request.outcome, DecisionOutcome.PENDING)
        self.assertEqual(
            [r.request_id for r in self.service.inbox.pending(HR_OPS)],
            [request.request_id],
        )

    def test_date_change_uses_the_date_change_gate(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )
        self.assertEqual(request.gate, GateName.DATE_CHANGE)
        self.assertEqual(request.approver_role.value, "hr_ops")

    def test_gated_request_is_audited(self) -> None:
        self.service.request_change(HR_OPS, self.onboarding_id, joining_date=NEW_JOINING)
        outcomes = [
            e.outcome for e in self.service.audit.events_for(HR_OPS)
        ]
        self.assertIn("pending_approval", outcomes)


class ApprovalRejectionTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )

    def test_rejection_is_recorded_with_its_reason(self) -> None:
        decided = self.service.decide_change(
            HR_OPS_APPROVER,
            self.request.request_id,
            DecisionOutcome.REJECTED,
            reason="business need not evidenced",
        )
        self.assertEqual(decided.outcome, DecisionOutcome.REJECTED)
        self.assertEqual(decided.decided_by, HR_OPS_APPROVER.actor_id)
        self.assertEqual(decided.reason, "business need not evidenced")
        self.assertIsNotNone(decided.decided_at)

    def test_rejected_change_cannot_be_applied(self) -> None:
        self.service.decide_change(
            HR_OPS_APPROVER, self.request.request_id, DecisionOutcome.REJECTED
        )
        with self.assertRaises(ApprovalRequiredError):
            self.service.apply_change(HR_OPS, self.request.request_id)

    def test_rejected_change_leaves_the_plan_untouched(self) -> None:
        before = {t.template_key: (t.due_date, t.status) for t in self.record.tasks}
        self.service.decide_change(
            HR_OPS_APPROVER, self.request.request_id, DecisionOutcome.REJECTED
        )
        with self.assertRaises(ApprovalRequiredError):
            self.service.apply_change(HR_OPS, self.request.request_id)

        after = {t.template_key: (t.due_date, t.status) for t in self.record.tasks}
        self.assertEqual(before, after)
        self.assertEqual(self.record.joining_date, self.trigger.joining_date)
        self.assertEqual(self.record.revision, 0)

    def test_rejection_is_audited(self) -> None:
        self.service.decide_change(
            HR_OPS_APPROVER, self.request.request_id, DecisionOutcome.REJECTED
        )
        outcomes = [
            e.outcome for e in self.service.audit.events_for(HR_OPS)
        ]
        self.assertIn("rejected", outcomes)

    def test_a_decided_request_cannot_be_decided_again(self) -> None:
        self.service.decide_change(
            HR_OPS_APPROVER, self.request.request_id, DecisionOutcome.REJECTED
        )
        with self.assertRaises(InvalidStateError) as ctx:
            self.service.decide_change(
                HR_OPS_APPROVER, self.request.request_id, DecisionOutcome.APPROVED
            )
        self.assertIn("already been decided", str(ctx.exception))

    def test_a_rejected_change_can_be_re_requested_and_approved(self) -> None:
        self.service.decide_change(
            HR_OPS_APPROVER, self.request.request_id, DecisionOutcome.REJECTED
        )
        retry = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )
        self.service.decide_change(
            HR_OPS_APPROVER, retry.request_id, DecisionOutcome.APPROVED
        )
        outcome = self.service.apply_change(HR_OPS, retry.request_id)
        self.assertEqual(self.record.joining_date, NEW_JOINING)
        self.assertEqual(outcome.revision, 1)


class ApproverAuthorisationTests(ServiceTestCase):
    def test_wrong_role_may_not_decide(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )
        with self.assertRaises(UnauthorizedActionError) as ctx:
            self.service.decide_change(
                IT_MANAGER, request.request_id, DecisionOutcome.APPROVED
            )
        self.assertIn("may not decide this gate", str(ctx.exception))

    def test_requester_may_not_decide_their_own_request(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )
        with self.assertRaises(UnauthorizedActionError) as ctx:
            self.service.decide_change(
                HR_OPS, request.request_id, DecisionOutcome.APPROVED
            )
        self.assertIn("own approval request", str(ctx.exception))

    def test_hr_ops_may_not_decide_a_manager_gate(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, project=PROJECT_ZEPHYR
        )
        with self.assertRaises(UnauthorizedActionError):
            self.service.decide_change(
                HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
            )
        self.service.decide_change(MANAGER, request.request_id, DecisionOutcome.APPROVED)

    def test_another_client_may_not_decide_the_request(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )
        with self.assertRaises(DataIsolationError):
            self.service.decide_change(
                HR_OPS_BETA, request.request_id, DecisionOutcome.APPROVED
            )

    def test_unknown_request_is_rejected(self) -> None:
        with self.assertRaises(InvalidStateError):
            self.service.decide_change(
                HR_OPS_APPROVER, "APR-999999", DecisionOutcome.APPROVED
            )

    def test_pending_is_not_a_valid_decision(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )
        with self.assertRaises(InvalidStateError):
            self.service.decide_change(
                HR_OPS_APPROVER, request.request_id, DecisionOutcome.PENDING
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
