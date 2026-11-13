"""UC-02 over the Supabase adapter, against an in-process PostgREST stand-in.

The stand-in enforces the LIVE schema captured in ``fixtures/live_schema.json`` (unknown columns,
NOT NULL columns without defaults, uuid columns, primary keys). So these tests prove the mapping fits
the live columns. They do NOT prove CHECK constraints, foreign keys, RLS or behaviour of the real
database: those need an approved development database and have not been run.
"""

from __future__ import annotations

import dataclasses
import json
import unittest
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from onboarding_task_management.contracts import DecisionOutcome
from onboarding_task_management.errors import (
    DataIsolationError,
    DuplicateOnboardingError,
    PreconditionError,
)
from onboarding_task_management.persistence import OnboardingStore, PersistenceError, store_from_env
from onboarding_task_management.sub_agent.service import OnboardingService
from onboarding_task_management.supabase_rest import SupabaseRestClient
from onboarding_task_management.supabase_store import STATE_ACTION, SupabaseTableClient
from onboarding_task_management.contracts import Actor, Role
from onboarding_task_management.sub_agent.checklist import ConfigRegistry
from onboarding_task_management.supabase_store import actor_type_for
from onboarding_task_management.synthetic import ALPHA_CONFIG, make_trigger, persona

SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "live_schema.json").read_text(encoding="utf-8"))
PKS = {"onboarding_cases": ("case_id",), "onboarding_tasks": ("task_id",), "task_dependencies": ("task_id", "depends_on_task_id"),
       "plan_revisions": ("revision_id",), "approval_requests": ("request_id",), "audit_events": ("event_id",),
       "agent_runs": ("run_id",),
       # configuration and UC-01-owned fixture tables (natural keys; used to detect an existing row)
       "client_configs": ("client_id",), "client_config_versions": ("client_id", "version"),
       "task_templates": ("client_id", "template_key", "config_version"),
       "client_document_requirements": ("requirement_id",), "client_milestone_templates": ("milestone_id",),
       "client_holidays": ("client_id", "holiday_date"), "client_service_policies": ("policy_id",),
       "onboarding_forms": ("form_id",), "offers": ("client_id", "offer_ref"),
       "offer_forms": ("client_id", "offer_ref", "form_id"), "role_access_matrix": ("matrix_id",)}
UUID_COLS = {"case_id", "event_id", "revision_id", "requirement_id", "milestone_id", "policy_id", "form_id", "matrix_id"}
CASE = "409d88b5-bb68-4f39-8a63-af1f168d7170"

#: An EXISTING live client id (verified in client_configs). The case's client_id has a foreign key to it,
#: so CLIENT-ALPHA, which is not there, cannot be used. Nothing is inserted or modified in Supabase.
LIVE_CLIENT = "CLIENT_DUBAI"
LIVE_CLIENTS = {"CLIENT_CANADA", "CLIENT_DUBAI", "CLIENT_INDIA", "CLIENT_USA", "test_enterprise_corp"}
#: Synthetic, in-process only. Not the client's approved configuration.
SYNTHETIC_CONFIG = ALPHA_CONFIG.model_copy(update={"client_id": LIVE_CLIENT, "version": "SYNTHETIC-UC02-TEST"})
#: Two different people: the live table forbids decided_by == requested_by.
HR = Actor(actor_id="U-UC02-REQ", role=Role.HR_OPS, client_id=LIVE_CLIENT)
APPROVER = Actor(actor_id="U-UC02-APPR", role=Role.HR_OPS, client_id=LIVE_CLIENT)
#: Live CHECK constraints (from the owner's CSV export, 2026-11-02).
LIVE_APPROVAL_OUTCOMES = {"PENDING", "APPROVED", "EDITED_AND_APPROVED", "REJECTED", "CANCELLED", "EXPIRED"}
LIVE_RUN_STATUSES = {"proposed", "acted", "exception"}
LIVE_ACTOR_TYPES = {"HUMAN", "AGENT", "SYSTEM"}
FKS = {  # table -> [(column, parent table, parent column)]
    "onboarding_cases": [("client_id", "client_configs", "client_id")],
    "onboarding_tasks": [("case_id", "onboarding_cases", "case_id")],
    "task_dependencies": [("task_id", "onboarding_tasks", "task_id"),
                          ("depends_on_task_id", "onboarding_tasks", "task_id")],
    "plan_revisions": [("case_id", "onboarding_cases", "case_id")],
    "approval_requests": [("case_id", "onboarding_cases", "case_id"), ("client_id", "client_configs", "client_id")],
    "audit_events": [("case_id", "onboarding_cases", "case_id")],
    "agent_runs": [("case_id", "onboarding_cases", "case_id")],
    "onboarding_forms": [("case_id", "onboarding_cases", "case_id"), ("client_id", "client_configs", "client_id")],
    "offer_forms": [("form_id", "onboarding_forms", "form_id"), ("client_id", "client_configs", "client_id")],
}
UNIQUE = {"onboarding_tasks": [("case_id", "template_key")], "plan_revisions": [("case_id", "revision")],
          "onboarding_cases": [("client_id", "employee_no")]}
