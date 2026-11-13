"""UC02's shared Supabase tables, in process, for the orchestration tests that run UC02 in its Supabase mode.

UC02's `SupabaseRestClient` reaches this through its injectable `transport` (`deps.uc02_rest_factory`); the
orchestrator's `SupabaseCaseStateStore` reaches `_fake_postgrest.FakePostgrest` through httpx. Both see the SAME
`onboarding_cases` rows (UC01's table, owned by the FakePostgrest), so a case exists for UC02 exactly when it exists
for the orchestrator, in the same tenant.

Reproduced -- and nothing more:
  * the PostgREST surface UC02 uses: `eq.` / `like.` filters, `order`, `limit`, the OpenAPI root (UC02 verifies its
    required columns at start-up), insert, upsert on a conflict key, PATCH, DELETE, `return=representation`;
  * primary / unique keys (a duplicate insert is 409), the foreign keys to `onboarding_cases` and between tasks and
    their dependencies (409 / 23503), and that UC02 never writes `onboarding_cases`;
  * an outage (`down`: HTTP 503, which UC02 reports as SupabaseUnavailableError).
Seeded with UC02's own synthetic configuration fixture (`supabase_synthetic.build_rows`), marked synthetic. A test
double, never evidence of the live database.
"""

from __future__ import annotations

import copy
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlparse

from onboarding_task_management.supabase_rest import SupabaseRestClient
from onboarding_task_management.supabase_schema import REQUIRED
from onboarding_task_management.supabase_synthetic import FixtureSpec, build_rows

URL = "https://fake-uc02.supabase.co"
KEY = "test-uc02-service-role-key-not-a-real-key"
UC02_ENV = {"UC02_STORE_MODE": "supabase", "UC02_ENV": "test", "UC02_ALLOW_SYNTHETIC_CONFIG": "1",
            "UC02_WORKER_ENV__SUPABASE_URL": URL, "UC02_WORKER_ENV__SUPABASE_SERVICE_ROLE_KEY": KEY}
CONFIG_TABLES = ("client_configs", "client_config_versions", "task_templates", "client_document_requirements",
                 "client_service_policies", "client_holidays", "client_milestone_templates")
KEYS: dict[str, tuple[str, ...]] = {
    "onboarding_tasks": ("task_id",), "task_dependencies": ("task_id", "depends_on_task_id"),
    "plan_revisions": ("case_id", "revision"), "approval_requests": ("request_id",), "audit_events": ("event_id",),
    "agent_runs": ("run_id",), "client_configs": ("client_id",), "client_config_versions": ("client_id", "version"),
    "task_templates": ("client_id", "template_key", "config_version"),
    "client_document_requirements": ("requirement_id",), "client_service_policies": ("policy_id",),
    "client_holidays": ("client_id", "holiday_date"), "client_milestone_templates": ("milestone_id",),
}
CASE_FK = ("onboarding_tasks", "plan_revisions", "approval_requests", "audit_events", "agent_runs")
CASE_DEFAULTS = {"employee_no": None, "joining_date": None, "location_name": None, "project_name": None,
                 "role_id": None, "grade": "", "department": None, "manager_id": None, "nationality_group": "",
                 "onboarding_attributes": [], "is_deleted": False, "status": "INTAKE_PENDING", "hr_officer_id": None,
                 "work_location_type": None, "current_stage": "intake", "opened_at": "2026-11-01T00:00:00+00:00"}


