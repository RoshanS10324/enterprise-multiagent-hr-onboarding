"""UC03 in Supabase mode inside the orchestrator: the table client UC03's own stores run on.

UC03 runs in the orchestrator's process (adapters/uc03_documents.py). Its Supabase stores (UC03
`integrations/supabase`: `uc03_case_state`, `audit_events`, `approval_requests`, `document_validations`,
`candidate_documents`) take an injected `TableClient`. Left alone, UC03 would build that client from the bare
`SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY`; in the orchestrator's environment a bare `SUPABASE_URL` is inherited
by every worker and switches UC01 into its Supabase mode. So the orchestrator builds UC03's client itself, from the
same scoped settings its case-state store uses (`ORCHESTRATOR_SUPABASE_URL` / `_SERVICE_ROLE_KEY` / `_SCHEMA`,
then the plain names), and hands it to UC03's own `build_stores(..., tables=...)`. No second Supabase project, no
second client configuration.

The client implements UC03's `TableClient` protocol (integrations/supabase/tables.py) over PostgREST and raises
UC03's own errors: `duplicate_error` on a primary-key / unique violation (HTTP 409, SQLSTATE 23505), and
`storage_error` with a short code otherwise. No row data, URL or key ever reaches an exception message or a log.
The service-role key stays in this process (workers/config.child_environment never passes it to a worker).

Settings: `UC03_STORE_MODE` (`memory` | `supabase`; default `memory`) selects UC03's stores.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import SecretStr

STORE_MODES = ("memory", "supabase")


class Uc03StoreConfigurationError(RuntimeError):
    """UC03's Supabase mode cannot start: names the missing setting, never its value."""


def uc03_store_mode(environ: Mapping[str, str]) -> str:
    mode = (environ.get("UC03_STORE_MODE") or "memory").strip().lower() or "memory"
    if mode not in STORE_MODES:
        raise Uc03StoreConfigurationError(f"UC03_STORE_MODE={mode!r} is not one of {list(STORE_MODES)}")
    return mode


def _setting(environ: Mapping[str, str], name: str) -> str:
    return (environ.get(f"ORCHESTRATOR_{name}") or environ.get(name) or "").strip()


class Uc03PostgrestTables:
    """UC03's `TableClient` (select / insert / update) over PostgREST with the backend's service-role key."""

    def __init__(self, *, url: str, service_role_key: SecretStr | str, schema: str = "public",
                 duplicate_error: type[Exception], storage_error: type[Exception],
                 transport: httpx.BaseTransport | None = None, timeout_s: float = 15.0):
        key = service_role_key if isinstance(service_role_key, SecretStr) else SecretStr(service_role_key or "")
        if not url:
            raise Uc03StoreConfigurationError("UC03_STORE_MODE=supabase but neither ORCHESTRATOR_SUPABASE_URL nor "
                                              "SUPABASE_URL is set")
        if not key.get_secret_value().strip():
            raise Uc03StoreConfigurationError("UC03_STORE_MODE=supabase but neither ORCHESTRATOR_SUPABASE_SERVICE_ROLE_"
                                              "KEY nor SUPABASE_SERVICE_ROLE_KEY is set")
        if not url.startswith("https://") and transport is None:
            raise Uc03StoreConfigurationError("the Supabase URL for UC03 must be an https:// URL")
        self._duplicate, self._error = duplicate_error, storage_error
        self._client = httpx.Client(base_url=url.rstrip("/") + "/rest/v1", timeout=timeout_s, transport=transport,
                                    headers={"apikey": key.get_secret_value(),
                                             "Authorization": f"Bearer {key.get_secret_value()}",
                                             "Accept-Profile": schema, "Content-Profile": schema})

    @classmethod
    def from_env(cls, environ: Mapping[str, str], *, duplicate_error: type[Exception], storage_error: type[Exception],
                 transport: httpx.BaseTransport | None = None) -> "Uc03PostgrestTables":
        return cls(url=_setting(environ, "SUPABASE_URL"), service_role_key=_setting(environ, "SUPABASE_SERVICE_ROLE_KEY"),
                   schema=_setting(environ, "SUPABASE_SCHEMA") or "public", duplicate_error=duplicate_error,
                   storage_error=storage_error, transport=transport,
                   timeout_s=float(environ.get("SUPABASE_TIMEOUT_S") or 15))

    # ------------------------------------------------------------------------------------------- TableClient
    def select(self, table: str, *, columns: str = "*", filters: dict[str, Any] | None = None,
               order: str | None = None, desc: bool = False, limit: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {"select": columns}
        params.update(self._eq(filters))
        if order:
            params["order"] = f"{order}.{'desc' if desc else 'asc'}"
        if limit:
            params["limit"] = str(int(limit))
        response = self._send("GET", table, params=params)
        return list(response.json() or [])

    def insert(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        response = self._send("POST", table, body=row, prefer="return=representation")
        rows = response.json() or []
        if not rows:
            raise self._error("DATABASE_ERROR", "insert returned no row")
        return dict(rows[0])

    def update(self, table: str, values: dict[str, Any], filters: dict[str, Any]) -> list[dict[str, Any]]:
        if not filters:
            raise self._error("DATABASE_ERROR", "an unfiltered update is not allowed")
        response = self._send("PATCH", table, params=self._eq(filters), body=values, prefer="return=representation")
        return list(response.json() or [])

    # ------------------------------------------------------------------------------------------------- http
    @staticmethod
    def _eq(filters: dict[str, Any] | None) -> dict[str, str]:
        out = {}
        for column, value in (filters or {}).items():
            if value is None:
                out[column] = "is.null"
            elif isinstance(value, bool):
                out[column] = f"is.{str(value).lower()}"
            else:
                out[column] = f"eq.{value}"
        return out

    def _send(self, method: str, table: str, *, params: dict[str, str] | None = None, body: Any = None,
              prefer: str | None = None) -> httpx.Response:
        headers = {"Prefer": prefer} if prefer else {}
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body, default=str)
        try:
            response = self._client.request(method, f"/{table}", params=params, content=content, headers=headers)
        except httpx.HTTPError:
            raise self._error("DATABASE_UNAVAILABLE", "supabase unreachable") from None
        if response.status_code < 300:
            return response
        try:
            code = (response.json() or {}).get("code") or ""
        except ValueError:
            code = ""
        # PostgREST answers 409 for a unique violation (23505) AND for a foreign-key violation (23503): only the
        # former is a duplicate. A missing parent row is an error the caller must see, never "already stored".
        if code == "23505" or (response.status_code == 409 and not code):
            raise self._duplicate("DUPLICATE_KEY", f"http {response.status_code} {code}".strip())
        if code == "23503":
            raise self._error("FOREIGN_KEY_VIOLATION", f"http {response.status_code} {code}")
        unavailable = response.status_code in (429, 502, 503, 504) or response.status_code >= 500
        raise self._error("DATABASE_UNAVAILABLE" if unavailable else "DATABASE_ERROR",
                          f"http {response.status_code} {code}".strip())
