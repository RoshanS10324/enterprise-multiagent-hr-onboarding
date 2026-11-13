# UC-02 → Supabase → orchestration: implementation report

Date: 2026-11-06. Branch `Prasanna`, working tree only: **nothing is committed**. The UC-02 project lives in
`UC02_Onboarding_Task_Agent/` (the brief called it `UC02_Task_Planning/`), so these documents are in its `docs/`.

## Summary

- **Before:** UC-02 already had a Supabase store, but the orchestrator did not use it. It built UC-02 on an
  **in-memory store with the synthetic configuration registry**, so plans were lost on restart and the configuration
  came from code. After a restart, replanning was impossible.
- **Now:** UC-02 runs in Supabase mode inside the orchestrator.
  - Reads the UC-01 case and the client's approved configuration.
  - Writes tenant-scoped tasks, dependencies, one plan revision per plan version, audit entries and agent runs.
  - Returns a structured result that the orchestrator persists to `case_orchestration_state` under `plan_tasks` /
    `replan_tasks`.
  - Is rebuilt from the database after a restart.
  - Fails closed: configuration problems BLOCK with a reason, and an outage becomes RETRY_PENDING.

This was verified with tests and **live against the development database, with `database/` removed** (§9).

## 1. Files changed

| File | Change |
|---|---|
| `UC02_Onboarding_Task_Agent/src/onboarding_task_management/supabase_store.py` | Tenant scoping on every planning query, with cross-tenant edges and writes refused. Snapshot order by `snapshot_seq`. Commit-marker write order (the snapshot is written last). One `plan_revisions` row per plan version (projection plus the approved-change row). `CaseNotFoundError`; a case of another client raises `DataIsolationError`. Optional `started_at` / `error_summary` on runs. |
| `UC02_Onboarding_Task_Agent/src/onboarding_task_management/supabase_rest.py` | `SupabaseUnavailableError`, a subclass of `PersistenceError`, for network errors, 429 and 5xx. |
| `UC02_Onboarding_Task_Agent/docs/UC02_FINAL_ARCHITECTURE_AND_STATUS.md` | A pointer to the new documents. |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/adapters/uc02_tasks.py` | `uc02_sub_agent` builds UC-02 from `UC02_STORE_MODE` (the single path into UC-02's agent, rebuilt after a restart). Supabase case check, classified failures with blockers, execution trace, and an enriched `task_plan`. The date-change gate works after a restart (`_require_record`, `refresh_task_plan`). |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/adapters/uc02_replanning.py` | Rebuilt sub-agent, outage classified as retryable, execution trace, `task_plan` refreshed from UC-02's record. |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/workflow_models.py` | `StageOutcome.result_summary` (optional). |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/workflow.py` | `stage_result_summary()` for `plan_tasks` / `replan_tasks`. |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/orchestrator_agent.py` | Records `result_summary` in every `StageOutcome`. |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/dependency_blockers.py` | Catalogue entries for the UC02 blocker codes. |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/deps.py` | `uc02_rest_factory` (test injection point; None in production). |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/supabase_state_store.py` | Dedicated `ORCHESTRATOR_SUPABASE_*` settings, with the plain `SUPABASE_*` still accepted. |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/workers/config.py` | Workers never receive `ORCHESTRATOR_SUPABASE_SERVICE_ROLE_KEY`. |
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/api.py` | Explicit HTTP codes for state-store failures (409 / 503 instead of 500). Optional, validated `attributes` on the start request, so HR can state checklist attributes. |
| `HR-Onboarding-Common/scripts/serve_orchestrator.py` | Refuses a Supabase case-state store combined with a non-Supabase UC-02; `/healthz` reports `uc02_store`. |
| `HR-Onboarding-Common/tests/orchestration/conftest.py` | Pins the UC-02 store settings empty, so no test reaches a live database. |
| `HR-Onboarding-Common/.env.example` | Documents `UC02_*` and `ORCHESTRATOR_SUPABASE_*` (placeholders). |
| `.env.example` (repository root) | **The committed service-role key was replaced with a placeholder** (see §6). |
| `onboarding-ui-prototype/src/api/onboardingApi.ts`, `src/components/StageSidebar.tsx` | The UC02 panel shows planned and required tasks, dependencies, ready/waiting, configuration version, store and last update; the task table shows `blocked_by`. |

Local, git-ignored configuration (not part of the diff): five UC-02 entries and four `ORCHESTRATOR_*` entries were
appended to `HR-Onboarding-Common/.env`, using the same Supabase project as its existing entries (§6). The UC-02
entries are scoped (`UC02_WORKER_ENV__UC02_STORE_MODE`, `…__UC02_ENV`, `…__UC02_ALLOW_SYNTHETIC_CONFIG`,
`…__SUPABASE_URL`, `…__SUPABASE_SERVICE_ROLE_KEY`) because that file is also loaded by UC-02's own processes. A first
version used plain `UC02_STORE_MODE`, which made 9 of UC-02's own API tests switch to Supabase mode, and was corrected
(§9).

## 2. Files created

| File | Purpose |
|---|---|
| `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/adapters/uc02_runtime.py` | UC-02 environment (scoped credentials), store build, failure classification, `verify_case`, execution trace (`StageRun`), plan projection. |
| `UC02_Onboarding_Task_Agent/src/onboarding_task_management/sub_agent/orchestration_support.py` | UC-02's audit and agent-run records for orchestrated executions; plan projection; store mode. |
| `UC02_Onboarding_Task_Agent/tests/test_supabase_planning_integrity.py` | 19 tests: tenant isolation, revisions, snapshot order, commit marker, idempotency, outage, run trace. |
| `HR-Onboarding-Common/tests/orchestration/_fake_uc02_supabase.py` | An in-process PostgREST for UC-02's tables, sharing `onboarding_cases` with the orchestrator fake. |
| `HR-Onboarding-Common/tests/orchestration/test_uc02_supabase_orchestration.py` | 20 tests: scenarios A–G, outage, fail-closed configuration, credential and settings scoping, server guard, API attributes. |
| `HR-Onboarding-Common/scripts/verify_uc02_supabase_workflow.py` | Opt-in live check through the API (preflight / plan / resume / branches / negative / verify). |
| `UC02_Onboarding_Task_Agent/docs/UC02_SUPABASE_DATABASE_MAPPING.md`, `UC02_RUNTIME_DEPENDENCY_AUDIT.md`, `UC02_SUPABASE_ARCHITECTURE.md`, this report | Documentation. |

## 3. Supabase tables used

| Table | Purpose | Read / write | Owner |
|---|---|---|---|
| `onboarding_cases` | The authoritative case; tenant | R | UC-01 |
| `client_configs`, `client_config_versions`, `task_templates`, `client_milestone_templates`, `client_document_requirements`, `client_service_policies`, `client_holidays` | Approved configuration and calendar | R | Configuration owner |
| `onboarding_tasks` | Planned tasks (rows with the `uc02` marker) | R/W | UC-02 |
| `task_dependencies` | Dependency graph | R/W | UC-02 |
| `plan_revisions` | One row per plan version | R/W | UC-02 |
| `audit_events` | UC-02 audit, case-state snapshots, execution trace | R/append | Shared |
| `agent_runs` | Advisory and orchestrated runs | W | Shared |
| `approval_requests` | UC-02's own gates (date change) | R/W | Shared |
| `case_orchestration_state` | Workflow state; UC-02's result in `stage_outcomes` | R/W **by the orchestrator only** | Orchestrator |

Column-level detail is in `UC02_SUPABASE_DATABASE_MAPPING.md`.

## 4. Runtime dependencies removed

- **In-memory UC-02 with the synthetic registry** in the orchestrator's real runtime. It is replaced by the Supabase
  store and the approved Supabase configuration when UC-02's store mode is `supabase` (set in the local `.env` as
  `UC02_WORKER_ENV__UC02_STORE_MODE=supabase`), and the
  server enforces this when the orchestration state is in Supabase.
- **The process-memory sub-agent as the only route to replanning and date changes.** It is now rebuilt from the store.
- **CSV:** none existed. No code reads `database/data`, which was verified by search and by running everything with
  the folder removed. Full classification: `UC02_RUNTIME_DEPENDENCY_AUDIT.md`.

## 5. Orchestration changes

- **UC01 → UC02.**
  - `plan_tasks` requires intake to be COMPLETE and the role context present (unchanged).
  - In Supabase mode, `verify_case` requires the case to be this tenant's `onboarding_cases` row, with a matching
    employee number.
  - UC-02 plans and writes its rows. The orchestrator then persists `task_plan`, `onboarding_plan`,
    `stage_outcomes.plan_tasks.result_summary` and the stage status. The routing decision stays the orchestrator's.
- **UC02 → UC03.**
  - UC-03 reads the plan version and its required documents from the persisted state. It is verified that nothing
    after planning needs UC-02 in process memory (scenario A).
- **UC03 → UC02.**
  - Only planning-impacting feedback (`replanning_required`) routes to `replan_tasks`; a correction stays in UC-03
    (scenario E).
  - UC-02 creates the next plan version and a new `plan_revisions` row, leaving earlier rows untouched, and replays
    idempotently by feedback id.
  - The orchestrator stores the revised plan, and UC-03 revalidates against it (scenario D).

## 6. Security

- **Tenant isolation.**
  - `client_id` is on every planning read, update and delete.
  - Cross-tenant dependency edges, task rows and trace rows are refused.
  - The orchestrator answers another tenant's case like a missing one (404).
  - `verify_case` refuses a case of another tenant (FAILED `TENANT_CONTEXT_CONFLICT`).
  - All of this is tested, and checked live.
- **Service-role key.**
  - Backend only. UC-02 reads it from `UC02_WORKER_ENV__SUPABASE_SERVICE_ROLE_KEY`, which no worker process receives.
    The orchestrator reads it from `ORCHESTRATOR_SUPABASE_SERVICE_ROLE_KEY`, which workers also never receive.
  - Never logged, never put in an error (tested), never returned by the API, never sent to the browser.
- **Key exposure found.** The repository-root `.env.example` (tracked; introduced in commit `c6cf80e`) contained the
  real service-role key of the development project.
  - The value was replaced with a placeholder in the working tree.
  - **It is still in git history, so the key must be rotated in Supabase**, and every `.env` that holds it updated.
  - `onboarding-ui-prototype/.env` also holds it. Vite does not expose it (checked: not in `dist/`), but it does not
    belong there.
- **Authorization.** Unchanged: `task_planning_trigger_roles` is checked before UC-02 runs, and the API's roles and
  identity provider are as before.
- **Sensitive data.**
  - UC-02 reads only non-personal case columns.
  - Audit and trace entries carry ids, codes and counts only.
  - The new `attributes` request field accepts codes only.

## 7. Recovery

- **Durable state.** The orchestrator persists after every stage to `case_orchestration_state`, using optimistic
  concurrency: a conditional `PATCH` on `version`, atomic in Postgres, so no RPC is needed.
- **UC-02 rebuilt on restart.** After a restart, UC-02 is rebuilt and loads its plan from Supabase.
- **Commit marker.** UC-02 writes its case-state snapshot last. If it fails part-way, the previous version stays in
  force; a restart never sees a new version without its tasks.
- **Crash between commit and record.** If UC-02 commits and the orchestrator crashes before recording it, the resume
  recognises the plan (`created: false`) and creates nothing twice.
- **Evidence:** scenario B tests, and the live `plan` → `resume` → `branches` steps, each run as a separate process.

## 8. Idempotency

Duplicates are prevented as follows:

- **Plan:** UC-02 recognises the existing onboarding of the case.
- **Tasks:** upsert on `task_id = <case>#<template_key>`.
- **Dependencies:** upsert on the (`task_id`, `depends_on_task_id`) pair.
- **Revisions:** insert-if-missing by (`case_id`, `revision`).
- **Snapshots:** an unchanged record writes no snapshot.
- **Replans:** idempotent by `feedback_id`.
- **Run rows:** one per execution id.

