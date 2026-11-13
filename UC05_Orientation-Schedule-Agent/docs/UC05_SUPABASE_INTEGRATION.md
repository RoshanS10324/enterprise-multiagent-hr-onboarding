# UC05 and the shared Supabase database

UC05 (the orientation scheduling agent, `employee-onboarding`) can keep its business records in the team's shared Supabase database. This document explains how that works, how to configure and run it, and what is still local.

The table-by-table mapping is in [UC05_DATABASE_MAPPING.md](UC05_DATABASE_MAPPING.md). What was built, how it was tested and what is blocked is in [UC05_SUPABASE_INTEGRATION_REPORT.md](UC05_SUPABASE_INTEGRATION_REPORT.md).

## 1. Architecture

UC05's business logic was already separated from storage. Everything it reads or writes goes through four interfaces:

| Interface | Used for | Mock mode (default) | Supabase mode |
|---|---|---|---|
| `OnboardingPort` ([repository.py](../src/sub_agent/repository.py)) | cases, buddies, sessions, participants, communications, client configuration, tasks, the idempotency ledger | `MockOnboarding` (in memory) or `SqliteOnboardingRepository` | `SupabaseOnboardingRepository` ([db/repository.py](../src/sub_agent/db/repository.py)) |
| `EmployeeDirectoryPort`, `EmailPort` (connectors) | who the new hire is; the "offer accepted" trigger | synthetic directory and mailbox | `SupabaseEmployeeDirectory`, `SupabaseCaseEventSource` ([db/directory.py](../src/sub_agent/db/directory.py)), both reading `onboarding_cases` |
| `HITLInbox` (Common) | human approvals | SQLite (`hitl.db`) or in memory | `SupabaseHITLInbox` ([db/approvals.py](../src/sub_agent/db/approvals.py)), stored in `approval_requests` |
| `AuditLog` (Common) | audit trail | JSONL or in memory | `SupabaseAuditLog` ([db/audit.py](../src/sub_agent/db/audit.py)), which also writes `audit_events`; plus `AgentRunRecorder` for `agent_runs` |

The layers:

1. **Configuration.** [db/config.py](../src/sub_agent/db/config.py) reads and validates the environment.
2. **Connection.** [db/postgrest.py](../src/sub_agent/db/postgrest.py) holds one HTTP client per process.
3. **Schema mapping.** [db/mapping.py](../src/sub_agent/db/mapping.py) lists the required columns, the status values UC05 writes, the rules for database ids, and what may not go into an audit row.
4. **Repository implementations.** The `db/` modules above.
5. **Services and business rules.** `services/`, unchanged except for two optional buddy-proposal calls.
6. **API and orchestration.** `api.py`, the worker run by the central orchestrator, and the scheduler. All three get their stores from `runtime.build_context()`.

The services do not import the Supabase code. Unit tests keep injecting the in-memory fakes, and the fake LLM is separate from the database fakes.

## 2. How UC05 connects

UC05 talks to Supabase's REST API (PostgREST) over `httpx`, using `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Only one client is created per process.

This follows the rest of the repository: UC01 (`adapters/supabase/client.py`) and UC02 (`supabase_rest.py`) reach the same project the same way. `httpx` is already a dependency of HR-Onboarding-Common, so nothing new is installed.

Two other routes were considered and not used:
- **`DATABASE_URL` / psycopg.** The root `.env` `DATABASE_URL` was found to point at another project, with a placeholder password (UC01 `docs/schema/live-schema-comparison.md` §4).
- **The `supabase-py` SDK.** UC01 recorded why it isn't used: its package name collides with the repository's `supabase/` folder.

Both clients would also mean a second database client.

**What PostgREST cannot do.** Each HTTP call is one statement, and several calls are not one transaction. UC05 compensates in three ways:

- **Deterministic primary keys.** A retried insert hits the same key and fails as a duplicate instead of creating a second row. Examples: the case link, sessions, participants, communications, and versioned state rows.
- **Conditional writes (compare-and-set).** A PATCH only applies if the row still holds the value UC05 read. If it matches nothing, someone else wrote first, so UC05 reads again and retries, up to 5 times. It never overwrites blindly. This covers `updated_at` on sessions, `responded_at` on RSVPs, `outcome = PENDING` on approval decisions, and `status` on buddy assignments.
- **Insert, then verify, for buddy capacity.** UC05 writes the assignment and then counts the buddy's active assignments. If the count is over the limit, it rolls back and refuses. Two racing assignments can both be refused, but both are never kept. A database function would make this one atomic step; see the report.

Retries: only reads (GET) are retried, at most twice, on a network error, a timeout, HTTP 429 or a 5xx response. Writes are never retried automatically. The 10-second request timeout can be changed with `UC05_SUPABASE_TIMEOUT_S`.

## 3. Environment

