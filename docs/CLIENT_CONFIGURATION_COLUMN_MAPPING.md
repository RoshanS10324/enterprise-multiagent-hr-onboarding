# Client configuration: column-to-data mapping (schema first, data second)

As of 2026-11-04. **Nothing in this document has been executed against the database.** Schema source of truth: the exports you ran in the Supabase SQL editor
(`information_schema.columns`, constraints, CHECK definitions, indexes), cross-checked with the live PostgREST OpenAPI document (identical column names, order and nullability for all
seven tables). Data source: the four profile files (`client_configuration/profiles/`) projected exactly as the generated seed SQL does.

## 1. Verified schema facts

**Columns, types, nullability, defaults:** complete and verified for all seven tables (export `information_schema.columns`; PostgREST agrees; section 2 lists every column).

**Constraints actually seen in your exports** (`pg_constraint`, `information_schema`, `pg_indexes`):

| Table | Primary key | Foreign keys | Unique | CHECK |
|---|---|---|---|---|
| `client_configs` | `client_id` | - | - | `template_count >= 0`, `milestone_count >= 0` |
| `client_config_versions` | `(client_id, version)` | `client_id` -> `client_configs(client_id)` | - | `status IN ('draft','approved','retired')`; `effective_to IS NULL OR effective_to > effective_from` |
| `task_templates` | `(client_id, template_key)` (from the index export; **no `config_version`**) | `client_id` -> `client_configs(client_id)` (PostgREST; not in the truncated export) | - | `category IN (19 values: hr_record, document_collection, it_provisioning, access_management, workplace, orientation, policy_acknowledgement, payroll, joining_confirmation, background_verification, visa, medical, biometrics, insurance, travel, accommodation, food, banking_letter, hr_system_activation)`; `owner_role IN (hr_ops, hr_lead, it_manager, facilities, finance, manager, buddy, employee, system, hr_officer, hr_manager, pro)` |
| `client_document_requirements` | `requirement_id` | **`(client_id, config_version)` -> `client_config_versions(client_id, version)`** | **`(client_id, config_version, requirement_key)`** | - |
| `client_service_policies` | not in the export | **`(client_id, config_version)` -> `client_config_versions(client_id, version)`** | not in the export | - |
| `client_milestone_templates` | `milestone_id` | **`(client_id, config_version)` -> `client_config_versions(client_id, version)`** | **`(client_id, config_version, milestone_key)`** | - |
| `client_holidays` | `(client_id, holiday_date)` | `client_id` -> `client_configs(client_id)` | - | - |
| `role_access_matrix` (excluded) | `matrix_id`; unique index `(client_id, role_id, item_id)` | `client_id` -> `client_configs` (PostgREST) | `uq_role_access_matrix` | `item_kind IN (HARDWARE, SOFTWARE_ACCESS, PHYSICAL_BADGE, SYSTEM_PERM)`; `approval_class IN (STANDARD, MANAGER_EXCEPTION, SECURITY_GATE)`; `lead_time_days >= 0` |

**Corrections to earlier statements (now superseded):** (1) the document, milestone and policy tables DO have composite foreign keys to `client_config_versions` and (documents, milestones) unique constraints - the
earlier "no visible unique constraint / no foreign key" came from PostgREST not exposing them; so a version row must exist before any child row (the seed already inserts it first). (2) My earlier
projection of `role_access_matrix` used `item_kind` EQUIPMENT/ACCESS and `approval_class` EQUIPMENT/ACCESS, which the live CHECK constraints would reject; the table stays excluded, the projection now refuses to run, and
no value is guessed.

**Compliance check:** all 207 rows destined for the seven tables were checked against every exported CHECK constraint (category, owner role, version status, date order, non-negative counts): **0 violations**. Composite foreign keys are
satisfied by construction (every child row uses `config_version = 1`, whose version row is inserted first).

