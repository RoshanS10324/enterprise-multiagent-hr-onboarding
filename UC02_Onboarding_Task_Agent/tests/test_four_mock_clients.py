"""The four EXISTING mock clients (CLIENT_CANADA, CLIENT_DUBAI, CLIENT_INDIA, CLIENT_USA) with their synthetic configurations: retrieval, eligibility,
attribute matching, dynamic checklist, documents, calendars, isolation, persistence and seeding.

LOCAL MOCK tests. The schema-enforcing mock database starts from a READ-ONLY snapshot of the live configuration rows
(``fixtures/existing_clients_config_snapshot.json``), then the four profiles are seeded INTO THE MOCK through the guarded, insert-only seeder. Nothing
touches Supabase. The live database has NOT been seeded; see docs/FOUR_MOCK_CLIENTS_IMPLEMENTATION_REPORT.md.
"""

from __future__ import annotations

import copy
import json
import unittest
from datetime import date
from pathlib import Path

from onboarding_task_management.client_configuration import seeding
from onboarding_task_management.client_configuration.document import PROFILE_DIR, load_profiles, validate_document
from onboarding_task_management.client_configuration.projection import project
from onboarding_task_management.client_configuration.retrieval import EmployeeProfile, retrieve
from onboarding_task_management.contracts import Actor, Role
from onboarding_task_management.errors import DuplicateOnboardingError, EmployeeNotEligibleError, MissingConfigurationError
from onboarding_task_management.persistence import OnboardingStore
from onboarding_task_management.sub_agent.service import OnboardingTrigger, OnboardingService
from onboarding_task_management.supabase_config import SupabaseCalendars, SupabaseConfigRegistry, SupabaseConfigSource
from onboarding_task_management.supabase_rest import SupabaseRestClient
from onboarding_task_management.supabase_store import SupabaseTableClient
from onboarding_task_management.supabase_synthetic import FixtureSpec, build_rows
from onboarding_task_management.synthetic import accepted_offer, approved_form
from tests.test_supabase_store import FakePostgrest

HOST = "example.supabase.co"
ENV = {"UC02_ENV": "development", seeding.ENV_ALLOW: "1"}
SNAP = json.loads((Path(__file__).parent / "fixtures" / "existing_clients_config_snapshot.json").read_text(encoding="utf-8"))
TABLES = ["client_configs", "client_config_versions", "task_templates", "client_document_requirements", "client_service_policies",
          "client_milestone_templates", "client_holidays"]
CLIENTS = ["CLIENT_CANADA", "CLIENT_DUBAI", "CLIENT_INDIA", "CLIENT_USA"]
JOIN = date(2027, 1, 18)
#: An EXPLICITLY enabled test environment: synthetic / mock configuration is denied everywhere else (config_safety).
TEST_ENV = {"UC02_ENV": "test", "UC02_ALLOW_SYNTHETIC_CONFIG": "1"}

#: One representative synthetic employee per client (IDs and codes only).
EMPLOYEES = {
    "CLIENT_CANADA": EmployeeProfile("CLIENT_CANADA", JOIN, "CA", "Canada", "IN", "software_engineer", "L3", "full_time",
                                     ("work_authorization:work_permit_holder", "work_province:ON")),
    "CLIENT_DUBAI": EmployeeProfile("CLIENT_DUBAI", JOIN, "AE", "Dubai", "IN", "software_engineer", "G5", "full_time",
                                    ("visa_route:overseas_entry", "needs_travel", "needs_accommodation")),
    "CLIENT_INDIA": EmployeeProfile("CLIENT_INDIA", JOIN, "IN", "India", "IN", "finance_analyst", "G4", "full_time"),
    "CLIENT_USA": EmployeeProfile("CLIENT_USA", JOIN, "US", "United States", "US", "sales_associate", "B3", "full_time",
                                  ("work_authorization:citizen", "work_state:NY")),
}
DOCS = {d["client_master"]["client_id"]: d for d in load_profiles(PROFILE_DIR / "existing_mock_clients")}


def snapshot_db():
    db = FakePostgrest()
    for t in TABLES:
        db.tables[t] = copy.deepcopy(SNAP[t])
    return db, SupabaseRestClient(f"https://{HOST}", "k", transport=db)


