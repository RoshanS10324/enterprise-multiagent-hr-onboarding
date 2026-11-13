# Client configuration JSON: schema and field reference

Schema: `client_configuration/schema/client_configuration.schema.json` (JSON Schema draft 2020-12, `schema_version` **1.0.0**). Profiles:
`client_configuration/profiles/client_a_config.json` .. `client_d_config.json`, generated deterministically by `client_configuration/build_profiles.py`
(a data-authoring tool; agents never import it). Validate with `python scripts/uc02_client_config.py validate` (in `UC02_Onboarding_Task_Agent`) or the test
`tests/test_client_configuration.py`. There is no section for any use case other than UC-01..UC-05, and the schema rejects unknown top-level keys.

**Conventions.** An empty applicability list means "applies to all". Dates are ISO `YYYY-MM-DD`. `offset_days` is relative to the joining date (negative =
before). `working_days` uses 1 = Monday .. 7 = Sunday (the existing reader's convention). Roles are the UC-02 role vocabulary. Nothing secret, no addresses,
no personal data; references (`template-library://`, `recipient-map://`, `client-policy-library://`) point at deployment-owned stores.

## Top level

| Key | Meaning |
|---|---|
| `schema_version` | `1.0.0` |
| `client_master` | identity, country, locations, version, dates, approval, data origin |
| `shared_configuration` | calendar, approval matrix, notification references, recipient mapping references, escalation, retry, event types, contract references |
| `uc01_employee_record` ... `uc05_orientation_policy` | one section per use case |
| `awaiting_confirmation` | what a real owner must still confirm, with the owning team (never empty) |

## `client_master`

`client_id` (e.g. `SYN-CLIENT-A`), `client_name`, `client_status` (`active`/`inactive`), `country{code,name}`, `supported_locations[{code,name,country_code,work_location_type}]`,
`timezone`, `configuration_version` (int), `effective_from`, `effective_until` (null = open), `approval_status` (`draft`/`approved`/`retired`), `approved_by`, `approved_at`,
`data_origin` (`synthetic`/`approved_client`), `created_at`, `updated_at`, `synthetic_notice`, `scenario`. `active` / `expired` are derived (see the architecture document).

## `shared_configuration`

`calendar{timezone, working_days[], sla_calendar, holidays[{date,name,observed}]}` | `approval_matrix[{action, approver_role, delegate_role, separation_of_duties}]` |
`notification_templates[{key, audience, channel, language, approval_required: true, template_ref}]` | `recipient_mapping_refs[{role_or_team, reference_key}]` |
`escalation_rules[{rule_key, trigger, after_working_days, escalate_to_role, channel}]` | `retry_policy{max_attempts, backoff_seconds[]}` |
`event_types[{event, producer, consumers[], idempotency_key_field}]` | `integration_contract_refs[{contract, document}]`.

## `uc01_employee_record`

`employee_fields[{field,label,required,type,sensitive}]`, `supported_roles`, `grades[{grade_id, band_order}]`, `nationality_groups[{group_code,label,nationality_codes[]}]`
(maps an employee's nationality code to the opaque group the other agents use), `employment_types`, `work_location_types`, `eligibility_rules[{rule_key, outcome: eligible|hold|review, applies_to?, reason}]`,
`field_validation`, `onboarding_record_requirements` (offer accepted and approved form both required), `trigger_event`, `duplicate_prevention` (scope `client`),
`uc02_handoff_mapping[{uc02_field, source, required}]`.

## `uc02_onboarding_tasks`

`task_templates[{template_key, category, title, description, owner_role, responsible_team, priority, depends_on[], parallel_group, offset_days, sla_working_days, evidence_required, approval_role, downstream_use_case, applicability}]`,
`assignment_rules[{category, team, downstream_use_case, mode, escalation_owner}]`, `document_gates`, `hr_approval{plan_approval_required: true, approver_role, delegate_role, sla_hours}`,
`due_date_calculation`, `reminders{first_after_working_days, repeat_every_working_days, handover_to_hr_call_after}`, `escalations[{rule_ref}]`,
`overdue_monitoring` (derived from due dates, never stored), `milestones`, `visa_vs_background_check` (client rule, BF DL-08).

## `uc03_document_validation`

`global{confidence_threshold, authenticity_decided_by: "hr", name_difference_policy, unrequested_document_policy, capabilities{antivirus_scanning: false, identity_verification: false, authority_verification: false}}` and
`documents[]` with: `document_key`, `name`, `category` (offer, identity, visa, education, employment, agreement, medical, insurance, family, bank_payroll, client_site), `mandatory`, `applicability`,
`submission{requirement, deadline_offset_days, channel}`, `accepted_formats`, `required_metadata`, `expiry{applies, min_remaining_validity_days, handling}`,
`verification{checks[], evidence_required, authenticity_decided_by: "hr", extraction_review, approval_required}`, `initial_status` (`pending`), `rejection_reasons[]`,
`resubmission{allowed, max_attempts, replacement_request_needs_hr_approval: true}`, `depends_on_tasks[]`, `blocks_tasks[]`, optional `min_count`, `attestation`, `sensitive`.
The schema makes it impossible to record a claim of antivirus, identity or authority verification.

## `uc04_equipment_access`

`equipment_catalogue[]`, `access_catalogue[]` (`item_code`, `name`, `kind`, `category`), `role_equipment_matrix[{role_id, grades[], items[]}]`, `role_access_matrix[{role_id, items[]}]`,
`approver_config{ACCESS, EQUIPMENT, EXCEPTION: {role, delegate_role}}`, `lead_times{EQUIPMENT, ACCESS, CLIENT_SITE_CLEARANCE}` (working days, 0..365), `handover_rules{mode: COLLECT|DELIVER|READY_AT_DESK, keep_reserved_on_missed_slot}`,
`evidence_roles`, `provisioning_prerequisites`, `completion_evidence`, `out_of_matrix_policy` (`exception_approval_required`), `client_site_rules` and `country_rules` (nullable).

## `uc05_orientation_policy`

`orientation_programmes[{programme_key, name, session_types[], mandatory, applicability, completion_deadline_offset_days, assessment{required, pass_mark_percent?}, completion_criteria, evidence_required}]`,
`policy_acknowledgements[{policy_key, name, policy_reference, mandatory, applicability, acknowledgement_deadline_offset_days, evidence_required, evidence_type}]`,
`reminder_rules`, `location_specific[{location_code, programme_keys, policy_keys}]`, `scheduling`. Policy **content** is never stored, only a reference.

## Readable example (SYN-CLIENT-A, abridged)

```json
{
  "schema_version": "1.0.0",
  "client_master": {"client_id": "SYN-CLIENT-A", "client_status": "active", "country": {"code": "AE", "name": "United Arab Emirates"},
                    "configuration_version": 1, "effective_from": "2026-01-01", "effective_until": null,
                    "approval_status": "approved", "data_origin": "synthetic", "...": "..."},
  "uc02_onboarding_tasks": {"task_templates": [
      {"template_key": "entry_permit", "category": "visa", "owner_role": "pro", "priority": "critical",
       "depends_on": ["collect_documents"], "offset_days": -12, "approval_role": null,
       "applicability": {"roles": [], "grades": [], "requires_attributes": ["location_status:overseas"], "...": []}}]},
  "uc03_document_validation": {"documents": [
      {"document_key": "passport", "category": "identity", "mandatory": true,
       "expiry": {"applies": true, "min_remaining_validity_days": 180, "handling": "reject_and_request_replacement"},
       "verification": {"authenticity_decided_by": "hr", "evidence_required": true, "approval_required": false}}]}
}
```

## The four synthetic profiles (all SYNTHETIC assumptions, not approved policies)

| Client | Country | Scenario | Distinguishing configuration |
|---|---|---|---|
| SYN-CLIENT-A | AE | Software Engineer G5, Indian national, overseas hire | entry-permit route, travel and accommodation tasks, visa and background check in parallel, engineering induction and information-security training, source-control and CI access |
| SYN-CLIENT-B | IN | Finance Analyst G4, Indian national | no visa or medical route, six-day week, tax and bank documents, payroll and finance-system approvals by the HR manager, finance compliance training |
| SYN-CLIENT-C | AE | Senior Manager G7, Indian national, in-country hire | background check must clear before the visa, family-insurance and privileged-access tasks for senior grades, executive equipment, conflict-of-interest declarations, 12-hour approval SLA |
| SYN-CLIENT-D | SA | Operations Associate G3, Indian national | Sunday-to-Thursday week, work-permit and residence route, mandatory medical and insurance, client-site clearance, mandatory safety induction with assessment |
