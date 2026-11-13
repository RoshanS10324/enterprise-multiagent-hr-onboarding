"""Connect UC-04 (and only UC-04) to Supabase.

`build_agent()` returns a normal `Uc04Agent` whose parent-case lookup, audit events and run records go to the Supabase tables, and (when
`state_mode="supabase"`) whose own case state is stored in the UC-04-owned tables. Nothing else is connected: Common, UC-01/02/03/05 are not
imported or changed.

Settings come from the process environment, or from the repository's `.env` file for any name not already set. Values are never printed.
State mode is explicit: "memory" (default) or "supabase". If "supabase" is requested and the UC-04 tables are missing, it raises; it never
falls back silently.
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
UC04 = HERE.parents[1]  # UC04_Equipment_Access_Integration_Ready
REPO = UC04.parent  # holds the shared .env
for p in (str(UC04 / "src"), str(UC04), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

STATE_TABLES = ("uc04_case_state", "uc04_replay_results")
SHARED_TABLES = ("onboarding_cases", "client_configs", "audit_events", "agent_runs")


class NotReady(RuntimeError):
    pass


def load_settings(env_file: Path | None = None) -> dict[str, str]:
    """Environment first, then the repository .env for names that are not set. Returns only the three names UC-04's connector uses."""
    values: dict[str, str] = {}
    path = env_file or (REPO / ".env")
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    wanted = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SCHEMA")
    return {k: os.environ.get(k) or values.get(k, "") for k in wanted}


def make_client(settings: dict[str, str] | None = None):
    from postgrest_table_client import PostgrestTableClient

    s = settings or load_settings()
    if not s.get("SUPABASE_URL") or not s.get("SUPABASE_SERVICE_ROLE_KEY"):
        raise NotReady("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are not configured")
    return PostgrestTableClient(s["SUPABASE_URL"], s["SUPABASE_SERVICE_ROLE_KEY"], s.get("SUPABASE_SCHEMA") or "public")


def readiness(client) -> dict[str, bool]:
    """Read-only: which tables the API exposes. No row data is read."""
    return {t: client.table_exists(t) for t in (*SHARED_TABLES, *STATE_TABLES)}


def build_agent(*, state_mode: str = "memory", client=None, identity_profile: str = "PLATFORM", **agent_kwargs):
    """A Uc04Agent wired to Supabase for UC-04 only. The identifier profile is fixed here, before UC-04 is imported (platform uuid case ids)."""
    if state_mode not in ("memory", "supabase"):
        raise ValueError("state_mode must be 'memory' or 'supabase'")
    from guardrails import identity

    identity.configure(identity_profile)
    from sub_agent.agent import Uc04Agent
    from sub_agent.persistence import (DatabaseAgentRunRecorder, DatabaseAuditSink, DatabaseCaseContext, DatabaseCaseRepository,
                                       TableStateBackend)

    client = client or make_client()
    store = None
    if state_mode == "supabase":
        missing = [t for t in STATE_TABLES if not client.table_exists(t)]
        if missing:
            raise NotReady(f"UC-04 state tables are missing: {missing}. Apply docs/proposed_migrations/uc04_state_tables.sql after review.")
        store = DatabaseCaseRepository(TableStateBackend(client))
    return Uc04Agent(cases=DatabaseCaseContext(client), store=store, audit_sink=DatabaseAuditSink(client),
                     run_recorder=DatabaseAgentRunRecorder(client), **agent_kwargs)
