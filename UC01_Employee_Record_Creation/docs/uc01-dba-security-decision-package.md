# UC-01 Supabase integration — DBA / security decision package

**For:** the DBA and the security owner. HR decisions are listed in §6
for routing, not for this audience to answer.

**Date:** 2026-11-04 · **Status: integration INCOMPLETE.**

Nothing in this package has been executed against a database. No
connection was made to the shared `Onboarding_Agent` project, no
migration applied, no production configuration changed.

## Evidence levels used throughout

| Label | Meaning |
|---|---|
| **[CR]** code-reviewed | Read from source. Reliable about what the code says. |
| **[ST]** structurally tested | A test parses the SQL text and asserts a property. Catches omissions. Does **not** prove the SQL compiles, a trigger fires, or a policy denies. |
| **[DV]** database-verified | Executed against a real Postgres. **Nothing in this package is [DV].** |

**Current test baseline: 791 passed, 15 skipped, coverage 93.80%**
(gate 93.0%). All green tests are [CR] or [ST].

Sources: `docs/schema/review-2026-11-03.md`,
`docs/schema/audit-chain-design.md`, `docs/local-test-plan.md`,
`docs/schema/proposed/d4_d5_additive.sql`,
`docs/schema/live-schema-comparison.md`.

---

## 1. D4 / D6 — connection method and audit sequencing

These are one decision, not two: the audit chain's requirements
determine the connection method. **[CR]**

