# Central client configuration: architecture

As of 2026-11-04. Scope UC-01..UC-05. UC-02 is the implementing team; the other use cases are consumers and integration partners (their internal
logic is not implemented here). **Status:** design, JSON schema, four synthetic profiles, projection code, dry-run and mocked tests are done and committed-ready;
**the Supabase Development seeding has NOT been run** (awaiting explicit approval of the dry-run); the shared retrieval service and four-client live tests follow the seeding.

## Principles (from the Business Framework)

* "The core process is the same for every client. Each client gets its own knowledge layer... a new client is set up by configuration, not by changing the product" (BF section 2; AC-12).
* One client's configuration is never used in another client's case (GR-16, AC-11). Missing, inactive, expired or invalid configuration stops the workflow with a clear error; nothing is guessed (DL-17, EC-52).
* A client setting never overrides a guardrail (GR-17, EC-51). Authenticity is always decided by HR; messages always need approval.
* No secrets, credentials or personal data in configuration. Files live in Supabase Storage if ever needed; they are never stored in JSONB.

## Data architecture (decision: reuse, no migration)

```
 client_configuration/profiles/client_x_config.json   (authoring + review; validated by the JSON Schema)
                    |  validate  ->  project()  ->  dry_run()  ->  (approval)  ->  apply() insert-only  ->  verify()
                    v
 Supabase (existing shared tables)
   client_configs                 one row per client: master data in settings_payload (data_origin, country, locations, status, hash)
   client_config_versions         one row per client+version: effective dates, approval status/by/at, config_hash
   client_service_policies        category 'cfg_section': the WHOLE document, six rows per version (uc01..uc05 + shared) = source of truth
                                  category 'uc02_assignment': assignment rules (the reader's existing convention)
   task_templates                 projection of uc02 task templates                         -> read by UC-02 today
   client_document_requirements   projection of uc03 documents                              -> UC-03 / UC-02
   client_milestone_templates     projection of uc02 milestones                             -> UC-02
   client_holidays                projection of shared holidays                             -> all
   role_access_matrix             projection of uc04 role entitlements (optional; UC-04's table, unversioned)
```

**Why no new table or column:** the existing tables already carry what each consumer reads (templates, document requirements, holidays, milestones, the
UC-04 role matrix) and `client_service_policies` has `config_version`, effective dates, `is_active` and two JSONB columns, which is enough to hold the six
versioned sections. A migration would only add a second place for the same data. The document is the single source of truth; the relational rows are a
deterministic projection (`projection.py`), and `verify()` proves the database holds exactly what the document says (including that the stored sections
rebuild the document and hash to `client_config_versions.config_hash`). Optional improvements (a `config_version` column on `role_access_matrix`, RLS
review) are listed in `CLIENT_CONFIGURATION_DATABASE_MAPPING.md` and **not applied**.

Known limits of the mapping (each fails closed or is documented): employment type has no column, so it is carried as the attribute `employment_type:<x>`;
`excludes_attributes` cannot be represented in the existing tables and makes the projection refuse; `role_access_matrix` has no version or grade column, so
only the active version's role->item entitlements are projected and grade-specific entitlements live in the section row.

## Configuration lifecycle

Stored vocabulary (existing architecture): version `status` in {`draft`, `approved`, `retired`}; client `is_active`; `effective_from` / `effective_to`.
The user-facing states are **derived** at retrieval, not stored:

| Derived state | Rule |
|---|---|
| `active` | client active AND version `approved` AND `effective_from <= date` AND (`effective_to` null OR `date <= effective_to`) |
| `expired` | version `approved` but `date > effective_to` |
| `not_yet_effective` | approved but `date < effective_from` |
| `inactive` | `client_configs.is_active = false` |
| `draft` / `retired` | version status |