| Variable | Mode | Meaning |
|---|---|---|
| `UC05_DATABASE_MODE` | all | `mock` (default) or `supabase` |
| `SUPABASE_URL` | supabase | `https://<project-ref>.supabase.co`, the project URL with no path |
| `SUPABASE_SERVICE_ROLE_KEY` | supabase | the backend-only secret |
| `SUPABASE_SCHEMA` | supabase | default `public` |
| `UC05_SUPABASE_TIMEOUT_S`, `UC05_SUPABASE_READ_RETRIES` | supabase | defaults 10 and 2 |
| `UC05_SUPABASE_VERIFY_SCHEMA` | supabase | `1` (default) checks every table and column UC05 uses at startup |

`SUPABASE_ANON_KEY` is not used. It is a browser credential governed by Row Level Security (RLS), not a substitute for the service-role key, and startup refuses a configuration where the two keys are the same.

Real values go in a git-ignored `.env`: UC05's own, or `HR-Onboarding-Common/.env` when `HR_ONBOARDING_ENV_FILE` points to it. Both `.gitignore` files exclude `.env`, and the `.env.example` files hold placeholders only.

At startup in Supabase mode, `runtime.build_context()`:
1. Validates the variables. Error messages name the variables involved, never their values.
2. Makes one round trip to Supabase. A refused key raises `PersistenceAuthError`; an unreachable server raises `PersistenceUnavailableError`.
3. Checks that every table and column UC05 uses exists, reading no rows. A gap raises `SchemaMismatchError` naming the tables.

Any of these failures becomes a `RuntimeConfigurationError`, and the service does not start.

## 4. The UC05 flow and where each step is stored

| Step | Stored in |
|---|---|
| Orchestrator sends the offer event; in Supabase mode its id is the case's uuid (`event_id = case_id`), under the actor's verified tenant | read: `onboarding_cases` |
| Load the case: identity, `joining_date`, manager, department | read: `onboarding_cases`, always fresh, never cached |
| Link the case to UC05; UC05 never creates a case | `audit_events` (`UC05_CASE_LINKED`, deterministic id) |
| Prepare the checklist | `audit_events` (`UC05_TASK_STATE`, versioned) |
| Load the client calendar: working days, timezone, holidays, and the approved configuration version in effect | read: `client_configs`, `client_holidays`, `client_config_versions` |
| Find a slot and check for conflicts with the employee's live sessions | read: `orientation_sessions` (the calendar check itself is still the simulated calendar) |
| Book the calendar hold through the idempotency ledger | `audit_events` (`UC05_OPERATION_STATE`) |
| Create the session | `orientation_sessions` (UTC, `session_metadata.owner = 'uc05'`) |
| Add participants and invitations | `session_participants` and `orientation_sessions.session_metadata.invitations` |
| Request human approval | `approval_requests` (`workflow_module = 'uc05_orientation_scheduling'`) |
| Apply the approval: confirm or cancel the session | `approval_requests` (decided once), `orientation_sessions.status` |
| Propose a buddy (same-team rule) | `buddy_profiles` (read), `buddy_assignments` (status `PROPOSED`, with `match_score` and `matching_criteria`), `approval_requests` |
| Approve the buddy: assign, check capacity, notify | `buddy_assignments` (status `CONFIRMED`), `audit_events` (`UC05_NOTIFICATION_RECORDED`) |
| Team introduction and buddy introduction sessions | same as for the induction session |
| RSVPs, kept newest-first so an older reply never overwrites a newer one | `session_participants` (`rsvp_status`, `responded_at`, `proposed_new_time`, `response_note`) |
| Joining day: draft the welcome message | `communications` (`DRAFT`; `review_gate_id` set to the approval request) |
| Welcome approved and delivered | `communications`: `SENT` plus `dispatched_at` only when a real provider accepted it; `SENT_SIMULATED` with no `dispatched_at` while delivery is simulated |
| Every tenant-scoped event, plus `workflow.completed` / `workflow.failed` | `audit_events` |
| Every run (API, orchestrator worker, approval listener) | `agent_runs` |
| Structured result to the orchestrator | the existing worker contract (`case_snapshot`, `run_event`), unchanged |

**Orchestration state.** UC05 does not write `case_orchestration_state`. The central orchestrator records UC05's outcome from the structured result UC05 returns, which is the existing contract; a write from UC05 would bypass the orchestrator. UC05 can read the row through `get_orchestration_state()`.

**Joining-date changes.** The joining date is read from `onboarding_cases` on every call. Each session records the date it was scheduled for (`session_metadata.joining_date_basis`). `case_status()` lists sessions drafted for an earlier date as `stale_sessions`. The orchestrator's existing date-mismatch checks then route the case to HR. Nothing is reported as successful against an out-of-date joining date.

## 5. Tenant isolation

