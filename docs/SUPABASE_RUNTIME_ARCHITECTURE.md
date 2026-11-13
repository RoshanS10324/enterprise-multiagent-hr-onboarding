# Supabase runtime architecture: UC01–UC05 and the orchestrator

Written before the implementation changes it plans (2026-11-06), from a trace of each use case's runtime path and a
read-only inspection of the live Supabase project; section 5 was completed with the decisions taken while implementing
(D11, D12). What was built and proven is in `SUPABASE_COMPLETE_INTEGRATION_REPORT.md`. Companion documents: `SUPABASE_UC_TABLE_OWNERSHIP.md` (every table),
`SUPABASE_UC_INTEGRATION_MATRIX.md`, `SUPABASE_END_TO_END_WORKFLOW.md`, `SUPABASE_COMPLETE_INTEGRATION_REPORT.md` (evidence).

## 1. The workflow

```
UC01 intake ─▶ UC02 plan_tasks ─▶ document_collection ─▶ document_submission ─▶ UC03 validate_documents
                     ▲                                                                   │
                     └──────────── UC02 replan_tasks ◀── (only a planning-impacting change)
                                                                                         │ verified
                                                         ┌───────────────────────────────┴──────────────┐
                                                         ▼                                              ▼
                                                UC04 equipment_access               UC05 orientation / buddy / team intro
                                                         └──────────────── merge ───────────────────────┘
                                                                            ▼
                                                                 day_one_readiness ─▶ joining_day_welcome (UC05)
```

The orchestrator (HR-Onboarding-Common, LangGraph) runs the stages. UC02 and UC03 run in the orchestrator's
process; UC01, UC04 and UC05 run as worker subprocesses (their top-level package names collide). A worker receives
only its own scoped settings (`UC0X_WORKER_ENV__NAME` → `NAME`, `workers/config.child_environment`).

## 2. Persistence today (before this change)

| Component | Store today | Supabase tables used today | Mock / in-memory / SQLite in the path |
|---|---|---|---|
| Orchestrator case state | `case_orchestration_state` (`ORCHESTRATOR_STATE_STORE=supabase`) | `case_orchestration_state`, `approval_requests` (write-through) | provisional (pre-intake) states in SQLite `provisional_case_states.db`; HITL inbox SQLite; LangGraph checkpoints SQLite; joining-day jobs SQLite |
| UC01 | in memory: `SUPABASE_URL` and `UC01_FIELD_ENCRYPTION_KEY` are not set for the worker | none | all ports in memory (`EMP-%06d` numbers from a memory counter) |
| UC02 | Supabase (`UC02_STORE_MODE=supabase`, done in the previous change) | `onboarding_tasks`, `task_dependencies`, `plan_revisions`, `audit_events`, `agent_runs`, `approval_requests`, configuration tables | none in Supabase mode |
| UC03 | in memory (case, audit) + SQLite document registry | `candidate_documents` only on upload, with `status='RECEIVED'` (refused by the CHECK) and lower-case types (refused) | `InMemoryCaseStore`, `InMemoryAuditStore`, `SqliteDocumentRegistry` |
| UC04 | in memory | none | `MockCaseContext`, in-memory `CaseStore`, in-memory `AuditLog`, `MockITServiceDesk`; configuration from synthetic JSON files |
| UC05 | SQLite (local mode) with three Supabase reads | `buddy_profiles`, `buddy_assignments` (read), `client_configs` (read) | SQLite repository, HITL, checkpoints, JSONL audit, simulated calendar JSON, `MockNotifier` |

Unused tables: `onboarding_reminders`, `onboarding_status_updates` (no code path), `role_access_matrix` (0 rows,
not read), `provisioning_*`, `equipment_handovers`, `joining_day_jobs` (never written).

## 3. Handoff today