def seeded():
    db, rest = snapshot_db()
    seeding.apply(rest, list(DOCS.values()), confirm_target=HOST, environ=ENV)
    return db, rest


def service(rest, store=None):
    src = SupabaseConfigSource(rest, allow_synthetic=True, environ=TEST_ENV)
    return OnboardingService(SupabaseConfigRegistry(source=src), calendars=SupabaseCalendars(src), require_client_calendar=True, store=store)


def preview(rest, profile, **over):
    resp = retrieve(rest, profile, allow_synthetic=True, environ=TEST_ENV)
    ctx = resp.checklist_context(profile)
    if over:
        ctx = ctx.model_copy(update=over)
    return resp, service(rest).preview_checklist(Actor(actor_id="hr", role=Role.HR_OPS, client_id=profile.client_id), ctx)


def keys(entries):
    return {e["key"] for e in entries}


class ProfileTests(unittest.TestCase):
    def test_four_profiles_validate_and_are_marked_mock_synthetic_existing(self) -> None:
        self.assertEqual(sorted(DOCS), CLIENTS)
        for c, d in DOCS.items():
            self.assertEqual(validate_document(d), [], c)
            m = d["client_master"]
            self.assertEqual((m["environment"], m["synthetic"], m["existing_client_record"], m["data_origin"], m["configuration_version"]),
                             ("mock", True, True, "synthetic", 2))
            self.assertTrue(d["awaiting_confirmation"])
            self.assertIn("not a client approval", m["approved_by"])

    def test_stored_master_values_are_preserved(self) -> None:
        for c, d in DOCS.items():
            stored = next(r for r in SNAP["client_configs"] if r["client_id"] == c)
            self.assertEqual((d["client_master"]["timezone"], d["shared_configuration"]["calendar"]["working_days"], d["client_master"]["client_name"]),
                             (stored["timezone"], stored["working_days"], stored["company_name"]))

    def test_the_four_are_meaningfully_different(self) -> None:
        tasks = {c: {t["template_key"] for t in d["uc02_onboarding_tasks"]["task_templates"]} for c, d in DOCS.items()}
        docs = {c: {x["document_key"] for x in d["uc03_document_validation"]["documents"]} for c, d in DOCS.items()}
        self.assertEqual(len({frozenset(v) for v in tasks.values()}), 4)
        self.assertEqual(len({frozenset(v) for v in docs.values()}), 4)
        self.assertEqual({d["uc02_onboarding_tasks"]["payroll_configuration"]["currency"] for d in DOCS.values()}, {"CAD", "AED", "INR", "USD"})
        self.assertEqual(DOCS["CLIENT_INDIA"]["uc02_onboarding_tasks"]["immigration_configuration"]["routes"], {})
        self.assertFalse({t for t in tasks["CLIENT_INDIA"] if "visa" in t or "permit" in t or "immigration" in t})

    def test_no_invented_legal_values_secrets_or_out_of_scope_use_case(self) -> None:
        for c, d in DOCS.items():
            text = json.dumps(d).lower()
            for banned in ("uc-06", "uc06", "password", "api_key", "@", "%", "i-9", "w-4", "td1"):
                self.assertNotIn(banned, text, f"{c}: {banned}")
            self.assertIn("awaiting_confirmation", text)


class SeedingTests(unittest.TestCase):
    def test_dry_run_preserves_existing_rows_and_adds_version_2_only(self) -> None:
        db, rest = snapshot_db()
        report = seeding.dry_run(rest, list(DOCS.values()))
        self.assertTrue(report.safe_to_apply, report.summary())
        self.assertEqual(report.tables["client_configs"].inserts, 0)
        self.assertEqual(report.tables["client_configs"].noops, 4)
        self.assertEqual(report.tables["client_config_versions"].inserts, 4)
        self.assertEqual(report.tables["client_holidays"].noops, 2)  # CLIENT_DUBAI's two stored [SAMPLE] holidays
        self.assertEqual(report.summary()["proposed_updates"] + report.summary()["proposed_deletes"], 0)

    def test_apply_never_changes_an_existing_row_and_is_idempotent(self) -> None:
        db, rest = snapshot_db()
        before = {t: copy.deepcopy([r for r in db.tables[t] if r["client_id"] in CLIENTS]) for t in TABLES}
        seeding.apply(rest, list(DOCS.values()), confirm_target=HOST, environ=ENV)
        for t in TABLES:
            for row in before[t]:
                self.assertIn(row, db.tables[t], f"{t} existing row changed")
        self.assertEqual(seeding.verify(rest, list(DOCS.values())), [])
        counts = {t: len(db.tables[t]) for t in TABLES}
        seeding.apply(rest, list(DOCS.values()), confirm_target=HOST, environ=ENV)
        self.assertEqual({t: len(db.tables[t]) for t in TABLES}, counts)
        self.assertFalse([c for c in db.calls if c[0] in ("PATCH", "DELETE")])

    def test_a_changed_stored_master_is_a_conflict_not_an_overwrite(self) -> None:
        db, rest = snapshot_db()
        next(r for r in db.tables["client_configs"] if r["client_id"] == "CLIENT_INDIA")["working_days"] = [1, 2, 3, 4, 5]
        self.assertFalse(seeding.dry_run(rest, list(DOCS.values())).safe_to_apply)


