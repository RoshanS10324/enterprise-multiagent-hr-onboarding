"""Read-only UC-01 Supabase connectivity check.

Pings the project, then SELECTs up to 1 row from every table UC-01 uses.
Writes nothing. Reads SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY /
SUPABASE_SCHEMA from the repo-root .env, then UC01's own .env, then the
process environment.

    uv run python scripts/check_db.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "src"))

from pydantic import SecretStr  # noqa: E402

from sub_agent.adapters.supabase.client import PostgrestClient, SupabaseError  # noqa: E402

TABLES = [
    "onboarding_forms",
    "person_records",
    "intake_discrepancies",
    "hris_sync_records",
    "domain_events_outbox",
    "client_holidays",
    "pending_starts",
    "offer_forms",
]


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main() -> int:
    _load_env(HERE.parent / ".env")
    _load_env(HERE / ".env")
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        print("FAIL: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are not set (repo-root .env).")
        return 2

    schema = os.environ.get("SUPABASE_SCHEMA", "public") or "public"
    failed = False
    with PostgrestClient(url, SecretStr(key), schema=schema) as db:
        try:
            db.ping()
            print(f"OK    connected to {url} (schema={schema})")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  ping: {exc}")
            return 1
        for table in TABLES:
            try:
                rows = db.select(table, limit=1)
                print(f"OK    {table}: readable ({'has data' if rows else 'empty'})")
            except SupabaseError as exc:
                failed = True
                print(f"FAIL  {table}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
