# Four existing clients: dynamic checklist test report

Date: 2026-11-04. UC-06 out of scope. Companion to `EXISTING_CLIENT_RETRIEVAL_AUDIT.md`.

## Result

| Client | Checklist generated? | Why |
|---|---|---|
| CLIENT_CANADA | **No** | configuration refused: marked mock; only version is a draft; 0 task templates, 0 documents |
| CLIENT_DUBAI | **No** | same; holidays present (2 sample dates) but nothing to schedule |
| CLIENT_INDIA | **No** | same |
| CLIENT_USA | **No** | same |

The real UC-02 checklist path (`OnboardingService.preview_checklist`, configuration read live, read-only) raised `MissingConfigurationError` for each of the four
clients. This is the correct behaviour: there is no configured requirement to put on a checklist, and inventing visa, medical, insurance or payroll requirements
for these countries would fabricate legal obligations. The four-client checklist acceptance criteria therefore **cannot be met with the current data**; nothing
was created to make them pass.

To show that the mechanism itself works, the same path was run against the one configured client, `SYN-UAE-TEST` (synthetic, not one of the four scenarios).

## Mechanism proof on SYN-UAE-TEST (live configuration, read-only; nothing persisted)

Employee test attributes come from the existing synthetic profiles (`synthetic_profiles.PROFILES`); joining date 2026-12-14; configuration version 1; calendar
`supabase client_configs + client_holidays` (weekend Sat/Sun, holidays 2026-12-02 and -03, **not approved**: the preview warns that due dates are provisional).

| Scenario | Attributes | Included | Excluded (reason) |
|---|---|---|---|
| In-country, L4 | role `eng`, grade L4, Dubai, `grp-a`, `bv_required`, `insurance_required`, `location_status:in_country` | 13: hr_record_create, collect_documents, background_verification, visa_status_change, medical_test, biometrics, insurance_enrolment, payroll_setup, laptop_request, system_access, orientation_slot, senior_briefing, policy_ack | entry_permit, medical_test_arrival, biometrics_arrival, flight_booking, accommodation (attributes) |
| Overseas, L2 | grade L2, `grp-b`, `needs_travel`, `needs_accommodation`, `location_status:overseas` | 12: hr_record_create, collect_documents, background_verification, entry_permit, medical_test_arrival, biometrics_arrival, flight_booking, accommodation, payroll_setup, laptop_request, orientation_slot, policy_ack | visa_status_change, medical_test, biometrics, insurance_enrolment (attributes); system_access, senior_briefing (grade) |
| Location status missing | as in-country but no `location_status:*` | 10 | all six visa/medical/biometrics route tasks (attributes) - **no review flag (gap)** |
| Lower grade L2 | as in-country, grade L2 | 11 | system_access, senior_briefing (grade) + the overseas route |
| Unsupported role `astronaut` | as in-country | 13 (same as in-country) - **not rejected (gap)** | as in-country |
| Unsupported location `Atlantis` | as in-country | 13 - **not rejected (gap)** | as in-country |

In-country details:

* **Categories:** visa/work authorisation = visa_status_change; medical = medical_test, biometrics; insurance = insurance_enrolment; payroll = payroll_setup;
  equipment/access = laptop_request, system_access; orientation/policy = orientation_slot, senior_briefing, policy_ack; employee record and documents = hr_record_create,
  collect_documents, background_verification. No configured template exists for bank verification, tax/statutory forms, VPN, access card, induction programmes or
  security training, so none appears.
* **Dependency layers (parallel batches):** [hr_record_create] -> [collect_documents, laptop_request, orientation_slot] -> [background_verification, insurance_enrolment,
  payroll_setup, policy_ack, senior_briefing, system_access, visa_status_change] -> [medical_test] -> [biometrics]. At generation only hr_record_create is ready; the rest are
  blocked by their prerequisites.
* **Due dates (working days back from joining, holidays skipped):** hr_record_create 2026-11-24, collect_documents 11-26, background_verification 11-27,
  visa_status_change 11-30, medical_test 12-04, laptop_request 12-07, biometrics / insurance_enrolment / payroll_setup 12-08, system_access 12-10, orientation_slot 12-14,
  senior_briefing 12-15, policy_ack 12-16.
* **Documents (same matching rules):** identity, education, status_change_pack, senior_declaration (mandatory) and family_cover_form (optional); excluded entry_permit_pack.
  Overseas: identity, education, entry_permit_pack. Location status missing: identity and education only.
* **Traceability:** every included task is a `task_templates` row of SYN-UAE-TEST with a reason code (`applies_to_all` or `matched_all_filters`); every exclusion carries
  `excluded_by_attributes` or `excluded_by_grade`. Mandatory vs optional is a property of documents (`is_mandatory`); task templates have no optional flag.

## Cross-client isolation

| Check | Result | Evidence |
|---|---|---|
| A client without its own templates never receives another client's | PASS | each of the four, made approved and non-mock in the mock copy, is refused with "no active task templates" (SYN-UAE-TEST's 18 are never used) |
| A template moved to another client disappears from the original | PASS | mock |
| Documents and holidays do not leak | PASS | an India-only document and a Dubai-only holiday never appear for SYN-UAE-TEST (mock) |
| Versions are never mixed | PASS | a newer approved version without templates is refused instead of reusing version 1 rows (mock) |
| Missing / invalid / wrong-case client id | PASS | live: `client is not in client_configs` for `CLIENT_MISSING_XYZ`, empty id, `client_dubai` |
| Live rows: every child row's client and version exist; no duplicate keys | PASS | live read-only inventory |
| DUBAI vs INDIA visa, CANADA vs USA, USA vs others | NOT TESTABLE | none of the four has any requirement to leak; covered generically by the tests above |

Limitation found: the mock database compares strings case-insensitively, so case sensitivity is proven only by the live check.

## Test results

| Suite | Result | Kind |
|---|---|---|
| Full UC-02 suite | **1515 passed, 1 skipped, 4 xfailed**, 2537 subtests | local mock / unit |
| Evaluation suite | **37 / 37** | local |
| `tests/test_existing_clients_retrieval.py` (new) | **23 passed, 4 expected failures** (the documented gaps: unsupported role, unsupported location, missing location status not flagged, task rows without `config_version`) | local mock seeded with the live snapshot |
| Live retrieval for 9 client ids | as in the audit, all results recorded | live read-only (GET only) |
| Live checklist preview, 4 + 6 scenarios | as above | live read-only configuration, nothing persisted |
| Live workflow for the four clients | **not run, not possible** (no configuration) | - |

## What would make the four-client test meaningful

An approved configuration for each client (templates with role/grade/location/nationality/attribute filters, document requirements, assignment policies, milestones,
holidays), supplied by the clients' HR and the country compliance owners, loaded into a Development project. Then rerun the preview and workflow tests per client
and the isolation checks between DUBAI/INDIA and CANADA/USA.
