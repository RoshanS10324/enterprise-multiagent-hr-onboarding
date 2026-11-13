"""Gap-closure regressions: identifiers, source authentication, audit, replan dates, config.

Everything here runs against the in-memory store. Nothing in this module
exercises PostgreSQL or Supabase; see ``test_persistence.py`` for the
(skipped-by-default) live-database test.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from onboarding_task_management.calendar_uae import UAE_PUBLIC_HOLIDAYS
from onboarding_task_management.contracts import (
    Actor,
    ChangeDriver,
    DecisionOutcome,
    ExternalOutcome,
    Role,
    SourceUseCase,
    StatusUpdate,
    TaskCategory,
    TaskStatus,
)
from onboarding_task_management.errors import DataIsolationError, MissingConfigurationError
from onboarding_task_management.sub_agent import mock_handoff
from onboarding_task_management.sub_agent.adapter import UC02SubAgent, WorkflowRequest
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    ConfigRegistry,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.sub_agent.status_updates import (
    REJECT_SOURCE_NOT_RESPONSIBLE,
    REJECT_SOURCE_UNAUTHENTICATED,
)
from onboarding_task_management.synthetic import (
    CLIENT_ALPHA,
    CLIENT_BETA,
    build_registry,
    make_trigger,
    persona,
)
from tests.support import HR_OPS, HR_OPS_APPROVER, HR_OPS_BETA

OCCURRED = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
UC03_AGENT = Actor(actor_id="svc-uc03", role=Role.SYSTEM, client_id=CLIENT_ALPHA)
UC04_AGENT = Actor(actor_id="svc-uc04", role=Role.SYSTEM, client_id=CLIENT_ALPHA)
PRINCIPALS = {
    SourceUseCase.UC03_DOCUMENT_VALIDATION: frozenset({"svc-uc03"}),
    SourceUseCase.UC04_EQUIPMENT_ACCESS: frozenset({"svc-uc04"}),
}


def _template(key: str, **kw) -> TaskTemplate:
    defaults = dict(
        category=TaskCategory.HR_RECORD,
        title=key,
        title_ar=key,
        owner=Role.HR_OPS,
        offset_days=0,
    )
    defaults.update(kw)
    return TaskTemplate(key=key, **defaults)


def _registry(**config_kw) -> ConfigRegistry:
    config_kw.setdefault("approval_status", "approved")  # explicit: unapproved configs are refused
    registry = ConfigRegistry()
    registry.register(
        ClientOnboardingConfig(
            client_id=CLIENT_ALPHA,
            templates=(
                _template("a_first"),
                _template("b_second", depends_on=("a_first",), sensitive_to=()),
            ),
            **config_kw,
        )
    )
    return registry


class SourceAuthenticationTests(unittest.TestCase):
    """UC-02 must not trust the caller-declared ``source`` field."""

    def setUp(self) -> None:
        self.service = OnboardingService(build_registry(), source_principals=PRINCIPALS)
        created = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"))
        )
        self.oid = created.onboarding_id
        self.service.complete_task(HR_OPS, self.oid, f"{self.oid}#hr_record_create")

    def _update(self, source=SourceUseCase.UC03_DOCUMENT_VALIDATION, outcome=ExternalOutcome.VALID):
        task = self.service.get(HR_OPS, self.oid).task_by_key("collect_documents")
        return mock_handoff.MOCK_ADAPTERS[source](task, outcome, OCCURRED)

    def test_registered_reporter_is_accepted_and_the_task_completes(self) -> None:
        result = self.service.apply_status_update(UC03_AGENT, self.oid, self._update())
        self.assertTrue(result.accepted, result.rejection_reason)
        self.assertEqual(result.applied_status, TaskStatus.COMPLETE)

    def test_hr_role_cannot_report_as_another_workflow(self) -> None:
        result = self.service.apply_status_update(HR_OPS, self.oid, self._update())
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection_code, REJECT_SOURCE_UNAUTHENTICATED)
        task = self.service.get(HR_OPS, self.oid).task_by_key("collect_documents")
        self.assertNotEqual(task.status, TaskStatus.COMPLETE)
        self.assertIsNone(task.external_outcome)

    def test_a_registered_reporter_cannot_claim_a_different_source(self) -> None:
        # svc-uc04 is real, but it is not registered for UC-03.
        result = self.service.apply_status_update(UC04_AGENT, self.oid, self._update())
        self.assertEqual(result.rejection_code, REJECT_SOURCE_UNAUTHENTICATED)

    def test_a_source_with_no_registered_principal_fails_closed(self) -> None:
        update = self._update(source=SourceUseCase.UC05_ORIENTATION)
        result = self.service.apply_status_update(UC03_AGENT, self.oid, update)
        self.assertEqual(result.rejection_code, REJECT_SOURCE_UNAUTHENTICATED)

    def test_authenticated_reporter_is_still_limited_to_its_own_categories(self) -> None:
        # svc-uc04 is authenticated as UC-04 but may not close a document task.
        update = self._update(source=SourceUseCase.UC04_EQUIPMENT_ACCESS)
        result = self.service.apply_status_update(UC04_AGENT, self.oid, update)
        self.assertEqual(result.rejection_code, REJECT_SOURCE_NOT_RESPONSIBLE)

    def test_duplicate_delivery_is_applied_once(self) -> None:
        update = self._update()
        first = self.service.apply_status_update(UC03_AGENT, self.oid, update)
        revision_after_first = self.service.get(HR_OPS, self.oid).updated_at
        second = self.service.apply_status_update(UC03_AGENT, self.oid, update)
        self.assertTrue(first.accepted and second.accepted)
        self.assertFalse(first.duplicate_replay)
        self.assertTrue(second.duplicate_replay)
        self.assertEqual(self.service.get(HR_OPS, self.oid).updated_at, revision_after_first)

    def test_another_clients_reporter_is_isolated(self) -> None:
        other = Actor(actor_id="svc-uc03", role=Role.SYSTEM, client_id=CLIENT_BETA)
        with self.assertRaises(DataIsolationError):
            self.service.apply_status_update(other, self.oid, self._update())

    def test_the_audit_event_records_before_after_and_verification(self) -> None:
        self.service.apply_status_update(UC03_AGENT, self.oid, self._update())
        event = [
            e for e in self.service.audit.events_for(UC03_AGENT)
            if e.action == "task.status_update"
        ][-1]
        self.assertEqual(event.outcome, "accepted")
        self.assertEqual(event.actor_id, "svc-uc03")
        self.assertEqual(event.before_status, TaskStatus.READY.value)
        self.assertEqual(event.after_status, TaskStatus.COMPLETE.value)
        self.assertTrue(event.task_id.endswith("#collect_documents"))
        self.assertEqual(event.detail["source_verification"], "verified")
        self.assertIsNotNone(event.at.tzinfo)

    def test_rejection_is_audited_with_the_code(self) -> None:
        self.service.apply_status_update(HR_OPS, self.oid, self._update())
        event = [
            e for e in self.service.audit.events_for(HR_OPS)
            if e.action == "task.status_update"
        ][-1]
        self.assertEqual(event.outcome, "rejected")
        self.assertEqual(event.detail["rejection_code"], REJECT_SOURCE_UNAUTHENTICATED)

    def test_without_principals_no_update_is_trusted(self) -> None:
        """No principals configured means no reporter is trusted: there is no fallback."""
        service = OnboardingService(build_registry())
        oid = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).onboarding_id
        service.complete_task(HR_OPS, oid, f"{oid}#hr_record_create")
        task = service.get(HR_OPS, oid).task_by_key("collect_documents")
        update = mock_handoff.MOCK_ADAPTERS[SourceUseCase.UC03_DOCUMENT_VALIDATION](
            task, ExternalOutcome.VALID, OCCURRED
        )
        result = service.apply_status_update(HR_OPS, oid, update)
        self.assertEqual(result.rejection_code, REJECT_SOURCE_UNAUTHENTICATED)
        event = [e for e in service.audit.events_for(HR_OPS) if e.action == "task.status_update"][-1]
        self.assertEqual(event.detail["source_verification"], "rejected_no_principals_configured")
        self.assertNotEqual(service.get(HR_OPS, oid).task_by_key("collect_documents").status, TaskStatus.COMPLETE)


class SharedIdentifierAndAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = OnboardingService(build_registry())
        trigger = make_trigger(persona("EMP-001")).model_copy(
            update={"case_id": "CASE-1", "correlation_id": "corr-9"}
        )
        self.record = self.service.create_onboarding(HR_OPS, trigger)
        self.oid = self.record.onboarding_id

    def test_case_and_correlation_ids_are_stored_not_invented(self) -> None:
        self.assertEqual(self.record.case_id, "CASE-1")
        self.assertEqual(self.record.correlation_id, "corr-9")
        plain = OnboardingService(build_registry()).create_onboarding(
            HR_OPS, make_trigger(persona("EMP-002"))
        )
        self.assertIsNone(plain.case_id)
        self.assertIsNone(plain.correlation_id)

    def test_every_event_for_the_case_carries_the_correlation_id(self) -> None:
        self.service.start_task(HR_OPS, self.oid, f"{self.oid}#hr_record_create")
        events = [e for e in self.service.audit.events_for(HR_OPS) if e.onboarding_id == self.oid]
        self.assertGreaterEqual(len(events), 2)
        self.assertTrue(all(e.correlation_id == "corr-9" for e in events))

    def test_start_and_complete_record_status_transitions(self) -> None:
        task_id = f"{self.oid}#hr_record_create"
        self.service.start_task(HR_OPS, self.oid, task_id)
        self.service.complete_task(HR_OPS, self.oid, task_id)
        by_action = {e.action: e for e in self.service.audit.events_for(HR_OPS) if e.task_id == task_id}
        self.assertEqual(
            (by_action["task.start"].before_status, by_action["task.start"].after_status),
            ("ready", "in_progress"),
        )
        self.assertEqual(
            (by_action["task.complete"].before_status, by_action["task.complete"].after_status),
            ("in_progress", "complete"),
        )

    def test_reassignment_records_previous_and_new_assignee(self) -> None:
        task_id = f"{self.oid}#hr_record_create"
        self.service.assign_owner(HR_OPS, self.oid, task_id, "U-A")
        self.service.assign_owner(HR_OPS, self.oid, task_id, "U-B")
        events = [e for e in self.service.audit.events_for(HR_OPS) if e.action == "task.assign_owner" and e.outcome == "assigned"]
        self.assertEqual([(e.previous_assignee_id, e.current_assignee_id) for e in events],
                         [(None, "U-A"), ("U-A", "U-B")])
        # An assignee is not a status: the status fields stay empty for an assignment.
        self.assertTrue(all(e.before_status is None and e.after_status is None for e in events))

    def test_approval_decision_records_the_approver(self) -> None:
        request = self.service.request_change(HR_OPS, self.oid, joining_date=date(2026, 11, 30))
        self.service.decide_change(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        event = [e for e in self.service.audit.events_for(HR_OPS) if e.action == "change.decide"][-1]
        self.assertEqual(event.approver_id, HR_OPS_APPROVER.actor_id)
        self.assertEqual(event.actor_id, HR_OPS_APPROVER.actor_id)

    def test_audit_still_refuses_personal_data_keys(self) -> None:
        with self.assertRaises(ValueError):
            self.service.audit.record(HR_OPS, "x", "y", email="a@b.c")


class ReplanDueDatePropagationTests(unittest.TestCase):
    def test_a_dependent_that_is_not_date_sensitive_follows_its_moved_prerequisite(self) -> None:
        service = OnboardingService(_registry())
        created = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        oid = created.onboarding_id
        a, b = f"{oid}#a_first", f"{oid}#b_second"
        before = {t.task_id: t.due_date for t in created.tasks}
        self.assertEqual(before[a], before[b])

        request = service.request_change(HR_OPS, oid, joining_date=date(2026, 11, 30))
        service.decide_change(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        outcome = service.apply_change(HR_OPS, request.request_id)

        after = {t.task_id: t.due_date for t in service.get(HR_OPS, oid).tasks}
        self.assertGreater(after[a], before[a])
        self.assertGreaterEqual(after[b], after[a])
        self.assertIn(b, outcome.rescheduled_task_ids)
        self.assertNotIn(b, outcome.retained_task_ids)

    def test_a_completed_dependent_is_never_moved(self) -> None:
        service = OnboardingService(_registry())
        created = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        oid = created.onboarding_id
        a, b = f"{oid}#a_first", f"{oid}#b_second"
        service.complete_task(HR_OPS, oid, a)
        service.complete_task(HR_OPS, oid, b)
        b_due = service.get(HR_OPS, oid).task_by_id(b).due_date
        request = service.request_change(HR_OPS, oid, joining_date=date(2026, 11, 30))
        service.decide_change(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        service.apply_change(HR_OPS, request.request_id)
        self.assertEqual(service.get(HR_OPS, oid).task_by_id(b).due_date, b_due)

    def test_unrelated_tasks_are_not_rewritten(self) -> None:
        """Only dependents of a changed task move; the rest of the plan is untouched."""
        registry = ConfigRegistry()
        registry.register(
            ClientOnboardingConfig(
                client_id=CLIENT_ALPHA,
                approval_status="approved",
                templates=(
                    _template("moves"),
                    _template("late_dependent_of_fixed", offset_days=-5, sensitive_to=(ChangeDriver.PROJECT,)),
                    _template("fixed", offset_days=5, sensitive_to=(ChangeDriver.PROJECT,)),
                ),
            )
        )
        service = OnboardingService(registry)
        created = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        oid = created.onboarding_id
        untouched = f"{oid}#fixed"
        before = service.get(HR_OPS, oid).task_by_id(untouched).due_date
        request = service.request_change(HR_OPS, oid, joining_date=date(2026, 11, 30))
        service.decide_change(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
        service.apply_change(HR_OPS, request.request_id)
        self.assertEqual(service.get(HR_OPS, oid).task_by_id(untouched).due_date, before)


class ConfigurationVersionTests(unittest.TestCase):
    def _create(self, registry):
        return OnboardingService(registry).create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"))
        )

    def test_the_configuration_version_is_recorded_on_the_onboarding(self) -> None:
        record = self._create(_registry(version="2026.2", approval_status="approved"))
        self.assertEqual(record.config_version, "2026.2")

    def test_a_draft_configuration_is_refused(self) -> None:
        with self.assertRaises(MissingConfigurationError):
            self._create(_registry(version="d1", approval_status="draft"))

    def test_a_retired_configuration_is_refused(self) -> None:
        with self.assertRaises(MissingConfigurationError):
            self._create(_registry(approval_status="retired"))

    def test_a_configuration_not_yet_effective_on_the_joining_date_is_refused(self) -> None:
        with self.assertRaises(MissingConfigurationError):
            self._create(_registry(effective_from=date(2027, 1, 1)))

    def test_a_configuration_that_has_expired_before_joining_is_refused(self) -> None:
        with self.assertRaises(MissingConfigurationError):
            self._create(_registry(effective_to=date(2026, 6, 30)))

    def test_a_configuration_in_force_on_the_joining_date_is_used(self) -> None:
        record = self._create(
            _registry(effective_from=date(2026, 1, 1), effective_to=date(2026, 12, 31), version="v1")
        )
        self.assertEqual(len(record.tasks), 2)

    def test_a_refused_configuration_creates_no_onboarding(self) -> None:
        service = OnboardingService(_registry(approval_status="draft"))
        with self.assertRaises(MissingConfigurationError):
            service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        self.assertEqual(service._client_records(CLIENT_ALPHA), {})


class ClientCalendarTests(unittest.TestCase):
    def test_a_client_specific_holiday_moves_that_clients_due_dates_only(self) -> None:
        baseline = OnboardingService(build_registry())
        base_task = baseline.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).task_by_key("hr_record_create")
        extra = base_task.due_date
        custom = OnboardingService(
            build_registry(),
            holidays_by_client={CLIENT_ALPHA: UAE_PUBLIC_HOLIDAYS | {extra}},
        )
        moved = custom.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).task_by_key("hr_record_create")
        self.assertLess(moved.due_date, extra)
        self.assertEqual(custom._holidays_for(CLIENT_BETA), custom.holidays)


class AdapterIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        service = OnboardingService(build_registry(), source_principals=PRINCIPALS)
        self.agent = UC02SubAgent(service=service)
        trigger = make_trigger(persona("EMP-001")).model_dump(mode="json")
        trigger.update(case_id="CASE-77", correlation_id="corr-77")
        self.result = self.agent.run(
            WorkflowRequest(trigger=trigger, as_of=date(2026, 11, 2), llm_behaviour="offline")
        )
        self.oid = self.result.onboarding_id
        service.complete_task(HR_OPS, self.oid, f"{self.oid}#hr_record_create")

    def _payload(self, **over):
        task = self.agent.service.get(HR_OPS, self.oid).task_by_key("collect_documents")
        payload = mock_handoff.MOCK_ADAPTERS[SourceUseCase.UC03_DOCUMENT_VALIDATION](
            task, ExternalOutcome.VALID, OCCURRED
        ).model_dump(mode="json")
        payload.update(over)
        return payload

    def test_the_result_echoes_the_shared_identifiers(self) -> None:
        self.assertEqual(self.result.case_id, "CASE-77")
        self.assertEqual(self.result.correlation_id, "corr-77")
        self.assertEqual(self.result.as_dict()["correlation_id"], "corr-77")

    def test_an_inbound_payload_is_validated_and_applied_for_the_authenticated_caller(self) -> None:
        result = self.agent.apply_status_update(
            self._payload(), client_id=CLIENT_ALPHA, actor_id="svc-uc03"
        )
        self.assertTrue(result.accepted)

    def test_the_payload_cannot_choose_the_tenant(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.agent.apply_status_update(
                self._payload(), client_id=CLIENT_BETA, actor_id="svc-uc03"
            )

    def test_an_unknown_caller_is_rejected(self) -> None:
        result = self.agent.apply_status_update(
            self._payload(), client_id=CLIENT_ALPHA, actor_id="someone-else"
        )
        self.assertEqual(result.rejection_code, REJECT_SOURCE_UNAUTHENTICATED)

    def test_a_malformed_payload_is_rejected_before_any_effect(self) -> None:
        with self.assertRaises(ValueError):
            self.agent.apply_status_update(
                self._payload(outcome="not-an-outcome"), client_id=CLIENT_ALPHA, actor_id="svc-uc03"
            )
        task = self.agent.service.get(HR_OPS, self.oid).task_by_key("collect_documents")
        self.assertIsNone(task.external_outcome)

    def test_a_second_client_cannot_read_or_touch_the_case(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.agent.service.get(HR_OPS_BETA, self.oid)


if __name__ == "__main__":
    unittest.main()