**Proven:**

- Scenario C: three executions of `plan_tasks` for the same state, from two processes, produce identical row counts.
- The live replay in the `resume` step: 13 tasks, 12 dependencies, 1 revision and 3 snapshots, before and after.

Audit entries are recorded once per execution by design.

## 9. Tests and live verification

### Baseline (before any change)

- **UC-02:** 1562 passed, **3 failed**, 10 skipped.
  - Two were the snapshot "latest" tie on Windows' clock resolution, now fixed.
  - One is an in-memory audit-order test that depends on timing; it passed in the later runs.
- **HR-Onboarding-Common:** all passed (exit 0).

### After the change

Final runs, all executed (`-o addopts="" -p no:cacheprovider`). The UC-02 run and the first final Common run were
made with `database/` renamed to `database_backup/` (restored afterwards); the last Common run followed the
settings-scoping fix:

| Suite | Result |
|---|---|
| UC-02 (`UC02_Onboarding_Task_Agent`, `pytest tests`) | **1584 passed, 10 skipped, 0 failed** (2595 subtests). Includes the 19 new tests and the 2 previously failing snapshot tests. |
| HR-Onboarding-Common (`pytest tests`, real UC01/UC04/UC05 worker environments) | **803 passed, 1 skipped, 0 failed**. Includes the 20 new tests; the run with `database/` removed gave 802 passed, before the scoped-settings test was added. |
| UI (`tsc --noEmit`, `npm run test:unit`) | 0 type errors; 5 passed |

