# UC-02 Supabase verification checklist

Status: **prepared only. Nothing has been written to Supabase.** Every query below is read-only.

## 1. Is the configured project development or production?

**Not confirmed, and I cannot confirm it from the code or the REST API.** Facts found:

* The project ref appears in no repository file, so nothing in the repo names this project.
* UC-02 docs treat Supabase as the *shared-dev* store and Azure Cosmos DB as production (`docs/UC02_ORCHESTRATOR_PERSISTENCE_AND_APPROVAL_FACTS.md`, decision table row 3).
* Existing data looks synthetic (`mock-*` config hashes, `test_enterprise_corp`, two sample cases).
* You earlier noted the Supabase `main` branch was identified as production. That conflicts with the above, so it must be settled by a person.

Confirm one of these, in writing, before any write:

- [ ] In the Supabase dashboard, open the project named by `NEXT_PUBLIC_SUPABASE_URL` and read its name, and its branch if branching is on (Project Settings > General; Branches). Record: project name, ref (last 4 characters only in the report), branch, environment label.
- [ ] The TL or project owner states "this project/branch is the approved development database for UC-02 testing."
- [ ] If it is production, supply a separate development project/branch URL and service-role key instead. UC-02 stays read-only against production.

## 2. Approved UC-01 test case: what must exist

UC-02 never creates `onboarding_cases`; UC-01 (or an approved seed by its owner) must. Mandatory columns, from the live schema:

| Column | Rule |
|---|---|
| `client_id` (text) | NOT NULL, no default. Must be a client UC-02 can plan for (see below). |
| `candidate_name`, `candidate_email` | NOT NULL, no default. Use obviously synthetic values (for example `Test Candidate`, `test.uc02@example.invalid`). |
| `hr_officer_id` | NOT NULL, no default. A test HR actor id. |
| `joining_date` | NOT NULL, no default. At least 10 working days in the future so due dates are valid. |
| `work_location_type` | NOT NULL, no default. Allowed values are unknown; copy the value an existing case uses. |
| `role_id`, `manager_id` | NOT NULL, no default. |
| `onboarding_attributes` | NOT NULL, no default (`text[]`; `{}` is fine). |
| `case_id` | Defaults to a uuid. UC-02 uses it as `onboarding_id`; record it. |
| `employee_no` | Nullable but **set it**: UC-02 uses it to refuse a duplicate onboarding for the same employee. |
| `location_name`, `project_name`, `grade`, `department`, `nationality_group` | Have defaults (`Headquarters`, `Internal Operations`, empty, `General`, empty). **Set explicit values**: they drive task selection. |
| other defaults | `status=INTAKE_PENDING`, `current_stage=intake`, variant flags false. Leave them. |

UC-02-specific requirements (without these the run cannot work):

* **Client config.** UC-02 currently plans from its in-process registry, whose test clients are `CLIENT-ALPHA` and `CLIENT-BETA`. The live clients (`CLIENT_DUBAI` and so on) have no templates and no approved version. Either (a) the test case's `client_id` is `CLIENT-ALPHA` and that value is permitted by any foreign key to `client_configs`, or (b) an approved config is loaded into the `client_*` tables, which needs its own approval. **Decision needed.**
* The case's `location_name`, `project_name`, `role_id` and `grade` must match templates in that config, or UC-02 correctly reports a configuration gap and plans nothing.
* The trigger needs an accepted offer and an approved form for that employee (UC-01 supplies these; the test harness can stand in).
* Mark the case clearly as a test: `employee_no` like `EMP-UC02-TEST-001`, candidate fields synthetic. Record the `case_id` so it can be found and removed by its owner afterwards. UC-02 will not delete it.

## 3. Read-only SQL to inspect constraints (run in the Supabase SQL editor)

CHECK constraints:

```sql
select conrelid::regclass as tbl, conname, pg_get_constraintdef(oid) as definition
from pg_constraint
where contype = 'c' and connamespace = 'public'::regnamespace
  and conrelid::regclass::text in ('onboarding_cases','onboarding_tasks','task_dependencies',
      'plan_revisions','approval_requests','audit_events','agent_runs',
      'onboarding_reminders','onboarding_status_updates')
order by 1, 2;
```

Foreign keys:

```sql
select conrelid::regclass as tbl, conname, pg_get_constraintdef(oid) as definition
from pg_constraint
where contype = 'f' and connamespace = 'public'::regnamespace
  and (conrelid::regclass::text in ('onboarding_cases','onboarding_tasks','task_dependencies',
        'plan_revisions','approval_requests','audit_events','agent_runs')
       or confrelid::regclass::text = 'onboarding_cases')
order by 1, 2;
```

Primary keys and unique constraints and indexes (idempotency targets for `on_conflict`):

```sql
select tablename, indexname, indexdef from pg_indexes
where schemaname = 'public'
  and tablename in ('onboarding_cases','onboarding_tasks','task_dependencies','plan_revisions',
                    'approval_requests','audit_events','agent_runs')
order by 1, 2;
```