class RetrievalTests(unittest.TestCase):
    def test_each_client_retrieves_its_own_version_2_configuration(self) -> None:
        db, rest = seeded()
        for c, p in EMPLOYEES.items():
            r = retrieve(rest, p, allow_synthetic=True, environ=TEST_ENV)
            self.assertEqual((r.client_id, r.configuration_version, r.data_origin), (c, 2, "synthetic"))
            self.assertEqual(r.review_required, [])
            own = {t["template_key"] for t in DOCS[c]["uc02_onboarding_tasks"]["task_templates"]}
            self.assertTrue(keys(r.applicable["tasks"]) <= own)
            self.assertTrue(r.applicable["equipment"] and r.applicable["access"] and r.applicable["orientation"] and r.applicable["policies"])
            self.assertTrue(r.applicable["payroll_tasks"])
            self.assertEqual(r.calendar["working_days"], DOCS[c]["shared_configuration"]["calendar"]["working_days"])

    def test_refused_without_the_synthetic_opt_in_and_for_missing_or_invalid_clients(self) -> None:
        db, rest = seeded()
        with self.assertRaisesRegex(MissingConfigurationError, "marked mock"):
            retrieve(rest, EMPLOYEES["CLIENT_DUBAI"])
        for cid in ("CLIENT_UNKNOWN", "", "SYN-UAE-TEST-X"):
            with self.assertRaises(MissingConfigurationError):
                retrieve(rest, EmployeeProfile(cid, JOIN, "AE", "Dubai", "IN", "software_engineer", "G5", "full_time"), allow_synthetic=True, environ=TEST_ENV)

    def test_unsupported_values_are_rejected_naming_the_dimension(self) -> None:
        db, rest = seeded()
        base = EMPLOYEES["CLIENT_DUBAI"]
        cases = {"role": dict(role="astronaut"), "work location": dict(work_location="Atlantis"), "grade": dict(grade="G99"),
                 "nationality": dict(nationality="ZZ"), "employment type": dict(employment_type="intern"), "work country": dict(work_country="IN")}
        for label, over in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(EmployeeNotEligibleError, label):
                    retrieve(rest, EmployeeProfile(**{**base.__dict__, **over}), allow_synthetic=True, environ=TEST_ENV)
        with self.assertRaisesRegex(EmployeeNotEligibleError, "nationality"):
            retrieve(rest, EmployeeProfile(**{**EMPLOYEES["CLIENT_INDIA"].__dict__, "nationality": "US"}), allow_synthetic=True, environ=TEST_ENV)

    def test_a_missing_required_attribute_is_an_hr_review_item(self) -> None:
        db, rest = seeded()
        for c, fam in (("CLIENT_DUBAI", "visa_route"), ("CLIENT_CANADA", "work_authorization"), ("CLIENT_USA", "work_state")):
            p = EMPLOYEES[c]
            r = retrieve(rest, EmployeeProfile(**{**p.__dict__, "attributes": tuple(a for a in p.attributes if not a.startswith(fam + ":"))}),
                         allow_synthetic=True, environ=TEST_ENV)
            self.assertTrue(any(x.startswith(fam) for x in r.review_required), (c, r.review_required))


