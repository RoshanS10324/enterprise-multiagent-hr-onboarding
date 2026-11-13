# Supabase table ownership

Every table in the shared Supabase project (`public` schema), who owns it, who reads and writes it at runtime,
and how it is isolated per tenant. Table names, columns, keys and row counts were read from the live project's
PostgREST catalogue on 2026-11-06. Allowed values were established with zero-write probes: each probe row points
at a case id that does not exist, so it can only be refused (23514 = a CHECK refused the value, 23503 = the value
passed every CHECK and only the missing parent stopped it). Nothing was written.

**Runtime** = written or read while a case is processed. **Seed / configuration** = loaded once by an operator or
a seed script, read at runtime. **Unused** = present in the database, no code path in this repository reads or
writes it.

The `database/` folder (CSV exports of these tables) is reference material only. No runtime path reads it.

## 0. Read / create / update / delete per UC (as the code does it)

Traced from the code paths the backend runs in its Supabase configuration. No use case deletes rows (UC02 removes
only its own task and dependency rows when a plan revision drops a task; audit and approval rows are never deleted).

| UC | Table | Read | Create | Update | Delete | Purpose |
|----|-------|------|--------|--------|--------|---------|
| UC01 | `onboarding_cases` | yes | yes | yes (status, stage) | no | the case: canonical `case_id`, employee number, joining date, placement |
| UC01 | `onboarding_forms` | yes | yes | yes (approval) | no | the internal onboarding form |
| UC01 | `person_records` | yes | yes | no | no | duplicate check / person directory |
| UC01 | `employee_number_counters` (via `rpc/next_employee_no`) | yes | no | yes (increment) | no | employee number sequence per tenant |
| UC01 | `uc01_approvals` | yes | yes | yes | no | re-hire / transfer / duplicate approvals |
| UC01 | `audit_events`, `domain_events_outbox` | yes / no | yes | no | no | audit chain, CaseOpened event |
| UC02 | `onboarding_cases` | yes | no | no | no | the case it plans for |
| UC02 | `client_configs`, `client_config_versions`, `client_holidays`, `client_service_policies`, `task_templates` | yes | no | no | no | approved configuration and calendar |
| UC02 | `onboarding_tasks`, `task_dependencies` | yes | yes | yes | own rows on replan | the plan |
| UC02 | `plan_revisions` | yes | yes | no | no | one row per plan version |
| UC02 | `approval_requests`, `audit_events`, `agent_runs` | yes | yes | approvals only | no | approvals, state snapshots, runs |
| UC03 | `onboarding_cases` | yes | no | no | no | case link (`onboarding_case_id`) |
| UC03 | `candidate_documents` | yes | yes (upload path) | yes (status after validation) | no | uploaded file metadata |
| UC03 | `document_validations` | yes | yes | no | no | one row per validation request |
| UC03 | `uc03_case_state` | yes | yes | yes (versioned) | no | UC03 case state; document collection (`collection:<case>`) |
| UC03 | `approval_requests`, `audit_events` | yes | yes | approvals only | no | UC03 approvals and audit |
| UC04 | `onboarding_cases` | yes | no | no | no | parent case (employee, tenant, status, deleted flag) |
| UC04 | `audit_events` | yes | yes (state snapshots `UC04_CASE_STATE`, audit) | no | no | UC04 state and audit |
| UC04 | `agent_runs` | no | yes | no | no | UC04 runs |
| UC04 | `client_service_policies` (read by the orchestrator for UC04) | yes | no | no | no | `uc04_equipment_access` configuration section |
| UC04 | `provisioning_requests`, `provisioning_tickets`, `equipment_handovers`, `role_access_matrix` | no | no | no | no | not used: section 8 |
| UC05 | `onboarding_cases` | yes | no | no | no | the employee and joining date (read on every run) |
| UC05 | `client_configs`, `client_config_versions`, `client_holidays`, `buddy_profiles` | yes | no | no | no | configuration, calendar, buddy pool |
| UC05 | `buddy_assignments` | yes | yes | yes (confirm / decline) | no | buddy proposals and assignments |
| UC05 | `orientation_sessions` | yes | yes | yes (status, metadata) | no | sessions |
| UC05 | `session_participants` | yes | yes | yes (RSVP) | no | participants with a known e-mail |
| UC05 | `communications` | yes | yes | yes (status) | no | welcome message |
| UC05 | `approval_requests`, `audit_events`, `agent_runs` | yes | yes | approvals only | no | approvals, ledger and state rows, runs |
| Orchestrator | `case_orchestration_state` | yes | yes | yes (compare-and-swap on `version`) | no | workflow state and typed handoffs |
| Orchestrator | `approval_requests` | yes | yes | yes (decision) | no | its own gates (form approval, day-one sign-off) |
| Orchestrator | `joining_day_jobs` | yes | yes | yes | no | joining-day job runs |
| Orchestrator | `onboarding_cases` | yes | no | no | no | existence and tenant check before storing the case state |