Two findings from the test runs:

- **Scoped settings.** A UC-02 run made right after the first local `.env` change had **9 failures** in
  `tests/testcase.py` (API and evaluation). Cause: plain `UC02_STORE_MODE=supabase` in the shared
  `HR-Onboarding-Common/.env`, which UC-02's advisor loads through `hr_onboarding_common.config.get_settings()`. Fixed
  by scoping UC-02's settings (`UC02_WORKER_ENV__*`); the next runs had 0 of those failures.
- **Flaky pre-existing test.**
  `test_uc02_mock_workflow_scenarios.py::WorkflowRecoveryTests::test_the_audit_trail_is_persisted_and_survives_a_restart`
  (in-memory store; audit ids generated in the same millisecond sort unpredictably) failed in one full run and passed
  5/5 alone and in the final run. It is pre-existing and unrelated to Supabase.


### Live verification

Development project, 2026-11-05/06; `database/` renamed for all of it.

- **preflight** (read-only): schema OK. Case `62e57066-…` (SYN-UAE-TEST, UC-01-written, approved configuration v1)
  had no UC-02 rows.
- **plan** (process 1, through the API, with the form-approval gate decided by an HR lead):
  - `plan_tasks` complete.
  - UC-02 wrote **13 tasks, 12 dependencies and plan revision 1**.
  - `case_orchestration_state` v10 holds `stage_outcomes.plan_tasks.result_summary` (`plan_revision` 1,
    `task_count` 13, `store` supabase).