class ChecklistTests(unittest.TestCase):
    def test_retrieval_and_the_uc02_checklist_agree_task_for_task(self) -> None:
        db, rest = seeded()
        for c, p in EMPLOYEES.items():
            resp, pv = preview(rest, p)
            self.assertEqual({d.template_key for d in pv.included}, keys(resp.applicable["tasks"]), c)
            self.assertFalse([i for i in pv.issues if i.severity.value in ("review_required", "blocking")], (c, pv.issues))

    def test_dubai_overseas_engineer_gets_the_entry_route_and_not_the_transfer_route(self) -> None:
        db, rest = seeded()
        resp, pv = preview(rest, EMPLOYEES["CLIENT_DUBAI"])
        inc = {d.template_key for d in pv.included}
        self.assertTrue({"entry_permit", "medical_fitness_test_on_arrival", "emirates_id_biometrics_on_arrival", "flight_booking",
                         "temporary_accommodation", "health_insurance_enrolment"} <= inc)
        self.assertFalse({"visa_status_change", "medical_fitness_test", "emirates_id_biometrics", "privileged_access_request"} & inc)
        self.assertIn("medical_fitness_certificate", keys(resp.applicable["documents"]))
        self.assertIn("passport", keys(resp.applicable["documents"]))

    def test_dubai_uae_national_needs_no_visa_route(self) -> None:
        db, rest = seeded()
        p = EmployeeProfile("CLIENT_DUBAI", JOIN, "AE", "Dubai", "AE", "operations_associate", "G4", "full_time", ("visa_route:not_required",))
        resp, pv = preview(rest, p)
        self.assertEqual(resp.nationality_group, "NG-UAE-NATIONAL-SYN")
        self.assertFalse({d.template_key for d in pv.included if d.category.value in ("visa", "medical", "biometrics")})
        self.assertIn("national_id_uae", keys(resp.applicable["documents"]))
        self.assertNotIn("passport", keys(resp.applicable["documents"]))

    def test_india_domestic_employee_never_gets_a_visa_task(self) -> None:
        db, rest = seeded()
        for role, grade in (("finance_analyst", "G4"), ("software_engineer", "G2"), ("operations_associate", "G6")):
            resp, pv = preview(rest, EmployeeProfile("CLIENT_INDIA", JOIN, "IN", "India", "IN", role, grade, "full_time"))
            self.assertFalse({d.template_key for d in pv.included if d.category.value in ("visa", "biometrics", "medical")}, role)
        resp, pv = preview(rest, EMPLOYEES["CLIENT_INDIA"])
        inc = {d.template_key for d in pv.included}
        self.assertTrue({"tax_identifier_verification", "statutory_declarations", "bank_account_verification", "payroll_setup",
                         "finance_application_access", "finance_controls_training", "previous_employment_verification"} <= inc)

    def test_canada_and_usa_authorisation_routes_follow_the_stated_status(self) -> None:
        db, rest = seeded()
        _, ca = preview(rest, EMPLOYEES["CLIENT_CANADA"])
        self.assertIn("work_permit_verification", {d.template_key for d in ca.included})
        self.assertNotIn("work_permit_application_support", {d.template_key for d in ca.included})
        _, citizen = preview(rest, EMPLOYEES["CLIENT_USA"])
        us = {d.template_key for d in citizen.included}
        self.assertIn("employment_eligibility_verification", us)  # for every US employee
        self.assertFalse({"work_visa_status_verification", "visa_sponsorship_case"} & us)
        p = EmployeeProfile(**{**EMPLOYEES["CLIENT_USA"].__dict__, "attributes": ("work_authorization:sponsorship_required", "work_state:NY")})
        _, sponsor = preview(rest, p)
        self.assertIn("visa_sponsorship_case", {d.template_key for d in sponsor.included})

    def test_role_grade_and_employment_type_change_tasks_and_access(self) -> None:
        db, rest = seeded()
        dubai = EMPLOYEES["CLIENT_DUBAI"]
        senior = EmployeeProfile(**{**dubai.__dict__, "role": "senior_manager", "grade": "G7", "attributes": ("visa_route:in_country_transfer",)})
        resp, pv = preview(rest, senior)
        self.assertIn("privileged_access_request", {d.template_key for d in pv.included})
        self.assertIn("privileged-admin-access", keys(resp.applicable["access"]))
        self.assertNotIn("source-control-access", keys(resp.applicable["access"]))
        part_time = EmployeeProfile(**{**EMPLOYEES["CLIENT_USA"].__dict__, "employment_type": "part_time"})
        _, pv = preview(rest, part_time)
        self.assertNotIn("benefits_enrolment", {d.template_key for d in pv.included})

    def test_a_missing_required_attribute_flags_the_checklist_for_hr_review(self) -> None:
        db, rest = seeded()
        p = EmployeeProfile(**{**EMPLOYEES["CLIENT_DUBAI"].__dict__, "attributes": ()})
        _, pv = preview(rest, p)
        self.assertTrue(pv.review_required)
        issue = next(i for i in pv.issues if i.code == "attribute_unstated" and i.subject == "visa_route")
        self.assertIn("entry_permit", issue.detail)

    def test_unsupported_role_is_rejected_by_the_uc02_checklist_itself(self) -> None:
        db, rest = seeded()
        resp = retrieve(rest, EMPLOYEES["CLIENT_DUBAI"], allow_synthetic=True, environ=TEST_ENV)
        ctx = resp.checklist_context(EMPLOYEES["CLIENT_DUBAI"]).model_copy(update={"role_id": "astronaut"})
        with self.assertRaises(EmployeeNotEligibleError):
            service(rest).preview_checklist(Actor(actor_id="hr", role=Role.HR_OPS, client_id="CLIENT_DUBAI"), ctx)
        ctx = ctx.model_copy(update={"role_id": "software_engineer", "location": "Abu Dhabi"})
        with self.assertRaises(EmployeeNotEligibleError):
            service(rest).preview_checklist(Actor(actor_id="hr", role=Role.HR_OPS, client_id="CLIENT_DUBAI"), ctx)

    def test_calendars_drive_due_dates(self) -> None:
        db, rest = seeded()
        _, india = preview(rest, EMPLOYEES["CLIENT_INDIA"])
        _, canada = preview(rest, EMPLOYEES["CLIENT_CANADA"])
        # hr_record_create is 10 working days before 2027-01-18 in both; India counts Saturdays as working days, Canada does not
        self.assertEqual(india.due_dates["hr_record_create"], date(2027, 1, 6))
        self.assertEqual(canada.due_dates["hr_record_create"], date(2027, 1, 4))
        for c, p in EMPLOYEES.items():
            _, pv = preview(rest, p)
            holidays = {date.fromisoformat(h["date"]) for h in DOCS[c]["shared_configuration"]["calendar"]["holidays"]}
            self.assertFalse(holidays & set(pv.due_dates.values()), c)

    def test_dependencies_form_layers_and_every_task_has_a_reason(self) -> None:
        db, rest = seeded()
        for c, p in EMPLOYEES.items():
            _, pv = preview(rest, p)
            self.assertEqual(pv.parallel_batches[0], ["hr_record_create"], c)
            self.assertTrue(any(len(b) > 1 for b in pv.parallel_batches), c)
            self.assertTrue(all(d.reason_code for d in pv.included + pv.excluded))


