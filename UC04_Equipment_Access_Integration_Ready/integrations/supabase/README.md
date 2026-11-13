# UC-04 Supabase integration (`integrations/supabase/`)

Connects **UC-04 only** to Supabase. It lives inside the UC-04 folder, so it travels with UC-04 and nothing is added at the repository root. It does not import, change or connect Common or UC-01/02/03/05.

The UC-04 core (`src/`) must not contain an HTTP client or read environment variables (its packaging tests enforce that), so the connection is kept in this folder, the only place network code is allowed, and is injected into UC-04's repositories.

| File | Purpose |
|---|---|
| `postgrest_table_client.py` | `TableClient` over the Supabase REST API: select, insert, upsert, conditional update. Always filtered; **no delete**; errors carry a status and code only, never the key, URL or row data. |
| `connect.py` | `build_agent(state_mode="memory" \| "supabase")` returns a normal `Uc04Agent` wired to Supabase (parent case lookup, audit events, run records, and optionally UC-04's own state). Settings come from the environment, then the repository `.env`. |
| `check_connection.py` | Read-only readiness check: which tables the API exposes. |
| `live_smoke_test.py` | Writes SYNTHETIC rows (`UC04TEST-<id>`) through the connector. Needs `--yes-write-synthetic-rows`. |
| `test_connector.py` | 12 offline tests (no network, no credentials). |

## Use
```
..\..\..\.venv\Scripts\python.exe check_connection.py
..\..\..\.venv\Scripts\python.exe -m pytest -p no:cacheprovider test_connector.py
```
```python
import connect
agent = connect.build_agent()                       # parent case, audit, run records on Supabase; UC-04 state in memory
agent = connect.build_agent(state_mode="supabase")  # also UC-04 state on Supabase (needs the two uc04_* tables)
```

## What is connected today
| Part | Status |
|---|---|
| Parent case lookup (`onboarding_cases`) | connected, verified live |
| Audit events (`audit_events`) | connected, verified live (append-only: rows stay) |
| Agent run records (`agent_runs`) | connected, verified live |
| UC-04's own state (`uc04_case_state`, `uc04_replay_results`) | code ready and tested offline; **tables not created**, so `state_mode="supabase"` refuses to start until the reviewed migration (`UC04_Equipment_Access_Integration_Ready/docs/proposed_migrations/uc04_state_tables.sql`) is applied |

Moving to a production database later means pointing the settings at that project and applying the same migration there.
