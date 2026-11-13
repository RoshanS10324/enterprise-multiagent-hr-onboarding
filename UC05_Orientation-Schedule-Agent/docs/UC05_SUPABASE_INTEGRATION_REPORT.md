# UC05 Supabase integration: report

**Date:** 2026-11-04. **Status:** implemented and tested against an in-process fake of the Supabase REST API. **Not yet verified against the live database**, because no credentials are available on this machine (section 6).

Each item below is marked with one of these labels:
- **[fake]**: tested against the fake REST server
- **[impl]**: implemented but not tested
- **[live]**: verified against the real database (nothing has this label yet)
- **[blocked]**: needs a decision or change from someone else

## 1. Files created

| File | Purpose |
|---|---|
| `src/sub_agent/db/__init__.py` | package overview |
| `src/sub_agent/db/config.py` | `UC05_DATABASE_MODE`, Supabase settings, validation that never prints values |
| `src/sub_agent/db/postgrest.py` | `PostgrestClient`: one `httpx` client; error mapping (HTTP status and SQLSTATE); retries for reads only |
| `src/sub_agent/db/mapping.py` | required columns, status values, deterministic ids, `sanitize` for audit rows |
| `src/sub_agent/db/repository.py` | `SupabaseOnboardingRepository`, implementing `OnboardingPort` |
| `src/sub_agent/db/directory.py` | `SupabaseEmployeeDirectory`, `SupabaseCaseEventSource` |
| `src/sub_agent/db/approvals.py` | `SupabaseHITLInbox`: Common's `HITLInbox`, stored in `approval_requests` |
| `src/sub_agent/db/audit.py` | `SupabaseAuditLog`, `TenantAuditLog`, `AgentRunRecorder` |
| `src/sub_agent/db/factory.py` | `build_supabase_stores`, `verify_schema` |
| `tests/fake_postgrest.py` | in-process PostgREST fake, running as an `httpx.MockTransport` |
| `tests/test_supabase_persistence.py` | 45 tests |
| `tests/test_supabase_live.py` | 3 opt-in live tests |
| `docs/UC05_SUPABASE_INTEGRATION.md`, `docs/UC05_DATABASE_MAPPING.md`, this report | documentation |

## 2. Files modified (UC05 only; Common, UC01–UC04 and the orchestrator are untouched)

| File | Change |
|---|---|
| `runtime.py` | Supabase mode: validates the configuration, pings the database, checks the schema, then wires the Supabase repository, directory, event source, approvals, audit and run recorder. Never falls back to mock data. |
| `tool_registry.py` | Binds tenant-aware stores to the verified actor (`_bind_tenant`). Adds `_scheduling_settings()`: the client's own timezone, working days and holidays where available. Adds the `run_recorder` dependency. |
| `services/scheduling.py` | Optional `BuddyAssignmentLedger` calls: record a buddy proposal; release it on rejection. The matching rule itself is unchanged. |
| `repository.py` | The `BuddyAssignmentLedger` protocol, an optional capability |
| `models.py` | `ClientCalendar`, plus new errors: `ClientCalendarUnavailableError`, `BuddyCapacityExceededError`, `OnboardingCaseNotFoundError`, `CaseOwnershipError`, `TenantScopeRequiredError` |
| `agent.py` | `agent_runs` recording; `workflow.completed` and `workflow.failed` events; a missing client calendar routes to HR; `stale_sessions` in `case_status` |
| `scheduler.py` | lists tenants from `client_configs` in Supabase mode |
| `reconciliation.py`, `api.py` | their direct repository reads now carry the tenant |
| `.env.example` (UC05 and repository root) | placeholder Supabase variables only |

## 3. Synthetic and in-memory stores replaced in Supabase mode

| Mock / local store | Supabase replacement |
|---|---|
| `MockOnboarding` / `SqliteOnboardingRepository` | `SupabaseOnboardingRepository` |
| `MockEmployeeDirectory` | `SupabaseEmployeeDirectory` (`onboarding_cases`) |
| `MockEmailAdapter` (synthetic offer mailbox) | `SupabaseCaseEventSource` (the case uuid is the event id) |
| `HITLInbox` on SQLite (`hitl.db`) | `SupabaseHITLInbox` (`approval_requests`) |
| `AuditLog` (JSONL only) | `SupabaseAuditLog` (JSONL plus `audit_events`) and `agent_runs` |
| policy.yaml timezone, weekend and holidays | `client_configs`, `client_holidays` and the effective `client_config_versions` row |

Kept on purpose:
- **Simulated connectors.** The calendar (`MockCalendar`) and notifications (`MockNotifier`) stay simulated, because no real integrations exist.
- **FakeLLM.** The LLM stays fake.
- **Local stores.** LangGraph checkpoints and joining-day job leases stay in local SQLite.

Mock mode is the default and is unchanged.

## 4. Database operations implemented

| Operation | Status |
|---|---|
| Fetch the onboarding case, read fresh every time; link it to UC05 without ever creating it | [fake] |
| Client configuration (welcome), calendar, holidays, effective approved version; missing or inactive configuration refused | [fake] |
| Eligible buddies: active, with capacity, tenant only; proposal; assignment; confirmation; duplicate prevention; capacity enforced with rollback; reassignment only through conditional updates | [fake] |
| Sessions: create, read, by-date-range query, status update, time update, cancel; conflicts and duplicates detected; stored in UTC | [fake] |
| Participants, invitations, RSVPs kept newest-first, proposed alternate time and note | [fake] |
| Welcome communications: drafted once per case, `review_gate_id` set, dispatch claimed only after a real delivery | [fake] |
| Approval requests: tenant-scoped, decided once across processes | [fake] |
| Audit events, sanitised; agent runs | [fake] |
| Startup checks: configuration, credential, schema | [fake] |
| Reading the orchestrator's `case_orchestration_state` | [impl] (read-only; not called by UC05's flow) |
| Running against the live database | [blocked] (section 6) |

