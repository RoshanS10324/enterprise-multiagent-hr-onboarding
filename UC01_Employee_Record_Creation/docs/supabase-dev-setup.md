# UC-01 / UC-02 development setup for Supabase: what is needed, and from whom

Checked read-only against the shared dev project on 2026-11-04.
**UC-01 writes to Supabase are still blocked.** This guide lists exactly
what must be supplied, and by whom, before they can be enabled.

## 1. The encryption key (B1, owner: team lead)

`UC01_FIELD_ENCRYPTION_KEY` (AES-256-GCM, base64-encoded 32 bytes)
encrypts `onboarding_forms.salary_and_terms_encrypted`.

- **Status (2026-11-04):** a versioned **development** key is in
  `UC01/.env` (`UC01_FIELD_ENCRYPTION_KEY_ID=dev-...`), created with
  `python scripts/create_dev_encryption_key.py` on the UC-01 owner's
  decision. It is for `APP_ENV=dev` and synthetic data only: `Settings`
  refuses a `dev-` key id anywhere else. The existing `SYN-UAE-TEST` row
  (another developer's key) is untouched and stays readable with that key.
- **Versioning and rotation:** new values are written as v2 ciphertexts that
  carry their key id (bound into the authenticated data). To rotate, run the
  script with `--rotate`: the old key moves to
  `UC01_FIELD_ENCRYPTION_PREVIOUS_KEYS` (decrypt only) and every existing row
  stays readable; rows are re-encrypted under the new key when next written.
- **Production:** the key comes from the approved secret store, never from
  this script.
- **How it must be supplied:**
  - through the team's secret store, as the **same key** every developer and
    the dev deployment use;
  - placed in the environment or in the repository-root `.env` (never
    committed, and `.gitignore` covers it);
  - never sent in chat or email.
- **Check it without printing it:** decrypt the existing `SYN-UAE-TEST` row
  in memory, and report only success or failure.
- **Must not be done:**
  - using a `dev-` key outside development or for real data;
  - reusing another developer's key without the team agreeing it;
  - disabling encryption;
  - relaxing `Settings._supabase_config_is_complete`.
- **Without the key:** `uvicorn sub_agent.api:app` refuses to start. It
  exits with code 3 and the message "UC01_FIELD_ENCRYPTION_KEY". Importing
  the package still works, because the app is built lazily (`api/app.py`,
  `LazyDefaultApp`).
- **Backup:** losing a key makes every value encrypted under it
  unreadable. Before production, the key needs a managed vault entry, an
  owner, a backup and a rotation schedule.

## 2. Client configuration (B8, owner: client-configuration owner)

| Client | `client_configs` | Employee-number counter | UC-02 configuration (`SupabaseConfigRegistry`) |
|---|---|---|---|
| `CLIENT_DUBAI`, `CLIENT_INDIA`, `CLIENT_CANADA`, `CLIENT_USA` | present | present | **refused**: "v1 is 'draft', not approved" (`client_config_versions.status = draft`, effective from 2026-11-02) |
| `SYN-UAE-TEST` | present | **absent** | not checked |
| `CLIENT-ALPHA` | **absent** | absent | **refused**: "client is not in client_configs". It exists only in UC-02's in-code synthetic registry. |
| `SYN-UC01-DOC-TEST` | **absent** | absent | refused: not in `client_configs` |

**No client in this database can currently run UC-02 in Supabase mode.**
A client configured in UC-02's code (`CLIENT-ALPHA`) is **not** configured
in the database.

To run UC-01 and UC-02 together against Supabase, one synthetic client
needs all three of the following. Each is shared data, created through the
owner's approved process, not by UC-01:

1. a `client_configs` row: `company_name`, `config_hash`, `working_days`,
   `settings_payload`, plus defaults;
2. an `employee_number_counters` row: `prefix`, `padding`, `last_value`;
3. an **approved** `client_config_versions` row in force on the test
   joining date, with the templates and milestones UC-02 plans from.

The quickest route is to approve v1 of one existing synthetic client, for
example `CLIENT_DUBAI`. It already has the first two.

## 3. Audit storage and all-or-nothing writes (B2 + B6, owner: DBA)

These two decisions are coupled:

- **The audit chain.** Each `AuditEntry` gets `sequence`, `prev_hash` and
  `entry_hash`, computed in **Python** by `audit_integrity.chain_entry` from
  the tail of the log. The chain is global, contiguous and zero-based
  (`docs/schema/audit-chain-design.md`).
- **The live `audit_events` table** requires `client_id`, `actor_role`,
  `outcome`, `detail` and `occurred_at`. It is shared: 398 rows from other
  use cases, none with UC-01's chain. `AuditEntry` has no `client_id`, and
  request-level entries such as `START_GATE_EVALUATED` have no case.
- **Opening a case** writes the case, form link, person record, approvals
  and audit entries. Over PostgREST each call is its own transaction.
  `PostgrestClient.rpc` now exists, so a SQL function can make one call
  atomic.

| Option | What the DBA decides | Fit with the existing chain code |
|---|---|---|
| **A. Direct connection (psycopg), restricted `uc01_app` role** | A `DATABASE_URL` for **this** project (the configured one points at another project), and the role's grants | **Best.** One transaction: advisory lock, then read the chain tail, compute the hash in Python with the existing code, then insert everything. This matches `audit-chain-design.md` §3. |
| B. One SQL function per operation via `/rpc` | Function bodies, `SECURITY DEFINER` or not, grants | The hash must be computed **in SQL**. That re-implements `entry_digest`'s canonical JSON, needs parity tests, and changes the chain's source of truth. |
| C. Dedicated `uc01_audit_log` table (draft DDL) with either A or B | Table and append-only triggers (`uc01_b2_b4_b7_b9_draft.sql`) | Avoids the shared table's `client_id` requirement and other use cases' rows |

**Recommendation for the DBA:** C, implemented with A. Until the DBA
decides, `UnsupportedAuditLog` stays in place, so no case can be opened
against Supabase.

## 4. Developer-side readiness (done, not active)

| Port | Live adapter | Wired? |
|---|---|---|
| Approvals (`uc01_approvals`) | `SupabaseApprovalRepository` | No. Enabled by adding `"approvals"` to `api/container.py` `APPROVED_SUPABASE_PORTS` (currently empty). |
| Employee numbers (`rpc/next_employee_no`) | `SupabaseEmployeeNumberGenerator` | No. Needs `"employee_numbers"` in the set **after** B7 confirms the function is concurrency-safe and granted. |
| Cases (`onboarding_cases`) | `SupabaseCaseRepository` | No. It has no default status mapping and refuses writes until a **B5-approved** mapping is passed in. It also needs B6. |
| People / HRIS sync | `SupabasePersonDirectory` (`hris_external_ref`), `SupabaseHrisRecordRepository` (`updates_external_ref`) | Yes, already used in Supabase mode. The stale refusals are removed now that the columns exist live. |
| Audit, offers, documents | none (B2, B3, B4) | No |
| Document object paths | `adapters/supabase/storage_paths.case_object_path` | Helper only. No bucket exists and nothing uploads (B4). |

All of the above is verified against a **fake PostgREST** only, which is
not evidence of real Supabase persistence.

## 5. UC-02 in Supabase mode (once UC-01 can write a case)

UC-02's own live-schema check (`supabase_schema.verify_schema`) passes. To
run it against the dev project with synthetic data:

```powershell
$env:UC02_STORE_MODE = "supabase"
$env:UC02_ENV = "development"              # explicit; never production for synthetic data
$env:UC02_ALLOW_SYNTHETIC_CONFIG = "1"     # allowed only with development/test (config_safety.py)
```

It still needs:

- an **approved** configuration version for the client (section 2);
- the UC-01 case row, because in Supabase mode the onboarding id **is** the
  UC-01 `case_id`. Without it `create_onboarding` raises "the shared
  database needs the UC-01 case_id to start an onboarding".

`config_safety.py` is not bypassed: an unknown or missing `UC02_ENV`, or
the synthetic opt-in outside development/test, is refused.
