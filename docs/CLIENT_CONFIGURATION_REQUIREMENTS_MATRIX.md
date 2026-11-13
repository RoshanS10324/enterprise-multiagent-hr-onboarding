# Client configuration requirements matrix (UC-01 to UC-05)

As of 2026-11-04. Scope: UC-01..UC-05 only. Built from the sources below; every row says whether the requirement is **DOCUMENTED**, **IMPLEMENTED**, **SYNTHETIC**
(a labelled test assumption) or **AWAITING** confirmation. Where a source is silent the row says "not documented": no real business rule was invented.

## Sources inspected

| Short | Source |
|---|---|
| BF | `UC03_Document_Validation/Onboarding_Agent_Business_Framework_v0.1.docx` (the business framework; sections 2, 5, 6 DL-xx, 8, 10 GR-xx, 11 EC-xx, 12 AC-xx, 15 OQ-xx) |
| U3C | `UC03_Document_Validation/UC-03_Document_Information_Validation_CORRECTED (1) (1).docx` (BR-01..16, TC-01..24) |
| U1 | `UC01_Employee_Record_Creation/` (`src/sub_agent/models/*`, `docs/uc01-handover.md`, `docs/schema/uc01-schema.md`, `specs/uc01-requirements.yaml`) |
| U2 | `UC02_Onboarding_Task_Agent/` (`docs/UC02_BUSINESS_RULES.md`, `UC02_INTEGRATION_CONTRACTS.md`, `src/.../contracts.py`, `checklist.py`, `supabase_config.py`) |
| U3 | `UC03_Document_Validation/` (`knowledge/checklists/default.yaml`, `src/sub_agent/rules.py`, `specs/uc03_spec.md`) |
| U4 | `UC04_Equipment_Access_Integration_Ready/` (`docs/CONFIG_GAP_REPORT.md`, `DB_SCHEMA_SPEC.md`, `src/sub_agent/contracts.py`, `data/*.json`) |
| U5 | `UC05_Orientation-Schedule-Agent/` (`README.md`, `policy.yaml`, `src/sub_agent/models.py`, `services/*`) |
| HC | `HR-Onboarding-Common/` (`config/tenants.yaml`, `orchestration/*`, `docs/UC0x_GAP_ANALYSIS.md`) |
| DB | live Supabase Development schema (read-only inspection 2026-11-04) and `UC01.../docs/schema/live_schema.json` |

Status of the configuration itself: **no real approved client configuration exists anywhere**; every value in the four profiles is SYNTHETIC.
Owner "business owner" below means the role named in BF section 15; no individual or team is named for supplying client configuration (BF OQ-04: who edits the
knowledge layer is itself open).

## UC-01 Employee Record Creation

