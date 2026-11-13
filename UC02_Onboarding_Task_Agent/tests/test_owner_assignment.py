"""UC02-BR-03 - HR manual task-owner assignment.

The agreed business rule is that **HR assigns the owner of each applicable
onboarding task**. There is no owner-assignment agent and no model in this
path: UC-02 records the assignment HR made, validates it, and tracks it.

The distinction these tests exist to protect
--------------------------------------------

Every task carries a configured owner *role* (``hr_ops``, ``it_manager``,
...). That role is **not** an assignment. It says which function the work
belongs to; it does not say that a person has been made responsible. A
system that treats the template default as "assigned" would report an
onboarding as fully staffed when in fact nobody has been asked to do
anything -- which is exactly the failure mode HR manual assignment exists to
prevent. Several tests below do nothing but hold that line.

Fixtures come from ``data/mock/owner_assignment_mock.json``, including the
assignments UC-02 must refuse.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from onboarding_task_management.contracts import (
    OPEN_STATUSES,
    Actor,
    AssignmentStatus,
    DecisionOutcome,
    Role,
    TaskStatus,
)
from onboarding_task_management.errors import (
    DataIsolationError,
    InvalidStateError,
    UnauthorizedActionError,
)
from onboarding_task_management.sub_agent import tracker
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import (
    CLIENT_ALPHA,
    build_registry,
    make_trigger,
    persona,
)
from tests.support import (
    HR_OPS,
    HR_OPS_BETA,
    IT_MANAGER,
)

MOCK_DIR = Path(__file__).resolve().parents[1] / "data" / "mock"
ASSIGNMENT_FIXTURE = MOCK_DIR / "owner_assignment_mock.json"


def load_assignments() -> dict:
    return json.loads(ASSIGNMENT_FIXTURE.read_text(encoding="utf-8"))


class AssignmentTestCase(unittest.TestCase):
    """EMP-007: the persona carrying every applicability attribute.

    Using the fully-loaded persona means the assignment surface under test is
    the real one -- visa, medical, insurance, travel, accommodation, food and
    banking-letter tasks all exist and all need an owner.
    """

    employee_id = "EMP-007"

    def setUp(self) -> None:
        self.service = OnboardingService(build_registry())
        created = self.service.create_onboarding(
            HR_OPS, make_trigger(persona(self.employee_id))
        )
        self.onboarding_id = created.onboarding_id
        self.fixture = load_assignments()

    @property
    def record(self):
        return self.service.get(HR_OPS, self.onboarding_id)

    def task_id(self, template_key: str) -> str:
        return f"{self.onboarding_id}#{template_key}"

    def task(self, template_key: str):
        task = self.record.task_by_key(template_key)
        assert task is not None, f"no task for {template_key!r}"
        return task

    def unassigned_keys(self) -> set[str]:
        return {
            t.template_key
            for t in self.service.unassigned_tasks(HR_OPS, self.onboarding_id)
        }

    def apply_fixture_assignments(self) -> list[str]:
        """Apply every assignment in the fixture. Returns the keys assigned."""
        applied = []
        for entry in self.fixture["assignments"]:
            key = entry["template_key"]
            if self.record.task_by_key(key) is None:
                continue  # not applicable to this persona
            self.service.assign_owner(
                HR_OPS,
                self.onboarding_id,
                self.task_id(key),
                entry["assignee_id"],
                team=entry["team"],
            )
            applied.append(key)
        return applied


# ---------------------------------------------------------------------------
# A role is not a person
# ---------------------------------------------------------------------------


class RoleIsNotAnAssignmentTests(AssignmentTestCase):
    def test_every_task_has_an_owner_role_from_the_start(self) -> None:
        """The precondition that makes the rest of this class meaningful."""
        self.assertTrue(self.record.tasks)
        for task in self.record.tasks:
            self.assertIsInstance(task.owner, Role)

    def test_but_no_task_starts_assigned(self) -> None:
        for task in self.record.tasks:
            with self.subTest(task=task.template_key):
                self.assertIsNone(task.assignee_id)
                self.assertFalse(task.is_assigned)

    def test_an_unassigned_task_reports_awaiting_hr_assignment(self) -> None:
        task = self.task("collect_documents")
        self.assertIs(
            task.assignment_status, AssignmentStatus.AWAITING_HR_ASSIGNMENT
        )

    def test_every_open_task_is_initially_unassigned(self) -> None:
        open_keys = {
            t.template_key for t in self.record.tasks if t.status in OPEN_STATUSES
        }
        self.assertEqual(self.unassigned_keys(), open_keys)
        self.assertTrue(open_keys, "persona should have open tasks")

    def test_assignment_does_not_change_the_configured_owner_role(self) -> None:
        before = self.task("collect_documents").owner
        self.service.assign_owner(
            HR_OPS, self.onboarding_id, self.task_id("collect_documents"), "U-HR-9"
        )
        self.assertIs(self.task("collect_documents").owner, before)


# ---------------------------------------------------------------------------
# Recording an assignment
# ---------------------------------------------------------------------------


class RecordingTests(AssignmentTestCase):
    def test_hr_assigns_a_valid_owner(self) -> None:
        task = self.service.assign_owner(
            HR_OPS,
            self.onboarding_id,
            self.task_id("collect_documents"),
            "U-HR-9",
            team="HR Shared Services",
        )
        self.assertEqual(task.assignee_id, "U-HR-9")
        self.assertEqual(task.assignee_team, "HR Shared Services")
        self.assertEqual(task.assigned_by, HR_OPS.actor_id)
        self.assertIsNotNone(task.assigned_at)
        self.assertIs(task.assignment_status, AssignmentStatus.ASSIGNED)

    def test_the_assignment_is_recorded_against_the_correct_task(self) -> None:
        """An assignment must not splash onto neighbouring tasks."""
        self.service.assign_owner(
            HR_OPS, self.onboarding_id, self.task_id("it_laptop"), "U-IT-7"
        )
        self.assertEqual(self.task("it_laptop").assignee_id, "U-IT-7")
        for key in ("collect_documents", "it_accounts", "payroll_setup"):
            with self.subTest(task=key):
                self.assertIsNone(self.task(key).assignee_id)

    def test_the_assignment_survives_a_reload(self) -> None:
        self.service.assign_owner(
            HR_OPS, self.onboarding_id, self.task_id("it_laptop"), "U-IT-7"
        )
        self.service.hydrate(CLIENT_ALPHA)
        self.assertEqual(self.task("it_laptop").assignee_id, "U-IT-7")

    def test_a_whitespace_padded_identifier_is_stored_trimmed(self) -> None:
        task = self.service.assign_owner(
            HR_OPS, self.onboarding_id, self.task_id("it_laptop"), "  U-IT-7  "
        )
        self.assertEqual(task.assignee_id, "U-IT-7")

    def test_assigning_is_audited_with_identifiers_only(self) -> None:
        self.service.assign_owner(
            HR_OPS, self.onboarding_id, self.task_id("it_laptop"), "U-IT-7"
        )
        events = [
            e
            for e in self.service.audit.events_for(HR_OPS)
            if e.action == "task.assign_owner"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].outcome, "assigned")
        self.assertEqual(events[0].detail["assignee_id"], "U-IT-7")
        self.assertEqual(events[0].detail["owner_role"], Role.IT_MANAGER.value)


# ---------------------------------------------------------------------------
# Detection of what HR has not yet done
# ---------------------------------------------------------------------------


class MissingOwnerDetectionTests(AssignmentTestCase):
    def test_assigning_removes_exactly_one_task_from_the_unassigned_list(self) -> None:
        before = self.unassigned_keys()
        self.service.assign_owner(
            HR_OPS, self.onboarding_id, self.task_id("it_laptop"), "U-IT-7"
        )
        self.assertEqual(before - self.unassigned_keys(), {"it_laptop"})

    def test_the_fixture_leaves_its_declared_tasks_unassigned(self) -> None:
        """The fixture deliberately under-assigns; detection must notice."""
        self.apply_fixture_assignments()
        expected = {
            key
            for key in self.fixture["_left_unassigned_deliberately"]["template_keys"]
            if self.record.task_by_key(key) is not None
        }
        self.assertTrue(expected)
        self.assertEqual(self.unassigned_keys(), expected)

    def test_unassigned_tasks_are_ordered_by_due_date(self) -> None:
        tasks = self.service.unassigned_tasks(HR_OPS, self.onboarding_id)
        self.assertEqual(
            [t.due_date for t in tasks], sorted(t.due_date for t in tasks)
        )

    def test_a_completed_task_is_not_reported_as_unassigned(self) -> None:
        """Settled work needs no assignee; it must not look like a gap."""
        self.service.complete_task(
            HR_OPS, self.onboarding_id, self.task_id("hr_record_create")
        )
        self.assertNotIn("hr_record_create", self.unassigned_keys())
        self.assertIs(
            self.task("hr_record_create").assignment_status,
            AssignmentStatus.NOT_REQUIRED,
        )

    def test_an_unassigned_open_task_raises_a_follow_up_flag(self) -> None:
        flags = self.service.follow_up_flags(
            HR_OPS, self.onboarding_id, date(2026, 1, 5)
        )
        unassigned = [f for f in flags if f.code == tracker.FLAG_UNASSIGNED]
        self.assertTrue(unassigned)
        flag = unassigned[0]
        self.assertIn("hr_ops", flag.follow_up_with)
        self.assertIn("not an assigned individual", flag.reason)

    def test_an_assigned_task_follows_up_with_the_assignee(self) -> None:
        self.service.assign_owner(
            HR_OPS,
            self.onboarding_id,
            self.task_id("it_laptop"),
            "U-IT-7",
            team="IT Service Desk",
        )
        # Make it overdue so it flags for a reason other than being unassigned.
        due = self.task("it_laptop").due_date
        flags = self.service.follow_up_flags(
            HR_OPS, self.onboarding_id, due + timedelta(days=5)
        )
        laptop = [
            f
            for f in flags
            if f.template_key == "it_laptop" and f.code == tracker.FLAG_OVERDUE
        ]
        self.assertEqual(len(laptop), 1)
        self.assertEqual(laptop[0].follow_up_with, "U-IT-7")
        self.assertEqual(laptop[0].assignee_team, "IT Service Desk")

    def test_a_follow_up_flag_is_not_a_notification(self) -> None:
        """UC-02 has no transport; a flag must never claim delivery."""
        flags = self.service.follow_up_flags(
            HR_OPS, self.onboarding_id, date(2026, 1, 5)
        )
        self.assertTrue(flags)
        for flag in flags:
            with self.subTest(task=flag.task_id, code=flag.code):
                self.assertFalse(flag.delivered)
                self.assertIn("no notification transport", flag.delivery_note.lower())


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class RefusalTests(AssignmentTestCase):
    def test_a_non_hr_role_may_not_assign(self) -> None:
        with self.assertRaises(UnauthorizedActionError):
            self.service.assign_owner(
                IT_MANAGER, self.onboarding_id, self.task_id("it_laptop"), "U-IT-7"
            )
        self.assertIsNone(self.task("it_laptop").assignee_id)

    def test_an_unauthorised_attempt_is_audited_as_denied(self) -> None:
        with self.assertRaises(UnauthorizedActionError):
            self.service.assign_owner(
                IT_MANAGER, self.onboarding_id, self.task_id("it_laptop"), "U-IT-7"
            )
        events = [
            e
            for e in self.service.audit.events_for(IT_MANAGER)
            if e.action == "task.assign_owner"
        ]
        self.assertEqual([e.outcome for e in events], ["denied"])

    def test_a_blank_assignee_is_not_an_assignment(self) -> None:
        for blank in ("", "   "):
            with self.subTest(value=repr(blank)):
                with self.assertRaises(UnauthorizedActionError):
                    self.service.assign_owner(
                        HR_OPS,
                        self.onboarding_id,
                        self.task_id("it_laptop"),
                        blank,
                    )
                self.assertIn("it_laptop", self.unassigned_keys())

    def test_an_unknown_task_id_is_rejected_not_created(self) -> None:
        before = len(self.record.tasks)
        with self.assertRaises(InvalidStateError):
            self.service.assign_owner(
                HR_OPS, self.onboarding_id, self.task_id("no_such_template"), "U-HR-9"
            )
        self.assertEqual(len(self.record.tasks), before)

    def test_another_clients_actor_cannot_assign(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.assign_owner(
                HR_OPS_BETA, self.onboarding_id, self.task_id("it_laptop"), "U-IT-7"
            )

    def test_the_fixtures_rejected_cases_are_all_refused(self) -> None:
        """Every case the fixture declares invalid must actually be refused."""
        errors = {
            "UnauthorizedActionError": UnauthorizedActionError,
            "InvalidStateError": InvalidStateError,
        }
        actors = {
            "hr_ops": HR_OPS,
            "it_manager": IT_MANAGER,
        }
        cases = self.fixture["_rejected_assignments"]["cases"]
        self.assertTrue(cases)
        for case in cases:
            with self.subTest(reason=case["reason"]):
                with self.assertRaises(errors[case["expect"]]):
                    self.service.assign_owner(
                        actors[case["acting_role"]],
                        self.onboarding_id,
                        self.task_id(case["template_key"]),
                        case["assignee_id"],
                    )


# ---------------------------------------------------------------------------
# Reassignment
# ---------------------------------------------------------------------------


class ReassignmentTests(AssignmentTestCase):
    def test_an_open_task_may_be_reassigned(self) -> None:
        self.service.assign_owner(
            HR_OPS, self.onboarding_id, self.task_id("it_laptop"), "U-IT-7"
        )
        task = self.service.assign_owner(
            HR_OPS,
            self.onboarding_id,
            self.task_id("it_laptop"),
            "U-IT-8",
            team="IT Identity",
        )
        self.assertEqual(task.assignee_id, "U-IT-8")
        self.assertEqual(task.assignee_team, "IT Identity")

    def test_every_assignment_leaves_its_own_audit_trail(self) -> None:
        """Who held the task before is recoverable from the log, not overwritten."""
        for assignee in ("U-IT-7", "U-IT-8", "U-IT-9"):
            self.service.assign_owner(
                HR_OPS, self.onboarding_id, self.task_id("it_laptop"), assignee
            )
        events = [
            e
            for e in self.service.audit.events_for(HR_OPS)
            if e.action == "task.assign_owner"
        ]
        self.assertEqual(
            [e.detail["assignee_id"] for e in events], ["U-IT-7", "U-IT-8", "U-IT-9"]
        )

    def test_a_completed_task_cannot_be_reassigned(self) -> None:
        self.service.assign_owner(
            HR_OPS, self.onboarding_id, self.task_id("hr_record_create"), "U-HR-9"
        )
        self.service.complete_task(
            HR_OPS, self.onboarding_id, self.task_id("hr_record_create")
        )
        with self.assertRaises(InvalidStateError):
            self.service.assign_owner(
                HR_OPS, self.onboarding_id, self.task_id("hr_record_create"), "U-HR-5"
            )
        self.assertEqual(self.task("hr_record_create").assignee_id, "U-HR-9")

    def test_a_cancelled_task_cannot_be_assigned(self) -> None:
        approver = Actor(
            actor_id="U-HR-2", role=Role.HR_OPS, client_id=CLIENT_ALPHA
        )
        request = self.service.request_cancellation(
            HR_OPS, self.onboarding_id, reason="candidate withdrew"
        )
        self.service.decide_change(
            approver, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_cancellation(HR_OPS, request.request_id)
        self.assertIs(self.task("it_laptop").status, TaskStatus.CANCELLED)
        with self.assertRaises(InvalidStateError):
            self.service.assign_owner(
                HR_OPS, self.onboarding_id, self.task_id("it_laptop"), "U-IT-7"
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