Every stage reads the previous stage's result from the orchestrator's case state, which is loaded from
`case_orchestration_state` at the start of each advance and written back with compare-and-swap on `version`. There
is no in-memory handoff between stages. What is missing is a **typed** handoff: each adapter writes its own keys
(`task_plan`, `document_validation_result`, `equipment_access_result`, `orientation_schedule_result`, …) and
`stage_outcomes[stage]` holds only status, error and a result summary.

## 4. What blocks a fully Supabase-backed run (facts, not plans)

1. **Every case table's `case_id` is a foreign key to `onboarding_cases`**, which only UC01 writes. UC01's Supabase
   mode refuses to start without `UC01_FIELD_ENCRYPTION_KEY` (UC01 decision B1: the team's key; generating one is
   forbidden). Until it is set, no UC01-opened case can be stored in any downstream table, and
   `case_orchestration_state` refuses the case (`CASE_NOT_IN_DATABASE`).
2. UC01 is sent `form_id = "FORM-CASE-<hex>"`; `onboarding_forms.form_id` is a uuid, so the form write would fail
   after the case row and an employee number were already taken.
3. `candidate_documents` CHECK lists: `status` ∈ UPLOADED/VALIDATED/REJECTED/EXPIRED; `document_type` has no value for
   `experience_letter`, `payslips` or family documents.
4. UC04's item vocabulary does not fit `provisioning_requests` (14 of 16 states refused; EQUIPMENT/ACCESS vs four
   other kinds); `role_access_matrix` is empty; UC04's proposed `uc04_*` tables are not applied.
5. UC04 configuration exists in Supabase only as the `uc04_equipment_access` section of a client configuration
   document, in a different shape from UC04's `CaseConfig`; the projection is an open UC04 decision (D-1/D-2).
6. UC05's Supabase mode is refused by the worker while the per-feature sources are set, and the worker calls the
   repository without a tenant in three places (`TenantScopeRequiredError`). UC01 never writes `manager_id`, which UC05
   needs for the team lead.

## 5. Decisions for this change

