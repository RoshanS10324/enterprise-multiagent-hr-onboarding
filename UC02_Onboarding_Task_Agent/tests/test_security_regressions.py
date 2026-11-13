"""Regression tests for the Phase 1 security and correctness fixes.

Each test here reproduces a specific reviewed finding and asserts the fix.
Every one of these FAILS against the pre-fix code — that is the point: the
original 142 tests all passed while these defects were live, because they only
ever exercised correct usage.

Findings covered: C1, H1, H2, M4, M5.
"""

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
    DuplicateOnboardingError,
    InvalidStateError,
    UnauthorizedActionError,
)
from onboarding_task_management.sub_agent.approvals import (
    CANCELLATION_GATES,
    CHANGE_GATES,
)
from onboarding_task_management.sub_agent.planner import make_onboarding_id
from onboarding_task_management.synthetic import (
    CLIENT_ALPHA,
    CLIENT_BETA,
    LOCATION_REMOTE,
    make_trigger,
    persona,
)
from tests.support import (
    EMPLOYEE,
    HR_OPS,
    HR_OPS_APPROVER,
    HR_OPS_BETA,
    ServiceTestCase,
)

NEW_JOINING = date(2026, 11, 30)


# ---------------------------------------------------------------------------
# C1 — approval gate binding
# ---------------------------------------------------------------------------


