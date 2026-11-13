# Extracted fields → UC-01 models → live Supabase columns

> **Update 2026-11-05:** the "Not stored" fields below, the supplementary rows
> and the onboarding budget now have a proposed home:
> `docs/schema/proposed/20261105120000_uc01_form_details_and_budget.sql`
> (not applied). The full field → column map is in
> `docs/uc01-budget-and-persistence.md` §3.

Live columns were checked on 2026-11-04 against the dev project's PostgREST
schema. The check was read-only. **No row has been written.**
"Not stored" means there is no live column for the value. It does **not**
mean the value is dropped from processing: it is still extracted,
validated and returned in the safe extraction view.

## Stored once Supabase writes are approved

| Source | UC-01 model field | Live table.column |
|---|---|---|
| `employee.employee_name` | `OnboardingForm.candidate_name`, `Case.candidate_name` | `onboarding_forms.candidate_name`, `onboarding_cases.candidate_name` |
| caller `candidate_email` (not in the form) | `candidate_email` | `onboarding_forms.candidate_email`, `onboarding_cases.candidate_email` |
| `employee.designation` | `OnboardingForm.designation` | `onboarding_forms.designation` (there is **no** case column) |
| `employee.joining_date` | `joining_date` | `onboarding_forms.joining_date`, `onboarding_cases.joining_date` |
| caller `work_location_type`, `client_site_id` | same | `onboarding_forms.*`, `onboarding_cases.work_location_type` |
| `employee.final_salary` | `OnboardingForm.salary_and_terms` (`SecretStr`) | `onboarding_forms.salary_and_terms_encrypted` (AES-256-GCM, bytea) |
| caller attestation | `status`, `approved_by`, `approved_at` | `onboarding_forms.*` |
| caller placement | `CasePlacement` | `onboarding_cases.location_name`, `department`, `grade`, `nationality_group`, `project_name` |
| generated | `Case.employee_no` | `onboarding_cases.employee_no` (via `rpc/next_employee_no`, B7) |
| HC_12 variant approvals | `Approval` | `uc01_approvals` (columns match the model exactly) |
| Audit entries | `AuditEntry` | **Undecided** (B2): shared `audit_events` or a dedicated table |
| Source file metadata | name, size, SHA-256, MIME | `candidate_documents` has these columns, but needs a `document_ref` to real storage (B4) and an existing case |

## Not stored (no live column): decision B9

These are extracted and validated, but no live table can hold them:

- `employee.reporting_to`
- `employee.probation_months`
- `employment.*`: visa duration and category, visit/entry permit, direct
  entry permit
- `documents.*`: checklist status
- `personal.*`: father's and mother's name, marital status, mobile number
- `insurance.*`
- `benefits.*`
- `offer.other_promises`, `offer.issuing_entity`
- `roles.responsibilities` and `roles.summary`

They would need a new table, for example `uc01_onboarding_form_details`,
keyed by `form_id`. Its sensitive columns (personal details and mobile
number) would need to be encrypted the same way as the salary. That is a
DBA and HR decision (B9 in `docs/uc01-supabase-decision-request.md`).
UC-01 will not put them in a free-text or JSON column of a shared table
without that decision.

`reference_charges` (the template's price lists) are company reference
information. They are **never** employee data and are never stored against
an employee.