| # | Decision | Why |
|---|---|---|
| D1 | **No new tables.** Use the existing schema only. | The brief; every gap is either solved with existing tables or reported with the exact missing table/row/field |
| D2 | **UC03**: use UC03's own Supabase stores (`integrations/supabase`, `UC03_STORE_MODE=supabase`, merged from `document-information-validation`) for `uc03_case_state`, `audit_events`, `approval_requests`, `document_validations`; the orchestrator injects the table client (built from the orchestrator's scoped Supabase settings) instead of letting UC03 read a bare `SUPABASE_URL`, which would reach the UC01 worker | Reuse, one source of truth, no second client configuration |
| D3 | **UC03 upload**: `candidate_documents` written with the schema's vocabulary (`UPLOADED`; UC03 type → schema type through UC03's own `mapping.DOCUMENT_TYPES`). In Supabase mode a type the schema cannot hold is **refused before anything is stored** (422 `DOCUMENT_TYPE_NOT_IN_SCHEMA`, naming the CHECK constraint), never stored without its metadata row | No `NOT_PERSISTED` success; BLOCKED over fake success |
| D4 | **UC04 state**: the existing `StateBackend` port gets an `audit_events` implementation (one append-only snapshot per version; the event id is derived from case and version, so two writers of the same version collide on the primary key = compare-and-swap). Selected with `UC04_STATE_MODE=supabase` in the UC04 worker, together with UC04's existing `DatabaseCaseContext` (`onboarding_cases`), `DatabaseAuditSink` and `DatabaseAgentRunRecorder` | UC04's own port and adapters are reused; no `uc04_*` table is needed; the same snapshot pattern UC02 and UC05 already use |
| D5 | **UC04 `provisioning_*` / `equipment_handovers`**: not written. Reported as BLOCKED with the exact vocabulary gap and the empty `role_access_matrix` | Mapping UC04's states onto a narrower list is a UC04 business decision |
| D6 | **UC04 configuration**: `UC04_CONFIG_SOURCE=supabase` makes the stage BLOCKED (`UC04_CONFIG_NOT_IN_SUPABASE`, naming `client_config_versions` / `uc04_equipment_access` and the open projection decision) instead of reading synthetic files | Configuration must come from Supabase or block |
| D7 | **UC05**: the worker runs UC05's own Supabase mode (`UC05_DATABASE_MODE=supabase`): employee from `onboarding_cases`, sessions/participants/buddy assignments/communications/approvals/audit/agent runs in Supabase. The worker's tenant-less calls are fixed; the placeholder-buddy check is applied in Supabase mode too; the schedule returned carries timezone, link and location; a simulated welcome is reported as `SENT_SIMULATED` (the `communications` column keeps APPROVED, which is what its CHECK allows) | UC05 already implements the tables; only the embedding was missing |
| D8 | **Orchestrator joining-day jobs**: a `joining_day_jobs` store (per tenant and run date) in Supabase mode | The table exists for it |
| D9 | **Typed handoffs**: `UC01Output`…`UC05Output` (case_id, client_id, stage, status, result, warnings, blockers, approval requirements, audit reference, timestamp, version) written into `stage_outcomes[stage].handoff` in `case_orchestration_state`; downstream stages read persisted state only | One contract, persisted, versioned |
| D10 | **UC01**: the orchestrator sends a uuid `form_id`, derived from the case id (a resubmitted intake names the same form). UC01's Supabase writes stay blocked on the key (B1), reported exactly | The key is the team's; nothing is generated |
| D11 | **UC05 participants without an address**: `session_participants.email` is NOT NULL and UC05 holds an e-mail only for the new hire. The team lead and buddy are recorded on the session (`session_metadata.participants_without_email`, reason `EMAIL_UNKNOWN`), their replies newest-wins in `session_metadata.responses_without_participant_row` | Found live: the first live run failed with SQLSTATE 23502. No address is invented |
| D12 | **UC04 and a soft-deleted case**: `onboarding_cases.is_deleted = true` makes the parent case WITHDRAWN for UC04 (as for UC05), whatever its status column says | Found live: UC04 read a deleted case as ACTIVE |

## 6. Target architecture

```
Browser ──HTTP──▶ Orchestrator API (service-role key stays in this process)
                     │
                     ├── case_orchestration_state   (compare-and-swap on version; typed handoffs in stage_outcomes)
                     ├── approval_requests, audit_events, joining_day_jobs
                     │
                     ├── UC02 (in-process) ── onboarding_tasks, task_dependencies, plan_revisions, audit_events, agent_runs
                     ├── UC03 (in-process) ── uc03_case_state, document_validations, audit_events, approval_requests
                     │        upload path ─── Storage (private bucket) + candidate_documents
                     ├── UC01 worker ──────── onboarding_cases, onboarding_forms, person_records, audit_events, … (needs B1)
                     ├── UC04 worker ──────── onboarding_cases (read), audit_events (state snapshots + audit), agent_runs
                     └── UC05 worker ──────── onboarding_cases (read), client_configs/_versions/_holidays, buddy_profiles,
                                              buddy_assignments, orientation_sessions, session_participants,
                                              communications, approval_requests, audit_events, agent_runs
```

Every read and write carries the tenant (`client_id`); a case of another tenant is refused. Configuration comes from
`client_configs` / `client_config_versions` (approved version in effect) and blocks when missing. `database/` is not
read by any runtime path.

Local runtime files that remain in Supabase mode, and why: LangGraph checkpoints of the orchestrator and of UC05 (an
execution cache; the authoritative state is in Supabase and a lost checkpoint re-runs from it); the orchestrator's
provisional states before UC01 has written the case (no `onboarding_cases` row can exist yet, so the foreign key
forbids storing them); UC05's simulated calendar (a simulation of an external provider, not business state).
