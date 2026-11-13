"""Final Phase 1 hardening regressions.

Findings covered:

* **N1** — a change approved after cancellation re-planned a cancelled
  onboarding. Reachable through public APIs only, no internals touched.
* **N2** — an approved cancellation could be replayed against an already
  cancelled onboarding.
* **N3** — ``replan`` mutated ``joining_date`` before validating it, so an
  unsupported date left the record corrupted rather than failing safely.
* **§2A** — successful reads of T2 personal data are now audited.

Each test fails against the pre-fix code.
"""

from __future__ import annotations

import unittest
from datetime import date

from onboarding_task_management.contracts import (
    DecisionOutcome,
    OnboardingStatus,
    TaskStatus,
)
from onboarding_task_management.errors import (
    CalendarRangeError,
    DataIsolationError,
    InvalidStateError,
)
from onboarding_task_management.synthetic import LOCATION_REMOTE, PROJECT_ZEPHYR
from tests.support import (
    EMPLOYEE,
    HR_OPS,
    HR_OPS_APPROVER,
    HR_OPS_BETA,
    MANAGER,
    ServiceTestCase,
)

NEW_JOINING = date(2026, 11, 30)
UNSUPPORTED_JOINING = date(2029, 3, 1)


class _GateHelpers(ServiceTestCase):
    def approve(self, request, approver=HR_OPS_APPROVER):
        self.service.decide_change(
            approver, request.request_id, DecisionOutcome.APPROVED
        )
        return request

    def cancel_onboarding(self) -> None:
        request = self.service.request_cancellation(HR_OPS, self.onboarding_id, "x")
        self.approve(request)
        self.service.apply_cancellation(HR_OPS, request.request_id)


# ---------------------------------------------------------------------------
# N1 — stale approval must not act on a cancelled onboarding
# ---------------------------------------------------------------------------


class StaleApprovalTests(_GateHelpers):
    def test_change_approved_after_cancellation_cannot_be_applied(self) -> None:
        # Requested while active...
        change = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )
        self.cancel_onboarding()
        # ...approved only after the onboarding was cancelled.
        self.approve(change)

        with self.assertRaises(InvalidStateError) as ctx:
            self.service.apply_change(HR_OPS, change.request_id)
        self.assertIn("not active", str(ctx.exception))

    def test_cancelled_onboarding_is_left_untouched(self) -> None:
        change = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )
        self.cancel_onboarding()
        self.approve(change)
        with self.assertRaises(InvalidStateError):
            self.service.apply_change(HR_OPS, change.request_id)

        record = self.record
        self.assertEqual(record.status, OnboardingStatus.CANCELLED)
        self.assertEqual(record.joining_date, self.trigger.joining_date)
        self.assertEqual(record.revision, 0)
        self.assertTrue(all(t.status is TaskStatus.CANCELLED for t in record.tasks))

    def test_refusal_is_audited(self) -> None:
        change = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )
        self.cancel_onboarding()
        self.approve(change)
        with self.assertRaises(InvalidStateError):
            self.service.apply_change(HR_OPS, change.request_id)

        events = [
            e
            for e in self.service.audit.events_for(HR_OPS)
            if e.outcome == "denied_not_active"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "change.apply")

    def test_holds_for_location_and_project_changes_too(self) -> None:
        location = self.service.request_change(
            HR_OPS, self.onboarding_id, location=LOCATION_REMOTE
        )
        project = self.service.request_change(
            HR_OPS, self.onboarding_id, project=PROJECT_ZEPHYR
        )
        self.cancel_onboarding()
        self.approve(location)
        self.approve(project, approver=MANAGER)

        for request in (location, project):
            with self.subTest(gate=request.gate):
                with self.assertRaises(InvalidStateError):
                    self.service.apply_change(HR_OPS, request.request_id)

    def test_a_change_approved_while_active_still_applies(self) -> None:
        change = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=NEW_JOINING
        )
        self.approve(change)
        self.service.apply_change(HR_OPS, change.request_id)
        self.assertEqual(self.record.joining_date, NEW_JOINING)


# ---------------------------------------------------------------------------
# N2 — cancellation must not be replayable
# ---------------------------------------------------------------------------


class CancellationReplayTests(_GateHelpers):
    def test_approved_cancellation_cannot_be_applied_twice(self) -> None:
        request = self.service.request_cancellation(HR_OPS, self.onboarding_id, "x")
        self.approve(request)
        self.service.apply_cancellation(HR_OPS, request.request_id)

        with self.assertRaises(InvalidStateError) as ctx:
            self.service.apply_cancellation(HR_OPS, request.request_id)
        self.assertIn("not active", str(ctx.exception))

    def test_replay_does_not_add_a_second_cancellation_event(self) -> None:
        request = self.service.request_cancellation(HR_OPS, self.onboarding_id, "x")
        self.approve(request)
        self.service.apply_cancellation(HR_OPS, request.request_id)
        with self.assertRaises(InvalidStateError):
            self.service.apply_cancellation(HR_OPS, request.request_id)

        outcomes = [
            e.outcome
            for e in self.service.audit.events_for(HR_OPS)
            if e.action == "onboarding.cancel"
        ]
        self.assertEqual(outcomes.count("cancelled"), 1)
        self.assertEqual(outcomes.count("denied_not_active"), 1)

    def test_replay_does_not_overwrite_cancellation_timestamps(self) -> None:
        request = self.service.request_cancellation(HR_OPS, self.onboarding_id, "x")
        self.approve(request)
        self.service.apply_cancellation(HR_OPS, request.request_id)
        first = [t.cancelled_at for t in self.record.tasks]

        with self.assertRaises(InvalidStateError):
            self.service.apply_cancellation(HR_OPS, request.request_id)
        self.assertEqual([t.cancelled_at for t in self.record.tasks], first)


