# Testing with synthetic onboarding documents

`scripts/mock_onboarding_documents.py` builds `.docx` files with the
**same table layout and template text** as the 2026 onboarding form. The
HR value column is filled with clearly synthetic data:

- client `SYN-UC01-DOC-TEST`
- `.invalid` email addresses
- placeholders such as `SYNTHETIC-MOBILE-01` and
  `SYNTHETIC-FATHER-NAME-01`, instead of realistic phone, passport or
  Emirates ID numbers

Each generated document's first row reads "SYNTHETIC TEST DOCUMENT - NOT A
REAL EMPLOYEE".

```powershell
python scripts/mock_onboarding_documents.py generate           # writes data/mock/documents/*.docx
python scripts/mock_onboarding_documents.py run                # every scenario, in memory
python scripts/mock_onboarding_documents.py run <file.docx>    # safe extraction view of one document
```

`run` sends each document through `DocumentIntakeService` and the
unchanged `open_onboarding_case` flow, using a fresh **in-memory**
container. "Today" is fixed at 2026-11-04 so that outcomes do not change
with the calendar. Nothing is persisted, and the local `.env` cannot point
the run at a database.

| Scenario | Document | Expected status |
|---|---|---|
| `complete_employee` | `SYNTHETIC_onboarding_complete.docx` | `CREATED` (employee number issued) |
| `missing_employee_details` | `..._missing_details.docx` | `VALIDATION_FAILED` (`missing:employee.employee_name`) |
| `missing_required_documents` | `..._missing_documents.docx` | `NEEDS_REVIEW` (`documents_outstanding`); `CREATED` once HR confirms |
| `missing_salary_and_joining_date` | `..._missing_salary_doj.docx` | `VALIDATION_FAILED` |
| `conflicting_fields` | `..._conflicting.docx` | `NEEDS_REVIEW` (two different joining dates, a 4-month probation, example responsibilities) |
| `complete_but_no_placement` | `..._complete_no_placement.docx` | `HELD` (`location_name_required`) |
| `form_not_yet_approved` | `..._complete_unapproved.docx` | `HELD` (`form not approved`) |
| `duplicate_submission` | the complete document again | `LINKED_EXISTING` |

Every scenario's caller context attests the form as APPROVED, except
`form_not_yet_approved`. This shows that the approval comes from the
caller, never from the document.

## The real template

The blank `Onboarding form - 2026.docx` is **not** stored in the
repository. When it was processed, the result was:

- template recognised
- 31 fields `MISSING`; only the template-printed issuing entity `PRESENT`
- 8 reference charges, none treated as salary
- 17 responsibilities, flagged as the template's example
- validation: `VALIDATION_FAILED`

`tests/test_document_intake.py` covers the same case with a generated
blank form (`test_a_blank_template_extracts_nothing_and_invents_nothing`).
