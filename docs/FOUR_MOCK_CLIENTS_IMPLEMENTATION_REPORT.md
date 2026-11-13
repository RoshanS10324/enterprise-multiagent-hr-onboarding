# Four existing mock clients: implementation and verification report

Date: 2026-11-04. Scope UC-01..UC-05 (UC-06 excluded). Clients: CLIENT_CANADA, CLIENT_DUBAI, CLIENT_INDIA, CLIENT_USA.

## Status in one paragraph

Complete synthetic configurations exist for all four clients, they validate, and in **local mock tests** each client retrieves its own configuration and gets a
different, correct, explained onboarding checklist; the three UC-02 gaps from the audit are fixed. **The live database has NOT been seeded**: the
Supabase project is `your-supabase-project-id` and its Development/Production label cannot be read through the data API, so per your instruction nothing was
written. The live read-only dry run is clean (231 inserts, 0 updates, 0 deletes, 0 conflicts). Live retrieval and live checklists for the four clients will only
work after an approved seed; until then they are proven on the mock only.

## Environment

| Item | Finding |
|---|---|
| Project | `your-supabase-project-id.supabase.co` (the only one configured) |
| Label | not visible to the data API; you report it is labelled PRODUCTION. **Unconfirmed.** |
| Access used in this work | PostgREST GET only (every read-only command now refuses any non-GET request in-process) |
| Writes in this work | none |

## Deliverables

| # | Deliverable | Where |
|---|---|---|
| 1 | Four client JSON profiles (version 2, `environment: mock`, `synthetic: true`, `existing_client_record: true`, `awaiting_confirmation`, data source) | `client_configuration/profiles/existing_mock_clients/client_{canada,dubai,india,usa}_config.json`, built by `client_configuration/build_existing_mock_clients.py` |
| 2 | Common JSON Schema (backward-compatible optional fields added) | `client_configuration/schema/client_configuration.schema.json` |
| 3 | Semantic validation | `onboarding_task_management/client_configuration/document.py` (`validate_document`); `python scripts/uc02_client_config.py validate --set existing` |
| 4-10 | Document, visa/work-authorisation, medical/insurance, payroll/banking, task-dependency, equipment/access, orientation/training matrices (+ calendars) | `docs/FOUR_MOCK_CLIENTS_CONFIGURATION_MATRICES.md` (generated from the profiles) |
| 11 | Central retrieval implementation | `onboarding_task_management/client_configuration/retrieval.py` (`retrieve(rest, EmployeeProfile)`) |
| 12 | UC-02 integration | `supabase_config.py` (eligibility catalogue, mock clients behind the synthetic opt-in), `sub_agent/selection.py` (attribute review), `sub_agent/service.py` (strict eligibility on preview and create), `supabase_store.py` (task `config_version`) |
| 13 | Four-client test suite | `tests/test_four_mock_clients.py` (26 tests) |
| 14 | Cross-client isolation report | `docs/FOUR_MOCK_CLIENTS_ISOLATION_REPORT.md` |
| 15 | Database mapping | `docs/CLIENT_CONFIGURATION_DATABASE_MAPPING.md`, `docs/CLIENT_CONFIGURATION_COLUMN_MAPPING.md` (existing-client mode noted below) |
| 16 | Read-only dry-run SQL (+ the seed SQL for later review) | `docs/four_mock_clients_dry_run_readonly.sql` (SELECT only, `BEGIN READ ONLY ... ROLLBACK`), `docs/four_mock_clients_seed.sql` (not executed) |
| 17 | This report | `docs/FOUR_MOCK_CLIENTS_IMPLEMENTATION_REPORT.md` |

## How the four profiles differ (all synthetic; nothing legal is asserted)