`verify_chain` asserts `sequence == index` over the complete log
([audit_integrity.py:196](../src/sub_agent/audit_integrity.py#L196)), so
the chain is global, contiguous and zero-based, and **a gap is a
permanent verification failure**. `prev_hash` is a SHA-256 over a
canonical JSON dump of the Pydantic model — the database cannot derive
it. An append is therefore necessarily:

```
read tail -> compute digest in Python -> insert
```

**Over PostgREST each step is its own transaction.** A crash between
read and insert leaves a consumed-but-absent sequence — a permanent
gap. **This is why UC-01 persists nothing today: every flow writes
audit first.** **[CR]**

### Option A — direct PostgreSQL (psycopg), restricted role

- *Atomic case creation:* one `BEGIN`/`COMMIT` across the 7 tables
  `open_onboarding_case` touches.
- *Audit sequencing:* `pg_advisory_xact_lock` → read tail → compute →
  insert, one transaction. The lock releases on **rollback** too, so a
  failed append consumes no number; gaplessness is structural, not
  dependent on retry logic.
- *Complexity:* needs a connection string, a `uc01_app` role, session-mode
  pooling (`:5432` — the `:6543` transaction pooler cannot hold advisory
  locks), and **RLS redesign**: policies must read a session GUC the app
  sets, because `request.jwt.claims` is PostgREST-only.
- *Security:* best. Connects as a restricted non-`BYPASSRLS` role;
  `service_role` never used; business logic stays in Python under the
  791-test suite.
- *Cost:* one global advisory lock serializes every audit append, and
  case creation writes ~8. **This is the throughput ceiling.**

### Option B — PostgREST + PL/pgSQL function

- *Atomic case creation:* yes — a function body is one implicit
  transaction.
- *Audit sequencing:* only if the digest moves into SQL. Re-implementing
  `entry_digest`'s canonical JSON in PL/pgSQL creates **two
  implementations of one integrity control** that must produce
  byte-identical output forever. If they ever diverge, every chain
  verification fails and the cause is non-obvious.
- *Complexity:* start-gate, duplicate and variant rules migrate into SQL,
  outside the Python test suite and duplicated against the in-memory
  path that must still work.
- *Security:* keeps `service_role` (BYPASSRLS) as the connection
  identity unless a separate PostgREST role is introduced; RLS is
  bypassed in practice.
- *Cost:* lower lock contention; much higher correctness risk.

### Recommendation

**Option A.** The audit digest cannot be safely duplicated in SQL, and
that single fact decides it. If A is refused, B must be scoped to a
*thin insert-orchestrator* taking already-decided values as parameters,
with **no rule logic and no digest computation** in SQL — and D4 then
has no correct answer, so the audit log must keep refusing.

**Approver: DBA (connection + role), with security sign-off on the
connection identity.**

---

## 2. F18 — `FORCE ROW LEVEL SECURITY` vs `SECURITY DEFINER`

`FORCE ROW LEVEL SECURITY` applies policies to the **table owner** as
well. A `SECURITY DEFINER` function runs as **its owner**. If the owner
of `next_employee_no()` also owns `employee_number_counters` under
FORCE, the function's `UPDATE` is blocked by the absence of an UPDATE
policy and **allocation fails closed**. **[CR]**

Test T4.6 in the local plan is written to fail until this is resolved.

| Option | Consequence |
|---|---|
| **(a) `ENABLE` but not `FORCE`** on the counter table | Owner bypasses RLS; the function works. Tenant scoping for allocation then rests on the function's own `app.client_ids` check, which exists **[CR]**. Smallest change. |
| (b) `FORCE` + an UPDATE policy admitting the function owner | Keeps FORCE uniform, but the policy must admit a principal that legitimately writes every tenant's row — a policy that is nearly `true` for that role, which is close to defeating the point. |
| (c) Drop `SECURITY DEFINER`, grant `uc01_app` direct UPDATE | Simplest, but removes the privilege boundary: any SQL path in the app can then mint numbers, not just the function. |

**Recommendation: (a)**, plus three conditions that are not optional:
the function owner **must not be a superuser** (it runs with owner
rights); `search_path` stays pinned (`SET search_path = pg_catalog,
public` — present **[ST]**); and `GRANT EXECUTE … TO uc01_app` must
actually be issued — it is currently commented out, so **nobody can
execute the function today** **[CR]**.

**Approver: DBA, with security sign-off on the ownership split.**

---

## 3. Audit verification under least privilege

Conflict **[CR]**: `verify_chain` needs the **complete** log, but the
tenant-scoped read policy means `uc01_app` can never see it — so
verification would report violations on a perfectly intact chain.
Removing the policy would expose every tenant's audit data to ordinary
application users. Local test T7.6 is written to fail until resolved.

| Option | Consequence |
|---|---|
| **(a) `SECURITY DEFINER` verification function returning only the verdict** | Returns `(entries_checked, violations[])` — never the rows. Ordinary users keep tenant-scoped reads; nobody gains bulk audit read. Verification logic stays in SQL or calls out with no row egress. |
| (b) A dedicated `uc01_auditor` role exempt from the policy | Simple, but creates a standing principal that can read every tenant's audit log. Needs its own access control and monitoring. |
| (c) Relax the read policy | Rejected — exposes all tenants to ordinary users. |

**Recommendation: (a).** It satisfies "verify the complete chain"
without anyone gaining the ability to *read* the complete chain. Note
the residual: the function's owner can still read everything, so
ownership must sit with the same non-superuser DDL role, not with
`uc01_app`.

**Approver: security.**

---

## 4. Audit truncation — external checkpoint requirement

`verify_chain`'s own docstring states the limit **[CR]**: dropping
entries off the **tail** leaves a shorter but internally valid chain.
Editing or deleting a middle row is detectable; truncation is not.

**Threat model.** The adversary is an actor with table-owner or
superuser rights — someone who can `DISABLE TRIGGER`, `TRUNCATE`, or
`DELETE` recent rows to erase a record of their own actions. Triggers do
not constrain them, and a high-water mark stored in the same database is
deleted by the same actor. Not in scope: an attacker who can rewrite
every row *and* the external mark.

**Requirement.** After commit (or on a short timer), publish
`(max_sequence, entry_hash)` to an **external append-only sink the
application cannot rewrite** — object storage with versioning/object-lock,
or a log stream in a separate account. Verification compares the
in-database head against that mark; a lower in-database sequence proves
truncation.

| Option | Consequence |
|---|---|
| **(a) Object storage with object-lock, periodic checkpoint** | Detects truncation down to the checkpoint interval. Modest cost. Interval is the exposure window. |
| (b) Checkpoint on every commit | Smallest window; adds an external write to the append path, which is already the throughput ceiling (§1). |
| (c) Accept undetectable truncation | Zero cost. Then "append-only" must not be claimed to auditors without this caveat. |

**Recommendation: (a)**, interval set by the security owner. This is the
only thing that makes "append-only" a real claim rather than a
detect-if-you're-lucky one.

**Approver: security (and infrastructure for the sink).**

---

## 5. RLS — per-table operations and scoping

Operations below are taken from the adapter call sites
(`adapters/supabase/repositories.py`), not assumed **[CR]**. All 13
tables now carry explicit `ENABLE` + policies **[ST]**; previously only 4
did, and the other 9 had **no row-level isolation at all** — prose, not
policies (review F17).

| Table | Ops required | Scoping rule | Notes |
|---|---|---|---|
| `cases` | S / I / U | `client_id` in scope; `HR_OFFICER` also `hr_officer_id = user` | |
| `onboarding_forms` | S / I / U | own `client_id` | **Not** via a join to `cases` — a form legitimately exists with `case_id IS NULL`. Upsert needs I **and** U. |
| `offer_forms` | S / I / U | `client_id` | Upsert — U is required |
| `approvals` | S / I / U | through `cases` | |
| `discrepancies` | S / I / U | through `cases` | |
| `hris_records` | S / I / U | through `cases` | |
| `person_records` | S / I | `client_id` | PII + duplicate detection; no update path exists |
| `pending_starts` | S / I / U | `client_id` | |
| `domain_events_outbox` | S / I | `client_id` | U belongs to a **separate relay role**, not `uc01_app` |
| `audit_log` | S / I only | `case_id IS NULL` or through `cases` | No U/D policy — denial by absence, plus triggers |
| `offers` | S | `client_id` | **Read-only**: adapter refuses writes ([Proposed]/dev-only) |
| `client_holidays` | S | `client_id` | **Read-only** |
| `employee_number_counters` | S | `client_id` | **Read-only** for `uc01_app`; writes only via the function (§2) |

Identity comes from `SET LOCAL app.client_ids` / `app.user_id` /
`app.role`. **`SET LOCAL`, not `SET`** — on a pooled connection, `SET`
leaks one request's tenant scope into the next **[ST]**. Unset ⇒ empty
array ⇒ zero rows (fail closed) **[CR]**.

**Every policy in this table still needs PostgreSQL execution tests** —
none has been run. Phase 3 of the local plan covers them, including
T3.3 (no `SET LOCAL` ⇒ 0 rows) and T3.4 (pooled-connection leak).

**Approver: DBA (apply), security (scoping rules).**

---

## 6. HR blockers — routing only, not for this audience

| ID | Decision | Owner |
|---|---|---|
| D1 | `role_id` source: upstream contract, authoritative `designation → role_id` mapping, or nullable | HR data owner |
| D2 | `manager_id` source — `hr_officer_id` is **not** the reporting manager | HR process owner |
| D3 | `department`, `grade`, `nationality_group`, `location_name`, `project_name`: supplied / derived / nullable, each stated | HR data owner |
| D3b | Rehire reactivation (EC-43) in scope? Needs `reactivates_record_id`, `reactivates_external_ref`, `person_records.hris_external_ref` | HR + lead |

All nine columns are `NOT NULL` with no default and appear **zero
times** in `src/`, `specs/`, `skills/` or the request contract **[CR]**.
No values have been invented. D1–D3 block case creation; they do not
block any decision in §1–§5.

---

## 7. Schema compatibility — adapter vs draft vs live

| # | Mismatch | Impact | Resolution |
|---|---|---|---|
| 1 | Draft `pending_starts` has a composite FK → `offers`; **live has none**, and the adapter inserts pending starts without an offer row | Adapter works live, **fails against the draft** | Drop the FK from the draft, or seed offers first. **DBA** |
| 2 | Draft `client_holidays` lacks `name` / `is_observed`; live has both NOT NULL, and the adapter **filters on `is_observed`** | Adapter works live, **fails against the draft** | Add both columns to the draft. **DBA** |
| 3 | `cases` vs live `onboarding_cases`: 9 NOT NULL columns; `variant_is_uae_resident` vs live **`variant_is_resident`** | Case creation blocked | D1–D3 |
| 4 | `approvals` vs live `approval_requests` — different entity (`gate`, `rationale`, `confidence_score`, `sla_due_at` NOT NULL; no `requested_to`) | Approvals cannot persist | Decision pending |
| 5 | `audit_log` vs live `audit_events` — no chain columns; requires `client_id`/`actor_role`/`outcome`/`detail` | All persistence blocked | D4 |
| 6 | `offers` live requires `candidate_name`/`designation`/`offer_payload` | Adapter refuses | Decision pending |
| 7 | `hris_records.idempotency_key` was `GENERATED ALWAYS` but the adapter writes it | Every HRIS insert would error | **Resolved 2026-11-04** — the live column is `GENERATED ALWAYS` + `UNIQUE` (tested directly); the adapter no longer sends it and the draft generates it. The 2026-11-03 "plain column + `CHECK`" resolution rested on PostgREST metadata, which cannot show generated columns **[ST]** |
| 8 | `hris_sync_records` lacks `comparison_result` / `updates_external_ref`; `person_records` lacks `hris_external_ref` | Adapter refuses rather than dropping data **[CR]** | D3b |

Items 1 and 2 are **draft bugs, not live bugs** — the adapters are
correct against the live schema. Fixing them is cheap and
uncontroversial, but was deliberately left out of this package because
the brief excluded further schema changes.

---

## 8. Local testing prerequisites

| # | Prerequisite | Owner | Status |
|---|---|---|---|
| P1 | Docker Desktop + Supabase CLI | engineer | **not installed** |
| P2 | `localhost` exemption for the `https://` check in `Settings` | engineer (reviewed change) | not done |
| P3 | D1–D3 answered, or `NOT NULL` relaxed **locally only** | HR | open |
| P4 | D4: shared `audit_events` + marker vs dedicated `uc01_audit_log` | DBA + security | open |
| P5 | D6: psycopg connection | DBA | open |
| P6 | Counter rows seeded per client | engineer | in baseline |
| P7 | §7 items 1–2 corrected, or the draft will not apply cleanly | engineer | open |

Steps: `supabase start` (applies the baseline) → apply
`d4_d5_additive.sql` → export `SUPABASE_URL`/keys/encryption key as
real env vars (they take precedence over both `.env` files **[ST]**) →
run `docs/local-test-plan.md` Phases 0–7.

**Phase 0 would be the first time any of this SQL has been executed.**
The baseline is reconstructed from PostgREST metadata — names, types,
nullability, PKs, FKs only. CHECKs, indexes, triggers, defaults and RLS
in the live project remain **unknown**; a `pg_dump --schema-only` is
still the cheapest way to close that gap.

Four local tests are written to **fail** on purpose — T2.4 (owner
bypass), T4.6 (F18), T7.4 (tail truncation), T7.6 (verification under
RLS). They document limits and must be resolved by an owner decision,
not by weakening a control to make them green.

---

## Approval summary

| Decision | Recommended | Approver |
|---|---|---|
| D4 / D6 connection + audit sequencing | Direct psycopg, restricted role | DBA + security |
| F18 counter RLS/ownership | `ENABLE` without `FORCE`; non-superuser owner; issue the `GRANT EXECUTE` | DBA + security |
| Audit verification | `SECURITY DEFINER` function returning the verdict only | Security |
| Truncation checkpoint | External object-lock sink, periodic | Security + infra |
| RLS policies and grants | As enumerated in §5 | DBA + security |
| Schema items §7 (1,2) | Correct the draft | DBA |
| D1–D3b | — | HR |

**The integration is not complete and must not be described as
complete.** No control in this package has been database-verified.