- **resume** (process 2, a restart):
  - The case was loaded from Supabase, with no UC-02 in memory.
  - Replaying `plan_tasks` gave `created: false` with **no duplicates**.
  - A passport showing the holder needs a visa went to UC-03; UC-02 replanned to **revision 2** (row 1 unchanged);
    UC-03 collected and validated against v2.
  - HR review followed. The real UC-04 adapter blocked: SYN-UAE-TEST has no UC-04 configuration
    (`UC04_CONFIGURATION_MISSING`).
- **branches** (process 3, a restart): with stage stand-ins for UC-04 and UC-05, both branches completed from the
  stored state. Each received plan v2 and verified plan v2. Day-one readiness is awaiting sign-off
  (`case_orchestration_state` v50).
- **negative:**
  - As CLIENT_DUBAI: the case read returned **404**, and UC-02's cross-tenant read was refused.
  - A CLIENT_DUBAI case presented in a SYN-UAE-TEST workflow: **FAILED `TENANT_CONTEXT_CONFLICT`**, with 0 rows
    written.
  - test_enterprise_corp (no configuration version): **BLOCKED, `MISSING_DATA` / `UC02_CONFIG_MISSING`**, with
    0 tasks or revisions written.
- **verify:** read back revisions 1 and 2 (`initial_planning`, `document_validation_feedback`), the `orchestration.*`
  audit trail, the `agent_runs` rows, and both stage summaries.
