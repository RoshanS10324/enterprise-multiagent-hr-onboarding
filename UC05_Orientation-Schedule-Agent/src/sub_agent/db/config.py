"""UC05 database configuration, read from the environment and validated at startup.

UC05_DATABASE_MODE
  mock      (default) UC05's existing local repositories: in-memory, or SQLite with HR_AGENT_PERSIST=1.
            Deterministic, no credentials, the mode every unit test runs in.
  supabase  the shared Supabase project, through PostgREST with the service-role key. Never falls
            back to local or synthetic data: a missing variable, a refused key or an unreachable
            database stops startup (or the operation) with a SupabaseConfigurationError /
            PersistenceError.

Variables (supabase mode)
  SUPABASE_URL                https://<project>.supabase.co   (required)
  SUPABASE_SERVICE_ROLE_KEY   backend-only secret              (required)
  SUPABASE_SCHEMA             default "public"
  UC05_SUPABASE_TIMEOUT_S     per-request timeout, default 10
  UC05_SUPABASE_READ_RETRIES  retries of idempotent reads, default 2
  UC05_SUPABASE_VERIFY_SCHEMA 1 (default) checks every table/column UC05 uses at startup

SUPABASE_ANON_KEY is deliberately NOT used: UC05 is backend code acting for many tenants; the anon
key is a browser credential governed by RLS and is not interchangeable with the service-role key.
DATABASE_URL is not used either (see sub_agent/db/postgrest.py for why).

No value read here is ever logged or put in an error message -- only variable NAMES are.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import SecretStr


class DatabaseMode(StrEnum):
    MOCK = "mock"
    SUPABASE = "supabase"


class SupabaseConfigurationError(RuntimeError):
    """A required database setting is missing or invalid. The message names variables, never values."""


@dataclass(frozen=True)
class SupabaseSettings:
    url: str
    service_role_key: SecretStr
    schema: str = "public"
    timeout_s: float = 10.0
    read_retries: int = 2
    verify_schema: bool = True

    def __repr__(self) -> str:  # the key never appears, even by accident
        return f"SupabaseSettings(url_host={urlparse(self.url).hostname!r}, schema={self.schema!r})"


def database_mode(environ: Mapping[str, str] | None = None) -> DatabaseMode:
    env = os.environ if environ is None else environ
    raw = (env.get("UC05_DATABASE_MODE") or DatabaseMode.MOCK.value).strip().lower()
    try:
        return DatabaseMode(raw)
    except ValueError:
        raise SupabaseConfigurationError(
            f"UC05_DATABASE_MODE must be one of {[m.value for m in DatabaseMode]}") from None


def _number(env: Mapping[str, str], name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SupabaseConfigurationError(f"{name} must be a number") from None
    if not minimum <= value <= maximum:
        raise SupabaseConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def load_supabase_settings(environ: Mapping[str, str] | None = None) -> SupabaseSettings:
    """Validates the supabase-mode configuration. Raises SupabaseConfigurationError naming every
    missing or invalid variable at once."""
    env = os.environ if environ is None else environ
    problems: list[str] = []
    url = (env.get("SUPABASE_URL") or "").strip()
    key = (env.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    schema = (env.get("SUPABASE_SCHEMA") or "public").strip()
    if not url:
        problems.append("SUPABASE_URL is not set")
    else:
        parsed = urlparse(url)
        if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
            problems.append("SUPABASE_URL must be an https:// URL (http is accepted for localhost only)")
        if not parsed.hostname:
            problems.append("SUPABASE_URL is not a valid URL")
        if parsed.path not in ("", "/"):
            problems.append("SUPABASE_URL must be the project URL without a path (no /rest/v1)")
    if not key:
        problems.append("SUPABASE_SERVICE_ROLE_KEY is not set")
    elif key.startswith("<") or key.lower() in {"changeme", "your-service-role-key", "placeholder"}:
        problems.append("SUPABASE_SERVICE_ROLE_KEY still holds a placeholder value")
    elif key.startswith("eyJ") and key.count(".") != 2:
        problems.append("SUPABASE_SERVICE_ROLE_KEY looks truncated: a legacy service_role key is a complete JWT "
                        "(three dot-separated parts); copy the whole key from Project Settings > API Keys")
    elif key.startswith("sb_publishable_"):
        problems.append("SUPABASE_SERVICE_ROLE_KEY holds a publishable (browser) key; the backend needs the secret "
                        "or service_role key")
    if key and key == (env.get("SUPABASE_ANON_KEY") or "").strip():
        problems.append("SUPABASE_SERVICE_ROLE_KEY equals SUPABASE_ANON_KEY; the backend needs the service-role key")
    if not schema.replace("_", "").isalnum():
        problems.append("SUPABASE_SCHEMA must be a plain schema name")
    try:
        timeout = _number(env, "UC05_SUPABASE_TIMEOUT_S", 10.0, minimum=1, maximum=120)
        retries = int(_number(env, "UC05_SUPABASE_READ_RETRIES", 2, minimum=0, maximum=5))
    except SupabaseConfigurationError as exc:
        problems.append(str(exc))
        timeout, retries = 10.0, 2
    verify = (env.get("UC05_SUPABASE_VERIFY_SCHEMA") or "1").strip() != "0"
    if problems:
        raise SupabaseConfigurationError("UC05_DATABASE_MODE=supabase is misconfigured: " + "; ".join(problems))
    return SupabaseSettings(url=url.rstrip("/"), service_role_key=SecretStr(key), schema=schema, timeout_s=timeout,
                            read_retries=retries, verify_schema=verify)
