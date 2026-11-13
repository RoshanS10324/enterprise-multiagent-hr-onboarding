"""Minimal PostgREST (Supabase REST) client for UC05's backend persistence.

Why PostgREST over httpx, and not supabase-py or psycopg
--------------------------------------------------------
It is the access path the rest of this repository already uses against the shared Supabase project
(UC01 `adapters/supabase/client.py`, UC02 `supabase_rest.py`): the project URL plus the service-role
key. The root `DATABASE_URL` was found to point at another project with a placeholder password
(UC01 docs/schema/live-schema-comparison.md §4), so a Postgres driver is not usable today. httpx is
already a dependency of hr-onboarding-common; nothing new is installed, and only ONE client is
created per process.

What PostgREST cannot do, stated rather than hidden
---------------------------------------------------
- Every call is one statement. Several calls are NOT one transaction. Where UC05 needs atomicity it
  uses a conditional write (a PATCH filtered on the value it expects, i.e. compare-and-set) or a
  primary-key insert that a concurrent writer cannot repeat; see docs/UC05_SUPABASE_INTEGRATION.md.
- The service-role key bypasses Row Level Security. Tenant isolation is therefore enforced by the
  callers: every tenant-scoped query carries an explicit `client_id` filter.

Retries: only reads (GET) are retried, and only on transport errors, 429 and 5xx. A write is never
retried blindly: its outcome may be unknown, and the caller decides (most UC05 writes are keyed so
a deliberate retry is idempotent).

Secrets: the key lives in a SecretStr and is only ever placed in request headers. It is never
logged, never put in a URL, and never included in an exception message or repr. PostgREST error
bodies are not echoed either (they can contain row values); only the HTTP status and SQLSTATE are.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import httpx
from pydantic import SecretStr

Filters = Sequence[tuple[str, str]]


class PersistenceError(RuntimeError):
    """A Supabase operation failed. Carries no credentials and no row values."""

    code = "PERSISTENCE_ERROR"
    retryable = False


class PersistenceUnavailableError(PersistenceError):
    """The database could not be reached (network, timeout, 5xx). The operation may be retried."""

    code = "PERSISTENCE_UNAVAILABLE"
    retryable = True


class PersistenceAuthError(PersistenceError):
    """The credential was refused (HTTP 401/403): a configuration problem, never retried."""

    code = "PERSISTENCE_AUTH_FAILED"


class SchemaMismatchError(PersistenceError):
    """A table or column UC05 needs does not exist (PGRST204/PGRST205/42703/42P01)."""

    code = "SCHEMA_MISMATCH"


class ConstraintViolation(PersistenceError):
    code = "CONSTRAINT_VIOLATION"


class UniqueViolation(ConstraintViolation):
    """SQLSTATE 23505: a unique index rejected the write."""

    code = "UNIQUE_VIOLATION"


class ForeignKeyViolation(ConstraintViolation):
    code = "FOREIGN_KEY_VIOLATION"


class NotNullViolation(ConstraintViolation):
    code = "NOT_NULL_VIOLATION"


class CheckViolation(ConstraintViolation):
    code = "CHECK_VIOLATION"


_SQLSTATE: dict[str, type[PersistenceError]] = {
    "23505": UniqueViolation, "23503": ForeignKeyViolation, "23502": NotNullViolation, "23514": CheckViolation,
    "42703": SchemaMismatchError, "42P01": SchemaMismatchError, "PGRST204": SchemaMismatchError,
    "PGRST205": SchemaMismatchError, "22P02": PersistenceError,
}


def in_list(values: Iterable[Any]) -> str:
    """A PostgREST `in.(...)` operand with every value double-quoted, so commas, parentheses and
    dots inside an id cannot change the filter."""
    quoted = []
    for v in values:
        text = str(v).replace("\\", "\\\\").replace('"', '\\"')
        quoted.append(f'"{text}"')
    return f"in.({','.join(quoted)})"


class PostgrestClient:
    """The verbs UC05's repositories need, and nothing else. A narrow surface is easier to keep safe:
    update and delete REQUIRE at least one filter, so an unrestricted write cannot be expressed."""

    def __init__(self, base_url: str, api_key: SecretStr, *, schema: str = "public", timeout: float = 10.0,
                 read_retries: int = 2, retry_backoff_s: float = 0.25,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.schema = schema
        self.read_retries = max(0, read_retries)
        self.retry_backoff_s = max(0.0, retry_backoff_s)
        key = api_key.get_secret_value()
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/rest/v1", timeout=timeout, transport=transport,
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": schema,
                     "Content-Profile": schema, "Content-Type": "application/json"})

    def __repr__(self) -> str:  # never shows headers
        return f"PostgrestClient(schema={self.schema!r})"

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _raise_for_status(resp: httpx.Response, table: str) -> None:
        if resp.status_code < 400:
            return
        code = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                code = str(body.get("code") or "")
        except ValueError:
            pass
        where = f"{resp.request.method} {table} failed with HTTP {resp.status_code}" + (
            f" (SQLSTATE {code})" if code else "")
        if resp.status_code in (401, 403) and code not in _SQLSTATE:
            raise PersistenceAuthError(f"{where}: the Supabase credential was refused")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise PersistenceUnavailableError(where)
        raise _SQLSTATE.get(code, PersistenceError)(where)

    def _send(self, method: str, table: str, *, params: list[tuple[str, str]], json: Any = None,
              prefer: str | None = None, retries: int = 0) -> list[dict[str, Any]]:
        headers = {"Prefer": prefer} if prefer else None
        attempt = 0
        while True:
            try:
                resp = self._client.request(method, f"/{table}", params=params, json=json, headers=headers)
                self._raise_for_status(resp, table)
                break
            except httpx.TimeoutException:
                error: PersistenceError = PersistenceUnavailableError(f"{method} {table} timed out")
            except httpx.TransportError as exc:
                error = PersistenceUnavailableError(f"{method} {table}: database unreachable ({type(exc).__name__})")
            except PersistenceUnavailableError as exc:
                error = exc
            if attempt >= retries:
                raise error from None
            attempt += 1
            time.sleep(self.retry_backoff_s * attempt)
        if not resp.content:
            return []
        data = resp.json()
        return data if isinstance(data, list) else [data]

    # ------------------------------------------------------------------ verbs
    def select(self, table: str, *, columns: str = "*", filters: Filters = (), order: str | None = None,
               limit: int | None = None) -> list[dict[str, Any]]:
        params = [("select", columns), *filters]
        if order:
            params.append(("order", order))
        if limit is not None:
            params.append(("limit", str(limit)))
        return self._send("GET", table, params=params, retries=self.read_retries)

    def insert(self, table: str, rows: Iterable[Mapping[str, Any]], *, on_conflict: str | None = None,
               merge_duplicates: bool = False, ignore_duplicates: bool = False) -> list[dict[str, Any]]:
        if merge_duplicates and ignore_duplicates:
            raise ValueError("choose merge_duplicates or ignore_duplicates, not both")
        prefer = ["return=representation"]
        if merge_duplicates:
            prefer.append("resolution=merge-duplicates")
        if ignore_duplicates:
            prefer.append("resolution=ignore-duplicates")
        params = [("on_conflict", on_conflict)] if on_conflict else []
        return self._send("POST", table, params=params, json=[dict(r) for r in rows], prefer=",".join(prefer))

    def update(self, table: str, values: Mapping[str, Any], *, filters: Filters) -> list[dict[str, Any]]:
        """PATCH the rows matching `filters`; returns the rows actually changed. Zero rows back means
        nothing matched -- which is how a compare-and-set (a filter on the expected old value) reports
        that someone else changed the row first."""
        if not filters:
            raise ValueError(f"refusing an unfiltered update of {table}")
        return self._send("PATCH", table, params=list(filters), json=dict(values), prefer="return=representation")

    def delete(self, table: str, *, filters: Filters) -> list[dict[str, Any]]:
        if not filters:
            raise ValueError(f"refusing an unfiltered delete from {table}")
        return self._send("DELETE", table, params=list(filters), prefer="return=representation")

    def ping(self) -> None:
        """One real round trip (the OpenAPI root). Raises PersistenceAuthError /
        PersistenceUnavailableError, so a misconfigured deployment fails at startup."""
        try:
            resp = self._client.get("/")
        except httpx.TimeoutException:
            raise PersistenceUnavailableError("Supabase did not answer within the timeout") from None
        except httpx.TransportError as exc:
            raise PersistenceUnavailableError(f"Supabase is unreachable ({type(exc).__name__})") from None
        self._raise_for_status(resp, "/")