- **Real backend** (`serve_orchestrator.py --dev-identity`, real UC01/UC04/UC05 workers):
  - Started; `/healthz` reported `state_store: SupabaseCaseStateStore` and `uc02_store: supabase`.
  - A CLIENT_DUBAI admin reading the SYN case got **404**.
  - A new CLIENT_DUBAI case reached intake through the real form-approval gate. The real UC-01 worker (in-memory)
    produced a case that is not in `onboarding_cases`, so the server answered **409 `CASE_NOT_IN_DATABASE`**, an
    explicit stop rather than a fabricated success.

Rows left in the development database by the live checks: UC-02's rows for case `62e57066-…` (13 tasks,
12 dependencies, 2 revisions, snapshots, trace, runs); its `case_orchestration_state` row; trace rows for the two
negative checks; and the orchestrator's form-approval request rows (the existing approval mirror). The real backend
check also raised one CLIENT_DUBAI form approval that stopped at intake. No UC-01 table was written.

## 10. Known limitations and decisions needed

1. **UC-01 does not write `onboarding_cases` in this environment.** It is blocked on decision B1, the shared
   encryption key. Until it does, a case started from the Control Center stops after intake with 409
   `CASE_NOT_IN_DATABASE` in the Supabase runtime. To develop UI-only flows locally, set
   `ORCHESTRATOR_STATE_STORE=sqlite` and `UC02_WORKER_ENV__UC02_STORE_MODE=memory` in `HR-Onboarding-Common/.env`; `task_plan.store`
   then says `memory`.
2. **Rotate the development service-role key** (§6).
3. **`UC02_ALLOW_SYNTHETIC_CONFIG=1` (scoped in the `.env`) is needed today** because every client configuration in the database is marked
   mock or synthetic. Production needs client configurations approved for real use, and must leave it unset.
4. **`StageOutcome.data_origin` is fixed to `"synthetic"`** by the existing shared contract, and now also labels real
   UC-02 outcomes. Changing it is a contract decision, so it was not changed.
5. **HR must state the checklist attributes** (for example `location_status:in_country`) that the client's templates
   depend on. Otherwise UC-02 holds the plan for review (`CHECKLIST_REVIEW_REQUIRED`). The API now accepts
   `attributes`. The Control Center's start form does not send them yet.
6. **`required_task_count` is derived from the client's milestones.** It is 0 for a client without milestone templates
   (SYN-UAE-TEST has none).
7. **`previous_schedule` / `new_schedule` in `plan_revisions` are not populated.**
8. **The UC-04 and UC-05 branches are not real here.** They used stand-ins in the live run, because SYN-UAE-TEST has
   no UC-04 configuration, and running the real UC-05 worker would write to UC-05's own tables. Their real
   integrations are unchanged.
9. **The orchestrator's venv holds a non-editable copy of UC-02.** After changing UC-02, run
   `uv pip install --python HR-Onboarding-Common/.venv/Scripts/python.exe --no-deps --reinstall UC02_Onboarding_Task_Agent`.
   This was done for this change.

## Acceptance checklist