Only `active` configurations drive a workflow. A version needs `approved_by`, `approved_at` and a matching `config_hash`. Synthetic documents
(`data_origin = synthetic`) are used only when the runtime opts in (`UC02_ALLOW_SYNTHETIC_CONFIG=1`, never in production); real clients use `approved_client`.
Approval authority for real client configurations is open (BF OQ-04); the synthetic profiles carry `approved_by = synthetic-fixture`, which is a development
placeholder, not an approval.

## Versioning

A change is a new `configuration_version` with its own rows. **Live-schema caveat (read-only inspection 2026-11-04):** `task_templates` has the primary key `(template_key, client_id)` - it does NOT include `config_version` - so a second version's templates cannot coexist with the first; version 1 seeds cleanly, but publishing version 2 needs a database-owner decision (change the key, or retire and replace rows). The other versioned tables (`client_document_requirements`, `client_service_policies`, `client_milestone_templates`) have a composite foreign key `(client_id, config_version)` to `client_config_versions` (so the version row must be inserted first), and documents and milestones a UNIQUE `(client_id, config_version, key)`; `client_service_policies`' own unique constraint was not in the schema export, so the seeding code's idempotency checks are the protection there. Existing versions are never edited: seeding is
insert-only and a differing existing row is reported as a conflict. The previous version stays for audit; it ends by setting `effective_until`
(an approved action outside this tooling). A generated plan records the `configuration_version` it used.

## Retrieval (shared contract; implemented after seeding is approved)

Input: client id, role, grade, nationality (code), country, work location, employment type, effective date. Steps: resolve the client; pick the approved
version by date; validate effective dates, approval fields and hash; rebuild the document from the six section rows and validate it against the schema;
validate eligibility (role, grade, nationality group, location, employment type must be declared by the client; client eligibility rules); return a typed
object with the use-case sections and the employee-specific slices (applicable tasks, documents, items, programmes, policies). Errors are explicit
(`ClientNotFound`, `ClientInactive`, `ConfigurationExpired`, `ConfigurationNotApproved`, `ConfigurationInvalid`, `EmployeeNotEligible`); it never selects another
client and nothing is left for a Fake LLM to invent. Contract details: `CLIENT_CONFIGURATION_INTEGRATION_CONTRACTS.md`.

## UC-02 checklist flow (what exists today, unchanged)

UC-01 event -> case/offer/form read from Supabase -> `SupabaseConfigSource.load(client_id, joining_date)` reads the projected rows of the approved version
(`task_templates`, document requirements, assignments, milestones, holidays) -> dynamic checklist (`selection.py`) -> tasks, dependencies, owners, due dates ->
HR approval gate and document gate -> the configuration version is stored with the plan. Nothing per client is in source code; a test seeds the four profiles into
the mock database and the existing reader returns a different configuration for each. The new retrieval service adds the eligibility validation and the
other use cases' sections before this flow starts.

## Security

* Service-role access only from the server; no key, secret or address in a profile (schema + test scan).
* Seeding needs `UC02_ENV=development`, `UC02_ALLOW_CONFIG_SEEDING=1` and the target host re-typed; production is refused.
* RLS and foreign keys are not visible through PostgREST. The captured schema shows `client_id` foreign keys to `client_configs` on `client_config_versions`, `task_templates`,
  `client_holidays`, `role_access_matrix`; `client_service_policies` and `client_document_requirements` show none. RLS policy state still needs a database-owner check.

## Status of each phase

| Phase | State |
|---|---|
| 1 requirements audit and matrix | done (`CLIENT_CONFIGURATION_REQUIREMENTS_MATRIX.md`) |
| 2 design, schema, 4 profiles | done; schema validation passes; semantic validation passes |
| 3-4 database audit and mapping | done (`CLIENT_CONFIGURATION_DATABASE_MAPPING.md`); **no migration needed or proposed** |
| 5 dry run | done against Development, read-only: see the seeding guide |
| 6 seeding | **NOT DONE: waiting for approval** |
| 7-8 retrieval service, four-client live tests | not started (follow seeding) |
