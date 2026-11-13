"""Existing clients (CLIENT_CANADA, CLIENT_DUBAI, CLIENT_INDIA, CLIENT_USA, plus SYN-UAE-TEST and test_enterprise_corp): configuration retrieval,
attribute matching, dynamic checklist, cross-client isolation and lifecycle gates.

LOCAL MOCK tests. The mock database is loaded with a READ-ONLY snapshot of the live configuration rows
(``fixtures/existing_clients_config_snapshot.json``, captured 2026-11-04), so the starting state is the real one; any state change (inactive, expired,
future version, an approved-but-empty client) is made to the in-memory copy only. Nothing here touches Supabase.

The three gaps found by the first audit are fixed and tested here: a missing location status is flagged for HR review, task rows carry the
configuration version, and a client without an eligibility catalogue is reported as "not validated" (strict validation of clients that declare one
is in tests/test_four_mock_clients.py).
"""

from __future__ import annotations

import copy
import json
import unittest
from datetime import date
from pathlib import Path

from onboarding_task_management.contracts import Actor, ChecklistContext, Role
from onboarding_task_management.errors import MissingConfigurationError
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.supabase_config import SupabaseCalendars, SupabaseConfigRegistry, SupabaseConfigSource
from onboarding_task_management.supabase_rest import SupabaseRestClient
from onboarding_task_management import synthetic_profiles as sp
from tests.test_supabase_store import FakePostgrest

SNAP = json.loads((Path(__file__).parent / "fixtures" / "existing_clients_config_snapshot.json").read_text(encoding="utf-8"))
TABLES = ["client_configs", "client_config_versions", "task_templates", "client_document_requirements", "client_service_policies",
          "client_milestone_templates", "client_holidays"]
EXISTING = ["CLIENT_CANADA", "CLIENT_DUBAI", "CLIENT_INDIA", "CLIENT_USA"]
SYN = "SYN-UAE-TEST"
ON = date(2026, 12, 14)
#: An EXPLICITLY enabled test environment: synthetic / mock configuration is denied everywhere else (config_safety).
TEST_ENV = {"UC02_ENV": "test", "UC02_ALLOW_SYNTHETIC_CONFIG": "1"}


def snapshot_db(mutate=None):
    db = FakePostgrest()
    tables = copy.deepcopy({t: SNAP[t] for t in TABLES})
    if mutate:
        mutate(tables)
    for t in TABLES:
        db.tables[t] = tables[t]
    return db, SupabaseRestClient("https://example.supabase.co", "k", transport=db)


def source(rest, allow_synthetic=True):
    return SupabaseConfigSource(rest, allow_synthetic=allow_synthetic, environ=TEST_ENV)


def row(tables, table, **match):
    return next(r for r in tables[table] if all(r.get(k) == v for k, v in match.items()))


def make_real(client):
    """In the MOCK copy only: turn an existing mock client into an approved, non-mock one (to prove nothing is borrowed from another client)."""
    def change(tables):
        cfg = row(tables, "client_configs", client_id=client)
        cfg["settings_payload"] = {**cfg["settings_payload"], "environment": "test"}
        cfg["config_hash"] = "test-hash"
        v = row(tables, "client_config_versions", client_id=client)
        v.update(status="approved", approved_by="test-approver", approved_at="2026-11-02T00:00:00+00:00", config_hash="test-hash")
    return change


class ExistingClientInventoryTests(unittest.TestCase):
    """What the live rows actually contain (from the snapshot)."""

    def test_each_existing_client_has_only_a_master_row_and_a_draft_version(self) -> None:
        for c in EXISTING:
            cfg = [r for r in SNAP["client_configs"] if r["client_id"] == c]
            versions = [r for r in SNAP["client_config_versions"] if r["client_id"] == c]
            self.assertEqual(len(cfg), 1, c)
            self.assertTrue(cfg[0]["is_active"])
            self.assertEqual(cfg[0]["settings_payload"].get("environment"), "mock")
            self.assertEqual([(v["version"], v["status"], v["approved_by"]) for v in versions], [(1, "draft", None)])
            for t in ("task_templates", "client_document_requirements", "client_service_policies", "client_milestone_templates"):
                self.assertEqual([r for r in SNAP[t] if r["client_id"] == c], [], f"{c} {t}")
        self.assertEqual(len([r for r in SNAP["client_holidays"] if r["client_id"] == "CLIENT_DUBAI"]), 2)
        for c in ("CLIENT_CANADA", "CLIENT_INDIA", "CLIENT_USA"):
            self.assertEqual([r for r in SNAP["client_holidays"] if r["client_id"] == c], [])

    def test_no_orphan_or_duplicate_configuration_rows(self) -> None:
        clients = {r["client_id"] for r in SNAP["client_configs"]}
        versions = {(r["client_id"], r["version"]) for r in SNAP["client_config_versions"]}
        for t in TABLES[1:]:
            self.assertTrue(all(r["client_id"] in clients for r in SNAP[t]), t)
        for t in ("task_templates", "client_document_requirements", "client_service_policies", "client_milestone_templates"):
            self.assertTrue(all((r["client_id"], r["config_version"]) in versions for r in SNAP[t]), t)
        keys = [(r["client_id"], r["template_key"]) for r in SNAP["task_templates"]]
        self.assertEqual(len(keys), len(set(keys)))


