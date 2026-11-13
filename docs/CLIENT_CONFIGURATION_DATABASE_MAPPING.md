# Client configuration: Supabase database audit and mapping

Read-only inspection of the Supabase **Development** project on 2026-11-04 (PostgREST schema and row counts; no write). Foreign keys, CHECK constraints
and RLS policies are not exposed by PostgREST; the foreign-key facts below come from the captured schema in `UC01_Employee_Record_Creation/docs/schema/live_schema.json`.
RLS policy state is **unverified**.

## Verification status of this inspection

Superseded: the authoritative, current schema facts (columns, constraints, CHECK definitions, what is still missing from the exports) are in `CLIENT_CONFIGURATION_COLUMN_MAPPING.md`, section 1, built from the exports you ran in the Supabase SQL editor. The first inspection used only PostgREST (the `DATABASE_URL` login fails authentication) and therefore wrongly reported that `client_document_requirements`, `client_service_policies` and `client_milestone_templates` had no foreign key and no unique constraint: they have a composite foreign key `(client_id, config_version)` to `client_config_versions`, and documents and milestones a UNIQUE `(client_id, config_version, key)`. `task_templates` has the primary key `(client_id, template_key)` (no `config_version`). All 207 projected seed rows pass every exported CHECK constraint.

## Existing configuration tables

| Table | Purpose | Columns | Owner | Reuse | Rows before this work |
|---|---|---|---|---|---|
| `client_configs` | one row per client | client_id (PK), company_name, config_hash, working_days int[], timezone, template_count, milestone_count, settings_payload jsonb, is_active, created_at, updated_at | shared | master data + calendar | 6 (5 mock clients + `SYN-UAE-TEST`) |
| `client_config_versions` | version, dates, approval | client_id (PK, FK -> client_configs), version (PK), effective_from, effective_to, status (default `draft`), config_hash, approved_by, approved_at, created_at | shared | version and approval | 5 (4 draft, 1 synthetic approved) |
| `task_templates` | UC-02 task templates | template_key + client_id (PK; client_id FK), category, title, title_ar, owner_role, default_lead_days, evidence_required, requires_attributes[], sensitive_to[], approval_role, description, role_ids[], grades[], locations[], projects[], nationality_groups[], depends_on_template_keys[], offset_days, default_priority, config_version, is_active | UC-02 | direct projection | 18 (all `SYN-UAE-TEST`) |
| `client_document_requirements` | UC-03 document list | requirement_id (PK), client_id, config_version, service_category, requirement_key, document_type, description, is_mandatory, applicable_roles/grades/locations/projects[], eligibility_rules jsonb, validation_profile jsonb, is_active | UC-03 reads none today | direct projection (profile in `validation_profile`) | 6 (synthetic) |
| `client_service_policies` | per-client service/policy rows | policy_id (PK), client_id + config_version (composite FK to `client_config_versions`), service_category, service_key, is_available, is_mandatory, eligibility_rules jsonb, policy_details jsonb, responsible_role, effective_from/to, is_active | shared; UC-02 uses `uc02_assignment` | assignment rules + the six section documents | 14 (all `uc02_assignment`) |
| `client_holidays` | non-working days | client_id + holiday_date (PK; client_id FK), name, is_observed | shared | direct | 4 |
| `client_milestone_templates` | UC-02 milestones | milestone_id (PK), client_id, config_version, milestone_key, title, offset_days, required_task_keys[], eligibility_rules jsonb, is_active | UC-02 | direct | 0 |
| `role_access_matrix` | UC-04 role -> item entitlements | matrix_id (PK), client_id (FK), role_id, item_id, item_kind, is_mandatory (default true), lead_time_days (default 5), approval_class (default `STANDARD`), created_at | UC-04 | optional projection; **unversioned, no grade column** | 0 |

Other shared tables inspected and left alone (operational, not configuration): `onboarding_cases`, `offers`, `onboarding_forms`, `offer_forms` and `person_records`
(UC-01), `onboarding_tasks`, `task_dependencies`, `plan_revisions`, `approval_requests`, `audit_events`, `agent_runs` (UC-02), `candidate_documents`,
`document_validations` (UC-03), `provisioning_requests`, `provisioning_tickets`, `equipment_handovers` (UC-04), `orientation_sessions`, `buddy_profiles`,
`session_participants`, `onboarding_reminders`, `communications` (UC-05 / shared), `case_orchestration_state`, `domain_events_outbox`.
**No UC-05 configuration table exists** and none is created.

