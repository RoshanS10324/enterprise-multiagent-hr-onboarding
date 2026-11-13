# UC-02 Task Planning on Supabase: architecture

Status as of 2026-11-06. This describes the orchestrated path, where UC-02 runs inside the central orchestrator. UC-02's
separate signed-event runtime (`RuntimeFlow`, `docs/UC02_RUNTIME_AND_ACTIVATION.md`) uses the same Supabase store and
is unchanged.

## 1. Layers

```
Control Center (onboarding-ui-prototype)          reads /v1/orchestration/cases/* -- never UC-02's tables or Supabase keys
      │  HTTP (identity headers, verified by the server)
      ▼
Orchestration API (hr_onboarding_common.orchestration.api)
      ▼
Central orchestrator (OnboardingOrchestratorAgent, LangGraph)
      │  stage plan_tasks / replan_tasks → adapters/uc02_tasks.py, uc02_replanning.py
      │  (uc02_runtime.py: configuration, case check, failure classification, trace, projection)
      ▼
UC-02 (onboarding_task_management: UC02SubAgent → OnboardingService → planner/selection/checklist)
      ▼
Repository: OnboardingStore → SupabaseTableClient (mapping, tenant scoping, commit marker)
            SupabaseConfigRegistry/SupabaseConfigSource (approved configuration, calendar)
      ▼
SupabaseRestClient (PostgREST over HTTPS, service-role key, server side only)
      ▼
Supabase PostgreSQL (shared public schema)

The orchestrator's own state: SupabaseCaseStateStore → case_orchestration_state (conditional PATCH on version)
```

## 2. Workflow

```
UC01 intake ──► onboarding_cases row (UC01 owns it)
      ▼
UC02 plan_tasks ──► onboarding_tasks, task_dependencies, plan_revisions (revision 1), audit_events, agent_runs
      │              result → orchestrator → case_orchestration_state (stage_outcomes.plan_tasks, task_plan, onboarding_plan)
      ▼
UC03 document_collection / submission / validation  (reads the plan version from case_orchestration_state)
      │  correction only ──► stays in UC03 (no UC02 call, no revision)
      │  planning-impacting change (new mandatory document, attribute such as visa_required, HR requirement change)
      ▼
UC02 replan_tasks ──► plan_revisions (revision N+1; earlier rows unchanged), changed task rows
      ▼
UC03 revalidates against the new revision
      ▼
UC04 equipment_access  ||  UC05 orientation (both receive verified_context from the stored state)
      ▼
merge ─► day-one readiness (HR sign-off) ─► joining-day welcome ─► complete
```

The orchestrator decides every transition (`route_after_*`, `dependencies.py`). UC-02 returns a result and never
decides the next stage.

## 3. Configuration resolution (Supabase mode)

```
case (onboarding_cases: client_id, joining date)
  → client_configs: present, is_active; data_origin/environment marker
      (mock or synthetic only with UC02_ENV=development|test and UC02_ALLOW_SYNTHETIC_CONFIG=1)
  → client_config_versions: newest version that is approved, has an approver and timestamp and a config_hash,
      and is in force on the joining date
  → task_templates (that version, is_active)          → planned tasks, owners, offsets, dependencies
  → client_milestone_templates (that version)          → milestones; "required tasks" = tasks a milestone requires
  → client_document_requirements (that version)        → document requirements
  → client_service_policies uc02_assignment / cfg_section → team mapping / eligibility catalogue
  → client_holidays                                     → working calendar (required, never assumed)
```

Anything missing, a draft, retired, not yet in force, or malformed raises `MissingConfigurationError`. The stage then
becomes BLOCKED with error `MISSING_DATA` and the blocker `UC02_CONFIG_MISSING`. **No task, dependency or revision is
written, and no default is invented.** When the configuration is approved, `resume` continues the case.

## 4. Read and write responsibilities

| Table | UC-02 | Orchestrator | Other UCs |
|---|---|---|---|
| `onboarding_cases` | read | checks existence and tenant before its first write | UC-01 writes |
| `client_*`, `task_templates` | read | — | configuration owner writes |
| `onboarding_tasks` | writes rows with the `uc02` marker | — | other UCs' rows are never touched |
| `task_dependencies`, `plan_revisions` | write | — | — |
| `audit_events`, `agent_runs` | append UC-02 rows | — | each UC its own |
| `case_orchestration_state` | **never** | writes (CAS on `version`) | UC-05 reads |

## 5. Tenant isolation

`client_id` goes with every query (details in `UC02_SUPABASE_DATABASE_MAPPING.md` §7). The checks happen at three levels:

- **Orchestrator:** the actor's tenant must match the case (404 otherwise).
- **UC-02 adapter:** before planning in Supabase mode, `verify_case` requires the case to be this tenant's
  `onboarding_cases` row. Another tenant's case is FAILED with `TENANT_CONTEXT_CONFLICT`. An `employee_no` that
  disagrees is BLOCKED with `UC02_CASE_IDENTITY_MISMATCH`.
- **Store:** queries are scoped, and cross-tenant writes and edges are refused.

## 6. Idempotency

| Replay | Why nothing is duplicated |
|---|---|
| `plan_tasks` again (resume, retry, at-least-once) | UC-02 recognises the stored onboarding (`created: false`). Tasks upsert on `task_id` (`<case>#<template_key>`), dependencies on their pair, and an unchanged record writes no new snapshot. |
| `replan_tasks` with the same feedback | The UC-02 ledger `docfb:<feedback_id>` returns the first outcome (`duplicate_replay: true`). There is no new version, revision, task or snapshot. |
| Revision rows | Insert-if-missing by (`case_id`, `revision`). A concurrent duplicate (409) counts as present. |
| Orchestrator writes | Conditional `PATCH … version=eq.n`, so a lost update becomes `StateVersionConflict`. A branch step is folded and retried. |
| Execution trace | One `agent_runs` row per execution id (upsert). Audit entries are per execution by design; each execution is a fact. |