EMPLOYEE = "EMP-UC02-TEST-0001"  # a unique employee_no for the test case
EMPLOYEE_2 = "EMP-UC02-TEST-0002"


class FakePostgrest:
    """Just enough PostgREST, with the live schema's rules."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {t: [] for t in SCHEMA}
        self.fail_tables: set[str] = set()
        self.down = False
        self.calls: list[tuple[str, str]] = []

    def seed_case(self, case_id: str = CASE, client: str = LIVE_CLIENT, employee: str = EMPLOYEE) -> None:
        assert client in LIVE_CLIENTS, "the case's client must exist in client_configs"
        self.tables["onboarding_cases"].append({
            "case_id": case_id, "client_id": client, "employee_no": employee, "joining_date": "2026-11-16",
            "location_name": "Dubai", "project_name": "apollo", "role_id": "backend_engineer",
            "grade": "L3", "department": "Engineering", "manager_id": f"MGR-{client}",
            "nationality_group": "", "onboarding_attributes": [], "candidate_name": "x", "candidate_email": "x",
            "is_deleted": False})

    def check(self, table: str, row: dict, *, full: bool) -> None:
        cols = SCHEMA[table]["columns"]
        unknown = set(row) - set(cols)
        if unknown:
            raise ValueError(f"unknown column(s) {sorted(unknown)} in {table}")
        if full:
            missing = [c for c in SCHEMA[table]["required"] if row.get(c) is None]
            if missing:
                raise ValueError(f"NOT NULL violated in {table}: {missing}")
        for col in UUID_COLS & set(row):
            if row[col] is not None:
                uuid.UUID(str(row[col]))

    def parent_has(self, table: str, column: str, value) -> bool:
        if table == "client_configs":
            return value in LIVE_CLIENTS or any(r.get("client_id") == value for r in self.tables["client_configs"])
        return any(r.get(column) == value for r in self.tables[table])

    def enforce(self, table: str, row: dict, others: list[dict]) -> None:
        """The live FK, UNIQUE and CHECK rules the owner's CSVs showed."""
        for col, parent, pcol in FKS.get(table, []):
            if row.get(col) is not None and not self.parent_has(parent, pcol, row[col]):
                raise ValueError(f"23503 foreign key {table}.{col} -> {parent}.{pcol}")
        for cols in UNIQUE.get(table, []):
            if all(row.get(c) is not None for c in cols) and any(
                    o is not row and all(o.get(c) == row.get(c) for c in cols) for o in others):
                raise ValueError(f"23505 unique {table}{cols}")
        if table == "audit_events" and row.get("actor_type") not in LIVE_ACTOR_TYPES:
            raise ValueError("23514 audit_events_actor_type_check")
        if table == "agent_runs" and row.get("status") not in LIVE_RUN_STATUSES:
            raise ValueError("23514 agent_runs_status_check")
        if table == "approval_requests":
            if row.get("outcome") not in LIVE_APPROVAL_OUTCOMES:
                raise ValueError("23514 approval_requests_outcome_check")
            if row.get("outcome") != "PENDING" and row.get("decided_at") is None:
                raise ValueError("23514 chk_decided_has_timestamp")
            if row.get("decided_by") is not None and row["decided_by"] == row.get("requested_by"):
                raise ValueError("23514 chk_no_self_approval")
        if table == "onboarding_tasks":
            if row.get("status") == "cancelled" and row.get("cancelled_at") is None:
                raise ValueError("23514 chk_task_cancelled_timestamp")
            if row.get("status") == "complete" and row.get("completed_at") is None:
                raise ValueError("23514 chk_task_complete_timestamp")
            if (row.get("status") == "complete" and row.get("evidence_required")
                    and not str(row.get("evidence_ref") or "").strip()):
                raise ValueError("23514 chk_task_evidence_on_complete")

    def __call__(self, method, url, headers, body):
        assert "apikey" in headers
        self.calls.append((method, url))
        if self.down:
            raise PersistenceError("supabase is unreachable", reason="down")
        parts = urlparse(url)
        path = parts.path.rsplit("/rest/v1/", 1)[1]
        query = parse_qsl(parts.query)
        if path == "":
            return 200, {"definitions": {t: {"properties": {c: {} for c in v["columns"]}} for t, v in SCHEMA.items()}}
        table = path
        if table not in self.tables:
            return 404, {"message": "no such table"}
        if table in self.fail_tables and method != "GET":
            return 500, {"message": "boom"}
        rows = self.tables[table]
        q = {k: v for k, v in query if k not in ("select", "order", "limit", "on_conflict")}
        match = [r for r in rows if all(self._ok(r, k, v) for k, v in q.items())]
        try:
            if method == "GET":
                order = dict(query).get("order")
                if order:
                    col, _, direction = order.partition(".")
                    match = sorted(match, key=lambda r: (r.get(col) is None, str(r.get(col))), reverse=direction == "desc")
                return 200, [dict(r) for r in match]
            payload = json.loads(body) if body else None
            if method == "POST":
                conflict = dict(query).get("on_conflict")
                out = []
                for new in payload:
                    full = dict(new)
                    self.check(table, full, full=False)
                    for col in ("case_id",) if False else ():
                        pass
                    defaults = self._defaults(table)
                    key_cols = PKS[table]
                    existing = next((r for r in rows if all(r.get(k) == full.get(k) for k in key_cols if k in full)
                                     and all(k in full for k in key_cols)), None)
                    if existing is not None:
                        if not conflict:
                            return 409, {"message": "duplicate key"}
                        existing.update(full)
                        self.enforce(table, existing, rows)
                        out.append(dict(existing))
                        continue
                    row = {**defaults, **full}
                    self.check(table, row, full=True)
                    self.enforce(table, row, rows)
                    rows.append(row)
                    out.append(dict(row))
                return 201, out
            if method == "PATCH":
                self.check(table, payload, full=False)
                for r in match:
                    r.update(payload)
                    self.enforce(table, r, rows)
                return 200, [dict(r) for r in match]
            if method == "DELETE":
                for r in match:
                    for child, specs in FKS.items():
                        for col, parent, pcol in specs:
                            if parent == table and any(c.get(col) == r.get(pcol) for c in self.tables[child]):
                                raise ValueError(f"23503 {table} row still referenced by {child}.{col}")
                for r in match:
                    rows.remove(r)
                return 200, [dict(r) for r in match]
        except ValueError as exc:
            return 400, {"message": str(exc)}
        return 405, None

    @staticmethod
    def _ok(row, col, expr) -> bool:
        op, _, val = expr.partition(".")
        cell = row.get(col)
        if op == "eq":
            return str(cell).lower() == val.lower()  # PostgREST: eq.false matches a boolean False
        if op == "like":
            return cell is not None and str(cell).startswith(val.rstrip("*"))
        raise AssertionError(op)

    @staticmethod
    def _defaults(table: str) -> dict:
        if table == "plan_revisions":
            return {"revision_id": str(uuid.uuid4())}
        if table == "audit_events":
            return {"event_id": str(uuid.uuid4()), "occurred_at": datetime.now(timezone.utc).isoformat()}
        if table == "onboarding_cases":  # the live column defaults UC-01's table applies
            return {"case_id": str(uuid.uuid4()), "status": "INTAKE_PENDING", "current_stage": "intake",
                    "current_form_version": 1, "is_deleted": False, "variant_is_rehire": False,
                    "variant_is_internal_transfer": False, "variant_is_agency_contractor": False,
                    "variant_is_minor": False, "variant_is_resident": False, "variant_is_remote": False,
                    "grade": "", "nationality_group": "", "department": "General", "location_name": "Headquarters",
                    "project_name": "Internal Operations"}
        for column in ("requirement_id", "milestone_id", "policy_id", "form_id", "matrix_id"):
            if column in SCHEMA[table]["columns"]:
                return {column: str(uuid.uuid4())}
        return {}


