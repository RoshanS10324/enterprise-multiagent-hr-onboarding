# End-to-end workflow through Supabase

How one stage's result reaches the next when the case state is in Supabase (`ORCHESTRATOR_STATE_STORE=supabase`).
There is no in-memory handoff between stages: every stage reads the case state the orchestrator loaded from
`case_orchestration_state`, and every stage's outcome is written back before the next stage runs.

## 0. Tables written at every stage

```
HR form approval      approval_requests (orchestrator gate), case_orchestration_state*
      |
UC01 intake           onboarding_cases, onboarding_forms, person_records, employee_number_counters, audit_events,
      |               domain_events_outbox (+ uc01_approvals for re-hire / transfer / duplicate)
      |               case_orchestration_state (from here on keyed by onboarding_cases.case_id)
UC02 plan_tasks       onboarding_tasks, task_dependencies, plan_revisions, audit_events, agent_runs, approval_requests
      |
UC03 collection       uc03_case_state ("collection:<case>")
UC03 submission       Storage (private bucket) + candidate_documents, uc03_case_state (revision)
UC03 validation       document_validations, candidate_documents.status, uc03_case_state, audit_events, approval_requests
      |   \
      |    \ planning-impacting change only:  UC02 replan_tasks -> onboarding_tasks, task_dependencies, plan_revisions
      |                                       -> UC03 collection moves to the new plan version
      | mandatory documents verified
      +-------------------------------+
UC04 equipment_access                 UC05 orientation / buddy / team introduction
audit_events (UC04_CASE_STATE +       audit_events (case link, ledger), orientation_sessions, session_participants,
audit), agent_runs                    buddy_assignments, approval_requests, agent_runs
      +---------------+---------------+
merge                 case_orchestration_state (branch_statuses)
day-one readiness     approval_requests (sign-off)
joining day           joining_day_jobs, communications (welcome), audit_events
complete              case_orchestration_state
```

\* Before UC01 has written `onboarding_cases`, the case state cannot be stored in `case_orchestration_state` (its
`case_id` is a foreign key to `onboarding_cases`): the orchestrator keeps the pre-intake case in its provisional store.
With UC01 blocked (no `UC01_FIELD_ENCRYPTION_KEY`), every new case stops here with `CASE_NOT_IN_DATABASE`.

## 1. The cycle of one stage

```
advance_case / resume / event
  └─ load case_orchestration_state (row for case_id, client_id checked; version = n)
       └─ stage node: the UC adapter reads the persisted state, calls the UC (in process or worker)
            └─ the UC writes ITS OWN tables (UC02 tasks, UC03 validations, UC04 snapshots, UC05 sessions, ...)
       └─ stage outcome: status, error, warnings, result_summary, typed handoff (UC0XOutput)
  └─ write case_orchestration_state with version n+1, only if the stored version is still n (compare-and-swap)
```

A concurrent writer that changed the case first makes the write fail (`StateVersionConflict`, HTTP 409); nothing is
overwritten and the caller re-reads. A Supabase outage is `StatePersistenceUnavailable` (HTTP 503); the stage is not
reported as done.

## 2. The typed handoff (`stage_outcomes.<stage>.handoff`)

Defined in `HR-Onboarding-Common/src/hr_onboarding_common/orchestration/handoff_contracts.py`. Same envelope for every
UC; `result` is built from an allow-list of keys per UC (ids, versions, counts, codes, statuses -- never a name, an
e-mail address, a document value or a message body).