## 7. Revisions

`plan_revisions.revision` is the plan version: 1, 2, 3 and so on. Each row records `trigger_source`,
`previous_revision`, `reason`, `correlation_id`, `applied_by`, `applied_at`, the task ids that were added, removed or
rescheduled, and the documents that were added or removed. Earlier revisions are never modified.

## 8. Recovery

- **The orchestrator persists after every stage.** It writes to `case_orchestration_state`, so a restart resumes from
  the committed row. No step depends on process memory: the plan, its version and its documents travel through the row.
- **UC-02's sub-agent is rebuilt on demand** over the same store, and loads the case from Supabase.
- **UC-02's write order puts the case-state snapshot last**, as a commit marker. If the backend stops part-way, the
  previous plan version stays in force, and the retried save rewrites the task rows. A restart never sees a new version
  whose tasks were not stored.
- **"Latest snapshot" means the highest `snapshot_seq`**, not a timestamp. Two snapshots inside the clock's resolution
  used to tie.
- **A crash after UC-02 commits but before the orchestrator records it** is safe. On resume UC-02 recognises its plan
  (`created: false`), and nothing is duplicated (scenario B, tested and run live).

## 9. Error handling (orchestrated stage)

| UC-02 failure | Stage | Error code | Blocker |
|---|---|---|---|
| Configuration missing, draft, expired or invalid | BLOCKED | `MISSING_DATA` | `UC02_CONFIG_MISSING` |
| Calendar missing | BLOCKED | `MISSING_DATA` | `UC02_CALENDAR_MISSING` |
| Employee outside the client's eligibility | BLOCKED | `MISSING_DATA` | `UC02_EMPLOYEE_NOT_ELIGIBLE` |
| Offer or form facts missing | BLOCKED | `MISSING_DATA` | `UC02_PRECONDITION_FAILED` |
| Unstated attribute the templates depend on | BLOCKED (plan kept, shown) | `CHECKLIST_REVIEW_REQUIRED` | same |
| UC-01 case not stored in Supabase | BLOCKED | `MISSING_DATA` | `UC02_CASE_NOT_FOUND` |
| Case of another tenant | FAILED | `TENANT_CONTEXT_CONFLICT` | same |
| Supabase unreachable, 5xx or 429 | RETRY_PENDING (retry budget) | `UC02_STORE_UNAVAILABLE` | same |
| Supabase refused (credentials, schema, constraint) | BLOCKED | `UC02_STORE_ERROR` | same |
| `UC02_ENV` missing, or an unsafe synthetic opt-in | BLOCKED | `UC02_RUNTIME_MISCONFIGURED` | same |

Each blocker carries `required_from`, `required_action`, `resolution` and `retryable` (`dependency_blockers.py`). The
API maps state-store failures explicitly: 503 `STATE_STORE_UNAVAILABLE`, 409 `CASE_NOT_IN_DATABASE`, 409
`STATE_VERSION_CONFLICT`.

## 10. Audit and agent runs

UC-02 writes to `audit_events`, tenant-scoped, with ids and codes only:

- each of its business events
- the `UC02_CASE_STATE` snapshot
- `orchestration.plan_tasks` / `orchestration.replan_tasks` entries with outcome `started`, then `completed`,
  `replanned`, `blocked`, `failed` or `retry_pending`

`agent_runs` gets one row per orchestrated execution (`run_id = UC02-<execution id>`). The status is `acted` or
`exception`, because the live CHECK constraint allows only `proposed`, `acted` and `exception`; the error code goes in
`error_summary`. The UC-02 advisory loop keeps its own row (`RUN-…`). A trace that cannot be written is logged and
reported (`task_plan.trace_recorded`), and it never changes the business outcome.

## 11. Modes

| `UC02_STORE_MODE` | Store | Configuration | Use |
|---|---|---|---|
| `supabase` | `SupabaseTableClient` | `SupabaseConfigSource` (approved versions) | **Runtime.** Fails closed. Requires `UC02_ENV` and the scoped credentials `UC02_WORKER_ENV__SUPABASE_URL` / `…SERVICE_ROLE_KEY`. |
| `sqlite` | local file | synthetic registry | local development only |
| unset / `memory` | none | synthetic registry | unit tests and demos; `task_plan.store = memory` |

- **Scoped names in the shared `.env`:** in `HR-Onboarding-Common/.env`, UC-02's settings are written scoped
  (`UC02_WORKER_ENV__UC02_STORE_MODE`, `…__UC02_ENV`, `…__UC02_ALLOW_SYNTHETIC_CONFIG`, `…__SUPABASE_URL`,
  `…__SUPABASE_SERVICE_ROLE_KEY`). That file is also loaded by UC-02's own processes and tests
  (`hr_onboarding_common.config.get_settings`), where a plain `UC02_STORE_MODE` would switch UC-02's standalone runtime
  as well. The orchestrator maps the scoped names (`uc02_runtime.uc02_environment`), and no worker receives them.
- **Production:** `UC02_ENV=production` refuses `memory`, `sqlite` and synthetic configuration.
- **Server start-up:** `serve_orchestrator.py` refuses `ORCHESTRATOR_STATE_STORE=supabase` with a non-Supabase UC-02.
- **Credentials:** the service-role key exists only on the backend. Workers never receive UC-02's or the orchestrator's
  key (`workers/config.child_environment`), and the browser never receives any key.