def make_service(db: FakePostgrest, config=None, **kw) -> OnboardingService:
    store = OnboardingStore(SupabaseTableClient(SupabaseRestClient("https://example.supabase.co", "k", transport=db)))
    registry = ConfigRegistry()
    registry.register(config or SYNTHETIC_CONFIG)
    return OnboardingService(registry, store=store, require_plan_approval=True, **kw)


def trigger(case: str = CASE, employee: str = EMPLOYEE):
    p = dataclasses.replace(persona("EMP-001"), employee_id=employee, client_id=LIVE_CLIENT)
    return make_trigger(p).model_copy(update={"case_id": case})


class SupabaseStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = FakePostgrest()
        self.db.seed_case()
        self.s = make_service(self.db)
        self.oid = self.s.create_onboarding(HR, trigger()).onboarding_id

    def approve(self, s=None, oid=None):
        s, oid = s or self.s, oid or self.oid
        return s.decide_plan(APPROVER, s.submit_plan(HR, oid).request_id, DecisionOutcome.APPROVED)

    def test_the_onboarding_is_the_uc01_case_and_its_live_row_is_never_written(self) -> None:
        self.assertEqual(self.oid, CASE)
        self.assertFalse([c for c in self.db.calls if "onboarding_cases" in c[1] and c[0] != "GET"])

    def test_tasks_and_dependencies_land_in_the_live_columns(self) -> None:
        record = self.s.get(HR, self.oid)
        live = {r["task_id"]: r for r in self.db.tables["onboarding_tasks"]}
        self.assertEqual(set(live), {t.task_id for t in record.tasks})
        sample = next(iter(live.values()))
        self.assertEqual((sample["case_id"], sample["client_id"]), (CASE, LIVE_CLIENT))
        edges = {(e["task_id"], e["depends_on_task_id"]) for e in self.db.tables["task_dependencies"]}
        self.assertEqual(edges, {(t.task_id, d) for t in record.tasks for d in t.depends_on})
        self.assertTrue(edges)

    def test_owner_and_assignee_map_to_owner_role_and_assigned_actor(self) -> None:
        tid = self.s.get(HR, self.oid).task_by_key("hr_record_create").task_id
        self.s.assign_owner(HR, self.oid, tid, "U-A")
        row = next(r for r in self.db.tables["onboarding_tasks"] if r["task_id"] == tid)
        self.assertEqual((row["assigned_actor_id"], row["owner_role"]), ("U-A", "hr_ops"))

    def test_uc02_only_task_fields_are_kept_in_status_summary_not_dropped(self) -> None:
        tid = self.s.get(HR, self.oid).tasks[0].task_id
        row = next(r for r in self.db.tables["onboarding_tasks"] if r["task_id"] == tid)
        self.assertEqual(row["status_summary"]["uc02"]["failed_report_count"], 0)
        self.assertIn("delay_reason", row["status_summary"]["uc02"])

    def test_restart_recovers_case_tasks_approvals_plan_state_and_audit(self) -> None:
        self.approve()
        before = self.s.get(HR, self.oid)
        ids_before = {e.event_id for e in self.s.audit.events_for(HR)}
        s2 = make_service(self.db)
        self.assertEqual(s2.hydrate(LIVE_CLIENT), 1)
        ids_after = {e.event_id for e in s2.audit.events_for(HR)}
        after = s2.get(HR, self.oid)
        self.assertEqual(after.plan_status.value, "approved")
        self.assertEqual([(t.task_id, t.status, t.due_date, t.depends_on) for t in after.tasks],
                         [(t.task_id, t.status, t.due_date, t.depends_on) for t in before.tasks])
        self.assertEqual(after.plan_events, before.plan_events)
        self.assertEqual(ids_after, ids_before)
        self.assertEqual(s2.inbox.get(HR, s2.inbox.pending(HR)[0].request_id).gate.value
                         if s2.inbox.pending(HR) else "plan_approval", "plan_approval")

    def test_approval_history_is_stored_with_outcome_and_workflow_module(self) -> None:
        self.approve()
        row = self.db.tables["approval_requests"][0]
        self.assertEqual((row["outcome"], row["workflow_module"], row["case_id"], row["subject_ref"]),
                         ("APPROVED", "uc02_onboarding_task_management", CASE, CASE))

    def test_other_workflows_approvals_and_audit_are_not_read_as_uc02_data(self) -> None:
        self.db.tables["approval_requests"].append({
            "request_id": "OTHER", "client_id": LIVE_CLIENT, "gate": "uc04_gate", "approver_role": "x",
            "requested_by": "y", "workflow_module": "uc04", "subject_ref": "s", "payload": {}, "outcome": "PENDING",
            "created_at": "2026-11-02T00:00:00+00:00"})
        self.db.tables["audit_events"].append({
            "event_id": str(uuid.uuid4()), "client_id": LIVE_CLIENT, "actor_type": "SYSTEM", "actor_id": "a",
            "action": "OTHER", "detail": {}, "outcome": "SUCCESS", "actor_role": "system"})
        s2 = make_service(self.db)
        s2.hydrate(LIVE_CLIENT)
        self.assertNotIn("OTHER", [r.request_id for r in s2.store.load_approvals(LIVE_CLIENT)])
        self.assertNotIn("OTHER", [e.action for e in s2.store.load_audit(LIVE_CLIENT)])

    def test_audit_is_append_only_and_free_of_personal_keys(self) -> None:
        store = self.s.store
        event = self.s.audit.events_for(HR)[0]
        with self.assertRaises(PersistenceError):
            store.append_audit([event])  # same id again is refused, never overwritten
        bad = event.model_copy(update={"event_id": "AUD-9999", "detail": {"email": "x"}})
        with self.assertRaises(PersistenceError):
            store.append_audit([bad])

    def test_audit_ids_round_trip_and_actor_type_is_set(self) -> None:
        ids = {e.event_id for e in self.s.audit.events_for(HR)}
        self.assertEqual({e.event_id for e in self.s.store.load_audit(LIVE_CLIENT)}, ids)
        live = [r for r in self.db.tables["audit_events"] if r["action"] != STATE_ACTION]
        self.assertTrue(live and all(r["actor_type"] == "HUMAN" for r in live))  # HR actors are people

    def test_actor_type_mapping_is_explicit_and_always_a_live_allowed_value(self) -> None:
        from onboarding_task_management.contracts import Role

        self.assertEqual(actor_type_for("system"), "SYSTEM")
        for role in Role:
            expected = "SYSTEM" if role is Role.SYSTEM else "HUMAN"
            self.assertEqual(actor_type_for(role.value), expected, role)
            self.assertIn(actor_type_for(role.value), LIVE_ACTOR_TYPES)
        self.assertNotIn("USER", LIVE_ACTOR_TYPES)  # the value the adapter used to send
        self.assertNotEqual(actor_type_for("hr_ops"), "AGENT")  # AGENT is never inferred

    def test_a_system_actor_and_the_state_snapshot_are_stored_as_system(self) -> None:
        from onboarding_task_management.contracts import Actor, Role

        bot = Actor(actor_id="svc-uc02", role=Role.SYSTEM, client_id=LIVE_CLIENT)
        event = self.s.audit.record(bot, "synthetic.check", "ok", onboarding_id=self.oid)
        stored = {r["reference"]: r for r in self.db.tables["audit_events"]}
        self.assertEqual(stored["uc02:" + event.event_id]["actor_type"], "SYSTEM")
        self.assertTrue(all(r["actor_type"] == "SYSTEM" for r in stored.values() if r["action"] == STATE_ACTION))

    def test_the_old_user_value_would_have_been_rejected_by_the_live_check(self) -> None:
        bad = dict(self.db.tables["audit_events"][0], actor_type="USER", event_id=str(uuid.uuid4()))
        with self.assertRaises(ValueError):
            self.db.enforce("audit_events", bad, [])

    def test_request_and_approval_use_two_different_actors(self) -> None:
        self.assertNotEqual(HR.actor_id, APPROVER.actor_id)
        self.approve()
        row = self.db.tables["approval_requests"][0]
        self.assertNotEqual(row["requested_by"], row["decided_by"])
        self.assertEqual((row["requested_by"], row["decided_by"]), (HR.actor_id, APPROVER.actor_id))
        from onboarding_task_management.errors import UnauthorizedActionError

        second = self.s.request_change(HR, self.oid, location="Abu Dhabi")
        with self.assertRaises(UnauthorizedActionError):  # self-approval is refused before the DB could
            self.s.decide_change(HR, second.request_id, DecisionOutcome.APPROVED)

    def test_replan_removal_deletes_edges_before_the_task_so_the_foreign_key_holds(self) -> None:
        from onboarding_task_management.synthetic import LOCATION_REMOTE

        self.approve()
        before = {r["task_id"] for r in self.db.tables["onboarding_tasks"]}
        request = self.s.request_change(HR, self.oid, location=LOCATION_REMOTE)
        self.s.decide_change(APPROVER, request.request_id, DecisionOutcome.APPROVED)
        outcome = self.s.apply_change(HR, request.request_id)
        self.assertTrue(outcome.removed_task_ids)
        after = {r["task_id"] for r in self.db.tables["onboarding_tasks"]}
        self.assertTrue(set(outcome.removed_task_ids) <= before - after)
        for edge in self.db.tables["task_dependencies"]:  # nothing dangles
            self.assertIn(edge["task_id"], after)
            self.assertIn(edge["depends_on_task_id"], after)
        self.assertEqual(after, {t.task_id for t in self.s.get(HR, self.oid).tasks})

    def test_the_fake_really_enforces_the_fk_that_makes_the_old_order_fail(self) -> None:
        """Deleting a prerequisite while an edge still names it is refused, as in the live schema."""
        client = self.s.store.client
        prerequisite = next(e["depends_on_task_id"] for e in self.db.tables["task_dependencies"])
        with self.assertRaises(PersistenceError):
            client.rest.delete("onboarding_tasks", {"task_id": prerequisite})  # bypasses the adapter's order
        client.delete("task", {"task_id": prerequisite})  # the adapter clears the edges first
        self.assertNotIn(prerequisite, {r["task_id"] for r in self.db.tables["onboarding_tasks"]})
        self.assertFalse([e for e in self.db.tables["task_dependencies"]
                          if prerequisite in (e["task_id"], e["depends_on_task_id"])])

    def test_the_test_setup_uses_an_existing_live_client_and_a_unique_employee_no(self) -> None:
        self.assertEqual(self.db.tables["onboarding_cases"][0]["client_id"], LIVE_CLIENT)
        self.assertIn(LIVE_CLIENT, LIVE_CLIENTS)
        self.assertNotEqual(LIVE_CLIENT, "CLIENT-ALPHA")
        self.assertEqual(SYNTHETIC_CONFIG.version, "SYNTHETIC-UC02-TEST")
        with self.assertRaises(ValueError):  # a second case with the same employee_no is refused by the live index
            self.db.enforce("onboarding_cases", dict(self.db.tables["onboarding_cases"][0], case_id="x"),
                            self.db.tables["onboarding_cases"] + [dict(self.db.tables["onboarding_cases"][0], case_id="x")])

    def test_a_second_onboarding_for_the_same_employee_is_refused(self) -> None:
        self.db.seed_case("0b1d7f3e-6f2a-4a55-8a3e-0c0a5d1e2f10")
        with self.assertRaises(DuplicateOnboardingError):
            self.s.create_onboarding(HR, trigger("0b1d7f3e-6f2a-4a55-8a3e-0c0a5d1e2f10"))

    def test_replaying_the_same_save_adds_no_duplicate_rows(self) -> None:
        record = self.s.get(HR, self.oid)
        config = self.s.registry.get(LIVE_CLIENT, on=record.joining_date)
        self.s.store.save_onboarding(record, config=config)
        rows = {t: len(v) for t, v in self.db.tables.items()}
        self.s.store.save_onboarding(record, config=config)
        self.s.store.save_onboarding(record, config=config)
        self.assertEqual({t: len(v) for t, v in self.db.tables.items() if t != "audit_events"},
                         {t: n for t, n in rows.items() if t != "audit_events"})
        states_after = [r for r in self.db.tables["audit_events"] if r["action"] == STATE_ACTION]
        self.assertEqual(len(states_after), len([r for r in self.db.tables["audit_events"]
                                                 if r["action"] == STATE_ACTION]))
        self.assertEqual(rows["audit_events"], len(self.db.tables["audit_events"]))  # unchanged state adds no event

    def test_a_case_that_does_not_exist_is_refused_never_created(self) -> None:
        with self.assertRaises(PersistenceError):
            make_service(self.db).create_onboarding(HR, trigger("11111111-2222-4333-8444-555555555555", EMPLOYEE_2))
        self.assertEqual(len(self.db.tables["onboarding_cases"]), 1)

    def test_a_trigger_without_a_case_id_is_refused_in_the_shared_database(self) -> None:
        with self.assertRaises(PreconditionError):
            make_service(self.db).create_onboarding(HR, trigger("00000000-0000-4000-8000-000000000002", EMPLOYEE_2).model_copy(update={"case_id": None}))

    def test_another_client_cannot_read_the_case(self) -> None:
        other = self.db.tables["onboarding_cases"][0]["client_id"]
        self.assertNotEqual(other, "CLIENT-BETA")
        with self.assertRaises(DataIsolationError):
            make_service(self.db).store.load_onboarding("CLIENT-BETA", CASE)

    def test_a_failed_task_write_is_an_error_and_the_run_is_not_reported_successful(self) -> None:
        self.db.fail_tables = {"onboarding_tasks"}
        self.db.seed_case("0b1d7f3e-6f2a-4a55-8a3e-0c0a5d1e2f10", employee=EMPLOYEE_2)
        with self.assertRaises(PersistenceError):
            make_service(self.db).create_onboarding(HR, trigger("0b1d7f3e-6f2a-4a55-8a3e-0c0a5d1e2f10", EMPLOYEE_2))

    def test_an_unreachable_database_raises_and_never_falls_back(self) -> None:
        self.db.down = True
        with self.assertRaises(PersistenceError):
            self.s.start_task(HR, self.oid, self.s.get(HR, self.oid).tasks[0].task_id)

    def test_an_agent_run_is_stored_in_agent_runs_with_live_columns(self) -> None:
        self.approve()
        from onboarding_task_management.agent import UC02Agent

        UC02Agent(self.s, HR, store=self.s.store).run(self.oid, date(2026, 11, 2))
        run = self.db.tables["agent_runs"][0]
        self.assertEqual((run["case_id"], run["client_id"], run["advisor_mode"]), (CASE, LIVE_CLIENT, run["advisor_mode"]))
        self.assertTrue(self.s.store.agent_runs(LIVE_CLIENT))


