# Isolated local end-to-end test plan

**Status: not executed.** Neither Docker nor the Supabase CLI is
installed, and neither was installed during this work. Every item below
is a plan, not a result. Setup: [local-test-env.md](local-test-env.md).

Until this plan passes, the integration stays **incomplete** and no
database-level claim should be made about any control.

## Prerequisites (all blocking)

| # | Prerequisite | Owner | Status |
|---|---|---|---|
| P1 | Docker Desktop + Supabase CLI installed | engineer | **not installed** |
| P2 | `localhost`/`127.0.0.1` exemption for the `https://` check in `Settings` | engineer (reviewed) | not done |
| P3 | D1–D3 answered, or `NOT NULL` relaxed **locally only** | HR data owner | **open** |
| P4 | D4 settled: shared `audit_events` vs dedicated `uc01_audit_log` | DBA + security | **open** |
| P5 | D6 settled: psycopg connection (the chain needs a transaction) | DBA | **open** |
| P6 | Counter rows seeded for every test client | engineer | in baseline |

P3–P5 are decisions, not tasks. The plan cannot run around them: without
D4/D6 the audit log still refuses, and every flow writes audit first.

## Phase 0 — apply SQL

| T | Test | Pass criterion |
|---|---|---|
| T0.1 | `supabase start`, baseline migration applies | no errors; 13 tables exist |
| T0.2 | `d4_d5_additive.sql` applies on top | columns, index, function exist |
| T0.3 | Draft DDL compiles at all | **first real syntax check — nothing to date has executed this SQL** |

## Phase 1 — immutability trigger

Each as a direct `UPDATE`, bypassing the application, since the trigger
is the control being tested.

| T | Test | Expect |
|---|---|---|
| T1.1 | `SET client_id` on APPROVED row | raise (F2) |
| T1.2 | `SET client_id` on DRAFT row | raise (guard precedes branching) |
| T1.3 | `SET form_id` | raise — it is the AES-GCM AAD |
| T1.4 | `SET version` | raise |
| T1.5 | `SET client_site_id` / `change_reason` on APPROVED | raise (F9) |
| T1.6 | `SET status='APPROVED', salary_…=…` from DRAFT, one statement | raise (F15) |
| T1.7 | link `case_id` to another client's case | raise (F16) |
| T1.8 | legal `DRAFT→AWAITING→APPROVED→SUPERSEDED` | succeed |
| T1.9 | `DRAFT→APPROVED` directly (`approve()` allows it) | succeed |
| T1.10 | `SUPERSEDED→anything`, `REJECTED→anything` | raise |
| T1.11 | one-time `case_id` backfill on APPROVED row | succeed |
| T1.12 | second `case_id` change | raise |
| T1.13 | **INSERT** a row directly as SUPERSEDED | **currently succeeds — F10 open** |

## Phase 2 — audit append-only

| T | Test | Expect |
|---|---|---|
| T2.1 | `UPDATE` an audit row | raise |
| T2.2 | `DELETE` an audit row | raise |
| T2.3 | **`TRUNCATE`** the audit table | raise (F3 — the row triggers do not fire here) |
| T2.4 | as table owner, `ALTER TABLE … DISABLE TRIGGER`, then mutate | **succeeds — documents the owner bypass; proves `uc01_app` must not own the table** |

## Phase 3 — tenant isolation (RLS)

Connect as `uc01_app` (never `service_role`), with
`SET LOCAL app.client_ids`.

| T | Test | Expect |
|---|---|---|
| T3.1 | read another tenant's case/form/person row | 0 rows |
| T3.2 | insert with a `client_id` outside the session scope | denied by `WITH CHECK` |
| T3.3 | **no** `SET LOCAL` at all | 0 rows — fail closed |
| T3.4 | `SET` instead of `SET LOCAL`, then reuse the pooled connection | **must not** leak scope to the next transaction |
| T3.5 | every table in `ADAPTER_OPERATIONS` is reachable for its own tenant | all adapter ops succeed (F17) |

## Phase 4 — employee-number allocation

| T | Test | Expect |
|---|---|---|
| T4.1 | 50 concurrent `next_employee_no()` for one client | 50 **distinct** numbers, no gaps |
| T4.2 | two clients concurrently | no cross-contention, independent sequences |
| T4.3 | client outside session scope | `insufficient_privilege` (F8) |
| T4.4 | unseeded client | `no_data_found`, **not** NULL (F14) |
| T4.5 | `uc01_app` with direct table UPDATE revoked | allocation still works via SECURITY DEFINER |
| T4.6 | `FORCE RLS` on the counter table | **expected to FAIL — the F18 conflict; resolve before applying** |

## Phase 5 — case creation and restart

Blocked on P3.

| T | Test | Expect |
|---|---|---|
| T5.1 | `POST /internal/onboarding-requests` | case + form + person + audit rows |
| T5.2 | **kill the process**, start a new one, `GET /cases/{id}/status` | identical case returned |
| T5.3 | salary decrypts after restart | plaintext matches; AAD still bound to `form_id` |
| T5.4 | related records consistent | form linked, person `OPEN_CASE`, outbox event staged |
| T5.5 | duplicate email, two concurrent requests | one case, one `DUPLICATE_OPEN_CASE`, unique index fires |

## Phase 6 — rollback

| T | Test | Expect |
|---|---|---|
| T6.1 | inject failure after `cases` insert, before `person_records` | **no** orphan case, form unlinked, no audit rows |
| T6.2 | inject failure after audit insert | sequence **not** consumed (lock released on rollback) |
| T6.3 | kill the process mid-transaction | same as T6.1 — the database rolls back |

## Phase 7 — audit chain

| T | Test | Expect |
|---|---|---|
| T7.1 | `/audit/integrity` after a clean run | zero violations |
| T7.2 | owner-level edit of one audit row's `after` | `DIGEST_MISMATCH` |
| T7.3 | owner-level delete of a middle row | `SEQUENCE_BREAK` + `PREV_HASH_MISMATCH` |
| T7.4 | delete the **last** row | **no violation — documents the undetectable tail truncation** |
| T7.5 | 20 concurrent appends | contiguous 0..N-1, no gaps, no duplicates |
| T7.6 | verification under RLS as `uc01_app` | **expected to FAIL — the §5 conflict; AUDITOR needs an exempt path** |

T2.4, T4.6, T7.4 and T7.6 are expected to fail. They are written to
**document** limits and unresolved conflicts, not to be made green by
weakening a control.

## Exit criteria

All of Phases 0–3 green; Phase 4 except T4.6; Phases 5–7 except T7.4 and
T7.6; and T2.4/T4.6/T7.6 each resolved by an owner decision rather than
by a code change made to pass a test.
