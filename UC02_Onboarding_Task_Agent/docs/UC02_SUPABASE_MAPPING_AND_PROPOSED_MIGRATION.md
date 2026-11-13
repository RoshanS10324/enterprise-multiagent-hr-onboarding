# UC-02 ↔ Supabase: verified mapping and proposed migration

> **Superseded in part (2026-11-04).** Statements here that Supabase persistence is unbuilt, unverified or blocked, that no LLM layer exists, or that only in-memory tests pass are historical. The verified state is in `UC02_FINAL_ARCHITECTURE_AND_STATUS.md`, `UC02_TRACEABILITY_MATRIX.md` and `UC02_INTEGRATION_CHECKLIST.md`.

Verified 2026-11-02 against the live shared dev project (PostgREST schema, service-role key; read-only).
**Nothing here has been applied. No schema, RLS or shared-table change was made.**

## Mapping (UC-02 table → live table)

| UC-02 | Live | Field renames (UC-02 → live) | Gaps |
|---|---|---|---|
| onboarding | onboarding_cases | onboarding_id→case_id (uuid), employee_id→employee_no, location→location_name, project→project_name, attributes→onboarding_attributes | UC-01 owns the row: NOT NULL `candidate_name`, `candidate_email`, `hr_officer_id`, `work_location_type`, variant flags. UC-02 must **read/update, never create**. Live status vocabulary (`DOCUMENTS_PENDING`…) ≠ UC-02 (`active`/`cancelled`). |
| task | onboarding_tasks | onboarding_id→case_id, owner→owner_role, assignee_id→assigned_actor_id, assignee_team→assigned_team; live id convention `<case_id>#<task_key>` | No column: `failed_report_count`, `max_failed_reports`, `delay_reason`, `delay_kind`, `delay_reported_by`, `delay_reported_at`. |
| task_dependency | task_dependencies | depends_on_id→depends_on_task_id | none |
| plan_revision | plan_revisions | onboarding_id→case_id; PK `revision_id` uuid | none |
| approval_request | approval_requests | onboarding_id→case_id (uuid) | Live NOT NULL `confidence_score`, `rationale`, `workflow_module`, `subject_ref`, `sla_due_at`: UC-02 has no confidence score; defaults unverified. |
| audit_event | audit_events | at→occurred_at, before/after_status→before_state/after_state (jsonb) | `actor_type` NOT NULL not carried by UC-02. |
| agent_run | agent_runs | onboarding_id→case_id | none |
| config ref | client_config_versions | — | Present: 3 draft rows + CANADA draft. None approved. |
| (none) | — | — | **UC-02 case state has no live home**: plan_status/plan_events/plan_version, final_status/final_submissions, applied_updates (idempotency ledger), revision, correlation_id, config_version. |

Config source tables `task_templates`, `client_holidays`, `client_milestone_templates`,
`client_document_requirements`, `client_service_policies` exist but hold **0 rows**; no config version is approved,
so UC-02 correctly refuses to plan from live config today.

Foreign keys and RLS policies are **not visible** through PostgREST; they need a DB-owner check.

## Proposed migration (NOT applied; needs TL approval)

```sql
alter table onboarding_tasks
  add column failed_report_count int not null default 0,
  add column max_failed_reports int,
  add column delay_reason text,
  add column delay_kind text,
  add column delay_reported_by text,
  add column delay_reported_at timestamptz;

create table uc02_case_state (
  case_id uuid primary key references onboarding_cases(case_id),
  client_id text not null,
  plan_version int not null default 1,
  plan_status text not null default 'proposed',
  plan_events jsonb not null default '[]',
  final_status text not null default 'not_submitted',
  final_submissions jsonb not null default '[]',
  applied_updates jsonb not null default '{}',
  revision int not null default 0,
  correlation_id text,
  config_version text not null default '',
  updated_at timestamptz not null default now()
);
-- plus RLS by client_id, matching the existing tables.
```

## Environment

`UC02_STORE_MODE=supabase` (or `PERSISTENCE_BACKEND=supabase`), `NEXT_PUBLIC_SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.
In this mode UC-02 connects, verifies the schema (`supabase_schema.verify_schema`) and raises `PersistenceError`
listing the gaps; it never falls back to memory. Once the schema check passes it still raises
"row-mapping store not implemented" until the mapping client is built.

## Open decisions

1. Approve the migration above (or choose another home for UC-02 state).
2. Confirm the target is a dev branch/project, not production, before any runtime write.
3. Name the approved test case/employee (UC-01 must create the `onboarding_cases` row).
4. Who loads and approves USA/Canada templates, holidays and policies.
5. Defaults for `approval_requests` NOT NULL columns, and DB-owner confirmation of FKs/RLS.

---

## Update: no-migration mapping implemented (`supabase_store.py`)

The adapter now maps UC-02 onto the live tables with **no schema change**, so the migration above is optional:

* `onboarding_cases` is read-only (UC-01 owns it). `onboarding_id` == case uuid; a missing case is refused.
* UC-02 case state (plan/final status, plan history, ledger, revision, config version, changed joining date,
  location, project) is an append-only `UC02_CASE_STATE` snapshot in `audit_events.after_state`; the latest overlays the case row.
* Task fields with no column (`failed_report_count`, `max_failed_reports`, `delay_*`) live in `onboarding_tasks.status_summary['uc02']`.
* Approvals carry `workflow_module='uc02_onboarding_task_management'`; UC-02 audit rows carry `reference='uc02:<id>'`; neither reads other workflows' rows.
* The approval and audit tables' defaults (`confidence_score`, `rationale`, `sla_due_at`, `actor_role`) exist, so nothing is fabricated.

Still unverified against the real DB (needs an approved dev database): CHECK constraints (e.g. `actor_type` values other than
`SYSTEM`, approval `gate`/`outcome` vocab), foreign keys, RLS, and the production-vs-dev identity of this project.
`onboarding_reminders` / `onboarding_status_updates` are not yet written by the store (reminder attempts and the update ledger
persist through audit and the case snapshot).
