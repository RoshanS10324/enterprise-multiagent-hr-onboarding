# Ten synthetic employees through UC-01 → UC-02 on an isolated test database

Status: **prepared, not executed.** The dataset and runner are ready and pass every offline check. Nothing has been
written to any database. The run waits for an isolated test database.

- **Dataset:** `data/mock/isolated_e2e/employees_10.json`
- **Runner:** `scripts/isolated_e2e_10_employees.py`
- **Tests:** `tests/test_isolated_e2e_runner.py`

The runner reuses the existing flow and does not re-implement it:
- `uc01_document_live_run.run`: UC-01 intake, persistence and independent read-back.
- `uc02_handoff`: the shared orchestrator adapter, then UC-02 with its own Supabase persistence.
- `e2e_uc01_to_uc02.plan_in_database`.

UC-02 and the shared orchestrator are not modified.

## 1. The dataset

The client is **SYN-CLIENT-A**, UC-02's synthetic profile `client_configuration/profiles/client_a_config.json`. It
has two roles, grades G3–G7, the nationality group `NG-SOUTH-ASIA-SYN` (IN/PK/BD) and the location Dubai. Its
15 task templates filter by role, by grade (accommodation needs G4+) and by attributes (overseas, travel,
accommodation). **No template or document requirement filters by project**, so the configuration accepts any
project. `preflight` re-checks this against the configuration actually stored in the test database and blocks
the run if project filters exist and an employee's project is not among them.

| Test id | Role | Grade | Nationality | Scenario | Family | Project | Joining | Budget (AED) | Expected tasks/deps |
|---|---|---|---|---|---|---|---|---|---|
| EMP-TEST-001 | software_engineer | G4 | IN | overseas | yes | SYN-PROJ-ATLAS | 2027-01-11 | 20,500 | 15/14 |
| EMP-TEST-002 | qa_engineer | G3 | PK | in country | no | SYN-PROJ-ORION | 2027-01-18 | 8,150 | 10/9 |
| EMP-TEST-003 | software_engineer | G6 | BD | overseas | yes | SYN-PROJ-ATLAS | 2027-01-25 | 28,100 | 15/14 |
| EMP-TEST-004 | qa_engineer | G5 | IN | overseas | no | SYN-PROJ-NOVA | 2027-02-01 | 14,200 | 15/14 |
| EMP-TEST-005 | software_engineer | G4 | PK | in country | yes | SYN-PROJ-ORION | 2027-02-08 | 14,850 | 10/9 |
| EMP-TEST-006 | qa_engineer | G3 | BD | overseas | no | SYN-PROJ-NOVA | 2027-02-15 | 12,350 | 14/13 |
| EMP-TEST-007 | software_engineer | G7 | IN | overseas | yes | SYN-PROJ-ATLAS | 2027-02-22 | 36,000 | 15/14 |
| EMP-TEST-008 | software_engineer | G5 | PK | in country | no | SYN-PROJ-ORION | 2027-03-01 | 8,700 | 10/9 |
| EMP-TEST-009 | qa_engineer | G4 | BD | overseas | yes | SYN-PROJ-NOVA | 2027-03-08 | 20,650 | 15/14 |
| EMP-TEST-010 | software_engineer | G6 | IN | in country | no | SYN-PROJ-ATLAS | 2027-03-15 | 9,950 | 10/9 |

Each employee also has:
- a name, a `.invalid` email, a department, a manager id, a reporting line and a fictional salary line (an input,
  never calculated);
- family details as `SYNTHETIC-*` placeholders.

How the dataset is used:
- **Form:** the runner fills a copy of the blank 2026 template for each employee with
  `synthetic_onboarding_form.populate_docx`.
- **Project:** it is stored on the case by UC-01 (`placement.project_name`) and also given as `uc02.project`. The
  handoff sees two sources that agree.
- **Employee numbers:** they are not in the dataset. UC-01 allocates them from the test database's
  `employee_number_counters`, and the ledger maps each test id to its number.
- **Expected task counts:** a hand-derived test oracle. UC-02 decides the plan; a mismatch is reported, never
  "corrected".

## 2. Database configuration validation (what the runner enforces)