## Gaps and how they are covered

| Need | Existing support | Gap | Handling |
|---|---|---|---|
| Per-version full JSONB document | none (`client_configs.settings_payload` is unversioned) | no versioned JSONB home | `client_service_policies` rows, category `cfg_section`, one per section and version |
| UC-01 eligibility, field rules, hand-off mapping | none | no table | section row `uc01_employee_record` |
| UC-04 catalogue, approvers, handover, evidence, client-site, country | `role_access_matrix` holds only role->item | catalogue and rules unmodelled | section row; role->item projected to the matrix table |
| UC-05 programmes, policy acknowledgements, reminders | none | no table (UC-05 persists to SQLite) | section row `uc05_orientation_policy` |
| Shared approval matrix, notification refs, escalation, retry, events | none | no table | section row `shared_configuration` |
| Employment type | no column on templates/documents | none | attribute `employment_type:<x>` in `requires_attributes` |
| `excludes_attributes` | no column | cannot be represented | projection refuses (fail closed); profiles do not use it |
| Grades on the UC-04 matrix | none | no column | section row keeps grade-specific entitlements |
| Integrity | `config_hash` column exists, algorithm undocumented | - | `sha256:` over canonical JSON, checked on read-back and retrieval |

**Migrations proposed: none. Migrations applied: none.** Optional, only if the owners want them (not required by this design): add `config_version int` to
`role_access_matrix` (so that matrix rows are versioned like everything else); confirm RLS on the configuration tables.

## JSON -> table mapping

| Document path | Table.column |
|---|---|
| `client_master.client_id / client_name / timezone / client_status` | `client_configs.client_id / company_name / timezone / is_active` |
| `client_master` (country, locations, data_origin, scenario, notice), hash | `client_configs.settings_payload` |
| `shared_configuration.calendar.working_days` | `client_configs.working_days` |
| `shared_configuration.calendar.holidays[]` | `client_holidays` (date, name, observed -> is_observed) |
| `client_master.configuration_version / effective_from / effective_until / approval_status / approved_by / approved_at` | `client_config_versions.version / effective_from / effective_to / status / approved_by / approved_at` |
| document hash | `client_configs.config_hash`, `client_config_versions.config_hash` |
| `uc02.task_templates[]` | `task_templates` (template_key, category, title, owner_role, offset_days, sla_working_days -> default_lead_days, priority -> default_priority, depends_on -> depends_on_template_keys, applicability.* -> role_ids / grades / locations / projects / nationality_groups / requires_attributes, evidence_required, approval_role) |
| `uc02.assignment_rules[]` | `client_service_policies` (`uc02_assignment`, service_key = category, policy_details = team, use_case, mode, escalation_owner) |
| `uc02.milestones[]` | `client_milestone_templates` |
| `uc03.documents[]` | `client_document_requirements` (document_key -> requirement_key and document_type, name -> description, category -> service_category, mandatory -> is_mandatory, applicability -> applicable_* and eligibility_rules, everything else -> validation_profile) |
| `uc04.role_equipment_matrix / role_access_matrix` + `lead_times` | `role_access_matrix` (kind EQUIPMENT / ACCESS, lead_time_days) |
| all six sections, whole | `client_service_policies` (`cfg_section`, service_key = section name, policy_details = {section, schema_version, document_hash, document}) |

Version relationships: every versioned row carries `config_version`; `client_config_versions (client_id, version)` is the parent. Foreign keys: all rows reference
`client_configs.client_id`, which is inserted first; the dry run checks that every row's client and version exist in the store or in the plan.

## Existing-client mode (CLIENT_CANADA, CLIENT_DUBAI, CLIENT_INDIA, CLIENT_USA)

Profiles with `client_master.existing_client_record = true` do not project a `client_configs` row: the stored row is preserved, never rewritten, and the dry run checks
that its timezone, working days and active flag equal the profile's (otherwise a conflict). The configuration is inserted as **version 2** next to the untouched draft
version 1. Stored holidays that the profile reproduces exactly are no-ops. Each section row also carries `sections_hash` (sha256 over the six sections), so a reader can
verify the stored sections without the client master. Task rows now carry `onboarding_tasks.config_version`. Dry run: `python scripts/uc02_client_config.py dry-run --set existing`.

