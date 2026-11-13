"""Approved client configuration read from (mocked) Supabase responses. No network, no writes."""

from __future__ import annotations

import json
import unittest
from datetime import date
from urllib.parse import parse_qsl, urlparse

from onboarding_task_management.contracts import Actor, DecisionOutcome, Role
from onboarding_task_management.errors import CalendarConfigurationError, MissingConfigurationError
from onboarding_task_management.persistence import InMemoryTableClient, OnboardingStore
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.supabase_config import (
    SupabaseCalendars,
    SupabaseConfigRegistry,
    SupabaseConfigSource,
)
from onboarding_task_management.supabase_rest import SupabaseRestClient
from onboarding_task_management.synthetic import make_trigger, persona

JOIN = date(2026, 11, 16)
HR = Actor(actor_id="U-REQ", role=Role.HR_OPS, client_id="CLIENT_X")
APPROVER = Actor(actor_id="U-APPR", role=Role.HR_OPS, client_id="CLIENT_X")


class Db:
    """Read-only PostgREST stand-in over plain row lists."""

    def __init__(self, **tables):
        self.tables = tables
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append(method)
        assert method == "GET", "the config reader must never write"
        parts = urlparse(url)
        table = parts.path.rsplit("/rest/v1/", 1)[1]
        query = parse_qsl(parts.query)
        rows = list(self.tables.get(table, []))
        for col, expr in query:
            if col in ("select", "order", "limit"):
                continue
            op, _, val = expr.partition(".")
            rows = [r for r in rows if str(r.get(col)).lower() == val.lower()]
        order = dict(query).get("order")
        if order:
            col, _, d = order.partition(".")
            rows.sort(key=lambda r: r.get(col), reverse=d == "desc")
        return 200, rows


def tmpl(key, category="hr_record", owner="hr_ops", offset=-5, version=1, **kw):
    row = {"template_key": key, "client_id": "CLIENT_X", "category": category, "title": key, "title_ar": "",
           "owner_role": owner, "offset_days": offset, "evidence_required": False, "requires_attributes": [],
           "sensitive_to": [], "approval_role": None, "role_ids": [], "grades": [], "locations": [], "projects": [],
           "nationality_groups": [], "depends_on_template_keys": [], "default_priority": "normal",
           "config_version": version, "is_active": True}
    row.update(kw)
    return row


def version(v=1, status="approved", **kw):
    row = {"client_id": "CLIENT_X", "version": v, "effective_from": "2026-01-01", "effective_to": None,
           "status": status, "config_hash": "sha-abc", "approved_by": "hr-lead", "approved_at": "2026-01-01T00:00:00+00:00"}
    row.update(kw)
    return row


def client_row(**kw):
    row = {"client_id": "CLIENT_X", "is_active": True, "config_hash": "real-1", "working_days": [1, 2, 3, 4, 5],
           "settings_payload": {}}
    row.update(kw)
    return row


def source(db: Db) -> SupabaseConfigSource:
    return SupabaseConfigSource(SupabaseRestClient("https://example.supabase.co", "k", transport=db))


def healthy(**over) -> Db:
    tables = {"client_configs": [client_row()], "client_config_versions": [version()],
              "task_templates": [tmpl("a"), tmpl("b", category="background_verification", offset=-4,
                                                 depends_on_template_keys=["a"])],
              "client_milestone_templates": [], "client_holidays": [
                  {"client_id": "CLIENT_X", "holiday_date": "2026-12-02", "name": "x", "is_observed": True}]}
    tables.update(over)
    return Db(**tables)