| Stage | Network | Checks |
|---|---|---|
| `check-config` | none | Required names present.<br>`ISOLATED_TEST_DATABASE=1`.<br>`https://`.<br>Host, project ref and service key differ from the shared project's. The shared values are read from `UC01/.env`, the repo-root `.env`, `UC02/.env` and `.env.local`, and compared by host/ref and SHA-256 only.<br>The encryption key is not shared, and its id starts with `dev-`.<br>No other URL in the file points at the shared ref. |
| isolation (all DB modes) | test host only | Pins every database and credential name before either module is imported; both modules' `.env` loaders never overwrite a set name.<br>Then scans the whole process environment for any shared host, ref or key and refuses if it finds one.<br>Refuses any DNS lookup other than the test host.<br>Confirms that **UC-01's `Settings` and UC-02's `SupabaseRestClient` both resolve to that host**.<br>`--confirm-host` must re-type it. |
| `preflight` | test host, read-only | Every UC-01 table and every column UC-02 needs (`supabase_schema.REQUIRED`).<br>The case-status CHECK, via the zero-write probe (an insert its FK always rejects).<br>The details/budget tables, the encryption round trip, the bucket, and the employee-number counter for SYN-CLIENT-A.<br>UC-02's registry resolves an **approved** SYN-CLIENT-A configuration for every joining date.<br>Project filters.<br>Dataset employees already present (these are reused, not duplicated). |

`run` and `retry-handoff` refuse unless `preflight` is fully PASS.

## 3. What the test database needs first (DBA / environment owner)

1. **Database:** a separate Supabase project (https), not `Onboarding_Agent`. A local `supabase start` stack serves
   plain http, which UC-01's `Settings` rejects until the documented localhost exemption exists
   (`docs/local-test-env.md`, "Known gaps").
2. **Schema:** a **schema-only** copy of the shared project's public tables, because UC-02's Supabase store writes
   the shared table names (`onboarding_tasks`, `task_dependencies`, `plan_revisions`, …) and the repository has no
   DDL for them. Then apply, in order:
   - `docs/schema/proposed/b5_allow_uc01_case_statuses.sql`
   - `docs/schema/proposed/20261105120000_uc01_form_details_and_budget.sql`
3. **UC-02 configuration:** seed SYN-CLIENT-A with **UC-02's own** CLI, pointed at the test project:
   `python scripts/uc02_client_config.py dry-run --set synthetic`, then `seed --confirm-write <test host>`. This
   needs `UC02_ENV=development` and `UC02_ALLOW_CONFIG_SEEDING=1`.
4. **Employee-number counter:** a counter for SYN-CLIENT-A, via
   `python scripts/seed_employee_number_counters.py --apply` against the test project.
5. **Storage:** a bucket for the generated forms (for example `uc01-isolated-test`).
6. **Original form:** a local copy of the blank `Onboarding form - 2026 (1).docx`. Its SHA-256 must start with
   `99939e02`, and the runner checks it.

Test env file. Keep it at `UC01_Employee_Record_Creation/.env.isolated-test`; it is gitignored by `.env.*`. Never
use the shared `.env`, which the runner refuses.

```
ISOLATED_TEST_DATABASE=1
SUPABASE_URL=https://<test-project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<test project service-role key>
UC01_FIELD_ENCRYPTION_KEY=<new dev key: python -c "from sub_agent.crypto import generate_key_b64; print(generate_key_b64())">
UC01_FIELD_ENCRYPTION_KEY_ID=dev-isolated-e2e-1
UC01_DOCUMENT_BUCKET=uc01-isolated-test
```

## 4. Expected tables and relationships

```
client_configs (SYN-CLIENT-A) ─┬─ employee_number_counters
                               ├─ client_config_versions ── task_templates      (UC-02 seed)
                               └─ onboarding_cases (case_id) ─┬─ onboarding_forms (form_id, version 1, salary encrypted)
                                                              ├─ person_records (case_id)
                                                              ├─ audit_events (case_id; UC-01 hash chain + UC-02)
                                                              ├─ uc01_onboarding_form_details (form_id → onboarding_forms)
                                                              │     ├─ uc01_onboarding_form_field_status (form_id)
                                                              │     ├─ uc01_onboarding_document_checklist (form_id)
                                                              │     ├─ uc01_onboarding_budget_lines (form_id)
                                                              │     └─ uc01_onboarding_budget (form_id; written last = complete)
                                                              ├─ onboarding_tasks (case_id, client_id, employee_id) ── task_dependencies (task_id → depends_on_task_id)
                                                              └─ plan_revisions / approval_requests / agent_runs (as UC-02 writes them)
```