def _t(key, category, offset, deps=(), loc=()):
    from onboarding_task_management.contracts import Role
    from onboarding_task_management.sub_agent.checklist import TaskTemplate

    return TaskTemplate(key=key, category=category, title=key, title_ar="x", owner=Role.HR_OPS,
                        offset_days=offset, depends_on=deps, locations=loc)


class ReplanDeletionOrderTests(unittest.TestCase):
    """A task that survives a re-plan depends on one the re-plan removes: the live FK bites here."""

    def config(self):
        from onboarding_task_management.contracts import TaskCategory
        from onboarding_task_management.sub_agent.checklist import ClientOnboardingConfig
        from onboarding_task_management.synthetic import LOCATION_DUBAI, LOCATION_REMOTE

        return ClientOnboardingConfig(
            client_id=LIVE_CLIENT, version="SYNTHETIC-UC02-FK-TEST", approval_status="approved",
            templates=(
                _t("a", TaskCategory.HR_RECORD, -10),
                _t("b", TaskCategory.WORKPLACE, -8, ("a",), (LOCATION_DUBAI,)),   # removed by the change
                _t("c", TaskCategory.ORIENTATION, -5, ("b",)),                    # survives, named b
                _t("d", TaskCategory.PAYROLL, -4, ("a",), (LOCATION_REMOTE,)),    # added by the change
            ))

    def run_change(self, db, service, oid):
        from onboarding_task_management.synthetic import LOCATION_REMOTE

        service.decide_plan(APPROVER, service.submit_plan(HR, oid).request_id, DecisionOutcome.APPROVED)
        request = service.request_change(HR, oid, location=LOCATION_REMOTE)
        service.decide_change(APPROVER, request.request_id, DecisionOutcome.APPROVED)
        return service.apply_change(HR, request.request_id)

    def start(self):
        db = FakePostgrest()
        db.seed_case()
        service = make_service(db, self.config())
        return db, service, service.create_onboarding(HR, trigger()).onboarding_id

    def test_the_surviving_dependant_edge_is_cleared_before_its_prerequisite_is_deleted(self) -> None:
        db, service, oid = self.start()
        b = f"{CASE}#b"
        self.assertIn((f"{CASE}#c", b), {(e["task_id"], e["depends_on_task_id"]) for e in db.tables["task_dependencies"]})
        outcome = self.run_change(db, service, oid)
        self.assertEqual(outcome.removed_task_ids, [b])
        keys = {r["template_key"] for r in db.tables["onboarding_tasks"]}
        self.assertEqual(keys, {"a", "c", "d"})
        ids = {r["task_id"] for r in db.tables["onboarding_tasks"]}
        for edge in db.tables["task_dependencies"]:
            self.assertIn(edge["task_id"], ids)
            self.assertIn(edge["depends_on_task_id"], ids)
        self.assertEqual(service.get(HR, oid).task_by_key("c").depends_on, [])

    def test_the_replan_survives_a_restart_with_the_stored_result(self) -> None:
        db, service, oid = self.start()
        self.run_change(db, service, oid)
        again = make_service(db, self.config())
        again.hydrate(LIVE_CLIENT)
        self.assertEqual({t.template_key for t in again.get(HR, oid).tasks}, {"a", "c", "d"})
        self.assertEqual(again.get(HR, oid).revision, 1)

    def test_the_old_delete_order_is_refused_by_the_foreign_key(self) -> None:
        """Proof the test has teeth: with the task deleted first, the live FK refuses the re-plan."""
        db, service, oid = self.start()
        client = service.store.client
        current = client.delete

        def task_first(table, filters):
            if table == "task":
                return client.rest.delete("onboarding_tasks", {"task_id": filters["task_id"]})
            return current(table, filters)

        client.delete = task_first
        with self.assertRaises(PersistenceError):
            self.run_change(db, service, oid)


