# Supabase integration report: UC01–UC05 and the orchestrator

2026-11-06. Evidence-based: every claim below is backed by a test run or a live read-back named here. Vocabulary:
**VERIFIED** = written to and read back from the live Supabase project (`your-supabase-project-id`); **IMPLEMENTED** =
code with passing tests, not run live; **BLOCKED** = cannot work until the named key, row or decision exists;
**NOT IMPLEMENTED** = deliberately not built, with the reason. Design: `SUPABASE_RUNTIME_ARCHITECTURE.md`; tables:
`SUPABASE_UC_TABLE_OWNERSHIP.md`; per UC: `SUPABASE_UC_INTEGRATION_MATRIX.md`; stage contracts: `SUPABASE_END_TO_END_WORKFLOW.md`.

## 1. Summary

| Part | Result |
|---|---|
| Orchestrator case state, approvals | VERIFIED (previous change), unchanged |
| Orchestrator typed handoffs (UC01Output–UC05Output) | IMPLEMENTED: in every UC stage outcome, persisted in `case_orchestration_state` |
| Orchestrator joining-day jobs | VERIFIED: `joining_day_jobs` claim, completion, read-back by a new store, cross-tenant refusal |
| UC01 | **BLOCKED** on `UC01_FIELD_ENCRYPTION_KEY` (decision B1). Form-id type mismatch fixed |
| UC02 | VERIFIED (previous change), unchanged |
| UC03 | VERIFIED: Storage + `candidate_documents`, collection + revisions and case state in `uc03_case_state`, `document_validations`, audit, reload by a new process, cross-tenant refusal |
| UC04 state | VERIFIED: versioned snapshot in `audit_events`, read back by a new process; parent case from `onboarding_cases` |
| UC04 evaluation in Supabase mode | **BLOCKED** on configuration: the `uc04_equipment_access` section exists, its projection to UC04's CaseConfig is an open UC04 decision (D-1/D-2) |
| UC04 provisioning tables | NOT IMPLEMENTED: the tables' vocabulary does not hold UC04's item states and kinds (section 9) |
| UC05 | VERIFIED: employee from `onboarding_cases`, induction in `orientation_sessions`, `session_participants`, `approval_requests`, `audit_events`, `agent_runs`; cross-tenant refusal |
| `database/` folder | Not present in the repository; no runtime path reads `database/data` or any CSV (section 7) |

## 2. Before the work: the in-progress merge

At 01:17 a merge of `document-information-validation` (UC03's own Supabase persistence) into `Prasanna` was started
and left with ~130 conflicted files. On your instruction it was resolved -- by the other Claude session (project-d1 /
project-f2), which was already doing it: every conflict resolved and **staged, not committed**. This work was done on
top of that staged resolution as **unstaged** working-tree changes (section 11), so committing the merge does not pick
them up. Before this work: Common 809 passed / 1 failed / 1 skipped -- the failure
(`test_onboarding_document_extraction::test_from_document_to_uc02_through_the_fully_wired_orchestrator`) also fails on
the clean `Prasanna` HEAD.

UC03's merged persistence (`UC03_Document_Validation/integrations/supabase`: case store, audit store, result store,
value mapping) is reused here, not duplicated.

## 3. UC01 — BLOCKED (exact dependency)

| Item | State |
|---|---|
| Code | UC01's Supabase adapters exist and are wired in interim mode (`UC01_SUPABASE_INTERIM_WRITES=1`) |
| Missing | `UC01_WORKER_ENV__UC01_FIELD_ENCRYPTION_KEY` (the team's key; UC01 refuses to start Supabase mode without it, and generating, reusing or disabling it is forbidden by decision B1). `UC01_WORKER_ENV__SUPABASE_URL` is commented out for the same reason |
| Effect | Every new case stops after the form approval with `CASE_NOT_IN_DATABASE`: every case table's `case_id` is a foreign key to `onboarding_cases`, which only UC01 writes |
| Fixed now | The orchestrator sent `form_id = "FORM-CASE-<hex>"`; `onboarding_forms.form_id` is a uuid, so UC01's form write would have failed after the case row and an employee number were taken. It now sends a uuid derived from the case id |
| Also needed for UC01 in Supabase mode | an `employee_number_counters` row per tenant (CLIENT-ALPHA has none), a `client_configs` row per tenant |

## 4. UC03 — VERIFIED

Changes: the orchestrator builds UC03's table client from its own scoped settings (`uc03_supabase.py`) and hands it to
UC03's `build_stores`, so a bare `SUPABASE_URL` never reaches the UC01 worker. UC03's document collection moves from
SQLite to `uc03_case_state` (`collection_registry.py`, row `collection:<case>`, compare-and-swap on `version`). The
upload writes the table's vocabulary (`UPLOADED`, UC03's type mapping; it wrote `RECEIVED` and lower-case types, both
refused by the CHECK constraints). In Supabase mode: a type the table cannot hold is refused before anything is stored
(422 `DOCUMENT_TYPE_NOT_IN_SCHEMA`); a missing metadata row refuses the upload (409 `CASE_NOT_IN_DATABASE` / 503); a
submitted document whose validation result was not stored blocks the stage (`UC03_RESULT_NOT_PERSISTED`, owner and fix
named); a UC03 case stored under another tenant fails the stage (`TENANT_CONTEXT_CONFLICT`).