RLS state and policies:

```sql
select c.relname, c.relrowsecurity as rls_enabled, c.relforcerowsecurity as rls_forced
from pg_class c where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'
  and c.relname in ('onboarding_cases','onboarding_tasks','task_dependencies','plan_revisions',
                    'approval_requests','audit_events','agent_runs');

select tablename, policyname, permissive, roles, cmd, qual, with_check
from pg_policies where schemaname = 'public'
  and tablename in ('onboarding_cases','onboarding_tasks','task_dependencies','plan_revisions',
                    'approval_requests','audit_events','agent_runs')
order by 1, 2;
```

Triggers (is `audit_events` append-only, any `updated_at` triggers):

```sql
select event_object_table, trigger_name, action_timing, event_manipulation, action_statement
from information_schema.triggers where trigger_schema = 'public'
  and event_object_table in ('onboarding_cases','onboarding_tasks','audit_events','approval_requests')
order by 1, 2;
```

Column types, nullability and defaults:

```sql
select table_name, column_name, data_type, is_nullable, column_default
from information_schema.columns where table_schema = 'public'
  and table_name in ('onboarding_cases','onboarding_tasks','task_dependencies','plan_revisions',
                     'approval_requests','audit_events','agent_runs')
order by table_name, ordinal_position;
```

What to look for in the results:

- [ ] `actor_type` CHECK: is `USER` allowed? (the adapter sends `SYSTEM` or `USER`)
- [ ] `approval_requests.gate` and `.outcome` CHECK: are `plan_approval`, `final_review` and `APPROVED`/`REJECTED`/`PENDING` allowed?
- [ ] `agent_runs.status` CHECK: are `acted`, `exception` and `proposed` allowed?
- [ ] `onboarding_tasks.status`, `category`, `priority` CHECKs against UC-02 values.
- [ ] Foreign keys: `onboarding_tasks.case_id` -> `onboarding_cases`, `client_id` -> `client_configs`?
- [ ] Unique constraints that match the `on_conflict` targets used: `onboarding_tasks(task_id)`, `task_dependencies(task_id, depends_on_task_id)`, `approval_requests(request_id)`, `agent_runs(run_id)`.
- [ ] `audit_events` UPDATE and DELETE blocked by trigger or policy.
- [ ] RLS: the service-role key bypasses it. Record which policies exist for other roles.

## 4. Run plan once an approved dev database and case are supplied

Pre-flight (read-only): the section 1 confirmation, `verify_schema` passes, the case row is readable, the registry client matches.

| # | Step | Expected | Result |
|---|---|---|---|
| 1 | Start in supabase mode with `UC02_ENV=production` | starts, names backend, no secrets printed | |
| 2 | Create onboarding from the test `case_id` | onboarding id equals the case id; tasks inserted | |
| 3 | Read back the case and tasks | identical to what was planned | |
| 4 | Dependencies stored | edges match the plan | |
| 5 | Submit and approve the plan | approval row (`workflow_module=uc02_...`), `plan_status=approved` | |
| 6 | Assign an owner, start and complete a task | task row updated, audit events appended | |
| 7 | Request and apply a joining-date change | plan revision row, tasks rescheduled, completed work kept | |
| 8 | Run the agent (Fake LLM) | `agent_runs` row with `advisor_mode` offline | |
| 9 | Duplicate create for the same employee | refused | |
| 10 | Replay a save | no duplicate rows, no new snapshot | |
| 11 | Wrong-client read | isolation error | |
| 12 | Stop and restart, hydrate | case, tasks, approvals, plan state, audit all recovered | |
| 13 | Try to rewrite an audit row | refused | |

Every step is reported as succeeded or failed with the failing request's status and message (no credentials). Cleanup is not performed by UC-02: it never deletes shared rows, so the test case and its UC-02 rows stay until their owner removes them.

Verification queries after the run (read-only; replace `:case`):

```sql
select task_id, status, owner_role, assigned_actor_id, due_date from onboarding_tasks where case_id = ':case' order by plan_order;
select task_id, depends_on_task_id from task_dependencies where task_id like ':case#%';
select request_id, gate, outcome, decided_by, workflow_module from approval_requests where case_id = ':case';
select revision, drivers, applied_by from plan_revisions where case_id = ':case' order by revision;
select run_id, status, advisor_mode, action_count from agent_runs where case_id = ':case';
select action, outcome, occurred_at from audit_events where case_id = ':case' order by occurred_at;
select occurred_at, after_state->>'plan_status' as plan_status from audit_events
  where case_id = ':case' and action = 'UC02_CASE_STATE' order by occurred_at desc limit 3;
```

## 5. What I need from you

1. The environment confirmation (section 1) and, if needed, the development URL and key.
2. The test `case_id` (or permission for its owner to create one per section 2).
3. The decision on the client config for the test (section 2, first bullet).
4. The SQL results from section 3, pasted back, so I can correct any value the adapter sends before the first write.
