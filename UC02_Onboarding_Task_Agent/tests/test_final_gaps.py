"""Source verification, configuration approval, client calendars, hr_record_create, contracts.

In-memory only. Principals and configurations below are TEST-ONLY fixtures: no real
service identity, calendar or client approval exists yet, and none is implied here.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from onboarding_task_management.calendar_uae import (
    UAE_PUBLIC_HOLIDAYS,
    WorkingCalendar,
    calendar_status,
)
from onboarding_task_management.contracts import (
    Actor,
    ExternalOutcome,
    Role,
    SourceUseCase,
    TaskCategory,
    TaskStatus,
)
from onboarding_task_management.errors import (
    CalendarConfigurationError,
    CalendarRangeError,
    DataIsolationError,
    InvalidStateError,
    MissingConfigurationError,
    UnauthorizedActionError,
)
from onboarding_task_management.sub_agent import status_updates
from onboarding_task_management.sub_agent.adapter import UC02SubAgent, WorkflowRequest
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    ConfigRegistry,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import (
    CLIENT_ALPHA,
    CLIENT_BETA,
    build_registry,
    make_trigger,
    persona,
)
from tests.support import EMPLOYEE, HR_OPS, HR_OPS_BETA

OCCURRED = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
UC03, UC04, UC05 = "svc-uc03", "svc-uc04", "svc-uc05"
TEST_PRINCIPALS = {  # TEST-ONLY identities
    SourceUseCase.UC03_DOCUMENT_VALIDATION: frozenset({UC03}),
    SourceUseCase.UC04_EQUIPMENT_ACCESS: frozenset({UC04}),
    SourceUseCase.UC05_ORIENTATION: frozenset({UC05}),
}


def _actor(actor_id: str, client: str = CLIENT_ALPHA) -> Actor:
    return Actor(actor_id=actor_id, role=Role.SYSTEM, client_id=client)


def _payload(oid, key, source, outcome, *, update_id=None, **extra):
    payload = dict(
        update_id=update_id or f"u-{key}-{outcome}",
        source=source,
        client_id=CLIENT_ALPHA,
        onboarding_id=oid,
        task_id=f"{oid}#{key}",
        outcome=outcome,
        occurred_at=OCCURRED.isoformat(),
    )
    payload.update(extra)
    return payload


class _Base(unittest.TestCase):
    employee = "EMP-001"

    def setUp(self) -> None:
        self.service = OnboardingService(build_registry(), source_principals=TEST_PRINCIPALS)
        self.agent = UC02SubAgent(service=self.service)
        trigger = make_trigger(persona(self.employee)).model_dump(mode="json")
        trigger.update(case_id="CASE-F1", correlation_id="corr-F1")
        self.oid = self.agent.run(
            WorkflowRequest(trigger=trigger, as_of=date(2026, 11, 2), llm_behaviour="offline")
        ).onboarding_id

    def task(self, key):
        return self.service.get(HR_OPS, self.oid).task_by_key(key)

    def complete_root(self):
        self.service.complete_task(HR_OPS, self.oid, f"{self.oid}#hr_record_create")

    def report(self, key, source, outcome, caller, **extra):
        return self.agent.apply_status_update(
            _payload(self.oid, key, source, outcome, **extra),
            client_id=CLIENT_ALPHA, actor_id=caller,
        )

    def last_verification(self):
        events = [e for e in self.service.audit.events_for(HR_OPS) if e.action == "task.status_update"]
        return events[-1].detail["source_verification"]


# --------------------------------------------------------------------------- Phase 2
class SourceVerificationTests(_Base):
    def test_missing_source_identity_is_rejected(self) -> None:
        self.complete_root()
        result = self.report("collect_documents", "uc03_document_validation", "valid", "   ")
        self.assertEqual(result.rejection_code, status_updates.REJECT_SOURCE_UNAUTHENTICATED)
        self.assertEqual(self.last_verification(), status_updates.UNVERIFIED_MISSING_IDENTITY)

    def test_unregistered_caller_is_rejected(self) -> None:
        self.complete_root()
        result = self.report("collect_documents", "uc03_document_validation", "valid", "stranger")
        self.assertEqual(result.rejection_code, status_updates.REJECT_SOURCE_UNAUTHENTICATED)
        self.assertEqual(self.last_verification(), status_updates.UNVERIFIED_CALLER_NOT_REGISTERED)

    def test_source_with_no_registered_identity_is_rejected(self) -> None:
        service = OnboardingService(
            build_registry(),
            source_principals={SourceUseCase.UC04_EQUIPMENT_ACCESS: frozenset({UC04})},
        )
        agent = UC02SubAgent(service=service)
        oid = agent.run(WorkflowRequest(
            trigger=make_trigger(persona("EMP-001")).model_dump(mode="json"),
            as_of=date(2026, 11, 2), llm_behaviour="offline")).onboarding_id
        service.complete_task(HR_OPS, oid, f"{oid}#hr_record_create")
        result = agent.apply_status_update(
            _payload(oid, "collect_documents", "uc03_document_validation", "valid"),
            client_id=CLIENT_ALPHA, actor_id=UC03)
        self.assertEqual(result.rejection_code, status_updates.REJECT_SOURCE_UNAUTHENTICATED)
        event = [e for e in service.audit.events_for(HR_OPS) if e.action == "task.status_update"][-1]
        self.assertEqual(event.detail["source_verification"], status_updates.UNVERIFIED_UNREGISTERED_SOURCE)

    def test_spoofed_source_identity_is_rejected(self) -> None:
        # svc-uc04 is genuine, but the payload claims to be UC-03 to close a document task.
        self.complete_root()
        result = self.report("collect_documents", "uc03_document_validation", "valid", UC04)
        self.assertEqual(result.rejection_code, status_updates.REJECT_SOURCE_UNAUTHENTICATED)
        self.assertNotEqual(self.task("collect_documents").status, TaskStatus.COMPLETE)
        self.assertIsNone(self.task("collect_documents").external_outcome)

    def test_a_default_service_trusts_nobody(self) -> None:
        service = OnboardingService(build_registry())
        self.assertIsNone(service.source_principals)
        oid = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001"))).onboarding_id
        service.complete_task(HR_OPS, oid, f"{oid}#hr_record_create")
        task = service.get(HR_OPS, oid).task_by_key("collect_documents")
        from onboarding_task_management.sub_agent import mock_handoff
        update = mock_handoff.MOCK_ADAPTERS[SourceUseCase.UC03_DOCUMENT_VALIDATION](
            task, ExternalOutcome.VALID, OCCURRED)
        for actor in (HR_OPS, _actor(UC03)):
            self.assertEqual(
                service.apply_status_update(actor, oid, update).rejection_code,
                status_updates.REJECT_SOURCE_UNAUTHENTICATED,
            )

    def test_the_payload_alone_never_establishes_identity(self) -> None:
        self.complete_root()
        payload = _payload(self.oid, "collect_documents", "uc03_document_validation", "valid")
        payload["actor_id"] = UC03  # extra claimed identity in the body is ignored
        result = self.agent.apply_status_update(payload, client_id=CLIENT_ALPHA, actor_id="stranger")
        self.assertFalse(result.accepted)

    def test_a_verified_caller_is_recorded_as_verified(self) -> None:
        self.complete_root()
        result = self.report("collect_documents", "uc03_document_validation", "valid", UC03)
        self.assertTrue(result.accepted)
        self.assertEqual(self.last_verification(), status_updates.VERIFIED)


# --------------------------------------------------------------------------- Phase 3
def _cfg(**kw) -> ConfigRegistry:
    registry = ConfigRegistry()
    registry.register(ClientOnboardingConfig(
        client_id=CLIENT_ALPHA,
        templates=(TaskTemplate(key="a", category=TaskCategory.HR_RECORD, title="a", title_ar="a",
                                owner=Role.HR_OPS, offset_days=0),),
        **kw,
    ))
    return registry


class ConfigurationApprovalTests(unittest.TestCase):
    def create(self, registry):
        return OnboardingService(registry).create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))

    def test_explicitly_approved_and_versioned_configuration_is_accepted(self) -> None:
        record = self.create(_cfg(approval_status="approved", version="2026.3"))
        self.assertEqual(record.config_version, "2026.3")
        self.assertEqual(len(record.tasks), 1)

    def test_missing_approval_status_is_rejected(self) -> None:
        with self.assertRaises(MissingConfigurationError) as ctx:
            self.create(_cfg())
        self.assertIn("not approved", ctx.exception.message)

    def test_unknown_approval_status_is_rejected_when_built(self) -> None:
        with self.assertRaises(ValueError):
            _cfg(approval_status="signed-off")
        with self.assertRaises(ValueError):
            _cfg(approval_status="APPROVED")  # no case-insensitive guessing

    def test_draft_and_retired_are_rejected(self) -> None:
        for status in ("draft", "retired"):
            with self.subTest(status=status), self.assertRaises(MissingConfigurationError):
                self.create(_cfg(approval_status=status))

    def test_expired_and_not_yet_effective_configurations_are_rejected(self) -> None:
        with self.assertRaises(MissingConfigurationError):
            self.create(_cfg(approval_status="approved", effective_to=date(2026, 6, 30)))
        with self.assertRaises(MissingConfigurationError):
            self.create(_cfg(approval_status="approved", effective_from=date(2027, 1, 1)))

    def test_effective_window_edges_are_inclusive(self) -> None:
        joining = date(2026, 11, 16)
        self.create(_cfg(approval_status="approved", effective_from=joining, effective_to=joining))

    def test_nothing_is_created_for_a_rejected_configuration(self) -> None:
        service = OnboardingService(_cfg(approval_status="draft"))
        with self.assertRaises(MissingConfigurationError):
            service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        self.assertEqual(service._client_records(CLIENT_ALPHA), {})

    def test_every_other_read_path_also_refuses_an_unapproved_configuration(self) -> None:
        registry = _cfg(approval_status="draft")
        with self.assertRaises(MissingConfigurationError):
            registry.get(CLIENT_ALPHA)


# --------------------------------------------------------------------------- Phase 4
def _two_client_registry() -> ConfigRegistry:
    registry = ConfigRegistry()
    for client in ("CLIENT-X", "CLIENT-Y"):
        registry.register(ClientOnboardingConfig(
            client_id=client, approval_status="approved",
            templates=(TaskTemplate(key="prep", category=TaskCategory.HR_RECORD, title="p", title_ar="p",
                                    owner=Role.HR_OPS, offset_days=-1),),
        ))
    return registry


SAT_SUN = WorkingCalendar(frozenset(), name="x-sat-sun", weekend_weekdays={5, 6})
FRI_SAT = WorkingCalendar(frozenset(), name="y-fri-sat", weekend_weekdays={4, 5})
MONDAY = date(2026, 11, 16)


def _create(service, client):
    trigger = make_trigger(persona("EMP-001"), joining_date=MONDAY).model_copy(update={"client_id": client})
    actor = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id=client)
    return service.create_onboarding(actor, trigger).task_by_key("prep").due_date


class ClientCalendarTests(unittest.TestCase):
    def service(self, **kw):
        return OnboardingService(
            _two_client_registry(), calendars={"CLIENT-X": SAT_SUN, "CLIENT-Y": FRI_SAT}, **kw
        )

    def test_two_clients_with_different_weekends_get_different_due_dates(self) -> None:
        service = self.service()
        self.assertEqual(_create(service, "CLIENT-X"), date(2026, 11, 13))  # Fri: day before Monday
        self.assertEqual(_create(service, "CLIENT-Y"), date(2026, 11, 15))  # Sun is a working day

    def test_a_client_holiday_shifts_only_that_clients_due_date(self) -> None:
        with_holiday = WorkingCalendar({date(2026, 11, 13)}, name="x-hol", weekend_weekdays={5, 6})
        service = OnboardingService(
            _two_client_registry(), calendars={"CLIENT-X": with_holiday, "CLIENT-Y": SAT_SUN})
        self.assertEqual(_create(service, "CLIENT-X"), date(2026, 11, 12))
        self.assertEqual(_create(service, "CLIENT-Y"), date(2026, 11, 13))

    def test_weekend_joining_date_snaps_using_the_clients_own_weekend(self) -> None:
        # Zero offset snaps forward to the client's first working day.
        registry = ConfigRegistry()
        registry.register(ClientOnboardingConfig(
            client_id="CLIENT-Y", approval_status="approved",
            templates=(TaskTemplate(key="day1", category=TaskCategory.HR_RECORD, title="d", title_ar="d",
                                    owner=Role.HR_OPS, offset_days=0),)))
        service = OnboardingService(registry, calendars={"CLIENT-Y": FRI_SAT})
        friday = date(2026, 11, 20)  # a weekend day for CLIENT-Y
        trigger = make_trigger(persona("EMP-001"), joining_date=friday).model_copy(update={"client_id": "CLIENT-Y"})
        actor = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id="CLIENT-Y")
        self.assertEqual(service.create_onboarding(actor, trigger).task_by_key("day1").due_date, date(2026, 11, 22))

    def test_replanning_uses_the_clients_calendar(self) -> None:
        from onboarding_task_management.contracts import DecisionOutcome
        service = self.service()
        trigger = make_trigger(persona("EMP-001"), joining_date=MONDAY).model_copy(update={"client_id": "CLIENT-Y"})
        requester = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id="CLIENT-Y")
        approver = Actor(actor_id="U-HR-2", role=Role.HR_OPS, client_id="CLIENT-Y")
        oid = service.create_onboarding(requester, trigger).onboarding_id
        request = service.request_change(requester, oid, joining_date=date(2026, 11, 23))
        service.decide_change(approver, request.request_id, DecisionOutcome.APPROVED)
        service.apply_change(requester, request.request_id)
        self.assertEqual(service.get(requester, oid).task_by_key("prep").due_date, date(2026, 11, 22))

    def test_missing_calendar_is_a_clear_error_when_one_is_required(self) -> None:
        service = OnboardingService(
            _two_client_registry(), calendars={"CLIENT-X": SAT_SUN}, require_client_calendar=True)
        self.assertEqual(_create(service, "CLIENT-X"), date(2026, 11, 13))
        with self.assertRaises(CalendarConfigurationError) as ctx:
            _create(service, "CLIENT-Y")
        self.assertIn("no working calendar", ctx.exception.message)
        self.assertEqual(service._client_records("CLIENT-Y"), {})

    def test_dates_outside_a_clients_declared_coverage_are_refused(self) -> None:
        bounded = WorkingCalendar(frozenset(), name="bounded", weekend_weekdays={5, 6},
                                  coverage_start=date(2026, 1, 1), coverage_end=date(2026, 12, 31))
        service = OnboardingService(_two_client_registry(), calendars={"CLIENT-X": bounded})
        trigger = make_trigger(persona("EMP-001"), joining_date=date(2027, 3, 1)).model_copy(
            update={"client_id": "CLIENT-X"})
        with self.assertRaises(CalendarRangeError):
            service.create_onboarding(Actor(actor_id="U", role=Role.HR_OPS, client_id="CLIENT-X"), trigger)

    def test_invalid_calendars_are_refused_when_built(self) -> None:
        for kwargs in (
            dict(name="", weekend_weekdays={5, 6}),
            dict(name="n", weekend_weekdays={0, 1, 2, 3, 4, 5, 6}),
            dict(name="n", weekend_weekdays={7}),
            dict(name="n", weekend_weekdays={5}, coverage_start=date(2026, 2, 1), coverage_end=date(2026, 1, 1)),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(CalendarConfigurationError):
                WorkingCalendar(frozenset(), **kwargs)

    def test_existing_default_behaviour_is_unchanged_without_a_client_calendar(self) -> None:
        baseline = OnboardingService(build_registry())
        explicit = OnboardingService(
            build_registry(),
            calendars={CLIENT_ALPHA: WorkingCalendar(
                UAE_PUBLIC_HOLIDAYS, name="uae-sat-sun", weekend_weekdays={5, 6})})
        a = baseline.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        b = explicit.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        self.assertEqual([t.due_date for t in a.tasks], [t.due_date for t in b.tasks])

    def test_calendar_status_reflects_the_clients_calendar(self) -> None:
        cal = WorkingCalendar({date(2026, 11, 13)}, name="x", weekend_weekdays={5, 6}, approved=True,
                              source="synthetic test calendar")
        status = calendar_status(cal)
        self.assertTrue(status.approved)
        self.assertEqual(status.source, "synthetic test calendar")
        self.assertFalse(calendar_status(SAT_SUN).approved)  # unapproved unless stated


# --------------------------------------------------------------------------- Phase 5
class HrRecordCreateDependencyTests(_Base):
    """Documents what the repository defines; invents no completion event."""

    def test_ownership_as_configured(self) -> None:
        root = self.task("hr_record_create")
        self.assertEqual((root.owner, root.category, root.evidence_required),
                         (Role.HR_OPS, TaskCategory.HR_RECORD, False))
        # The agent run started it (READY -> IN_PROGRESS); starting is not completing.
        self.assertIn(root.status, (TaskStatus.READY, TaskStatus.IN_PROGRESS))
        self.assertEqual(self.task("collect_documents").status, TaskStatus.BLOCKED)
        self.assertEqual(self.task("collect_documents").depends_on, [root.task_id])

    def test_collect_documents_stays_blocked_until_hr_record_create_is_completed(self) -> None:
        result = self.report("collect_documents", "uc03_document_validation", "valid", UC03)
        self.assertEqual(result.rejection_code, status_updates.REJECT_PREREQUISITES_UNMET)
        self.assertEqual(self.task("collect_documents").status, TaskStatus.BLOCKED)

    def test_nothing_completes_hr_record_create_automatically(self) -> None:
        self.report("collect_documents", "uc03_document_validation", "valid", UC03)
        self.report("it_laptop", "uc04_equipment_access", "completed", UC04)
        self.report("orientation_schedule", "uc05_orientation", "completed", UC05)
        self.assertIn(self.task("hr_record_create").status, (TaskStatus.READY, TaskStatus.IN_PROGRESS))
        self.assertIsNone(self.task("hr_record_create").completed_at)

    def test_only_an_authorised_human_role_can_complete_it_today(self) -> None:
        with self.assertRaises(UnauthorizedActionError):
            self.service.complete_task(EMPLOYEE, self.oid, f"{self.oid}#hr_record_create")
        with self.assertRaises(DataIsolationError):
            self.service.complete_task(HR_OPS_BETA, self.oid, f"{self.oid}#hr_record_create")
        self.complete_root()
        self.assertEqual(self.task("collect_documents").status, TaskStatus.READY)

    def test_no_reporting_workflow_is_registered_for_hr_record_work_except_operations(self) -> None:
        reporters = [s for s, cats in status_updates.SOURCE_CATEGORIES.items()
                     if TaskCategory.HR_RECORD in cats]
        self.assertEqual(reporters, [SourceUseCase.OPERATIONS])  # UC-01 is not a UC-02 reporting source

    def test_uc03_cannot_report_on_hr_record_create(self) -> None:
        result = self.report("hr_record_create", "uc03_document_validation", "completed", UC03)
        self.assertEqual(result.rejection_code, status_updates.REJECT_SOURCE_NOT_RESPONSIBLE)


# --------------------------------------------------------------------------- Phase 6
class StatusContractFixtureTests(_Base):
    """UC-02-side schema fixtures. These are NOT agreed cross-agent contracts."""

    def setUp(self) -> None:
        super().setUp()
        self.complete_root()

    def test_uc03_document_status_payload(self) -> None:
        result = self.report("collect_documents", "uc03_document_validation", "valid", UC03,
                             case_id="CASE-F1", correlation_id="corr-F1", reference="DOC-SET-1")
        self.assertTrue(result.accepted)
        task = self.task("collect_documents")
        self.assertEqual((task.status, task.external_source, task.external_reference),
                         (TaskStatus.COMPLETE, SourceUseCase.UC03_DOCUMENT_VALIDATION, "DOC-SET-1"))
        event = [e for e in self.service.audit.events_for(HR_OPS) if e.action == "task.status_update"][-1]
        self.assertEqual(event.correlation_id, "corr-F1")

    def test_uc04_equipment_status_payloads(self) -> None:
        started = self.report("it_laptop", "uc04_equipment_access", "in_progress", UC04)
        self.assertEqual(started.applied_status, TaskStatus.IN_PROGRESS)
        done = self.report("it_laptop", "uc04_equipment_access", "completed", UC04, reference="TCK-9")
        self.assertEqual(done.applied_status, TaskStatus.COMPLETE)

    def test_uc05_orientation_status_payload(self) -> None:
        result = self.report("orientation_schedule", "uc05_orientation", "completed", UC05, reference="SES-1")
        self.assertTrue(result.accepted)
        self.assertEqual(self.task("orientation_schedule").status, TaskStatus.COMPLETE)

    def test_each_source_is_limited_to_its_own_task_categories(self) -> None:
        for key, source, caller in (
            ("it_laptop", "uc03_document_validation", UC03),
            ("collect_documents", "uc04_equipment_access", UC04),
            ("it_laptop", "uc05_orientation", UC05),
        ):
            with self.subTest(task=key, source=source):
                result = self.report(key, source, "completed", caller)
                self.assertEqual(result.rejection_code, status_updates.REJECT_SOURCE_NOT_RESPONSIBLE)

    def test_invalid_external_status_payloads_are_rejected_before_any_effect(self) -> None:
        for bad in (dict(outcome="done"), dict(source="uc09_unknown"), dict(occurred_at="yesterday"),
                    dict(update_id=None)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.agent.apply_status_update(
                    {**_payload(self.oid, "collect_documents", "uc03_document_validation", "valid"), **bad},
                    client_id=CLIENT_ALPHA, actor_id=UC03)
        self.assertIsNone(self.task("collect_documents").external_outcome)

    def test_unknown_task_and_wrong_onboarding_are_rejected(self) -> None:
        unknown = self.report("no_such_task", "uc03_document_validation", "valid", UC03)
        self.assertEqual(unknown.rejection_code, status_updates.REJECT_UNKNOWN_TASK)
        with self.assertRaises(InvalidStateError):  # an onboarding this client does not have
            self.agent.apply_status_update(
                _payload("ONB-OTHER", "collect_documents", "uc03_document_validation", "valid"),
                client_id=CLIENT_ALPHA, actor_id=UC03)

    def test_a_different_shared_case_is_rejected(self) -> None:
        result = self.report("collect_documents", "uc03_document_validation", "valid", UC03,
                             case_id="CASE-OTHER")
        self.assertEqual(result.rejection_code, status_updates.REJECT_CASE_MISMATCH)
        self.assertNotEqual(self.task("collect_documents").status, TaskStatus.COMPLETE)

    def test_cross_client_update_is_rejected(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.agent.apply_status_update(
                _payload(self.oid, "collect_documents", "uc03_document_validation", "valid"),
                client_id=CLIENT_BETA, actor_id=UC03)
        with self.assertRaises(DataIsolationError):
            self.agent.apply_status_update(
                {**_payload(self.oid, "collect_documents", "uc03_document_validation", "valid"),
                 "client_id": CLIENT_BETA},
                client_id=CLIENT_ALPHA, actor_id=UC03)

    def test_duplicate_status_update_is_applied_once_with_one_event(self) -> None:
        first = self.report("collect_documents", "uc03_document_validation", "valid", UC03, update_id="dup-1")
        events = len(self.service.audit)
        second = self.report("collect_documents", "uc03_document_validation", "valid", UC03, update_id="dup-1")
        self.assertTrue(first.accepted and second.duplicate_replay)
        self.assertEqual(len(self.service.audit), events)


# --------------------------------------------------------------------------- Phase 7 / 8
class ApprovalRoleAndDuplicateCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = OnboardingService(build_registry())
        self.oid = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-007"))).onboarding_id
        self.root = f"{self.oid}#hr_record_create"

    def test_approval_role_is_an_informational_flag_not_an_approval(self) -> None:
        self.service.complete_task(HR_OPS, self.oid, self.root)
        view = self.service.checklist(HR_OPS, self.oid, date(2026, 11, 2))
        letter = f"{self.oid}#salary_transfer_letter"
        self.assertIn(letter, view.awaiting_approval_task_ids)
        # No approval request exists for it: the flag is configuration, not state.
        self.assertEqual(view.pending_gate_request_ids, [])
        # CURRENT behaviour, pinned so it cannot change unnoticed: the flag does not gate completion.
        # Whether it should is a team decision (NEEDS TEAM CONFIRMATION).
        done = self.service.complete_task(HR_OPS, self.oid, letter, "EVID-1")
        self.assertEqual(done.status, TaskStatus.COMPLETE)

    def test_duplicate_completion_stays_refused_and_changes_nothing(self) -> None:
        self.service.complete_task(HR_OPS, self.oid, self.root)
        original = self.service.get(HR_OPS, self.oid).task_by_key("hr_record_create").completed_at
        for _ in range(3):
            with self.assertRaises(InvalidStateError):
                self.service.complete_task(HR_OPS, self.oid, self.root)
        task = self.service.get(HR_OPS, self.oid).task_by_key("hr_record_create")
        self.assertEqual(task.completed_at, original)
        events = [e for e in self.service.audit.events_for(HR_OPS)
                  if e.action == "task.complete" and e.task_id == self.root]
        self.assertEqual([e.outcome for e in events].count("complete"), 1)  # one completion event
        # Each refused attempt is its own, distinctly-labelled "denied" event, never another "complete".
        self.assertEqual([e.outcome for e in events].count("denied"), 3)


class CalendarTypeIntegrityTests(unittest.TestCase):
    """A client's weekend rule must travel with its holidays, or consumers silently fall back to Sat/Sun."""

    CAL = WorkingCalendar({date(2026, 11, 13)}, name="fri-sat", weekend_weekdays={4, 5},
                          coverage_start=date(2026, 1, 1), coverage_end=date(2026, 12, 31),
                          approved=True, source="synthetic test calendar")

    def assertSameRules(self, other) -> None:
        self.assertIsInstance(other, WorkingCalendar)
        self.assertEqual(other.weekend_weekdays, frozenset({4, 5}))
        self.assertEqual((other.name, other.coverage_start, other.coverage_end, other.approved, other.source),
                         ("fri-sat", date(2026, 1, 1), date(2026, 12, 31), True, "synthetic test calendar"))

    def test_copy_deepcopy_and_pickle_keep_the_rules(self) -> None:
        import copy
        import pickle

        for clone in (copy.copy(self.CAL), copy.deepcopy(self.CAL), pickle.loads(pickle.dumps(self.CAL)), self.CAL.copy()):
            self.assertSameRules(clone)
            self.assertEqual(set(clone), {date(2026, 11, 13)})

    def test_set_operations_do_not_silently_drop_the_weekend_rule(self) -> None:
        extra = {date(2026, 12, 25)}
        for derived in (self.CAL | extra, frozenset(extra) | self.CAL, self.CAL.union(extra), self.CAL & {date(2026, 11, 13)},
                        self.CAL - {date(2026, 1, 1)}, self.CAL ^ extra, self.CAL.difference(extra),
                        self.CAL.intersection(self.CAL), self.CAL.symmetric_difference(extra)):
            self.assertSameRules(derived)
        self.assertEqual(set(self.CAL | extra), {date(2026, 11, 13), date(2026, 12, 25)})
        # Limit of the language, pinned so nobody assumes otherwise: a plain ``set`` on the LEFT is handled by
        # ``set.__or__`` before the calendar is consulted, so it yields a plain set. Combine with a frozenset.
        self.assertNotIsInstance(set(extra) | self.CAL, WorkingCalendar)

    def test_a_derived_calendar_still_schedules_by_the_clients_weekend(self) -> None:
        from onboarding_task_management.calendar_uae import add_business_days, is_business_day

        derived = self.CAL | {date(2026, 12, 1)}
        self.assertTrue(is_business_day(date(2026, 11, 15), derived))   # Sunday works in a Fri/Sat-weekend calendar
        self.assertFalse(is_business_day(date(2026, 11, 14), derived))  # Saturday does not
        self.assertEqual(add_business_days(date(2026, 11, 16), -1, derived), date(2026, 11, 15))

    def test_the_service_hands_consumers_the_clients_calendar_type_and_does_not_fall_back(self) -> None:
        service = OnboardingService(_two_client_registry(), calendars={"CLIENT-X": SAT_SUN},
                                    require_client_calendar=True)
        self.assertIsInstance(service._holidays_for("CLIENT-X"), WorkingCalendar)
        self.assertIs(service._holidays_for("CLIENT-X"), SAT_SUN)
        with self.assertRaises(CalendarConfigurationError):  # required, absent: an error, never UAE
            service._holidays_for("CLIENT-Y")
        optional = OnboardingService(_two_client_registry(), calendars={"CLIENT-X": SAT_SUN})
        self.assertIsNot(optional._holidays_for("CLIENT-X"), optional.holidays)
        self.assertEqual(optional._holidays_for("CLIENT-Y"), optional.holidays)  # explicit default, only when not required

    def test_due_dates_follow_the_clients_weekend_after_the_calendar_is_copied(self) -> None:
        import copy

        service = OnboardingService(_two_client_registry(), calendars={"CLIENT-Y": copy.deepcopy(FRI_SAT)})
        self.assertEqual(_create(service, "CLIENT-Y"), date(2026, 11, 15))


if __name__ == "__main__":
    unittest.main()