| ID | Requirement | Source | Config field (`uc01_employee_record.*`) | Table | Consumer | Validation | Status today | Missing / awaiting | Owner to confirm |
|---|---|---|---|---|---|---|---|---|---|
| C01-01 | Required employee fields (client, name, email, designation, joining date, work-location type, salary and terms) | U1 `models/onboarding_form.py`, `api/schemas.py:35-65` | `employee_fields` | section row | UC-01 | required-field and type checks | DOCUMENTED + IMPLEMENTED in code (hardcoded) | Full form field list is open (U1 handover Q-12). Extra per-client fields in profiles are SYNTHETIC | process SME |
| C01-02 | Work-location type OFFICE / CLIENT_SITE; site id when client site | U1 `models/enums.py`, `onboarding_form.py` | `work_location_types`, `client_master.supported_locations` | `client_configs.settings_payload`, section row | UC-01, UC-02, UC-04 | enum; site required for CLIENT_SITE | IMPLEMENTED (enum hardcoded) | No client-site list exists (OQ-07 owner of client-site profile) | business owner |
| C01-03 | Supported roles, grades, nationality groups, employment types per client | BF section 2 ("Which documents are needed: by role, grade, nationality and location"), DL-03, DL-10 | `supported_roles`, `grades`, `nationality_groups`, `employment_types` | section row; `task_templates`/`client_document_requirements` arrays | UC-01 validation, UC-02 | role/grade/group/type must be declared by the client | **NOT in UC-01 code**: no role catalogue, grade, nationality or employment-type concept (U1 audit); orchestrator input carries `role_id`, `grade`, `nationality_group` but UC-01 does not validate them (HC `UC02_GAP_ANALYSIS.md:131`) | Real catalogues; nationality-to-group mapping | client HR / process SME |
| C01-04 | Client-specific eligibility | BF DL-17, EC-52 | `eligibility_rules` | section row | UC-01 / retrieval | outcome eligible / hold / review | not documented in UC-01 | Real rules | client HR |
| C01-05 | Start conditions: offer accepted AND form approved, else hold, do not contact candidate | BF DL-01, EC-19; U3C BR-01 | `onboarding_record_requirements`, `eligibility_rules` (hold rules) | section row | UC-01, UC-02 | both required | DOCUMENTED + IMPLEMENTED (`start_gate.py`) | none | - |
| C01-06 | Trigger event and idempotency; case creation | U1 `models/events.py`, `open_case.py`; HC `orchestration/contracts.py:63-65` | `trigger_event` | section row | UC-01 -> UC-02 | required fields, `event_id` idempotency | IMPLEMENTED locally; how offer-accepted reaches UC-01 is open (OQ-01 / Q-11); topic names undocumented | trigger source, topic/queue names | process SME |
| C01-07 | Duplicate prevention (per client; open case links, name-only is a non-blocking notice) | BF EC-38, EC-39; U1 `duplicate_check.py` | `duplicate_prevention` | section row | UC-01 | match on email / id number within client | IMPLEMENTED; matching assumption AWAITING (Q-23) | confirm matching keys | process SME |
| C01-08 | Information handed to UC-02 (case id, employee id, joining date, role, grade, nationality group, location, attributes) | U2 `UC02_INTEGRATION_CONTRACTS.md` section 1; HC `UC02_GAP_ANALYSIS.md:131, 325` | `uc02_handoff_mapping` | section row | UC-02 | required fields present | UC-02 side IMPLEMENTED; UC-01 does not send grade, nationality group, attributes, approval timestamps (UNVERIFIED) | UC-01 to send the fields | UC-01 team |
| C01-09 | Error handling (404 / 403 / 409 / 422 codes) | U1 `api/error_handlers.py` | (not client-specific) | - | UC-01 | - | IMPLEMENTED; structured conflict codes a known gap (#26) | - | UC-01 team |

## UC-02 Onboarding Task Management

| ID | Requirement | Source | Config field (`uc02_onboarding_tasks.*`) | Table | Consumer | Validation | Status today | Missing / awaiting | Owner |
|---|---|---|---|---|---|---|---|---|---|
| C02-01 | Client task templates by role, grade, nationality, location | U2 BR-02; BF section 2 | `task_templates[].applicability` | `task_templates` | UC-02 | applicability values declared by client; dependencies exist, acyclic | DOCUMENTED + IMPLEMENTED (reader `supabase_config.py`) | Real templates per client | client HR / HR ops |
| C02-02 | Owners, teams, assignment | U2 BR-03; BF section 8 | `owner_role`, `responsible_team`, `assignment_rules` | `task_templates`, `client_service_policies` (`uc02_assignment`) | UC-02 | rule per category | IMPLEMENTED; the `uc02_assignment` convention is PROPOSED (awaiting config owners) | approve convention; real team names | configuration owners |
| C02-03 | Dependencies and parallel activities | U2 BR-04, BR-05; BF DL-06 | `depends_on`, `parallel_group` | `task_templates.depends_on_template_keys` | UC-02 | no unknown key, no cycle | IMPLEMENTED | - | - |
| C02-04 | Priority, SLA, due dates from joining date backwards, working calendar | U2 BR-06; BF DL-07, AC-07 | `priority`, `offset_days`, `sla_working_days`, `due_date_calculation` | `task_templates`, `client_configs.working_days/timezone`, `client_holidays` | UC-02 | calendar covers date range | IMPLEMENTED; UAE built-in calendar provisional (D-5) | approved holiday calendars per client | client HR |
| C02-05 | HR approval before the plan is released; approver and delegate | U2 BR-11; BF section 8, GR-01, GR-02 | `hr_approval`, `shared_configuration.approval_matrix` | section row; `task_templates.approval_role` | UC-02, approval portal | approver role exists | IMPLEMENTED (signed callback); portal external | real approvers and delegates (BF "Approvals: who approves each step, and delegates") | client HR |
| C02-06 | Document gate: mandatory documents before downstream | BF DL-03, GR-07; U3C BR-09 | `document_gates` | section row | UC-02 | gate lists mandatory docs | IMPLEMENTED (`flow.document_gate`) | - | - |
| C02-07 | Reminders on the client's schedule, then hand to HR; escalation; at-risk and overdue | BF DL-14, GR-12; U2 BR-10 | `reminders`, `escalations`, `overdue_monitoring`, `shared_configuration.escalation_rules` | section row | UC-02 | numeric, roles exist | IMPLEMENTED as drafts and records; no scheduler | client reminder cadence is SYNTHETIC; scheduler is platform work | client HR / platform |
| C02-08 | Visa versus background-check ordering is a client rule | BF DL-08, OQ-02 | `visa_vs_background_check` | section row | UC-02 | enum | SYNTHETIC values; OQ-02 open | decision | process SME |
| C02-09 | Day-one readiness milestones | BF section 5 stage 4 | `milestones` | `client_milestone_templates` | UC-02 | required tasks exist | IMPLEMENTED reader; 0 rows live before this work | - | - |

## UC-03 Document and Information Validation

| ID | Requirement | Source | Config field (`uc03_document_validation.documents[]`) | Table | Consumer | Validation | Status today | Missing / awaiting | Owner |
|---|---|---|---|---|---|---|---|---|---|
| C03-01 | Document checklist by role, grade, nationality, location; never collect unlisted documents | U3C BR-03; BF DL-03, GR-15 | `applicability`, `mandatory` | `client_document_requirements` | UC-03, UC-02 | applicability declared | UC-03 reads ONE hardcoded `default.yaml`; client id never selects a checklist (U3 audit) | per-client lists | client HR admin |
| C03-02 | Identity, passport, visa, work permit, residence; education; employment; payslips; NDA; policy acks; family; bank and payroll; medical; insurance | BF section 5 "Documents collected at offer acceptance"; U3 `default.yaml` (10 documents) | `document_key`, `category` | same | UC-03 | category enum | DOCUMENTED (offer, ID, education, work experience, agreements, family, bank); work permit, residence, medical, insurance, bank are **not documented as UC-03 documents** (U3 audit) | real lists | client HR / UC-03 |
| C03-03 | Mandatory vs optional per client | BF "clients decide which documents are needed" | `mandatory`, `submission.requirement` | `is_mandatory` | UC-03 | flag agrees with requirement | `required:false` is declared but ignored by UC-03 code (HC `UC03_GAP_ANALYSIS.md` section 3) | - | UC-03 team |
| C03-04 | Required metadata, accepted formats | U3 `default.yaml`; `api/uc03.py` (pdf, jpeg, png, tiff; no size limit) | `required_metadata`, `accepted_formats` | `validation_profile` | UC-03 | format enum | IMPLEMENTED partly; size limit and per-document formats not documented | size limits | UC-03 team |
| C03-05 | Submission deadline | BF section 9 example ("by Thursday 23 October") | `submission.deadline_offset_days` | `validation_profile` | UC-02, UC-03 | integer | **not documented** as a rule; example only. SYNTHETIC | real deadlines | client HR |
| C03-06 | Expiry rules | BF DL-04; U3 `rules.py` | `expiry` | `validation_profile` | UC-03 | days | expiry vs validation date IMPLEMENTED; minimum remaining validity not documented | grace period / minimum validity | client HR |
| C03-07 | Verification: complete, readable, not expired, right person; **HR decides authenticity** | BF DL-04; U3C BR-05; GR-07, GR-09 | `verification` | `validation_profile` | UC-03 | `authenticity_decided_by = hr` | IMPLEMENTED | - | - |
| C03-08 | **No** OCR/antivirus/identity/authority verification may be claimed | U3 audit; U3 `uc03_spec.md` section 2 | `global.capabilities` (antivirus, identity, authority = false) | `validation_profile.global` | all | schema forbids `true` | Local text extraction exists but is not on the orchestrated path; no antivirus, identity or authority verification exists | - | - |
| C03-09 | Approval and HR review | U3C BR-11 | `verification.approval_required` | `validation_profile` | UC-03, UC-02 | boolean | IMPLEMENTED (`policy/approval_gates.yaml` documented, not loaded) | - | UC-03 team |
| C03-10 | Rejection reasons and resubmission | U3 `state/enums.py` (`ValidationFinding`); BF EC-02 | `rejection_reasons`, `resubmission` | `validation_profile` | UC-03, UC-02 | enum; replacement request needs HR approval | enum IMPLEMENTED; corrected resubmission is wrongly flagged DUPLICATE (HC `UC03_GAP_ANALYSIS.md`); resubmission limit not documented | max attempts | UC-03 team |
| C03-11 | Dependencies and blocking conditions | BF section 5 (visa starts when passport, photo and ID received) | `depends_on_tasks`, `blocks_tasks` | `validation_profile` | UC-02 | tasks exist | SYNTHETIC | real triggers | process SME |
| C03-12 | Sensitive documents referred to as "HR item"; unrequested documents not used | BF GR-06, EC-05; U3C BR-12, BR-15 | `sensitive`, `global.unrequested_document_policy` | `validation_profile` | UC-03 | - | rule IMPLEMENTED; retention policy not documented | retention / deletion rule | client HR |
| C03-13 | Confidence threshold | U3 `rules.py:17` (0.7, hardcoded in four places) | `global.confidence_threshold` | `validation_profile.global` | UC-03 | 0..1 | hardcoded today | confirm value | UC-03 team |

## UC-04 Equipment and Approved Access

| ID | Requirement | Source | Config field (`uc04_equipment_access.*`) | Table | Consumer | Validation | Status today | Missing / awaiting | Owner |
|---|---|---|---|---|---|---|---|---|---|
| C04-01 | Equipment catalogue and role-based equipment | U4 `CaseConfig.role_equipment_matrix`; BF "Laptop and licences per role" | `equipment_catalogue`, `role_equipment_matrix` | section row; `role_access_matrix` (kind EQUIPMENT) | UC-04 | items in catalogue; role supported | IMPLEMENTED in code (two JSON files, SYNTHETIC); grade, nationality, location entitlements not modelled (U4 `CONFIG_GAP_REPORT.md:35`); no catalogue/quantity model | real matrices | business owner |
| C04-02 | Software and application access, access categories, permissions | U4 `role_access_matrix`; BF "Equipment and access" | `access_catalogue`, `role_access_matrix` | section row; `role_access_matrix` (kind ACCESS) | UC-04 | same | IMPLEMENTED SYNTHETIC; access level, MFA, licences not modelled | real access lists | client IT |
| C04-03 | Approval matrix, delegates, exception approver | U4 `approver_config`; BF GR-03, EC-31, EC-37 | `approver_config`, `out_of_matrix_policy` | section row | UC-04 | three classes present | IMPLEMENTED; exception approver not named in the Framework | approvers | business owner |
| C04-04 | Lead times (equipment, access, client-site clearance) | U4 `lead_times` (0..365) | `lead_times` | section row; `role_access_matrix.lead_time_days` | UC-04, UC-02 | integer range | IMPLEMENTED SYNTHETIC | real values per client/country | business owner |
| C04-05 | Handover: collect / deliver / ready at desk; missed slot | U4 `handover_rules`; BF EC-32 | `handover_rules` | section row | UC-04 | enum | IMPLEMENTED SYNTHETIC | client choice | client IT |
| C04-06 | Provisioning prerequisites and completion evidence; evidence roles | U4 `evidence_roles`; BF GR-07 | `provisioning_prerequisites`, `completion_evidence`, `evidence_roles` | section row | UC-04 | - | IMPLEMENTED (evidence format open) | evidence format | UC-04 team |
| C04-07 | Client-site and country rules | U4 `client_site_rules`, `country_rules`; BF DL-13, EC-34, EC-36 | `client_site_rules`, `country_rules` | section row | UC-04 | optional | MISSING in both U4 clients; SYN-CLIENT-D carries a SYNTHETIC block | owner of client-site profile (OQ-07) | business owner |
| C04-08 | Return / cancellation | BF EC-22, EC-23, EC-44 | (not configured) | - | UC-04 | - | item state CANCELLED exists; no equipment-return policy documented | return policy | client IT |

## UC-05 Orientation and Policy Acknowledgement

| ID | Requirement | Source | Config field (`uc05_orientation_policy.*`) | Table | Consumer | Validation | Status today | Missing / awaiting | Owner |
|---|---|---|---|---|---|---|---|---|---|
| C05-01 | Orientation, induction, team introduction, buddy introduction (scheduling) | BF section 5; U5 `models.py` `SessionType`, `services/scheduling.py:58` | `scheduling.session_types`, `orientation_programmes` | section row | UC-05 | session types known | session types and durations IMPLEMENTED (hardcoded) | durations to config | UC-05 team |
| C05-02 | Role-based and location-specific training, mandatory training, schedules, completion deadlines, assessments | BF section 5 ("Orientation and team introductions") | `orientation_programmes[]`, `location_specific` | section row | UC-05, UC-02 | applicability, deadline | **not documented and not modelled in UC-05** (U5 audit). Profile values are SYNTHETIC design proposals | whole programme catalogue | client HR / UC-05 team |
| C05-03 | Policy documents and acknowledgement categories, evidence | BF section 5 ("NDA and HR policies... tracks acknowledgement"); U3C | `policy_acknowledgements[]` | section row | UC-05, UC-03, UC-02 | evidence required | Acknowledgement tracking was **retired** from UC-05 (`README.md` known limitations); UC-02 has task `policy_ack`; UC-03 holds NDA/policy documents | which use case owns acknowledgement; policy lists | process SME |
| C05-04 | Reminder and escalation for acknowledgements | BF DL-14, GR-12 | `reminder_rules` | section row | UC-05, UC-02 | numeric | not documented for UC-05 | cadence | client HR |
| C05-05 | Policy Q&A knowledge base with client scope | BF section 7; U5 `context_layer/knowledge/policies` | (document references only) | - | UC-05 | tenant isolation | IMPLEMENTED with SYNTHETIC documents; real policies are the client's | real policy library | client HR |
| C05-06 | Welcome-message templates and branding per client | U5 `ClientConfig` (`models.py:137-160`), SQLite `client_configs(client_id, body)` | `shared_configuration.notification_templates` (references only) | section row | UC-05 | placeholders limited | IMPLEMENTED in UC-05's own SQLite store; not central | migrate to central | UC-05 team |

## Shared configuration

| ID | Requirement | Source | Config field (`shared_configuration.*`) | Table | Consumer | Status today | Missing / awaiting |
|---|---|---|---|---|---|---|---|
| CSH-01 | Working days, holidays, timezone, SLA calendar | U2 BR-06; U4 `work_calendar`; U5 `policy.yaml` (global, empty holidays); U1 `client_holidays` (proposed) | `calendar` | `client_configs.working_days/timezone`, `client_holidays` | UC-01, UC-02, UC-04, UC-05 | each use case keeps its own today | approved calendars per client and country |
| CSH-02 | Approval matrices with delegates | BF section 8, GR-01..03, EC-37 | `approval_matrix` | section row | all | per-agent policy files | real approvers |
| CSH-03 | Notification templates and recipient mapping (references only) | BF "Messages and tone"; U2 `UC02_OUTLOOK_GRAPH_SETUP.md` | `notification_templates`, `recipient_mapping_refs` | section row | UC-02 and others | UC-02 recipients come from a deployment file | real templates; recipient file ownership |
| CSH-04 | Escalation and retry policy | BF GR-12; U2 `flow.MAX_*_ATTEMPTS` | `escalation_rules`, `retry_policy` | section row | all | constants in code | confirm |
| CSH-05 | Event types and integration contract references | U2 contracts; HC orchestration | `event_types`, `integration_contract_refs` | section row | all | documented in code and docs | agreed contracts for UC-03, UC-04, UC-05 senders |
| CSH-06 | Tenant orchestration settings | HC `config/tenants.yaml` (timezone, joining-day run time, employee identity scheme; CLIENT-TEST-A/B only) | `client_master.timezone` (run time and identity scheme stay in the orchestrator file) | - | orchestrator | SYNTHETIC | real tenants |

## Cross-cutting guardrails reflected in the configuration

* One client's configuration is never used for another (BF GR-16, AC-11): retrieval is keyed by client and never falls back.
* A client setting never overrides a guardrail (BF GR-17, EC-51): the schema fixes `authenticity_decided_by = hr`, `approval_required = true` on messages, `out_of_matrix_policy = exception_approval_required`.
* No salary, bank, medical or special-category value appears in any configuration (BF GR-06, GR-18); such documents are only marked `sensitive`.
* Only synthetic data in dev (BF GR-13): every profile carries `data_origin = synthetic`.