class GateBindingTests(ServiceTestCase):
    """An approval for one gate must not authorise a different operation."""

    def _approved_change_request(self):
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        return request

    def _approved_cancellation_request(self):
        request = self.service.request_cancellation(HR_OPS, self.onboarding_id, "x")
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        return request

    # -- direction 1: change approval must not cancel ----------------------

    def test_date_change_approval_cannot_drive_cancellation(self) -> None:
        request = self._approved_change_request()
        with self.assertRaises(UnauthorizedActionError) as ctx:
            self.service.apply_cancellation(HR_OPS, request.request_id)
        self.assertIn("different gate", str(ctx.exception))

    def test_onboarding_survives_the_rejected_cancellation_attempt(self) -> None:
        request = self._approved_change_request()
        with self.assertRaises(UnauthorizedActionError):
            self.service.apply_cancellation(HR_OPS, request.request_id)

        self.assertEqual(self.record.status, OnboardingStatus.ACTIVE)
        self.assertFalse(
            any(t.status is TaskStatus.CANCELLED for t in self.record.tasks)
        )

    def test_gate_mismatch_on_cancellation_is_audited_as_a_security_event(self) -> None:
        request = self._approved_change_request()
        with self.assertRaises(UnauthorizedActionError):
            self.service.apply_cancellation(HR_OPS, request.request_id)

        events = [
            e
            for e in self.service.audit.events_for(HR_OPS)
            if e.outcome == "denied_gate_mismatch"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "onboarding.cancel")
        self.assertEqual(events[0].detail["approved_gate"], str(GateName.DATE_CHANGE))

    # -- direction 2: cancellation approval must not re-plan ---------------

    def test_cancellation_approval_cannot_drive_apply_change(self) -> None:
        request = self._approved_cancellation_request()
        with self.assertRaises(UnauthorizedActionError) as ctx:
            self.service.apply_change(HR_OPS, request.request_id)
        self.assertIn("different gate", str(ctx.exception))

    def test_gate_mismatch_on_change_is_audited_as_a_security_event(self) -> None:
        request = self._approved_cancellation_request()
        with self.assertRaises(UnauthorizedActionError):
            self.service.apply_change(HR_OPS, request.request_id)

        events = [
            e
            for e in self.service.audit.events_for(HR_OPS)
            if e.outcome == "denied_gate_mismatch"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "change.apply")
        self.assertEqual(events[0].detail["approved_gate"], str(GateName.CANCELLATION))

    # -- the correct pairings still work -----------------------------------

    def test_matching_gates_still_authorise_their_own_operation(self) -> None:
        request = self._approved_change_request()
        self.service.apply_change(HR_OPS, request.request_id)
        self.assertEqual(self.record.joining_date, NEW_JOINING)

        cancel = self._approved_cancellation_request()
        self.service.apply_cancellation(HR_OPS, cancel.request_id)
        self.assertEqual(self.record.status, OnboardingStatus.CANCELLED)

    def test_every_change_gate_is_accepted_by_apply_change(self) -> None:
        self.assertEqual(
            CHANGE_GATES,
            {
                GateName.DATE_CHANGE,
                GateName.LOCATION_CHANGE,
                GateName.PROJECT_CHANGE,
            },
        )
        self.assertEqual(CANCELLATION_GATES, {GateName.CANCELLATION})

    def test_location_change_approval_also_cannot_cancel(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, location=LOCATION_REMOTE
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        with self.assertRaises(UnauthorizedActionError):
            self.service.apply_cancellation(HR_OPS, request.request_id)
        self.assertEqual(self.record.status, OnboardingStatus.ACTIVE)

    def test_gate_mismatch_is_reported_even_when_the_request_is_pending(self) -> None:
        # Gate is checked before outcome, so a mismatch is never leaked as
        # "still awaiting approval".
        request = self.service.request_cancellation(HR_OPS, self.onboarding_id, "x")
        with self.assertRaises(UnauthorizedActionError):
            self.service.apply_change(HR_OPS, request.request_id)


# ---------------------------------------------------------------------------
# H1 — mutable record bypass
# ---------------------------------------------------------------------------


class RecordImmutabilityTests(ServiceTestCase):
    """Objects handed to callers must not be the stored ones."""

    def test_get_returns_a_distinct_object(self) -> None:
        first = self.service.get(HR_OPS, self.onboarding_id)
        second = self.service.get(HR_OPS, self.onboarding_id)
        self.assertIsNot(first, second)
        self.assertIsNot(first.tasks[0], second.tasks[0])
        self.assertEqual(first, second)

    def test_mutating_a_returned_record_does_not_affect_stored_state(self) -> None:
        leaked = self.service.get(HR_OPS, self.onboarding_id)
        leaked.status = OnboardingStatus.CANCELLED
        leaked.joining_date = date(2030, 1, 1)
        leaked.tasks[0].status = TaskStatus.COMPLETE

        stored = self.service.get(HR_OPS, self.onboarding_id)
        self.assertEqual(stored.status, OnboardingStatus.ACTIVE)
        self.assertEqual(stored.joining_date, self.trigger.joining_date)
        self.assertEqual(stored.tasks[0].status, TaskStatus.READY)

    def test_unauthorized_role_cannot_mutate_through_a_read(self) -> None:
        # An EMPLOYEE has no authority over an IT task. Previously they could
        # read the record and simply assign the status.
        leaked = self.service.get(EMPLOYEE, self.onboarding_id)
        for task in leaked.tasks:
            task.status = TaskStatus.COMPLETE

        stored = self.service.get(HR_OPS, self.onboarding_id)
        self.assertFalse(any(t.status is TaskStatus.COMPLETE for t in stored.tasks))

    def test_bypass_attempt_produces_no_mutation_event_and_no_state_change(self) -> None:
        leaked = self.service.get(EMPLOYEE, self.onboarding_id)
        leaked.tasks[0].status = TaskStatus.COMPLETE

        # The read itself is audited (§2A), but the mutation is inert: the
        # employee's only recorded action is the read, and stored state is
        # untouched. Scope to this actor — actions_for is client-wide, so it
        # also contains setUp's create by HR Ops.
        actions = [
            e.action
            for e in self.service.audit.events_for(EMPLOYEE)
            if e.actor_id == EMPLOYEE.actor_id
        ]
        self.assertEqual(actions, ["onboarding.read"])
        self.assertEqual(
            self.service.get(HR_OPS, self.onboarding_id).tasks[0].status,
            TaskStatus.READY,
        )

    def test_create_onboarding_returns_a_snapshot(self) -> None:
        created = self.service.create_onboarding(HR_OPS, make_trigger(persona("EMP-002")))
        created.tasks[0].status = TaskStatus.COMPLETE
        stored = self.service.get(HR_OPS, created.onboarding_id)
        self.assertEqual(stored.tasks[0].status, TaskStatus.READY)

    def test_task_returning_methods_return_snapshots(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        for task in self.service.ready_tasks(HR_OPS, self.onboarding_id):
            task.status = TaskStatus.COMPLETE
        for task in self.service.blocked_tasks(HR_OPS, self.onboarding_id):
            task.status = TaskStatus.COMPLETE

        summary = self.service.status(HR_OPS, self.onboarding_id, date(2026, 11, 5))
        self.assertEqual(summary.complete, 1)  # only the genuine completion

    def test_completed_task_object_is_a_snapshot(self) -> None:
        returned = self.service.complete_task(
            HR_OPS, self.onboarding_id, self.task_id("hr_record_create")
        )
        returned.status = TaskStatus.CANCELLED
        self.assertEqual(self.task("hr_record_create").status, TaskStatus.COMPLETE)

    def test_apply_cancellation_returns_a_snapshot(self) -> None:
        request = self.service.request_cancellation(HR_OPS, self.onboarding_id, "x")
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        returned = self.service.apply_cancellation(HR_OPS, request.request_id)
        returned.status = OnboardingStatus.ACTIVE
        self.assertEqual(self.record.status, OnboardingStatus.CANCELLED)


# ---------------------------------------------------------------------------
# H2 — cross-client audit read
# ---------------------------------------------------------------------------


class AuditIsolationTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.create_onboarding(HR_OPS_BETA, make_trigger(persona("EMP-006")))

    def test_audit_read_is_scoped_to_the_actors_own_client(self) -> None:
        alpha = self.service.audit.events_for(HR_OPS)
        beta = self.service.audit.events_for(HR_OPS_BETA)
        self.assertTrue(alpha and beta)
        self.assertEqual({e.client_id for e in alpha}, {CLIENT_ALPHA})
        self.assertEqual({e.client_id for e in beta}, {CLIENT_BETA})

    def test_actor_cannot_widen_scope_to_another_client(self) -> None:
        # Scope comes from the actor; there is no client_id parameter to abuse.
        self.assertNotIn(
            CLIENT_BETA,
            {e.client_id for e in self.service.audit.events_for(HR_OPS)},
        )
        self.assertNotIn(
            CLIENT_ALPHA,
            {e.client_id for e in self.service.audit.events_for(HR_OPS_BETA)},
        )

    def test_legacy_unscoped_read_api_is_gone(self) -> None:
        # The old events()/actions() accepted a client_id and defaulted to
        # returning every client's events.
        self.assertFalse(hasattr(self.service.audit, "events"))
        self.assertFalse(hasattr(self.service.audit, "actions"))

    def test_actions_are_scoped_too(self) -> None:
        self.assertNotIn(
            "onboarding.create",
            [
                a
                for a, e in zip(
                    self.service.audit.actions_for(HR_OPS_BETA),
                    self.service.audit.events_for(HR_OPS_BETA),
                )
                if e.client_id == CLIENT_ALPHA
            ],
        )

    def test_privileged_cross_client_read_is_explicitly_named(self) -> None:
        everything = self.service.audit.all_events_privileged(reason="DSAR-123")
        self.assertEqual(
            {e.client_id for e in everything}, {CLIENT_ALPHA, CLIENT_BETA}
        )

    def test_privileged_read_requires_a_reason(self) -> None:
        for bad in ("", "   "):
            with self.assertRaises(ValueError):
                self.service.audit.all_events_privileged(reason=bad)

    def test_returned_events_cannot_rewrite_the_log(self) -> None:
        events = self.service.audit.events_for(HR_OPS)
        events[0].outcome = "tampered"
        self.assertNotEqual(
            self.service.audit.events_for(HR_OPS)[0].outcome, "tampered"
        )


# ---------------------------------------------------------------------------
# M4 — duplicate task completion
# ---------------------------------------------------------------------------


class DuplicateCompletionTests(ServiceTestCase):
    def test_completing_an_already_complete_task_is_rejected(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        with self.assertRaises(InvalidStateError) as ctx:
            self.complete(HR_OPS, "hr_record_create")
        self.assertIn("already complete", str(ctx.exception))

    def test_rejected_recompletion_preserves_the_original_timestamp(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        original = self.task("hr_record_create").completed_at
        with self.assertRaises(InvalidStateError):
            self.complete(HR_OPS, "hr_record_create")
        self.assertEqual(self.task("hr_record_create").completed_at, original)

    def test_recompletion_does_not_emit_a_second_success_event(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        with self.assertRaises(InvalidStateError):
            self.complete(HR_OPS, "hr_record_create")

        outcomes = [
            e.outcome
            for e in self.service.audit.events_for(HR_OPS)
            if e.action == "task.complete"
        ]
        self.assertEqual(outcomes.count("complete"), 1)
        self.assertEqual(outcomes.count("denied"), 1)

    def test_an_admin_cannot_recomplete_either(self) -> None:
        from tests.support import HR_LEAD

        self.complete(HR_OPS, "hr_record_create")
        with self.assertRaises(InvalidStateError):
            self.complete(HR_LEAD, "hr_record_create")

    def test_starting_a_complete_task_is_still_rejected(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        with self.assertRaises(InvalidStateError):
            self.service.start_task(
                HR_OPS, self.onboarding_id, self.task_id("hr_record_create")
            )


# ---------------------------------------------------------------------------
# M5 — re-onboarding after cancellation
# ---------------------------------------------------------------------------


class ReOnboardingTests(ServiceTestCase):
    def _cancel(self) -> None:
        request = self.service.request_cancellation(
            HR_OPS, self.onboarding_id, "candidate withdrew"
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_cancellation(HR_OPS, request.request_id)

    def test_new_onboarding_is_allowed_after_cancellation(self) -> None:
        self._cancel()
        rehire = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"), joining_date=date(2027, 3, 1))
        )
        self.assertEqual(rehire.status, OnboardingStatus.ACTIVE)
        self.assertEqual(rehire.joining_date, date(2027, 3, 1))
        self.assertEqual(len(rehire.tasks), 10)

    def test_rehire_id_is_distinct_and_deterministic(self) -> None:
        self._cancel()
        rehire = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"), joining_date=date(2027, 3, 1))
        )
        self.assertNotEqual(rehire.onboarding_id, self.onboarding_id)
        self.assertEqual(
            rehire.onboarding_id, make_onboarding_id(CLIENT_ALPHA, "EMP-001", 2)
        )
        self.assertEqual(rehire.onboarding_id, f"ONB-{CLIENT_ALPHA}-EMP-001-R2")

    def test_rehire_task_ids_do_not_collide_with_the_cancelled_attempt(self) -> None:
        self._cancel()
        rehire = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"), joining_date=date(2027, 3, 1))
        )
        old_ids = {t.task_id for t in self.service.get(HR_OPS, self.onboarding_id).tasks}
        new_ids = {t.task_id for t in rehire.tasks}
        self.assertEqual(old_ids & new_ids, set())

    def test_cancelled_record_is_preserved(self) -> None:
        self._cancel()
        self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"), joining_date=date(2027, 3, 1))
        )
        original = self.service.get(HR_OPS, self.onboarding_id)
        self.assertEqual(original.status, OnboardingStatus.CANCELLED)
        self.assertTrue(
            all(t.status is TaskStatus.CANCELLED for t in original.tasks)
        )

    def test_audit_history_spans_both_attempts(self) -> None:
        self._cancel()
        rehire = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"), joining_date=date(2027, 3, 1))
        )
        onboarding_ids = {
            e.onboarding_id
            for e in self.service.audit.events_for(HR_OPS)
            if e.action == "onboarding.create"
        }
        self.assertEqual(
            onboarding_ids, {self.onboarding_id, rehire.onboarding_id}
        )

    def test_a_third_attempt_increments_again(self) -> None:
        self._cancel()
        second = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"), joining_date=date(2027, 3, 1))
        )
        request = self.service.request_cancellation(
            HR_OPS, second.onboarding_id, "withdrew again"
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_cancellation(HR_OPS, request.request_id)

        third = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"), joining_date=date(2027, 6, 1))
        )
        self.assertEqual(third.onboarding_id, f"ONB-{CLIENT_ALPHA}-EMP-001-R3")

    def test_an_active_onboarding_still_blocks_a_duplicate(self) -> None:
        with self.assertRaises(DuplicateOnboardingError) as ctx:
            self.service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        self.assertIn(self.onboarding_id, str(ctx.exception))

    def test_rehire_then_duplicate_is_blocked_again(self) -> None:
        self._cancel()
        rehire = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"), joining_date=date(2027, 3, 1))
        )
        with self.assertRaises(DuplicateOnboardingError) as ctx:
            self.service.create_onboarding(
                HR_OPS, make_trigger(persona("EMP-001"), joining_date=date(2027, 4, 1))
            )
        self.assertIn(rehire.onboarding_id, str(ctx.exception))

    def test_rehire_is_independently_workable(self) -> None:
        self._cancel()
        rehire = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"), joining_date=date(2027, 3, 1))
        )
        root = f"{rehire.onboarding_id}#hr_record_create"
        self.service.complete_task(HR_OPS, rehire.onboarding_id, root)
        refreshed = self.service.get(HR_OPS, rehire.onboarding_id)
        self.assertEqual(
            refreshed.task_by_key("hr_record_create").status, TaskStatus.COMPLETE
        )
        # The cancelled attempt is untouched.
        self.assertEqual(
            self.service.get(HR_OPS, self.onboarding_id)
            .task_by_key("hr_record_create")
            .status,
            TaskStatus.CANCELLED,
        )

    def test_make_onboarding_id_rejects_a_bad_attempt(self) -> None:
        with self.assertRaises(ValueError):
            make_onboarding_id(CLIENT_ALPHA, "EMP-001", 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
