# UC-03 persistence on Supabase PostgreSQL (`integrations/supabase/`)

UC-03 stores its case state, approvals, audit trail and validation results in Supabase when `UC03_STORE_MODE=supabase`. The default is `memory`, so tests and offline work need no credentials.

> **Status: implemented and tested against a strict fake of the real tables. Not live-verified.** No UC-03 row has been written to a real database, and the migration below has **not** been applied. The only live database access was read-only (table names, column names, required flags and row counts) on the shared project.

## Architecture
```
UC-03 API / orchestrator ──▶ DocumentValidationService (deterministic decision)
                                 │ CaseStore / AuditStore / ValidationResultStore   (src/state/store.py protocols)
                                 ▼
                       integrations/supabase/stores.py   (repositories)
                                 │ TableClient
                                 ▼
                       integrations/supabase/tables.py   SharedSupabaseTables
                                 │
                                 ▼
          hr_onboarding_common.platform.supabase_client  (the SHARED Common client; PostgREST)
                                 │
                                 ▼
                          Supabase PostgreSQL
```
* **Selection:** `integrations/supabase/runtime.py`, `build_stores()`. Read `UC03_STORE_MODE` (`memory` | `supabase`). In `supabase` mode a missing `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` stops start-up with a message that names the variable. There is **no silent fall back to memory**.
* **One client per process:** the Common singleton (`get_supabase_client()`). Never one per request.
* **Wiring:** the API through `src/main.py` (composition root, `configure_api()` at start-up), and the orchestrator through `HR-Onboarding-Common/.../adapters/uc03_documents.py` (UC-03's own `build_stores()`).

## Tables
| Table | Owner | UC-03 reads | UC-03 writes | How |
|---|---|---|---|---|
| `uc03_case_state` | UC-03 (**new, proposed migration**) | yes | insert, versioned update | UC-03's own case state; optimistic concurrency on `version` |
| `approval_requests` | shared | yes | insert, update of its own rows | `workflow_module='uc03'`, `subject_ref='uc03:<case>:<action>'`, `requested_by='uc03-agent'` |
| `audit_events` | shared, append-only, hash-chained by the DB | yes | insert only | `reference='uc03:<case>'`; a retried event keeps its id and is not duplicated |
| `candidate_documents` | shared | yes | insert (registration), status update | registered by the ingestion path, which knows size, type and checksum |
| `document_validations` | shared (+ **additive columns**) | yes | insert | one row per distinct request (`request_key`); model metadata only in `advisory` |
| `onboarding_cases` | UC-01 | **read only** | never | links a UC-03 case to a real onboarding case |

### Value mapping (`mapping.py`)
UC-03's enums are wider than some shared `CHECK` lists. The shared column gets the closest value, and UC-03's exact value is kept in an additive column. Examples: compliance `COMPLETE→COMPLIANT`, `BLOCKED→NON_COMPLIANT`; actor `HR_USER→HUMAN`. Anything without a defensible counterpart raises (`UnmappedValue`): for example approval status `NOT_REQUIRED`, and the document types `experience_letter`, `payslips` and `family_dependent_documents`, which are absent from `candidate_documents.document_type`.

### When a result is not stored
`persistence_status` on every result says what happened:
* `PERSISTED`: stored, or already stored by an identical earlier request.
* `NOT_PERSISTED:CASE_NOT_AN_ONBOARDING_CASE`: the case id is not a real `onboarding_cases` row (e.g. a standalone UC-03 id). The shared tables require one.
* `NOT_PERSISTED:DOCUMENT_TYPE_NOT_IN_SCHEMA`: the type has no counterpart in the shared list.
* `NOT_PERSISTED:DOCUMENT_NOT_REGISTERED`: no `candidate_documents` row (it needs file metadata from the ingestion path).
* `NOT_CONFIGURED`: memory mode.

A database **error** is never a status: it raises (`StorageError`), and the API answers 500. The orchestrator records a technical failure, never a document verdict.

### Consistency
PostgREST offers no multi-row transaction here, so every write is a single row and idempotent: a primary key or unique key prevents duplicates on retry. A failure after the case state or audit is written but before the result is stored leaves those rows; the result row is simply absent and the call raises.

## Migration
`supabase/migrations/20261104120000_uc03_persistence.sql`: **proposed, not applied.** It is additive only and safe to re-run:
1. creates `uc03_case_state` (RLS on, no policies: service role only);
2. adds nullable UC-03 columns to `document_validations` (`uc03_case_ref`, `request_key`, `document_status`, `required_next_action`, `approval_status`, `evidence_status`, `uc03_compliance_status`, `uc03_visa_pack_status`, `decision_detail`, `audit_reference`, `advisory`);
3. adds a unique index on `request_key`.

Nothing is dropped or narrowed, and no other use case's column or row changes. A person applies it, first to an isolated or approved project.

## Configuration (names only)
| Variable | Read by | Purpose |
|---|---|---|
| `UC03_STORE_MODE` | UC-03 | `memory` (default) or `supabase` |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_SCHEMA` | the shared Common client | connection. UC-03 only checks they are set and never prints, logs or sends them |

Install: the `gateway` extra brings in `hr-onboarding-common`, which provides the shared client.

**Warning about Common.** `hr_onboarding_common.platform.supabase_client` calls `load_dotenv()` when imported, which loads the repository-root `.env`. UC-03 imports it **only** in supabase mode, and the tests never import it on the default path.

## Security
* The service-role key stays in the Common client. It is never passed to a model, a log, an exception or a result (database errors are reduced to a status and a PostgreSQL code).
* `uc03_case_state` holds the name from the onboarding form, so RLS is on with no policies.
* No document content is stored. `document_validations` holds finding codes and statuses; `extracted_name` and `extracted_fields` are the schema's own columns for that purpose.

## Tests (offline)
* `tests/unit/test_uc03_supabase_persistence.py`: mode selection, case create/read/versioned update, concurrent change detection, document registration, results, HR referrals, audit (append-only, retry-safe), approvals, idempotency, failure handling, error reduction, and the API with database-backed stores.
* `tests/unit/test_uc03_end_to_end.py`: all scenarios with gateway and database together.
* `tests/unit/fake_tables.py`: the strict fake. Columns and required flags come from `tests/fixtures/uc03_live_schema.json`, a read-only snapshot with names only. The fake enforces `CHECK` lists, keys, foreign keys and append-only audit.

## To live-verify (needs approval)
1. A person applies the migration to an isolated or development Supabase project.
2. Set `UC03_STORE_MODE=supabase` and that project's settings.
3. Run a tagged synthetic case through `/onboarding/uc03/validate` and read the rows back.

Do not use the shared project: its `audit_events` is append-only and hash-chained, so test rows would stay there permanently.
