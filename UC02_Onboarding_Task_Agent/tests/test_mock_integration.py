"""Mock-data layer: nationality boundary, escalation, providers and fixtures.

Covers the work added in the mock-completion pass:

* UC02-BR-02 nationality dimension as an opaque, unfiltered-by-default group
* UC02-BR-10 escalation (GR-12 triggers, Framework §10 routing)
* integration boundaries and their mock/local adapters
* the scenario fixtures under ``data/mock/``
* the two extended activities that previously had no direct test

No test here contacts an external system; all storage is local.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from onboarding_task_management.contracts import (
    DecisionOutcome,
    EscalationLevel,
    OnboardingRecord,
    Role,
    TaskCategory,
    TaskStatus,
)
from onboarding_task_management.errors import (
    CalendarRangeError,
    DataIsolationError,
    EvidenceRequiredError,
    MissingConfigurationError,
    PreconditionError,
)
from onboarding_task_management.providers import (
    BuiltInCalendarProvider,
    DraftOnlyNotificationProvider,
    InMemoryOnboardingRepository,
    JsonCalendarProvider,
    JsonEmployeeDataProvider,
    JsonFileOnboardingRepository,
    LocalUC06HandoffProvider,
    ProviderDataError,
    RegistryConfigurationProvider,
)
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    TaskTemplate,
)
from onboarding_task_management.sub_agent.service import (
    OnboardingService,
    OnboardingTrigger,
)
from onboarding_task_management.synthetic import build_registry, make_trigger, persona
from tests.support import HR_OPS, HR_OPS_APPROVER, HR_OPS_BETA, ServiceTestCase

ROOT = Path(__file__).resolve().parents[1]
MOCK_DIR = ROOT / "data" / "mock"
CANONICAL = ROOT / "data" / "UC02_Synthetic_Onboarding_Test_Data.json"

AS_OF_LATE = date(2026, 11, 10)
AS_OF_EARLY = date(2026, 10, 26)


def _payload(name: str) -> dict:
    data = json.loads((MOCK_DIR / name).read_text(encoding="utf-8"))
    data.pop("_mock_note", None)
    return data


# ---------------------------------------------------------------------------
# UC02-BR-02 — nationality as an opaque group, unfiltered by default
# ---------------------------------------------------------------------------


class NationalityGroupTests(unittest.TestCase):
    def _config(self) -> ClientOnboardingConfig:
        base = dict(
            category=TaskCategory.HR_RECORD,
            title="T",
            title_ar="ت",
            owner=Role.HR_OPS,
            offset_days=-1,
        )
        return ClientOnboardingConfig(
            approval_status="approved",  # test fixture
            client_id="C",
            templates=(
                TaskTemplate(key="everyone", **base),
                TaskTemplate(key="group_a_only", nationality_groups=("grp-a",), **base),
            ),
        )

    def test_unfiltered_by_default(self) -> None:
        # No client has an approved nationality policy, so a template without
        # nationality_groups applies to everyone.
        got = self._config().applicable(location="x", project="y", role_id="z")
        self.assertIn("everyone", {t.key for t in got})

    def test_group_restricted_template_excluded_without_a_group(self) -> None:
        got = self._config().applicable(location="x", project="y", role_id="z")
        self.assertNotIn("group_a_only", {t.key for t in got})

    def test_group_restricted_template_included_for_matching_group(self) -> None:
        got = self._config().applicable(
            location="x", project="y", role_id="z", nationality_group="grp-a"
        )
        self.assertEqual({t.key for t in got}, {"everyone", "group_a_only"})

    def test_non_matching_group_excluded(self) -> None:
        got = self._config().applicable(
            location="x", project="y", role_id="z", nationality_group="grp-b"
        )
        self.assertEqual({t.key for t in got}, {"everyone"})

    def test_no_nationality_value_is_ever_stored(self) -> None:
        # The contract carries a GROUP CODE, never a nationality.
        self.assertIn("nationality_group", OnboardingRecord.model_fields)
        self.assertNotIn("nationality", OnboardingRecord.model_fields)
        self.assertIn("nationality_group", OnboardingTrigger.model_fields)
        self.assertNotIn("nationality", OnboardingTrigger.model_fields)

    def test_group_flows_from_trigger_to_record(self) -> None:
        service = OnboardingService(build_registry())
        trigger = make_trigger(persona("EMP-001")).model_copy(
            update={"nationality_group": "grp-a"}
        )
        record = service.create_onboarding(HR_OPS, trigger)
        self.assertEqual(record.nationality_group, "grp-a")

    def test_shipped_config_declares_no_nationality_groups(self) -> None:
        # The policy is unresolved, so no shipped template filters on it.
        for template in build_registry().get("CLIENT-ALPHA").templates:
            with self.subTest(template=template.key):
                self.assertEqual(template.nationality_groups, ())


# ---------------------------------------------------------------------------
# UC02-BR-10 — escalation
# ---------------------------------------------------------------------------


class EscalationTests(ServiceTestCase):
    def items(self, as_of: date):
        return self.service.escalations(HR_OPS, self.onboarding_id, as_of)

    def test_nothing_escalates_before_anything_is_due(self) -> None:
        self.assertEqual(self.items(date(2026, 10, 16)), [])

    def test_overdue_task_escalates(self) -> None:
        items = [i for i in self.items(AS_OF_LATE) if i.subject_type == "task"]
        self.assertTrue(items)
        self.assertTrue(all(i.source_rule == "GR-12" for i in items))

    def test_first_responder_is_the_hr_officer(self) -> None:
        # Within the one-working-day response window (Framework §10).
        items = self.items(date(2026, 11, 3))
        first = [i for i in items if i.level is EscalationLevel.HR_OFFICER]
        self.assertTrue(first)
        self.assertTrue(all(i.escalate_to is Role.HR_OFFICER for i in first))

    def test_escalates_to_operations_lead_past_the_response_window(self) -> None:
        items = self.items(AS_OF_LATE)
        late = [i for i in items if i.level is EscalationLevel.HR_OPS_LEAD]
        self.assertTrue(late)
        self.assertTrue(all(i.escalate_to is Role.HR_LEAD for i in late))
        self.assertTrue(all(i.days_waiting > 1 for i in late if i.subject_type == "task"))

    def test_escalation_names_the_responsible_owner(self) -> None:
        for item in self.items(AS_OF_LATE):
            if item.subject_type == "task":
                with self.subTest(task=item.subject_id):
                    self.assertIsInstance(item.owner, Role)

    def test_delayed_milestone_escalates(self) -> None:
        milestones = [i for i in self.items(AS_OF_LATE) if i.subject_type == "milestone"]
        self.assertTrue(milestones)

    def test_invalid_owner_escalates_to_the_operations_lead(self) -> None:
        from onboarding_task_management.sub_agent.checklist import ConfigRegistry

        registry = ConfigRegistry()
        registry.register(
            ClientOnboardingConfig(
                approval_status="approved",  # test fixture
                client_id="CLIENT-BADOWNER",
                templates=(
                    TaskTemplate(
                        key="orphan",
                        category=TaskCategory.HR_RECORD,
                        title="T",
                        title_ar="ت",
                        owner=Role.SYSTEM,
                        offset_days=-1,
                    ),
                ),
            )
        )
        service = OnboardingService(registry)
        from onboarding_task_management.contracts import Actor

        actor = Actor(actor_id="U-X", role=Role.HR_OPS, client_id="CLIENT-BADOWNER")
        trigger = make_trigger(persona("EMP-001")).model_copy(
            update={"client_id": "CLIENT-BADOWNER"}
        )
        record = service.create_onboarding(actor, trigger)
        items = service.escalations(actor, record.onboarding_id, AS_OF_EARLY)
        config_items = [i for i in items if i.subject_type == "configuration"]
        self.assertEqual(len(config_items), 1)
        self.assertIs(config_items[0].escalate_to, Role.HR_LEAD)
        self.assertIn("invalid task owner", config_items[0].reason)

    def test_stalled_approval_escalates(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=date(2026, 11, 30)
        )
        # The date-change gate SLA is 24h. Evaluate relative to the request's
        # own creation date so the test does not depend on the wall clock.
        from datetime import timedelta

        as_of = request.created_at.date() + timedelta(days=3)
        items = self.service.escalations(HR_OPS, self.onboarding_id, as_of)
        approvals = [i for i in items if i.subject_type == "approval"]
        self.assertTrue(approvals)
        self.assertIn("stalled", approvals[0].reason)

    def test_decided_approval_does_not_escalate(self) -> None:
        request = self.service.request_change(
            HR_OPS, self.onboarding_id, joining_date=date(2026, 11, 30)
        )
        self.service.decide_change(
            HR_OPS_APPROVER, request.request_id, DecisionOutcome.REJECTED
        )
        from datetime import timedelta

        as_of = request.created_at.date() + timedelta(days=3)
        items = self.service.escalations(HR_OPS, self.onboarding_id, as_of)
        self.assertEqual([i for i in items if i.subject_type == "approval"], [])

    def test_escalation_is_audited(self) -> None:
        self.items(AS_OF_LATE)
        self.assertIn("escalation.detected", self.service.audit.actions_for(HR_OPS))

    def test_escalation_does_not_send_anything(self) -> None:
        # Escalation produces a list for UC-06; it has no transport.
        import pathlib

        import onboarding_task_management.sub_agent.tracker as tracker

        source = pathlib.Path(tracker.__file__).read_text(encoding="utf-8").lower()
        for forbidden in ("def send", "smtp", "httpx", "requests."):
            self.assertNotIn(forbidden, source)

    def test_handoff_carries_escalations(self) -> None:
        handoff = self.service.joining_handoff(HR_OPS, self.onboarding_id, AS_OF_LATE)
        self.assertTrue(handoff.escalations)
        self.assertFalse(handoff.case_closed_by_uc02)

    def test_another_client_cannot_read_escalations(self) -> None:
        with self.assertRaises(DataIsolationError):
            self.service.escalations(HR_OPS_BETA, self.onboarding_id, AS_OF_LATE)


# ---------------------------------------------------------------------------
# Integration boundaries — mock adapters
# ---------------------------------------------------------------------------


class ProviderTests(unittest.TestCase):
    def test_employee_provider_loads_a_payload(self) -> None:
        provider = JsonEmployeeDataProvider(MOCK_DIR)
        payload = provider.get_trigger_payload("CLIENT-ALPHA", "01_valid_onboarding")
        self.assertEqual(payload["client_id"], "CLIENT-ALPHA")

    def test_employee_provider_reports_a_missing_payload(self) -> None:
        provider = JsonEmployeeDataProvider(MOCK_DIR)
        with self.assertRaises(ProviderDataError):
            provider.get_trigger_payload("CLIENT-ALPHA", "no-such-employee")

    def test_employee_provider_rejects_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "broken.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ProviderDataError) as ctx:
                JsonEmployeeDataProvider.load_file(bad)
            self.assertIn("not valid JSON", str(ctx.exception))

    def test_configuration_provider_wraps_the_registry(self) -> None:
        provider = RegistryConfigurationProvider(build_registry())
        self.assertEqual(provider.get_config("CLIENT-ALPHA").client_id, "CLIENT-ALPHA")

    def test_configuration_provider_reports_missing_configuration(self) -> None:
        provider = RegistryConfigurationProvider(build_registry())
        with self.assertRaises(MissingConfigurationError):
            provider.get_config("CLIENT-NOT-CONFIGURED")

    def test_builtin_calendar_is_never_approved(self) -> None:
        self.assertFalse(BuiltInCalendarProvider().status().approved)

    def test_json_calendar_fixture_is_provisional(self) -> None:
        provider = JsonCalendarProvider(MOCK_DIR / "holidays_mock.json")
        status = provider.status()
        self.assertFalse(status.approved)
        self.assertTrue(status.warnings)
        self.assertEqual(len(provider.holidays()), 16)

    def test_json_calendar_can_declare_approval(self) -> None:
        provider = JsonCalendarProvider(MOCK_DIR / "holidays_approved_example.json")
        self.assertTrue(provider.status().approved)

    def test_json_calendar_rejects_a_missing_file(self) -> None:
        with self.assertRaises(ProviderDataError):
            JsonCalendarProvider(MOCK_DIR / "does_not_exist.json")

    def test_json_calendar_rejects_a_bad_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            path.write_text(json.dumps({"holidays": ["not-a-date"]}), encoding="utf-8")
            with self.assertRaises(ProviderDataError):
                JsonCalendarProvider(path).holidays()

    def test_in_memory_repository_roundtrip_and_isolation(self) -> None:
        repo = InMemoryOnboardingRepository()
        service = OnboardingService(build_registry())
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        repo.save(record)
        self.assertIsNotNone(repo.get("CLIENT-ALPHA", record.onboarding_id))
        self.assertIsNone(repo.get("CLIENT-BETA", record.onboarding_id))

    def test_in_memory_repository_returns_copies(self) -> None:
        repo = InMemoryOnboardingRepository()
        service = OnboardingService(build_registry())
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        repo.save(record)
        fetched = repo.get("CLIENT-ALPHA", record.onboarding_id)
        fetched.tasks[0].status = TaskStatus.COMPLETE
        again = repo.get("CLIENT-ALPHA", record.onboarding_id)
        self.assertNotEqual(again.tasks[0].status, TaskStatus.COMPLETE)

    def test_json_repository_persists_and_reloads(self) -> None:
        service = OnboardingService(build_registry())
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        with tempfile.TemporaryDirectory() as tmp:
            repo = JsonFileOnboardingRepository(Path(tmp))
            repo.save(record)
            # A NEW repository instance proves the data is on disk, not in RAM.
            reloaded = JsonFileOnboardingRepository(Path(tmp)).get(
                "CLIENT-ALPHA", record.onboarding_id
            )
            self.assertIsNotNone(reloaded)
            self.assertEqual(len(reloaded.tasks), len(record.tasks))
            self.assertEqual(reloaded.joining_date, record.joining_date)

    def test_json_repository_isolates_clients(self) -> None:
        service = OnboardingService(build_registry())
        alpha = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        with tempfile.TemporaryDirectory() as tmp:
            repo = JsonFileOnboardingRepository(Path(tmp))
            repo.save(alpha)
            self.assertIsNone(repo.get("CLIENT-BETA", alpha.onboarding_id))
            self.assertEqual(repo.list_for_client("CLIENT-BETA"), [])

    def test_json_repository_reports_a_corrupt_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = JsonFileOnboardingRepository(Path(tmp))
            (Path(tmp) / "CLIENT-ALPHA.json").write_text("{broken", encoding="utf-8")
            with self.assertRaises(ProviderDataError) as ctx:
                repo.get("CLIENT-ALPHA", "anything")
            self.assertIn("corrupt", str(ctx.exception))

    def test_json_repository_leaves_no_temp_files(self) -> None:
        service = OnboardingService(build_registry())
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        with tempfile.TemporaryDirectory() as tmp:
            JsonFileOnboardingRepository(Path(tmp)).save(record)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_notification_provider_never_sends(self) -> None:
        service = OnboardingService(build_registry())
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        drafts = service.reminder_drafts(HR_OPS, record.onboarding_id, AS_OF_LATE)
        provider = DraftOnlyNotificationProvider()
        for draft in drafts:
            provider.queue_draft(draft)
        self.assertEqual(len(provider.outcomes()), len(drafts))
        self.assertTrue(
            all(o["outcome"] == "queued_not_sent" for o in provider.outcomes())
        )

    def test_notification_provider_refuses_a_non_draft(self) -> None:
        service = OnboardingService(build_registry())
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        draft = service.reminder_drafts(HR_OPS, record.onboarding_id, AS_OF_LATE)[0]
        sent = draft.model_copy(update={"delivery_status": "sent"})
        with self.assertRaises(ProviderDataError):
            DraftOnlyNotificationProvider().queue_draft(sent)

    def test_uc06_provider_accepts_an_open_case(self) -> None:
        service = OnboardingService(build_registry())
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        handoff = service.joining_handoff(HR_OPS, record.onboarding_id, AS_OF_LATE)
        provider = LocalUC06HandoffProvider()
        provider.publish(handoff)
        self.assertEqual(len(provider.published()), 1)

    def test_uc06_provider_refuses_a_closed_case(self) -> None:
        service = OnboardingService(build_registry())
        record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-001")))
        handoff = service.joining_handoff(HR_OPS, record.onboarding_id, AS_OF_LATE)
        closed = handoff.model_copy(update={"case_closed_by_uc02": True})
        with self.assertRaises(ProviderDataError):
            LocalUC06HandoffProvider().publish(closed)

    def test_no_provider_performs_a_live_call(self) -> None:
        import pathlib

        import onboarding_task_management.providers as providers

        source = pathlib.Path(providers.__file__).read_text(encoding="utf-8")
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        banned = (
            "requests",
            "httpx",
            "urllib",
            "socket",
            "psycopg",
            "sqlalchemy",
            "supabase",
            "azure",
            "boto3",
        )
        for line in import_lines:
            for name in banned:
                with self.subTest(line=line, module=name):
                    self.assertNotIn(name, line)
        # No credential or endpoint handling in executable code.
        code = " ".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        ).lower()
        for secret in ("api_key =", "password =", "https://", "connection_string"):
            self.assertNotIn(secret, code)


# ---------------------------------------------------------------------------
# Scenario fixtures
# ---------------------------------------------------------------------------


class MockFixtureScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = OnboardingService(build_registry())

    def test_canonical_fixture_is_not_in_the_mock_directory(self) -> None:
        self.assertTrue(CANONICAL.exists())
        self.assertNotIn(CANONICAL.name, {p.name for p in MOCK_DIR.glob("*.json")})

    def test_every_fixture_is_labelled_as_mock(self) -> None:
        for path in MOCK_DIR.glob("*.json"):
            with self.subTest(fixture=path.name):
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("_mock_note", raw)
                self.assertIn("MOCK", raw["_mock_note"])

    def test_no_fixture_contains_personal_data(self) -> None:
        for path in MOCK_DIR.glob("*.json"):
            with self.subTest(fixture=path.name):
                text = path.read_text(encoding="utf-8").lower()
                for forbidden in ('"name"', '"email"', '"nationality"', "passport"):
                    self.assertNotIn(forbidden, text)

    def test_scenario_01_valid_onboarding(self) -> None:
        record = self.service.create_onboarding(
            HR_OPS, OnboardingTrigger(**_payload("01_valid_onboarding.json"))
        )
        self.assertEqual(len(record.tasks), 10)

    def test_scenario_02_offer_not_accepted(self) -> None:
        with self.assertRaises(PreconditionError):
            self.service.create_onboarding(
                HR_OPS, OnboardingTrigger(**_payload("02_offer_not_accepted.json"))
            )

    def test_scenario_03_form_not_approved(self) -> None:
        with self.assertRaises(PreconditionError):
            self.service.create_onboarding(
                HR_OPS, OnboardingTrigger(**_payload("03_form_not_approved.json"))
            )

    def test_scenario_03b_form_incomplete(self) -> None:
        with self.assertRaises(PreconditionError) as ctx:
            self.service.create_onboarding(
                HR_OPS, OnboardingTrigger(**_payload("03b_form_incomplete.json"))
            )
        self.assertIn("incomplete", str(ctx.exception))

    def test_scenario_04_missing_client_configuration(self) -> None:
        from onboarding_task_management.contracts import Actor

        payload = _payload("04_missing_client_config.json")
        actor = Actor(
            actor_id="U-X", role=Role.HR_OPS, client_id=payload["client_id"]
        )
        with self.assertRaises(MissingConfigurationError):
            self.service.create_onboarding(actor, OnboardingTrigger(**payload))

    def test_scenario_06_visa_activities_apply(self) -> None:
        record = self.service.create_onboarding(
            HR_OPS, OnboardingTrigger(**_payload("06_visa_activities.json"))
        )
        keys = {t.template_key for t in record.tasks}
        for expected in ("background_verification", "visa_pack_handover", "visa_processing"):
            self.assertIn(expected, keys)
        self.assertNotIn("travel_booking", keys)

    def test_scenario_07_full_relocation_applies_everything(self) -> None:
        record = self.service.create_onboarding(
            HR_OPS, OnboardingTrigger(**_payload("07_full_relocation.json"))
        )
        keys = {t.template_key for t in record.tasks}
        for expected in (
            "travel_booking",
            "accommodation_booking",
            "food_arrangement",
            "salary_transfer_letter",
            "non_liability_letter",
            "hr_system_activation",
            "family_insurance_enrolment",
        ):
            with self.subTest(task=expected):
                self.assertIn(expected, keys)

    def test_scenario_08_evidence_required(self) -> None:
        record = self.service.create_onboarding(
            HR_OPS, OnboardingTrigger(**_payload("08_evidence_required.json"))
        )
        oid = record.onboarding_id
        self.service.complete_task(HR_OPS, oid, f"{oid}#hr_record_create")
        with self.assertRaises(EvidenceRequiredError):
            self.service.complete_task(HR_OPS, oid, f"{oid}#salary_transfer_letter")

    def test_scenario_11_cross_client_access_denied(self) -> None:
        alpha = self.service.create_onboarding(
            HR_OPS, OnboardingTrigger(**_payload("01_valid_onboarding.json"))
        )
        with self.assertRaises(DataIsolationError):
            self.service.get(HR_OPS_BETA, alpha.onboarding_id)

    def test_scenario_13_missing_calendar_coverage(self) -> None:
        with self.assertRaises(CalendarRangeError):
            self.service.create_onboarding(
                HR_OPS, OnboardingTrigger(**_payload("13_missing_calendar_coverage.json"))
            )

    def test_scenario_14_local_hire_has_no_extended_activities(self) -> None:
        record = self.service.create_onboarding(
            HR_OPS, OnboardingTrigger(**_payload("14_local_hire_no_extras.json"))
        )
        keys = {t.template_key for t in record.tasks}
        for absent in ("travel_booking", "visa_processing", "background_verification"):
            self.assertNotIn(absent, keys)

    def test_duplicate_request_is_rejected(self) -> None:
        payload = _payload("01_valid_onboarding.json")
        self.service.create_onboarding(HR_OPS, OnboardingTrigger(**payload))
        from onboarding_task_management.errors import DuplicateOnboardingError

        with self.assertRaises(DuplicateOnboardingError):
            self.service.create_onboarding(HR_OPS, OnboardingTrigger(**payload))

    def test_evidence_reference_fixture_has_no_document_contents(self) -> None:
        data = json.loads(
            (MOCK_DIR / "evidence_references_mock.json").read_text(encoding="utf-8")
        )
        for item in data["evidence"]:
            with self.subTest(item=item["template_key"]):
                self.assertEqual(set(item), {"template_key", "evidence_ref"})


# ---------------------------------------------------------------------------
# Previously untested extended activities
# ---------------------------------------------------------------------------


class RemainingActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = OnboardingService(build_registry())
        self.record = self.service.create_onboarding(
            HR_OPS, OnboardingTrigger(**_payload("07_full_relocation.json"))
        )

    def task(self, key: str):
        found = self.record.task_by_key(key)
        assert found is not None, key
        return found

    def test_biometrics_has_owner_due_date_and_evidence(self) -> None:
        task = self.task("biometrics_capture")
        self.assertEqual(task.owner, Role.HR_OPS)
        self.assertEqual(task.category, TaskCategory.BIOMETRICS)
        self.assertTrue(task.evidence_required)
        self.assertEqual(task.approval_role, Role.HR_OPS)

    def test_biometrics_depends_on_the_medical_test(self) -> None:
        self.assertIn(
            f"{self.record.onboarding_id}#medical_test",
            self.task("biometrics_capture").depends_on,
        )
        self.assertEqual(self.task("biometrics_capture").status, TaskStatus.BLOCKED)

    def test_food_arrangement_has_owner_and_spend_approver(self) -> None:
        task = self.task("food_arrangement")
        self.assertEqual(task.owner, Role.FACILITIES)
        self.assertEqual(task.category, TaskCategory.FOOD)
        self.assertEqual(task.approval_role, Role.HR_MANAGER)

    def test_food_arrangement_requires_no_evidence(self) -> None:
        # Deliberate configuration choice, asserted so a silent change is caught.
        self.assertFalse(self.task("food_arrangement").evidence_required)
        oid = self.record.onboarding_id
        completed = self.service.complete_task(
            HR_OPS, oid, f"{oid}#food_arrangement"
        )
        self.assertEqual(completed.status, TaskStatus.COMPLETE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