Expected rows for the 10 employees. The UC-01 detail counts come from UC-01's own `build_form_details` in
`validate`; the task counts come from the oracle.

| Table | Expected |
|---|---|
| onboarding_cases | 10 |
| onboarding_forms | 10 |
| person_records | 10 (one per employee email, checked by the read-back) |
| uc01_onboarding_form_details | 10 |
| uc01_onboarding_form_field_status | 380 (38 each) |
| uc01_onboarding_document_checklist | 80 (8 each) |
| uc01_onboarding_budget_lines | 90 (9 each) |
| uc01_onboarding_budget | 10 |
| onboarding_tasks | 129 |
| task_dependencies | 119 |
| plan_revisions, approval_requests, agent_runs, audit_events | reported as found (UC-02 decides) |

The runner reads every table back through a **fresh** container, counted only for this run's case and form ids.

The per-employee read-back has 30+ checks:
- case, form, person and details exist and are linked;
- extraction and budget match exactly;
- the decrypted salary equals the form's value, and no plaintext secret appears in any row;
- there is exactly one case per email and one details row per document;
- the source document's SHA-256 is in Storage;
- the audit actions are present and the audit chain is intact.

UC-02 checks:
- same case, client and employee;
- tasks come from the approved templates;
- every task has an owner, status and due date;
- dependencies stay inside the plan;
- tasks and dependencies are persisted, and the counts match the oracle.

## 5. Execution instructions

All commands run from `UC01_Employee_Record_Creation`, with `PYTHONPATH=src`:

```
python scripts/isolated_e2e_10_employees.py validate                                  # offline rehearsal (done)
python scripts/isolated_e2e_10_employees.py check-config --env-file .env.isolated-test # offline
python scripts/isolated_e2e_10_employees.py preflight --env-file .env.isolated-test --confirm-host <ref>.supabase.co
python scripts/isolated_e2e_10_employees.py run --env-file .env.isolated-test --confirm-host <ref>.supabase.co \
       --original "<path>/Onboarding form - 2026 (1).docx" --i-understand-this-writes --out output/isolated-e2e
python scripts/isolated_e2e_10_employees.py retry-handoff --env-file .env.isolated-test --confirm-host <ref>.supabase.co \
       [--test-id EMP-TEST-004] --out output/isolated-e2e
```

Outputs:
- **`output/isolated-e2e/ledger.json`:** the per-employee results. It is written after every employee and holds
  the steps, the read-back checks, the plan counts, the expectation and the final labels:
  - `ISOLATED_DB_PERSISTENCE_VERIFIED`
  - `ISOLATED_DB_READBACK_VERIFIED`
  - `ISOLATED_DB_UC02_HANDOFF_VERIFIED`
  - otherwise `BLOCKED`, `FAIL` or `NOT TRIGGERED`
- **Database summary:** per-table counts.
- **Console:** the employee table.

Re-running `run` is safe. Each document is content-addressed, so an existing employee is reused (LINKED_EXISTING)
and an interrupted save is resumed; no second case is created. `retry-handoff` only re-runs UC-02 for employees
whose UC-01 record is verified.

## 6. Pre-execution safety checklist

- [ ] The test project is a **different** Supabase project from `Onboarding_Agent`; `check-config` is all true.
- [ ] `.env.isolated-test` holds only test credentials, is not committed, and is not one of the shared `.env`
      files.
- [ ] The UC-01 encryption key is **new**, dev-only (`dev-` id), and used nowhere else.
- [ ] The schema copy is applied, plus the B5 and details/budget migrations, on the **test** project only.
- [ ] SYN-CLIENT-A is seeded by UC-02's own CLI on the test project, and the counter and bucket exist.
- [ ] `preflight` shows every line PASS, and both modules resolve to the test host.
- [ ] The blank original is a local file with SHA-256 `99939e02…`.
- [ ] Nobody else is writing to the test project during the run.
- [ ] After the run, `ledger.json` is reviewed before any result is reported. Only `ISOLATED_DB_*` labels count;
      MOCK/LIVE labels never come from this runner.
