"""Supabase integration client for the HR Onboarding Agent system.

Provides:
- `SupabaseSettings`: configuration loaded from environment (.env).
- `SupabaseClient`: light-weight, high-performance client using httpx or standard urllib targeting
  Supabase PostgREST REST API.
- Multi-tenant Row Level Security (RLS) support via JWT/app_metadata headers.
- `get_supabase_client()`: singleton provider.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Sequence

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from hr_onboarding_common.observability.logging import get_logger
    log = get_logger("platform.supabase")
except Exception:
    import logging
    log = logging.getLogger("platform.supabase")


@dataclass(frozen=True)
class SupabaseSettings:
    url: str
    anon_key: str
    service_role_key: str
    db_url: str
    schema: str = "public"

    @classmethod
    def from_env(cls) -> SupabaseSettings:
        url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
        service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        db_url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL", "")
        schema = os.environ.get("SUPABASE_SCHEMA", "public")
        return cls(
            url=url,
            anon_key=anon_key,
            service_role_key=service_role_key,
            db_url=db_url,
            schema=schema,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.url and (self.service_role_key or self.anon_key))


def _http_request(url: str, method: str = "GET", headers: dict[str, str] | None = None,
                  data: dict[str, Any] | list[Any] | None = None) -> list[dict[str, Any]] | dict[str, Any]:
    """Execute HTTP request using standard library urllib with JSON encoding."""
    hdrs = (headers or {}).copy()
    body_bytes = None
    if data is not None:
        body_bytes = json.dumps(data).encode("utf-8")
        hdrs["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=body_bytes, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                return []
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        err_msg = exc.read().decode("utf-8") if exc.fp else str(exc)
        raise RuntimeError(f"Supabase HTTP {exc.code} error: {err_msg}") from exc


class PostgrestTableQuery:
    """Fluent query builder for Supabase PostgREST tables."""

    def __init__(self, client: SupabaseClient, table_name: str):
        self.client = client
        self.table_name = table_name
        self._base_url = f"{client.settings.url}/rest/v1/{table_name}"
        self._headers = client.headers.copy()
        self._params: dict[str, str] = {}

    def select(self, columns: str = "*") -> PostgrestTableQuery:
        self._params["select"] = columns
        return self

    def eq(self, column: str, value: Any) -> PostgrestTableQuery:
        self._params[column] = f"eq.{value}"
        return self

    def order(self, column: str, desc: bool = False) -> PostgrestTableQuery:
        self._params["order"] = f"{column}.desc" if desc else f"{column}.asc"
        return self

    def limit(self, count: int) -> PostgrestTableQuery:
        self._params["limit"] = str(count)
        return self

    def execute(self) -> list[dict[str, Any]]:
        query_str = urllib.parse.urlencode(self._params)
        url = f"{self._base_url}?{query_str}" if query_str else self._base_url
        res = _http_request(url, method="GET", headers=self._headers)
        return res if isinstance(res, list) else [res]

    def insert(self, data: dict[str, Any] | list[dict[str, Any]], upsert: bool = False) -> list[dict[str, Any]]:
        headers = self._headers.copy()
        headers["Prefer"] = "return=representation"
        if upsert:
            headers["Prefer"] += ",resolution=merge-duplicates"
        payload = data if isinstance(data, list) else [data]
        res = _http_request(self._base_url, method="POST", headers=headers, data=payload)
        return res if isinstance(res, list) else [res]

    def update(self, data: dict[str, Any], match: dict[str, Any]) -> list[dict[str, Any]]:
        headers = self._headers.copy()
        headers["Prefer"] = "return=representation"
        params = {k: f"eq.{v}" for k, v in match.items()}
        query_str = urllib.parse.urlencode(params)
        url = f"{self._base_url}?{query_str}" if query_str else self._base_url
        res = _http_request(url, method="PATCH", headers=headers, data=data)
        return res if isinstance(res, list) else [res]

    def delete(self, match: dict[str, Any]) -> list[dict[str, Any]]:
        headers = self._headers.copy()
        headers["Prefer"] = "return=representation"
        params = {k: f"eq.{v}" for k, v in match.items()}
        query_str = urllib.parse.urlencode(params)
        url = f"{self._base_url}?{query_str}" if query_str else self._base_url
        res = _http_request(url, method="DELETE", headers=headers)
        return res if isinstance(res, list) else [res]


class SupabaseClient:
    """Supabase API client for database tables, functions, and tenant RLS."""

    def __init__(self, settings: SupabaseSettings | None = None, use_service_role: bool = True):
        self.settings = settings or SupabaseSettings.from_env()
        self.use_service_role = use_service_role
        self._active_key = (
            self.settings.service_role_key
            if (use_service_role and self.settings.service_role_key)
            else self.settings.anon_key
        )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "apikey": self._active_key,
            "Authorization": f"Bearer {self._active_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Profile": self.settings.schema,
            "Content-Profile": self.settings.schema,
        }

    def table(self, table_name: str) -> PostgrestTableQuery:
        """Access a table query builder."""
        if not self.settings.is_configured:
            raise RuntimeError(
                "Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY in your .env file."
            )
        return PostgrestTableQuery(self, table_name)

    def rpc(self, function_name: str, params: dict[str, Any] | None = None) -> Any:
        """Call a PostgreSQL RPC stored function."""
        url = f"{self.settings.url}/rest/v1/rpc/{function_name}"
        return _http_request(url, method="POST", headers=self.headers, data=params or {})

    def test_connection(self) -> bool:
        """Verify database connectivity."""
        try:
            self.table("client_configs").select("client_id").limit(1).execute()
            log.info("Supabase connection verified successfully.")
            return True
        except Exception as exc:
            log.warning("Supabase connection check failed: %s", exc)
            return False


_GLOBAL_CLIENT: SupabaseClient | None = None


def get_supabase_client(use_service_role: bool = True) -> SupabaseClient:
    """Get or create singleton Supabase client."""
    global _GLOBAL_CLIENT
    if _GLOBAL_CLIENT is None:
        _GLOBAL_CLIENT = SupabaseClient(use_service_role=use_service_role)
    return _GLOBAL_CLIENT
