# UC-02 mock fixtures

MOCK DATA - fictional identifiers only. Not real employee data.

These files are **additional** scenario fixtures. The canonical fixture
`data/UC02_Synthetic_Onboarding_Test_Data.json` is never modified.

| File | Scenario |
|---|---|
| `01_valid_onboarding.json` | Valid onboarding |
| `02_offer_not_accepted.json` | Offer not accepted (BR-01) |
| `03_form_not_approved.json` | Form not approved (BR-01) |
| `03b_form_incomplete.json` | Form incomplete (BR-01) |
| `04_missing_client_config.json` | Missing client configuration (BR-02) |
| `06_visa_activities.json` | Visa / BV / insurance activities (BR-07) |
| `07_full_relocation.json` | Travel, accommodation, food, banking, HR activation (BR-07/08) |
| `08_evidence_required.json` | Completion evidence (BR-12) |
| `11_unauthorized_other_client.json` | Cross-client isolation (BR-13) |
| `13_missing_calendar_coverage.json` | Date outside calendar coverage (BR-06) |
| `14_local_hire_no_extras.json` | No applicable extended activities |
| `holidays_mock.json` | Provisional calendar (`approved: false`) |
| `holidays_approved_example.json` | Shape of an approved calendar fixture |
| `approval_decisions_mock.json` | Approved / rejected decisions (BR-09) |
| `evidence_references_mock.json` | Evidence references only (BR-12) |
| `owner_assignment_mock.json` | HR manual task-owner assignment (BR-03), including assignments UC-02 must refuse and tasks left deliberately unassigned |
| `status_updates_mock.json` | Inbound UC-03 / UC-04 / UC-05 / operations status reports: success, invalid, explicitly-simulated suspected fraud, delay, rejection and duplicate cases |

Scenarios 05 (invalid owner), 09 (overdue/risk), 10 (re-planning) and 12
(duplicate) are driven by in-test configuration and service calls rather than
input payloads, since they depend on configuration or call sequence.

## Trigger fixtures vs. the other two

The numbered files and `01`–`14` are **onboarding triggers**: payloads that
start a case. `owner_assignment_mock.json` and `status_updates_mock.json` are
not triggers — they are keyed by `template_key`, which resolves against an
already-created case as `<onboarding_id>#<template_key>`. Both are written
against the `EMP-M07` persona in `07_full_relocation.json`, which carries
every applicability attribute, so no additional trigger fixture was needed.

## These are mocks, not integrations

`status_updates_mock.json` describes what UC-03, UC-04, UC-05 and the client's
operational teams **would** report. **No approved cross-use-case interface
exists in this repository and UC-02 contains no transport of any kind.**
Applying one of these updates demonstrates that UC-02 handled a report
correctly. It is not evidence that a document was validated, a laptop was
shipped, a visa was filed or anyone was notified.
