# Orchestration workflow: execution, handoffs, state and recovery

How the central orchestrator (`hr_onboarding_common.orchestration.OnboardingOrchestratorAgent`, a LangGraph graph) runs UC01–UC05 as the code is today. Each statement points to the code it describes.

## 1. The actual graph (`orchestrator_agent.py: build_graph`)

```
intake (UC01) ─► intake_mismatch_proposal ─► plan_tasks (UC02, initial plan)
                                                  │
                                                  ▼
                       ┌──────────────► document_collection (UC03: collection for plan version N, one request)
                       │                          │
                       │                document_submission ── waits (WAITING_FOR_INPUT) until every
                       │                          │            required document of version N is stored
                       │                validate_documents (UC03)
                       │                          │
                       │                evaluate_document_result ──┬─ correction needed ─► await (WAITING_FOR_INPUT); no replan
                       │                          │                ├─ HR review needed  ─► await (AWAITING_APPROVAL)
                       │                          │                ├─ technical failure ─► await retry (RETRY_PENDING)
                       │                          │                └─ plan must change  ─► replan_tasks (UC02)
                       └──── apply_revised_plan ◄─────────────────────────────────────────┘ (version N+1)
                                                  │ every document of the current version verified
                                         record_verification (verified_context)
                                                  │
                                          dispatch_branches  ─── mandatory-document gate
                                  ┌───────────────┴────────────────┐
                        equipment_access (UC04)     orientation_scheduling ─► buddy_assignment ─► team_introduction (UC05)
                                  │                                          uc05_branch_end
                                  └───────────────┬────────────────┘
                                           merge_outcomes  (fan-in: waits for both)
                                                  │
                                       evaluate_overall_status ─► await_branches (a branch not complete)
                                                  │ both complete, results current for the joining date
                                          day_one_readiness ─► day_one_signoff_proposal ─► await signoff (HITL, HR)
                                                  │ approved
                                       joining_day_welcome (UC05) ─► waits for the joining-day event
                                                  │
                                               complete
```

Every `await_*` node ends the invocation. The case resumes on the next event: documents submitted, an approval decided, a retry, or the joining day.

## 2. Ordering rules and where they are enforced

| Rule | Enforcement | Test |
|---|---|---|
| UC01 completes before UC02 | `route_after_intake`; `dependencies.py` | `test_02_an_invalid_intake_never_reaches_uc02` |
| UC02's plan before UC03 | `route_after_plan_tasks`; `PREREQUISITE_NOT_MET` | `test_plan_tasks_blocked_prevents_document_collection_*` |
| Documents verified before any branch | `dispatch_branches` is reachable only from `record_verification` | `test_23_a_uc03_failure_starts_neither_branch`, `test_24_*` |
| Plan changes go back to UC02; UC03 revalidates | `evaluate_document_result` routes to `replan_tasks` and then `apply_revised_plan` | `test_13_to_17_*`, `test_C_*` |
| A plain correction does not replan | Correction routes to `await_*` | `test_19_a_plain_resubmission_needs_no_replanning`, `test_B_*` |
| UC04 and UC05 run in parallel | Two successors of `dispatch_branches`, run in one LangGraph step | `test_equipment_access_and_orientation_scheduling_run_at_the_same_time`, `test_D_both_branches_run_in_parallel_*` |
| Merge waits for both branches | `add_edge(["equipment_access", "uc05_branch_end"], "merge_outcomes")` | `test_D_merge_waits_for_both_branches` |
| A failed branch is never "complete" | `derive_branch_status` / `derive_workflow_status`: FAILED outranks every other status | `test_E_uc04_failure_*`, `test_E_uc05_scheduling_failure_*`, `test_real_agents_..._a_failed_branch_is_never_complete` |
| Signoff only when both branches are complete and current | `route_after_evaluation`; `joining_date.consistency` | `test_readiness_refuses_signoff_for_results_produced_for_another_date` |
| A joining-date change re-opens stale results | `_reconcile_joining_date` re-opens them and withdraws the old signoff | `test_G_*`, `test_a_change_after_both_branches_completed_*` |
| Welcome only after signoff and on the joining day | `joining_day_welcome` prerequisites; `PrematureLifecycleEventError` | `test_happy_path_*`, `test_A_*` |
| Resumable after interruption | Write-through after every stage; resume from the case store | `test_E_a_process_stopping_*`, `test_A_every_step_runs_in_a_new_process_*` |