## 1. Case spine (UC01)

| Table | Owner | Writers | Readers | Stage | Purpose | Foreign keys | Tenant isolation | Kind |
|---|---|---|---|---|---|---|---|---|
| `onboarding_cases` | UC01 | UC01 (`open_onboarding_case`, interim mode only) | UC02, UC03, UC04, UC05, orchestrator (existence + tenant check), document upload | intake | The case: employee number, joining date, candidate e-mail, department, manager | `client_id → client_configs` | `client_id` column; every reader filters `case_id` **and** `client_id` | Runtime |
| `onboarding_forms` | UC01 | UC01 | UC01 | intake | The internal onboarding form of a case. `form_id` is a **uuid** | `client_id → client_configs`, `case_id → onboarding_cases` | `client_id` | Runtime |
| `uc01_onboarding_form_details`, `uc01_onboarding_budget`, `uc01_onboarding_budget_lines`, `uc01_onboarding_document_checklist`, `uc01_onboarding_form_field_status` | UC01 | UC01 document-intake service | UC01 | intake (form extraction) | Extracted form details, budget, checklist and per-field status | `form_id → uc01_onboarding_form_details / onboarding_forms`; `client_id`, `case_id` on the two parent tables | `client_id` on the parent rows; children reached through `form_id` | Runtime (not on the orchestrated worker path) |
| `person_records` | UC01 | UC01 | UC01 (duplicate check) | intake | Person directory used by the duplicate check | `client_id → client_configs`, `case_id → onboarding_cases` | `client_id` | Runtime |
| `offers`, `offer_forms` | UC01 | UC01 e-mail intake | UC01 | pre-intake | Offer records | `client_id → client_configs`; `form_id → onboarding_forms` | `client_id` | Runtime (e-mail intake only) |
| `intake_discrepancies` | UC01 | UC01 | UC01 | intake | Form vs offer discrepancies | `case_id → onboarding_cases` | through `case_id` (no `client_id` column) | Runtime (not on the orchestrated path) |
| `hris_sync_records` | UC01 | UC01 | UC01 | intake | HRIS synchronisation log | `case_id → onboarding_cases` | `client_id` | Runtime (not on the orchestrated path) |
| `pending_starts` | UC01 | UC01 | UC01 | intake | Starts held for placement | none | `client_id` | Runtime (not on the orchestrated path) |
| `employee_number_counters` | UC01 | UC01 via `rpc/next_employee_no` | UC01 | intake | Per-tenant employee-number sequence | `client_id → client_configs` (primary key) | one row per tenant | Seed (one row per tenant) + runtime increment |
| `uc01_approvals` | UC01 | UC01 (re-hire, transfer, possible duplicate) | UC01 | intake | UC01's own approvals | `case_id → onboarding_cases` | through `case_id` | Runtime |
| `domain_events_outbox` | UC01 | UC01 (`CaseOpenedEvent`) | none in this repository | intake | Outbox of domain events | `case_id → onboarding_cases` | `client_id` | Runtime |

## 2. Task planning (UC02)

| Table | Owner | Writers | Readers | Stage | Purpose | Foreign keys | Tenant isolation | Kind |
|---|---|---|---|---|---|---|---|---|
| `onboarding_tasks` | UC02 | UC02 | UC02, orchestrator (task projection) | plan_tasks, replan_tasks | The plan's tasks; `status_summary.uc02` marks UC02-owned rows | `case_id → onboarding_cases` | `client_id`; UC02 reads and deletes only inside the case's tenant | Runtime |
| `task_dependencies` | UC02 | UC02 | UC02 | plan_tasks | Task dependency edges; cross-tenant and cross-case edges are refused | `task_id`, `depends_on_task_id → onboarding_tasks` | `client_id` | Runtime |
| `plan_revisions` | UC02 | UC02 | UC02, orchestrator | plan_tasks, replan_tasks | One row per plan version (`revision` = plan_version) | `case_id → onboarding_cases` | `client_id` | Runtime |
| `task_templates` | configuration | seed | UC02 | plan_tasks | Task templates per client | `client_id → client_configs` | `client_id` | Seed |
| `onboarding_reminders`, `onboarding_status_updates` | UC02 (schema) | none | none | — | Reminders and status updates | several | `client_id` | **Unused** |

## 3. Documents (UC03)