class ConfigRetrievalTests(unittest.TestCase):
    def test_an_approved_version_becomes_a_config_with_its_templates_and_pins_the_version(self) -> None:
        src = source(healthy())
        config = src.load("CLIENT_X", JOIN)
        self.assertEqual((config.client_id, config.version, config.approval_status), ("CLIENT_X", "1", "approved"))
        self.assertEqual({t.key for t in config.templates}, {"a", "b"})
        self.assertEqual(config.templates[1].depends_on, ("a",))
        self.assertEqual(src.loaded["CLIENT_X"], {"version": 1, "config_hash": "sha-abc"})

    def test_only_get_requests_are_ever_made(self) -> None:
        db = healthy()
        source(db).load("CLIENT_X", JOIN)
        self.assertEqual(set(db.calls), {"GET"})

    def test_a_missing_client_is_a_gap(self) -> None:
        with self.assertRaises(MissingConfigurationError):
            source(healthy(client_configs=[])).load("CLIENT_X", JOIN)

    def test_an_inactive_client_is_a_gap(self) -> None:
        with self.assertRaises(MissingConfigurationError):
            source(healthy(client_configs=[client_row(is_active=False)])).load("CLIENT_X", JOIN)

    def test_mock_clients_are_refused_for_real_onboarding(self) -> None:
        for row in (client_row(config_hash="mock-usa-v1"), client_row(settings_payload={"environment": "mock"})):
            with self.assertRaises(MissingConfigurationError) as ctx:
                source(healthy(client_configs=[row])).load("CLIENT_X", JOIN)
            self.assertIn("mock", str(ctx.exception))

    def test_a_draft_or_unapproved_version_is_never_treated_as_approved(self) -> None:
        for versions, expect in (
            ([version(status="draft")], "draft"),
            ([version(approved_by=None)], "without an approver"),
            ([version(approved_at=None)], "without an approver"),
            ([version(config_hash="")], "no config_hash"),
            ([version(status="retired")], "retired"),
            ([], "no configuration version"),
        ):
            with self.subTest(expect=expect), self.assertRaises(MissingConfigurationError) as ctx:
                source(healthy(client_config_versions=versions)).load("CLIENT_X", JOIN)
            self.assertIn(expect, str(ctx.exception.__dict__) + str(ctx.exception))

    def test_the_version_in_force_on_the_joining_date_is_chosen(self) -> None:
        versions = [version(1, effective_to="2026-06-30"), version(2, effective_from="2026-07-01")]
        db = healthy(client_config_versions=versions,
                     task_templates=[tmpl("old", version=1), tmpl("new", version=2)])
        self.assertEqual({t.key for t in source(db).load("CLIENT_X", date(2026, 11, 16)).templates}, {"new"})
        self.assertEqual({t.key for t in source(db).load("CLIENT_X", date(2026, 3, 1)).templates}, {"old"})
        with self.assertRaises(MissingConfigurationError):
            source(healthy(client_config_versions=[version(effective_from="2027-01-01")])).load("CLIENT_X", JOIN)

    def test_a_draft_newer_version_does_not_replace_the_approved_one(self) -> None:
        db = healthy(client_config_versions=[version(1), version(2, status="draft")],
                     task_templates=[tmpl("v1only", version=1), tmpl("draft", version=2)])
        self.assertEqual({t.key for t in source(db).load("CLIENT_X", JOIN).templates}, {"v1only"})

    def test_no_active_templates_is_a_gap(self) -> None:
        with self.assertRaises(MissingConfigurationError):
            source(healthy(task_templates=[tmpl("a", is_active=False)])).load("CLIENT_X", JOIN)

    def test_malformed_templates_are_reported_together_not_silently_skipped(self) -> None:
        bad = [tmpl("a", category="not_a_category"), tmpl("b", owner="nobody"), tmpl("c", offset=None), tmpl("ok")]
        with self.assertRaises(MissingConfigurationError) as ctx:
            source(healthy(task_templates=bad)).load("CLIENT_X", JOIN)
        text = str(ctx.exception.__dict__)
        for key in ("'a'", "'b'", "'c'"):
            self.assertIn(key, text)

    def test_a_dependency_on_an_unknown_template_is_a_gap(self) -> None:
        with self.assertRaises(MissingConfigurationError):
            source(healthy(task_templates=[tmpl("a", depends_on_template_keys=["missing"])])).load("CLIENT_X", JOIN)

    def test_milestones_of_the_same_version_are_loaded(self) -> None:
        ms = [{"client_id": "CLIENT_X", "config_version": 1, "milestone_key": "m", "title": "M", "offset_days": -1,
               "required_task_keys": ["a"], "eligibility_rules": {}, "is_active": True}]
        config = source(healthy(client_milestone_templates=ms)).load("CLIENT_X", JOIN)
        self.assertEqual([(m.key, m.requires) for m in config.milestones], [("m", ("a",))])


