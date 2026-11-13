# UC-01 integration status (2026-11-04)

Every status below is backed by code, tests or a recorded check. "In-memory"
means a check ran against UC-01's own in-memory repositories, not against a
database.

## Real and verified

| Item | Evidence |
|---|---|
| Domain flow: start gate, duplicate, rehire and transfer detection, case creation, form changes, discrepancies, HRIS prepare, approve and write, policy and SoD | UC-01 suite (`pytest`), 19/19 evals, `demo/full_flow.py`, all **in-memory** |
| Placement missing gives HELD `location_name_required`; nothing is invented | `tests/test_onboarding_entry.py`, `tests/test_mock_seed.py`, real-orchestrator run (intake BLOCKED) |
| "unacceptable" and "acceptable" are no longer read as an acceptance | `tests/test_email_intake.py` |
| Central LLM gateway: `GatewayLlm` over `hr_onboarding_common.llm`, offline and online, using a stand-in transport | `tests/test_llm_gateway.py`; see `docs/uc01-llm-gateway.md` |
| LLM draft text kept out of the audit log; refusals audited distinctly | `tests/test_llm_gateway.py`, `tests/test_llm.py` |
| Onboarding-document workflow: `.docx` read, extract, validate, then the existing case flow, **in-memory** | `tests/test_document_intake.py`; `scripts/mock_onboarding_documents.py run`; see `docs/document-extraction.md` |
| UC-01 → UC-02 end to end with the **database-first rule**: UC-02 is triggered only for an employee saved in and read back from Supabase. In-memory runs stop before UC-02; runs over UC-01's real Supabase adapters on a FAKE PostgREST reach UC-02 (10 tasks, UC-02 on local SQLite) | `tests/test_e2e_uc01_to_uc02.py`; `python -m demo.uc01_to_uc02_mock_e2e`. Fake-database runs are **MOCK PASS**; the live run (`data/mock/sample_employee_record.json`, `CLIENT_DUBAI`) is **BLOCKED** on the team encryption key (B1). |
| Supabase **read** connectivity to the configured dev project: URL, service-role auth, all 15 UC-01 tables exposed, `rpc/next_employee_no` present | Read-only probe (GET/HEAD only), 2026-11-04 |
| **Live (2026-11-05):** the populated SYNTHETIC form is uploaded to Storage (`Doc_Collection/SYN-UAE-TEST/uc01-intake/…`) and read back by SHA-256, with no duplicate on re-run. Extraction, validation and the budget run on it (COMPLETE, AED 21,500.00), and 5 UC-01 audit rows are written per run (chain intact). Case creation is refused **before any write** (B5 and B9). | `scripts/uc01_document_live_run.py run --live`; `docs/uc01-budget-and-persistence.md` §5 |

## Mocked or simulated

| Item | Stand-in |
|---|---|
| Email intake | `MockOutlookConnector` and the `/internal/inbound-email` simulator. There is no mailbox, webhook or poller. |
| Zoho People and Zoho Payroll | `StubHrisGateway` (`ext-<uuid>` refs). The target HRIS product is not decided. |
| Identity | `X-User-*` headers, accepted in every environment. There is no identity provider. |
| Offline LLM | `FakeLlm` (deterministic) |
| Mock employees | `data/mock/employees.json`, seeded **in-memory** only |

## Blocked: Supabase persistence (no employee can be stored yet)

No Supabase write was made. The work stopped at these dependencies.