Live (case `e548a40d-225c-4b0c-9857-b986944f5ab0`, tenant SYN-UAE-TEST, `scripts/verify_supabase_runtime.py uc03`):

| Row | Read back |
|---|---|
| Storage `Doc_Collection/SYN-UAE-TEST/e548a40d…/<document_id>.pdf` | created |
| `candidate_documents` | 1 row, `PASSPORT`, `VALIDATED` (set by UC03's result store) |
| `uc03_case_state` | 2 rows: `collection:e548a40d…` (`DOCUMENT_COLLECTION`, version 2) and `e548a40d…` (UC03 status `INCOMPLETE`, version 2) |
| `document_validations` | 1 row: `compliance_item_status=NON_COMPLIANT`, `uc03_compliance_status=BLOCKED`, `hr_review_status=PENDING` |
| `audit_events` | `DOCUMENT_VALIDATED` |
| new process | collection plan version 1, passport revision 1, UC03 case tenant SYN-UAE-TEST, received revisions `{passport: 1}` |

The synthetic file has no readable fields, so UC03 judges it non-compliant -- the point is the persistence path.
Replanning (UC03 → UC02) is triggered only by a planning-impacting change; this is unchanged and covered by the
existing replanning tests.

## 5. UC04 — state VERIFIED, evaluation BLOCKED on configuration

Changes: `AuditEventStateBackend` (UC04 `src/sub_agent/persistence/audit_state.py`) implements UC04's existing
`StateBackend` port on `audit_events` (one append-only snapshot per save; `event_id` from case, version and sequence, so
two writers of the same version collide on the primary key; no `uc04_*` table). The UC04 worker in
`UC04_STATE_MODE=supabase` runs UC04's own database adapters: parent case from `onboarding_cases`, state snapshots,
audit entries, agent runs; it refuses to start without the PLATFORM identity profile or its settings. A soft-deleted
case (`is_deleted`) is now WITHDRAWN for UC04 (it was ACTIVE). `PLAN_PENDING`, a live status UC04 did not know, maps to
ACTIVE like the other in-progress stages.

`UC04_CONFIG_SOURCE=supabase`: no synthetic file is read. The orchestrator reads the tenant's `uc04_equipment_access`
section from `client_service_policies` and blocks:

| Case | Blocker | required_table / required_record / required_configuration |
|---|---|---|
| Section exists (CLIENT_DUBAI, CLIENT_CANADA, CLIENT_INDIA, CLIENT_USA: `config_version` 2) | `UC04_CONFIG_PROJECTION_UNDECIDED` | `client_service_policies` / `client_id=<tenant>, service_category=cfg_section, service_key=uc04_equipment_access, is_active=true` / approved projection to UC04's CaseConfig (decisions D-1/D-2) |
| No section (e.g. SYN-UAE-TEST, CLIENT-ALPHA) | `UC04_CONFIGURATION_MISSING` | same table and record |
| Supabase unreachable | `UC04_CONFIG_UNAVAILABLE` (retryable, ops) | — |

Live (`verify_supabase_runtime.py uc04`): the worker started in Supabase mode; parent case read from `onboarding_cases`
(employee `UC01DEMO-e548a40d`, SYN-UAE-TEST, ACTIVE); snapshot version 1 written (`audit_events`, `UC04_CASE_STATE`,
event `e163bcee…`) and read back by a new process.

## 6. UC05 — VERIFIED

Changes: the worker runs UC05's own Supabase mode (`UC05_WORKER_ENV__UC05_DATABASE_MODE=supabase`; the three
per-feature sources are removed from `.env`). Fixed in the worker: three repository calls without a tenant (they raised
`TenantScopeRequiredError`) now use UC05's tenant-bound view; the simulated-calendar realignment no longer crashes the
worker at start in Supabase mode. Fixed in UC05's repository: placeholder buddy profiles (`<colleague full name>`,
`sample`, …) are never offered or assigned; participants without an e-mail are recorded on the session instead of being
written with `email = NULL`, which the live table refuses (decision D11). The orchestrator's schedule now carries
session id, type, status, start/end (UTC), timezone, meeting link and location (None when UC05 holds none), and the
welcome's `delivery_status` (`SENT_SIMULATED` for UC05's simulated notifier, `SENT` for a real provider).

