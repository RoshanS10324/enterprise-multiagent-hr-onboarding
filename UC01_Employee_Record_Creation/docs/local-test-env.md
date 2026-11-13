# Local test environment (Supabase CLI)

Runs real Postgres **and real PostgREST** in Docker on your machine, so
the Supabase adapters are exercised exactly as they would be in
production — without touching the shared `Onboarding_Agent` project.

> `Onboarding_Agent` is a **shared live database**. Other use cases write
> to it concurrently (row counts moved between reads during
> investigation). Integration tests must never run against it.

## Prerequisites — not currently installed on this machine

Both must be installed before any of this runs:

| Tool | Check | Install |
|---|---|---|
| Docker Desktop | `docker --version` | <https://docs.docker.com/desktop/> |
| Supabase CLI | `supabase --version` | `scoop install supabase` / `npm i -g supabase` |

## Start

```bash
cd UC01_Employee_Record_Creation
supabase start          # applies supabase/migrations/ automatically
supabase status         # prints the local URL and keys
```

`supabase/migrations/20261103120000_uc01_local_baseline.sql` recreates
the UC-01-relevant subset of the live schema, including the parts that
currently **block** us (the nine `NOT NULL` columns on
`onboarding_cases`, and `audit_events` with no hash-chain columns). That
is deliberate: the local environment must reproduce the blockers, or a
green test there would mean nothing.

To test the proposed fixes, apply the additive changes on top:

```bash
psql "$(supabase status -o env | grep DB_URL | cut -d= -f2-)" \
     -f docs/schema/proposed/d4_d5_additive.sql
```

## Point UC-01 at the local stack

Use real environment variables rather than editing `.env` — deployment
env vars take precedence over both `.env` files by design:

```bash
export SUPABASE_URL="http://127.0.0.1:54321"      # from `supabase status`
export SUPABASE_SERVICE_ROLE_KEY="<local service_role key>"
export UC01_FIELD_ENCRYPTION_KEY="$(uv run python -c \
  'from sub_agent.crypto import generate_key_b64; print(generate_key_b64())')"
```

The local service-role key is a well-known development value printed by
`supabase status`. It is not a secret and must never be reused anywhere
else.

> `SUPABASE_URL` must start with `https://` in `Settings`. The local
> stack serves plain HTTP, so that check needs a localhost exemption
> before this works end to end — see "Known gaps" below.

## Run the tests

```bash
uv run pytest                                   # hermetic; unchanged
UC01_LIVE_DB_TESTS=1 uv run pytest tests/test_supabase_live.py
```

With the env vars above exported, the "live" suite targets the **local**
stack, not the cloud project — it reads `SUPABASE_URL` like everything
else.

## What the integration test must prove (D8)

Not yet written — it cannot pass until D1–D5 are decided. It must show:

1. **Create** — `POST /internal/onboarding-requests` produces a case,
   form, person record and audit entries.
2. **Restart** — a brand-new process (not just a new repository object)
   retrieves the same case.
3. **Audit integrity** — `/audit/integrity` verifies the chain, and a
   tampered row is detected.
4. **Concurrency** — N parallel allocations yield N distinct employee
   numbers.
5. **No partial writes** — an injected mid-operation failure leaves no
   orphan case, form or person record.

Only after 1–5 pass should UC-01 be described as end-to-end persistent.

## Known gaps in this scaffolding

- **Docker and the Supabase CLI are not installed here**, so none of the
  above has been executed. The SQL and config are unverified by running.
- The baseline is reconstructed from PostgREST metadata (names, types,
  nullability, PKs, FKs). PostgREST does not expose CHECK constraints,
  indexes, triggers, defaults or RLS, so those are **not** faithful.
  Replace it with a real `pg_dump --schema-only` when one is available.
- `Settings` rejects non-`https://` URLs, which blocks a plain-HTTP
  localhost stack. Needs a narrow exemption for `127.0.0.1`/`localhost`
  (deliberately not added yet — it loosens a security check, so it
  should be a reviewed change rather than a side effect of scaffolding).
