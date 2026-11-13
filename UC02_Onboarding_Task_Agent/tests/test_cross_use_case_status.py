"""Inbound cross-use-case status updates, and the end-to-end UC-02 workflow.

What is real here, and what is not
----------------------------------

**No approved cross-use-case interface exists in this repository, and UC-02
contains no transport of any kind.** Every "UC-03 said", "UC-04 said" and
"UC-05 said" in this module is a :class:`StatusUpdate` minted by
``sub_agent.mock_handoff`` -- a pure function, not a call to anything.

So these tests prove that UC-02 *handles a report correctly*: that it
validates it, attributes it, applies it to the right task, recomputes the
plan around it, and refuses it when it should. They prove nothing whatsoever
about a document being validated, a laptop being shipped or a visa being
filed, and nothing about anyone being notified. The assertions in
:class:`MockHonestyTests` exist specifically to keep that boundary visible.

The fixture ``data/mock/status_updates_mock.json`` declares, for each
scenario, the outcome expected -- including the rejection code -- so a test
here cannot quietly pass by accepting something it should have refused.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from onboarding_task_management.contracts import (
    OPEN_STATUSES,
    UNRESOLVED_OUTCOMES,
    DecisionOutcome,
    ExternalOutcome,
    SourceUseCase,
    StatusUpdate,
    TaskStatus,
)
from onboarding_task_management.errors import DataIsolationError
from onboarding_task_management.sub_agent import mock_handoff, status_updates, tracker
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.synthetic import (
    CLIENT_ALPHA,
    build_registry,
    make_trigger,
    persona,
)
from tests.support import (
    HR_OPS,
    HR_OPS_APPROVER,
    HR_OPS_BETA,
    TEST_ONLY_SOURCE_PRINCIPALS,
)

MOCK_DIR = Path(__file__).resolve().parents[1] / "data" / "mock"
UPDATE_FIXTURE = MOCK_DIR / "status_updates_mock.json"

#: Explicit, because UC-02 reads no clock on an inbound path.
OCCURRED = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
AS_OF = date(2026, 11, 2)


class StatusUpdateTestCase(unittest.TestCase):
    """EMP-007, the persona for which every extended activity applies."""

    employee_id = "EMP-007"

    def setUp(self) -> None:
        self.service = OnboardingService(
            build_registry(), source_principals=TEST_ONLY_SOURCE_PRINCIPALS
        )
        created = self.service.create_onboarding(
            HR_OPS, make_trigger(persona(self.employee_id))
        )
        self.onboarding_id = created.onboarding_id
        self.fixture = json.loads(UPDATE_FIXTURE.read_text(encoding="utf-8"))

    # -- helpers -----------------------------------------------------------

    @property
    def record(self):
        return self.service.get(HR_OPS, self.onboarding_id)

    def task(self, key: str):
        task = self.record.task_by_key(key)
        assert task is not None, f"no task for {key!r}"
        return task

    def task_id(self, key: str) -> str:
        return f"{self.onboarding_id}#{key}"

    def status_of(self, key: str) -> TaskStatus:
        return self.task(key).status

    def apply(self, update: StatusUpdate):
        return self.service.apply_status_update(HR_OPS, self.onboarding_id, update)

    def report(
        self,
        key: str,
        source: SourceUseCase,
        outcome: ExternalOutcome,
        *,
        blocking_reason: str = "",
        attempt: int = 1,
        occurred_at: datetime = OCCURRED,
    ):
        """Mint and apply one mock report against ``key``."""
        adapter = mock_handoff.MOCK_ADAPTERS[source]
        update = adapter(
            self.task(key),
            outcome,
            occurred_at,
            blocking_reason=blocking_reason,
            attempt=attempt,
        )
        return self.apply(update)

    def complete_root(self) -> None:
        """Clear ``hr_record_create``, the single initially-ready task."""
        self.service.complete_task(
            HR_OPS, self.onboarding_id, self.task_id("hr_record_create")
        )

    def ready_keys(self) -> set[str]:
        return {
            t.template_key
            for t in self.service.ready_tasks(HR_OPS, self.onboarding_id)
        }

    def flag_codes(self, key: str, as_of: date = AS_OF) -> set[str]:
        return {
            f.code
            for f in self.service.follow_up_flags(HR_OPS, self.onboarding_id, as_of)
            if f.template_key == key
        }


# ---------------------------------------------------------------------------
# Document validation (UC-03)
# ---------------------------------------------------------------------------


class DocumentValidationTests(StatusUpdateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.complete_root()
        self.service.assign_owner(
            HR_OPS,
            self.onboarding_id,
            self.task_id("collect_documents"),
            "U-HR-9",
            team="HR Shared Services",
        )

    def test_a_valid_result_completes_the_task_and_unblocks_dependants(self) -> None:
        result = self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.VALID,
        )
        self.assertTrue(result.accepted)
        self.assertIs(result.applied_status, TaskStatus.COMPLETE)
        unblocked = {i.split("#", 1)[1] for i in result.unblocked_task_ids}
        self.assertEqual(
            unblocked,
            {
                "background_verification",
                "payroll_setup",
                "visa_application",
                "visa_pack_handover",
            },
        )

    def test_an_invalid_result_leaves_the_prerequisite_unresolved(self) -> None:
        result = self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.INVALID,
            blocking_reason="identity document scan unreadable",
        )
        self.assertTrue(result.accepted)
        self.assertTrue(result.follow_up_raised)
        self.assertIn(self.status_of("collect_documents"), OPEN_STATUSES)
        self.assertNotIn("payroll_setup", self.ready_keys())
        self.assertIs(self.status_of("payroll_setup"), TaskStatus.BLOCKED)

    def test_requires_review_also_leaves_it_unresolved(self) -> None:
        self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.REQUIRES_REVIEW,
            blocking_reason="manual check needed",
        )
        self.assertIn(self.status_of("collect_documents"), OPEN_STATUSES)
        self.assertIs(self.status_of("payroll_setup"), TaskStatus.BLOCKED)

    def test_an_unresolved_result_raises_a_follow_up_naming_the_assignee(self) -> None:
        self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.INVALID,
            blocking_reason="identity document scan unreadable",
        )
        flags = [
            f
            for f in self.service.follow_up_flags(HR_OPS, self.onboarding_id, AS_OF)
            if f.code == tracker.FLAG_EXTERNAL_UNRESOLVED
        ]
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].follow_up_with, "U-HR-9")
        self.assertIs(flags[0].source, SourceUseCase.UC03_DOCUMENT_VALIDATION)
        self.assertIn("identity document scan unreadable", flags[0].reason)

    def test_a_corrected_document_resolves_it_and_recalculates_readiness(self) -> None:
        """Scenario 19: invalid, then a corrected submission that validates."""
        self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.INVALID,
            blocking_reason="identity document scan unreadable",
        )
        self.assertIs(self.status_of("payroll_setup"), TaskStatus.BLOCKED)

        result = self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.VALID,
            attempt=2,  # a distinct report, not a replay of the rejection
        )
        self.assertTrue(result.accepted)
        self.assertIs(self.status_of("collect_documents"), TaskStatus.COMPLETE)
        self.assertIn("payroll_setup", self.ready_keys())
        self.assertEqual(self.flag_codes("collect_documents"), set())

    def test_suspected_fraud_is_never_inferred_from_invalid(self) -> None:
        """The whole point of keeping the two outcomes separate."""
        self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.INVALID,
            blocking_reason="identity document scan unreadable",
        )
        self.assertIs(
            self.task("collect_documents").external_outcome, ExternalOutcome.INVALID
        )

    def test_an_explicit_suspected_fraud_report_is_recorded_as_such(self) -> None:
        result = self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.SUSPECTED_FRAUD,
            blocking_reason="credential not confirmed by issuing body",
        )
        self.assertTrue(result.accepted)
        self.assertTrue(result.follow_up_raised)
        self.assertIs(
            self.task("collect_documents").external_outcome,
            ExternalOutcome.SUSPECTED_FRAUD,
        )
        # It resolves nothing: downstream work stays blocked.
        self.assertIs(self.status_of("payroll_setup"), TaskStatus.BLOCKED)
        self.assertIn(tracker.FLAG_EXTERNAL_UNRESOLVED, self.flag_codes("collect_documents"))

    def test_only_the_validating_workflow_may_allege_fraud(self) -> None:
        update = mock_handoff.uc04_equipment_result(
            self.task("it_laptop"),
            ExternalOutcome.SUSPECTED_FRAUD,
            OCCURRED,
            blocking_reason="asserted by the wrong workflow",
        )
        result = self.apply(update)
        self.assertFalse(result.accepted)
        self.assertEqual(
            result.rejection_code, status_updates.REJECT_FRAUD_NOT_REPORTABLE
        )

    def test_the_audit_record_carries_no_document_content(self) -> None:
        self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.INVALID,
            blocking_reason="identity document scan unreadable",
        )
        events = [
            e
            for e in self.service.audit.events_for(HR_OPS)
            if e.action == "task.status_update"
        ]
        self.assertEqual(len(events), 1)
        detail = events[0].detail
        # Identifiers, a source and an outcome. No reason prose, no content.
        self.assertEqual(detail["reported_outcome"], "invalid")
        self.assertEqual(detail["source"], "uc03_document_validation")
        blob = json.dumps(detail).lower()
        self.assertNotIn("unreadable", blob)
        self.assertNotIn("identity document", blob)


# ---------------------------------------------------------------------------
# Equipment and access (UC-04)
# ---------------------------------------------------------------------------


class EquipmentAccessTests(StatusUpdateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.complete_root()
        self.service.assign_owner(
            HR_OPS,
            self.onboarding_id,
            self.task_id("it_laptop"),
            "U-IT-7",
            team="IT Service Desk",
        )

    def test_a_completion_updates_the_correct_task_only(self) -> None:
        result = self.report(
            "it_laptop",
            SourceUseCase.UC04_EQUIPMENT_ACCESS,
            ExternalOutcome.COMPLETED,
        )
        self.assertIs(result.applied_status, TaskStatus.COMPLETE)
        self.assertIs(self.status_of("it_laptop"), TaskStatus.COMPLETE)
        self.assertIn(self.status_of("desk_allocation"), OPEN_STATUSES)
        self.assertIs(self.status_of("collect_documents"), TaskStatus.READY)

    def test_completing_the_laptop_unblocks_accounts_only(self) -> None:
        result = self.report(
            "it_laptop",
            SourceUseCase.UC04_EQUIPMENT_ACCESS,
            ExternalOutcome.COMPLETED,
        )
        unblocked = {i.split("#", 1)[1] for i in result.unblocked_task_ids}
        self.assertEqual(unblocked, {"it_accounts"})

    def test_an_in_progress_report_does_not_complete_anything(self) -> None:
        """A started task is not a finished one."""
        result = self.report(
            "it_laptop",
            SourceUseCase.UC04_EQUIPMENT_ACCESS,
            ExternalOutcome.IN_PROGRESS,
        )
        self.assertIs(result.applied_status, TaskStatus.IN_PROGRESS)
        self.assertIs(self.status_of("it_accounts"), TaskStatus.BLOCKED)

    def test_a_delay_is_reflected_and_identifies_the_responsible_owner(self) -> None:
        result = self.report(
            "it_laptop",
            SourceUseCase.UC04_EQUIPMENT_ACCESS,
            ExternalOutcome.DELAYED,
            blocking_reason="stock arrives week 47",
        )
        self.assertTrue(result.follow_up_raised)
        task = self.task("it_laptop")
        self.assertIs(task.external_outcome, ExternalOutcome.DELAYED)
        self.assertEqual(task.external_blocking_reason, "stock arrives week 47")

        flags = [
            f
            for f in self.service.follow_up_flags(HR_OPS, self.onboarding_id, AS_OF)
            if f.template_key == "it_laptop"
            and f.code == tracker.FLAG_EXTERNAL_UNRESOLVED
        ]
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0].follow_up_with, "U-IT-7")
        self.assertEqual(flags[0].assignee_team, "IT Service Desk")

    def test_a_delayed_task_keeps_its_dependants_blocked(self) -> None:
        self.report(
            "it_laptop",
            SourceUseCase.UC04_EQUIPMENT_ACCESS,
            ExternalOutcome.DELAYED,
            blocking_reason="stock arrives week 47",
        )
        self.assertIs(self.status_of("it_accounts"), TaskStatus.BLOCKED)
        self.assertIs(self.status_of("access_apollo"), TaskStatus.BLOCKED)

    def test_a_delay_then_a_completion_restores_the_chain(self) -> None:
        self.report(
            "it_laptop",
            SourceUseCase.UC04_EQUIPMENT_ACCESS,
            ExternalOutcome.DELAYED,
            blocking_reason="stock arrives week 47",
        )
        self.report(
            "it_laptop",
            SourceUseCase.UC04_EQUIPMENT_ACCESS,
            ExternalOutcome.COMPLETED,
            attempt=2,
        )
        self.assertIs(self.status_of("it_laptop"), TaskStatus.COMPLETE)
        self.assertEqual(self.task("it_laptop").external_blocking_reason, "")
        self.assertIn("it_accounts", self.ready_keys())

    def test_uc04_may_not_close_a_document_task(self) -> None:
        update = mock_handoff.uc04_equipment_result(
            self.task("collect_documents"), ExternalOutcome.COMPLETED, OCCURRED
        )
        result = self.apply(update)
        self.assertFalse(result.accepted)
        self.assertEqual(
            result.rejection_code, status_updates.REJECT_SOURCE_NOT_RESPONSIBLE
        )
        self.assertIs(self.status_of("collect_documents"), TaskStatus.READY)

    def test_uc02_stores_no_provisioning_detail(self) -> None:
        """UC-02 tracks the outcome; the how is UC-04's business, not ours."""
        self.report(
            "it_laptop",
            SourceUseCase.UC04_EQUIPMENT_ACCESS,
            ExternalOutcome.COMPLETED,
        )
        task = self.task("it_laptop")
        self.assertIs(task.external_source, SourceUseCase.UC04_EQUIPMENT_ACCESS)
        self.assertTrue(
            task.external_reference.startswith(mock_handoff.MOCK_REFERENCE_PREFIX)
        )


