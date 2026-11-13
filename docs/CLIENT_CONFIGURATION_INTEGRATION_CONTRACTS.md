# Client configuration: consumption contracts for UC-01 to UC-05

As of 2026-11-04. These are the interfaces by which each use-case agent reads **only its own section** of the central configuration. UC-02 is implemented and verified
at the table level; the shared retrieval service (below) is **specified, not yet implemented** (it follows the approved seeding). The other use cases' internal logic is
not implemented here and none of their teams has agreed these contracts yet: every "UC-0x side" row is **awaiting that team**.

## Shared retrieval contract (IMPLEMENTED 2026-11-04: `onboarding_task_management/client_configuration/retrieval.py`; proven on the mock database for the four existing mock clients, not yet live because nothing is seeded)

```
retrieve(client_id, role_id, grade, nationality_code, country, location_code, employment_type, on_date) -> ClientConfiguration
```

| Item | Contract |
|---|---|
| Client resolution | exact `client_id`; unknown -> `ClientNotFound`; `client_configs.is_active = false` -> `ClientInactive`. No fuzzy match, no default client. |
| Version selection | approved version with `effective_from <= on_date` and (`effective_to` null or `on_date <= effective_to`), highest number. None approved -> `ConfigurationNotApproved`; only expired -> `ConfigurationExpired`; draft/retired are never selected. |
| Integrity | the six `cfg_section` rows must exist and rebuild the document; its sha256 must equal `client_config_versions.config_hash`; approved versions need approver and timestamp; otherwise `ConfigurationInvalid`. |
| Structure | the rebuilt document is validated against the JSON Schema; failure -> `ConfigurationInvalid` with the problems. |
| Data origin | `synthetic` documents are returned only if the caller opts in (`UC02_ALLOW_SYNTHETIC_CONFIG=1`, never production). |
| Eligibility | role, grade, location, employment type must be declared by the client; nationality code must belong to a declared group; client eligibility rules evaluated (`hold` / `review` outcomes surfaced). Failure -> `EmployeeNotEligible` naming the dimension (unsupported role, unsupported location, ...). |
| Result | typed object: master, derived lifecycle state, the five sections plus shared, and employee slices (applicable tasks, documents, equipment and access items, programmes, policies) using the applicability rule (empty list = all; every `requires_attributes` present; none of `excludes_attributes`). |
| Isolation | the result carries one client only; a request for client X never reads another client's rows (verified by test). |
| Never | silently selects another client or version; fills a gap with a default; lets a model invent a requirement. A gap is an error. |

Employee context attributes follow the existing convention (`location_status:in_country|overseas`, `visa_required`, `needs_travel`, `client_site`, ...) plus
`employment_type:<value>`; the nationality group is derived from `uc01_employee_record.nationality_groups`.

## UC-01 (Employee Record Creation)

| Reads | `uc01_employee_record` (fields, roles, grades, nationality groups, employment types, eligibility, validation, trigger, duplicate prevention, hand-off mapping) and the shared calendar. |
|---|---|
| Interface | the retrieval contract, or a read of the section row (`client_service_policies`, `service_category = cfg_section`, `service_key = uc01_employee_record`, the client's approved `config_version`). |
| Gives UC-02 | `uc02_handoff_mapping`: client, case, employee id, joining date, role, grade, **nationality group**, location, employment type, attributes. Today UC-01 sends only case id, employee number and joining date (HC `UC02_GAP_ANALYSIS.md`), so grade, group and attributes must still be added by UC-01. |
| Status | UC-01 does not read any client configuration today (U1 audit). **Awaiting UC-01 team.** |

## UC-02 (Onboarding Task Management) - implemented

| Reads | `uc02_onboarding_tasks` + `shared_configuration`, through the projected tables via `SupabaseConfigSource.load(client_id, joining_date)`. |
|---|---|
| Behaviour | approved version by joining date; refuses draft/retired/synthetic-unless-opted-in; templates, assignment rules, documents, milestones, calendar, holidays; the version is stored with the plan. |
| Verified | the existing reader returns a different configuration for each seeded client on the mocked database (test); live verification follows seeding. |
| To add after approval | call the shared retrieval first so eligibility errors (unsupported role/location/...) stop the workflow before a plan exists. |

## UC-03 (Document and Information Validation)

| Reads | `uc03_document_validation` (documents with applicability, mandatory, formats, metadata, expiry, verification, rejection reasons, resubmission, deadlines, dependencies) via `client_document_requirements` (`validation_profile` holds everything beyond applicability) or the section row. |
|---|---|
| Today | UC-03 loads one hardcoded `knowledge/checklists/default.yaml`; the client id never selects a checklist, and it does not read `client_document_requirements` (U3 audit). |
| Needed from UC-03 | select the checklist by client via the retrieval contract; honour `mandatory` and `min_count`; read the confidence threshold from `global.confidence_threshold`. |
| Guardrail | `authenticity_decided_by` is always `hr`; no antivirus, identity or authority verification is claimed. **Awaiting UC-03 team.** |

## UC-04 (Equipment and Approved Access Management)

| Reads | `uc04_equipment_access`: catalogues, role matrices, `approver_config`, `lead_times`, `handover_rules`, `evidence_roles`, `client_site_rules`, `country_rules`, `out_of_matrix_policy`. |
|---|---|
| Interface | the section row; role -> item entitlements are also projected to `role_access_matrix` (kind, lead time, approval class) if the UC-04 team accepts that. UC-04's own model (`CaseConfig`, ten blocks, per-block state) maps onto the section; the UC-04 team decides whether to keep reading JSON files, the section row, or the matrix table. |
| Today | two JSON files; the orchestrator passes a file path in `uc04_client_config_ref`; no UC-04 SQL exists. UC-04 reports items, UC-02 has tasks: the item-to-task mapping is **not agreed**. **Awaiting UC-04 team.** |

## UC-05 (Orientation and Policy Acknowledgement)

| Reads | `uc05_orientation_policy` (programmes, policy-acknowledgement references, reminder rules, location-specific items). |
|---|---|
| Today | UC-05 persists to SQLite, has welcome-message `ClientConfig` of its own, session durations and required tasks hardcoded, and **no** training, assessment or acknowledgement configuration (acknowledgement tracking was retired from it). The programme and policy values in the profiles are design proposals, not UC-05 requirements. |
| Open | which use case owns acknowledgement evidence (UC-02 `policy_ack` task, UC-03 documents, UC-05); the UC-05 event that completes `orientation_slot` vs `policy_ack`. **Awaiting UC-05 team.** |

## Not shared through this configuration

Secrets, trigger URLs and signing keys (environment/secret store); personal data; policy document content; uploaded files.