| # | Needed | Owner | Why it blocks |
|---|---|---|---|
| B1 | **Encryption key.** Resolved for dev (2026-11-04, UC-01 owner's decision): ciphertexts are now **versioned** (v2 records the key id, bound into the authenticated data), retired keys stay usable for decryption (`UC01_FIELD_ENCRYPTION_PREVIOUS_KEYS`), and `scripts/create_dev_encryption_key.py` creates a `dev-...` key for synthetic data only. `Settings` refuses any `dev-` key outside `APP_ENV=dev`. The other developer's existing `SYN-UAE-TEST` row is untouched and stays readable with their key. | Team lead (production key: approved secret store) | Production still needs the managed key, its owner, backup and rotation schedule. |
| B2 | **Audit storage design (decision D4).** Live `audit_events` is shared: it holds 398 rows from other use cases, requires `client_id` (UC-01's `AuditEntry` has none, and request-level entries have no case), and cannot give UC-01's hash chain a gap-free sequence over PostgREST. The options are in `docs/uc01-supabase-decisions.md`, D4 (dedicated `uc01_audit_log` recommended). | DBA | Every UC-01 flow writes to the audit log first, so **nothing persists until D4 is decided.** |
| B3 | **Offers mapping.** Live `offers` requires `candidate_name`, `designation` and `offer_payload` (all NOT NULL). UC-01's `OfferRecord` has none of them. | HR data owner + DBA | Saving an offer would mean inventing values or changing the schema. |
| B4 | **Document store.** There is no table or storage bucket for case folders. Only `onboarding_cases.folder_ref` exists. | Product / platform | `create_case_folder` has nowhere real to create a folder. |
| B5 | **Case status vocabulary - CONFIRMED BLOCKER (2026-11-04).** `onboarding_cases_status_check` accepts at least `INTAKE_PENDING`, `DOCUMENTS_PENDING`, `ON_HOLD`, `WITHDRAWN`, `READY_FOR_DAY_ONE`, `COMPLETED`, `CANCELLED` and **rejects** UC-01's `HELD`, `PLAN_PENDING`, `PLAN_APPROVED`, `IN_PROGRESS`, `AT_RISK`, `ONBOARDED`, `CLOSED` (zero-write probes). The first live run failed with `CheckViolation`; no case, form or person row was written. Proposed additive fix: `docs/schema/proposed/b5_allow_uc01_case_statuses.sql` (keeps every existing value, adds UC-01's). | DBA (run in the SQL editor) | **No UC-01 case can be created in Supabase until it is applied.** |
| B6 | **Atomic writes (decision D6).** PostgREST runs one statement per call. Creating a case spans the case, form, person, approval and audit tables, so a failure part-way leaves a partial record. Options: psycopg with a restricted role, or a SQL function per operation. | DBA | Required by "never return success when persistence fails" and "no partially created employee records". |
| B7 | **Employee-number function body.** `rpc/next_employee_no(p_client_id)` exists, but its atomicity cannot be verified through PostgREST (decision D5). | DBA | Needed for unique, persistent employee numbers. |
| B9 | **Onboarding-form details and budget tables.** Reporting line, probation, visa terms, the documents checklist, personal details, insurance, benefits, responsibilities and the onboarding **budget** have no live column. **2026-11-05:** migration prepared, `docs/schema/proposed/20261105120000_uc01_form_details_and_budget.sql` (5 tables, not applied), and adapter `documents/supabase_store.py` (fake-PostgREST tested). The intake service checks these tables BEFORE opening a case, so the live run stops with nothing written (`docs/uc01-budget-and-persistence.md`). | DBA + HR data owner | Document-extracted details and the budget cannot be stored live. |
| B4 | **Update 2026-11-05:** a private bucket `Doc_Collection` now exists. UC-01 stores the received form there (interim, dev) at `<client>/uc01-intake/<id>.<ext>`, content-addressed and verified by SHA-256. **Live-tested** with the synthetic form. Registering it in `candidate_documents` is pending a decision: its type CHECK has no onboarding-form value, and the table belongs to UC-03. | DBA + UC-03 + UC-01 owner | Only the `candidate_documents` listing. |
| B8a | **Common test client.** `SYN-UAE-TEST` is the client both use: UC-02 accepts it (approved v1, 18 task templates) and it is in `client_configs`. Its missing `employee_number_counters` row (created after the 2026-11-03 counter seeding) was added on 2026-11-04 by the insert-only, idempotent `scripts/seed_employee_number_counters.py` (SQL: `supabase/migrations/20261104170000_seed_missing_employee_number_counters.sql`). `CLIENT_DUBAI` stays draft with 0 templates; its reviewed seed `docs/four_mock_clients_seed.sql` needs explicit approval and the SQL editor. | Configuration owner (CLIENT_DUBAI seed) | - |
| B8 | **Synthetic client row.** `onboarding_cases.client_id` has a foreign key to `client_configs`. The mock client `SYN-UC01-MOCK` would need a `client_configs` row, which is shared configuration. | Configuration owner | Without it every insert fails the foreign key. |

Already mappable once B1, B2 and B6 are decided: `uc01_approvals` (its
columns match the `Approval` model exactly; ids are UUIDs) and the case
columns, including the placement and rehire columns that now exist live.
The six `Unsupported*` adapters in `adapters/supabase/unsupported.py` are
still in place on purpose. They refuse loudly instead of losing data. Their
messages about missing *columns* are stale, because the live schema now has
those columns. The blockers that remain are the ones above.

## Blocked: orchestrator

| Item | Needed |
|---|---|
| Placement reaches UC-01 | An HR decision on whether `location` is `location_name`, then two shared-file edits. See `docs/uc01-placement-orchestrator-handover.md` (owner: Prasanna). Until then, orchestrator intake is BLOCKED (`location_name_required`), as designed. |
| UC-01 worker `.venv` | Missing on this machine, so the full-orchestrator tests skip unless `UC01_WORKER_PYTHON` is set. |

## Not production-ready

- **Authentication:** header identity with no environment gate (`api/identity.py`).
- **RLS:** the service-role key bypasses it, and the draft RLS is not applied.
- **Key management:** a single key with no rotation (`crypto.py`).