# ---------------------------------------------------------------------------
# Orientation (UC-05) and the operational activities
# ---------------------------------------------------------------------------


class OrientationAndOperationsTests(StatusUpdateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.complete_root()

    def test_uc05_completes_orientation_and_unblocks_policy_ack(self) -> None:
        result = self.report(
            "orientation_schedule",
            SourceUseCase.UC05_ORIENTATION,
            ExternalOutcome.COMPLETED,
        )
        self.assertIs(result.applied_status, TaskStatus.COMPLETE)
        unblocked = {i.split("#", 1)[1] for i in result.unblocked_task_ids}
        self.assertEqual(unblocked, {"policy_ack"})

    def test_uc05_may_not_close_an_equipment_task(self) -> None:
        update = mock_handoff.uc05_orientation_result(
            self.task("it_laptop"), ExternalOutcome.COMPLETED, OCCURRED
        )
        self.assertEqual(
            self.apply(update).rejection_code,
            status_updates.REJECT_SOURCE_NOT_RESPONSIBLE,
        )

    def test_operations_can_progress_each_extended_activity(self) -> None:
        """Visa, medical, biometrics, insurance, travel, food, banking.

        Each is generated by applicability, assigned by HR, then reported by
        the operational team through the mock adapter.
        """
        independent = [
            "medical_test",
            "insurance_enrolment",
            "travel_booking",
            "accommodation_booking",
            "food_arrangement",
            "salary_transfer_letter",
            "non_liability_letter",
            "hr_system_activation",
        ]
        for key in independent:
            with self.subTest(task=key):
                self.service.assign_owner(
                    HR_OPS, self.onboarding_id, self.task_id(key), f"U-OPS-{key[:4]}"
                )
                result = self.report(
                    key, SourceUseCase.OPERATIONS, ExternalOutcome.COMPLETED
                )
                self.assertTrue(result.accepted, result.rejection_reason)
                self.assertIs(self.status_of(key), TaskStatus.COMPLETE)

    def test_biometrics_follows_the_medical_test(self) -> None:
        self.assertIs(self.status_of("biometrics_capture"), TaskStatus.BLOCKED)
        self.report("medical_test", SourceUseCase.OPERATIONS, ExternalOutcome.COMPLETED)
        self.assertIs(self.status_of("biometrics_capture"), TaskStatus.READY)
        self.report(
            "biometrics_capture", SourceUseCase.OPERATIONS, ExternalOutcome.COMPLETED
        )
        self.assertIs(self.status_of("biometrics_capture"), TaskStatus.COMPLETE)

    def test_family_insurance_follows_employee_insurance(self) -> None:
        self.assertIs(self.status_of("family_insurance_enrolment"), TaskStatus.BLOCKED)
        self.report(
            "insurance_enrolment", SourceUseCase.OPERATIONS, ExternalOutcome.COMPLETED
        )
        self.assertIs(self.status_of("family_insurance_enrolment"), TaskStatus.READY)

    def test_family_insurance_is_absent_when_the_attribute_is_not_set(self) -> None:
        """Applicability still rules; a status interface does not create tasks."""
        plain = self.service.create_onboarding(
            HR_OPS, make_trigger(persona("EMP-001"))
        )
        keys = {t.template_key for t in plain.tasks}
        self.assertNotIn("family_insurance_enrolment", keys)
        self.assertNotIn("travel_booking", keys)

    def test_visa_processing_waits_for_the_pack_handover(self) -> None:
        self.assertIs(self.status_of("visa_processing"), TaskStatus.BLOCKED)
        self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.VALID,
        )
        self.report(
            "visa_pack_handover", SourceUseCase.OPERATIONS, ExternalOutcome.COMPLETED
        )
        self.assertIs(self.status_of("visa_processing"), TaskStatus.READY)