class IsolationTests(unittest.TestCase):
    def test_no_client_ever_receives_another_clients_tasks_documents_or_holidays(self) -> None:
        db, rest = seeded()
        for c, p in EMPLOYEES.items():
            resp, pv = preview(rest, p)
            own_tasks = {t["template_key"] for t in DOCS[c]["uc02_onboarding_tasks"]["task_templates"]}
            own_docs = {x["document_key"] for x in DOCS[c]["uc03_document_validation"]["documents"]}
            self.assertTrue({d.template_key for d in pv.included} <= own_tasks)
            self.assertTrue(keys(resp.applicable["documents"]) <= own_docs)
            cal = SupabaseConfigSource(rest, allow_synthetic=True, environ=TEST_ENV).calendar(c)
            self.assertEqual({str(d) for d in cal}, {h["date"] for h in DOCS[c]["shared_configuration"]["calendar"]["holidays"]}, c)
        dubai_only = {t["template_key"] for t in DOCS["CLIENT_DUBAI"]["uc02_onboarding_tasks"]["task_templates"]} - \
            {t["template_key"] for t in DOCS["CLIENT_INDIA"]["uc02_onboarding_tasks"]["task_templates"]}
        _, india = preview(rest, EMPLOYEES["CLIENT_INDIA"])
        self.assertFalse(dubai_only & {d.template_key for d in india.included})

    def test_a_client_cannot_preview_another_clients_checklist(self) -> None:
        db, rest = seeded()
        resp = retrieve(rest, EMPLOYEES["CLIENT_CANADA"], allow_synthetic=True, environ=TEST_ENV)
        from onboarding_task_management.errors import DataIsolationError

        with self.assertRaises(DataIsolationError):
            service(rest).preview_checklist(Actor(actor_id="hr", role=Role.HR_OPS, client_id="CLIENT_USA"),
                                            resp.checklist_context(EMPLOYEES["CLIENT_CANADA"]))

    def test_version_1_draft_rows_are_never_mixed_in(self) -> None:
        db, rest = seeded()
        for c in CLIENTS:
            cfg = SupabaseConfigSource(rest, allow_synthetic=True, environ=TEST_ENV).load(c, JOIN)
            self.assertEqual(cfg.version, "2")


