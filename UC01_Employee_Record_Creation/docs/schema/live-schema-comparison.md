# UC-01 vs. the live `Onboarding_Agent` schema

Verified by reading the live PostgREST schema of the confirmed project
(`eu-north-1`) on 2026-11-03. Columns, types, nullability, primary keys
and foreign keys below are **observed**, not assumed.

**`docs/schema/uc01_draft.sql` is NOT the authority for this work.** It
has drifted from both the models and the database (see §3). Where they
disagree, the Pydantic models define what UC-01 needs and the live
schema defines what exists.

---

## 1. What is implemented and durable

| Port | Live table | Adapter | Notes |
|---|---|---|---|
| `FormRepository` | `onboarding_forms` | `SupabaseFormRepository` | Full mapping. Salary AES-256-GCM encrypted. |
| `PersonDirectory` | `person_records` | `SupabasePersonDirectory` | Full except `hris_external_ref` (§2). |
| `DiscrepancyRepository` | `intake_discrepancies` | `SupabaseDiscrepancyRepository` | `field` → `field_name`. |
| `HrisRecordRepository` | `hris_sync_records` | `SupabaseHrisRecordRepository` | `system` → `target_system`; two fields refused (§2). |
| `EventPublisher` | `domain_events_outbox` | `SupabaseEventPublisher` | Full mapping. |
| `HolidayCalendar` | `client_holidays` | `SupabaseHolidayCalendar` | Read-only; honours `is_observed`. |
| `PendingStartRepository` | `pending_starts` | `SupabasePendingStartRepository` | Full mapping. |
| `OnboardingFormSource` | `offer_forms` | `SupabaseOnboardingFormSource` | Bridge + form. |

## 2. Blocked, with the exact missing capability

Each raises `SchemaGapError` **before** any write
(`adapters/supabase/unsupported.py`). None fabricates a value, weakens a
constraint, or falls back to memory.

### `CaseRepository` → `onboarding_cases` — **blocks the core flow**
NOT NULL, no default, and absent from UC-01 (0 references in `src/`,
`specs/`, `skills/`, and the `POST /internal/onboarding-requests` body):

```
role_id, manager_id, department, grade, nationality_group,
location_name, project_name, current_stage, onboarding_attributes
```

`role_id` and `manager_id` are the blocking pair. `designation` is free
text ("Senior Engineer") and is **not** a `role_id`; no manager is
captured anywhere in the intake flow. **Decision required** — one of:

1. extend the upstream contract so the caller supplies both, and add
   them to `Case`; or
2. confirm an authoritative `designation → role_id` mapping plus a
   manager-resolution rule, implemented as an explicit tested lookup; or
3. agree these columns become nullable/defaulted for UC-01 rows.

Also absent: `reactivates_record_id`, `reactivates_external_ref` (on the
`Case` model, no columns live). Present and mapped: the variant flags,
with `is_uae_resident` → **`variant_is_resident`**.

### `AuditLog` → `audit_events` — **audit integrity cannot be preserved**
`AuditEntry` is hash-chained (`audit_integrity.py`): `sequence`,
`prev_hash`, `entry_hash`. **The live table has none of the three.**
Writing entries without them yields an audit log that looks complete but
can never be verified — `/audit/integrity` would have nothing to check.
That is worse than not persisting, so the adapter refuses.

The table also requires `client_id`, `actor_role`, `outcome`, `detail`
(all NOT NULL), which `AuditEntry` does not have; it names the timestamp
`occurred_at` and the correlation id `correlation_id`.

### `ApprovalRepository` → `approval_requests` — different entity
Live: `request_id` (text PK), `gate`, `approver_role`,
`workflow_module`, `payload`, `rationale`, `confidence_score`,
`sla_due_at` (all NOT NULL), `outcome`.
UC-01 `Approval`: `approval_id` (uuid), `checkpoint`, `subject_ref`,
`requested_to`, `status`, `reason`, `decision_note`.
This is the cross-use-case orchestration approval, not UC-01's human
checkpoint. `requested_to` has no column (`approver_role` is a role, not
a person).

### `OfferRepository` → `offers`
Requires `candidate_name`, `designation`, `offer_payload` (NOT NULL);
`OfferRecord` has none. It is also `[Proposed]`/dev-simulator-only in
`ports.py`, so writing to a shared table from it would be wrong anyway.

### `EmployeeNumberGenerator` → `employee_number_counters`
Allocation is a read-modify-write. PostgREST gives one statement per
call, no transaction, no row lock — two concurrent requests can mint the
**same** number. Needs an atomic `UPDATE ... RETURNING` exposed as an
RPC, or a direct psycopg connection. The table also requires `prefix`
and `padding`, while `EMP-%06d` lives in Python (two sources of truth).

### `DocumentStore`
No table backs it; `candidate_documents` stores validated documents, not
case folders.

## 3. Draft-vs-model drift (fix the draft before reusing it)

Seven fields are on the models and used in `src/`, but absent from
`uc01_draft.sql` **and** from the live schema:

| Model | Field | `src/` refs |
|---|---|---|
| `AuditEntry` | `sequence`, `prev_hash`, `entry_hash` | 15 / 16 / 9 |
| `Case` | `reactivates_record_id`, `reactivates_external_ref` | 5 / 3 |
| `HrisRecord` | `updates_external_ref` | 6 |
| `PersonRecord` | `hris_external_ref` | 2 |

## 4. Connection method, and what it costs

`psycopg` (the design's choice, §9 of `uc01-schema.md`) needs a Postgres
connection string. The root `.env` `DATABASE_URL` names a **different
project in a different region** (`ap-southeast-1`, not `eu-north-1`) and
its password is still the literal `[YOUR-PASSWORD]` placeholder. The
only working credentials are the PostgREST URL + service-role key.

Consequences, stated rather than hidden:

- **No multi-statement transactions.** Operations #1–#12 of §9 are not
  atomic over PostgREST. The two operations that most need atomicity
  (`open_onboarding_case`, `approve_form_change`) are blocked anyway by
  the `CaseRepository`/`AuditLog` gaps above, so no partially-written
  case can occur today — the write is refused before it starts.
- **No advisory locks**, hence no safe employee-number allocation.
- **RLS is bypassed.** The service-role key carries `BYPASSRLS`;
  isolation currently rests on `access.py`/`policy.py` in application
  code, exactly as it did with the in-memory adapters.

To move to `psycopg`: supply the real connection string for *this*
project (session mode `:5432`, not the `:6543` transaction pooler, which
does not support advisory locks), ideally as a restricted `uc01_app`
role rather than `service_role`.

## 5. Encryption

`onboarding_forms.salary_and_terms_encrypted` (`bytea`) is written by
`sub_agent/crypto.py` using **AES-256-GCM**: random 12-byte nonce per
write, `form_id` as AAD (so a ciphertext cannot be moved to another
row), 1-byte version prefix for future rotation. The key is 32 bytes,
base64, from `UC01_FIELD_ENCRYPTION_KEY` — never defaulted, never
derived, never logged. `pgcrypto`/`pgp_sym_encrypt` was not used: it
would require sending the plaintext and the key to the database in a
statement.

No form rows existed at integration time, so no re-encryption was
needed. Rotating the key makes existing values unreadable.
