# UC-01: onboarding budget, field mapping and Supabase persistence

Status as of 2026-11-05. Covers the onboarding-form document flow:

```
SYNTHETIC populated form ─► Supabase Storage ─► extraction (.docx / text .pdf) ─► normalisation
  ─► validation ─► budget extraction ─► budget calculation ─► employee record (case, form, person)
  ─► details + budget persistence ─► read-back verification ─► audit ─► (UC-02, only after verification)
```

Code: `documents/budget.py` (budget), `documents/extraction.py` (rows, supplementary
rows, synthetic marker), `documents/persistence.py` (record, in-memory store,
read-back comparison), `documents/supabase_store.py` (Supabase tables + Storage),
`documents/service.py` (flow). Scripts: `scripts/synthetic_onboarding_form.py`
(populated copy), `scripts/uc01_document_live_run.py` (live run).

## 1. The synthetic populated form

The original "Onboarding form - 2026" is a blank template in
`Doc_Collection/CLIENT-TEST-A/Onboarding form - 2026 (1).docx` (SHA-256
`99939e02…35dc`, 2,671,313 bytes). It is **only read**, never written.
`scripts/synthetic_onboarding_form.py` copies it and changes `word/document.xml`
only:

- a banner, "SYNTHETIC TEST DATA - NOT A REAL EMPLOYEE";
- the empty value column of each template row filled with fictional values (it
  refuses to overwrite a filled cell);
- a separate, labelled table for details the template has no row for: email,
  department, nationality, work location, grade, employment duration;
- "(eg given below)" removed from the Roles and Responsibilities heading, so the
  listed duties are confirmed duties rather than the template's example.

The label column, the printed rate card, styles and fonts are unchanged. The
output is deterministic:
`data/mock/documents/SYNTHETIC_onboarding_form_2026_populated.docx` (SHA-256
`606eaee5…a1c8`), plus a text-based `.pdf` built from the populated `.docx`.
Both extract identically (`tests/test_synthetic_onboarding_form.py`).

## 2. Budget: selected expenses vs reference rates

| | What it is | Counted? |
|---|---|---|
| **Selected expense** (`BudgetLine`) | An amount HR wrote in the value column for this employee | Yes, when `EXTRACTED` or `CALCULATED` |
| **Reference rate** (`ReferenceRate`) | A price the template prints (e.g. `2 YRS – OUTSIDE COUNTRY: AED 4800/-`) | **Never.** It is shown next to the matching selection as information only |

Where each category comes from:

| Category | Template row(s) | Line key(s) |
|---|---|---|
| VISA | "1 year or 2 years visa", "Visa – visit + entry permit" | `visa.fee`, `visa.non_sponsored_id_card`, `visa.visit.fee` |
| INSURANCE | "Insurance for Employee", "Insurance for Family" | `insurance.employee.premium`, `insurance.family.premium`, `*.maternity` |
| (selection) | "Insurance" | `insurance.plan.selection`: the plan only. Any amount here is recorded as `stated_rate` and **not counted**, so a premium cannot be counted twice |
| TRAVEL | "Annual ticket – Employee / Family" | `travel.employee.ticket`, `travel.family.ticket` (+ sector and travel dates as details) |
| ACCOMMODATION | "Hotel stay …" | `accommodation.hotel.budget` (+ location and nightly rate) |
| OTHER | "Any other promises made to the candidate" | `other.<label>` per amount |

Provenance of a line: `EXTRACTED`, `CALCULATED` (a nightly rate × the stated
number of nights, nothing else), `NOT_APPLICABLE` (HR wrote N/A, None, Nil),
`INFERRED_NOT_APPLICABLE` (hotel row empty and the visa is processed inside the
country: the template says the hotel applies only to staff outside the UAE),
`MISSING`, `INVALID` (unsupported currency), `UNCERTAIN` (two values in one row,
an amount without a currency, a nightly rate without nights). A recurring
amount in "other promises" (for example "AED 500 per month") is kept with the
stated amount but not counted, because it is a term of employment, not a
one-off onboarding expense.

### Calculation rules