Live (`verify_supabase_runtime.py uc05`, case `e548a40d…`):

| Step | Result |
|---|---|
| preflight | employee `UC01DEMO-e548a40d` read from `onboarding_cases`, no missing field, joining date 2026-11-25 |
| offer_accepted | case linked (`UC05_CASE_LINKED`), stage `induction_awaiting_approval` |
| `orientation_sessions` | `9e4aa2a5…` `HR_INDUCTION`, `SCHEDULED`, 05:00–06:30 UTC, link and room NULL |
| participants | new hire: `session_participants` row (`NEW_HIRE`, `PENDING`); team lead `SYN-MGR-001`: recorded on the session (`MANAGER`, `EMAIL_UNKNOWN`) |
| `approval_requests` | `apr-cb69acb78dfc`, `uc05_orientation_scheduling`, `scheduling_approval`, `PENDING` |
| `agent_runs` | `run-5f9d6b96a655` `employee-onboarding` `acted` (UC05 leaves `case_id` empty on its runs) |

Not exercised live: buddy assignment (SYN-UAE-TEST has no `buddy_profiles` row) and the welcome (the joining day is in
the future). Both are covered by UC05's Supabase tests (fake PostgREST that now also enforces NOT NULL columns).

## 7. Runtime dependency classification

No runtime code reads a CSV, `database/data`, or the root `client_config_versions_rows.csv`. What remains outside
Supabase in the Supabase configuration, and why:

| Occurrence | Classification | Reason |
|---|---|---|
| Orchestrator `provisional_case_states.db` (SQLite) | Required local | A case before UC01 has written it has no `onboarding_cases` row; the foreign key forbids storing it |
| Orchestrator LangGraph `checkpoints.db`, UC05 `checkpoints.db` (SQLite) | Execution cache | The authoritative state is in Supabase; a lost checkpoint re-runs from it |
| Orchestrator HITL inbox `hitl.db` (SQLite) | Local, mirrored | Every change is written through to `approval_requests` |
| UC05 `uc05_jobs.db`, JSONL audit copy | Local | UC05's own job leases and a local audit copy; its audit and ledger are in `audit_events` |
| UC05 `simulated_calendar.json`, `MockCalendar`, `MockNotifier` | Simulation of an external provider | No real calendar or mail provider exists in this project; a simulated send is reported as `SENT_SIMULATED` |
| UC04 `MockITServiceDesk`, `case_context_mock`, `approval_mock`, `slot_availability_mock` | Mock (memory mode only) / simulation | In Supabase mode the case comes from `onboarding_cases`; the IT desk is a simulation (no real desk integration) |
| UC04 synthetic configuration files (`config/uc04`, `tenants.yaml`) | Not used in `UC04_CONFIG_SOURCE=supabase` | Used only with `UC04_CONFIG_SOURCE=file` |
| UC03 `SqliteDocumentRegistry` | Not used in `UC03_STORE_MODE=supabase` | Memory/SQLite only for the local modes |
| UC02 `sqlite_store.py`, UC01/UC02/UC03 in-memory stores, Common/UC05 `connectors/*/mock.py` | Test / local modes | Selected only by their non-Supabase settings |
| `InMemory*` classes in `documents.py`, `case_store.py`, `joining_day_scheduler.py` | Tests and memory mode | Never selected in the Supabase configuration |

## 8. Settings

