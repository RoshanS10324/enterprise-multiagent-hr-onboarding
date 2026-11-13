# UC-01 Supabase integration — decisions required

**Status:** connectivity, encryption and 8 of 14 repositories are done and
tested. End-to-end case creation is blocked on the decisions below.
**Nothing here has been applied to any database.**

Each decision names one owner. Please reply with the **Answer** line
filled in — that is all that is needed to unblock.

Evidence: `docs/schema/live-schema-comparison.md` (verified against the
live `Onboarding_Agent` schema, `eu-north-1`, 2026-11-03).

---

## For the HR data / process owner — D1, D2, D3

`onboarding_cases` declares nine columns `NOT NULL` with no default.
UC-01 has a value for none of them: each appears **zero times** across
`src/`, `specs/`, `skills/`, and the `POST /internal/onboarding-requests`
request body. We will not invent values, insert empty strings, or drop
the constraints.

> **RESOLVED 2026-11-04 — D1, D2, D3b.**
> * **D1 / D2: `role_id` and `manager_id` are no longer required** for
>   UC-01 onboarding-case creation. No application change was needed:
>   UC-01 never produced either value. The remaining work is the DB
>   constraint — `ALTER COLUMN … DROP NOT NULL` for those two columns
>   only, in `docs/schema/proposed/d4_d5_additive.sql`, pending DBA
>   confirmation that no other writer depends on them.
> * **D3b: rehire support is in scope.** Detection and handling already
>   existed and are tested; the gap is persistence (four additive
>   columns, same file).
> * **D3 remains OPEN and still blocks case creation** — `department`,
>   `grade`, `nationality_group`, `location_name`, `project_name`
>   (plus `current_stage`, `onboarding_attributes`) are still NOT NULL
>   with no UC-01 value. Resolving D1/D2 did not unblock creation.

### D1 — `role_id` — **RESOLVED: not required**
`designation` is free text ("Senior Engineer") typed by whoever drafts
the offer. `role_id` is a keyed reference. Mapping one to the other
without an authoritative catalogue creates an unenforced fake foreign
key that silently mis-files employees.

- [ ] **A.** The caller will supply `role_id` — we extend the intake contract.
- [ ] **B.** Use mapping table `____________`; unmatched designations should `____________`.
- [ ] **C.** Make `role_id` nullable for UC-01-originated rows.

**Answer:** ______  **Owner:** ______

### D2 — `manager_id` — **RESOLVED: not required**
Not captured anywhere in UC-01's intake. `hr_officer_id` is the HR
officer processing the case, **not** the candidate's reporting manager —
substituting it would put wrong data in a field downstream systems use
for approval routing.

- [ ] **A.** The caller will supply `manager_id`.
- [ ] **B.** Resolve it from `____________` (e.g. department head lookup).
- [ ] **C.** Make it nullable for UC-01-originated rows.

**Answer:** ______  **Owner:** ______

### D3 — the other five — **RESOLVED 2026-11-04**

| Column | Approved outcome | Implemented as |
|---|---|---|
| `department` | optional, populate later | `Optional[str] = None`; blank → NULL |
| `grade` | optional, populate later | `Optional[str] = None`; blank → NULL |
| `nationality_group` | optional, populate later | `Optional[str] = None`; blank → NULL |
| `project_name` | optional, populate later | `Optional[str] = None`; blank → NULL |
| `location_name` | **required from the caller** | `str`, trimmed; missing/NULL/empty/whitespace rejected |
| `current_stage` | default `INTAKE` | `DEFAULT_CURRENT_STAGE`; explicit value preserved |
| `onboarding_attributes` | default empty | `default_factory=list` (no shared mutable default) |

Application code: `models/case.py` (`Case`, `CasePlacement`),
`api/schemas.py`, `open_case.create_case`, `onboarding_entry`.
DDL: `docs/schema/proposed/d4_d5_additive.sql` — **NOT APPLIED**.