- Only `EXTRACTED` and `CALCULATED` lines are summed, per category and per currency.
- Amounts in different currencies are **never added** unless the caller supplies
  a rate for every foreign currency (`conversion_rates` + `base_currency`). The
  rates used are stored with the budget. No rate is looked up or assumed.
- An amount without a currency is never assumed to be AED.
- Status: `COMPLETE` (every required row is resolved and there is one currency),
  `PARTIAL` (something is missing, invalid, uncertain, or in a second currency
  without a rate; what is stated is still totalled), `UNAVAILABLE` (no counted
  amount, for example on the blank template).
- Required rows for `COMPLETE`: visa, employee and family insurance, employee
  and family ticket, hotel. The visit-visa and other-promises rows are optional.
- A selection that differs from the rate card is listed in
  `reference_differences`. The selected amount is the one used.
- `INVALID` or `UNCERTAIN` budget lines become HR review reasons
  (`budget:invalid:<line>`). A missing or partial budget does **not** block the
  employee record; it is stored with its status.
- The salary is never part of the budget and is never parsed or changed.

Result for the synthetic employee: **COMPLETE, AED 21,500.00**: visa 4,800 ·
insurance 8,000 · travel 3,550 · accommodation 3,150 (450 × 7 nights) · other
2,000. Every selection equals its rate-card price.

## 3. Field → table.column

**Live tables** (written by the unchanged `open_onboarding_case` flow):

| Value | Table.column |
|---|---|
| Employee name | `onboarding_cases.candidate_name`, `onboarding_forms.candidate_name`, `person_records.full_name` |
| Email (caller's value; the document's value if the caller gives none) | `onboarding_cases.candidate_email`, `onboarding_forms.candidate_email`, `person_records.email` |
| Designation | `onboarding_forms.designation` |
| Joining date | `onboarding_cases.joining_date`, `onboarding_forms.joining_date` |
| Final salary (verbatim, never changed) | `onboarding_forms.salary_and_terms_encrypted` (AES-256-GCM, AAD = `form_id`) |
| Employee number | `onboarding_cases.employee_no` (`rpc/next_employee_no`) |
| Work location (caller's `location_name`; **never** taken from the document, decision D3) | `onboarding_cases.location_name` |
| Department, grade (caller's value; the document's value only when the caller leaves it empty; a difference is a review conflict) | `onboarding_cases.department`, `onboarding_cases.grade` |
| Form approval (caller attestation) | `onboarding_forms.status`, `approved_by`, `approved_at` |
| Audit | `audit_events` (interim; `detail.origin = "uc01"`, hash chain) |

**Proposed tables** (migration `docs/schema/proposed/20261105120000_uc01_form_details_and_budget.sql`, **not applied**):

| Value | Table.column |
|---|---|
| Reporting to, probation, visa years and location, visit / direct entry permit, insurance texts, tickets, hotel days and text, other promises, R&R summary, issuing entity | `uc01_onboarding_form_details.*` (typed columns with CHECKs) |
| Nationality, employment duration, work location as written on the form | `uc01_onboarding_form_details.nationality`, `employment_duration`, `work_location_on_form` (nationality is **not** `nationality_group`) |
| Father's / mother's name, mobile number | `uc01_onboarding_form_details.personal_details_encrypted` (one ciphertext, AAD `<form_id>:personal_details`) |
| Marital status | `uc01_onboarding_form_details.marital_status` |
| Responsibilities | `uc01_onboarding_form_details.responsibilities` (text[]) + `responsibilities_is_example` |
| Source file reference | `uc01_onboarding_form_details.source_document_id`, `source_bucket`, `source_object_path`, `source_document_sha256`, `source_size_bytes`, `source_mime_type` |
| Synthetic vs real | `uc01_onboarding_form_details.data_origin` (`SYNTHETIC` / `DOCUMENT`) |
| HR review confirmation | `review_confirmed_by`, `review_confirmed_at` |
| Per-field status (present / missing / invalid / uncertain + source cell) | `uc01_onboarding_form_field_status` |
| Documents checklist, as stated (never validated) | `uc01_onboarding_document_checklist` |
| Budget totals | `uc01_onboarding_budget.visa_budget_amount`, `insurance_budget_amount`, `travel_budget_amount`, `accommodation_budget_amount`, `other_expenses_amount`, `total_onboarding_budget`, `budget_currency`, `budget_status`, `status_reasons`, `category_totals`, `totals_by_currency`, `conversion`, `calculation_version` |
| Reference rate card (as printed on this form version) | `uc01_onboarding_budget.reference_rates` (jsonb, reference only) |
| Each selected expense | `uc01_onboarding_budget_lines` (amount numeric(14,2) + currency + original text + provenance + `counted` + matching reference rate) |

Migration safeguards: numeric(14,2) with a currency next to every amount; a
`counted` line must have an amount and an `EXTRACTED`/`CALCULATED` provenance;
`COMPLETE` requires a currency and a total; one details row per (client, file
SHA-256), so a resubmitted file cannot create a second employee or budget;
UPDATE that changes a value, and DELETE, are refused by trigger (an identical
retry is allowed); RLS is enabled and forced. Rollback steps are in the file.

**Not stored in `candidate_documents`.** That table is UC-03's (validated
documents). Its type CHECK accepts e.g. `PASSPORT`, `OFFER_LETTER`, `VISA`,
`NATIONAL_ID` (zero-write probes, 2026-11-05), but no onboarding-form value, and
UC-01's structural tests forbid it from writing document types. Mapping the form
onto `OFFER_LETTER` would be lossy, so it is not done. The source file's
metadata is kept on the details row instead. An optional, commented-out CHECK
extension is at the end of the migration if the DBA, UC-03 and the UC-01 owner
decide otherwise.