class DifferentClientsDifferentChecklists(unittest.TestCase):
    """The same employee profile gets different plans from different approved configurations and filters."""

    def keys(self, templates, **trigger_kw) -> set[str]:
        db = healthy(task_templates=templates)
        registry = SupabaseConfigRegistry(source=source(db))
        calendars = SupabaseCalendars(source(db))
        service = OnboardingService(registry, calendars=calendars, require_client_calendar=True)
        p = persona("EMP-001").__class__(**{**persona("EMP-001").__dict__, "client_id": "CLIENT_X"})
        trigger = make_trigger(p, joining_date=JOIN)
        return {t.template_key for t in service.create_onboarding(HR, trigger).tasks}

    def test_role_grade_location_and_attribute_filters_decide_which_tasks_apply(self) -> None:
        p = persona("EMP-001")
        templates = [
            tmpl("everyone"),
            tmpl("this_role", role_ids=[p.role_id]), tmpl("other_role", role_ids=["someone_else"]),
            tmpl("this_place", locations=[p.location]), tmpl("other_place", locations=["Nowhere"]),
            tmpl("this_grade", grades=[p.grade]), tmpl("other_grade", grades=["ZZ"]),
            tmpl("needs_flag", requires_attributes=["needs_travel"]),
        ]
        self.assertEqual(self.keys(templates), {"everyone", "this_role", "this_place", "this_grade"})

    def test_a_different_client_configuration_gives_a_different_checklist(self) -> None:
        one = self.keys([tmpl("a"), tmpl("b", category="medical")])
        two = self.keys([tmpl("a"), tmpl("c", category="insurance")])
        self.assertNotEqual(one, two)

    def test_owner_roles_come_from_the_configuration_not_from_code(self) -> None:
        db = healthy(task_templates=[tmpl("a", owner="finance"), tmpl("b", owner="it_manager")])
        config = source(db).load("CLIENT_X", JOIN)
        self.assertEqual({t.key: t.owner for t in config.templates}, {"a": Role.FINANCE, "b": Role.IT_MANAGER})


class RegistryAndCalendarTests(unittest.TestCase):
    def test_the_registry_refuses_an_unapproved_client_and_uses_no_synthetic_fallback(self) -> None:
        registry = SupabaseConfigRegistry(source=source(healthy(client_config_versions=[version(status="draft")])))
        with self.assertRaises(MissingConfigurationError):
            registry.get("CLIENT_X", on=JOIN)
        with self.assertRaises(MissingConfigurationError):
            SupabaseConfigRegistry(source=source(healthy())).get("CLIENT-ALPHA", on=JOIN)  # not in the database

    def test_a_request_cannot_name_its_own_configuration(self) -> None:
        # The only lookup key is the client id; there is no argument that carries a config or a path.
        import inspect

        self.assertEqual(list(inspect.signature(SupabaseConfigRegistry.get).parameters), ["self", "client_id", "on"])

    def test_the_calendar_comes_from_working_days_and_holidays(self) -> None:
        cal = source(healthy(client_configs=[client_row(working_days=[7, 1, 2, 3, 4])])).calendar("CLIENT_X")
        self.assertEqual(cal.weekend_weekdays, frozenset({4, 5}))  # Fri/Sat weekend when Sun-Thu work
        self.assertIn(date(2026, 12, 2), cal)
        self.assertFalse(cal.approved)  # nothing in the schema approves a calendar

    def test_a_client_without_holidays_has_no_calendar_and_none_is_assumed(self) -> None:
        with self.assertRaises(CalendarConfigurationError):
            source(healthy(client_holidays=[])).calendar("CLIENT_X")
        with self.assertRaises(CalendarConfigurationError):
            "CLIENT_X" in SupabaseCalendars(source(healthy(client_holidays=[])))

    def test_build_service_in_memory_mode_keeps_the_synthetic_registry_for_local_use(self) -> None:
        from onboarding_task_management.sub_agent.runtime import build_service

        self.assertNotIsInstance(build_service({}).registry, SupabaseConfigRegistry)

    def test_build_service_in_supabase_mode_reads_config_from_the_database_never_the_synthetic_registry(self) -> None:
        from unittest import mock

        from onboarding_task_management.sub_agent import runtime
        from onboarding_task_management.supabase_store import SupabaseTableClient

        rest = SupabaseRestClient("https://example.supabase.co", "k", transport=healthy())
        store = OnboardingStore(SupabaseTableClient(rest))
        with mock.patch.object(runtime, "store_from_env", return_value=store):
            service = runtime.build_service({"UC02_STORE_MODE": "supabase", "UC02_ENV": "test"})  # explicit environment, no test-data opt-in
        self.assertIsInstance(service.registry, SupabaseConfigRegistry)
        self.assertTrue(service.require_client_calendar)
        self.assertIsInstance(service.calendars, SupabaseCalendars)
        self.assertEqual({t.key for t in service.registry.get("CLIENT_X", on=JOIN).templates}, {"a", "b"})
        with self.assertRaises(MissingConfigurationError):  # the synthetic client does not exist in the database
            service.registry.get("CLIENT-ALPHA", on=JOIN)