Two things this did **not** resolve:

* **`onboarding_attributes` type.** The decision says "empty JSON
  object `{}`", but the live column is **`text[]`**, and `{}` is also
  Postgres' empty-ARRAY literal. The implementation matches the live
  type (empty array). A jsonb object would need a shared column-type
  change. **DBA confirmation needed.**
* **Later population.** No authorized UC-01 path writes these four
  fields after creation — see the superseded text below and the final
  report.

<details><summary>Original D3 question (superseded)</summary>

For each: **supplied** upstream, **derived** by a stated rule, or
**nullable**.

| Column | Suggested | Your answer |
|---|---|---|
| `department` | derived from `role_id` (depends on D1) | ______ |
| `grade` | derived from `role_id` (depends on D1) | ______ |
| `location_name` | derived from `work_location_type` + `client_site_id` | ______ |
| `project_name` | nullable — meaningless for non-`CLIENT_SITE` cases | ______ |
| `nationality_group` | supplied; PII-adjacent, may have a residency rule | ______ |

`current_stage` and `onboarding_attributes` also need a UC-01 default
(suggest `'INTAKE'` and `'{}'`). **Answer:** ______

</details>

### D3b — rehire reactivation (EC-43) — **RESOLVED: IN SCOPE**
`Case.reactivates_record_id`, `Case.reactivates_external_ref` and
`PersonRecord.hris_external_ref` have **no columns** live. Until they
exist, rehire/internal-transfer reactivation cannot persist, and the
adapter refuses rather than silently dropping the HRIS link.

- [ ] In scope — add the three columns.  - [ ] Out of scope for now.

---

## For the DBA / platform owner — D4, D5, D6

### D4 — audit chain columns (blocks ALL persistence)
`AuditEntry` is hash-chained (`audit_integrity.py`): `sequence`,
`prev_hash`, `entry_hash` link each row to its predecessor so edits,
deletions and reordering are detectable. **`audit_events` has none of
the three.** Writing without them gives an audit log that looks complete
but can never be verified — `/audit/integrity` would have nothing to
check. That is worse than not persisting, so the adapter refuses.

