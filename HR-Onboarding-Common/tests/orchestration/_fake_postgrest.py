"""An in-process PostgREST for `case_orchestration_state` and `onboarding_cases`, as an httpx transport.

It reproduces the database behaviour the SupabaseCaseStateStore depends on -- and nothing more:
  * `eq.` filters, including JSON paths (`orchestrator_context->>origin_case_id`), select, order,
    limit/offset;
  * the primary key (a second INSERT of a case is 409 / SQLSTATE 23505) and the foreign key to
    `onboarding_cases` (409 / 23503);
  * a conditional PATCH is ONE atomic step under a lock, as a single UPDATE ... WHERE is in Postgres;
  * the service-role key is checked (401 otherwise) and the database can be taken down (connection error).
`before_write` lets a test act as another process between the store's read and its write.
Synthetic data only. This is a test double, never evidence of a live database.
"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import unquote

import httpx

KEY = "test-service-role-key-not-a-real-key"
URL = "https://fake-project.supabase.co"
_CONTROL = {"select", "order", "limit", "offset"}


class FakePostgrest:
    def __init__(self):
        self.tables: dict[str, dict[str, dict]] = {"onboarding_cases": {}, "case_orchestration_state": {}}
        self.lock = threading.Lock()
        self.down = False
        self.requests: list[tuple[str, str, dict]] = []
        self.before_write: Callable[[str, str, dict], None] | None = None  # (method, case_id, body)

    # ------------------------------------------------------------------ setup helpers
    def add_case(self, case_id: str, client_id: str) -> None:
        self.tables["onboarding_cases"][case_id] = {"case_id": case_id, "client_id": client_id}

    def row(self, case_id: str) -> dict | None:
        return copy.deepcopy(self.tables["case_orchestration_state"].get(case_id))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ request handling
    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("database unreachable", request=request)
        if request.headers.get("apikey") != KEY or request.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"message": "Invalid API key"})
        table = request.url.path.rsplit("/", 1)[-1]
        if table not in self.tables:
            return httpx.Response(404, json={"code": "PGRST205", "message": "relation does not exist"})
        params = [(unquote(k), unquote(v)) for k, v in request.url.params.multi_items()]
        filters = [(k, v[3:]) for k, v in params if k not in _CONTROL]
        self.requests.append((request.method, table, dict(params)))
        body = json.loads(request.content) if request.content else None
        method = request.method
        if method in ("POST", "PATCH") and self.before_write is not None:
            case_id = (body or {}).get("case_id") or dict(filters).get("case_id")
            self.before_write(method, case_id, body)
        with self.lock:
            rows = self.tables[table]
            matched = [r for r in rows.values() if all(self._value(r, k) == v for k, v in filters)]
            if method == "GET":
                return httpx.Response(200, json=self._shape(matched, dict(params)))
            if method == "POST":
                key = body["case_id"]
                if key in rows:
                    return httpx.Response(409, json={"code": "23505", "message": "duplicate key"})
                if table == "case_orchestration_state" and key not in self.tables["onboarding_cases"]:
                    return httpx.Response(409, json={"code": "23503", "message": "foreign key violation"})
                rows[key] = {"updated_at": datetime.now(timezone.utc).isoformat(), **copy.deepcopy(body)}
                return httpx.Response(201)
            if method == "PATCH":
                for r in matched:
                    r.update(copy.deepcopy(body))
                return httpx.Response(200, json=copy.deepcopy(matched))
            if method == "DELETE":
                for r in matched:
                    rows.pop(r["case_id"])
                return httpx.Response(200, json=copy.deepcopy(matched))
        return httpx.Response(405)

    @staticmethod
    def _value(row: dict, key: str):
        if "->>" in key:
            column, path = key.split("->>", 1)
            value = (row.get(column) or {}).get(path)
            return None if value is None else str(value)
        value = row.get(key)
        return None if value is None else str(value)

    @staticmethod
    def _shape(rows: list[dict], params: dict[str, str]) -> list[dict]:
        rows = copy.deepcopy(rows)
        if "order" in params:
            rows.sort(key=lambda r: str(r.get(params["order"].split(".")[0])))
        offset = int(params.get("offset", 0))
        rows = rows[offset:offset + int(params["limit"])] if "limit" in params else rows[offset:]
        select = params.get("select", "*")
        if select != "*":
            columns = select.split(",")
            rows = [{c: r.get(c) for c in columns} for r in rows]
        return rows