class PersistenceTests(unittest.TestCase):
    """The generated plan through the Supabase adapter on the mock: task-level config_version, traceability, idempotency."""

    def case_rows(self, db):
        template = build_rows(FixtureSpec())["onboarding_cases"][0]
        for n, (c, p) in enumerate(EMPLOYEES.items(), 1):
            row = dict(template, case_id=f"00000000-0000-4000-8000-00000000000{n}", client_id=c, employee_no=f"MOCK-EMP-{n:04d}",
                       joining_date=JOIN.isoformat(), location_name=p.work_location, role_id=p.role, grade=p.grade)
            SupabaseRestClient(f"https://{HOST}", "k", transport=db).insert("onboarding_cases", [row])  # into the MOCK, with its defaults
            yield row["case_id"], c, p

    def test_each_client_plan_persists_with_config_version_and_source_template_and_is_not_duplicated(self) -> None:
        db, rest = seeded()
        store = OnboardingStore(SupabaseTableClient(rest))
        svc = service(rest, store)
        for case_id, c, p in list(self.case_rows(db)):
            resp = retrieve(rest, p, allow_synthetic=True, environ=TEST_ENV)
            ctx = resp.checklist_context(p)
            emp = f"MOCK-{c}"
            trigger = OnboardingTrigger(client_id=c, employee_id=emp, joining_date=JOIN, role_id=p.role, department="Synthetic", manager_id="MGR-SYN",
                                        location=p.work_location, project="", grade=p.grade, nationality_group=ctx.nationality_group,
                                        attributes=tuple(ctx.attributes), case_id=case_id, offer=accepted_offer(emp), form=approved_form(emp))
            actor = Actor(actor_id="hr", role=Role.HR_OPS, client_id=c)
            svc.create_onboarding(actor, trigger)
            rows = rest.select("onboarding_tasks", {"case_id": case_id})
            own = {t["template_key"] for t in DOCS[c]["uc02_onboarding_tasks"]["task_templates"]}
            self.assertEqual({r["template_key"] for r in rows}, keys(resp.applicable["tasks"]), c)
            self.assertTrue(all(r["config_version"] == 2 and r["client_id"] == c and r["template_key"] in own for r in rows), c)
            with self.assertRaises(DuplicateOnboardingError):
                svc.create_onboarding(actor, trigger)
            self.assertEqual(len(rest.select("onboarding_tasks", {"case_id": case_id})), len(rows))

    def test_an_ineligible_employee_creates_nothing(self) -> None:
        db, rest = seeded()
        store = OnboardingStore(SupabaseTableClient(rest))
        case_id, c, p = next(iter(self.case_rows(db)))
        emp = "MOCK-X"
        trigger = OnboardingTrigger(client_id=c, employee_id=emp, joining_date=JOIN, role_id="astronaut", department="Synthetic", manager_id="M",
                                    location=p.work_location, project="", grade=p.grade, nationality_group="NG-INTL-SYN", case_id=case_id,
                                    attributes=("employment_type:full_time", "work_authorization:citizen", "work_province:ON"),
                                    offer=accepted_offer(emp), form=approved_form(emp))
        with self.assertRaises(EmployeeNotEligibleError):
            service(rest, store).create_onboarding(Actor(actor_id="hr", role=Role.HR_OPS, client_id=c), trigger)
        self.assertEqual(rest.select("onboarding_tasks", {"case_id": case_id}), [])


if __name__ == "__main__":
    unittest.main()