class RetrievalTests(unittest.TestCase):
    def test_the_four_existing_clients_are_refused_today(self) -> None:
        db, rest = snapshot_db()
        for c in EXISTING:
            with self.assertRaisesRegex(MissingConfigurationError, "marked mock"):
                source(rest, allow_synthetic=False).load(c, ON)  # a runtime that did not opt in to test data
            with self.assertRaisesRegex(MissingConfigurationError, "'draft', not approved"):
                source(rest, allow_synthetic=True).load(c, ON)  # opted in: the mock gate passes, the draft version is still refused

    def test_their_only_version_is_a_draft_so_the_version_gate_also_refuses(self) -> None:
        db, rest = snapshot_db()
        for c in EXISTING:
            with self.assertRaisesRegex(MissingConfigurationError, "'draft', not approved"):
                source(rest)._version_row(c, ON)

    def test_missing_invalid_and_wrong_case_client_ids_are_refused_without_fallback(self) -> None:
        db, rest = snapshot_db()
        # (a lower-case 'client_dubai' is refused by live PostgreSQL - see the audit report; the mock database compares strings case-insensitively,
        # so case sensitivity is NOT provable here and is not asserted)
        for c in ("CLIENT_MISSING_XYZ", "", "CLIENT-DUBAI", "CLIENT DUBAI"):
            with self.assertRaisesRegex(MissingConfigurationError, "not in client_configs"):
                source(rest).load(c, ON)
        with self.assertRaisesRegex(MissingConfigurationError, "no configuration version"):
            source(rest).load("test_enterprise_corp", ON)

    def test_the_synthetic_client_loads_only_when_the_runtime_opts_in_and_returns_only_its_own_rows(self) -> None:
        db, rest = snapshot_db()
        with self.assertRaisesRegex(MissingConfigurationError, "synthetic test fixture"):
            source(rest, allow_synthetic=False).load(SYN, ON)
        cfg = source(rest).load(SYN, ON)
        self.assertEqual((cfg.client_id, cfg.version, cfg.data_origin), (SYN, "1", "synthetic"))
        self.assertEqual({t.key for t in cfg.templates}, {r["template_key"] for r in SNAP["task_templates"] if r["client_id"] == SYN})
        self.assertEqual({d.key for d in cfg.document_requirements},
                         {r["requirement_key"] for r in SNAP["client_document_requirements"] if r["client_id"] == SYN})
        self.assertEqual(len(cfg.assignments), 14)
        self.assertEqual(cfg.milestones, ())

    def test_the_calendar_comes_from_the_requested_clients_rows(self) -> None:
        db, rest = snapshot_db()
        cal = source(rest).calendar(SYN)
        self.assertEqual(sorted(str(d) for d in cal), ["2026-12-02", "2026-12-03"])
        self.assertEqual(set(cal.weekend_weekdays), {5, 6})
        self.assertFalse(cal.approved)


class LifecycleGateTests(unittest.TestCase):
    """State changes on the MOCK copy of SYN-UAE-TEST only."""

    def refused(self, change, pattern):
        db, rest = snapshot_db(change)
        with self.assertRaisesRegex(MissingConfigurationError, pattern):
            source(rest).load(SYN, ON)

    def test_inactive_client(self) -> None:
        self.refused(lambda t: row(t, "client_configs", client_id=SYN).__setitem__("is_active", False), "not active")

    def test_expired_version(self) -> None:
        self.refused(lambda t: row(t, "client_config_versions", client_id=SYN).__setitem__("effective_to", "2026-06-30"), "ended on 2026-06-30")

    def test_future_effective_version(self) -> None:
        self.refused(lambda t: row(t, "client_config_versions", client_id=SYN).__setitem__("effective_from", "2027-01-01"), "not effective until")

    def test_approved_without_approver(self) -> None:
        self.refused(lambda t: row(t, "client_config_versions", client_id=SYN).__setitem__("approved_by", None), "without an approver")

    def test_missing_version(self) -> None:
        self.refused(lambda t: t["client_config_versions"].clear(), "no configuration version")