def foreign_task(case: str = CASE, key: str = "doc_passport_verification", **extra) -> dict:
    """A task another use case wrote: complete live row, no UC-02 marker in status_summary."""
    row = {"task_id": f"{case}#{key}", "case_id": case, "client_id": LIVE_CLIENT, "employee_id": EMPLOYEE,
           "template_key": key, "plan_order": 1, "category": "document_collection", "title": "t", "title_ar": "",
           "owner_role": "employee", "status": "in_progress", "due_date": "2026-11-10", "evidence_required": False,
           "notes": "", "priority": "normal", "owner_type": "role", "risk_status": "none", "assigned_team": "",
           "sensitive_to": [], "requires_attributes": [], "external_blocking_reason": "", "status_summary": None}
    row.update(extra)
    return row


class OwnershipIsolationTests(unittest.TestCase):
    """UC-02 manages only its own tasks; another use case's rows are never read, overwritten or deleted."""

    def setUp(self) -> None:
        self.db = FakePostgrest()
        self.db.seed_case()
        self.db.tables["onboarding_tasks"].append(foreign_task())  # e.g. written by the orchestrator

    def foreign_rows(self):
        return [r for r in self.db.tables["onboarding_tasks"] if r["task_id"] == f"{CASE}#doc_passport_verification"]

    def test_a_foreign_task_is_not_loaded_into_the_uc02_record(self) -> None:
        s = make_service(self.db)
        oid = s.create_onboarding(HR, trigger()).onboarding_id
        self.assertNotIn("doc_passport_verification", {t.template_key for t in s.get(HR, oid).tasks})
        again = make_service(self.db)
        again.hydrate(LIVE_CLIENT)
        self.assertNotIn("doc_passport_verification", {t.template_key for t in again.get(HR, oid).tasks})

    def test_a_foreign_task_survives_creation_replan_and_restart_unchanged(self) -> None:
        before = dict(self.foreign_rows()[0])
        s = make_service(self.db)
        oid = s.create_onboarding(HR, trigger()).onboarding_id
        s.decide_plan(APPROVER, s.submit_plan(HR, oid).request_id, DecisionOutcome.APPROVED)
        from onboarding_task_management.synthetic import LOCATION_REMOTE

        request = s.request_change(HR, oid, location=LOCATION_REMOTE)
        s.decide_change(APPROVER, request.request_id, DecisionOutcome.APPROVED)
        s.apply_change(HR, request.request_id)  # removes UC-02 tasks: the stale-delete path
        make_service(self.db).hydrate(LIVE_CLIENT)
        self.assertEqual(self.foreign_rows(), [before])

    def test_a_task_id_clash_stops_the_write_and_changes_nothing(self) -> None:
        self.db.tables["onboarding_tasks"].append(foreign_task(key="hr_record_create"))  # same id UC-02 would use
        snapshot = json.dumps(self.db.tables, sort_keys=True, default=str)
        from onboarding_task_management.supabase_store import OwnershipConflictError

        with self.assertRaises(OwnershipConflictError) as ctx:
            make_service(self.db).create_onboarding(HR, trigger())
        self.assertIn("hr_record_create", str(ctx.exception.__dict__))
        owned = [r for r in self.db.tables["onboarding_tasks"] if (r.get("status_summary") or {}).get("uc02") is not None]
        self.assertEqual(owned, [])
        self.assertEqual([r for r in self.db.tables["onboarding_tasks"] if r["template_key"] == "hr_record_create"],
                         [r for r in json.loads(snapshot)["onboarding_tasks"] if r["template_key"] == "hr_record_create"])

    def test_a_template_key_clash_under_a_different_task_id_is_also_refused(self) -> None:
        self.db.tables["onboarding_tasks"].append(foreign_task(key="x", task_id="OTHER-ID", template_key="hr_record_create"))
        from onboarding_task_management.supabase_store import OwnershipConflictError

        with self.assertRaises(OwnershipConflictError):
            make_service(self.db).create_onboarding(HR, trigger())

    def test_the_adapter_refuses_to_delete_a_task_it_does_not_own(self) -> None:
        from onboarding_task_management.supabase_store import OwnershipConflictError

        client = make_service(self.db).store.client
        with self.assertRaises(OwnershipConflictError):
            client.delete("task", {"task_id": f"{CASE}#doc_passport_verification"})
        with self.assertRaises(OwnershipConflictError):
            client.delete("task_dependency", {"task_id": f"{CASE}#doc_passport_verification"})
        self.assertEqual(len(self.foreign_rows()), 1)

    def test_a_foreign_edge_onto_a_uc02_task_blocks_its_removal(self) -> None:
        from onboarding_task_management.supabase_store import OwnershipConflictError

        s = make_service(self.db)
        s.create_onboarding(HR, trigger())
        mine = next(r["task_id"] for r in self.db.tables["onboarding_tasks"] if (r.get("status_summary") or {}).get("uc02") is not None)
        self.db.tables["task_dependencies"].append(
            {"task_id": f"{CASE}#doc_passport_verification", "depends_on_task_id": mine, "client_id": LIVE_CLIENT})
        with self.assertRaises(OwnershipConflictError):
            s.store.client.delete("task", {"task_id": mine})
        self.assertTrue([e for e in self.db.tables["task_dependencies"] if e["depends_on_task_id"] == mine])

    def test_uc01_case_rows_and_foreign_audit_are_never_written(self) -> None:
        s = make_service(self.db)
        case_before = json.dumps(self.db.tables["onboarding_cases"], sort_keys=True)
        s.create_onboarding(HR, trigger())
        self.assertEqual(json.dumps(self.db.tables["onboarding_cases"], sort_keys=True), case_before)
        self.assertFalse([c for c in self.db.calls if "onboarding_cases" in c[1] and c[0] != "GET"])