# ---------------------------------------------------------------------------
# N3 — re-planning must fail safely on an unsupported date
# ---------------------------------------------------------------------------


class UnsupportedDateReplanTests(_GateHelpers):
    def test_unsupported_date_is_refused_at_request_time(self) -> None:
        with self.assertRaises(CalendarRangeError) as ctx:
            self.service.request_change(
                HR_OPS, self.onboarding_id, joining_date=UNSUPPORTED_JOINING
            )
        self.assertIn("outside the verified", str(ctx.exception))

    def test_refused_request_creates_no_pending_approval(self) -> None:
        with self.assertRaises(CalendarRangeError):
            self.service.request_change(
                HR_OPS, self.onboarding_id, joining_date=UNSUPPORTED_JOINING
            )
        self.assertEqual(self.service.inbox.pending(HR_OPS), [])

    def test_record_is_untouched_by_a_refused_request(self) -> None:
        before = self.record
        with self.assertRaises(CalendarRangeError):
            self.service.request_change(
                HR_OPS, self.onboarding_id, joining_date=UNSUPPORTED_JOINING
            )
        after = self.record
        self.assertEqual(after.joining_date, before.joining_date)
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(
            [(t.task_id, t.due_date) for t in after.tasks],
            [(t.task_id, t.due_date) for t in before.tasks],
        )

    def test_replan_leaves_no_half_applied_state(self) -> None:
        # Direct planner call: even bypassing the service guard, the record
        # must not be mutated before validation.
        from onboarding_task_management.sub_agent import planner

        record = self.service.get(HR_OPS, self.onboarding_id)
        config = self.service.registry.get(record.client_id)
        original_date = record.joining_date
        original_revision = record.revision

        with self.assertRaises(CalendarRangeError):
            planner.replan(record, config, joining_date=UNSUPPORTED_JOINING)

        self.assertEqual(record.joining_date, original_date)
        self.assertEqual(record.revision, original_revision)

    def test_supported_boundary_dates_are_still_accepted(self) -> None:
        change = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=date(2027, 12, 15)
        )
        self.approve(change)
        self.service.apply_change(HR_OPS, change.request_id)
        self.assertEqual(self.record.joining_date, date(2027, 12, 15))


# ---------------------------------------------------------------------------
# §2A — successful-read auditing
# ---------------------------------------------------------------------------


class ReadAuditTests(ServiceTestCase):
    def _reads(self, actor):
        return [
            e
            for e in self.service.audit.events_for(actor)
            if e.action == "onboarding.read"
        ]

    def test_successful_get_is_audited(self) -> None:
        before = len(self._reads(HR_OPS))
        self.service.get(HR_OPS, self.onboarding_id)
        reads = self._reads(HR_OPS)
        self.assertEqual(len(reads), before + 1)
        self.assertEqual(reads[-1].detail["operation"], "get")
        self.assertEqual(reads[-1].outcome, "read")

    def test_status_and_risk_reads_are_audited_with_their_operation(self) -> None:
        self.service.status(HR_OPS, self.onboarding_id, date(2026, 11, 5))
        self.service.joining_risk(HR_OPS, self.onboarding_id, date(2026, 11, 5))
        operations = [e.detail["operation"] for e in self._reads(HR_OPS)]
        self.assertIn("status", operations)
        self.assertIn("joining_risk", operations)

    def test_read_events_carry_actor_and_ids_only(self) -> None:
        self.service.get(EMPLOYEE, self.onboarding_id)
        event = self._reads(EMPLOYEE)[-1]
        self.assertEqual(event.actor_id, EMPLOYEE.actor_id)
        self.assertEqual(event.onboarding_id, self.onboarding_id)
        # IDs and the operation name only — no record contents.
        self.assertEqual(set(event.detail), {"operation"})

    def test_denied_cross_client_read_is_audited_as_isolation_not_read(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.get(HR_OPS_BETA, self.onboarding_id)
        beta = self.service.audit.events_for(HR_OPS_BETA)
        self.assertEqual([e.outcome for e in beta], ["denied_isolation"])
        # A denied read must not also be logged as a successful one.
        self.assertEqual(self._reads(HR_OPS_BETA), [])

    def test_reminder_drafts_does_not_double_log_a_read(self) -> None:
        before = len(self._reads(HR_OPS))
        self.service.reminder_drafts(HR_OPS, self.onboarding_id, date(2026, 11, 5))
        # reminder.draft already records the operation; a second read event
        # for the same call would be a duplicate.
        self.assertEqual(len(self._reads(HR_OPS)), before)
        self.assertIn("reminder.draft", self.service.audit.actions_for(HR_OPS))

    def test_read_audit_is_scoped_to_the_reading_client(self) -> None:
        self.service.get(HR_OPS, self.onboarding_id)
        self.assertEqual(self._reads(HR_OPS_BETA), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