| SETTING | DEFAULT | CURRENT VALUE (HR-Onboarding-Common/.env) | CONSUMED BY |
|---|---|---|---|
| `ORCHESTRATOR_STATE_STORE` | memory / sqlite | `supabase` | bootstrap.py (case state) |
| `ORCHESTRATOR_SUPABASE_URL` / `_SERVICE_ROLE_KEY` / `_SCHEMA` | — | set (key not shown) / `public` | case state, UC03 table client, UC04 configuration reader, joining-day jobs |
| `ORCHESTRATOR_JOINING_DAY_STORE` | `supabase` when the state store is, else `sqlite` | (default → supabase) | joining_day_scheduler.build_job_store |
| `UC02_WORKER_ENV__UC02_STORE_MODE` (+ `UC02_ENV`, `ALLOW_SYNTHETIC_CONFIG`, Supabase settings) | memory | `supabase` | UC02 (in process) |
| `UC03_STORE_MODE` | `memory` | `supabase` | adapters/uc03_documents.py, documents.py (strict upload) |
| `DOCUMENT_STORAGE_BUCKET` / `_SUPABASE_URL` / `_SERVICE_ROLE_KEY` | — | `Doc_Collection` / set | documents.py (Storage + candidate_documents) |
| `UC04_IDENTITY_PROFILE` | LOCAL_SIMULATION | `PLATFORM` | UC04 worker |
| `UC04_WORKER_ENV__UC04_STATE_MODE` | `memory` | `supabase` | UC04 worker (uc_worker_runtime._Uc04Handler) |
| `UC04_WORKER_ENV__SUPABASE_URL` / `_SERVICE_ROLE_KEY` | — | set (copied from UC05's worker settings) | UC04 worker |
| `UC04_CONFIG_SOURCE` | `file` | `supabase` | adapters/uc04_equipment_access.py |
| `UC05_WORKER_ENV__UC05_DATABASE_MODE` | local | `supabase` | UC05 worker; bootstrap (offer event = the case) |
| `UC05_WORKER_ENV__SUPABASE_URL` / `_SERVICE_ROLE_KEY` | — | set | UC05 worker |
| `UC05_OFFER_EVENT_SOURCE`, `UC05_BUDDY_SOURCE`, `UC05_WELCOME_CONFIG_SOURCE` | — | commented out (replaced by UC05's Supabase mode; the bootstrap refuses the combination) | — |
| `UC01_WORKER_ENV__SUPABASE_URL`, `UC01_WORKER_ENV__UC01_FIELD_ENCRYPTION_KEY` | — | commented out / **not set** | UC01 worker (BLOCKED) |

The service-role key reaches only backend processes: workers receive only their own scoped copy
(`workers/config.child_environment` removes the orchestrator's and the document store's keys); the browser calls the
backend API only. `/healthz` reports the modes (`stores`), never a URL or key.

## 9. Not implemented, with the exact reason

| Item | Why | What would unblock it |
|---|---|---|
| UC04 → `provisioning_requests`, `provisioning_tickets`, `equipment_handovers` | `item_kind` accepts HARDWARE / SOFTWARE_ACCESS / PHYSICAL_BADGE / SYSTEM_PERM (UC04: EQUIPMENT / ACCESS); `state` accepts 8 values, refusing 14 of UC04's 16 item states; `requested_by`, `payload` NOT NULL; `role_access_matrix` (which could supply the kind) has 0 rows | A UC04-owner mapping decision (state and kind), or seeded `role_access_matrix` rows |
| `candidate_documents` for `experience_letter`, `payslips`, family documents | No value in the `document_type` CHECK | Schema owner widens the CHECK |
| `session_participants` rows for team lead / buddy | `email` NOT NULL; no table holds their address | An employee directory with work e-mails, or a nullable column |
| UC05 `agent_runs.case_id` | UC05 records runs per event without the case | UC05 change (small) |

## 10. Migrations inventory

| File | Status | Evidence |
|---|---|---|
| UC01 `supabase/migrations/20261103120000_uc01_local_baseline.sql` | LOCAL ONLY (local Supabase baseline) | header |
| UC01 `docs/schema/proposed/20261105120000_uc01_form_details_and_budget.sql` | APPLIED (header still says proposed) | live tables `uc01_onboarding_*` exist with rows |
| UC01 `docs/schema/proposed/d4_d5_additive.sql` | UNVERIFIED | its functions are not visible in the captured API description |
| UC01 `docs/schema/proposed/uc01_gaps_complete.sql` | DUPLICATE (consolidates d4_d5_additive + gaps) | header |
| UC01 `docs/schema/proposed/uc01_b2_b4_b7_b9_draft.sql`, `docs/schema/uc01_draft.sql` | NOT APPLIED / OBSOLETE drafts | headers |
| UC02 `supabase/migrations/20261027090000_uc02_init.sql`, `…090100_uc02_rls.sql` | NOT APPLIED, OBSOLETE (UC02 uses the shared public tables) | no `uc02` tables in the live API |
| UC03 `supabase/migrations/20261104120000_uc03_persistence.sql` | APPLIED (header says not applied) | live `uc03_case_state` and the `uc03_*` columns of `document_validations` exist |
| UC04 `docs/proposed_migrations/uc04_state_tables.sql` | NOT APPLIED; superseded by the `audit_events` backend | no `uc04_case_state` in the live API |
| `consolidated_database_schema.sql` (root) | REFERENCE (schema export from the merge) | — |
| `docs/four_mock_clients_seed.sql`, `docs/manual_seed_four_synthetic_clients.sql`, `docs/four_mock_clients_dry_run_readonly.sql` | SEED / read-only check (applied to the project: CLIENT_* version 2 approved) | live `client_config_versions` |
| This change | **No migration.** No table or column was created or altered | — |

## 11. Tests

| Suite | Result |
|---|---|
| HR-Onboarding-Common | 849 passed, 1 skipped, 1 failed -- the failure already present before the merge (`test_onboarding_document_extraction::test_from_document_to_uc02_through_the_fully_wired_orchestrator`, which also fails on the clean `Prasanna` HEAD). Before this work: 809 passed |
| UC02 | 1584 passed, 10 skipped (no `database/` folder present) |
| UC03 | 252 passed |
| UC04 | 704 passed, 1 skipped; ruff (documented scope) clean |
| UC05 | 671 passed, 3 skipped |
| UI | `tsc --noEmit` clean |

New tests: `test_uc03_supabase_mode.py` (collection in `uc03_case_state` across a restart, results / case state / audit
stored, unstored result blocks, tenant refusal, strict upload, PostgREST client errors), `test_uc04_uc05_supabase_modes.py`
(UC04 configuration source, worker startup refusals, SENT_SIMULATED, full schedule), `test_joining_day_jobs_supabase.py`
(claims, CAS, tenant, restart), `test_handoff_contracts.py` (contracts, no personal data); UC04
`test_audit_state_backend.py` (same decisions as the in-memory store, restart, CAS, tenant) and deleted-case test; UC05
placeholder-buddy and participants-without-e-mail tests.

The conftest now removes the worker-scoped settings it used to pin empty (a scoped value wins inside the worker, so an
empty pin silently defeated tests that set the plain name). UC04 tests that draw synthetic approval ids restore UC04's
global fixture counter: a direct call's `run_id` is a digest of its input, and for some counter values that digest
(`direct-00…`) reads as a phone number to UC04's privacy scanner -- a latent fragility in UC04, reported here.

## 12. Rows the verification left in the shared project (all synthetic, tenant SYN-UAE-TEST)

| Case | Rows | Note |
|---|---|---|
| `e548a40d…` | Storage object, `candidate_documents`, `document_validations`, `uc03_case_state` ×2, `audit_events`, UC04 snapshot, `orientation_sessions`, `session_participants`, `approval_requests` (`apr-cb69acb78dfc`, pending), `agent_runs`, `joining_day_jobs` | the full verification |
| `62e3b13e…` | the same UC03 / UC04 rows, and a **partial UC05 induction** (`orientation_sessions` + the new hire's participant, no approval) from the run that hit the `email` NOT NULL bug | UC05's writes are not atomic; this session row has no approval. Left as is (nothing deleted without your decision) |
| `77bf3f7d…` (soft-deleted case) | UC03 rows and a UC04 snapshot | written before the deleted flag was noticed; this led to fix D12 |

## 13. Still open

1. `UC01_FIELD_ENCRYPTION_KEY` (B1): the one setting that turns new cases into `onboarding_cases` rows. Until it is
   set, every new case stops at `CASE_NOT_IN_DATABASE`.
2. UC04 configuration projection (D-1/D-2) and the provisioning-table mapping.
3. `candidate_documents.document_type` CHECK; an e-mail source for team leads and buddies.
4. Rotate the service-role key that is in git history (commit `c6cf80e`, root `.env.example`), as noted in the previous change.
5. The staged merge is not committed; review it, then commit it and these changes separately.