**Correction (second review pass):** an earlier version of this document
proposed a **per-case** chain, `UNIQUE (case_id, sequence)`. That was
wrong. `verify_chain` asserts `entry.sequence == index` over the
**complete log in append order**
([audit_integrity.py:196](../src/sub_agent/audit_integrity.py#L196)), and
`verify_audit_integrity` is AUDITOR-only precisely because "the whole
log spans every client". A per-case chain has gaps in `sequence` by
construction and would be reported as `SEQUENCE_BREAK` on an intact log.
Adding three columns is **not** a complete implementation.

The chain is global, contiguous and zero-based. That forces a choice:

- [ ] **A. Add the columns to the shared `audit_events`.** Requires a
      way to identify UC-01's rows (the "complete log" is UC-01's
      subset), and every UC-01 append must serialize on one global
      counter, in the same transaction as the insert so no gap is ever
      left behind. Cheapest DDL, most coupling.
- [ ] **B. Give UC-01 its own `uc01_audit_log` table.** The chain is
      then unambiguously "every row in this table", no marker column is
      needed, and other use cases are unaffected by UC-01's
      serialization. Two audit tables in one project. **Recommended.**
- [ ] **C. Change `verify_chain` to support scoped chains** (per client
      or per case) and accept that this is a change to an integrity
      control, requiring its own review and test updates.

`audit_events` also requires `client_id`, `actor_role`, `outcome`,
`detail` (NOT NULL), which `AuditEntry` lacks. Proposed mapping:
`actor_role` from the existing `Viewer`, `outcome` from the action
result, `detail` from `before`/`after`, `correlation_id` from
`request_id`.

**Answer:** ______  Append-only enforcement confirmed? ______
(note: must include a `BEFORE TRUNCATE ... FOR EACH STATEMENT` trigger —
row-level `BEFORE DELETE` does **not** fire on `TRUNCATE`)

### D5 — atomic employee-number allocation
Today's read-then-update over PostgREST has no transaction and no row
lock: two concurrent requests can mint the **same** number.

```sql
CREATE FUNCTION next_employee_no(p_client_id text) RETURNS text
LANGUAGE sql VOLATILE AS $$
  UPDATE employee_number_counters
     SET last_value = last_value + 1, updated_at = now()
   WHERE client_id = p_client_id
  RETURNING prefix || lpad(last_value::text, padding, '0');
$$;
```

`UPDATE … RETURNING` takes a row lock and is atomic in one statement, so
it is safe over PostgREST. It also removes a duplicated rule: `prefix`
and `padding` live in the table, and the Python `EMP-%06d` literal is
deleted rather than kept as a second source of truth. Counter rows must
be seeded per client. **The function must not be granted to `anon`.**

- [ ] Approve.  - [ ] Prefer `SELECT … FOR UPDATE` once D6 lands.

**Answer:** ______  Seed counters for all clients in `client_configs`? ______

### D6 — connection method (determines whether writes can be atomic)
`psycopg` is what the design specifies, because `open_onboarding_case`
touches 7 tables and must be all-or-nothing. We cannot use it: the root
`.env` `DATABASE_URL` names a **different project in a different
region** (`ap-southeast-1`, not `eu-north-1`) and its password is still
the literal `[YOUR-PASSWORD]`. Only the PostgREST URL + service-role key
work, and PostgREST runs one statement per call.

- [ ] **A.** Provide a psycopg connection string for *this* project —
      **session mode `:5432`**, not the `:6543` transaction pooler
      (which cannot hold the advisory locks `execute_hris_write` needs),
      ideally as a restricted `uc01_app` role rather than `service_role`
      (which carries `BYPASSRLS`).
- [ ] **B.** Keep PostgREST and move case creation into a PL/pgSQL
      function (one implicit transaction, therefore atomic) — a larger
      architectural commitment, as business logic moves into SQL.

**Answer:** ______

---

## For security — D7

The field-encryption key currently in the root `.env` is **one I
generated locally for development and must be replaced before
production.** Required: 32 random bytes, base64, held in Key Vault /
Supabase Vault and injected as `UC01_FIELD_ENCRYPTION_KEY` — never a
column default, a database setting, or a committed file.

Rotation is **not yet implemented**: the ciphertext carries a version
byte, but there is no multi-key reader. Rotating today would make
existing salary values unreadable. `onboarding_forms` currently has 0
rows, so rotating now is free — it will not stay that way.

- Key owner: ______  Rotation interval: ______
- [ ] Build multi-key rotation support now  - [ ] Defer (accept the gap)

---

## For the engineering lead — D8

### Test environment
Agreed approach: **local Supabase CLI stack** (`supabase start` — real
Postgres + PostgREST in Docker). Full fidelity for the adapters, zero
risk to the shared project, CI-capable. Scaffolding is in
`supabase/` and `docs/local-test-env.md`.

`Onboarding_Agent` is a **shared live database** — row counts moved
between reads during investigation because other use cases write to it
concurrently. Integration tests must not run against it.

- [ ] Approve local stack  - [ ] Require a separate cloud project instead

**Prerequisite not yet met:** neither Docker nor the Supabase CLI is
installed on the current machine. Someone must install both before the
integration test can run.

---

## Dependency order

```
D4 ─┐
D1 ─┼─> case creation possible ─> D5 ─> employee numbers ─> D8 proves E2E
D2 ─┤
D3 ─┘
D6 ─────> writes become atomic (independent, but needed before production)
D7 ─────> needed before production, not before testing
```

**D4 blocks everything** — no UC-01 flow completes without an audit
write. D1/D2 block case creation specifically. D6 and D7 do not block
testing but do block production.