class CrossClientIsolationTests(unittest.TestCase):
    def test_an_existing_client_made_valid_in_the_mock_never_borrows_another_clients_templates(self) -> None:
        for c in EXISTING:
            db, rest = snapshot_db(make_real(c))
            with self.assertRaisesRegex(MissingConfigurationError, "no active task templates"):
                source(rest).load(c, ON)  # SYN-UAE-TEST's 18 templates are never used for it

    def test_a_template_reassigned_to_another_client_disappears_from_the_original_client(self) -> None:
        def move(t):
            row(t, "task_templates", client_id=SYN, template_key="payroll_setup")["client_id"] = "CLIENT_DUBAI"
        db, rest = snapshot_db(move)
        cfg = source(rest).load(SYN, ON)
        self.assertNotIn("payroll_setup", {t.key for t in cfg.templates})

    def test_documents_and_holidays_never_leak_between_clients(self) -> None:
        def add(t):
            t["client_holidays"].append({"client_id": "CLIENT_DUBAI", "holiday_date": "2026-12-25", "name": "[SAMPLE] other", "is_observed": True})
            extra = copy.deepcopy(row(t, "client_document_requirements", client_id=SYN, requirement_key="identity"))
            extra.update(client_id="CLIENT_INDIA", requirement_key="india_only_doc", requirement_id="00000000-0000-0000-0000-000000000001")
            t["client_document_requirements"].append(extra)
        db, rest = snapshot_db(add)
        self.assertNotIn("2026-12-25", {str(d) for d in source(rest).calendar(SYN)})
        self.assertNotIn("india_only_doc", {d.key for d in source(rest).load(SYN, ON).document_requirements})

    def test_versions_are_never_mixed(self) -> None:
        def v2(t):
            t["client_config_versions"].append({"client_id": SYN, "version": 2, "effective_from": "2026-11-01", "effective_to": None,
                                                "status": "approved", "config_hash": "h2", "approved_by": "x", "approved_at": "2026-11-01T00:00:00+00:00"})
        db, rest = snapshot_db(v2)
        # version 2 is the one in force; it has no templates of its own, so the load fails rather than reusing version 1's rows
        with self.assertRaisesRegex(MissingConfigurationError, "no active task templates"):
            source(rest).load(SYN, ON)


def service_over(rest) -> OnboardingService:
    src = source(rest)
    return OnboardingService(SupabaseConfigRegistry(source=src), calendars=SupabaseCalendars(src), require_client_calendar=True)


def context(profile: str, **over) -> ChecklistContext:
    p = sp.PROFILES[profile]
    attrs = list(p.attributes) + ([f"location_status:{p.current_location_status}"] if p.current_location_status else [])
    base = dict(client_id=SYN, joining_date=ON, location=p.location, project="synthetic-project", role_id=p.role_id, grade=p.grade,
                nationality_group=p.nationality_group, attributes=attrs)
    base.update(over)
    return ChecklistContext(**base)


HR = Actor(actor_id="audit", role=Role.HR_OPS, client_id=SYN)


