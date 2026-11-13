"""Seed the missing employee_number_counters rows - insert-only, idempotent.

    python scripts/seed_employee_number_counters.py            # dry run: lists what is missing
    python scripts/seed_employee_number_counters.py --apply    # inserts only the missing rows

next_employee_no(p_client_id) allocates numbers with one atomic
UPDATE ... RETURNING on employee_number_counters and refuses a client that
has no counter row (fail closed). The UC-01 gap closure
(docs/schema/proposed/uc01_gaps_complete.sql) seeded a counter for every
client that existed when it ran; a client created afterwards (for example
SYN-UAE-TEST, created 2026-11-04) has none, so UC-01 cannot number its
employees.

This applies the same documented rule - every client in client_configs gets
(last_value 0, prefix 'EMP-', padding 6) - to the clients still missing one.
It never updates or deletes: existing counters (and the numbers already
issued from them) are untouched, and a concurrent insert is ignored
(ON CONFLICT DO NOTHING). The SQL equivalent is
supabase/migrations/20261104170000_seed_missing_employee_number_counters.sql.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

UC01 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(UC01 / "src"))

#: The rule uc01_gaps_complete.sql applied to every existing client.
DEFAULT_COUNTER = {"last_value": 0, "prefix": "EMP-", "padding": 6}


def missing_counters(client) -> list[str]:
    clients = {r["client_id"] for r in client.select("client_configs", columns="client_id")}
    seeded = {r["client_id"] for r in client.select("employee_number_counters", columns="client_id")}
    return sorted(clients - seeded)


def seed(client, *, apply: bool, only: list[str] | None = None) -> dict:
    missing = missing_counters(client)
    if only:
        unknown = sorted(set(only) - set(missing))
        missing = [c for c in missing if c in only]
    else:
        unknown = []
    inserted: list[str] = []
    if apply and missing:
        client.insert("employee_number_counters", [{"client_id": c, **DEFAULT_COUNTER} for c in missing],
                      on_conflict_ignore=True)  # primary key client_id: never overwrites
        seeded = {r["client_id"] for r in client.select("employee_number_counters", columns="client_id")}
        inserted = [c for c in missing if c in seeded]
    return {"missing": missing, "inserted": inserted, "applied": apply,
            "not_missing_or_unknown": unknown, "row": DEFAULT_COUNTER}


def _client():
    from sub_agent.adapters.supabase import PostgrestClient
    from sub_agent.api.settings import Settings

    settings = Settings.from_env()
    if not settings.persistence_enabled:
        raise SystemExit("SUPABASE_URL is not set")
    return PostgrestClient(settings.supabase_url, settings.supabase_service_role_key,
                           schema=settings.supabase_schema, timeout=settings.supabase_timeout_s)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="insert the missing rows (default: dry run)")
    parser.add_argument("--client", action="append", help="limit to these client ids (repeatable)")
    args = parser.parse_args(argv)
    result = seed(_client(), apply=args.apply, only=args.client)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
