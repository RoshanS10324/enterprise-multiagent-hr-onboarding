# UC-02 Task Planning ↔ Supabase: database mapping

Status as of 2026-11-06. Applies to the **shared `public` schema** of the development Supabase project, through
`SupabaseTableClient` (`src/onboarding_task_management/supabase_store.py`), `SupabaseConfigSource`
(`supabase_config.py`), and the orchestrator's `SupabaseCaseStateStore`
(`HR-Onboarding-Common/src/hr_onboarding_common/orchestration/supabase_state_store.py`).

Sources: the live PostgREST schema (read 2026-11-05) and the CSV export in `database/data/`, which was used for
analysis only. **No column below is invented.** Every column named exists in the live schema. No migration was
written or applied. The separate `uc02` schema in `supabase/migrations/` was never applied and is not used.

## 1. Tables UC-02 uses

| Table | UC-02 role | Operation | Key fields | Relationship | Owner | Source of truth |
|---|---|---|---|---|---|---|
| `onboarding_cases` | Input: the authoritative case | READ (non-personal columns only) | `case_id`, `client_id`, `employee_no`, `joining_date`, `role_id`, `grade`, `department`, `manager_id`, `location_name`, `project_name`, `nationality_group`, `onboarding_attributes`, `is_deleted` | The case. A UC-02 onboarding **is** a case: `onboarding_id = case_id` | UC-01 | Supabase |
| `client_configs` | Client gate: active, working days, test-data marker | READ | `client_id`, `is_active`, `working_days`, `config_hash`, `settings_payload.data_origin` / `.environment` | Client → its versions | Client configuration owner | Supabase |
| `client_config_versions` | Version in force on the joining date | READ | `client_id`, `version`, `status`, `approved_by`, `approved_at`, `config_hash`, `effective_from`, `effective_to` | Client → version | Client configuration owner | Supabase |
| `task_templates` | Planning rules | READ | `client_id`, `config_version`, `template_key`, `category`, `owner_role`, `offset_days`, `depends_on_template_keys`, `role_ids`, `grades`, `locations`, `projects`, `nationality_groups`, `requires_attributes`, `sensitive_to`, `evidence_required`, `approval_role`, `default_priority`, `is_active` | Version → templates | Client configuration owner | Supabase |
| `client_milestone_templates` | Milestones; "required tasks" | READ | `client_id`, `config_version`, `milestone_key`, `offset_days`, `required_task_keys`, `eligibility_rules`, `is_active` | Version → milestones | Client configuration owner | Supabase |
| `client_document_requirements` | Document requirements of the version | READ | `client_id`, `config_version`, `requirement_key`, `document_type`, `is_mandatory`, `applicable_*`, `eligibility_rules`, `is_active` | Version → requirements | Client configuration owner | Supabase |
| `client_service_policies` | Team mapping (`service_category = uc02_assignment`); eligibility catalogue (`cfg_section` / `uc01_employee_record`) | READ | `client_id`, `config_version`, `service_category`, `service_key`, `policy_details`, `is_available`, `is_active` | Version → policies | Client configuration owner | Supabase |
| `client_holidays` | Working calendar (required; never assumed) | READ | `client_id`, `holiday_date`, `is_observed` | Client → holidays | Client configuration owner | Supabase |
| `onboarding_tasks` | Planned tasks | INSERT / UPDATE (upsert on `task_id`); DELETE only of UC-02's own rows on a replan | `task_id` (`<case_id>#<template_key>`), `case_id`, `client_id`, `template_key`, `plan_order`, `status`, `due_date`, `config_version`, `status_summary.uc02` | Case → tasks | **UC-02** (rows carrying the `status_summary.uc02` marker) | Supabase |
| `task_dependencies` | Dependency graph | INSERT / UPSERT on (`task_id`, `depends_on_task_id`); DELETE of UC-02's edges | `task_id`, `depends_on_task_id`, `client_id` | Task → task (same case, same client) | **UC-02** | Supabase |
| `plan_revisions` | Revision history (one row per plan version) | INSERT-if-missing on (`case_id`, `revision`); UPDATE of the same version's row by the approved-change path | `case_id`, `revision` (= plan version), `client_id`, `drivers`, `added/removed/rescheduled/retained_task_ids`, `applied_by`, `applied_at`, `approval_request_id`, `reason`, `correlation_id`, `change_summary` | Case → revisions | **UC-02** | Supabase |
| `audit_events` | UC-02 audit; `UC02_CASE_STATE` snapshots; orchestrated-execution trace | INSERT (append-only) | `event_id`, `case_id`, `client_id`, `action`, `outcome`, `detail`, `after_state`, `reference` (`uc02:` prefix), `correlation_id`, `occurred_at` | Case → events | Shared infrastructure (UC-02 rows identified by `reference`) | Supabase |
| `agent_runs` | UC-02 advisory runs and orchestrated stage runs | UPSERT on `run_id` | `run_id`, `case_id`, `client_id`, `agent_id`, `status` (`proposed` / `acted` / `exception`), `tool_calls`, `error_summary`, `started_at`, `finished_at` | Case → runs | Shared infrastructure | Supabase |
| `approval_requests` | UC-02's own gates (plan approval, date change, cancellation) | UPSERT on `request_id` | `request_id`, `case_id`, `client_id`, `gate`, `workflow_module = uc02_onboarding_task_management`, `outcome` | Case → requests | Shared infrastructure (filtered by `workflow_module`) | Supabase |
| `case_orchestration_state` | Workflow handoff | READ / UPDATE **by the orchestrator only** | `case_id`, `client_id`, `stage_statuses`, `stage_outcomes`, `completed_stages`, `blockers`, `version`, `orchestrator_context` | Case → workflow state | **Orchestrator** | Supabase |