## 4. Idempotency and failure handling

- Storage: the object path is content-addressed
  (`<client>/uc01-intake/<uuid5(client, sha256)>.<ext>`). A re-run finds the
  object and does not upload again. Every store is proven by downloading the
  object and comparing its SHA-256. An unreadable or unsafe file is never stored.
- The same file for the same client gives `LINKED_EXISTING` (`duplicate_document`)
  with the first case. Nothing new is written.
- A corrected file for the same employee gives `LINKED_EXISTING` through the
  existing duplicate check. The stored budget is never replaced.
- The details store is checked **before** the case is opened. A missing table
  stops the flow with `PERSISTENCE_FAILED` and nothing is written.
- After the case: details and budget are saved, then everything is read back
  (details and budget compared field by field; case, form with the decrypted
  salary, and person record). Only then is `persistence_verified` true. Any
  failure or mismatch is `PERSISTENCE_FAILED`. Success is never reported for
  unverified data.
- Known limit (DBA decision B6): PostgREST writes are not one transaction. A
  failure after the case insert leaves the case without its details. The flow
  reports it as `PERSISTENCE_FAILED`, audits the stage, and never hides it.

## 5. Status (2026-11-05)

| Capability | Classification | Evidence |
|---|---|---|
| Synthetic populated form (.docx + .pdf) from the real original | Implemented and tested | `tests/test_synthetic_onboarding_form.py`; original SHA unchanged after the run |
| DOCX and text-PDF extraction of every template field, the supplementary rows and the synthetic marker | Implemented and tested | same + `tests/test_document_pdf.py` |
| Budget extraction, rate card, calculation, multi-currency rules | Implemented and tested | `tests/test_onboarding_budget.py` (29 tests) |
| Upload of the synthetic form to Supabase Storage, with read-back by SHA-256 and no duplicate upload | **Implemented and live-tested** | live run: `Doc_Collection/SYN-UAE-TEST/uc01-intake/a7db828e-0edd-5c0a-8336-ec09e69ca242.docx`; second run reported `already_stored` |
| Extraction, validation and budget on the live run | **Implemented and live-tested** | live run: completeness 1.0, no errors or review reasons, COMPLETE AED 21,500.00 |
| UC-01 audit entries for the document flow | **Implemented and live-tested** | 5 rows per run in `audit_events` (DOCUMENT_RECEIVED, DOCUMENT_STORED, DOCUMENT_EXTRACTED, BUDGET_CALCULATED, DOCUMENT_PERSISTENCE_FAILED); UC-01 hash chain: 0 violations |
| Details and budget persistence, read-back, idempotency, refusal on change | Implemented, **mock-tested only** | `tests/test_form_details_persistence.py` (fake PostgREST) |
| Case, form and person creation from the document, with read-back | Implemented, **mock-tested only**; **blocked live** | B5 and the details migration |
| Registering the form in `candidate_documents` | **Pending approval** | see section 3 |
| UC-02 trigger after a verified UC-01 record | Not triggered (by design) | database-first rule; runs once the record is verified live |

