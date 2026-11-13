"""DL-14 (employee does not respond) and DL-09 (route by where the employee is).

Neither rule has a built-in policy: DL-14 needs the client's own interval and attempt limit, and
DL-09 needs the client's templates to ask for the location status. Without them nothing is guessed.
"""

from __future__ import annotations

import unittest
from datetime import date

from onboarding_task_management.contracts import DecisionOutcome, Role, TaskCategory, TaskStatus
from onboarding_task_management.errors import (
    InvalidStateError,
    MissingConfigurationError,
    UnauthorizedActionError,
)
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    ConfigRegistry,
    NonResponsePolicy,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import ALPHA_CONFIG, make_trigger, persona
from tests.support import HR_OPS, HR_OPS_APPROVER, IT_MANAGER

D0 = date(2026, 11, 2)


def service_with(policy: NonResponsePolicy | None) -> OnboardingService:
    registry = ConfigRegistry()
    registry.register(ALPHA_CONFIG.model_copy(update={"non_response_policy": policy}))
    return OnboardingService(registry, require_plan_approval=True)


def released_case(service: OnboardingService) -> tuple[str, str]:
    oid = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).onboarding_id
    service.decide_plan(HR_OPS_APPROVER, service.submit_plan(HR_OPS, oid).request_id,
                        DecisionOutcome.APPROVED)
    return oid, service.get(HR_OPS, oid).tasks[0].task_id


class NonResponseDL14Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.s = service_with(NonResponsePolicy(interval_days=2, max_attempts=2))
        self.oid, self.tid = released_case(self.s)

    def attempt(self, day: int):
        return self.s.record_reminder_attempt(HR_OPS, self.oid, self.tid, date(2026, 11, day))

    def test_no_policy_means_no_reminders_and_no_guessed_default(self) -> None:
        s = service_with(None)
        oid, tid = released_case(s)
        with self.assertRaises(MissingConfigurationError):
            s.record_reminder_attempt(HR_OPS, oid, tid, D0)

    def test_attempts_follow_the_configured_interval_and_never_duplicate(self) -> None:
        self.assertEqual(self.attempt(2).attempts, 1)
        self.assertEqual(self.attempt(2).attempts, 1)  # same-day replay
        self.assertEqual(self.attempt(3).attempts, 1)  # interval not elapsed
        status = self.attempt(4)
        self.assertEqual((status.attempts, status.next_attempt_on), (2, None))
        self.assertEqual(self.attempt(9).attempts, 2)  # the limit holds
        self.assertIn("no message was sent", status.delivery)

    def test_after_the_limit_and_one_more_interval_it_escalates_to_hr_once(self) -> None:
        self.attempt(2), self.attempt(4)
        early = self.s.escalate_unanswered(HR_OPS, self.oid, self.tid, date(2026, 11, 5))
        self.assertEqual((early.state, early.follow_up_required), ("active", False))
        done = self.s.escalate_unanswered(HR_OPS, self.oid, self.tid, date(2026, 11, 6))
        self.assertEqual((done.state, done.follow_up_required), ("escalated", True))
        self.s.escalate_unanswered(HR_OPS, self.oid, self.tid, date(2026, 11, 7))
        events = [e for e in self.s.audit.events_for(HR_OPS) if e.action == "reminder.escalated"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].detail["escalate_to"], Role.HR_OFFICER.value)

    def test_a_manual_follow_up_outcome_is_recorded_only_when_one_is_outstanding(self) -> None:
        with self.assertRaises(InvalidStateError):
            self.s.record_follow_up_outcome(HR_OPS, self.oid, self.tid, "called_no_answer")
        self.attempt(2), self.attempt(4)
        self.s.escalate_unanswered(HR_OPS, self.oid, self.tid, date(2026, 11, 6))
        status = self.s.record_follow_up_outcome(HR_OPS, self.oid, self.tid, "called_no_answer")
        self.assertEqual((status.state, status.follow_up_outcome, status.follow_up_required),
                         ("follow_up_done", "called_no_answer", False))

    def test_a_response_stops_reminders_and_is_idempotent(self) -> None:
        self.attempt(2)
        self.s.record_employee_response(HR_OPS, self.oid, self.tid, date(2026, 11, 3))
        status = self.s.record_employee_response(HR_OPS, self.oid, self.tid, date(2026, 11, 3))
        self.assertEqual(status.state, "stopped_responded")
        self.assertEqual(self.attempt(8).attempts, 1)
        self.assertEqual(len([e for e in self.s.audit.events_for(HR_OPS)
                              if e.action == "reminder.employee_response"]), 1)

    def test_a_completed_task_stops_reminders(self) -> None:
        tid = self.s.get(HR_OPS, self.oid).task_by_key("hr_record_create").task_id
        self.s.start_task(HR_OPS, self.oid, tid)
        self.s.complete_task(HR_OPS, self.oid, tid)
        done = self.s.record_reminder_attempt(HR_OPS, self.oid, tid, D0)
        self.assertEqual((done.state, done.attempts), ("stopped_closed", 0))

    def test_history_survives_a_restart_of_the_status_read(self) -> None:
        self.attempt(2)
        again = self.s.reminder_status(HR_OPS, self.oid, self.tid)
        self.assertEqual((again.attempts, again.last_attempt_on), (1, date(2026, 11, 2)))

    def test_only_hr_may_drive_the_cycle_and_it_waits_for_plan_release(self) -> None:
        with self.assertRaises(UnauthorizedActionError):
            self.s.record_reminder_attempt(IT_MANAGER, self.oid, self.tid, D0)
        fresh = service_with(NonResponsePolicy(interval_days=1, max_attempts=1))
        oid = fresh.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).onboarding_id
        with self.assertRaises(Exception):
            fresh.record_reminder_attempt(HR_OPS, oid, fresh.get(HR_OPS, oid).tasks[0].task_id, D0)

    def test_policy_values_must_be_positive(self) -> None:
        for bad in ({"interval_days": 0, "max_attempts": 1}, {"interval_days": 1, "max_attempts": 0}):
            with self.assertRaises(ValueError):
                NonResponsePolicy(**bad)