UC-02 never writes `onboarding_cases`. A save for a case without a row in that client is refused
(`CaseNotFoundError`), and a case that belongs to another client is refused as an isolation breach
(`DataIsolationError`).

## 2. Where UC-02's result lives in `case_orchestration_state`

The orchestrator writes all of these. UC-02 returns its result to the adapter and never writes this table.

| Location | Content |
|---|---|
| `stage_statuses.plan_tasks` / `.replan_tasks` | The canonical stage keys from `orchestration/state.py` (`Stage.PLAN_TASKS`, `Stage.REPLAN_TASKS`). |
| `stage_outcomes.plan_tasks` | The execution record (`StageOutcome`: operation `initial_task_planning`, inputs, attempt, execution id, dependencies, error), plus **`result_summary`**: `status`, `error_code`, `blockers`, `plan_id`, `plan_revision`, `task_count`, `required_task_count`, `dependency_count`, `ready_count`, `blocked_count`, `required_documents`, `config_version`, `store`, `created`, `agent_run_id`, `planned_at`. |
| `stage_outcomes.replan_tasks` | The same, with `plan_revision`, `previous_plan_revision`, `feedback_id`, `duplicate_replay`, `added_tasks` / `removed_tasks` / `modified_tasks`. |
| `orchestrator_context.state.task_plan` | UC-02's record, projected: tasks (id, key, status, owner, due date, `blocked_by`), `ready` / `blocked`, `plan_version`, `revision`, `config_version`, `data_origin`, `store`, the `dependencies` graph, counts, `review_required` / `issues`, `agent_run_id`. |
| `orchestrator_context.state.onboarding_plan` / `plan_history` / `revised_plan` | Plan id and version, the plan's document requirements, and the append-only plan history. |

## 3. Column mappings that differ from UC-02's own field names

| UC-02 field | Live column |
|---|---|
| `onboarding_id` | `onboarding_cases.case_id`, `onboarding_tasks.case_id`, `plan_revisions.case_id` |
| `employee_id` | `onboarding_cases.employee_no` |
| `location`, `project`, `attributes` | `location_name`, `project_name`, `onboarding_attributes` |
| task `owner`, `assignee_id`, `assignee_team` | `owner_role`, `assigned_actor_id`, `assigned_team` |
| dependency `depends_on_id` | `depends_on_task_id` |
| task fields with no live column (delay, failed-report counters …) | `onboarding_tasks.status_summary.uc02` |
| case state (plan status, plan events, documents, ledger …) | `audit_events.after_state` of the latest `UC02_CASE_STATE` snapshot, chosen by `snapshot_seq` |

## 4. `plan_revisions` semantics (shared database)

- **`revision` is the plan version.** It is the number the orchestrator and the Control Center show (`onboarding_plan.plan_version`).
  - Revision 1 is the initial plan.
  - Each replan adds the next revision.
  - Earlier rows are never changed. Revision *N−1* stays auditable, and revision *N* is authoritative.