class DocumentRequirementAndHashTests(unittest.TestCase):
    def doc(self, key, mandatory=True, **kw):
        row = {"requirement_id": "x", "client_id": "CLIENT_X", "config_version": 1, "service_category": "docs",
               "requirement_key": key, "document_type": f"type_{key}", "description": f"why {key}",
               "is_mandatory": mandatory, "applicable_roles": [], "applicable_grades": [], "applicable_locations": [],
               "applicable_projects": [], "eligibility_rules": {}, "validation_profile": {}, "is_active": True}
        row.update(kw)
        return row

    def test_document_requirements_of_the_approved_version_are_loaded_with_their_filters(self) -> None:
        rows = [self.doc("id"), self.doc("grade_doc", mandatory=False, applicable_grades=["L4"]),
                self.doc("attr_doc", eligibility_rules={"requires_attributes": ["x"], "allow_not_applicable": True}),
                self.doc("old", config_version=2), self.doc("off", is_active=False)]
        config = source(healthy(client_document_requirements=rows)).load("CLIENT_X", JOIN)
        self.assertEqual({d.key for d in config.document_requirements}, {"id", "grade_doc", "attr_doc"})
        included, excluded = config.documents_for(location="L", project="P", role_id="r", grade="L2")
        self.assertEqual({r.key for r, _ in included}, {"id"})
        self.assertTrue(any("grade" in why for _, why in excluded))
        self.assertTrue(next(d for d in config.document_requirements if d.key == "attr_doc").allow_not_applicable)

    def test_a_config_with_no_document_requirements_loads_but_has_none(self) -> None:
        self.assertEqual(source(healthy()).load("CLIENT_X", JOIN).document_requirements, ())

    def test_a_malformed_document_requirement_is_reported(self) -> None:
        bad = self.doc("broken")
        del bad["is_mandatory"]
        with self.assertRaises(MissingConfigurationError):
            source(healthy(client_document_requirements=[bad])).load("CLIENT_X", JOIN)

    def test_hash_verification_is_opt_in_and_a_mismatch_is_a_gap(self) -> None:
        from onboarding_task_management.persistence import config_hash

        db = healthy()
        good = source(db).load("CLIENT_X", JOIN)
        matching = healthy(client_config_versions=[version(config_hash=config_hash(good))])
        checked = SupabaseConfigSource(SupabaseRestClient("https://example.supabase.co", "k", transport=matching),
                                       hash_check=config_hash)
        self.assertEqual(checked.load("CLIENT_X", JOIN).version, "1")
        wrong = SupabaseConfigSource(SupabaseRestClient("https://example.supabase.co", "k", transport=healthy()),
                                     hash_check=config_hash)
        with self.assertRaises(MissingConfigurationError) as ctx:
            wrong.load("CLIENT_X", JOIN)
        self.assertIn("config_hash", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
