"""Choose UC-03's stores from `UC03_STORE_MODE`, the one place the storage backend is selected.

  memory   (default)  in-memory stores: tests, offline development, no credentials needed.
  supabase            the Supabase-backed stores over the SHARED Common client (SUPABASE_URL and the
  service-role key,
                      read by Common, never by UC-03). A missing setting stops start-up with a
                      message naming the
                      variable; there is NO silent fall back to memory.

The repository-root .env is the configuration source for every entry point (UC-03's API and the
shared orchestrator): build_stores() fills any storage setting the real environment lacks from it.
Importing this module does not touch the database or load any .env file. Common's supabase_client
module calls
load_dotenv() when it is imported, so it is imported only when supabase mode is selected.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODES = ("memory", "supabase")


class StoreConfigurationError(RuntimeError):
    """The storage configuration cannot work. Names variables, never their values."""


@dataclass
class UC03Stores:
    mode: str
    case_store: Any
    audit_store: Any
    result_store: Any = None  # None in memory mode: results are only returned
    document_registry: Any = None


def store_mode(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    mode = env.get("UC03_STORE_MODE", "memory").strip().lower() or "memory"
    if mode not in MODES:
        raise StoreConfigurationError(f"UC03_STORE_MODE must be one of {list(MODES)}")
    return mode


REPO_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
_SUPABASE_VARS = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SCHEMA",
)


def _adopt_repo_env(env_file: Path = REPO_ENV_FILE) -> None:
    """Fill UC-03's storage settings from the repository-root .env, whichever entry point started
    the process (UC-03's own API or the shared orchestrator, whose own .env loading never reads
    that file). Only UC03_STORE_MODE is read first; the SUPABASE_* settings are taken only when
    supabase mode is selected. A value already in the environment always wins, so a deployment or
    a test guard (which sets UC03_STORE_MODE=memory) is never overridden."""
    if not env_file.is_file():
        return
    from dotenv import dotenv_values

    values = dotenv_values(env_file)
    mode = values.get("UC03_STORE_MODE")
    if mode:
        os.environ.setdefault("UC03_STORE_MODE", mode)
    if store_mode() != "supabase":
        return
    for name in _SUPABASE_VARS:
        value = values.get(name)
        if value:
            os.environ.setdefault(name, value)


def build_stores(environ: Mapping[str, str] | None = None, *, tables: Any = None) -> UC03Stores:
    """The stores this process should use. `tables` injects a table client (tests); otherwise
    supabase mode builds one over the shared Common client, once. With no explicit `environ`, the
    repository-root .env supplies any storage setting the real environment does not."""
    if environ is None:
        _adopt_repo_env()
    mode = store_mode(environ)
    if mode == "memory" and tables is None:
        from src.state.store import InMemoryAuditStore, InMemoryCaseStore

        return UC03Stores("memory", InMemoryCaseStore(), InMemoryAuditStore())
    if tables is None:
        tables = _shared_tables()
    from .stores import build_supabase_stores

    case, audit, results, registry = build_supabase_stores(tables)
    return UC03Stores("supabase", case, audit, results, registry)


def _shared_tables() -> Any:
    try:
        from hr_onboarding_common.platform.supabase_client import (
            SupabaseSettings,
            get_supabase_client,
        )
    except ImportError:
        raise StoreConfigurationError(
            "UC03_STORE_MODE=supabase needs the shared hr-onboarding-common package "
            "(install UC-03 with the `gateway` extra)"
        ) from None
    settings = SupabaseSettings.from_env()
    missing = [
        name
        for name, value in (
            ("SUPABASE_URL", settings.url),
            ("SUPABASE_SERVICE_ROLE_KEY", settings.service_role_key),
        )
        if not value
    ]
    if missing:
        raise StoreConfigurationError(
            f"UC03_STORE_MODE=supabase but {', '.join(missing)} is not set"
        )
    from .tables import SharedSupabaseTables

    return SharedSupabaseTables(get_supabase_client(use_service_role=True))


def configure_api(environ: Mapping[str, str] | None = None) -> UC03Stores:
    """Install the selected stores for UC-03's FastAPI app (called once by src/main.py)."""
    from src.state.store import configure_stores

    stores = build_stores(environ)
    configure_stores(stores.case_store, stores.audit_store, stores.result_store)
    return stores