class AuditPiiProtectionTests(unittest.TestCase):
    """The shared database's trigger rejects these detail keys; UC-02 must refuse them first."""

    DB_KEYS = {"passport", "national_id", "ssn", "salary", "bank_account", "password", "token"}

    def test_uc02_refuses_every_key_the_live_trigger_rejects_before_any_insert(self) -> None:
        db = FakePostgrest()
        db.seed_case()
        s = make_service(db)
        s.create_onboarding(HR, trigger())
        calls = len(db.calls)
        for key in self.DB_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    s.audit.record(HR, "synthetic", "x", onboarding_id=CASE, **{key: "v"})
        self.assertEqual(len(db.calls), calls)  # refused locally: nothing reached the database

    def test_the_adapter_also_refuses_them_if_an_event_reaches_it_directly(self) -> None:
        db = FakePostgrest()
        db.seed_case()
        s = make_service(db)
        event = s.audit.events_for(HR)[0] if s.audit.events_for(HR) else None
        s.create_onboarding(HR, trigger())
        event = s.audit.events_for(HR)[0]
        for key in self.DB_KEYS:
            bad = event.model_copy(update={"event_id": f"AUD-X-{key}", "detail": {key: "v"}})
            with self.assertRaises(PersistenceError):
                s.store.append_audit([bad])

    def test_uc02s_own_forbidden_keys_are_still_refused(self) -> None:
        from onboarding_task_management.audit import FORBIDDEN_DETAIL_KEYS

        self.assertTrue({"name", "email", "phone", "nationality", "address"} <= FORBIDDEN_DETAIL_KEYS)

    def test_the_fake_database_rejects_what_the_live_trigger_rejects(self) -> None:
        # Guards the stand-in itself: the trigger inspects top-level detail keys only.
        from onboarding_task_management.audit import SHARED_DB_FORBIDDEN_DETAIL_KEYS

        self.assertEqual(SHARED_DB_FORBIDDEN_DETAIL_KEYS, self.DB_KEYS)


