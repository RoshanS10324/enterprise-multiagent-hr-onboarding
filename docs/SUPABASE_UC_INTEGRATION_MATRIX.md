# Supabase integration matrix: UC01–UC05 and the orchestrator

What each part reads and writes in Supabase when the backend runs with the settings in
`HR-Onboarding-Common/.env` (2026-11-06), and how far that is proven. **Status** uses the report's vocabulary:
VERIFIED (written and read back on the live project), IMPLEMENTED (code and tests, not yet run live), BLOCKED (named
missing key/row/decision), NOT IMPLEMENTED. Evidence: `docs/SUPABASE_COMPLETE_INTEGRATION_REPORT.md`.

| UC | Supabase mode (setting) | Tables | Reads | Writes | Status |
|---|---|---|---|---|---|
| Orchestrator | `ORCHESTRATOR_STATE_STORE=supabase` (+ `ORCHESTRATOR_SUPABASE_*`) | `case_orchestration_state`, `approval_requests`, `joining_day_jobs`, `client_service_policies` | case state (CAS on `version`), UC04 configuration section | case state with typed handoffs, its own approval requests, joining-day jobs | VERIFIED (case state, approvals: previous change; jobs: this change). Typed handoffs IMPLEMENTED |
| UC01 | `UC01_WORKER_ENV__SUPABASE_URL` + `UC01_WORKER_ENV__UC01_FIELD_ENCRYPTION_KEY` (+ interim writes flag) | `onboarding_cases`, `onboarding_forms`, `person_records`, `audit_events`, `employee_number_counters`, `uc01_approvals`, `domain_events_outbox` | person directory, counters | case, form, person record, audit, outbox | **BLOCKED**: `UC01_FIELD_ENCRYPTION_KEY` (the team's key, decision B1) is not set; nothing may generate one. The form-id mismatch that would have failed the form write is fixed |
| UC02 | `UC02_WORKER_ENV__UC02_STORE_MODE=supabase` | `onboarding_cases` (read), `client_configs`, `client_config_versions`, `client_holidays`, `client_service_policies`, `task_templates`, `onboarding_tasks`, `task_dependencies`, `plan_revisions`, `approval_requests`, `audit_events`, `agent_runs` | case, approved configuration, calendar | tasks, dependencies, plan revisions, state snapshots, approvals, runs | VERIFIED (previous change; unchanged) |
| UC03 | `UC03_STORE_MODE=supabase` (in the orchestrator's process, on `ORCHESTRATOR_SUPABASE_*`) | `onboarding_cases` (read), `candidate_documents`, `document_validations`, `uc03_case_state`, `approval_requests`, `audit_events`; Storage bucket `Doc_Collection` | case link, collection and revisions, UC03 case state, its audit | file + metadata row (upload), collection + revisions, UC03 case state, validation results, audit, approvals | VERIFIED (case `e548a40d…`): upload, collection, validation, reload in a new process, cross-tenant refusal. Types without a `candidate_documents.document_type` value: BLOCKED (schema) |
| UC04 | `UC04_WORKER_ENV__UC04_STATE_MODE=supabase` + `UC04_IDENTITY_PROFILE=PLATFORM`; `UC04_CONFIG_SOURCE=supabase` | `onboarding_cases` (read), `audit_events` (state snapshots + audit), `agent_runs`, `client_service_policies` (configuration, read by the orchestrator) | parent case, its own state | versioned state snapshots, audit entries, runs | State: VERIFIED (snapshot written and read back by a new process; parent case from `onboarding_cases`). Evaluation in Supabase mode: **BLOCKED** on configuration (`UC04_CONFIG_PROJECTION_UNDECIDED`). `provisioning_requests` / `provisioning_tickets` / `equipment_handovers`: NOT IMPLEMENTED (vocabulary decision) |
| UC05 | `UC05_WORKER_ENV__UC05_DATABASE_MODE=supabase` | `onboarding_cases` (read), `client_configs`, `client_config_versions`, `client_holidays`, `buddy_profiles`, `buddy_assignments`, `orientation_sessions`, `session_participants`, `approval_requests`, `communications`, `audit_events`, `agent_runs` | employee (from the case), configuration, calendar, buddies | case link, sessions, participants, buddy assignments, approvals, welcome draft, audit, runs | VERIFIED (case `e548a40d…`): employee from `onboarding_cases`, induction session, participants, approval, run; cross-tenant refusal. Buddy assignment and welcome on the live project: not exercised (SYN-UAE-TEST has no buddy profiles; the joining day is in the future) |

## Per-UC notes

- **UC03 vocabulary.** `candidate_documents.status` starts at `UPLOADED` (UC03's result store then sets `VALIDATED` /
  `REJECTED`). UC03's own type and status values are kept in the `uc03_*` columns of `document_validations`; the shared
  columns hold the CHECK list's mapped value (`COMPLETE → COMPLIANT`, `BLOCKED → NON_COMPLIANT`, `APPROVED → ACCEPTED`,
  `OUTSTANDING_ITEMS → INCOMPLETE`, `READY_FOR_PRO → READY`).
- **UC04 state** is kept in `audit_events` rows with `action = UC04_CASE_STATE`, one per saved version (`event_id`
  derived from case, version and sequence, so concurrent writers collide on the primary key). No `uc04_*` table.
- **UC05 participants.** `session_participants.email` is NOT NULL. UC05 holds an address only for the new hire (the
  case's `candidate_email`). The team lead (`onboarding_cases.manager_id`, an id) and the buddy have none in any table,
  so they are recorded on the session (`session_metadata.participants_without_email`, reason `EMAIL_UNKNOWN`) and their
  replies in `session_metadata.responses_without_participant_row`; the participant listing returns both sources.
- **UC05 welcome.** `communications.status` accepts DRAFT / APPROVED / DISPATCHED / FAILED / CANCELLED. A simulated send
  stays `APPROVED` with no `dispatched_at` there; the orchestrator's result reports `delivery_status = SENT_SIMULATED`.
