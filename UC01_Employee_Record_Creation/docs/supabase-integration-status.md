# UC-01 Supabase integration status (2026-11-04)

**Writes are paused. UC-01 has written no row, file or object to Supabase.**
Every result below comes from read-only GET/HEAD requests to the
configured dev project. No secret or row value was printed.

## Verified read-only

| Check | Result |
|---|---|
| Project URL and service-role authentication | HTTP 200 |
| UC-01 tables exposed | All 15. Row counts: `onboarding_cases` 12, `onboarding_forms` 1, `offers` 7, `uc01_approvals` 0, `audit_events` 398, `person_records` 6, `employee_number_counters` 5, `candidate_documents` 0, `document_validations` 0 |
| `rpc/next_employee_no(p_client_id)` | Present. The function body cannot be seen through PostgREST (B7). |
| Storage buckets (`/storage/v1/bucket`) | **None exist** |
| `client_configs` row `SYN-UC01-DOC-TEST` / `SYN-UC01-MOCK` | **Absent** (B8) |
| `UC01_FIELD_ENCRYPTION_KEY` locally | **Missing** (B1) |
| Existing encrypted form | 1 row (client `SYN-UAE-TEST`, created by another developer under a key that is not on this machine) |
| CHECK constraints, RLS policies, triggers | **Not verifiable** through PostgREST. They need the DBA or SQL access (B5, B6). |

Developer-side fixes, the client-configuration finding, the key procedure
and the B2/B6 options are in `docs/supabase-dev-setup.md`.

## Implemented but not wired (verified against the fake PostgREST only)

| Adapter | Live target | Why it is not wired |
|---|---|---|
| `SupabaseApprovalRepository` | `uc01_approvals` (columns match the model) | No flow completes until the audit store (B2) exists |
| `SupabaseCaseRepository` | `onboarding_cases` (`variant_is_resident` rename; explicit NULLs; attributes array) | No approved status mapping (B5); writes are not atomic (B6) |
| `SupabaseEmployeeNumberGenerator` | `rpc/next_employee_no` | Concurrency safety of the live function is unverified (B7). Allocating live numbers in flows that then fail at the audit step would consume counter values for nothing. |

Draft DDL for B2, B4, B7 and B9 is in
`docs/schema/proposed/uc01_b2_b4_b7_b9_draft.sql`. It is **not applied**.

## What works without Supabase

The full document workflow runs against UC-01's in-memory repositories:
read, extract, validate, then create the case with an employee number,
approvals and audit. So does `scripts/seed_mock_employees.py`. Records
created that way **exist only for the life of the process**.

## Blocking decisions

Details, options and owners are in `docs/uc01-supabase-decision-request.md`.

| # | Decision | Owner |
|---|---|---|
| B1 | One shared dev encryption key | Team lead |
| B2 | Audit storage: the shared `audit_events` table, or a dedicated table. **Blocks all writes.** | DBA |
| B3 | `offers` NOT NULL columns that UC-01 cannot fill | HR data owner + DBA |
| B4 | Document storage. No bucket exists, so the source file cannot be stored. | Product / platform |
| B5 | `onboarding_cases.status` vocabulary and CHECK constraint | DBA |
| B6 | All-or-nothing writes (SQL function vs psycopg) | DBA |
| B7 | `next_employee_no` concurrency and grants | DBA |
| B8 | Synthetic client rows (`SYN-UC01-DOC-TEST`) in `client_configs` | Client-configuration owner |
| B9 | **New.** A table for the onboarding-form details with no live column: reporting line, probation, visa terms, document checklist, personal details, insurance, benefits and responsibilities. See `docs/employee-record-mapping.md`. | DBA + HR data owner |

## Once the decisions arrive

1. Implement the six `Unsupported*` repositories, plus the B9 table if it is
   approved.
2. Run them against the fake PostgREST.
3. Share the DDL for DBA review; the DBA applies it.
4. Run the acceptance test:
   1. Process `data/mock/documents/SYNTHETIC_onboarding_complete.docx`.
   2. Insert the case.
   3. Read it back by `case_id` and by `employee_no`.
   4. Compare it with the extraction.
   5. Restart the process and read it again.
5. Report persistence as verified only after all of that has actually
   succeeded.
