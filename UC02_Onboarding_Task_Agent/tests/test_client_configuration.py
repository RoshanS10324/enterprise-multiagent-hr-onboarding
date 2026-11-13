"""Central client configuration: the four synthetic profiles, the JSON Schema, semantic validation, projection onto the existing shared
tables, dry-run, guarded insert-only seeding, read-back verification and rollback planning. Mocked Supabase transport only; the live
dry-run is a separate, read-only command (scripts/uc02_client_config.py dry-run)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path

import jsonschema

from onboarding_task_management.client_configuration import seeding
from onboarding_task_management.client_configuration.document import (
    CONFIG_DIR,
    PROFILE_DIR,
    SECTIONS,
    document_hash,
    load_document,
    load_profiles,
    load_schema,
    validate_document,
)
from onboarding_task_management.client_configuration.projection import INSERT_ORDER, NATURAL_KEYS, ProjectionError, project
from onboarding_task_management.persistence import PersistenceError
from onboarding_task_management.supabase_config import SupabaseConfigSource
from onboarding_task_management.supabase_rest import SupabaseRestClient
from tests.test_supabase_store import FakePostgrest

HOST = "example.supabase.co"
ENV = {"UC02_ENV": "development", seeding.ENV_ALLOW: "1"}
IDS = ["SYN-CLIENT-A", "SYN-CLIENT-B", "SYN-CLIENT-C", "SYN-CLIENT-D"]
ROOT = Path(__file__).resolve().parents[1]


def docs():
    return load_profiles()


def fresh():
    db = FakePostgrest()
    return db, SupabaseRestClient(f"https://{HOST}", "synthetic-key", transport=db)


class ProfileTests(unittest.TestCase):
    def test_the_schema_is_itself_valid_and_all_four_profiles_validate(self) -> None:
        jsonschema.Draft202012Validator.check_schema(load_schema())
        self.assertEqual([d["client_master"]["client_id"] for d in docs()], IDS)
        for d in docs():
            self.assertEqual(validate_document(d), [], d["client_master"]["client_id"])

    def test_consistent_schema_version_unique_ids_effective_dates_and_synthetic_marker(self) -> None:
        ds = docs()
        self.assertEqual({d["schema_version"] for d in ds}, {"1.0.0"})
        self.assertEqual(len({d["client_master"]["client_id"] for d in ds}), 4)
        for d in ds:
            m = d["client_master"]
            self.assertEqual((m["data_origin"], m["configuration_version"], m["effective_from"]), ("synthetic", 1, "2026-01-01"))
            self.assertIn("SYNTHETIC", m["client_name"])
            self.assertTrue(d["awaiting_confirmation"])

    def test_five_use_case_sections_and_shared_configuration_and_nothing_out_of_scope(self) -> None:
        for d in docs():
            self.assertEqual(set(d) - {"schema_version", "client_master", "awaiting_confirmation"}, set(SECTIONS))
        for path in [*PROFILE_DIR.glob("*.json"), CONFIG_DIR / "schema" / "client_configuration.schema.json",
                     *(ROOT / "src" / "onboarding_task_management" / "client_configuration").glob("*.py")]:
            text = path.read_text(encoding="utf-8").lower()
            for banned in ("uc-06", "uc06", "uc_06"):
                self.assertNotIn(banned, text, path.name)

    def test_no_secret_credential_or_address_is_present(self) -> None:
        for d in docs():
            text = json.dumps(d)
            for banned in ("@", "eyJ", "sb_secret", "service_role", "password", "api_key", "BEGIN PRIVATE"):
                self.assertNotIn(banned, text, d["client_master"]["client_id"])

    def test_the_committed_profiles_are_exactly_what_the_builder_generates(self) -> None:
        sys.path.insert(0, str(CONFIG_DIR))
        try:
            import build_profiles
        finally:
            sys.path.pop(0)
        for name, spec in build_profiles.PROFILES.items():
            self.assertEqual(load_document(PROFILE_DIR / name), json.loads(json.dumps(build_profiles.build(spec))), name)

    def test_the_four_clients_have_meaningfully_different_requirements(self) -> None:
        ds = {d["client_master"]["client_id"]: d for d in docs()}
        tasks = {c: {t["template_key"] for t in d["uc02_onboarding_tasks"]["task_templates"]} for c, d in ds.items()}
        documents = {c: {x["document_key"] for x in d["uc03_document_validation"]["documents"]} for c, d in ds.items()}
        self.assertEqual(len({frozenset(v) for v in tasks.values()}), 4)
        self.assertEqual(len({frozenset(v) for v in documents.values()}), 4)
        self.assertNotIn("entry_permit", tasks["SYN-CLIENT-B"])  # India: no visa route
        self.assertNotIn("visa_status_change", tasks["SYN-CLIENT-B"])
        self.assertIn("work_permit_processing", tasks["SYN-CLIENT-D"])  # Saudi Arabia: its own permit route
        self.assertIn("client_site_setup", tasks["SYN-CLIENT-D"])
        self.assertIn("privileged_access_exception", tasks["SYN-CLIENT-C"])  # senior grades only
        self.assertIn("finance_system_access", tasks["SYN-CLIENT-B"])
        self.assertEqual(ds["SYN-CLIENT-C"]["uc02_onboarding_tasks"]["visa_vs_background_check"], "wait_for_clear_background_check_before_visa")
        self.assertEqual(ds["SYN-CLIENT-A"]["uc02_onboarding_tasks"]["visa_vs_background_check"],
                         "run_visa_and_background_check_in_parallel_flag_cost_risk_to_hr")
        self.assertEqual([ds[c]["shared_configuration"]["calendar"]["working_days"] for c in IDS],
                         [[1, 2, 3, 4, 5], [1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5], [7, 1, 2, 3, 4]])
        self.assertEqual({d["uc04_equipment_access"]["handover_rules"]["mode"] for d in ds.values()}, {"COLLECT", "DELIVER", "READY_AT_DESK"})
        self.assertEqual(len({d["uc02_onboarding_tasks"]["hr_approval"]["approver_role"] for d in ds.values()}), 3)
        self.assertEqual(len({frozenset(i["item_code"] for i in d["uc04_equipment_access"]["equipment_catalogue"]) for d in ds.values()}), 4)
        self.assertEqual(len({frozenset(p["policy_key"] for p in d["uc05_orientation_policy"]["policy_acknowledgements"]) for d in ds.values()}), 4)
        self.assertEqual({d["client_master"]["country"]["code"] for d in ds.values()}, {"AE", "IN", "SA"})

    def test_not_every_document_is_mandatory_and_validation_claims_are_honest(self) -> None:
        for d in docs():
            documents = d["uc03_document_validation"]["documents"]
            self.assertTrue(any(not x["mandatory"] for x in documents) or d["client_master"]["client_id"] == "SYN-CLIENT-C")
            caps = d["uc03_document_validation"]["global"]["capabilities"]
            self.assertEqual((caps["antivirus_scanning"], caps["identity_verification"], caps["authority_verification"]), (False, False, False))
            self.assertTrue(all(x["verification"]["authenticity_decided_by"] == "hr" for x in documents))
        categories = {x["category"] for d in docs() for x in d["uc03_document_validation"]["documents"]}
        self.assertTrue({"identity", "education", "employment", "medical", "insurance", "bank_payroll", "family", "agreement", "visa"} <= categories)


class ValidationTests(unittest.TestCase):
    def mutated(self, change):
        d = copy.deepcopy(docs()[0])
        change(d)
        return validate_document(d)

    def test_each_kind_of_bad_document_is_rejected_with_a_reason(self) -> None:
        cases = {
            "missing section": lambda d: d.pop("uc05_orientation_policy"),
            "unknown section": lambda d: d.__setitem__("uc07_other", {}),
            "unknown dependency": lambda d: d["uc02_onboarding_tasks"]["task_templates"][2]["depends_on"].append("nope"),
            "dependency cycle": lambda d: d["uc02_onboarding_tasks"]["task_templates"][0]["depends_on"].append("system_access"),
            "secret key": lambda d: d["shared_configuration"].__setitem__("api_key", "x"),
            "secret value": lambda d: d["client_master"].__setitem__("synthetic_notice", "Bearer abcdefghijklmnop"),
            "out of scope use case": lambda d: d["client_master"].__setitem__("scenario", "includes UC-06 follow-up"),
            "approved without approver": lambda d: d["client_master"].__setitem__("approved_by", None),
            "bad dates": lambda d: d["client_master"].__setitem__("effective_until", "2025-01-01"),
            "unsupported role in matrix": lambda d: d["uc04_equipment_access"]["role_access_matrix"][0].__setitem__("role_id", "astronaut"),
            "item not in catalogue": lambda d: d["uc04_equipment_access"]["role_access_matrix"][0]["items"].append("ghost-item"),
            "undeclared grade": lambda d: d["uc02_onboarding_tasks"]["task_templates"][3]["applicability"]["grades"].append("G99"),
            "missing assignment rule": lambda d: d["uc02_onboarding_tasks"].__setitem__("assignment_rules", d["uc02_onboarding_tasks"]["assignment_rules"][1:]),
            "authenticity decided by machine": lambda d: d["uc03_document_validation"]["documents"][0]["verification"].__setitem__("authenticity_decided_by", "agent"),
            "claims antivirus": lambda d: d["uc03_document_validation"]["global"]["capabilities"].__setitem__("antivirus_scanning", True),
            "mandatory flag disagrees": lambda d: d["uc03_document_validation"]["documents"][0].__setitem__("mandatory", False),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.assertTrue(self.mutated(change), f"{name} was accepted")

    def test_invalid_json_on_disk_is_a_clear_error(self) -> None:
        import tempfile

        from onboarding_task_management.client_configuration.document import ConfigurationDocumentError

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "client_x_config.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ConfigurationDocumentError):
                load_document(bad)

    def test_the_hash_changes_when_anything_changes(self) -> None:
        d = docs()[0]
        e = copy.deepcopy(d)
        e["uc05_orientation_policy"]["reminder_rules"]["repeat_every_working_days"] += 1
        self.assertNotEqual(document_hash(d), document_hash(e))
        self.assertEqual(document_hash(d), document_hash(copy.deepcopy(d)))

    def test_the_validation_command_runs_offline(self) -> None:
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "uc02_client_config.py"), "validate"], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count("VALID"), 4)


class ProjectionTests(unittest.TestCase):
    def test_projection_is_deterministic_complete_and_uses_only_existing_tables(self) -> None:
        for d in docs():
            a, b = project(d), project(copy.deepcopy(d))
            self.assertEqual(a, b)
            self.assertLessEqual(set(a), set(INSERT_ORDER))
            self.assertEqual(len(a["task_templates"]), len(d["uc02_onboarding_tasks"]["task_templates"]))
            self.assertEqual(len(a["client_document_requirements"]), len(d["uc03_document_validation"]["documents"]))
            sections = [r for r in a["client_service_policies"] if r["service_category"] == "cfg_section"]
            self.assertEqual({r["service_key"] for r in sections}, set(SECTIONS))

    def test_the_uc04_role_access_matrix_is_excluded_and_cannot_be_projected_until_uc04_confirms_the_mapping(self) -> None:
        for d in docs():
            self.assertNotIn("role_access_matrix", project(d))
            with self.assertRaises(ProjectionError):
                project(d, include_role_access_matrix=True)
        db, rest = fresh()
        seeding.apply(rest, docs(), confirm_target=HOST, environ=ENV)
        self.assertEqual(db.tables["role_access_matrix"], [])

    def test_natural_keys_are_unique_within_a_document(self) -> None:
        for d in docs():
            for table, rows in project(d).items():
                keys = [tuple(r[c] for c in NATURAL_KEYS[table]) for r in rows]
                self.assertEqual(len(keys), len(set(keys)), table)

    def test_employment_type_is_carried_as_an_attribute_and_unrepresentable_constructs_fail_closed(self) -> None:
        d = copy.deepcopy(docs()[3])  # SYN-CLIENT-D has a contract-only document
        row = next(r for r in project(d)["client_document_requirements"] if r["requirement_key"] == "agency_documents")
        self.assertIn("employment_type:contract", row["eligibility_rules"]["requires_attributes"])
        d["uc02_onboarding_tasks"]["task_templates"][0]["applicability"]["excludes_attributes"] = ["x"]
        with self.assertRaises(ProjectionError):
            project(d)

    def test_the_stored_sections_rebuild_the_document_and_no_secret_enters_a_row(self) -> None:
        d = docs()[2]
        rows = project(d)["client_service_policies"]
        rebuilt = {r["service_key"]: r["policy_details"]["document"] for r in rows if r["service_category"] == "cfg_section"}
        self.assertEqual(rebuilt, {s: d[s] for s in SECTIONS})
        self.assertTrue(all(r["policy_details"]["document_hash"] == document_hash(d) for r in rows if r["service_category"] == "cfg_section"))


class SeedingTests(unittest.TestCase):
    def counts(self, db):
        return {t: len(db.tables[t]) for t in INSERT_ORDER}

    def seeded(self):
        db, rest = fresh()
        seeding.apply(rest, docs(), confirm_target=HOST, environ=ENV)
        return db, rest

    def test_dry_run_only_reads_and_reports_inserts_and_expected_counts(self) -> None:
        db, rest = fresh()
        before = self.counts(db)
        report = seeding.dry_run(rest, docs())
        self.assertEqual(self.counts(db), before)
        self.assertFalse([c for c in db.calls if c[0] in ("POST", "PATCH", "DELETE")])
        self.assertTrue(report.safe_to_apply)
        self.assertEqual((report.summary()["proposed_updates"], report.summary()["proposed_deletes"]), (0, 0))
        self.assertEqual(report.tables["client_configs"].inserts, 4)
        self.assertEqual(report.tables["client_config_versions"].inserts, 4)
        self.assertEqual(report.tables["client_service_policies"].inserts,
                         sum(len(d["uc02_onboarding_tasks"]["assignment_rules"]) + 6 for d in docs()))
        self.assertEqual(report.total_inserts, sum(len(rows) for d in docs() for rows in project(d).values()))
        self.assertEqual(set(report.coverage), set(IDS))
        self.assertEqual(len(report.rollback), report.total_inserts)

    def test_seeding_needs_the_development_opt_in_and_the_target_host(self) -> None:
        db, rest = fresh()
        for env in ({}, {"UC02_ENV": "development"}, {"UC02_ENV": "production", seeding.ENV_ALLOW: "1"}):
            with self.assertRaises(PersistenceError):
                seeding.apply(rest, docs(), confirm_target=HOST, environ=env)
        with self.assertRaises(PersistenceError):
            seeding.apply(rest, docs(), confirm_target="other.host", environ=ENV)
        self.assertEqual(sum(self.counts(db).values()), 0)

    def test_apply_inserts_everything_and_read_back_confirms_it(self) -> None:
        db, rest = fresh()
        result = seeding.apply(rest, docs(), confirm_target=HOST, environ=ENV)
        self.assertEqual(result["client_configs"]["inserted"], 4)
        self.assertEqual(seeding.verify(rest, docs()), [])
        self.assertEqual({r["client_id"] for r in db.tables["client_configs"]}, set(IDS))
        for t in ("task_templates", "client_document_requirements", "client_service_policies"):
            self.assertEqual({r["client_id"] for r in db.tables[t]}, set(IDS), t)

    def test_seeding_is_idempotent_and_never_updates_or_deletes(self) -> None:
        db, rest = self.seeded()
        before = self.counts(db)
        calls = len(db.calls)
        again = seeding.apply(rest, docs(), confirm_target=HOST, environ=ENV)
        self.assertEqual(self.counts(db), before)
        self.assertTrue(all(v["inserted"] == 0 for v in again.values()))
        self.assertFalse([c for c in db.calls[calls:] if c[0] in ("POST", "PATCH", "DELETE")])
        self.assertFalse([c for c in db.calls if c[0] in ("PATCH", "DELETE")])

    def test_an_existing_different_row_is_a_conflict_that_blocks_every_write_and_is_never_overwritten(self) -> None:
        db, rest = self.seeded()
        db.tables["client_config_versions"][0]["approved_by"] = "someone-else"
        report = seeding.dry_run(rest, docs())
        self.assertFalse(report.safe_to_apply)
        self.assertTrue(any("client_config_versions" in c and "approved_by" in c for c in report.conflicts))
        with self.assertRaises(PersistenceError):
            seeding.apply(rest, docs(), confirm_target=HOST, environ=ENV)
        self.assertEqual(db.tables["client_config_versions"][0]["approved_by"], "someone-else")

    def test_existing_unrelated_clients_are_left_alone(self) -> None:
        db, rest = fresh()
        db.tables["client_configs"].append({"client_id": "CLIENT_DUBAI", "company_name": "x", "config_hash": "h", "working_days": [1],
                                            "timezone": "Asia/Dubai", "settings_payload": {}, "is_active": True})
        db.tables["client_config_versions"].append({"client_id": "CLIENT_DUBAI", "version": 1, "status": "draft", "effective_from": "2026-11-02",
                                                    "config_hash": "h"})
        snapshot = copy.deepcopy(db.tables)
        seeding.apply(rest, docs(), confirm_target=HOST, environ=ENV)
        self.assertEqual(db.tables["client_configs"][0], snapshot["client_configs"][0])
        self.assertEqual(db.tables["client_config_versions"][0], snapshot["client_config_versions"][0])
        self.assertEqual(len(db.tables["client_configs"]), 5)

    def test_verify_detects_tampering_and_missing_rows(self) -> None:
        db, rest = self.seeded()
        row = next(r for r in db.tables["client_service_policies"] if r["service_category"] == "cfg_section" and r["service_key"] == "uc03_document_validation")
        row["policy_details"]["document"]["global"]["confidence_threshold"] = 0.1
        db.tables["task_templates"].pop()
        problems = seeding.verify(rest, docs())
        self.assertTrue(any("task_templates" in p for p in problems))
        self.assertTrue(any("rebuild the document" in p for p in problems))

    def test_rollback_plan_deletes_children_before_parents_by_natural_key(self) -> None:
        plan = seeding.rollback_plan(docs())
        tables = [t for t, _ in plan]
        self.assertEqual(tables[-1], "client_configs")
        self.assertLess(tables.index("task_templates"), tables.index("client_config_versions"))
        self.assertLess(tables.index("client_config_versions"), tables.index("client_configs"))
        for table, key in plan:
            self.assertEqual(set(key), set(NATURAL_KEYS[table]))

    def test_the_four_clients_stay_isolated_in_the_database(self) -> None:
        db, rest = self.seeded()
        for cid in IDS:
            own = rest.select("task_templates", {"client_id": cid})
            self.assertTrue(own and {r["client_id"] for r in own} == {cid})
        keysets = {cid: {r["template_key"] for r in rest.select("task_templates", {"client_id": cid})} for cid in IDS}
        self.assertEqual(len({frozenset(v) for v in keysets.values()}), 4)


class ExistingReaderConsumesTheProjectionTests(unittest.TestCase):
    """UC-02's existing configuration reader works on the seeded rows unchanged: nothing is hardcoded per client."""

    def test_uc02_reads_each_seeded_client_and_gets_a_different_configuration(self) -> None:
        db, rest = fresh()
        seeding.apply(rest, docs(), confirm_target=HOST, environ=ENV)
        source = SupabaseConfigSource(rest, allow_synthetic=True, environ={"UC02_ENV": "test", "UC02_ALLOW_SYNTHETIC_CONFIG": "1"})
        loaded = {cid: source.load(cid, date(2026, 12, 1)) for cid in IDS}
        for cid, config in loaded.items():
            doc = next(d for d in docs() if d["client_master"]["client_id"] == cid)
            self.assertEqual(config.client_id, cid)
            self.assertEqual(config.version, "1")
            self.assertEqual(config.data_origin, "synthetic")
            self.assertEqual({t.key for t in config.templates}, {t["template_key"] for t in doc["uc02_onboarding_tasks"]["task_templates"]})
            self.assertEqual({d.key for d in config.document_requirements}, {d["document_key"] for d in doc["uc03_document_validation"]["documents"]})
            self.assertEqual(len(config.assignments), len(doc["uc02_onboarding_tasks"]["assignment_rules"]))
        self.assertEqual(len({frozenset(t.key for t in c.templates) for c in loaded.values()}), 4)

    def test_synthetic_clients_are_refused_unless_the_runtime_opts_in(self) -> None:
        db, rest = fresh()
        seeding.apply(rest, docs(), confirm_target=HOST, environ=ENV)
        from onboarding_task_management.errors import MissingConfigurationError

        with self.assertRaises(MissingConfigurationError):
            SupabaseConfigSource(rest).load("SYN-CLIENT-A", date(2026, 12, 1))


if __name__ == "__main__":
    unittest.main()