| Table | Owner | Writers | Readers | Stage | Purpose | Foreign keys | Tenant isolation | Kind |
|---|---|---|---|---|---|---|---|---|
| `candidate_documents` | UC03 | document upload (orchestrator backend) | UC03, orchestrator | document_submission | One row per uploaded file: Storage reference, size, type, checksum | `case_id → onboarding_cases` | `client_id` column; written only after the case is found in the same tenant | Runtime |
| `document_validations` | UC03 | UC03 result store | UC03, orchestrator | validate_documents | One row per validated document revision; UC03's exact values in the `uc03_*` columns, mapped values in the shared columns | `document_id → candidate_documents`, `case_id → onboarding_cases` | **no `client_id` column**: isolated through `case_id` (whose tenant is checked) and `document_id` | Runtime |
| `uc03_case_state` | UC03 | UC03 case store | UC03 | validate_documents | UC03's own case state (authorised actors, revisions received, status); optimistic concurrency on `version` | `onboarding_case_id → onboarding_cases` | `client_id` column | Runtime |
| `client_document_requirements` | configuration | seed | UC02 configuration | plan_tasks | Per-client document requirements | none | `client_id` | Seed |

Value vocabulary (probed):

| Column | Accepted values |
|---|---|
| `candidate_documents.document_type` | PASSPORT, OFFER_LETTER, VISA, NATIONAL_ID, EDUCATIONAL_CERTIFICATE, BACKGROUND_CHECK, SIGNED_POLICY, MEDICAL_FITNESS (others may exist; these are the ones verified) |
| `candidate_documents.status` | UPLOADED, VALIDATED, REJECTED, EXPIRED |
| `document_validations.compliance_item_status` | INCOMPLETE, COMPLIANT, NON_COMPLIANT |
| `document_validations.visa_pack_status` | NOT_APPLICABLE, INCOMPLETE, READY |
| `document_validations.hr_review_status` | PENDING, ACCEPTED, REJECTED, NOT_REQUIRED |
| `document_validations.uc03_*`, `document_status`, `approval_status`, `evidence_status`, `required_next_action` | free text (UC03's own enum values) |

UC03 document types with **no** accepted `candidate_documents.document_type`: `experience_letter`, `payslips`,
`family_documents` / `family_dependent_documents`. A schema change is required to store them (section 8).

## 4. Equipment and access (UC04)

| Table | Owner | Writers | Readers | Stage | Purpose | Foreign keys | Tenant isolation | Kind |
|---|---|---|---|---|---|---|---|---|
| `provisioning_requests` | UC04 | none yet | none | equipment_access | One row per requested item | `case_id → onboarding_cases` | `client_id` | Runtime (not written: section 8) |
| `provisioning_tickets` | UC04 | none | none | equipment_access | IT tickets of a request | `request_id → provisioning_requests` | through `request_id` | Runtime (not written) |
| `equipment_handovers` | UC04 | none | none | equipment_access | Delivery / collection of equipment | `case_id`, `request_id` | through `case_id` | Runtime (not written) |
| `role_access_matrix` | configuration | seed | UC04 (proposed) | equipment_access | Role → item, kind, lead time, approval class | `client_id → client_configs` | `client_id` | Seed (**0 rows**) |

Value vocabulary (probed): `provisioning_requests.item_kind` ∈ HARDWARE, SOFTWARE_ACCESS, PHYSICAL_BADGE,
SYSTEM_PERM (UC04 uses EQUIPMENT / ACCESS); `provisioning_requests.state` accepts DRAFT_PREPARED, PENDING_APPROVAL,
APPROVED, TICKET_CREATED, DELIVERED, ACTIVE, FAILED, CANCELLED (14 of UC04's 16 item states are refused);
`clearance_status` ∈ NOT_REQUIRED, PENDING, CLEARED, REJECTED.

## 5. Orientation, buddy, team introduction, welcome (UC05)

| Table | Owner | Writers | Readers | Stage | Purpose | Foreign keys | Tenant isolation | Kind |
|---|---|---|---|---|---|---|---|---|
| `orientation_sessions` | UC05 | UC05 (Supabase mode) | UC05, orchestrator (via UC05) | orientation_scheduling, team_introduction | Sessions: type, start/end UTC, link, room, status, calendar event | `case_id → onboarding_cases` | `client_id` | Runtime |
| `session_participants` | UC05 | UC05 (only participants with a known e-mail: `email` is NOT NULL) | UC05 | orientation_scheduling, team_introduction | Participants and RSVP; participants without an address are kept on the session (`session_metadata`) | `session_id → orientation_sessions` | through `session_id` | Runtime |
| `buddy_profiles` | configuration | seed | UC05 | buddy_assignment | Buddy pool | `client_id → client_configs` | `client_id` | Seed (2 rows) |
| `buddy_assignments` | UC05 | UC05 | UC05 | buddy_assignment | Proposed / confirmed buddy | `case_id → onboarding_cases` | `client_id` | Runtime |
| `communications` | UC05 | UC05 | UC05 | joining_day_welcome | Welcome message draft and delivery | `case_id → onboarding_cases` | `client_id` | Runtime |
| `joining_day_jobs` | orchestrator | orchestrator's joining-day scheduler (`SupabaseJoiningDayJobStore`; one row per case and joining date, claim = compare-and-swap on `job_payload->>version`) | the scheduler | joining_day_welcome | Joining-day job runs | `client_id → client_configs` | `client_id` | Runtime |

Value vocabulary (probed): `orientation_sessions.session_type` ∈ HR_INDUCTION, DEPARTMENT_WELCOME, BUDDY_SYNC;
`orientation_sessions.status` ∈ SCHEDULED, CANCELLED, COMPLETED, RESCHEDULED; `buddy_assignments.status` ∈
PROPOSED, CONFIRMED, DECLINED, COMPLETED; `communications.status` ∈ DRAFT, APPROVED, DISPATCHED, FAILED,
CANCELLED (**SENT_SIMULATED is refused**: a simulated send is recorded as APPROVED with no `dispatched_at`, and the
exact status lives in UC05's audit state row); `communications.comm_type` accepts WELCOME_EMAIL.

## 6. Configuration (all use cases)

| Table | Owner | Writers | Readers | Purpose | Tenant isolation | Kind |
|---|---|---|---|---|---|---|
| `client_configs` | configuration | seed | UC01 (FK), UC02, UC05, orchestrator | Tenant row: company, timezone, working days, settings | primary key `client_id` | Seed (7 rows) |
| `client_config_versions` | configuration | seed / approval | UC02, UC05 | Versioned client configuration documents; only an approved / active / published version in effect is used | `client_id` | Seed (9 rows) |
| `client_holidays` | configuration | seed | UC02, UC05 | Holidays per tenant | `client_id` | Seed (12 rows) |
| `client_service_policies`, `client_milestone_templates` | configuration | seed | UC02 configuration | Policies and milestones | `client_id` | Seed |

## 7. Cross-cutting

| Table | Owner | Writers | Readers | Purpose | Foreign keys | Tenant isolation | Kind |
|---|---|---|---|---|---|---|---|
| `case_orchestration_state` | orchestrator | orchestrator (`ORCHESTRATOR_STATE_STORE=supabase`); compare-and-swap on `version` | orchestrator, API | Workflow status, stage outcomes, handoffs, pending actions, blockers | `case_id → onboarding_cases` (primary key) | `client_id`; a case of another tenant is refused | Runtime |
| `approval_requests` | shared | orchestrator (its HITL gates), UC02, UC03, UC05 (Supabase mode) | the same | Approval requests and decisions; `workflow_module` names the writer | `case_id → onboarding_cases`, `client_id → client_configs` | `client_id` | Runtime |
| `audit_events` | shared, append-only | UC01, UC02, UC03, UC04 (Supabase mode), UC05 (Supabase mode), orchestrator | the same | Audit trail; UC02, UC04 (`UC04_CASE_STATE`) and UC05 also keep versioned state snapshots here | `case_id → onboarding_cases` (nullable) | `client_id` (no FK) | Runtime |
| `agent_runs` | shared | UC02, UC05 (Supabase mode), UC04 (Supabase mode) | operations | One row per agent run | `case_id → onboarding_cases` | `client_id` | Runtime |

## 8. Gaps found in the schema (no data was invented to close them)

| Gap | Effect | Required change |
|---|---|---|
| `candidate_documents.document_type` has no value for `experience_letter`, `payslips`, family documents | A document of those types is stored in Storage and validated, but its metadata row cannot be written | Widen the CHECK constraint (schema owner) |
| `document_validations` has no `client_id` | Tenant isolation is indirect (through `case_id`) | None required; documented |
| `provisioning_requests.item_kind` / `state` use a vocabulary different from UC04's | UC04 item state cannot be stored there without a mapping decision; `role_access_matrix` (which could supply the kind) has 0 rows | UC04 owner decision on the mapping, or seeded `role_access_matrix` rows |
| `uc04_case_state`, `uc04_replay_results` (UC04's proposed migration) | Not applied | Not needed: UC04 state is kept in `audit_events` snapshots (see the architecture document) |
| `session_participants.email` is NOT NULL | No row for a team lead or buddy: no table holds their e-mail address (`onboarding_cases.manager_id` is an id; `buddy_profiles` has no e-mail column) | An employee directory with work addresses, or a nullable column (schema owner). Until then UC05 records them on the session |
| `communications.status` refuses SENT_SIMULATED | A simulated delivery cannot be marked as such in that column | None required: UC05 keeps APPROVED + no `dispatched_at`, and the exact status in its audit state row |
| Every case table's `case_id` is a foreign key to `onboarding_cases` | Nothing downstream can be stored for a case UC01 did not write | UC01's Supabase writes (needs `UC01_FIELD_ENCRYPTION_KEY`) |