- **Where a row comes from.** Each row is projected from the record's append-only `plan_events`: `proposed`, `replanned_documents`, `replanned` and `revised`.
- **When it is written.** The row is written the first time a save carries that version, insert-if-missing.
- **What `change_summary` holds:**
  - `trigger_source`: one of `initial_planning`, `document_validation_feedback`, `approved_change`, `hr_plan_revision`.
  - `previous_revision`.
  - `feedback_id` or `request_id`.
  - Added and removed documents.
  - `changed_fields`.
  - `task_revision` (UC-02's internal task-replan counter).
- **Columns not written:** `previous_schedule` and `new_schedule`. The schedule of the authoritative revision is in the task rows, and `rescheduled_task_ids` lists what moved.
- **In-memory and SQLite stores** keep their original local numbering. They are used only by unit tests and demos.

## 5. Tables UC-02 does not use

| Tables | Owner | Why not |
|---|---|---|
| `onboarding_forms`, `offers`, `offer_forms`, `person_records`, `hris_sync_records`, `intake_discrepancies`, `pending_starts`, `employee_number_counters`, `uc01_*`, `domain_events_outbox` | UC-01 | UC-02 receives the verified offer and form facts through the orchestrator (`uc02_role_context`). It never reads personal data or forms. |
| `candidate_documents`, `document_validations`, `uc03_case_state` | UC-03 | UC-02 defines which documents the plan requires. UC-03 collects and validates them, and reports back only through the structured feedback. |
| `provisioning_requests`, `provisioning_tickets`, `equipment_handovers`, `role_access_matrix` | UC-04 | — |
| `orientation_sessions`, `session_participants`, `buddy_profiles`, `buddy_assignments`, `communications` | UC-05 (HR maintains `buddy_profiles`) | — |
| `onboarding_reminders`, `onboarding_status_updates` | Not written by any use case today (both are empty) | UC-02 keeps its reminder drafts and its status-update idempotency ledger in its case-state snapshot. Mapping them is a possible later step and is **not implemented**. |
| `joining_day_jobs` | No code writes it | — |

## 6. Ownership summary

| Owner | Tables |
|---|---|
| UC-01 | `onboarding_cases` and its intake and offer tables (§5) |
| **UC-02** | `onboarding_tasks` (rows with the `uc02` marker), `task_dependencies` (edges of those tasks), `plan_revisions` |
| UC-03 | `candidate_documents`, `document_validations`, `uc03_case_state` |
| UC-04 | `provisioning_*`, `equipment_handovers`, `role_access_matrix` |
| UC-05 | `orientation_sessions`, `session_participants`, `buddy_assignments`, `communications` |
| Orchestrator | `case_orchestration_state`; its own rows in `approval_requests` (`workflow_module = onboarding_orchestrator`) |
| Shared infrastructure | `audit_events`, `agent_runs`, `approval_requests` (partitioned by `reference` / `workflow_module`) |
| Client configuration owner | `client_configs`, `client_config_versions`, `task_templates`, `client_milestone_templates`, `client_document_requirements`, `client_service_policies`, `client_holidays` |

## 7. Tenant isolation (every UC-02 query)

The service-role key bypasses Row Level Security, so the filters below *are* the isolation.

- **Tenant of a case:** `onboarding_cases.client_id`. **Tenant of a task:** the `client_id` on its own row.
- **Reads:**
  - Tasks are read with `case_id` **and** `client_id`.
  - Dependencies are read with `task_id` **and** `client_id`.
  - Revisions are read with `case_id` **and** `client_id`.
  - Snapshots are read with `case_id` **and** `client_id`.
- **Updates and deletes** carry the `client_id` filter.
- **Refused writes:**
  - A dependency between tasks of two clients, or of two cases.
  - A task row of the same case id under another client.
  - Audit, run and approval rows for a case of another client.
- **Lookups by primary key** (`task_id`, `case_id`) are used only to detect ownership or a cross-tenant clash. They never return data into a record.

The tests that prove this are in `tests/test_supabase_planning_integrity.py` (`TenantIsolationTests`) and
`HR-Onboarding-Common/tests/orchestration/test_uc02_supabase_orchestration.py` (scenario F). The same behaviour was
checked live (`HR-Onboarding-Common/scripts/verify_uc02_supabase_workflow.py negative`).