| | CLIENT_CANADA | CLIENT_DUBAI | CLIENT_INDIA | CLIENT_USA |
|---|---|---|---|---|
| Stored values kept | America/Toronto, Mon-Fri | Asia/Dubai, Mon-Fri, its 2 `[SAMPLE]` holidays | Asia/Kolkata, **Mon-Sat** | America/New_York, Mon-Fri |
| Location | Canada (province **not stored**: provincial items flagged) | Dubai | India (city not stored) | United States (state **not stored**: state items flagged) |
| Required checklist attributes | `work_authorization`, `work_province` | `visa_route` (not_required / in_country_transfer / overseas_entry) | none beyond the common set | `work_authorization`, `work_state` |
| Immigration | permit verification for permit holders; permit support if required (awaiting) | entry permit, medical fitness, Emirates ID biometrics, residence stamping on either route; none for UAE nationals | none configured (domestic only) | eligibility verification for everyone (awaiting); visa verification / sponsorship only for the matching status |
| Medical / insurance | no pre-employment medical; benefits for full-time | medical fitness only on a visa route; health insurance; dependant cover G6-G7 | no medical; group health insurance for full-time | no medical; benefits for full-time |
| Payroll | CAD; tax onboarding forms (awaiting); direct deposit | AED; bank account; salary transfer letter on request | INR; tax identifier; statutory declarations (awaiting); bank verification | USD; federal and state withholding (awaiting); direct deposit |
| Tasks / documents | 19 / 16 | 26 / 16 | 18 / 16 | 20 / 13 |

No salary, tax rate, contribution percentage, government fee, statutory deadline or named statutory form is stored. Frequencies, probation and weekly hours are
`awaiting_confirmation`.

## Gaps fixed

| Gap (from the audit) | Fix | Evidence |
|---|---|---|
| Unsupported role / location accepted | The client's catalogue (`uc01_employee_record`: roles, grades, work locations, nationality groups, employment types) is loaded with the version; UC-02 refuses an employee outside it (`EmployeeNotEligibleError`) on preview and on create, and nothing is created. A client without a catalogue (SYN-UAE-TEST) gets an `eligibility_not_configured` note instead of a guess. | tests: unsupported role, location, grade, nationality, employment type, country |
| Missing location status silently removed visa tasks | Any required or referenced attribute family that is not stated raises a `review_required` issue naming the withheld tasks (e.g. `visa_route` -> entry_permit, visa_status_change, ...). | tests on SYN-UAE-TEST and CLIENT_DUBAI/CANADA/USA |
| Task `config_version` NULL | The Supabase adapter writes the plan's version on every task row (existing integer column; **no migration**). | mock: every task row `config_version = 2`. The 13 existing live tasks were not changed. |

## Checklist results (mock, joining date 2027-01-18)

| Scenario | Version / group | Tasks (excl.) | Docs mand./opt. | Immigration | Medical | Insurance | Payroll | Equipment / access tasks | Orientation / policy |
|---|---|---|---|---|---|---|---|---|---|
| Canada, software engineer L3, Indian national, work-permit holder, ON | 2 / NG-INTL-SYN | 16 (3) | 12 / 1 | work_permit_verification | - | benefits_enrolment | tax_onboarding_forms, direct_deposit_setup, payroll_setup | laptop_request, system_access | 5 |
| Dubai, software engineer G5, Indian national, overseas entry | 2 / NG-EXPAT-SYN | 19 (7) | 11 / 0 | entry_permit, emirates_id_biometrics_on_arrival, residence_visa_stamping_on_arrival | medical_fitness_test_on_arrival | health_insurance_enrolment | payroll_setup, bank_account_setup | laptop_request, system_access, office_access_card | 4 |
| Dubai, operations associate G4, UAE national | 2 / NG-UAE-NATIONAL-SYN | 13 (13) | 10 / 0 | none | none | health_insurance_enrolment | payroll_setup, bank_account_setup | 3 | 4 |
| India, finance analyst G4, Indian national | 2 / NG-DOMESTIC-SYN | 18 (0) | 14 / 2 | **none** | none | group_health_insurance_enrolment | tax_identifier_verification, statutory_declarations, bank_account_verification, payroll_setup | 4 incl. finance_application_access | 5 incl. finance_controls_training |
| USA, sales associate B3, US citizen, NY | 2 / NG-US-SYN | 18 (2) | 9 / 1 | none (eligibility verification is in the HR-record tasks) | none | benefits_enrolment | federal and state withholding, direct_deposit_verification, payroll_setup | 4 incl. sales_platform_access | 5 |
| USA, sponsorship required | 2 / NG-US-SYN | 19 (1) | 9 / 1 | visa_sponsorship_case | none | benefits_enrolment | as above | as above | 5 |
| Dubai, visa route NOT stated | 2 / NG-EXPAT-SYN | 13 (13) | 10 / 0 | none included; **flagged for HR review** listing the withheld route tasks | - | - | - | - | - |