## 6. Live blockers, re-checked (2026-11-05, second pass)

| Blocker | Root cause (live evidence) | Owner / action |
|---|---|---|
| Case creation | `onboarding_cases_status_check` rejects 7 of UC-01's 10 statuses (HELD, PLAN_PENDING, PLAN_APPROVED, IN_PROGRESS, AT_RISK, ONBOARDED, CLOSED). Zero-write probes of every other column UC-01 writes (work location, stage, form status, person kind, approval checkpoint, audit) all pass, so this is the **only** CHECK in the way. | DBA runs `docs/schema/proposed/b5_allow_uc01_case_statuses.sql` (additive, idempotent, verification query included) |
| Details + budget | The five `uc01_onboarding_*` tables do not exist (PostgREST lists none). | DBA reviews and runs `docs/schema/proposed/20261105120000_uc01_form_details_and_budget.sql` |
| Applying either from here | Not possible and not authorised: `DATABASE_URL` is a placeholder, no SQL client, and PostgREST exposes only `current_client_id` and `next_employee_no`. | - |
| Encryption | **Not a blocker.** A `dev-` key is configured; v2 ciphertexts carry the key id; round trip and row binding verified. The only existing `onboarding_forms` row (another developer's) was written with their key and does not decrypt with this one, as expected. No key was created, rotated or replaced. | Production: managed key from the approved secret store (B1) |
| UC-02 | **Ready.** UC-02's own registry resolves `SYN-UAE-TEST` live: config v1 approved, 18 templates. | - |

### Partial writes (B2/B6)

PostgREST runs one transaction per statement, so a record spanning several
tables cannot be atomic. UC-01 does not claim it is. Instead:

- The details and budget tables are written as one statement each, all
  `INSERT ... ON CONFLICT DO NOTHING`. The budget row goes last and marks the
  record complete. A record without it raises `IncompleteFormDetails`, and the
  next run inserts only what is missing. Nothing is ever updated or deleted.
- A re-run of the same document whose case and form exist but whose details are
  missing or incomplete is **resumed**, and only when the stored form equals this
  document's form (salary included). The result carries the reason
  `resumed_incomplete_record` and still needs full read-back to succeed.
- An open UC-01 case for the same email with no form or no person record (an
  interrupted `open_onboarding_case`) stops the intake with
  `incomplete_case_exists:<case_id>` for HR. It never creates a second employee.
- Other use cases' rows in the shared table (statuses outside UC-01's
  vocabulary) are ignored by these checks.

A true all-or-nothing write needs B6 (a SQL function per operation or a direct
connection), which is a DBA decision.

## 7. To finish the live path

1. **DBA:** run `b5_allow_uc01_case_statuses.sql`, then the details/budget
   migration, each with its verification query.
2. `python scripts/uc01_document_live_run.py check --out output/after-migration`.
   The B5 probe (all ten statuses), the tables and encryption must be PASS.
3. `python scripts/uc01_document_live_run.py run --live --out output/live-final`.
   It reads the form from Storage (no re-upload), creates the employee, and
   reads everything back through a fresh container. It then hands off to UC-02
   through the existing contract and verifies UC-02's tasks in the database.
   The final labels are `LIVE_PERSISTENCE_VERIFIED`, `LIVE_READBACK_VERIFIED` and
   `UC02_HANDOFF_VERIFIED`, or `BLOCKED` with the reason.
4. Re-running step 3 reuses the same employee: no second case, budget or upload.

Still open from earlier decisions: B2/B6 (audit store and atomic writes), B7
(employee-number function), production encryption key (B1).