class Uc02Supabase:
    def __init__(self, cases: dict[str, dict[str, Any]], *, client_ids: tuple[str, ...] = ()):
        """`cases`: the orchestrator fake's `tables["onboarding_cases"]` (shared, never written here)."""
        self.cases = cases
        self.tables: dict[str, list[dict[str, Any]]] = {t: [] for t in KEYS}
        self.lock = threading.Lock()
        self.down = False
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        for client_id in client_ids:
            self.seed_client(client_id)

    # ------------------------------------------------------------------ setup
    def seed_client(self, client_id: str) -> None:
        """UC02's synthetic, approved configuration for `client_id` (task templates, documents, team mapping,
        holidays), exactly as UC02's development fixture stores it."""
        rows = build_rows(FixtureSpec(client_id=client_id))
        for table in CONFIG_TABLES:
            self.tables[table].extend(copy.deepcopy(rows.get(table, [])))

    def remove_configuration(self, client_id: str) -> None:
        """The client's configuration is withdrawn (no approved version any more)."""
        for table in CONFIG_TABLES:
            self.tables[table] = [r for r in self.tables[table] if r.get("client_id") != client_id]

    def rest(self) -> SupabaseRestClient:
        return SupabaseRestClient(URL, KEY, transport=self)

    def rows(self, table: str, **match: Any) -> list[dict[str, Any]]:
        source = self._case_rows() if table == "onboarding_cases" else self.tables[table]
        return [copy.deepcopy(r) for r in source if all(r.get(k) == v for k, v in match.items())]

    def _case_rows(self) -> list[dict[str, Any]]:
        return [{**CASE_DEFAULTS, **row} for row in self.cases.values()]

    # ------------------------------------------------------------------ the transport UC02 calls
    def __call__(self, method: str, url: str, headers, body: bytes | None):
        if headers.get("apikey") != KEY:
            return 401, {"message": "Invalid API key"}
        if self.down:
            return 503, {"message": "service unavailable (fake outage)"}
        parts = urlparse(url)
        table = parts.path.rsplit("/rest/v1/", 1)[-1]
        query = parse_qsl(parts.query)
        self.requests.append((method, table, dict(query)))
        if table == "":
            definitions = {t: {"properties": {c: {} for c in cols}} for t, cols in REQUIRED.items()}
            return 200, {"definitions": definitions}
        if table not in self.tables and table != "onboarding_cases":
            return 404, {"message": "relation does not exist"}
        filters = [(k, v) for k, v in query if k not in ("select", "order", "limit", "on_conflict")]
        payload = json.loads(body) if body else None
        with self.lock:
            if table == "onboarding_cases":
                if method != "GET":
                    return 403, {"message": "UC02 never writes onboarding_cases (UC01's table)"}
                return 200, self._select(self._case_rows(), filters, dict(query))
            rows = self.tables[table]
            if method == "GET":
                return 200, self._select(rows, filters, dict(query))
            if method == "POST":
                return self._post(table, rows, payload, dict(query).get("on_conflict"))
            matched = [r for r in rows if self._match(r, filters)]
            if method == "PATCH":
                for r in matched:
                    r.update(copy.deepcopy(payload))
                return 200, copy.deepcopy(matched)
            if method == "DELETE":
                if table == "onboarding_tasks":
                    ids = {r["task_id"] for r in matched}
                    if any(e["task_id"] in ids or e["depends_on_task_id"] in ids for e in self.tables["task_dependencies"]):
                        return 409, {"code": "23503", "message": "still referenced by task_dependencies"}
                for r in matched:
                    rows.remove(r)
                return 200, copy.deepcopy(matched)
        return 405, None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _match(row: dict[str, Any], filters: list[tuple[str, str]]) -> bool:
        for column, expr in filters:
            op, _, value = expr.partition(".")
            cell = row.get(column)
            if op == "eq" and str(cell).lower() != value.lower():
                return False
            if op == "like" and not (cell is not None and str(cell).startswith(value.rstrip("*"))):
                return False
        return True

    def _select(self, rows, filters, query) -> list[dict[str, Any]]:
        out = [copy.deepcopy(r) for r in rows if self._match(r, filters)]
        if query.get("order"):
            column, _, direction = query["order"].partition(".")
            out.sort(key=lambda r: (r.get(column) is None, str(r.get(column))), reverse=direction == "desc")
        if query.get("limit"):
            out = out[: int(query["limit"])]
        return out

    def _post(self, table: str, rows: list[dict[str, Any]], payload: list[dict[str, Any]], conflict: str | None):
        out = []
        keys = KEYS[table]
        for new in payload:
            row = copy.deepcopy(new)
            if table == "audit_events":
                row.setdefault("event_id", str(uuid.uuid4()))
                row.setdefault("occurred_at", datetime.now(timezone.utc).isoformat())
            if table == "plan_revisions":
                row.setdefault("revision_id", str(uuid.uuid4()))
                row.setdefault("applied_at", datetime.now(timezone.utc).isoformat())
            if table in CASE_FK and row.get("case_id") is not None and row["case_id"] not in self.cases:
                return 409, {"code": "23503", "message": f"{table}.case_id -> onboarding_cases"}
            if table == "task_dependencies":
                ids = {r["task_id"] for r in self.tables["onboarding_tasks"]}
                if row["task_id"] not in ids or row["depends_on_task_id"] not in ids:
                    return 409, {"code": "23503", "message": "task_dependencies -> onboarding_tasks"}
            key_cols = tuple(conflict.split(",")) if conflict else keys
            existing = next((r for r in rows if all(r.get(k) == row.get(k) for k in key_cols)), None)
            if existing is None and conflict is None:
                existing_pk = next((r for r in rows if all(r.get(k) == row.get(k) for k in keys)), None)
                if existing_pk is not None:
                    return 409, {"code": "23505", "message": f"duplicate key in {table}"}
            if existing is not None:
                if conflict is None:
                    return 409, {"code": "23505", "message": f"duplicate key in {table}"}
                existing.update(row)
                out.append(copy.deepcopy(existing))
                continue
            rows.append(row)
            out.append(copy.deepcopy(row))
        return 201, out


__all__ = ["KEY", "UC02_ENV", "URL", "Uc02Supabase"]