class LocationRouteDL09Tests(unittest.TestCase):
    """The route comes from the client's templates; UC-02 only carries the location status."""

    CONFIG = ClientOnboardingConfig(
        client_id=ALPHA_CONFIG.client_id, version="dl09", approval_status="approved",
        templates=(
            TaskTemplate(key="status_change", category=TaskCategory.VISA, title="Visa status change",
                         title_ar="x", owner=Role.HR_OPS, offset_days=-10,
                         requires_attributes=("location_status:in_country",)),
            TaskTemplate(key="entry_permit", category=TaskCategory.VISA, title="Entry permit",
                         title_ar="x", owner=Role.HR_OPS, offset_days=-15,
                         requires_attributes=("location_status:overseas",)),
        ),
    )

    def keys(self, status: str) -> set[str]:
        registry = ConfigRegistry()
        registry.register(self.CONFIG)
        service = OnboardingService(registry)
        trigger = make_trigger(persona("EMP-001")).model_copy(update={"current_location_status": status})
        return {t.template_key for t in service.create_onboarding(HR_OPS, trigger).tasks}

    def test_in_country_gets_only_the_status_change_route(self) -> None:
        self.assertEqual(self.keys("in_country"), {"status_change"})

    def test_overseas_gets_only_the_entry_permit_route(self) -> None:
        self.assertEqual(self.keys("overseas"), {"entry_permit"})

    def test_an_unstated_location_selects_neither_route_and_is_never_guessed(self) -> None:
        with self.assertRaises(MissingConfigurationError):  # reported as a gap, not defaulted
            self.keys("")


if __name__ == "__main__":
    unittest.main()
