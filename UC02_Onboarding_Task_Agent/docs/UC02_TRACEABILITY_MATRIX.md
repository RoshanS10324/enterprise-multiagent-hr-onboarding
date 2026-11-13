# UC-02 requirement-to-code traceability matrix

Authoritative as of 2026-11-04, branch `logesh`. Every row was checked against the code named in it. Evidence is one of:

* **Unit/mocked** - automated tests over mocked Supabase transport (`FakePostgrest` enforcing the live schema) or in-memory seams.
* **Live (dev)** - observed against the Supabase *Development* project with the synthetic case
  `62e3b13e-4448-5959-9b2e-56908d33678b`, mock Outlook and mock downstream. Read-back was done with the read-only `verify` stage.

Status words: **DONE** (implemented, tested), **DONE-MOCK** (implemented, only a mock counterpart exists), **EXTERNAL** (needs another team or
configuration; UC-02's side is done), **NOT DONE** (a gap; none remain in UC-02's own scope, see the end).

## Sources of requirements (what is authoritative)

| Document | Role |
|---|---|
| `docs/UC02_BUSINESS_RULES.md` | **Business rules `UC02-BR-01..14`** and test-case map. The approved business specification itself is not in the repository; the rules here are the approved rules as transcribed by the team. Requirements below that are not in it come from the project owner's instructions and are labelled **(owner)**. |
| `docs/UC02_INTEGRATION_CONTRACTS.md` | Contracts with the orchestrator and other use cases (implemented side only). |
| `docs/UC02_FINAL_ARCHITECTURE_AND_STATUS.md` | Verified architecture, workflow sequence, persistence mapping, test results. |
| `docs/UC02_INTEGRATION_CHECKLIST.md` | Completed / external / awaiting items; email configuration list. |
| `docs/UC02_RUNTIME_AND_ACTIVATION.md`, `docs/UC02_OUTLOOK_GRAPH_SETUP.md` | Runtime, signing, environment, Microsoft Graph. |
| `docs/uc01-uc02-uc03-workflow.md`, `docs/COORDINATOR_ORCHESTRATION_INTEGRATION.md` | Cross-use-case flow and the coordinator hand-off. |
| `docs/pre-development-review.md`, `docs/UC02_PROGRESS_REPORT.md`, `docs/UC02_CONTEXT.md` | **Historical.** Superseded where they disagree with the files above. |

## Authoritative workflow (verified)

UC-01 employee event (signed, idempotent) -> read case/offer/form from Supabase -> approved client configuration (Supabase) -> dynamic checklist
-> Fake LLM review (synthetic, never approves) -> plan `submitted` -> HR review email (draft, mock Outlook) -> HR decision via signed callback
(`approved` / `rejected` / `changes_requested`) -> team assignments + document requests prepared -> authorised sends -> documents reported through the signed
channel (mandatory gate) -> UC-04 / UC-05 triggers -> task execution with dependency unblocking, due dates, overdue, sweep -> restart recovery.

## Matrix