# ---------------------------------------------------------------------------
# Validation, authorisation and idempotency of the interface itself
# ---------------------------------------------------------------------------


class InterfaceValidationTests(StatusUpdateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.complete_root()

    def _update(self, **overrides) -> StatusUpdate:
        base = dict(
            update_id="UPD-1",
            source=SourceUseCase.UC03_DOCUMENT_VALIDATION,
            client_id=CLIENT_ALPHA,
            onboarding_id=self.onboarding_id,
            task_id=self.task_id("collect_documents"),
            outcome=ExternalOutcome.VALID,
            occurred_at=OCCURRED,
            reference="MOCK-REF-1",
        )
        base.update(overrides)
        return StatusUpdate(**base)

    def test_an_unknown_task_id_is_rejected(self) -> None:
        result = self.apply(self._update(task_id=self.task_id("no_such_template")))
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection_code, status_updates.REJECT_UNKNOWN_TASK)

    def test_an_unknown_onboarding_id_is_rejected(self) -> None:
        result = self.apply(self._update(onboarding_id="ONB-NOPE"))
        self.assertFalse(result.accepted)
        self.assertEqual(
            result.rejection_code, status_updates.REJECT_UNKNOWN_ONBOARDING
        )

    def test_an_update_for_another_client_is_refused_as_isolation(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.apply(self._update(client_id="CLIENT-BETA"))

    def test_another_clients_actor_cannot_apply_an_update(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.apply_status_update(
                HR_OPS_BETA, self.onboarding_id, self._update()
            )

    def test_an_unresolved_outcome_must_carry_a_reason(self) -> None:
        for outcome in sorted(UNRESOLVED_OUTCOMES):
            with self.subTest(outcome=outcome):
                result = self.apply(
                    self._update(
                        update_id=f"UPD-{outcome}",
                        outcome=outcome,
                        blocking_reason="   ",
                    )
                )
                self.assertFalse(result.accepted)
                self.assertEqual(
                    result.rejection_code, status_updates.REJECT_MISSING_REASON
                )

    def test_completing_an_evidence_required_task_needs_a_reference(self) -> None:
        self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.VALID,
        )
        self.assertTrue(self.task("visa_pack_handover").evidence_required)
        result = self.apply(
            self._update(
                update_id="UPD-NOEV",
                source=SourceUseCase.OPERATIONS,
                task_id=self.task_id("visa_pack_handover"),
                outcome=ExternalOutcome.COMPLETED,
                reference=None,
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(
            result.rejection_code, status_updates.REJECT_MISSING_EVIDENCE
        )

    def test_a_report_cannot_complete_a_task_out_of_order(self) -> None:
        self.assertIs(self.status_of("it_accounts"), TaskStatus.BLOCKED)
        result = self.apply(
            self._update(
                update_id="UPD-OOO",
                source=SourceUseCase.UC04_EQUIPMENT_ACCESS,
                task_id=self.task_id("it_accounts"),
                outcome=ExternalOutcome.COMPLETED,
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(
            result.rejection_code, status_updates.REJECT_PREREQUISITES_UNMET
        )
        self.assertIs(self.status_of("it_accounts"), TaskStatus.BLOCKED)

    def test_a_settled_task_is_not_reopened_by_a_late_report(self) -> None:
        self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.VALID,
        )
        result = self.apply(
            self._update(
                update_id="UPD-LATE",
                outcome=ExternalOutcome.INVALID,
                blocking_reason="arrived after completion",
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection_code, status_updates.REJECT_TASK_SETTLED)
        self.assertIs(self.status_of("collect_documents"), TaskStatus.COMPLETE)

    def test_a_cancelled_onboarding_accepts_no_updates(self) -> None:
        request = self.service.request_cancellation(
            HR_OPS, self.onboarding_id, reason="candidate withdrew"
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_cancellation(HR_OPS, request.request_id)
        result = self.apply(self._update(update_id="UPD-CANCELLED"))
        self.assertFalse(result.accepted)
        self.assertIn(
            result.rejection_code,
            {status_updates.REJECT_NOT_ACTIVE, status_updates.REJECT_TASK_SETTLED},
        )

    def test_a_rejected_update_is_audited_as_rejected(self) -> None:
        self.apply(self._update(task_id=self.task_id("no_such_template")))
        events = [
            e
            for e in self.service.audit.events_for(HR_OPS)
            if e.action == "task.status_update"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].outcome, "rejected")
        self.assertEqual(
            events[0].detail["rejection_code"], status_updates.REJECT_UNKNOWN_TASK
        )

    # -- idempotency -------------------------------------------------------

    def test_a_replayed_update_is_applied_once(self) -> None:
        first = self.apply(self._update())
        self.assertTrue(first.accepted)
        self.assertFalse(first.duplicate_replay)

        second = self.apply(self._update())
        self.assertTrue(second.accepted)
        self.assertTrue(second.duplicate_replay)
        self.assertEqual(second.unblocked_task_ids, [])

    def test_a_replay_does_not_change_state_a_second_time(self) -> None:
        self.apply(self._update())
        completed_at = self.task("collect_documents").completed_at
        self.apply(self._update())
        self.assertEqual(self.task("collect_documents").completed_at, completed_at)

    def test_the_same_update_id_aimed_at_another_task_is_a_conflict(self) -> None:
        self.apply(self._update())
        result = self.apply(
            self._update(
                task_id=self.task_id("background_verification"),
                outcome=ExternalOutcome.VALID,
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(
            result.rejection_code, status_updates.REJECT_DUPLICATE_CONFLICT
        )
        self.assertIn(
            self.status_of("background_verification"), OPEN_STATUSES
        )

    def test_distinct_reports_about_one_task_are_not_confused_for_replays(self) -> None:
        self.apply(
            self._update(
                update_id="UPD-A",
                outcome=ExternalOutcome.INVALID,
                blocking_reason="first rejection",
            )
        )
        second = self.apply(
            self._update(
                update_id="UPD-B",
                outcome=ExternalOutcome.INVALID,
                blocking_reason="second rejection",
            )
        )
        self.assertTrue(second.accepted)
        self.assertFalse(second.duplicate_replay)
        self.assertEqual(
            self.task("collect_documents").external_blocking_reason,
            "second rejection",
        )

    def test_the_mock_adapter_derives_a_stable_update_id(self) -> None:
        task = self.task("it_laptop")
        a = mock_handoff.uc04_equipment_result(
            task, ExternalOutcome.COMPLETED, OCCURRED
        )
        b = mock_handoff.uc04_equipment_result(
            task, ExternalOutcome.COMPLETED, OCCURRED
        )
        self.assertEqual(a.update_id, b.update_id)
        c = mock_handoff.uc04_equipment_result(
            task, ExternalOutcome.COMPLETED, OCCURRED, attempt=2
        )
        self.assertNotEqual(a.update_id, c.update_id)


# ---------------------------------------------------------------------------
# Silence is not success
# ---------------------------------------------------------------------------


class MissingResponseTests(StatusUpdateTestCase):
    def test_a_task_with_no_report_is_not_complete(self) -> None:
        self.complete_root()
        for key in ("it_laptop", "collect_documents", "medical_test"):
            with self.subTest(task=key):
                self.assertIn(self.status_of(key), OPEN_STATUSES)
                self.assertIsNone(self.task(key).external_outcome)

    def test_no_report_ever_arriving_leaves_the_case_incomplete(self) -> None:
        self.complete_root()
        summary = self.service.status(HR_OPS, self.onboarding_id, AS_OF)
        self.assertGreater(
            summary.blocked + summary.ready + summary.in_progress, 0
        )

    def test_an_overdue_unreported_task_is_flagged_not_assumed_done(self) -> None:
        self.complete_root()
        due = self.task("it_laptop").due_date
        flags = self.flag_codes("it_laptop", as_of=due + timedelta(days=3))
        self.assertIn(tracker.FLAG_OVERDUE, flags)
        self.assertIs(self.status_of("it_laptop"), TaskStatus.READY)


class MockHonestyTests(StatusUpdateTestCase):
    """Guards against a mock being mistaken for a real integration."""

    def test_every_mock_reference_is_marked_as_mock(self) -> None:
        self.complete_root()
        self.report(
            "it_laptop",
            SourceUseCase.UC04_EQUIPMENT_ACCESS,
            ExternalOutcome.COMPLETED,
        )
        self.assertTrue(
            self.task("it_laptop").external_reference.startswith(
                mock_handoff.MOCK_REFERENCE_PREFIX
            )
        )

    def test_uc02_ships_no_transport_for_cross_use_case_updates(self) -> None:
        """A grep-level guard: no HTTP client in the handoff modules."""
        for module in ("status_updates.py", "mock_handoff.py"):
            path = (
                Path(status_updates.__file__).parent / module
            )
            source = path.read_text(encoding="utf-8").lower()
            with self.subTest(module=module):
                for banned in ("import requests", "import httpx", "urllib.request"):
                    self.assertNotIn(banned, source)

    def test_follow_up_flags_never_claim_delivery(self) -> None:
        self.complete_root()
        flags = self.service.follow_up_flags(HR_OPS, self.onboarding_id, AS_OF)
        self.assertTrue(flags)
        self.assertTrue(all(not f.delivered for f in flags))


# ---------------------------------------------------------------------------
# Fixture-driven: the declared expectations must actually hold
# ---------------------------------------------------------------------------


class FixtureDrivenTests(StatusUpdateTestCase):
    def test_every_rejected_fixture_case_is_rejected_with_its_declared_code(
        self,
    ) -> None:
        cases = self.fixture["rejected_updates"]
        self.assertTrue(cases)
        for case in cases:
            with self.subTest(scenario=case["_scenario"]):
                # Each case gets a clean onboarding, since some depend on an
                # untouched dependency state.
                self.setUp()
                self.complete_root()
                task = self.record.task_by_key(case["template_key"])
                task_id = (
                    task.task_id
                    if task is not None
                    else self.task_id(case["template_key"])
                )
                update = StatusUpdate(
                    update_id=f"UPD-FIX-{case['_scenario'][:12]}",
                    source=SourceUseCase(case["source"]),
                    client_id=CLIENT_ALPHA,
                    onboarding_id=self.onboarding_id,
                    task_id=task_id,
                    outcome=ExternalOutcome(case["outcome"]),
                    occurred_at=datetime.fromisoformat(case["occurred_at"]),
                    reference=case.get("reference"),
                    blocking_reason=case.get("blocking_reason", ""),
                )
                result = self.apply(update)
                self.assertFalse(result.accepted)
                self.assertEqual(
                    result.rejection_code, case["expect_rejection_code"]
                )

    def test_the_fixture_only_uses_outcomes_the_contract_supports(self) -> None:
        groups = ("accepted_updates", "rejected_updates")
        for group in groups:
            for case in self.fixture[group]:
                with self.subTest(scenario=case["_scenario"]):
                    ExternalOutcome(case["outcome"])  # raises if unsupported
                    SourceUseCase(case["source"])


# ---------------------------------------------------------------------------
# The complete synthetic workflow
# ---------------------------------------------------------------------------


class EndToEndWorkflowTests(StatusUpdateTestCase):
    """One synthetic employee, start to readiness report.

    Every step goes through the shipped service. Nothing here reimplements a
    rule, and every external outcome is an explicit mock report.
    """

    def test_the_full_onboarding_workflow(self) -> None:
        svc, oid = self.service, self.onboarding_id

        # 1-2. The case exists and the checklist was generated.
        self.assertTrue(self.record.tasks)
        self.assertIs(self.status_of("hr_record_create"), TaskStatus.READY)

        # 3. Every applicable task starts unassigned.
        unassigned_before = len(svc.unassigned_tasks(HR_OPS, oid))
        self.assertEqual(unassigned_before, len(self.record.tasks))

        # 4. HR assigns owners.
        for key, who in (
            ("hr_record_create", "U-HR-1"),
            ("collect_documents", "U-HR-9"),
            ("it_laptop", "U-IT-7"),
            ("orientation_schedule", "U-HR-3"),
            ("medical_test", "U-HR-5"),
        ):
            svc.assign_owner(HR_OPS, oid, self.task_id(key), who)
        self.assertEqual(
            len(svc.unassigned_tasks(HR_OPS, oid)), unassigned_before - 5
        )

        # 5. The root task completes, releasing the first wave.
        self.complete_root()
        self.assertIn("collect_documents", self.ready_keys())

        # 6-7. UC-03 reports the document INVALID; dependants stay blocked.
        self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.INVALID,
            blocking_reason="identity document scan unreadable",
        )
        self.assertIs(self.status_of("payroll_setup"), TaskStatus.BLOCKED)
        self.assertIn(
            tracker.FLAG_EXTERNAL_UNRESOLVED, self.flag_codes("collect_documents")
        )

        # 8-9. A corrected document validates; eligible work is released.
        self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.VALID,
            attempt=2,
        )
        self.assertIn("payroll_setup", self.ready_keys())
        self.assertEqual(self.flag_codes("collect_documents"), set())

        # 10-11. UC-04 delays the laptop, then completes it.
        self.report(
            "it_laptop",
            SourceUseCase.UC04_EQUIPMENT_ACCESS,
            ExternalOutcome.DELAYED,
            blocking_reason="stock arrives week 47",
        )
        self.assertIs(self.status_of("it_accounts"), TaskStatus.BLOCKED)
        self.report(
            "it_laptop",
            SourceUseCase.UC04_EQUIPMENT_ACCESS,
            ExternalOutcome.COMPLETED,
            attempt=2,
        )
        self.assertIn("it_accounts", self.ready_keys())

        # 12. UC-05 completes orientation.
        self.report(
            "orientation_schedule",
            SourceUseCase.UC05_ORIENTATION,
            ExternalOutcome.COMPLETED,
        )
        self.assertIn("policy_ack", self.ready_keys())

        # 13. The operational activities progress through mock handlers.
        for key in (
            "medical_test",
            "insurance_enrolment",
            "travel_booking",
            "accommodation_booking",
            "food_arrangement",
            "salary_transfer_letter",
            "non_liability_letter",
            "visa_pack_handover",
        ):
            with self.subTest(task=key):
                result = self.report(
                    key, SourceUseCase.OPERATIONS, ExternalOutcome.COMPLETED
                )
                self.assertTrue(result.accepted, result.rejection_reason)
        self.assertIs(self.status_of("biometrics_capture"), TaskStatus.READY)
        self.assertIs(self.status_of("visa_processing"), TaskStatus.READY)

        # 14. A still-open, unreported task is flagged, not assumed done.
        late = self.task("desk_allocation").due_date + timedelta(days=4)
        self.assertIn(tracker.FLAG_OVERDUE, self.flag_codes("desk_allocation", late))

        # 15. A joining-date change replans open work and preserves the done.
        completed_before = {
            t.template_key
            for t in self.record.tasks
            if t.status is TaskStatus.COMPLETE
        }
        old_due = self.task("desk_allocation").due_date
        new_joining = self.record.joining_date + timedelta(days=14)
        request = svc.request_change(HR_OPS, oid, joining_date=new_joining)
        svc.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        svc.apply_change(HR_OPS, request.request_id)

        self.assertEqual(self.record.joining_date, new_joining)
        self.assertGreater(self.task("desk_allocation").due_date, old_due)
        completed_after = {
            t.template_key
            for t in self.record.tasks
            if t.status is TaskStatus.COMPLETE
        }
        self.assertTrue(completed_before.issubset(completed_after))

        # Replanning must not duplicate tasks.
        keys = [t.template_key for t in self.record.tasks]
        self.assertEqual(len(keys), len(set(keys)))

        # 16. The consolidated plan and readiness report.
        summary = svc.status(HR_OPS, oid, AS_OF)
        self.assertEqual(
            summary.blocked
            + summary.ready
            + summary.in_progress
            + summary.complete
            + summary.cancelled,
            summary.total_tasks,
        )
        self.assertEqual(summary.total_tasks, len(self.record.tasks))
        self.assertGreater(summary.complete, 0)
        risk = svc.joining_risk(HR_OPS, oid, AS_OF)
        self.assertIsNotNone(risk)
        # Work genuinely remains: nothing was fabricated as finished.
        self.assertGreater(summary.blocked + summary.ready + summary.in_progress, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