The service-role key bypasses RLS, so isolation is enforced in UC05's own code:

- **Bound to the verified tenant.** `OnboardingToolRegistry` binds the repository, directory, event source and audit log to the verified actor's `client_id` (`for_tenant`). Every query on a tenant table then carries `client_id=eq.<tenant>`. A test asserts this for every GET, PATCH and DELETE.
- **No unscoped lookups.** An unbound repository refuses case-scoped lookups that don't name a tenant (`TenantScopeRequiredError`). An `employee_no` is unique only within a client, and the tests use the same `employee_no` in two tenants.
- **Another tenant's record reads as not found.** Asking for another tenant's session, communication or approval returns `KeyError`, never the record.
- **Participants.** `session_participants` has no `client_id` column. A participant row is only read or written after its session has been read with the tenant filter.
- **Where the tenant comes from.** The `client_id` always comes from the authenticated identity (JWT) or the orchestrator's verified actor, never from a request body.
- **What audit and run rows keep.** Ids, statuses, counts and short codes only; names, addresses, message text and credentials are dropped (`mapping.sanitize`).
- **RLS.** RLS is not disabled or changed by this work.

## 6. Errors and retries

| Error | When | Behaviour |
|---|---|---|
| `SupabaseConfigurationError` → `RuntimeConfigurationError` | missing or invalid variable | startup fails |
| `PersistenceAuthError` | HTTP 401/403 | never retried |
| `PersistenceUnavailableError` (retryable) | network error, timeout, 429, 5xx | reads retried twice. A failed write stops the run where it is; sending the same event again resumes it, and deterministic keys stop anything being created twice. |
| `UniqueViolation`, `CheckViolation`, `ForeignKeyViolation`, `NotNullViolation` | constraint failures | translated to UC05's own duplicate errors where that is the meaning; otherwise surfaced |
| `SchemaMismatchError` | missing table or column | startup fails, naming the tables |
| `ConcurrentUpdateError` | a conditional write lost 5 times | surfaced; nothing is overwritten |
| `ClientCalendarUnavailableError` | no active configuration or no effective approved version | scheduling routes to HR (`scheduling_failed`) |
| `BuddyCapacityExceededError` | assigning the buddy would exceed `max_active_buddies` | not assigned; the approval's `apply_error` is set and can be retried |
| `CaseOwnershipError`, `OnboardingCaseNotFoundError` | an attempt to change UC01's case, or a missing case | refused |

Error messages carry the HTTP status and the SQLSTATE code. They never include the response body, which can echo row values, and never the key.

## 7. Running locally

```powershell
# mock mode (default): no credentials needed
cd UC05_Orientation-Schedule-Agent
uv run uvicorn sub_agent.api:app --port 8010

# supabase mode: set the variables in a git-ignored .env first
$env:UC05_DATABASE_MODE = "supabase"
uv run uvicorn sub_agent.api:app --port 8010
```

Under the orchestrator, set `UC05_DATABASE_MODE` and the Supabase variables in the orchestrator's environment. The UC05 worker inherits them, and `runtime.build_context()` does the rest.

## 8. Tests

```powershell
# everything that runs without credentials (unit, tenant isolation, failure and recovery, end to end on the fake)
uv run pytest tests/test_supabase_persistence.py
uv run pytest            # the whole UC05 suite, mock mode

# live and opt-in: only with approved test records, see tests/test_supabase_live.py
$env:UC05_SUPABASE_LIVE_TESTS = "1"; $env:UC05_LIVE_TEST_CLIENT_ID = "<test tenant>"; $env:UC05_LIVE_TEST_EMPLOYEE_NO = "<test case>"
uv run pytest tests/test_supabase_live.py           # read-only
$env:UC05_LIVE_TEST_ALLOW_WRITES = "1"              # adds a link + session create/cancel round trip
```

`tests/fake_postgrest.py` is an in-process PostgREST fake (an `httpx.MockTransport`). It enforces primary and unique keys, rejects unknown tables and columns, and can simulate an outage or a refused key. It checks that UC05 sends correct requests. It does not check the live database's CHECK constraints, foreign keys or RLS; only the live tests do.

## 9. Mock and Supabase modes compared

| | mock | supabase |
|---|---|---|
| Data | synthetic, seeded in development or test | the shared database; nothing synthetic is ever seeded or mixed in |
| Credentials | none | URL and service-role key |
| Failure | n/a | a clear persistence error; never a fallback to mock data |
| Approvals and audit | SQLite or JSONL | `approval_requests`, `audit_events` (plus the local JSONL when `HR_AGENT_PERSIST=1`) |
| Still local in both modes | LangGraph checkpoints, joining-day job leases (`HR_AGENT_DATA_DIR`) | same; no table exists for them (see the report) |
| Calendar, notifications, LLM | simulated / FakeLLM | unchanged: still simulated / FakeLLM |
