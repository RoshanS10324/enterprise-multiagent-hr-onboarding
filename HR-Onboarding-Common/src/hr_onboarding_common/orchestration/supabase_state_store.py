"""The orchestrator's case state in Supabase: the shared `case_orchestration_state` table.

`case_orchestration_state` is the authoritative orchestration state of a case once UC01 has created
its `onboarding_cases` row (the table's `case_id` is a uuid foreign key to `onboarding_cases`). The
orchestrator owns every write to it; the UCs return structured results and never write it (UC05 only
reads it, read-only, scoped to its tenant).

Columns <- state (orchestration/state.py; nothing is renamed or invented):
  case_id, client_id, correlation_id, workflow_status, overall_status, stage_statuses,
  branch_statuses, completed_stages, pending_actions, blockers, stage_outcomes, retry_metadata,
  verified_context                          the state fields of the same names
  last_error_code / last_error_detail       error_code / error_detail (detail truncated)
  version                                   state_version (monotonic, +1 per write)
  updated_at                                updated_at
  orchestrator_context                      {"format", "origin_case_id", "state": every other field}
Reading a row back gives the same state (`row_to_state(state_to_row(s)) == s`), except that an
error detail longer than MAX_ERROR_DETAIL characters is stored truncated.

What this store guarantees, and how
-----------------------------------
- Optimistic concurrency, enforced by the database: every update is ONE conditional PATCH filtered on
  `case_id`, `client_id` AND the version this store expects (`version=eq.<n>`). PostgREST runs it as a
  single `UPDATE ... WHERE version = n`, atomic in Postgres, so two writers can never both succeed from
  the same version. The expected version is the one the orchestrator last loaded (`load`) or this store
  last wrote; a write by anyone else in between makes the PATCH match no row and raises
  `StateVersionConflict` -- never a silent overwrite. The first write of a case is an INSERT, which the
  primary key makes exclusive (a concurrent first write gets a conflict, not a duplicate). No RPC is
  needed for this single-row compare-and-swap; a multi-row transaction is NOT provided (none is used).
- Tenant isolation on every query: reads scoped to a tenant filter on `client_id`; every write filters
  on the state's own `client_id`; before the first write of a case, its `onboarding_cases` row must exist
  in that same tenant (the foreign key alone checks only `case_id`). The service-role key bypasses RLS,
  so these filters are the isolation -- they are applied by this class, not left to callers.
- No silent fallback: an unreachable database, a refused key or a schema mismatch raises. Nothing is
  written anywhere else instead.

Provisional cases (explicit, not a fallback)
--------------------------------------------
Before UC01 opens the case, a case has only the orchestrator's provisional id (`CASE-...`, not a
uuid) and no `onboarding_cases` row, so the foreign key makes a `case_orchestration_state` row
impossible. Those states, and the alias entries that map a provisional id to UC01's case id, are kept
in the `provisional` store the caller configures (bootstrap: SQLite under the orchestrator's data
directory, or memory without one). From UC01's case creation on, the case lives here. After a restart,
a provisional id is still resolved through `orchestrator_context.origin_case_id`.

Secrets: the service-role key is held in a SecretStr and only ever placed in request headers -- never
logged, never put in an exception. PostgREST error bodies are not echoed (they can carry row values);
only the HTTP status and SQLSTATE are.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, MutableMapping
from typing import Any

import httpx
from pydantic import SecretStr

from hr_onboarding_common.orchestration.case_store import (CaseOwnershipError, StatePersistenceError,
                                                           StatePersistenceUnavailable, StateVersionConflict)

log = logging.getLogger(__name__)

TABLE = "case_orchestration_state"
CASES_TABLE = "onboarding_cases"
FORMAT = "hr_onboarding_common.orchestration/case_state@1"
# State field -> column, for the fields stored in a column of their own (everything else goes to
# orchestrator_context.state).
COLUMN_OF: dict[str, str] = {
    "case_id": "case_id", "client_id": "client_id", "correlation_id": "correlation_id",
    "workflow_status": "workflow_status", "overall_status": "overall_status", "stage_statuses": "stage_statuses",
    "branch_statuses": "branch_statuses", "completed_stages": "completed_stages",
    "pending_actions": "pending_actions", "blockers": "blockers", "stage_outcomes": "stage_outcomes",
    "retry_metadata": "retry_metadata", "verified_context": "verified_context", "error_code": "last_error_code",
    "error_detail": "last_error_detail", "state_version": "version", "updated_at": "updated_at",
}
_JSON_DEFAULTS: dict[str, Any] = {"stage_statuses": {}, "branch_statuses": {}, "completed_stages": [],
                                  "pending_actions": [], "blockers": [], "stage_outcomes": {}, "retry_metadata": {}}
MAX_ERROR_DETAIL = 500
_COLUMNS = ",".join(sorted({*COLUMN_OF.values(), "orchestrator_context"}))


def is_case_uuid(case_id: str) -> bool:
    try:
        uuid.UUID(str(case_id))
    except ValueError:
        return False
    return True


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def state_to_row(state: Mapping[str, Any]) -> dict[str, Any]:
    """The `case_orchestration_state` row of a case state (see the module docstring)."""
    state = _jsonable(dict(state))
    row: dict[str, Any] = {column: state.get(field) for field, column in COLUMN_OF.items()}
    for field, default in _JSON_DEFAULTS.items():
        if row[COLUMN_OF[field]] is None:
            row[COLUMN_OF[field]] = default
    row["completed_stages"] = [str(s) for s in row["completed_stages"]]
    row["version"] = int(state.get("state_version") or 0)
    if row["last_error_detail"] and len(row["last_error_detail"]) > MAX_ERROR_DETAIL:
        row["last_error_detail"] = row["last_error_detail"][:MAX_ERROR_DETAIL] + "..."
    if not row["updated_at"]:
        row.pop("updated_at")  # the column default (now()) applies
    rest = {k: v for k, v in state.items() if k not in COLUMN_OF}
    row["orchestrator_context"] = {"format": FORMAT,
                                   "origin_case_id": (state.get("intake_trigger") or {}).get("origin_case_id"),
                                   "state": rest}
    return row


def row_to_state(row: Mapping[str, Any]) -> dict[str, Any]:
    """The case state a row holds. A row this orchestrator did not write is refused, not guessed at."""
    context = row.get("orchestrator_context") or {}
    if context.get("format") != FORMAT or not isinstance(context.get("state"), dict):
        raise StatePersistenceError(f"{TABLE} row for case {row.get('case_id')} was not written by the "
                                    f"onboarding orchestrator (orchestrator_context.format is not {FORMAT})")
    state = copy.deepcopy(context["state"])
    for field, column in COLUMN_OF.items():
        state[field] = copy.deepcopy(row.get(column))
    state["state_version"] = int(row.get("version") or 0)
    return state


class SupabaseCaseStateStore(MutableMapping):
    """Case states in `case_orchestration_state` (see the module docstring). Drop-in for the
    orchestrator's `deps.case_states`: `load(case_id, client_id=...)` is the read the orchestrator
    uses before it changes a case (it sets the version the next write expects); `[]`/`get` read
    without changing that expectation."""

    def __init__(self, *, url: str, service_role_key: SecretStr | str, provisional: MutableMapping,
                 schema: str = "public", timeout_s: float = 15.0, transport: httpx.BaseTransport | None = None,
                 read_attempts: int = 3):
        if not url.startswith("https://") and transport is None:
            raise StatePersistenceError("SUPABASE_URL must be an https:// URL")
        key = service_role_key if isinstance(service_role_key, SecretStr) else SecretStr(service_role_key)
        if not key.get_secret_value().strip():
            raise StatePersistenceError("SUPABASE_SERVICE_ROLE_KEY is not set")
        self.provisional = provisional
        self._schema = schema
        self._read_attempts = max(1, read_attempts)
        self._client = httpx.Client(base_url=url.rstrip("/") + "/rest/v1", timeout=timeout_s, transport=transport,
                                    headers={"apikey": key.get_secret_value(),
                                             "Authorization": f"Bearer {key.get_secret_value()}",
                                             "Accept-Profile": schema, "Content-Profile": schema})
        self._lock = threading.RLock()
        self._expected: dict[str, int] = {}  # case_id -> the version the next write expects
        self.writes = 0  # successful writes by this instance (evidence for tests and reports)

    @classmethod
    def from_env(cls, environ: Mapping[str, str], *, provisional: MutableMapping,
                 transport: httpx.BaseTransport | None = None) -> "SupabaseCaseStateStore":
        """ORCHESTRATOR_SUPABASE_URL / ORCHESTRATOR_SUPABASE_SERVICE_ROLE_KEY / ORCHESTRATOR_SUPABASE_SCHEMA first:
        dedicated names, like DOCUMENT_STORAGE_*, because the UC worker processes inherit this environment and a
        bare SUPABASE_URL switches UC01 into its Supabase mode (workers never receive the orchestrator's key,
        workers/config.child_environment). The plain SUPABASE_* names remain accepted."""
        def setting(name: str) -> str:
            return (environ.get(f"ORCHESTRATOR_{name}") or environ.get(name) or "").strip()

        url = setting("SUPABASE_URL")
        if not url:
            raise StatePersistenceError("ORCHESTRATOR_STATE_STORE=supabase but neither ORCHESTRATOR_SUPABASE_URL nor "
                                        "SUPABASE_URL is set")
        return cls(url=url, service_role_key=setting("SUPABASE_SERVICE_ROLE_KEY"),
                   schema=setting("SUPABASE_SCHEMA") or "public",
                   timeout_s=float(environ.get("SUPABASE_TIMEOUT_S") or 15), provisional=provisional,
                   transport=transport)

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, *, params: dict[str, str] | None = None,
                 body: Any = None, prefer: str | None = None, what: str) -> httpx.Response:
        headers = {"Prefer": prefer} if prefer else {}
        content = json.dumps(body, default=str) if body is not None else None
        if content is not None:
            headers["Content-Type"] = "application/json"
        attempts = self._read_attempts if method == "GET" else 1  # a write is never retried blindly
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.request(method, path, params=params, content=content, headers=headers)
            except httpx.HTTPError as exc:
                if attempt < attempts:
                    time.sleep(0.2 * attempt)
                    continue
                raise StatePersistenceUnavailable(f"{what}: Supabase unreachable ({type(exc).__name__})") from None
            if response.status_code in (429, 500, 502, 503, 504) and attempt < attempts:
                time.sleep(0.2 * attempt)
                continue
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _fail(response: httpx.Response, what: str) -> StatePersistenceError:
        try:
            sqlstate = (response.json() or {}).get("code")
        except ValueError:
            sqlstate = None
        detail = f"{what} failed with HTTP {response.status_code}" + (f" (SQLSTATE {sqlstate})" if sqlstate else "")
        if response.status_code in (401, 403):
            return StatePersistenceError(detail + ": the Supabase key was refused")
        if response.status_code >= 500 or response.status_code == 429:
            return StatePersistenceUnavailable(detail)
        if sqlstate == "23505":
            return StateVersionConflict(detail + ": the case state was created concurrently")
        if sqlstate == "23503":
            return CaseOwnershipError(detail + ": the case has no onboarding_cases row")
        return StatePersistenceError(detail)

    # ------------------------------------------------------------------ reads
    def _select(self, filters: dict[str, str], what: str) -> list[dict[str, Any]]:
        response = self._request("GET", f"/{TABLE}", params={"select": _COLUMNS, **filters}, what=what)
        if response.status_code != 200:
            raise self._fail(response, what)
        return response.json()

    def _row(self, case_id: str, client_id: str | None) -> dict[str, Any] | None:
        filters = {"case_id": f"eq.{case_id}"}
        if client_id is not None:
            filters["client_id"] = f"eq.{client_id}"
        rows = self._select(filters, f"read {TABLE} {case_id}")
        return rows[0] if rows else None

    def _alias_from_origin(self, origin: str, client_id: str | None) -> dict[str, Any] | None:
        filters = {"orchestrator_context->>origin_case_id": f"eq.{origin}"}
        if client_id is not None:
            filters["client_id"] = f"eq.{client_id}"
        rows = self._select(filters, f"resolve provisional case {origin}")
        if len(rows) > 1:
            raise StatePersistenceError(f"provisional case {origin} maps to {len(rows)} cases")
        return {"alias_of": rows[0]["case_id"], "client_id": rows[0]["client_id"]} if rows else None

    def load(self, case_id: str, *, client_id: str | None = None) -> dict[str, Any] | None:
        """The stored state (or alias entry), scoped to `client_id` when given. The version read
        becomes the version the next write of this case expects."""
        if not is_case_uuid(case_id):
            entry = self.provisional.get(case_id)
            if entry is not None and client_id is not None and entry.get("client_id") != client_id:
                return None
            return copy.deepcopy(entry) if entry is not None else self._alias_from_origin(case_id, client_id)
        row = self._row(case_id, client_id)
        if row is None:
            return None
        state = row_to_state(row)
        with self._lock:
            self._expected[case_id] = state["state_version"]
        return state

    def __getitem__(self, case_id: str) -> dict[str, Any]:
        if not is_case_uuid(case_id):
            entry = self.provisional.get(case_id)
            entry = copy.deepcopy(entry) if entry is not None else self._alias_from_origin(case_id, None)
        else:
            row = self._row(case_id, None)
            entry = row_to_state(row) if row is not None else None
        if entry is None:
            raise KeyError(case_id)
        return entry

    def __contains__(self, case_id: object) -> bool:
        try:
            self[case_id]  # type: ignore[index]
        except KeyError:
            return False
        return True

    def __iter__(self) -> Iterator[str]:
        ids: list[str] = list(self.provisional)
        offset, page = 0, 500
        while True:
            response = self._request("GET", f"/{TABLE}", params={
                "select": "case_id", "orchestrator_context->>format": f"eq.{FORMAT}", "order": "case_id",
                "limit": str(page), "offset": str(offset)}, what=f"list {TABLE}")
            if response.status_code != 200:
                raise self._fail(response, f"list {TABLE}")
            rows = response.json()
            ids.extend(r["case_id"] for r in rows)
            if len(rows) < page:
                return iter(ids)
            offset += page

    def __len__(self) -> int:
        return sum(1 for _ in self)

    # ------------------------------------------------------------------ writes
    def save(self, state: dict[str, Any]) -> None:
        self[state["case_id"]] = state

    def __setitem__(self, case_id: str, state: dict[str, Any]) -> None:
        if not is_case_uuid(case_id):
            # Provisional (pre-intake) id or alias entry: no onboarding_cases row can exist for it.
            log.info("orchestration.state_store.provisional", extra={"case_id": case_id,
                                                                    "client_id": state.get("client_id")})
            self.provisional[case_id] = copy.deepcopy(state)
            return
        if "alias_of" in state:
            raise StatePersistenceError(f"alias entries are keyed by provisional ids, not by case {case_id}")
        if state.get("case_id") != case_id:
            raise StatePersistenceError(f"state for case {state.get('case_id')} stored under {case_id}")
        client_id = state.get("client_id")
        if not client_id:
            raise StatePersistenceError(f"case {case_id}: a state without client_id cannot be stored")
        row = state_to_row(state)
        with self._lock:
            expected = self._expected.get(case_id)
        if expected is None:
            self._insert(case_id, client_id, row)
        else:
            self._update(case_id, client_id, row, expected)
        with self._lock:
            self._expected[case_id] = row["version"]
            self.writes += 1

    def _insert(self, case_id: str, client_id: str, row: dict[str, Any]) -> None:
        owner = self._request("GET", f"/{CASES_TABLE}", params={
            "select": "case_id", "case_id": f"eq.{case_id}", "client_id": f"eq.{client_id}"},
            what=f"check {CASES_TABLE} {case_id}")
        if owner.status_code != 200:
            raise self._fail(owner, f"check {CASES_TABLE} {case_id}")
        if not owner.json():
            # Unknown case, or a case of another tenant: reported the same way.
            raise CaseOwnershipError(f"case {case_id} has no {CASES_TABLE} row in tenant {client_id}")
        response = self._request("POST", f"/{TABLE}", body=row, prefer="return=minimal", what=f"create {TABLE} {case_id}")
        if response.status_code not in (200, 201, 204):
            raise self._fail(response, f"create {TABLE} {case_id}")

    def _update(self, case_id: str, client_id: str, row: dict[str, Any], expected: int) -> None:
        if row["version"] <= expected:
            raise StatePersistenceError(f"case {case_id}: version {row['version']} does not advance {expected}")
        body = {k: v for k, v in row.items() if k not in ("case_id", "client_id")}
        response = self._request("PATCH", f"/{TABLE}", params={
            "case_id": f"eq.{case_id}", "client_id": f"eq.{client_id}", "version": f"eq.{expected}"},
            body=body, prefer="return=representation", what=f"update {TABLE} {case_id}")
        if response.status_code != 200:
            raise self._fail(response, f"update {TABLE} {case_id}")
        if response.json():
            return
        current = self._row(case_id, client_id)
        if current is None:
            raise CaseOwnershipError(f"case {case_id} has no {TABLE} row in tenant {client_id}")
        raise StateVersionConflict(f"case {case_id}: expected version {expected}, the stored version is "
                                   f"{current.get('version')} (written by another process)")

    def __delitem__(self, case_id: str) -> None:
        if not is_case_uuid(case_id):
            del self.provisional[case_id]
            return
        response = self._request("DELETE", f"/{TABLE}", params={"case_id": f"eq.{case_id}"},
                                 prefer="return=representation", what=f"delete {TABLE} {case_id}")
        if response.status_code != 200:
            raise self._fail(response, f"delete {TABLE} {case_id}")
        if not response.json():
            raise KeyError(case_id)
        with self._lock:
            self._expected.pop(case_id, None)

    def close(self) -> None:
        self._client.close()