## 3. UC handoff contracts (input built from persisted state, output to state)

| Stage | UC call | Input (from state) | Output (state fields the stage owns) |
|---|---|---|---|
| intake | UC01 worker `open_onboarding_case` | `intake_trigger`, plus the intake-only inputs (form, id number, email, placement), held in process memory and never in state | `case_id` (UC01's uuid), `employee_id`, `joining_date` |
| plan_tasks | UC02 `UC02SubAgent.run` in process, LLM = `ctx.llm` | `employee_id`, `joining_date`, `uc02_role_context` (role, department, manager, location, project, approval timestamps) | `task_plan`, `onboarding_plan` (versioned document requirements), `plan_history` |
| document_collection / submission / validate_documents | UC03 in process, LLM = `ctx.llm` | `onboarding_plan` version N, `submitted_documents` (references and metadata, never contents) | `document_collection`, `document_validation_result`, `validation_issues`, `correction_requirements`, `document_validation_feedback` |
| replan_tasks | UC02 `replan_documents` | `document_validation_feedback` (structured: feedback id, plan version, recommended task changes) | `onboarding_plan` version N+1, `revised_plan` |
| equipment_access | UC04 worker `evaluate_orchestrated` | `verified_context` and ids, `joining_date`, the tenant's `CaseConfig` (`uc04_client_config_ref`) | `equipment_access_result` |
| orientation_scheduling, buddy_assignment, team_introduction | UC05 worker | `verified_context`, `employee_ref` (canonical id), `joining_date` | `orientation_schedule_result`, `buddy_assignment_result`, `team_introduction_result` |
| joining_day_welcome | UC05 worker | Readiness signed off, plus the joining-day event | `welcome_message_result` |

Each stage execution is wrapped by `execution.invoke`, which records:
- the request, validated, and its dependencies, checked before the UC is called;
- a `StageOutcome` in `stage_outcomes[stage]`: operation, inputs passed, status, attempt, error, execution id, trace id and correlation id.

A UC returns a structured result. The orchestrator alone writes the case state; UC05 reads `case_orchestration_state` read-only.

## 4. Supabase `case_orchestration_state` as the authoritative state

Enable it with `ORCHESTRATOR_STATE_STORE=supabase` plus `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_SCHEMA`, or `build_deps(state_store="supabase")`. Implementation: `orchestration/supabase_state_store.py`. No schema change: it uses the table's existing columns.

### 4.1 Column mapping

| Column | State field |
|---|---|
| `case_id` (uuid, FK `onboarding_cases`), `client_id`, `correlation_id` | same names |
| `workflow_status`, `overall_status` | same names |
| `stage_statuses`, `branch_statuses`, `completed_stages`, `pending_actions`, `blockers` | same names |
| `stage_outcomes` (one structured outcome per stage) | same name |
| `retry_metadata`, `verified_context` | same names |
| `last_error_code`, `last_error_detail` | `error_code`, `error_detail` (detail truncated to 500 characters) |
| `version` | `state_version` (+1 per write) |
| `updated_at` | `updated_at` |
| `orchestrator_context` | `{"format": "hr_onboarding_common.orchestration/case_state@1", "origin_case_id", "state": {every other field: plan, documents, results, history…}}` |

A row in another format is refused, not guessed at. The two rows that existed before were written by another tool, and this store never modifies them.

### 4.2 State transitions per UC execution

The steps the task asked for, and where each is implemented:

1. **Read the current state:** `_load(case_id, client_id=...)` calls `store.load`, which runs `SELECT … WHERE case_id AND client_id` and records the version read.
2. **Validate `case_id` and `client_id` ownership:**
   - the tenant filter is in the query;
   - `_check_tenant` checks the actor;
   - before the first write of a case, its `onboarding_cases` row must exist in that tenant.
3. **Verify prerequisites:** `dependencies.py` and `execution.invoke`. An unmet prerequisite means the UC is not called (`PREREQUISITE_NOT_MET`).
4. **Load upstream outputs:** from the loaded state. These are the columns, plus `orchestrator_context.state` for the plan and the documents.
5. **Build the UC input:** the handoff contracts in §3.
6. **Mark running:** an `IN_PROGRESS` event is appended to `stage_history`.
7. **Execute the UC.**
8. **Validate the result:** `validate_workflow_state` (the Pydantic `WorkflowStateModel`) and `completed_stage_regressions` (a COMPLETE stage cannot silently regress).
9. **Persist the outcome:** `stage_outcomes[stage]`.
10. **Update statuses:** `stage_statuses`, `branch_statuses`, `completed_stages`, `blockers` and `pending_actions` are derived by `refresh_workflow_view`.
11. **Increment the version safely:** a conditional `PATCH … ?case_id=eq.X&client_id=eq.T&version=eq.<expected>`. It is one atomic `UPDATE … WHERE version = expected` in Postgres. If another writer has moved the row, the PATCH matches nothing and raises `StateVersionConflict`; it never overwrites.
12. **Next UC:**
    - Within one invocation, the next node receives the state that was just committed (the same object the PATCH wrote).
    - Across invocations and restarts, it is rebuilt only from the committed row. `test_A_every_step_runs_in_a_new_process_from_the_committed_row_only` runs every step in a new process to prove this.

### 4.3 Concurrency

- **Same process:** a per-case `RLock` serializes operations. The two parallel branches each write only their own delta, folded onto the freshly loaded state under a store lock (`_persist_branch`).
- **Another process writes during a run:**
  - The branch write is refused by the database, then reloaded, re-folded and written again (up to 3 attempts), so neither result is lost.
  - That run's later whole-state writes would overwrite the other writer's change, so they are refused with `StateVersionConflict` ("resume the case").
  - The resume starts from the committed row, which holds both the branch results and the other change. Test: `test_D_a_concurrent_writer_never_loses_a_branch_result_or_its_own_change`.
- **No RPC needed:** single-row compare-and-swap is atomic over PostgREST. The orchestrator never needs a multi-row transaction, so no RPC was added.

### 4.4 Provisional cases (explicit, not a fallback)

- **Before UC01 creates the case:** the case has only the orchestrator's provisional id, `CASE-<hash>`. It has no `onboarding_cases` row, so the foreign key makes a row in this table impossible.
- **Where those states live:** pre-intake states, and the alias from the provisional id to UC01's uuid, are kept in the configured provisional store. That is SQLite under the data directory, or memory without one.
- **After a restart:** a provisional id is still resolved through `orchestrator_context->>origin_case_id`.
- **No silent fallback:** a uuid case is never written anywhere but Supabase. If Supabase is down, the write fails and nothing is written elsewhere (`test_E_supabase_unavailable_*`).

## 5. Retry and recovery

| Situation | Behaviour |
|---|---|
| UC technical failure (worker down, timeout, UC03 store unreachable) | Stage `RETRY_PENDING` with a retry budget (`stage_max_attempts`); `retry_stage` by an authorized operator; never treated as an invalid document |
| Write with an unknown outcome | Stage `UNCERTAIN`; never retried automatically; a person reconciles it |
| Business block (missing data, approval needed) | `BLOCKED` / `AWAITING_APPROVAL`, with an owner and a next action in `blockers` and `pending_actions` |
| Process killed mid-run | At most the running stage is lost. UC calls carry stable idempotency keys (`idempotency_keys`), so a re-run is replayed by the UC, not duplicated |
| Duplicate start (same tenant and offer reference) | Same case id. After intake it resumes the existing case: UC01 is called once and the document request is sent once (`test_E_a_duplicate_start_*`) |
| Supabase unavailable | `StatePersistenceUnavailable` is raised to the caller and nothing is written elsewhere; a later submission or resume continues from the last committed version |
| LLM unavailable, invalid key, timeout, invalid output | The gateway retries transient errors, then raises `LLMError`. UC02 degrades to its rules engine (`advice_degraded`); UC03 falls back to HR review, the local result or the template; UC04 and UC05 keep their deterministic output. No stage fails because of a model |