class AttributeMatchingTests(unittest.TestCase):
    def preview(self, ctx):
        db, rest = snapshot_db()
        return service_over(rest).preview_checklist(HR, ctx)

    def test_existing_mock_clients_cannot_produce_a_checklist(self) -> None:
        db, rest = snapshot_db()
        svc = service_over(rest)
        for c in EXISTING:
            with self.assertRaises(MissingConfigurationError):
                svc.preview_checklist(Actor(actor_id="audit", role=Role.HR_OPS, client_id=c), context("uae_inside", client_id=c))

    def test_in_country_and_overseas_routes_are_mutually_exclusive(self) -> None:
        inside = {d.template_key for d in self.preview(context("uae_inside")).included}
        overseas = {d.template_key for d in self.preview(context("uae_overseas")).included}
        self.assertTrue({"visa_status_change", "medical_test", "biometrics"} <= inside)
        self.assertFalse({"entry_permit", "medical_test_arrival", "biometrics_arrival", "flight_booking"} & inside)
        self.assertTrue({"entry_permit", "medical_test_arrival", "biometrics_arrival", "flight_booking", "accommodation"} <= overseas)
        self.assertFalse({"visa_status_change", "medical_test", "biometrics"} & overseas)

    def test_grade_filters_and_attribute_filters_are_applied(self) -> None:
        low = self.preview(context("uae_inside", grade="L2"))
        excluded = {d.template_key: d.reason_code for d in low.excluded}
        self.assertEqual((excluded["system_access"], excluded["senior_briefing"]), ("excluded_by_grade", "excluded_by_grade"))
        no_insurance = self.preview(context("uae_inside", attributes=["location_status:in_country", "bv_required"]))
        self.assertIn("insurance_enrolment", {d.template_key for d in no_insurance.excluded})

    def test_a_missing_attribute_is_never_treated_as_a_match(self) -> None:
        unknown = self.preview(context("uae_unstated"))
        included = {d.template_key for d in unknown.included}
        self.assertFalse({"visa_status_change", "entry_permit", "medical_test", "medical_test_arrival", "biometrics", "biometrics_arrival"} & included)

    def test_every_included_task_is_traceable_to_a_template_of_the_same_client(self) -> None:
        pv = self.preview(context("uae_inside"))
        own = {r["template_key"] for r in SNAP["task_templates"] if r["client_id"] == SYN}
        self.assertTrue(pv.included and all(d.template_key in own for d in pv.included))
        self.assertTrue(all(d.reason_code for d in pv.included + pv.excluded))
        self.assertEqual(set(pv.due_dates), {d.template_key for d in pv.included})

    def test_documents_follow_the_same_attribute_rules(self) -> None:
        db, rest = snapshot_db()
        cfg = source(rest).load(SYN, ON)

        def docs(profile):
            p = sp.PROFILES[profile]
            attrs = list(p.attributes) + ([f"location_status:{p.current_location_status}"] if p.current_location_status else [])
            inc, _ = cfg.documents_for(location=p.location, project="synthetic-project", role_id=p.role_id, grade=p.grade,
                                       nationality_group=p.nationality_group, attributes=attrs)
            return {d.key for d, _ in inc}
        self.assertEqual(docs("uae_inside"), {"identity", "education", "status_change_pack", "senior_declaration", "family_cover_form"})
        self.assertEqual(docs("uae_overseas"), {"identity", "education", "entry_permit_pack"})
        self.assertEqual(docs("uae_unstated"), {"identity", "education"})

    def test_without_a_catalogue_role_and_location_are_not_validated_and_the_checklist_says_so(self) -> None:
        """SYN-UAE-TEST stores no eligibility catalogue (no uc01 section). UC-02 cannot invent one, so it reports that the values were
        not validated. Clients that declare a catalogue are validated strictly (tests/test_four_mock_clients.py)."""
        for over in ({"role_id": "astronaut"}, {"location": "Atlantis"}):
            pv = self.preview(context("uae_inside", **over))
            self.assertIn("eligibility_not_configured", {i.code for i in pv.issues})

    def test_a_missing_location_status_is_flagged_for_hr_review_and_lists_the_withheld_tasks(self) -> None:
        pv = self.preview(context("uae_unstated"))
        self.assertTrue(pv.review_required)
        issue = next(i for i in pv.issues if i.code == "attribute_unstated" and i.subject == "location_status")
        for key in ("visa_status_change", "entry_permit", "medical_test", "biometrics"):
            self.assertIn(key, issue.detail)


class PersistenceTraceabilityTests(unittest.TestCase):
    """Generated plan persisted through the Supabase adapter on the schema-enforcing mock (the synthetic fixture client)."""

    def test_persisted_tasks_carry_client_case_version_and_source_template(self) -> None:
        from tests.test_supabase_only_workflow import ENV, SPEC, prepared
        from onboarding_task_management.sub_agent import supabase_demo as demo

        db, rest = prepared(approve=False)
        tasks = rest.select("onboarding_tasks", {"case_id": SPEC.case_id})
        own = {r["template_key"] for r in rest.select("task_templates", {"client_id": SPEC.client_id})}
        self.assertTrue(tasks)
        for t in tasks:
            self.assertEqual((t["client_id"], t["case_id"]), (SPEC.client_id, SPEC.case_id))
            self.assertIn(t["template_key"], own)
        self.assertEqual(demo.verify(ENV, rest=rest, spec=SPEC)["config_version"], "1")  # the plan's version, in the case-state snapshot
        deps = [e for t in tasks for e in rest.select("task_dependencies", {"task_id": t["task_id"]})]
        self.assertTrue(deps and all(e["depends_on_task_id"].startswith(SPEC.case_id) for e in deps))
        before = len(tasks)
        demo.recover(ENV, rest=rest, spec=SPEC, mock_downstream=True)  # restart + replayed event
        self.assertEqual(len(rest.select("onboarding_tasks", {"case_id": SPEC.case_id})), before)

    def test_each_task_row_stores_the_configuration_version(self) -> None:
        """Fixes the live finding of 2026-11-04 (config_version NULL on all 13 persisted tasks): the adapter now writes it."""
        from tests.test_supabase_only_workflow import SPEC, prepared

        db, rest = prepared(approve=False)
        tasks = rest.select("onboarding_tasks", {"case_id": SPEC.case_id})
        self.assertTrue(all(t.get("config_version") == 1 for t in tasks))


if __name__ == "__main__":
    unittest.main()