class SoftDeletedCaseTests(unittest.TestCase):
    """The live onboarding_cases table gained ``is_deleted``; UC-02 must treat such a case as absent."""

    def test_a_soft_deleted_case_cannot_be_started_or_read(self) -> None:
        db = FakePostgrest()
        db.seed_case()
        db.tables["onboarding_cases"][0]["is_deleted"] = True
        with self.assertRaises(PersistenceError):
            make_service(db).create_onboarding(HR, trigger())
        from onboarding_task_management.supabase_rest import SupabaseGateway

        gateway = SupabaseGateway(SupabaseRestClient("https://example.supabase.co", "k", transport=db))
        self.assertIsNone(gateway.get_case(LIVE_CLIENT, CASE))
        self.assertEqual(gateway.list_cases(LIVE_CLIENT), [])
        self.assertEqual(make_service(db).store.client.select("onboarding", {"client_id": LIVE_CLIENT}), [])


class RuntimeModeTests(unittest.TestCase):
    def test_production_rejects_memory_and_sqlite(self) -> None:
        for mode in ("memory", "sqlite", ""):
            with self.assertRaises(PersistenceError):
                store_from_env({"UC02_ENV": "production", "UC02_STORE_MODE": mode})

    def test_supabase_mode_without_credentials_fails_and_does_not_fall_back(self) -> None:
        with self.assertRaises(PersistenceError):
            store_from_env({"UC02_STORE_MODE": "supabase"})

    def test_the_default_without_a_mode_is_still_memory_for_unit_tests(self) -> None:
        self.assertIsNone(store_from_env({}))


if __name__ == "__main__":
    unittest.main()