**Still missing (your exports were cut off at the editor's 100-row limit):** for `task_templates` and `role_access_matrix` all constraints from `pg_constraint`; for `client_service_policies` its primary key and any UNIQUE constraint;
and the index list for `client_config_versions`, `client_document_requirements`, `client_service_policies`, `client_milestone_templates`. None of this blocks the SQL (it uses its own duplicate guards and aborts on any violation), but
it should be confirmed. Run these two read-only queries (each returns well under 100 rows) and send the CSVs:

```sql
select conrelid::regclass::text as table_name, conname, contype, pg_get_constraintdef(oid) as definition
from pg_constraint
where conrelid in ('public.task_templates'::regclass, 'public.client_service_policies'::regclass, 'public.role_access_matrix'::regclass,
                   'public.client_config_versions'::regclass, 'public.client_document_requirements'::regclass, 'public.client_milestone_templates'::regclass)
  and contype in ('p','f','u')
order by 1, 3, 2;

select tablename, indexname, indexdef from pg_indexes
where schemaname = 'public' and tablename in ('client_configs','client_config_versions','task_templates','client_document_requirements',
      'client_service_policies','client_milestone_templates','client_holidays','role_access_matrix') order by 1, 2;
```

## 2. Column mapping

"Required?" means the column is NOT NULL with no default, so the INSERT must supply it. Examples are the actual projected values (SYN-CLIENT-A, and SYN-CLIENT-D to show the client-specific difference);
`(default)` means the column is not supplied and the database default applies. Source = the path in the profile JSON.


### `client_configs`

| Database table | Actual column | Data type | Required? | Proposed mock value (SYN-CLIENT-A) | SYN-CLIENT-D | Source profile field |
|---|---|---|---|---|---|---|
| `client_configs` | `client_id` | text | **required** | `SYN-CLIENT-A` | `SYN-CLIENT-D` | client_master.client_id |
| `client_configs` | `company_name` | text | **required** | `SYNTHETIC CLIENT A - UAE engineering (not a real client)` | `SYNTHETIC CLIENT D - Saudi Arabia operations (not a rea...` | client_master.client_name |
| `client_configs` | `config_hash` | text | **required** | `sha256:b390b8468e16c2219d499c050a79980ded92723d6be8bddc...` | `sha256:894cbf965e975a9fc2d476eacfb94e552f7ba556903f1354...` | sha256 of the whole profile (computed) |
| `client_configs` | `working_days` | integer[] default '{1,2,3,4,5}'::integer[] | optional (default) | `[1,2,3,4,5]` | `[7,1,2,3,4]` | shared_configuration.calendar.working_days |
| `client_configs` | `timezone` | text default 'Asia/Dubai'::text | optional (default) | `Asia/Dubai` | `Asia/Riyadh` | client_master.timezone |
| `client_configs` | `template_count` | int4 default 0 | optional (default) | `15` | `16` | count(uc02_onboarding_tasks.task_templates) |
| `client_configs` | `milestone_count` | int4 default 0 | optional (default) | `1` | `1` | count(uc02_onboarding_tasks.milestones) |
| `client_configs` | `settings_payload` | jsonb default '{}'::jsonb | optional (default) | `{"data_origin":"synthetic","client_status":"active","co...` | `{"data_origin":"synthetic","client_status":"active","co...` | client_master (country, locations, status, data_origin, scenario, notice) + hash |
| `client_configs` | `is_active` | bool default true | optional (default) | `true` | `true` | client_master.client_status == 'active' |
| `client_configs` | `created_at` | timestamptz default now() | optional (default) | `(default)` | `(default)` | (not supplied: default now()) |
| `client_configs` | `updated_at` | timestamptz default now() | optional (default) | `(default)` | `(default)` | (not supplied: default now()) |

### `client_config_versions`

| Database table | Actual column | Data type | Required? | Proposed mock value (SYN-CLIENT-A) | SYN-CLIENT-D | Source profile field |
|---|---|---|---|---|---|---|
| `client_config_versions` | `client_id` | text | **required** | `SYN-CLIENT-A` | `SYN-CLIENT-D` | client_master.client_id |
| `client_config_versions` | `version` | int4 | **required** | `1` | `1` | client_master.configuration_version |
| `client_config_versions` | `effective_from` | date | **required** | `2026-01-01` | `2026-01-01` | client_master.effective_from |
| `client_config_versions` | `effective_to` | date | optional (nullable) | `NULL` | `NULL` | client_master.effective_until |
| `client_config_versions` | `status` | text default 'draft'::text | optional (default) | `approved` | `approved` | client_master.approval_status |
| `client_config_versions` | `config_hash` | text | **required** | `sha256:b390b8468e16c2219d499c050a79980ded92723d6be8bddc...` | `sha256:894cbf965e975a9fc2d476eacfb94e552f7ba556903f1354...` | sha256 of the whole profile (computed) |
| `client_config_versions` | `approved_by` | text | optional (nullable) | `synthetic-fixture` | `synthetic-fixture` | client_master.approved_by |
| `client_config_versions` | `approved_at` | timestamptz | optional (nullable) | `2026-11-04T00:00:00+00:00` | `2026-11-04T00:00:00+00:00` | client_master.approved_at |
| `client_config_versions` | `created_at` | timestamptz default now() | optional (default) | `(default)` | `(default)` | (not supplied: default now()) |

### `task_templates`

| Database table | Actual column | Data type | Required? | Proposed mock value (SYN-CLIENT-A) | SYN-CLIENT-D | Source profile field |
|---|---|---|---|---|---|---|
| `task_templates` | `template_key` | text | **required** | `hr_record_create` | `hr_record_create` | uc02.task_templates[].template_key |
| `task_templates` | `client_id` | text | **required** | `SYN-CLIENT-A` | `SYN-CLIENT-D` | client_master.client_id |
| `task_templates` | `category` | text | **required** | `hr_record` | `hr_record` | [].category |
| `task_templates` | `title` | text | **required** | `Hr record create` | `Hr record create` | [].title |
| `task_templates` | `title_ar` | text default ''::text | optional (default) | `Hr record create` | `Hr record create` | [].title (copied; the profiles carry no Arabic title) |
| `task_templates` | `owner_role` | text | **required** | `hr_ops` | `hr_ops` | [].owner_role |
| `task_templates` | `default_lead_days` | int4 default 0 | optional (default) | `3` | `3` | [].sla_working_days |
| `task_templates` | `evidence_required` | bool default false | optional (default) | `false` | `false` | [].evidence_required |
| `task_templates` | `requires_attributes` | text[] default '{}'::text[] | optional (default) | `[]` | `[]` | [].applicability.requires_attributes + employment_type:<x> |
| `task_templates` | `sensitive_to` | text[] default '{}'::text[] | optional (default) | `[]` | `[]` | (no profile field: empty array) |
| `task_templates` | `approval_role` | text | optional (nullable) | `NULL` | `NULL` | [].approval_role |
| `task_templates` | `description` | text | optional (nullable) | `hr record create (SYNTHETIC ASSUMPTION - not an approve...` | `hr record create (SYNTHETIC ASSUMPTION - not an approve...` | [].description |
| `task_templates` | `role_ids` | text[] default '{}'::text[] | optional (default) | `[]` | `[]` | [].applicability.roles |
| `task_templates` | `grades` | text[] default '{}'::text[] | optional (default) | `[]` | `[]` | [].applicability.grades |
| `task_templates` | `locations` | text[] default '{}'::text[] | optional (default) | `[]` | `[]` | [].applicability.locations |
| `task_templates` | `projects` | text[] default '{}'::text[] | optional (default) | `[]` | `[]` | [].applicability.projects |
| `task_templates` | `nationality_groups` | text[] default '{}'::text[] | optional (default) | `[]` | `[]` | [].applicability.nationality_groups |
| `task_templates` | `depends_on_template_keys` | text[] default '{}'::text[] | optional (default) | `[]` | `[]` | [].depends_on |
| `task_templates` | `offset_days` | int4 | optional (nullable) | `-12` | `-14` | [].offset_days |
| `task_templates` | `default_priority` | text default 'normal'::text | optional (default) | `high` | `normal` | [].priority |
| `task_templates` | `config_version` | int4 | optional (nullable) | `1` | `1` | client_master.configuration_version |
| `task_templates` | `is_active` | bool default true | optional (default) | `true` | `true` | constant true |

### `client_document_requirements`

| Database table | Actual column | Data type | Required? | Proposed mock value (SYN-CLIENT-A) | SYN-CLIENT-D | Source profile field |
|---|---|---|---|---|---|---|
| `client_document_requirements` | `requirement_id` | uuid default gen_random_uuid() | optional (default) | `78c16fdd-039c-536e-8abd-b46fdd91919c` | `0dda6cec-1320-5bbb-8cdc-77e75f69d4d5` | uuid5(client, version, document_key) (computed) |
| `client_document_requirements` | `client_id` | text | **required** | `SYN-CLIENT-A` | `SYN-CLIENT-D` | client_master.client_id |
| `client_document_requirements` | `config_version` | int4 | **required** | `1` | `1` | client_master.configuration_version |
| `client_document_requirements` | `service_category` | text | **required** | `offer` | `offer` | uc03.documents[].category |
| `client_document_requirements` | `requirement_key` | text | **required** | `signed_offer_letter` | `signed_offer_letter` | [].document_key |
| `client_document_requirements` | `document_type` | text | **required** | `signed_offer_letter` | `signed_offer_letter` | [].document_key |
| `client_document_requirements` | `description` | text | optional (nullable) | `Signed offer letter` | `Signed offer letter` | [].name |
| `client_document_requirements` | `is_mandatory` | bool default true | optional (default) | `true` | `true` | [].mandatory |
| `client_document_requirements` | `applicable_roles` | text[] default '{}'::text[] | optional (default) | `[]` | `[]` | [].applicability.roles |
| `client_document_requirements` | `applicable_grades` | text[] default '{}'::text[] | optional (default) | `[]` | `[]` | [].applicability.grades |
| `client_document_requirements` | `applicable_locations` | text[] default '{}'::text[] | optional (default) | `[]` | `[]` | [].applicability.locations |
| `client_document_requirements` | `applicable_projects` | text[] default '{}'::text[] | optional (default) | `[]` | `[]` | [].applicability.projects |
| `client_document_requirements` | `eligibility_rules` | jsonb default '{}'::jsonb | optional (default) | `{"nationality_groups":[],"requires_attributes":[],"allo...` | `{"nationality_groups":[],"requires_attributes":[],"allo...` | [].applicability.{nationality_groups, requires_attributes + employment_type:<x>} + allow_not_applicable |
| `client_document_requirements` | `validation_profile` | jsonb default '{}'::jsonb | optional (default) | `{"submission":{"requirement":"required","deadline_offse...` | `{"submission":{"requirement":"required","deadline_offse...` | [] submission, accepted_formats, required_metadata, expiry, verification, initial_status, rejection_reasons, resubmission, depends_on_tasks, blocks_tasks, extras + uc03.global |
| `client_document_requirements` | `is_active` | bool default true | optional (default) | `true` | `true` | constant true |
| `client_document_requirements` | `created_at` | timestamptz default now() | optional (default) | `(default)` | `(default)` | (not supplied: default now()) |

### `client_service_policies`

| Database table | Actual column | Data type | Required? | Proposed mock value (SYN-CLIENT-A) | SYN-CLIENT-D | Source profile field |
|---|---|---|---|---|---|---|
| `client_service_policies` | `policy_id` | uuid default gen_random_uuid() | optional (default) | `c37e4d5b-ce81-5efe-a2d9-bdf3b9829890` | `ad9d9189-5ce7-5f54-8255-686e9e0a1faa` | uuid5(client, version, category, key) (computed) |
| `client_service_policies` | `client_id` | text | **required** | `SYN-CLIENT-A` | `SYN-CLIENT-D` | client_master.client_id |
| `client_service_policies` | `config_version` | int4 | **required** | `1` | `1` | client_master.configuration_version |
| `client_service_policies` | `service_category` | text | **required** | `uc02_assignment` | `uc02_assignment` | 'uc02_assignment' (assignment rules) or 'cfg_section' (the six sections) |
| `client_service_policies` | `service_key` | text | **required** | `hr_record` | `hr_record` | assignment category, or the section name |
| `client_service_policies` | `is_available` | bool default false | optional (default) | `true` | `true` | constant true |
| `client_service_policies` | `is_mandatory` | bool default false | optional (default) | `false` | `false` | false (assignment) / true (section) |
| `client_service_policies` | `eligibility_rules` | jsonb default '{}'::jsonb | optional (default) | `{}` | `{}` | constant {} |
| `client_service_policies` | `policy_details` | jsonb default '{}'::jsonb | optional (default) | `{"team":"HR Operations (synthetic)","use_case":"","mode...` | `{"team":"HR Operations (synthetic)","use_case":"","mode...` | assignment: {team, use_case, mode, escalation_owner}; section: {section, schema_version, document_hash, document} |
| `client_service_policies` | `responsible_role` | text | optional (nullable) | `NULL` | `NULL` | (no profile field: NULL) |
| `client_service_policies` | `effective_from` | date | optional (nullable) | `(default)` | `(default)` | client_master.effective_from (sections only) |
| `client_service_policies` | `effective_to` | date | optional (nullable) | `(default)` | `(default)` | client_master.effective_until (sections only) |
| `client_service_policies` | `is_active` | bool default true | optional (default) | `true` | `true` | constant true |
| `client_service_policies` | `created_at` | timestamptz default now() | optional (default) | `(default)` | `(default)` | (not supplied: default now()) |

### `client_milestone_templates`

| Database table | Actual column | Data type | Required? | Proposed mock value (SYN-CLIENT-A) | SYN-CLIENT-D | Source profile field |
|---|---|---|---|---|---|---|
| `client_milestone_templates` | `milestone_id` | uuid default gen_random_uuid() | optional (default) | `473b6b60-dacc-5bda-92fc-603fcd78b077` | `dd4d9b80-bc1e-54e0-8820-5dd033e0fd5b` | uuid5(client, version, key) (computed) |
| `client_milestone_templates` | `client_id` | text | **required** | `SYN-CLIENT-A` | `SYN-CLIENT-D` | client_master.client_id |
| `client_milestone_templates` | `config_version` | int4 | **required** | `1` | `1` | client_master.configuration_version |
| `client_milestone_templates` | `milestone_key` | text | **required** | `day_one_ready` | `day_one_ready` | uc02.milestones[].milestone_key |
| `client_milestone_templates` | `title` | text | **required** | `Ready for day one` | `Ready for day one` | [].title |
| `client_milestone_templates` | `offset_days` | int4 default 0 | optional (default) | `-1` | `-1` | [].offset_days |
| `client_milestone_templates` | `required_task_keys` | text[] default '{}'::text[] | optional (default) | `["laptop_request","system_access","payroll_setup"]` | `["laptop_request","system_access","safety_induction"]` | [].required_task_keys |
| `client_milestone_templates` | `eligibility_rules` | jsonb default '{}'::jsonb | optional (default) | `{}` | `{}` | constant {} |
| `client_milestone_templates` | `is_active` | bool default true | optional (default) | `true` | `true` | constant true |

### `client_holidays`

| Database table | Actual column | Data type | Required? | Proposed mock value (SYN-CLIENT-A) | SYN-CLIENT-D | Source profile field |
|---|---|---|---|---|---|---|
| `client_holidays` | `client_id` | text | **required** | `SYN-CLIENT-A` | `SYN-CLIENT-D` | client_master.client_id |
| `client_holidays` | `holiday_date` | date | **required** | `2026-12-02` | `2026-09-23` | shared_configuration.calendar.holidays[].date |
| `client_holidays` | `name` | text default 'Public Holiday'::text | optional (default) | `SYNTHETIC holiday 1` | `SYNTHETIC holiday 1` | [].name |
| `client_holidays` | `is_observed` | bool default true | optional (default) | `true` | `true` | [].observed |

## 3. Client-specific data by topic (what each profile contributes)

| Topic | SYN-CLIENT-A | SYN-CLIENT-B | SYN-CLIENT-C | SYN-CLIENT-D |
|---|---|---|---|---|
| Employee onboarding requirements | 7 fields, 2 roles, 5 grades (section row `uc01_employee_record`) | 8 fields, 2 roles, 4 grades (section row `uc01_employee_record`) | 8 fields, 2 roles, 4 grades (section row `uc01_employee_record`) | 7 fields, 2 roles, 4 grades (section row `uc01_employee_record`) |
| Identity and education documents | passport, photo, emirates_id, degree_certificate | national_id, address_proof, degree_certificate, professional_qualification | passport, emirates_id, photo, degree_certificate | passport, photo, education_certificate |
| Employment and background verification | background_verification, experience_letter, payslips | background_verification, relieving_letter, payslips | background_verification, executive_references, experience_letter, payslips | background_verification, experience_letter, agency_documents |
| Visa and work authorisation | entry_permit, biometrics_arrival | none (no visa route) | visa_status_change, biometrics | work_permit_processing, residence_permit |
| Medical and insurance | medical_test_arrival, insurance_enrolment, medical_fitness_report, insurance_enrolment_form | insurance_enrolment, insurance_nomination | medical_test, family_insurance_enrolment, insurance_enrolment, medical_fitness_report | medical_test, insurance_enrolment, medical_fitness_report, insurance_enrolment_form |
| Payroll and banking | payroll_setup, bank_details | bank_account_setup, payroll_setup, tax_registration_proof, bank_details | payroll_setup, bank_details | payroll_setup, bank_details |
| Working calendar and holidays | working_days [1, 2, 3, 4, 5], tz Asia/Dubai, 2 holidays | working_days [1, 2, 3, 4, 5, 6], tz Asia/Kolkata, 2 holidays | working_days [1, 2, 3, 4, 5], tz Asia/Dubai, 3 holidays | working_days [7, 1, 2, 3, 4], tz Asia/Riyadh, 2 holidays |
| Task templates and dependencies | 15 templates, 14 dependency links | 11 templates, 12 dependency links | 15 templates, 14 dependency links | 16 templates, 15 dependency links |
| Equipment and approved access | equipment: laptop-standard, headset-basic, monitor-27; access: email-teams-account, source-control-access, ci-pipeline-access | equipment: laptop-standard, headset-basic; access: email-teams-account, erp-finance-read, payroll-system-access | equipment: laptop-executive, mobile-phone, headset-basic; access: email-teams-account, board-reporting-access, privileged-admin-access | equipment: laptop-basic, mobile-phone, safety-kit; access: email-teams-account, ops-dashboard-access |
| Orientation and training | general_orientation, engineering_induction, infosec_awareness; policies: nda, hr_handbook, code_of_conduct, infosec_policy | general_orientation, finance_compliance_training; policies: nda, hr_handbook, financial_controls, data_privacy | general_orientation, leadership_briefing, executive_compliance; policies: nda, hr_handbook, code_of_conduct, conflict_of_interest, delegation_of_authority | general_orientation, safety_induction, client_site_orientation; policies: nda, hr_handbook, safety_policy, site_rules |
| Client-specific service policies | 20 rows (14 assignment rules + 6 section documents) | 15 rows (9 assignment rules + 6 section documents) | 18 rows (12 assignment rules + 6 section documents) | 21 rows (15 assignment rules + 6 section documents) |
| Milestones | day_one_ready (requires 3 tasks) | day_one_ready (requires 3 tasks) | day_one_ready (requires 3 tasks) | day_one_ready (requires 3 tasks) |

Profiles: SYN-CLIENT-A UAE Software Engineer G5 Indian (overseas hire); SYN-CLIENT-B India Finance Analyst G4 Indian; SYN-CLIENT-C UAE Senior Manager G7 Indian (in-country hire);
SYN-CLIENT-D Saudi Arabia Operations Associate G3 Indian. Every value is a **synthetic assumption**: none is an approved client policy and none states a legal obligation. Each profile lists what a real owner must still
confirm (`awaiting_confirmation`). The version row is `approved` with `approved_by = synthetic-fixture` only because the live CHECK allows draft/approved/retired and the workflow reader needs `approved`; the clients are marked
`data_origin = synthetic`, so a workflow uses them only when it opts in to synthetic configuration. UC-06 is not referenced. `role_access_matrix` is excluded.

## 4. Profile fields with no dedicated column (nothing was invented)

**A. Carried in an existing JSONB payload (supported, lossless):**
- Everything in the six sections is stored whole in `client_service_policies.policy_details` (`service_category = 'cfg_section'`): all of `uc01_employee_record`, `uc04_equipment_access` (catalogues, approvers, lead times, handover, evidence roles), `uc05_orientation_policy`, `shared_configuration` (approval matrix, notification and recipient references, escalation, retry, event types), `uc03_document_validation.global`, and the non-column task fields.
- `client_document_requirements.validation_profile`: submission, formats, metadata, expiry, verification, rejection reasons, resubmission, dependencies, extras. `eligibility_rules`: nationality groups and attribute conditions.
- `client_configs.settings_payload`: country, supported locations, client status, data origin, scenario, notice, document hash.

**B. Present only in the JSONB copy, not as a queryable column:** `responsible_team`, `parallel_group`, `downstream_use_case` (assignment policy rows carry the use case), `employment_types` (also encoded as the attribute tag `employment_type:<x>` in `requires_attributes`), the nationality-to-group mapping, grade ordering (`band_order`), `excludes_attributes` (the projection refuses it; no profile uses it), Arabic titles (`title_ar` is a copy of the English title).

**C. Needs an architecture / database-owner decision:**
1. `task_templates` primary key is `(client_id, template_key)`: a second configuration version's templates cannot coexist with version 1 (needs a key change or retire-and-replace).
2. `role_access_matrix` vocabulary (HARDWARE / SOFTWARE_ACCESS / PHYSICAL_BADGE / SYSTEM_PERM; STANDARD / MANAGER_EXCEPTION / SECURITY_GATE) and its unique key `(client_id, role_id, item_id)` (no `item_kind`, no version, no grade): UC-04 must confirm how the profiles' equipment and access entitlements map.
3. `sensitive_to` and `responsible_role` have no profile source (left empty / NULL).
4. Who may approve a real client configuration version (the `approved` status is a development placeholder).
5. The missing constraint and index facts listed in section 1.
