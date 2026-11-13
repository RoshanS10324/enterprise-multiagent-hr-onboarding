# Manual seed: four synthetic clients (Supabase Development)

**Files:** `docs/manual_seed_four_synthetic_clients.sql` (this README's subject) and this README. **Status: generated, NOT executed anywhere.** Nothing was run against
Supabase, no migration was applied, nothing was committed. Wait for a manual review before running it.

## What it does

Inserts the four synthetic client configurations `SYN-CLIENT-A`, `SYN-CLIENT-B`, `SYN-CLIENT-C`, `SYN-CLIENT-D` into the **Development** database: **207 rows** across seven
existing tables. The rows are generated from `client_configuration/profiles/client_[a-d]_config.json` (the source of truth) by
`client_configuration/generate_manual_seed_sql.py`, which reuses the same projection as the Python seeder's dry run, so the SQL and the profiles cannot drift. Re-run the
generator (`python client_configuration/generate_manual_seed_sql.py`) if a profile changes; it also re-counts the rows in the file and fails if the count is not 207.

It does **not** touch `role_access_matrix` (UC-04 has not confirmed that projection), creates no table/column/index, and contains no secret, personal data or real client data.
The text `role_access_matrix` appears only as a JSON field name inside each client's UC-04 section document (stored in a `client_service_policies` row); no statement targets that table.
Every value is a labelled synthetic assumption, not an approved client policy. Out-of-scope use cases are not referenced.

## Expected inserts

| Table | Rows | SYN-CLIENT-A | SYN-CLIENT-B | SYN-CLIENT-C | SYN-CLIENT-D |
|---|---:|---:|---:|---:|---:|
| `client_configs` | 4 | 1 | 1 | 1 | 1 |
| `client_config_versions` | 4 | 1 | 1 | 1 | 1 |
| `task_templates` | 57 | 15 | 11 | 15 | 16 |
| `client_document_requirements` | 55 | 13 | 12 | 17 | 13 |
| `client_service_policies` | 74 | 20 | 15 | 18 | 21 |
| `client_milestone_templates` | 4 | 1 | 1 | 1 | 1 |
| `client_holidays` | 9 | 2 | 2 | 3 | 2 |
| **TOTAL** | **207** | | | | |

Per-client differences (task sets, documents, equipment, programmes, calendars, approvers) are exactly those of the JSON profiles; nothing was invented or replaced.

## Columns used (exact live column names, live column order)

| Table | Columns | Names |
|---|---:|---|
| `client_configs` | 9 | `client_id, company_name, config_hash, working_days, timezone, template_count, milestone_count, settings_payload, is_active` |
| `client_config_versions` | 8 | `client_id, version, effective_from, effective_to, status, config_hash, approved_by, approved_at` |
| `task_templates` | 22 | `template_key, client_id, category, title, title_ar, owner_role, default_lead_days, evidence_required, requires_attributes, sensitive_to, approval_role, description, role_ids, grades, locations, projects, nationality_groups, depends_on_template_keys, offset_days, default_priority, config_version, is_active` |
| `client_document_requirements` | 15 | `requirement_id, client_id, config_version, service_category, requirement_key, document_type, description, is_mandatory, applicable_roles, applicable_grades, applicable_locations, applicable_projects, eligibility_rules, validation_profile, is_active` |
| `client_service_policies` | 13 | `policy_id, client_id, config_version, service_category, service_key, is_available, is_mandatory, eligibility_rules, policy_details, responsible_role, effective_from, effective_to, is_active` |
| `client_milestone_templates` | 9 | `milestone_id, client_id, config_version, milestone_key, title, offset_days, required_task_keys, eligibility_rules, is_active` |
| `client_holidays` | 4 | `client_id, holiday_date, name, is_observed` |

Columns not listed are left to their database defaults on purpose: `created_at` / `updated_at` (`now()`). Every NOT NULL column without a default is supplied (checked by the
script's own schema guard and by the generator).

## Schema facts the script is built on

From your schema exports (`information_schema.columns`, constraints, CHECK definitions, indexes; see `CLIENT_CONFIGURATION_COLUMN_MAPPING.md`) cross-checked with the live PostgREST schema, which has identical columns, order and nullability:

| Table | Primary key | Foreign key | Unique constraint / indexes |
|---|---|---|---|
| `client_configs` | `client_id` | none | CHECK `template_count >= 0`, `milestone_count >= 0` |
| `client_config_versions` | `(client_id, version)` | `client_id` -> `client_configs.client_id` | CHECK `status IN (draft, approved, retired)`; CHECK `effective_to IS NULL OR effective_to > effective_from` |
| `task_templates` | `(client_id, template_key)` - **`config_version` is NOT in the key** | `client_id` -> `client_configs.client_id` (PostgREST) | CHECK `category` (19 values) and `owner_role` (12 values); the rest of its constraints were not in the cut-off export |
| `client_document_requirements` | `requirement_id` (uuid) | `(client_id, config_version)` -> `client_config_versions(client_id, version)` | UNIQUE `(client_id, config_version, requirement_key)` |
| `client_service_policies` | not in the export (`policy_id` uuid per PostgREST) | `(client_id, config_version)` -> `client_config_versions(client_id, version)` | **not in the export (unconfirmed)** |
| `client_milestone_templates` | `milestone_id` (uuid) | `(client_id, config_version)` -> `client_config_versions(client_id, version)` | UNIQUE `(client_id, config_version, milestone_key)` |
| `client_holidays` | `(client_id, holiday_date)` | `client_id` -> `client_configs.client_id` | not verified |

Column types are the live ones: `text`, `integer` (int32), `boolean`, `jsonb`, `uuid`, `date`, `timestamptz`, `text[]`, `integer[]`. Every value carries an explicit cast.
Section 1d of the script prints the constraints actually present, so you can compare them with this table before running anything.

**Still unconfirmed (your constraint export stopped at the editor's 100-row limit):** all constraints of `task_templates` beyond its CHECKs and index-derived key, the primary key and any UNIQUE constraint of `client_service_policies`,
the indexes of the versions, documents, policies and milestones tables, and row-level-security policies. The two read-only queries that close the gap are in `CLIENT_CONFIGURATION_COLUMN_MAPPING.md`. All 207 rows were checked against every CHECK constraint that was exported (0 violations); anything unseen that rejects a row makes the script fail and roll back, never partially load.

## Insert order (foreign keys)

`client_configs` -> `client_config_versions` -> `task_templates` -> `client_document_requirements` -> `client_service_policies` -> `client_milestone_templates` -> `client_holidays`.
Rows with a database foreign key are inserted after their parent. Tables without a foreign key (documents, policies, milestones) are still inserted after the version row, and the
assertions check that every such row points at an existing client and version.

## Duplicate protection

The existing Python seeder's natural keys (`projection.NATURAL_KEYS`) are used, with one correction for the live schema:

| Table | Guard key |
|---|---|
| `client_configs` | `(client_id)` |
| `client_config_versions` | `(client_id, version)` |
| `task_templates` | `(client_id, template_key)` - the live primary key; the seeder's `config_version` is not part of it |
| `client_document_requirements` | `(client_id, config_version, requirement_key)` |
| `client_service_policies` | `(client_id, config_version, service_category, service_key)` |
| `client_milestone_templates` | `(client_id, config_version, milestone_key)` |
| `client_holidays` | `(client_id, holiday_date)` |

How it is enforced, in layers: (1) **Guard 2** refuses to run if any row already exists for the four client ids; (2) every `INSERT ... SELECT ... WHERE NOT EXISTS (natural key)`; (3) the
assertion block then checks exact per-table and per-client counts, so a row skipped by (2) is **an error, not a silent skip**; (4) a duplicate-key check; (5) `ON CONFLICT` is not used
anywhere (it would hide skipped rows, and it cannot target `client_service_policies`, whose unique constraint is unconfirmed). Nothing is updated, deleted or overwritten.

### Concurrency limitation of application-level duplicate prevention

A `NOT EXISTS` check is not a constraint. Two sessions inserting at the same moment could each pass the check and both insert. This script narrows that window for its own run by taking
`LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE` on the three versioned tables (documents, policies, milestones) inside the transaction: other sessions' INSERT/UPDATE/DELETE on those tables wait until it commits
(reads continue). The limits that remain: it protects only the duration of this transaction; any process that writes duplicates later (for example running the Python seeder or another copy of
this script after a rollback race) is not stopped by the database on `client_service_policies`, whose unique constraint is unconfirmed. The document and milestone tables are additionally protected by their live UNIQUE constraints, which would fail loudly.
If the export confirms `client_service_policies` has no unique constraint on its natural key, a unique index would remove the limitation, but that is a schema change and is **not** made here.
For the tables with a primary key or UNIQUE constraint, a concurrent duplicate would violate it and fail loudly rather than load.

## Transaction handling and failure behaviour

`BEGIN` ... `COMMIT`, in one transaction. Guard 1 (schema drift: every supplied column exists, no NOT NULL-without-default column is unsupplied), Guard 2 (first-time seed only), the seven
INSERTs, then the assertion block (counts, client references, version references, duplicates, dependency and milestone references, hash and approval consistency, synthetic marker). Any
`RAISE EXCEPTION` aborts the transaction, so either all 207 rows are committed or none. In the Supabase SQL editor a failed run is rolled back automatically; if you run it from psql, issue
`ROLLBACK;` after an error. The file is large (about 330 KB, mostly the six JSON section documents per client stored in `client_service_policies`); paste it whole. If the editor rejects it for size,
run it with `psql -f` against the Development connection instead.

## How to run (after your review)

1. Open the Development project's SQL editor (role `postgres`). Confirm you are on Development.
2. Run section 1 on its own. Expected: 0 rows for the four clients in every table; the four `SYN-CLIENT` ids absent from 1c.
3. Run sections 2-5 together. Expected final message: `seed verified: 207 rows, 4 clients, no duplicates, no missing references`, then section 5 results: every `ok = true`, `total_rows_seeded = 207`, 5d and 5e empty.
4. Optionally confirm from the repository side (read-only): `python scripts\uc02_client_config.py verify --no-uc04-matrix` in `UC02_Onboarding_Task_Agent`.

## Verification already done without a database

The generator checks that every projected column exists in the live schema and that all required columns are supplied. A separate structural lint of the generated file (no connection) confirmed:
quotes and dollar-quotes balanced; 23 statements (1 BEGIN, 1 LOCK, 3 DO, 7 INSERT, 10 SELECT, 1 COMMIT); no UPDATE, DELETE, TRUNCATE, DROP, ALTER, CREATE, GRANT or ON CONFLICT outside string
literals; every VALUES tuple has the same number of fields as its column list; every uuid, date, timestamptz, integer, boolean, array and JSON literal parses; 207 tuples, with the per-client counts above.
**The SQL has not been parsed or executed by PostgreSQL**, so syntax and constraint behaviour are unproven until you run it.

## Schema limitations (summary)

- `task_templates` primary key has no `config_version`: version 1 seeds cleanly, but a second version's templates for the same client cannot coexist with it (needs a database-owner decision).
- Documents, policies and milestones have a composite foreign key to `client_config_versions`, so the version row must exist first (the script inserts it first). `client_service_policies` uniqueness is unconfirmed; this script's checks and lock cover it.
- Indexes of four tables, part of the constraint list and RLS are unconfirmed (export cut off at 100 rows; see the mapping document for the queries). The SQL editor role bypasses RLS, so RLS is not exercised by this script.
- `role_access_matrix` is excluded; its live CHECK vocabulary (HARDWARE / SOFTWARE_ACCESS / PHYSICAL_BADGE / SYSTEM_PERM; STANDARD / MANAGER_EXCEPTION / SECURITY_GATE) does not match the profiles' equipment/access split, so UC-04 must confirm a mapping.
- `client_config_versions.status = 'approved'` with `approved_by = 'synthetic-fixture'` is a development placeholder; the clients are marked synthetic (`settings_payload.data_origin`) and a workflow uses them only when it opts in to synthetic configuration.

## Rollback reference (manual; do NOT run without a separate approval)

Seeding is insert-only, so undoing it is deleting exactly these rows. Order: children first.

```sql
-- REFERENCE ONLY. Not part of the seed script.
BEGIN;
DELETE FROM public.client_holidays WHERE client_id IN ('SYN-CLIENT-A','SYN-CLIENT-B','SYN-CLIENT-C','SYN-CLIENT-D');
DELETE FROM public.client_milestone_templates WHERE client_id IN ('SYN-CLIENT-A','SYN-CLIENT-B','SYN-CLIENT-C','SYN-CLIENT-D');
DELETE FROM public.client_service_policies WHERE client_id IN ('SYN-CLIENT-A','SYN-CLIENT-B','SYN-CLIENT-C','SYN-CLIENT-D');
DELETE FROM public.client_document_requirements WHERE client_id IN ('SYN-CLIENT-A','SYN-CLIENT-B','SYN-CLIENT-C','SYN-CLIENT-D');
DELETE FROM public.task_templates WHERE client_id IN ('SYN-CLIENT-A','SYN-CLIENT-B','SYN-CLIENT-C','SYN-CLIENT-D');
DELETE FROM public.client_config_versions WHERE client_id IN ('SYN-CLIENT-A','SYN-CLIENT-B','SYN-CLIENT-C','SYN-CLIENT-D');
DELETE FROM public.client_configs WHERE client_id IN ('SYN-CLIENT-A','SYN-CLIENT-B','SYN-CLIENT-C','SYN-CLIENT-D');
-- check the deleted row counts equal client_configs 4, client_config_versions 4, task_templates 57, client_document_requirements 55, client_service_policies 74, client_milestone_templates 4, client_holidays 9 before COMMIT;
COMMIT;
```

Every key includes one of the four new client ids, so the existing clients (`CLIENT_DUBAI`, `CLIENT_USA`, `CLIENT_INDIA`, `CLIENT_CANADA`, `test_enterprise_corp`, `SYN-UAE-TEST`) and the synthetic workflow case are not affected.
