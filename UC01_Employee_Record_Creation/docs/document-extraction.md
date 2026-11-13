# Onboarding-document extraction (template `onboarding-form-2026`)

UC-01 reads an onboarding-form `.docx` and extracts its fields. It then
validates them and passes the result to the **existing**
`open_onboarding_case` flow. No business rule for creating a case changes.

```
POST /internal/onboarding-documents  (system key; base64 .docx + caller context)
  -> documents/docx_reader.py   read safely (stdlib only)
  -> documents/extraction.py    template-aware field extraction (deterministic)
  -> documents/validation.py    errors vs review reasons (deterministic)
  -> brain task responsibilities_review  (optional, escalate-only, sanitised)
  -> onboarding_entry.open_onboarding_case  (unchanged start gate, duplicates, placement)
```

Service: `documents/service.py` (`DocumentIntakeService`). Script:
`scripts/mock_onboarding_documents.py`. Tests: `tests/test_document_intake.py`.

## Supported input

| Format | Status |
|---|---|
| `.docx` | Supported. Parsed with the standard library (`zipfile` and `ElementTree`); no new dependency. |
| `.pdf` (text-based) | Supported through the optional `pdf` extra (`pypdf`, pure Python). `documents/pdf_reader.py` rebuilds the form's rows from the text positions and hands them to the **same** extractor and validation as `.docx`. Without `pypdf` a PDF is `REJECTED` with `PDF_SUPPORT_UNAVAILABLE`. Refused: encrypted (`ENCRYPTED_PDF`), more than 20 pages (`TOO_MANY_PAGES`), malformed (`CORRUPT_PDF`, `NOT_A_PDF`). Every synthetic scenario extracts identically from its PDF and its `.docx` (`tests/test_document_pdf.py`). |
| Scanned images / image-only PDFs | **Not supported.** A PDF without a text layer is `REJECTED` with `NO_TEXT_LAYER`; nothing is read from images, and no paid service is used. |

The reader enforces limits before it parses anything:

- 10 MB file size
- 60 MB uncompressed
- 2,000 parts
- only `word/document.xml` is read

Any document that declares a DTD is refused, which blocks entity-expansion
attacks. Macros and embedded objects are never opened.

## What the template contains

The template is one 3-column table: the label, the text the template
prints, and the HR value. After the table comes a "Roles and
Responsibilities" section.

| Row label | Extracted as | Notes |
|---|---|---|
| Offer Letter | `offer.issuing_entity` | The header cell, printed by the template |
| Name of Employee | `employee.employee_name` | **Mandatory** |
| Reporting to | `employee.reporting_to` | Review if missing |
| Designation | `employee.designation` | **Mandatory** |
| R&R | `roles.summary` | Short cell; the full list comes from the section below the table |
| Final Salary | `employee.final_salary` (`SecretStr`) | **Mandatory**. Never filled from a price line |
| DOJ | `employee.joining_date` | **Mandatory**. ISO, `DD/MM/YYYY` (day first) or month-name dates. An ambiguous day/month is **UNCERTAIN** |
| Probation period (3 or 6 months) | `employee.probation_months` | Any value other than 3 or 6 is **INVALID** |
| 1 year or 2 years visa | `employment.visa_duration_years`, `visa_location_category` | The template's price list is kept in `reference_charges` |
| Visa – visit + entry permit | `employment.visit_visa_entry_permit` | Same |
| Direct entry permit | `employment.direct_entry_permit`, `documents.*`, `personal.*` | Value lines in the form `key: value` (see below) |
| Insurance | `insurance.plan` | Premium lines go to `reference_charges` |
| Annual ticket – Employee / Family | `benefits.annual_*_ticket` | |
| Insurance for Employee / Family | `insurance.employee_insurance` / `family_insurance` | |
| Hotel stay | `benefits.hotel_stay_days`, `hotel_budget_and_location` | |
| Any other promises | `offer.other_promises` | |
| Roles and Responsibilities section | `roles.responsibilities` (each paragraph kept verbatim) | `is_example` is set when the heading says "eg", "example" or "sample" |

In the "Direct entry permit" row, HR writes one line per item:

- `Passport: RECEIVED` or `Passport: PENDING`, and likewise for the passport
  cover, visa copy, cancellation paper, Emirates ID, signed offer letter,
  photograph and degree certificate
- `Father's name: …`, `Mother's name: …`, `Marital status: …`,
  `Mobile number: …`

UC-01 records **status only** for documents. It never reads a document
number, never checks expiry, and never validates a document; validation is
UC-03's job. A narrowed scope test enforces this:
`test_the_form_reader_records_documents_but_never_validates_them`.

## Rules the extractor keeps

- **Nothing is invented.** An empty cell is `MISSING`, even when another
  field could suggest a value.