## 5. Tests executed

Run on 2026-11-04 with Python 3.12.16 on Windows. The fake REST server ran in-process; no network and no credentials were used.

| Suite | Result |
|---|---|
| `tests/test_supabase_persistence.py` | **45 passed** |
| Whole UC05 suite (mock mode plus the new tests) | **589 passed, 3 skipped, 1 failed** |
| UC05 suite before this work | 544 passed, 1 failed |

The failure was already there before this work: `test_orchestration_contract.py::test_status_vocabularies_match_what_the_orchestrator_validates` expects Common's `Decision` values without `withdrawn`. The 3 skipped tests are the opt-in live tests.

The 45 tests cover:
- **Repository operations:** the case, configuration and holidays, buddies, assignment, duplicates, sessions, conflicts, update and cancel, participants and RSVPs, communications, approvals, audit and runs.
- **Tenant isolation:** the same `employee_no` exists in two tenants. Client A cannot read or modify B's case, buddies, assignment, sessions, participants, communications or approvals. Every tenant-table GET, PATCH and DELETE carries B's or A's own `client_id` filter.
- **Failures:**
  - missing or invalid variables, with no values in the messages
  - a refused credential, with the key not leaked
  - an unreachable database
  - a missing table
  - a read retried, while a write is not
  - a write failure
  - concurrent buddy assignments
  - a decision raced by another process
- **Recovery:** a partial failure is resent with nothing created twice; welcome messages are not duplicated.
- **Workflow behaviour:** a joining-date revision is read live and the stale session is flagged; an approval rejection releases the booking or the buddy proposal.
- **End to end:** offer → induction → buddy → team introduction → RSVP → joining-day welcome → delivery. The test checks the rows in every table, that `onboarding_cases` is never written, that tenant B's data is untouched, that `agent_runs` and `audit_events` are filled, and that the client's holidays move the first slot.

**Test environment caveat.** During this session UC05's three skill files (`context_layer/skills/__init__.py`, `hr-policy-answerer/SKILL.md`, `onboarding-welcome-writer/SKILL.md`) were deleted from the working tree by someone else. UC05 does not import without them. The results above come from a scratch copy of UC05 with those three files restored from git `HEAD`. The working tree was not changed.

## 6. Live Supabase verification

**Not performed.** No `.env` file exists in the repository, and none of `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` or the test-record variables are set. No live write or read was made, and no claim here rests on one.

To verify, set the credentials and an approved test tenant and case, then run `tests/test_supabase_live.py`:
- The read-only checks include the startup schema check.
- With `UC05_LIVE_TEST_ALLOW_WRITES=1`, the test also links the case and creates and cancels one session.

## 7. Remaining blockers

1. **Live credentials and an approved test tenant and case.** UC01 must have created the case, because UC05 never creates one.
2. **No welcome configuration exists.** `client_configs.settings_payload.uc05_welcome` is not populated for any client, so live welcomes stop at `welcome_not_configured`.
3. **No approved configuration version.** UC02 observed only `draft` versions, so live scheduling routes to HR (`ClientCalendarUnavailableError`) until the configuration owners approve one.
4. **CHECK constraints are unknown** for sessions, participants, buddy assignments and communications, and for `approval_requests.outcome` and `gate`. The values UC05 writes are listed in `mapping.py`; a mismatch fails loudly with `CheckViolation`.
5. **The calendar and notifications are still simulated.** Sessions are real rows, but no real invitation or email is sent.
6. **The offer event in Supabase mode is the case uuid.** Callers of the orchestrator must pass the case's `case_id` as `offer_acceptance_event_id` in Supabase mode.
7. **The working tree is missing UC05's skill files** (section 5). Restore them or confirm the deletion was intended.

## 8. Actions needed from the database owner (nothing has been applied)

| # | Proposal | Why |
|---|---|---|
| 1 | Export the CHECK, foreign-key, unique and RLS definitions for `orientation_sessions`, `session_participants`, `buddy_assignments`, `buddy_profiles`, `communications` and `case_orchestration_state` | confirm or correct `mapping.py` |
| 2 | A partial unique index on `orientation_sessions (case_id, session_type, start_utc) WHERE status <> 'CANCELLED'` | moves duplicate-session prevention into the database |
| 3 | A database function `uc05_assign_buddy(case_id, client_id, buddy)` that locks the buddy's assignments, checks `max_active_buddies` and upserts, all in one transaction | makes the capacity check one atomic step instead of insert, verify and roll back |
| 4 | Allow `SENT_SIMULATED` in `communications.status` (or name an alternative value) | lets a simulated delivery be recorded without claiming a real one |
| 5 | Optional tables `uc05_operations` and `uc05_checklist_tasks` | replace the versioned audit rows UC05 uses today |
| 6 | A unique constraint on `onboarding_cases (client_id, employee_no)`, as UC01's design proposes | UC05 currently refuses to guess when two live cases match |
| 7 | Populate `settings_payload.uc05_welcome` and approve a configuration version for each test tenant | needed for the live welcome and scheduling flows |
| 8 | A restricted database role in place of `service_role`, with RLS on all UC05 tables | defence in depth; UC05 already filters every query by tenant itself |