| Criterion | Status |
|---|---|
| UC02 reads real data from Supabase | ✔ (live) |
| UC02 writes real planning data to Supabase | ✔ (live) |
| CSV files not required at runtime | ✔ (search, plus runs with `database/` removed) |
| Tenant isolated | ✔ (tests, live) |
| Uses real client configuration | ✔ (approved versions from Supabase; synthetic-marked clients only with the dev opt-in) |
| Creates/updates onboarding tasks; dependencies and revisions persisted | ✔ |
| UC02 output reaches `case_orchestration_state` | ✔ (`stage_outcomes.plan_tasks.result_summary`, `task_plan`) |
| UC03 receives UC02 output from persisted state | ✔ (scenario A, live `resume`) |
| UC03 can request replanning; a new authoritative revision | ✔ |
| A document correction does not replan | ✔ (scenario E) |
| Survives backend restart | ✔ (scenario B, live processes) |
| Duplicate UC02 execution is idempotent | ✔ |
| Audit events and agent runs persisted | ✔ |
| Missing configuration produces a controlled blocker | ✔ |
| Cross-tenant access rejected | ✔ |
| Mock mode isolated | ✔ (explicit mode, labelled store, server guard, production refusal) |
| `database/` can be removed; backend still starts | ✔ |
| End-to-end orchestration test passes | ✔ (scenarios A–G, live run). UC-01 case creation in Supabase is the external blocker (limitation 1). |
| Existing test suite remains passing | ✔ (§9; one pre-existing timing-dependent UC-02 test is intermittent) |
| Documentation and report | ✔ |

## Follow-up fix (2026-11-06): a form approval stranded at UC01

**Reported:** in the Control Center, approving an internal onboarding form answered "apr-… already decided
(approved)", and the case stayed at "UC01 Employee Record Creation — Awaiting Approval".

**Cause:**
1. With `ORCHESTRATOR_STATE_STORE=supabase`, the decision was recorded.
2. UC01, running in memory, then opened a case that has no `onboarding_cases` row.
3. Saving the workflow state was refused (foreign key, `CASE_NOT_IN_DATABASE`).
4. The case kept listing the decided approval, and every further click was refused.
5. The intake inputs had also been discarded before the case was stored, so a retry had nothing to retry with.

**Fix (`orchestrator_agent.py`):**
- Intake inputs are dropped only once the completed intake is stored.
- A decided approval leaves the pending list.
- A case the store refuses is recorded as intake **BLOCKED `CASE_NOT_IN_DATABASE`** on its provisional state, with a
  catalogue blocker naming UC01, and is resumable.
- The same decision repeated by an entitled approver re-runs the follow-up of an approval that was recorded but
  never applied (never a second or different decision; the requester is still refused).

**Tests:** `tests/orchestration/test_case_not_stored_recovery.py` (3). HR-Onboarding-Common: **806 passed,
1 skipped, 0 failed**. Checked through the real backend as well.

**Start form:** an optional "Checklist attributes" field sends the `attributes` the API accepts. CLIENT_DUBAI plans
without review once `employment_type:full_time, visa_route:in_country_transfer` are stated (checked against its live
configuration).

**Runtime:** the owner chose the full Supabase runtime. The UC01 worker settings were added to the local
`HR-Onboarding-Common/.env`, scoped to UC01's worker. The `SUPABASE_URL` and `UC01_FIELD_ENCRYPTION_KEY` lines stay
commented until the team's shared development key (UC01 decision B1) is pasted in. CLIENT-ALPHA has no
employee-number counter and no approved configuration in Supabase, so test the full flow with CLIENT_DUBAI.

**Visibility (same day):** `/healthz` now reports `uc01_store` (`supabase` | `memory`).
`serve_orchestrator.py` warns at start when the case state is in Supabase but UC01 runs in memory. The Control Center
shows the same warning as a banner, so this configuration is visible before a form is approved rather than at approval
time. A backend started before the fix keeps the old behaviour until it is restarted.