| Field | Meaning |
|---|---|
| `contract` | `UC01Output` … `UC05Output` (orchestrator-internal stages have none) |
| `contract_version` | `1.0` |
| `case_id`, `client_id` | the case and its tenant |
| `stage`, `status` | the orchestrator stage and how this run ended |
| `result` | what the next stage relies on (below) |
| `warnings` | the UC's warning codes |
| `blockers` | code, reason, owner, next action, and `required_table` / `required_record` / `required_configuration` when known |
| `approval_requirements` | approvals the stage waits for: reference, gate, approver role, proposal type, record |
| `audit_reference` | the execution id (joins the UC's `audit_events` / `agent_runs` rows) |
| `timestamp` | when the stage run finished (UTC) |
| `version` | the stage run's attempt; a later run's handoff supersedes it |

| Contract | Stage(s) | `result` keys |
|---|---|---|
| UC01Output | intake | `employee_id`, `employee_ref`, `joining_date`, `joining_date_revision`, `case_timezone` |
| UC02Output | plan_tasks, replan_tasks | plan id and revision (`plan_version`, `previous_plan_version`), attributes, config version, task / dependency / ready / blocked counts, required documents, agent run id, store |
| UC03Output | document_collection | `plan_version`, `required_documents` |
| UC03Output | document_submission | per document: `revision`, `source` |
| UC03Output | validate_documents | feedback id, plan version validated, validation status, verified / missing / invalid / expired documents, pending HR reviews, `replanning_required` and reasons; per document: revision, classification, document status, compliance status, next action, **persistence status**, audit reference |
| UC04Output | equipment_access | overall status, readiness reason code, items, approval status, evaluation time, store version |
| UC05Output | orientation_scheduling, buddy_assignment, team_introduction, buddy_introduction, joining_day_welcome | session id, type, status, start / end (UTC), timezone, meeting link, location (None when UC05 holds none), calendar reference, approval id and status, buddy id and validity, welcome draft id and status, delivery and `delivery_status` (`SENT_SIMULATED` for a simulated provider) |

## 3. The edges of the workflow

| From → to | What is handed over | Where it is persisted |
|---|---|---|
| UC01 → UC02 | the case (uuid), employee number, joining date, tenant | `onboarding_cases` (UC01); `case_orchestration_state` (UC01Output) |
| UC02 → UC03 | the plan version and its required documents | `plan_revisions`, `onboarding_tasks` (UC02); `uc03_case_state` collection row (UC03) |
| UC03 → UC02 (replanning) | `DocumentValidationFeedback`: only a planning-impacting change (a new mandatory document, a document-derived attribute, an HR requirement change) sets `replanning_required`; a plain correction never does | `document_validations` (UC03); `case_orchestration_state` (UC03Output) |
| UC02 → UC03 (after replanning) | the new plan version; the collection moves to it (older version superseded) | `plan_revisions`; `uc03_case_state` |
| UC03 → UC04 ∥ UC05 | verified documents; the gate opens only when every mandatory document is verified | `case_orchestration_state` |
| UC04 ∥ UC05 → merge | each branch's outcome, independent (one branch blocked never fails the other) | `case_orchestration_state` (`branch_statuses`, UC04Output / UC05Output) |
| merge → day-one readiness → joining day | readiness sign-off (HR approval), then the joining-day job | `approval_requests`; `joining_day_jobs` |

The joining date is always read from the case (`onboarding_cases.joining_date`, as the case state holds it). A changed
date reopens the branches whose results depended on the old one (stale-branch reconciliation, `joining_date.py`).

## 4. Restart behaviour

| Process restarted | What survives | Proof |
|---|---|---|
| Orchestrator | the whole case state and every handoff (`case_orchestration_state`) | previous change: resume after restart without duplicates |
| UC02 (in process) | tasks, dependencies, revisions | previous change |
| UC03 (in process) | its case state, collection and revisions, validation results | live: reloaded in a new process (`verify_supabase_runtime.py uc03`) |
| UC04 worker | its state snapshots | live: read back by a new process (`verify_supabase_runtime.py uc04`) |
| UC05 worker | sessions, participants, approvals, operation ledger, case links | live rows read back; local: only checkpoints, job leases and the simulated calendar |
| Joining-day scheduler | its jobs | live: read back by a new store instance (`verify_supabase_runtime.py jobs`) |
