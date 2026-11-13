"""An in-process fake of the PostgREST (Supabase REST) API, mounted as an httpx.MockTransport, so the
supabase-mode repositories run against a real HTTP exchange with no credentials and no network.

It implements only what UC05 sends -- select/order/limit, the filter operators eq, neq, in, is, lt,
lte, gt, gte, like (with JSON `col->>key` paths), POST insert with on_conflict and
resolution=merge|ignore-duplicates, PATCH and DELETE -- and refuses anything else with HTTP 400, so
an unsupported query fails the test instead of being silently approximated.

Constraints: each table declares its primary key and unique keys (taken from the captured live
schema and from the columns UC05 relies on); a violation returns 409 with SQLSTATE 23505, like
PostgREST. Unknown tables (404, PGRST205) and unknown columns (400, 42703) are refused.

Failure injection: `down` (connection refused), `auth_refused` (401), `fail_next(method, table,
status, code)` for one-shot errors. `requests` records every call for tenant-filter assertions.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl

import httpx

from sub_agent.db.mapping import REQUIRED_COLUMNS

EXTRA_COLUMNS = {"onboarding_cases": ("opened_at", "grade", "hr_officer_id"), "audit_events": ("task_id",)}
KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    "onboarding_cases": (("case_id",),), "case_orchestration_state": (("case_id",),),
    "client_configs": (("client_id",),), "client_holidays": (("client_id", "holiday_date"),),
    "client_config_versions": (("client_id", "version"),), "buddy_profiles": (("buddy_id",),),
    "buddy_assignments": (("assignment_id",), ("case_id",)), "orientation_sessions": (("session_id",),),
    "session_participants": (("participant_id",),), "communications": (("comm_id",),),
    "approval_requests": (("request_id",),), "audit_events": (("event_id",),), "agent_runs": (("run_id",),),
}


# The live CHECK constraints (pg_constraint export, 2026-11-04). NULL passes a CHECK, as in Postgres.
CHECKS: dict[str, dict[str, frozenset[str]]] = {
    "buddy_assignments": {"status": frozenset({"PROPOSED", "CONFIRMED", "DECLINED", "REPLACED", "COMPLETED"})},
    "orientation_sessions": {
        "session_type": frozenset({"HR_INDUCTION", "IT_BOOTCAMP", "BUDDY_SYNC", "DEPARTMENT_WELCOME", "LEADERSHIP_MEET"}),
        "status": frozenset({"SCHEDULED", "IN_PROGRESS", "COMPLETED", "CANCELLED", "RESCHEDULED"})},
    "session_participants": {
        "role": frozenset({"NEW_HIRE", "HOST", "PRESENTER", "BUDDY", "MANAGER", "OBSERVER"}),
        "rsvp_status": frozenset({"PENDING", "ACCEPTED", "TENTATIVE", "DECLINED", "DELEGATED"})},
    "communications": {
        "comm_type": frozenset({"WELCOME_EMAIL", "SESSION_INVITE", "SLACK_INTRO", "TEAMS_BROADCAST", "SURVEY_PROMPT",
                                "URGENT_NOTICE"}),
        "status": frozenset({"DRAFT", "AWAITING_REVIEW", "APPROVED", "DISPATCHED", "FAILED", "CANCELLED"})},
    "approval_requests": {"outcome": frozenset({"PENDING", "APPROVED", "EDITED_AND_APPROVED", "REJECTED", "CANCELLED",
                                                "EXPIRED"})},
    "audit_events": {"actor_type": frozenset({"HUMAN", "AGENT", "SYSTEM"})},
    "agent_runs": {"status": frozenset({"proposed", "acted", "exception"})},
}


# NOT NULL columns without a default, as the live schema has them (read-only catalogue, 2026-11-06): a write the real
# database refuses with 23502 is refused here too.
NOT_NULL: dict[str, frozenset[str]] = {
    "session_participants": frozenset({"session_id", "actor_id", "email", "role"}),
    "orientation_sessions": frozenset({"session_id", "case_id", "client_id", "session_type", "start_utc", "end_utc",
                                       "session_metadata"}),
    "buddy_assignments": frozenset({"case_id", "client_id", "buddy_employee_id", "matching_criteria"}),
    "communications": frozenset({"client_id", "comm_type", "recipient_email", "subject", "body_text"}),
}


def _check(table: str, row: dict[str, Any]) -> str | None:
    """The name of the first live CHECK constraint `row` violates, else None."""
    for column, allowed in CHECKS.get(table, {}).items():
        if row.get(column) is not None and row[column] not in allowed:
            return f"{table}_{column}_check"
    if table == "orientation_sessions" and row.get("start_utc") and row.get("end_utc") \
            and _cmp(row["end_utc"], row["start_utc"]) <= 0:
        return "chk_session_times"
    if table == "approval_requests":
        if row.get("outcome") not in (None, "PENDING") and row.get("decided_at") is None:
            return "chk_decided_has_timestamp"
        if row.get("decided_by") is not None and row.get("decided_by") == row.get("requested_by"):
            return "chk_no_self_approval"
    if table == "audit_events" and not str(row.get("action") or "").strip():
        return "audit_events_action_check"
    for column in ("action_count", "exception_count", "proposal_count"):
        if table == "agent_runs" and row.get(column) is not None and row[column] < 0:
            return f"agent_runs_{column}_check"
    if table == "buddy_profiles" and row.get("max_active_buddies") is not None and row["max_active_buddies"] < 1:
        return "buddy_profiles_max_active_buddies_check"
    return None


def _norm(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _as_time(text: str):
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _cmp(left: Any, right: str) -> int:
    a, b = _norm(left), right
    ta, tb = _as_time(a), _as_time(b)
    if ta is not None and tb is not None and (ta.tzinfo is None) == (tb.tzinfo is None):
        return (ta > tb) - (ta < tb)
    try:
        fa, fb = float(a), float(b)
        return (fa > fb) - (fa < fb)
    except ValueError:
        return (a > b) - (a < b)


def _parse_in(operand: str) -> list[str]:
    if not (operand.startswith("(") and operand.endswith(")")):
        raise ValueError("bad in operand")
    values, buf, quoted, i, body = [], "", False, 0, operand[1:-1]
    while i < len(body):
        ch = body[i]
        if quoted:
            if ch == "\\" and i + 1 < len(body):
                buf += body[i + 1]
                i += 2
                continue
            if ch == '"':
                quoted = False
            else:
                buf += ch
        elif ch == '"':
            quoted = True
        elif ch == ",":
            values.append(buf)
            buf = ""
        else:
            buf += ch
        i += 1
    if body:
        values.append(buf)
    return values


class FakePostgrest:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {t: [] for t in REQUIRED_COLUMNS}
        self.columns = {t: set(c) | set(EXTRA_COLUMNS.get(t, ())) for t, c in REQUIRED_COLUMNS.items()}
        self.requests: list[tuple[str, str, list[tuple[str, str]]]] = []
        self.down = False
        self.auth_refused = False
        self._failures: list[tuple[str, str, int, str]] = []
        self.transport = httpx.MockTransport(self._handle)

    # ------------------------------------------------------------------ test helpers
    def seed(self, table: str, *rows: dict[str, Any]) -> None:
        for row in rows:
            self.tables[table].append({c: None for c in self.columns[table]} | copy.deepcopy(row))

    def rows(self, table: str, **match: Any) -> list[dict[str, Any]]:
        return [r for r in self.tables[table] if all(r.get(k) == v for k, v in match.items())]

    def fail_next(self, method: str, table: str, status: int = 500, code: str = "") -> None:
        self._failures.append((method, table, status, code))

    def calls(self, method: str, table: str) -> list[list[tuple[str, str]]]:
        return [p for m, t, p in self.requests if m == method and t == table]

    # ------------------------------------------------------------------ HTTP
    @staticmethod
    def _error(status: int, code: str, message: str = "") -> httpx.Response:
        return httpx.Response(status, json={"code": code, "message": message, "details": None, "hint": None})

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if self.auth_refused:
            return self._error(401, "PGRST301", "JWT invalid")
        path = request.url.path
        assert path.startswith("/rest/v1"), path
        table = path[len("/rest/v1"):].strip("/")
        params = parse_qsl(request.url.query.decode(), keep_blank_values=True)
        self.requests.append((request.method, table, params))
        if table == "":
            return httpx.Response(200, json={})
        for i, (method, t, status, code) in enumerate(self._failures):
            if method == request.method and t == table:
                del self._failures[i]
                return self._error(status, code)
        if table not in self.tables:
            return self._error(404, "PGRST205", "table not found")
        try:
            if request.method == "GET":
                return self._select(table, params)
            if request.method == "POST":
                return self._insert(table, params, json.loads(request.content or b"[]"),
                                    request.headers.get("Prefer", ""))
            if request.method == "PATCH":
                return self._update(table, params, json.loads(request.content or b"{}"))
            if request.method == "DELETE":
                return self._delete(table, params)
        except _Refused as exc:
            return self._error(exc.status, exc.code, exc.message)
        return self._error(405, "PGRST000", "method not supported")

    # ------------------------------------------------------------------ query evaluation
    def _check_column(self, table: str, column: str) -> str:
        base = column.split("->>", 1)[0]
        if base not in self.columns[table]:
            raise _Refused(400, "42703", f"column {base} does not exist")
        return column

    def _value(self, row: dict[str, Any], column: str) -> Any:
        if "->>" in column:
            base, key = column.split("->>", 1)
            inner = row.get(base) or {}
            value = inner.get(key) if isinstance(inner, dict) else None
            return None if value is None else (json.dumps(value) if isinstance(value, (dict, list)) else _norm(value))
        return row.get(column)

    def _match(self, table: str, row: dict[str, Any], filters: list[tuple[str, str]]) -> bool:
        for column, expr in filters:
            self._check_column(table, column)
            negate = expr.startswith("not.")
            if negate:
                expr = expr[4:]
            op, _, operand = expr.partition(".")
            value = self._value(row, column)
            if op == "eq":
                ok = _norm(value) == operand
            elif op == "neq":
                ok = _norm(value) != operand
            elif op == "in":
                ok = _norm(value) in _parse_in(operand)
            elif op == "is":
                ok = {"null": value is None, "true": value is True, "false": value is False}[operand]
            elif op in ("lt", "lte", "gt", "gte"):
                if value is None:
                    ok = False
                else:
                    c = _cmp(value, operand)
                    ok = {"lt": c < 0, "lte": c <= 0, "gt": c > 0, "gte": c >= 0}[op]
            elif op == "like":
                pattern = "^" + re.escape(operand).replace(r"\*", ".*").replace("%", ".*").replace("_", ".") + "$"
                ok = value is not None and re.match(pattern, _norm(value)) is not None
            else:
                raise _Refused(400, "PGRST100", f"operator {op} not supported by the fake")
            if ok == negate:
                return False
        return True

    @staticmethod
    def _split(params: list[tuple[str, str]]) -> tuple[dict[str, str], list[tuple[str, str]]]:
        special = {k: v for k, v in params if k in ("select", "order", "limit", "on_conflict")}
        return special, [(k, v) for k, v in params if k not in special]

    def _select(self, table: str, params: list[tuple[str, str]]) -> httpx.Response:
        special, filters = self._split(params)
        columns = [c.strip() for c in special.get("select", "*").split(",")]
        if columns != ["*"]:
            for c in columns:
                self._check_column(table, c)
        rows = [r for r in self.tables[table] if self._match(table, r, filters)]
        for part in reversed([p for p in special.get("order", "").split(",") if p]):
            col, _, direction = part.partition(".")
            self._check_column(table, col)
            rows.sort(key=lambda r: (r.get(col) is None, _norm(r.get(col))), reverse=direction.startswith("desc"))
        if "limit" in special:
            rows = rows[: int(special["limit"])]
        return httpx.Response(200, json=[copy.deepcopy(r if columns == ["*"] else {c: r.get(c) for c in columns})
                                         for r in rows])

    def _conflict(self, table: str, row: dict[str, Any], *, ignore: dict[str, Any] | None = None):
        for key in KEYS.get(table, ()):
            for other in self.tables[table]:
                if other is not ignore and all(other.get(k) == row.get(k) for k in key) \
                        and all(row.get(k) is not None for k in key):
                    return other, key
        return None, None

    def _insert(self, table: str, params: list[tuple[str, str]], rows: list[dict[str, Any]],
                prefer: str) -> httpx.Response:
        special, _ = self._split(params)
        on_conflict = tuple(c.strip() for c in special["on_conflict"].split(",")) if "on_conflict" in special else None
        out = []
        staged = [r for r in self.tables[table]]
        for incoming in rows:
            for c in incoming:
                self._check_column(table, c)
            row = {c: None for c in self.columns[table]} | copy.deepcopy(incoming)
            missing = sorted(c for c in NOT_NULL.get(table, ()) if row.get(c) is None)
            if missing:
                self.tables[table] = staged
                raise _Refused(400, "23502", f'null value in column "{missing[0]}" of relation "{table}" violates '
                                             f'not-null constraint')
            violated = _check(table, row)
            if violated:
                self.tables[table] = staged
                raise _Refused(400, "23514", f'new row for relation "{table}" violates check constraint "{violated}"')
            existing, key = self._conflict(table, row)
            if existing is not None:
                if on_conflict and key == on_conflict and "resolution=merge-duplicates" in prefer:
                    existing.update(copy.deepcopy(incoming))
                    out.append(copy.deepcopy(existing))
                    continue
                if "resolution=ignore-duplicates" in prefer:
                    continue
                self.tables[table] = staged  # the whole statement fails
                raise _Refused(409, "23505", "duplicate key value violates unique constraint")
            self.tables[table].append(row)
            out.append(copy.deepcopy(row))
        return httpx.Response(201, json=out)

    def _update(self, table: str, params: list[tuple[str, str]], values: dict[str, Any]) -> httpx.Response:
        _, filters = self._split(params)
        for c in values:
            self._check_column(table, c)
        matched = [r for r in self.tables[table] if self._match(table, r, filters)]
        for row in matched:
            candidate = {**row, **copy.deepcopy(values)}
            violated = _check(table, candidate)
            if violated:
                raise _Refused(400, "23514", f'new row for relation "{table}" violates check constraint "{violated}"')
            other, _ = self._conflict(table, candidate, ignore=row)
            if other is not None:
                raise _Refused(409, "23505", "duplicate key value violates unique constraint")
        for row in matched:
            row.update(copy.deepcopy(values))
        return httpx.Response(200, json=copy.deepcopy(matched))

    def _delete(self, table: str, params: list[tuple[str, str]]) -> httpx.Response:
        _, filters = self._split(params)
        gone = [r for r in self.tables[table] if self._match(table, r, filters)]
        self.tables[table] = [r for r in self.tables[table] if r not in gone]
        return httpx.Response(200, json=gone)


class _Refused(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message