- **Missing is not the same as invalid.** `MISSING` means nothing was
  written. `INVALID` means something was written that cannot be parsed.
- **Conflicts are not resolved.** If two cells in one row hold different
  values, the field is `UNCERTAIN` with no value. A person decides.
- **The template's own text is not a value.** It is recognised line by line
  (`TEMPLATE_TEXT`). Price lines (visa from AED 4700, a visit visa at
  AED 380, insurance from AED 1000) are kept as `reference_charges` and are
  **never** treated as salary.
- **Example responsibilities are not confirmed duties.** The template's
  section is headed "(eg given below)", so it is extracted with
  `is_example=True`. Validation then requires HR review.
- **Every value keeps its source**, for example
  `table[0].row[6].cell[2]` or `paragraph[3]`.
- **Sensitive values are `SecretStr`.** This covers the salary, the mobile
  number and both parents' names. The API response and the audit log report
  their status only.

## Validation and outcome

| Finding | Status | Case attempted? |
|---|---|---|
| Unreadable, unsafe or unsupported file | `REJECTED` | No |
| Template not recognised; name, designation, joining date or salary missing or invalid; candidate email or work location not supplied by the caller | `VALIDATION_FAILED` | No |
| Uncertain or conflicting value; probation outside policy; reporting line, probation or personal details missing; required documents outstanding; responsibilities missing or only an example; joining date already past; the model asked for a look | `NEEDS_REVIEW` | Only after an HR user sets `review_confirmed_by` (audited as `DOCUMENT_REVIEW_CONFIRMED`) |
| Otherwise | the existing flow decides: `CREATED`, `LINKED_EXISTING`, `HELD` (offer not accepted, form not approved, `location_name_required`) or `PAUSED` (hiring freeze) | Yes |
| Storage refused or failed | `PERSISTENCE_FAILED` | Attempted. Success is never reported. |

Required documents:

- **Always:** passport, passport cover, signed offer letter, photograph,
  degree certificate.
- **In addition, for a visa processed inside the country:** visa copy,
  cancellation paper and Emirates ID.

Outstanding documents do not block the case, because collecting documents
is the later document-collection stage. They do require HR to acknowledge
them.

The extracted form is **never approved by extraction**. Its status comes
only from the caller's attestation (`form_status`, `approved_by`,
`approved_at`), the same contract as `POST /internal/onboarding-requests`.

## The advisory model

The task is `brain/tasks/responsibilities_review.yaml`, with escalate-only
authority. Its inputs are only the **designation** and the **responsibility
text**, capped at 4,000 characters. The model never sees:

- the name
- the salary
- contact details
- the documents checklist
- any other field

The privacy guard also refuses any responsibility text that contains an
email address or a run of 9 or more digits, such as a phone or ID number.
In that case nothing is sent, and the refusal is audited as
`LLM_INPUT_REFUSED`.

Any label other than `MATCHES_DESIGNATION` adds the review reason
`model_flagged_responsibilities`. A model failure, timeout, refusal or
invalid reply leaves the deterministic outcome unchanged. All model calls
go through the central gateway; see `docs/uc01-llm-gateway.md`.

## Budget, supplementary rows and the synthetic marker (2026-11-05)

- **Budget.** The extractor also builds `extraction.budget`
  (`documents/budget.py`): HR's selected expenses per row (visa, insurance,
  tickets, hotel, other promises), the template's printed prices as a
  structured rate card (reference only, never counted), and a deterministic
  total per category and currency. Rules and statuses are in
  `docs/uc01-budget-and-persistence.md`.
- **Supplementary rows.** Rows the template does not print (employee email,
  department, nationality, work location, grade, employment duration) are
  read from any table when a document adds them. They do not count toward the
  template's completeness. A value that differs from the caller's is an HR
  review conflict.
- **Synthetic marker.** A document that labels itself "SYNTHETIC TEST DATA" or
  "SYNTHETIC TEST DOCUMENT" sets `metadata.synthetic_document`, and its
  details are stored with `data_origin = SYNTHETIC`.

## What is stored

When `UC01_DOCUMENT_BUCKET` is set (Supabase mode, port `source_documents`),
the received file is stored once in Supabase Storage at
`<client_id>/uc01-intake/<document_id>.<ext>`. The id is derived from the
client and the file's SHA-256, never from the file name. The file is
downloaded again and compared before the flow continues. Otherwise the file
is not stored. The details and budget go to the proposed
`uc01_onboarding_*` tables (`docs/uc01-budget-and-persistence.md`).

Audit entries record the file's SHA-256 and size, the storage reference,
field names, counts, codes and the budget totals. They never record personal
values, the salary or the file name, which can carry a person's name.