For every scenario: only `hr_record_create` is ready at generation and every other task is blocked by its prerequisites. Independent tasks share a dependency
layer and can run in parallel (4-6 layers). Due dates count working days backwards on the client's own calendar and never fall on a holiday; India's Monday-Saturday
week shifts dates compared with Canada's (`hr_record_create` 2027-01-06 against 2027-01-04). Every included or excluded task carries a reason code
(`applies_to_all`, `matched_all_filters`, `excluded_by_attributes`, `excluded_by_grade`, `excluded_by_role_id`, ...). Every persisted task is traceable to its
template and version. Calendars are reported as not approved, with the warning that due dates are provisional.

## Tests (exact)

| Suite | Result | Kind |
|---|---|---|
| Full UC-02 suite | **1544 passed, 1 skipped**, 2543 subtests | local unit and mock |
| Evaluation | **37 / 37** | local |
| `test_four_mock_clients.py` | 26 passed | mock seeded from the live snapshot |
| `test_existing_clients_retrieval.py` | 26 passed (the former expected-failure gaps are now real tests) | mock |
| `mypy src` | 39 findings (unchanged; none from the new code) | static |
| Live read-only dry run (four clients) | 231 inserts, 0 updates, 0 deletes, 0 conflicts, 0 FK problems; 4 client rows and 2 Dubai holidays preserved | live, GET only |
| Live retrieval / live checklist for the four | **not possible before seeding** | - |

## Live read-only dry run (2026-11-04)

| Table | Rows now | Inserts | No-op (preserved) | After |
|---|---|---|---|---|
| client_configs | 6 | 0 | 4 (stored rows match the profiles) | 6 |
| client_config_versions | 5 | 4 (version 2) | - | 9 |
| task_templates | 18 | 83 | - | 101 |
| client_document_requirements | 6 | 61 | - | 67 |
| client_service_policies | 14 | 71 | - | 85 |
| client_milestone_templates | 0 | 4 | - | 4 |
| client_holidays | 4 | 8 | 2 (CLIENT_DUBAI) | 12 |
| **Total** | | **231** | | |

## Known limitations

* Everything is synthetic: country rules (immigration, medical, payroll, tax, holidays) are `awaiting_confirmation` with named owners. The version is `approved` with
  `approved_by = "synthetic-fixture (development placeholder; not a client approval)"`, because the reader requires `approved`; these clients are mock and are only served
  to a runtime that opted in to synthetic configuration (never production).
* The existing `client_configs` rows are not changed, so their `template_count`, `milestone_count` and `config_hash` stay 0 / `mock-...-v1`. The version row carries the
  document hash. Updating them would need a separate approval.
* `task_templates` key is `(client_id, template_key)`: a future version 3 cannot coexist with version 2.
* `role_access_matrix` is not written; equipment and access live in the `uc04_equipment_access` section until UC-04 confirms a mapping onto its CHECK vocabulary.
* UC-05 acknowledgement tracking is retired; programmes and policy acknowledgements here are configuration only.
* Province, state and city are not stored; the dependent items are flagged, not assumed.
* The mock compares strings case-insensitively.

## Next steps (each needs your decision)

1. Confirm whether `your-supabase-project-id` is Development or Production. If Production, decide whether test data may be written there at all.
2. Review `docs/four_mock_clients_dry_run_readonly.sql` (you can run it; it is read-only) and `docs/four_mock_clients_seed.sql`.
3. On explicit approval: seed (`python scripts/uc02_client_config.py seed --set existing --confirm-write <host>` with `UC02_ENV=development` and
   `UC02_ALLOW_CONFIG_SEEDING=1`, or the SQL file), then `verify --set existing` and the live retrieval and checklist checks.