| # | Requirement | Expected behaviour | Code | Supabase tables | Evidence | Status |
|---|---|---|---|---|---|---|
| 1 | Start only after offer accepted and form approved (BR-01) | Refuse to plan otherwise | `service.validate_preconditions`; `supabase_store` offer/form readers | read `offers`, `onboarding_forms`, `offer_forms` | `PreconditionTests`; mocked workflow | DONE |
| 2 | UC-01 trigger and case lookup | Signed `EmployeeCreatedEvent`; UC-02 reads employee/case from Supabase; never writes UC-01 rows | `flow_runtime.receive_employee_event`; `runtime.build_flow` employee source | read `onboarding_cases` (+offers/forms) | `test_supabase_only_workflow` (`uc01_tables_written_by_uc02 == 0`); **Live (dev):** `verify` reports 0 | DONE |
| 3 | Approved client configuration (owner) | Role/grade/nationality/location templates, document requirements, SLA, calendar, dependencies from Supabase; only approved (or opted-in synthetic) versions | `supabase_config.py` (`SupabaseConfigSource`, registry, calendars) | read `client_configs`, `client_config_versions`, `task_templates`, `client_document_requirements`, `client_service_policies`, `client_holidays`, `client_milestone_templates` | `test_supabase_config.py`; **Live (dev):** run with config_version `1`, `data_origin synthetic` | DONE for synthetic config. **EXTERNAL:** real approved client configuration (owners must approve; team-assignment convention is PROPOSED) |
| 4 | Dynamic checklist (BR-02) | Checklist differs by client, role, grade, nationality group, location; with reasons | `selection.py`, `checklist.py`, `TaskTemplate.dimension_verdicts` | via #3 | `test_dynamic_checklist.py` (68), `test_multi_client_matrix.py`, `test_synthetic_profiles_and_agent.py` (UAE/India profiles differ) | DONE |
| 5 | Fake LLM review (owner) | Generated from retrieved data; labelled synthetic; cannot approve | `agent/fake_llm.py`, `agent/analysis.py` (`FakeAnalyst`), `flow_runtime.generate_review` | `audit_events` snapshot `analyses` | `test_fake_llm.py`, `test_supabase_only_workflow` ("never approves"); **Live (dev):** `reviews_stored ["p1"]` | DONE-MOCK (no real model; none claimed) |
| 6 | HR review notification (owner) | Email draft for HR with the plan and review; mock Outlook by default | `flow.prepare_hr_email`, `flow_runtime`, `connectors/outlook/*` | snapshot `messages` | tests; **Live (dev):** message `hrmail` v1 stored | DONE-MOCK; real delivery **EXTERNAL** |
| 7 | HR approval: approved / rejected / changes_requested (owner, BR-11) | Signed callback; approver, role, timestamp, reason persisted; self-approval refused; rejection needs a reason; change request -> next version | `flow.ApprovalCallback`, `sign_callback`, `process_signed_callback`; `approvals.py` | `approval_requests`, `audit_events` | `test_supabase_only_workflow`, `test_flow_supabase.py`, `test_runtime_flow.py`; **Live (dev):** `plan_approval APPROVED` | DONE (portal that calls the callback is **EXTERNAL**) |
| 8 | Assignments to teams and owners (BR-03) | Team drafts, hold reasons, no sends before authorisation | `flow.prepare_team_assignments`, `drafts.py`, `recipients.py` | snapshot `assignments`; `onboarding_tasks.assigned_team` | `test_team_assignment_flow.py`; **Live (dev):** 9 assignments | DONE |
| 9 | Documents: pending/submitted/under_review/verified/rejected, mandatory gate, resubmission (owner) | Signed status channel; invalid transitions refused; gate blocks downstream | `flow.apply_document_update`, `document_gate`, `DocumentState` | snapshot `documents` | `test_flow_supabase.py`, `test_supabase_only_workflow`; **Live (dev):** 4 mandatory verified, 1 optional pending | DONE-MOCK (statuses come from a labelled synthetic source) |
| 10 | No real OCR / antivirus / identity verification claimed (owner) | No such claim in code or output | status is "reported by a document source"; labels in demo | - | grep of output labels; docs | DONE (not implemented, not claimed) |
| 11 | Dependencies: no dependent task starts early (BR-04) | Start refused until prerequisites complete | `planner.unmet_prerequisites`; `tracker.start_task` | `task_dependencies` | `test_planner_dependencies.py`; `test_task_lifecycle_supabase.py`; **Live (dev):** 12 edges stored; `medical_test`/`biometrics` stayed blocked | DONE |
| 12 | Parallel independent tasks (BR-05) | Independent tasks ready simultaneously | `planner.ready_tasks`, `parallel_batches` | `onboarding_tasks.status` | **Live (dev):** after wave 2, 7 tasks ready at once; DB read-back 4 complete / 7 ready / 2 blocked | DONE |
| 13 | Task states and completion (BR-12) | blocked / ready / in_progress / complete / cancelled stored; overdue **derived**; evidence required where configured; unblocking on completion | `contracts.TaskStatus`, `tracker.py`, `service.complete_task`, `status_updates.py` | `onboarding_tasks` | `test_task_lifecycle_reliability.py`, `test_task_lifecycle_supabase.py`; **Live (dev)** | DONE. **Design note:** `failed` and `overdue` are not stored states. `overdue` is derived from `due_date` and the clock (so it cannot go stale); a failed external outcome (`invalid` / `requires_review` / `suspected_fraud` / `delayed`) leaves the task open with `external_blocking_reason` and keeps dependants blocked (`status_updates.py`). |
| 14 | Due dates (BR-06) | From lead times, joining date, dependencies, calendar | `calendar_uae.py`, `planner`, `SupabaseCalendars` | `onboarding_tasks.due_date`; `client_holidays` | `test_calendar_uae.py`; **Live (dev):** 13 due dates | DONE |
| 15 | Overdue, reminders, delay tracking (BR-10) | Overdue/at-risk with owner and follow-up; reminder **drafts**; each escalation once | `tracker.outstanding_activities/escalations`, `reminders.py`, `service.monitoring_sweep` | `audit_events` | `test_reminders.py`; **Live (dev):** as-of 2026-11-20 -> 6 overdue, 6 escalations, 5 reminder drafts; second pass recorded 0 new | DONE. Reminders are drafts; sending is **EXTERNAL** (UC-06 / connector). There is **no scheduler**: a sweep runs when called (documented). |
| 16 | Retries (owner) | Bounded retries for sends and triggers; write-ahead intents; idempotent | `flow.MAX_MESSAGE_SEND_ATTEMPTS=3`, `MAX_TRIGGER_ATTEMPTS=3`; Graph retry/backoff | snapshot `downstream.attempts`, audit | `test_flow_supabase.py`, `test_outlook_graph.py` | DONE |
| 17 | Re-plan after approved change (BR-09) | Change gate then new version; stale approvals refused | `planner.replan`, `CHANGE_GATES` | `plan_revisions`, `approval_requests` | `ApprovalControlledReplanTests`, `test_replanning.py` | DONE |
| 18 | Mock access provisioning (owner) | Access tasks (`laptop_request`, `system_access`) completed through a labelled MOCK UC-04 status update; no provisioning logic in UC-02 | `mock_handoff.uc04_equipment_result`, `supabase_demo.tasks` | `onboarding_tasks`, `audit_events` | `test_task_lifecycle_supabase.py`; **Live (dev):** `laptop_request` completed via "MOCK uc04_equipment_access status update", `system_access` unblocked | DONE-MOCK; real provisioning is UC-04's, **EXTERNAL** |
| 19 | UC-04 / UC-05 triggers (owner) | Signed POST to the team's https URL after the document gate; mock adapter labelled; `NOT_CONFIGURED` otherwise | `downstream.HttpTriggerAdapter`, `flow.MockTrigger`, `flow` trigger logic | snapshot `downstream` | tests; **Live (dev):** both `triggered` (mock) | DONE-MOCK; real endpoints **EXTERNAL** |
| 20 | Persistence in Supabase only (owner) | No silent memory/SQLite; production rejects them | `runtime.require_supabase_runtime`, `supabase_store.py`, `envfile.py` | all above | `test_supabase_only_workflow` (InMemory constructor patched to fail), `test_envfile.py`; **Live (dev):** preflight/verify | DONE |
| 21 | Restart recovery and duplicate-event idempotency (owner) | New runtime reloads; replay creates nothing | `service.hydrate`, ledger `ev:<id>`, deterministic ids | snapshot ledger | `RecoveryAndIdempotencyTests`, `test_restart_recovery.py`; **Live (dev):** `recover` -> replay `duplicate`, tasks 13/13, approvals 1/1, assignments 9/9; task progress survived | DONE |
| 22 | Isolation, authorisation, audit (BR-13/14, CR-*) | Client scoping, roles, append-only audit, no PII | `contracts.Actor`, `audit.py`, `privacy.py` | `audit_events` | `DataIsolationTests`, `test_authorization.py`, `test_security_regressions.py` | DONE |
| 23 | Authenticated inbound channels | HMAC signatures on events, documents, UC-04/05 status, decisions | `flow_runtime.sign_model`, `runtime_principals` | - | tests; **Live (dev):** event/decision/document updates accepted | DONE |
| 24 | Environment and secrets (owner) | One loader, local dev secrets generated only on request, stage validation | `envfile.py`, `scripts/*.ps1`, `scripts/uc02_env.py` | - | `test_envfile.py` (31) | DONE |
| 25 | Real email delivery | Graph connector with approval gate, idempotency, retry | `connectors/outlook/graph.py`, `factory.py` | snapshot `messages` | `test_outlook_graph.py` (mocked Graph) | IMPLEMENTED, **not live-verified; EXTERNAL configuration required** (`docs/UC02_INTEGRATION_CHECKLIST.md` section 3) |
| 27 | Isolated local mock demonstration (owner) | One command runs the whole workflow with nothing real; fails loudly on any broken check | `demo/uc02_local_mock_demo.py` over the `supabase_demo` stages and the schema-enforcing mock transport | none (mock) | `tests/test_local_mock_demo.py` (3); 22 in-demo checks | DONE-MOCK |
| 26 | Hand-off to UC-06 (BR) | `joining_handoff`; UC-02 never closes the case | `service.joining_handoff` | - | `ResponsibilityBoundaryTests` | DONE |

## Not done / not UC-02's to do

* Real approved client configuration, real Microsoft Graph delivery, real UC-04/UC-05 endpoints, the HR approval portal, real UC-03 document
  verification: external (see the integration checklist).
* No scheduler runs the monitoring sweep or sends reminders (a deliberate boundary: UC-06 owns delivery).
* Task-delay/failed-report columns (`failed_report_count`, `delay_*`) have no live column; they are carried in the `UC02_CASE_STATE` snapshot. A migration
  was proposed in `UC02_SUPABASE_MAPPING_AND_PROPOSED_MIGRATION.md` and **not applied** (not authorised).
