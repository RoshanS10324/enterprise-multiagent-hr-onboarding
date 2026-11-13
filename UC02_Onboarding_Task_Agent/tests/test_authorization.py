"""Unauthorized actions, client data isolation, and audit logging."""

from __future__ import annotations

import unittest

from onboarding_task_management.audit import AuditLog
from onboarding_task_management.contracts import TaskStatus
from onboarding_task_management.errors import (
    DataIsolationError,
    InvalidStateError,
    UnauthorizedActionError,
)
from onboarding_task_management.synthetic import (
    CLIENT_ALPHA,
    CLIENT_BETA,
    make_trigger,
    persona,
)
from tests.support import (
    EMPLOYEE,
    FACILITIES,
    HR_LEAD,
    HR_OPS,
    HR_OPS_BETA,
    IT_MANAGER,
    ServiceTestCase,
)


class TaskAuthorizationTests(ServiceTestCase):
    def test_non_owner_may_not_complete_a_task(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        with self.assertRaises(UnauthorizedActionError) as ctx:
            self.complete(EMPLOYEE, "it_laptop")
        self.assertIn("may not act on this task", str(ctx.exception))
        self.assertEqual(self.task("it_laptop").status, TaskStatus.READY)

    def test_non_owner_may_not_start_a_task(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        with self.assertRaises(UnauthorizedActionError):
            self.service.start_task(
                FACILITIES, self.onboarding_id, self.task_id("it_laptop")
            )

    def test_owner_may_act_on_their_own_task(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(IT_MANAGER, "it_laptop")
        self.assertEqual(self.task("it_laptop").status, TaskStatus.COMPLETE)

    def test_admin_role_may_act_on_any_task(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        self.complete(HR_LEAD, "it_laptop")
        self.assertEqual(self.task("it_laptop").status, TaskStatus.COMPLETE)

    def test_employee_may_not_create_an_onboarding(self) -> None:
        with self.assertRaises(UnauthorizedActionError) as ctx:
            self.service.create_onboarding(EMPLOYEE, make_trigger(persona("EMP-002")))
        self.assertIn("only HR roles", str(ctx.exception))

    def test_unauthorized_attempt_is_audited(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        with self.assertRaises(UnauthorizedActionError):
            self.complete(EMPLOYEE, "it_laptop")
        denials = [
            e
            for e in self.service.audit.events_for(HR_OPS)
            if e.outcome == "denied"
        ]
        self.assertTrue(denials)
        self.assertEqual(denials[-1].detail["error"], "UnauthorizedActionError")

    def test_unknown_task_is_rejected(self) -> None:
        with self.assertRaises(InvalidStateError):
            self.service.start_task(HR_OPS, self.onboarding_id, "nope")


class DataIsolationTests(ServiceTestCase):
    def test_another_client_cannot_read_the_record(self) -> None:
        with self.assertRaises(DataIsolationError) as ctx:
            self.service.get(HR_OPS_BETA, self.onboarding_id)
        self.assertIn("another client", str(ctx.exception))

    def test_another_client_cannot_complete_a_task(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.complete_task(
                HR_OPS_BETA, self.onboarding_id, self.task_id("hr_record_create")
            )

    def test_another_client_cannot_request_a_change(self) -> None:
        from datetime import date

        with self.assertRaises(DataIsolationError):
            self.service.request_change(
                HR_OPS_BETA, self.onboarding_id, joining_date=date(2026, 12, 7)
            )

    def test_actor_cannot_create_for_another_client(self) -> None:
        with self.assertRaises(DataIsolationError) as ctx:
            self.service.create_onboarding(HR_OPS_BETA, make_trigger(persona("EMP-002")))
        self.assertIn("another client", str(ctx.exception))

    def test_isolation_breach_is_audited_against_the_calling_client(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.get(HR_OPS_BETA, self.onboarding_id)
        beta_events = self.service.audit.events_for(HR_OPS_BETA)
        self.assertIn("denied_isolation", [e.outcome for e in beta_events])
        # The breach attempt must not appear in the victim client's log.
        alpha_outcomes = [
            e.outcome for e in self.service.audit.events_for(HR_OPS)
        ]
        self.assertNotIn("denied_isolation", alpha_outcomes)

    def test_each_client_sees_only_its_own_records(self) -> None:
        beta = self.service.create_onboarding(
            HR_OPS_BETA, make_trigger(persona("EMP-006"))
        )
        self.assertEqual(
            {t.template_key for t in beta.tasks}, {"hr_record_create", "it_accounts"}
        )
        with self.assertRaises(DataIsolationError):
            self.service.get(HR_OPS, beta.onboarding_id)

    def test_unknown_onboarding_is_not_reported_as_isolation(self) -> None:
        with self.assertRaises(InvalidStateError):
            self.service.get(HR_OPS, "ONB-CLIENT-ALPHA-NOBODY")


class AuditLogTests(ServiceTestCase):
    def test_lifecycle_actions_are_recorded(self) -> None:
        self.complete(HR_OPS, "hr_record_create")
        actions = self.service.audit.actions_for(HR_OPS)
        self.assertIn("onboarding.create", actions)
        self.assertIn("task.complete", actions)

    def test_events_are_append_only_and_sequenced(self) -> None:
        before = len(self.service.audit)
        self.complete(HR_OPS, "hr_record_create")
        events = self.service.audit.events_for(HR_OPS)
        self.assertGreater(len(self.service.audit), before)
        ids = [e.event_id for e in events]
        self.assertEqual(ids, sorted(ids))

    def test_events_carry_actor_and_ids_only(self) -> None:
        event = self.service.audit.events_for(HR_OPS)[0]
        self.assertEqual(event.actor_id, HR_OPS.actor_id)
        self.assertEqual(event.onboarding_id, self.onboarding_id)
        self.assertEqual(event.detail["task_count"], "10")

    def test_personal_data_keys_are_refused(self) -> None:
        log = AuditLog()
        for key in ("name", "email", "nationality", "passport"):
            with self.assertRaises(ValueError, msg=key):
                log.record(HR_OPS, "test", "ok", **{key: "x"})

    def test_audit_is_partitioned_by_client(self) -> None:
        self.service.create_onboarding(HR_OPS_BETA, make_trigger(persona("EMP-006")))
        alpha = self.service.audit.events_for(HR_OPS)
        beta = self.service.audit.events_for(HR_OPS_BETA)
        self.assertTrue(alpha and beta)
        self.assertTrue(all(e.client_id == CLIENT_ALPHA for e in alpha))
        self.assertTrue(all(e.client_id == CLIENT_BETA for e in beta))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
