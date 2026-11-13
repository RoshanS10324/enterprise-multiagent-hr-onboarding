# Phase 4: Critical Defect Remediation (HR-Onboarding-Agent)

Date: 2026-10-27. Scope: sections A–F of the Phase 4 brief. This follows the defects and blockers in
`docs/PHASE_3_VERIFICATION_REPORT.md`.

**Status in one line:** every defect in scope is fixed and covered by a regression test that fails
when the defect is put back (14 of 14 mutants caught). The service is **not production-ready**.
Every external connector is still a fake, no real identity provider has been tested, and SQLite is
single-host. `HR_AGENT_ENV=production` deliberately refuses to start.

Neither the Recruitment Agent nor `hr-recruitment-common` / `hr-onboarding-common` was modified.
No credentials, endpoints or identity-provider settings were invented. No real email, meeting or
message was sent: every test runs against the fakes in `src/sub_agent/fakes.py`.

---

## 1. Files changed

New modules stay inside the approved structure. Orchestration remains in `agent.py`, provider
contracts stay in `connectors/`, and persistence sits behind `OnboardingPort`.

| File | Change |
|---|---|
| `src/sub_agent/identity.py` (new) | `JwtIdentityProvider`, `JwtSettings`, `build_identity_provider`; the dev-only header mode and its guard |
| `src/sub_agent/persistence.py` (new) | `Database` (SQLite, WAL, IMMEDIATE transactions, schema version), `SqliteOnboardingRepository`, `SqliteJobStore` |
| `src/sub_agent/runtime.py` (new) | `build_context()`: HR_AGENT_ENV rules, stores, fakes; `SanitizedLLM`. Shared by the API and the scheduler |
| `src/sub_agent/scheduler.py` (new) | `JoiningDayScheduler`, `JobStore` protocol, `SchedulerSettings`, CLI `python -m sub_agent.scheduler` |
| `src/sub_agent/reconciliation.py` (new) | read-only report of what a crash left out of step between the stores |
| `src/sub_agent/services/delivery.py` (new) | `deliver_once` (ledger-backed sends), `resolve_operation`, `DeliveryOutcomeUncertainError` |
| `src/sub_agent/services/scheduling.py` | booking ledger, silent holds plus invite-on-confirm, `_release`, RSVP ordering, superseded conflicts, resumable replans |
| `src/sub_agent/agent.py` | in-flight booking reuse; stages `conflict_superseded`, `response_superseded`, `welcome_delivery_uncertain`; RSVP `stale` route; case status hides superseded conflicts |
| `src/sub_agent/tool_registry.py` | welcome, approval-request and conflict notices via the ledger; new operations: `in_flight_booking`, `conflict_superseded_reason`, `superseded_conflicts`, `notify_conflict_superseded`, `list_unsettled_operations`, `resolve_operation`, `reconciliation_report` |
| `src/sub_agent/api.py` | JWT identity; runtime builder; `retry-apply` role check; admin routes; `responded_at`; generic 404 for cross-tenant access |
| `src/sub_agent/models.py` | `OperationRecord` (+`detail`), `OperationStatus`, `JoiningDayJob`, `JobStatus`, `InvitationStatus.PENDING`, `OnboardingSession.booking_operation_key` |
| `src/sub_agent/repository.py` | contract: newest-wins RSVPs, `update_invitation_status`, operation ledger methods |
| `src/sub_agent/fakes.py` | `MockOnboarding` ledger and newest-wins; idempotent `FakeCalendar`/`FakeNotifier` (with non-idempotent and lost-response modes); `InMemoryJobStore`, `FakeClock`; synthetic seed for SQLite; mypy fix |
| `connectors/calendar/base.py`, `connectors/notifications/base.py` | idempotency key and capability flags; `notify_attendees` on book/confirm |
| `guardrails/policy.yaml` | v9: `settings.invitations`; tools `onboarding.list_participant_responses`, `onboarding.reconciliation_report`, `operations.list`, `operations.resolve`, `notifications.conflict_superseded` |
| `pyproject.toml`, `uv.lock` | `pyjwt[crypto]`, `langgraph-checkpoint-sqlite`, `python-dotenv` declared. The lockfile only **adds** cffi 2.1.1, cryptography 50.0.2, pycparser 3.12 and PyJWT 2.15.1; no existing version changed |
| `.env.example`, `README.md` | safe defaults (`HR_AGENT_ENV=production`, `IDENTITY_MODE=jwt`, `HR_AGENT_PERSIST=1`); dev lines commented out |
| `tests/test_identity.py`, `tests/test_persistence_and_recovery.py`, `tests/test_scheduler.py` (new) | see section 6 |
| `tests/testcase.py`, `test_invitations_and_timeouts.py`, `test_onboarding_repository.py`, `test_startup_and_config.py`, `test_api_routes.py`, `test_security_fixes.py`, `conftest.py` | new regressions; 2 xfails promoted; contract changes listed in section 6.2 |

## 2. Design decisions and persistence backend

**Persistence: SQLite through stdlib `sqlite3`** (`src/sub_agent/persistence.py`, one file
`<HR_AGENT_DATA_DIR>/onboarding.db`). Chosen because:
- it is the storage this project already configures for durable state (`hitl.db`, `checkpoints.db`);
- it needs no new service or driver;
- no Enterprise HR Agent-approved server database exists, and none was available to test against.

What it is suitable for:
- One host on local disk: one API process plus the scheduler process. WAL mode, `BEGIN IMMEDIATE`
  write transactions and a 30 s busy timeout make that safe, and a test proves cross-connection
  uniqueness and single-winner job claims with 8 concurrent connections.
- It is **not** suitable for several hosts or replicas, network file systems or high availability.
  Those need a server database implementing the same `OnboardingPort`, and
  `tests/test_onboarding_repository.py` is the acceptance suite for it.
- The contract rules are enforced by the schema, not only by code: partial unique indexes for live
  tasks and sessions, and unique acknowledgements, cases, RSVPs and operation keys.
- Schema version is pinned (`PRAGMA user_version`); a newer database is refused. There is no
  migration tool yet.
- Backups must copy the database with its `-wal` file. Personal data is unencrypted at rest, so
  encrypt the volume.

What is persisted:
- cases and checklist tasks
- acknowledgements
- sessions with their booking operation
- invitations (PENDING/SENT/FAILED) and RSVPs with provider timestamps
- buddy assignments and notification records
- welcome drafts
- the operation ledger (provider ids and statuses)
- joining-day jobs

`MockOnboarding` stays for tests and development, and the same contract suite runs against both.

**Environments** (`runtime.py`, `HR_AGENT_ENV` defaults to `production`):

| `HR_AGENT_ENV` | In-memory data | Header identity | Fake connectors |
|---|---|---|---|
| `development`, `test` | allowed; an empty database is seeded with synthetic data | allowed | allowed |
| `staging` | refused (`HR_AGENT_PERSIST=1` required) | refused | allowed, logged |
| `production` | refused | refused | **refused**, so it cannot start until real adapters exist |

**Other decisions:**
- **Invitations** default to `on_approval`: holds are silent, Invitation rows are PENDING, and
  confirmation invites. `on_hold` is an explicit per-policy opt-in.
- **RSVPs** are newest-wins by the provider's timestamp, enforced atomically in the repository.
- **Stale conflicts** are derived from the records at decision time. The decision is not applied,
  and HR is notified once.
- **Cross-tenant access** returns the same 404 bytes as a missing record; the audit log keeps the
  truth.
- **Model errors**: `SanitizedLLM` swaps every provider message for a generic one.

## 3. Identity flow

1. A request carries `Authorization: Bearer <JWT>` issued by the organisation's IdP or by a
   trusted gateway that authenticated the caller.
2. `JwtIdentityProvider.authenticate` verifies:
   - the signature, with **asymmetric algorithms only** (`none` and `HS*` are refused, which blocks
     RS/HS algorithm confusion); the key comes from `IDENTITY_JWT_PUBLIC_KEY(_FILE)` or
     `IDENTITY_JWKS_URL`;
   - `iss`, `aud`, `exp`/`nbf` with leeway, and that `iat` is no older than
     `IDENTITY_JWT_MAX_AGE_S`;
   - that the claims are present: a non-empty `sub`, a non-empty tenant claim, and a list-of-strings
     roles claim.

   Anything else is `401`. `X-User-Id`, `X-User-Roles` and `X-Client-Id` are ignored.
3. The resulting `VerifiedActor` becomes the single `Actor` that authorizes:
   - the route;
   - the workflow run;
   - every tool call (`OnboardingToolRegistry`: tenant and role checks, kill switch, allowlist,
     audit);
   - HITL decisions (`HITLInbox.decide` checks the approver role and tenant);
   - decision retries. `POST /v1/approvals/{id}/retry-apply` checks only the tenant in the shared
     library, so `api._authorize_retry_apply` adds "approver role or `hr_admin`". The retried side
     effects run as the recorded approver, as before.
4. **Development mode:** `IDENTITY_MODE=insecure-dev-headers` is refused at startup unless
   `HR_AGENT_ENV` is `development` or `test`. `HR_AGENT_ENV` defaults to `production`, so it cannot be
   switched on by accident. The retired `HR_AGENT_ALLOW_INSECURE_HEADER_IDENTITY` now fails startup
   instead of being silently ignored.

**Not verified:**
- no real IdP or gateway was configured or called;
- JWKS was exercised only against a local HTTP server serving a generated key;
- issuer, audience and claim names must be agreed with the IdP owners;
- token revocation is not checked; short token lifetimes are the mitigation.

## 4. Idempotency strategy and its limits

Every external side effect has a stable logical key in the operation ledger:

| Operation | Key |
|---|---|
| calendar hold | `calendar:<client>:<employee>:<type>:<start UTC>` |
| welcome message | `welcome:<client>:<draft>` |
| approval request | `approval-request:<client>:<approval>` |
| buddy notice | `buddy-notice:<client>:<approval>:<recipient>` |
| "no decision needed" notice | `conflict-superseded:<client>:<approval>` |
| conflict replan | `replan:<client>:<approval>` (tracks a cancel-then-rebook in progress) |

**How a call goes through the ledger:**
1. The ledger record is written PENDING **before** the provider call. Its `operation_id` is the
   provider idempotency key, reused on every retry.
2. The outcome is recorded as:
   - SUCCEEDED, with the provider's id (event ref or delivery ref);
   - REJECTED, for a definitive refusal;
   - UNCERTAIN, for a timeout or outage, which may have taken effect.
3. A retry of an UNCERTAIN or PENDING operation:
   - reuses the same key when the provider advertises `idempotent_booking` or
     `idempotent_delivery`;
   - otherwise raises `BookingOutcomeUncertainError` or `DeliveryOutcomeUncertainError`, which the
     workflow routes to HR (`scheduling_failed`, `welcome_delivery_uncertain`). HR checks the
     provider and calls `POST /v1/onboarding/admin/operations/resolve`.
4. A booking whose hold is later cancelled is marked RELEASED, so re-booking that slot starts a
   fresh operation.
5. The workflow retries an in-flight booking at **its** slot rather than searching again; the
   uncertain hold would make that slot look busy.

**Limits:**
- This is **not exactly-once**. It is at-most-once towards the recipient only when the provider
  really deduplicates on the key. With a provider that cannot, an uncertain outcome needs a human.
- `confirm_event` and `cancel_event` are not ledgered; they rely on idempotent update and delete
  semantics at the provider (the fake behaves that way).
- The ledger, the HITL inbox and the checkpoints are separate SQLite files, so a crash between two
  writes can leave them out of step. `GET /v1/onboarding/admin/reconciliation` reports:
  - interrupted runs
  - failed decisions
  - unsettled operations
  - sessions without approval
  - approvals without records
  - unreleased rejections

  It changes nothing; a hold whose approval was never created needs HR.
- Only fakes implement the capability flags. A real adapter must set them truthfully.

## 5. Scheduler design

`python -m sub_agent.scheduler [--once]` runs as its own process, separate from the workflow, over
the same stores (`runtime.build_context`). It refuses to start without `HR_AGENT_PERSIST=1`.

- **Unit of work:** one `JoiningDayJob` per (tenant, local date), unique in `onboarding.db`.
  - A tick claims a job in one IMMEDIATE transaction and then runs
    `OnboardingAgent.run_event(JOINING_DAY)` as a tenant-scoped system actor
    (`system:joining-day-scheduler`, role `system_scheduler`), the same path as the API route.
  - It drafts welcomes for HR approval and sends nothing itself.
- **No duplicate runs:**
  - DONE jobs are never claimed again, across restarts and across scheduler processes.
  - A RUNNING job is skipped while its lease is live.
  - Underneath, the welcome service skips anyone already drafted.
- **No missed runs:**
  - Each tick looks back `SCHEDULER_CATCH_UP_DAYS` local days, so downtime within the window is
    caught up late, not skipped.
  - A dead worker's lease expires and the job is reclaimed; the run resumes from its checkpoint.
  - Failures are retried until `SCHEDULER_MAX_ATTEMPTS`.
- A date is due from `SCHEDULER_RUN_AT` in the policy timezone.
- **Deterministic tests:** `FakeClock`, `InMemoryJobStore` (same claim semantics), and
  `SqliteJobStore` on a temporary file.

## 6. Defects fixed, with tests

### 6.1 Regression tests per requirement

| Req. | Defect | Fix | Tests (file :: test) |
|---|---|---|---|
| A | Caller identity came from trusted headers | `JwtIdentityProvider`; dev mode guarded by `HR_AGENT_ENV` | `test_identity.py`: 37 tests, including `test_in_jwt_mode_request_headers_can_never_supply_a_user_role_or_tenant` (headers alone 401; `X-User-Roles: hr_admin` on an employee token stays 403 on the sweep, the reconciliation report and decisions; `X-Client-Id` cannot change tenant), plus the expired, wrong-key, wrong-issuer/audience, `none`/HS256 and missing-claim cases |
| A | `retry-apply` had no role check | `_authorize_retry_apply` | same test: retry is 401 without a token, 403 for the wrong role, 404 for another tenant, 200 for the approver role |
| A | Header identity enabled by default (`=1`) | removed; `IDENTITY_MODE` + `HR_AGENT_ENV` | `test_startup_and_config.py::test_header_identity_is_refused_at_startup_outside_development_and_test`, `…retired_header_identity_switch…`, `…default_jwt_mode_fails_startup` |
| B | Onboarding records were in memory only | `SqliteOnboardingRepository`; production requires persistence | `test_persistence_and_recovery.py` (9 tests: restart with the workflow continuing; interrupted run resumed after restart; failed decision retried after restart; build_context restart; cross-connection uniqueness; concurrent claims; schema guard; reconciliation); `test_onboarding_repository.py` (49 contract tests × memory/sqlite); `test_startup_and_config.py::test_non_development_environments_refuse_in_memory_data_and_production_refuses_fakes`, `…staging_runs_on_persistent_storage…` |
| B | Reconciliation deadlocked on `SqliteSaver.list()` (found while testing) | thread ids collected before reading state | `test_persistence_and_recovery.py::test_reconciliation_finds_a_hold_whose_approval_was_never_created` (hung before the fix) |
| C | Lost booking response created a second hold (was a strict xfail) | booking ledger + idempotency key | `testcase.py::test_booking_timeout_after_the_provider_created_the_event_leaves_no_orphan_hold` (**promoted**) |
| C | Lost welcome response sent the welcome twice (was a strict xfail) | `deliver_once` | `testcase.py::test_welcome_delivery_timeout_after_acceptance_is_not_sent_twice` (**promoted**) |
| C | Approval request and buddy notice resent after a lost response | `deliver_once` | `testcase.py::test_a_lost_approval_request_response_is_not_delivered_twice_on_resume`, `…lost_buddy_notice…`; `test_persistence_and_recovery.py::test_an_approval_request_lost_mid_send_is_reported_then_settled_on_resume` |
| C | Non-idempotent providers would be retried blindly | uncertain → HR; resolve route | `testcase.py::test_a_lost_booking_response_with_a_non_idempotent_calendar_goes_to_hr_not_a_second_booking`, `…non_idempotent_notifier_is_never_resent`, `…operations_admin_routes_are_hr_admin_only_and_tenant_scoped` |
| D | Invitations went out with the tentative hold | silent hold, invite on confirm | `test_invitations_and_timeouts.py::test_a_tentative_hold_invites_nobody_until_the_booking_is_approved`, `…failed_invite_does_not_block_the_confirmation`, `…rejected_hold_never_invites…`, `…on_hold_invitation_policy…`, `…unknown_invitation_policy…` |
| D | Out-of-order RSVPs overwrote newer ones | newest-wins in the repository | `testcase.py::test_an_rsvp_delivered_out_of_order_never_rolls_back_a_newer_one`, `…out_of_order_rsvp_on_the_api_is_a_409`; contract `test_participant_responses_are_newest_wins_by_responded_at[memory,sqlite]` |
| D | Stale conflict decisions were applied | derived at decision time; HR told once | `testcase.py::test_a_decline_withdrawn_before_hr_decides_supersedes_the_conflict`, `…repeated_decline_does_not_open_a_second_hr_decision` |
| D | A replan interrupted after cancelling lost the meeting | replan ledger; resume on retry | `testcase.py::test_a_replan_interrupted_after_cancelling_the_original_resumes_on_retry`, `…replan_whose_new_slot_is_taken_routes_to_hr` |
| D | Untested routes | — | `testcase.py`: `validate_offer_event` inactive, `draft_induction_meeting` exists, `assign_buddy` awaiting/assigned, `prepare_team_intro` exists/failed, `apply_welcome_decision` sent, `deliver_welcome` duplicate, `record_participant_response` reject, `replan_failed` (conflict) |
| E | No joining-day trigger | `sub_agent.scheduler` | `test_scheduler.py` (9 tests: once per tenant-day, run time, catch-up, restart, retry, max attempts, lease expiry, CLI refuses in-memory, two CLI processes) |
| F | 403 vs 404 revealed that another tenant's record exists | same 404 bytes; audited | `test_api_routes.py::test_cross_tenant_read_is_indistinguishable_from_not_found`, `…write…`; `test_security_fixes.py::test_http_run_against_another_tenants_employee_is_404_not_executed`; `testcase.py::test_r04_cross_tenant_errors_are_indistinguishable_from_not_found`, `…rsvp_for_another_tenants_session_is_404` |
| F | Provider text in model-backed responses | `SanitizedLLM` | `testcase.py::test_a_model_outage_while_drafting_a_welcome_returns_no_provider_text` (plus the Phase 3 policy-question tests) |
| F | mypy error (`fakes.py`, pre-existing) | typed list | mypy: no issues in 41 files |

**`apply_buddy_decision -[stale]`** has no test. It can't be reached with the HITL statuses that
exist: an approved decision is always assigned or raises, and a rejection routes to `rejected`.
The branch is defensive.

### 6.2 Contract changes to existing tests (deliberate; assertions not weakened)

- **Invitation tests:** the SENT/FAILED assertions moved from booking time to confirmation time,
  and booking time now asserts PENDING and nobody invited.
- **Cross-tenant tests:** five `== 403` tests became `== 404`, plus byte-equality with the
  genuinely-missing response.
- **The header-identity test** became the `IDENTITY_MODE`/`HR_AGENT_ENV` tests.
- **The two strict xfails** are now normal tests with ledger assertions added.

## 7. Remaining risks and blockers

1. **No real connectors.** Email, HRMS, calendar, Teams/Outlook and e-signature are fakes. Each
   real adapter must honour `notify_attendees=False` holds and set the idempotency flags
   truthfully. Production cannot start until then.
2. **Identity provider not integrated.** Issuer, audience, key or JWKS URL, and claim names need
   the IdP owners. Nothing was tested against a real IdP or gateway. `HR_AGENT_ENV` is still an
   environment variable, so an operator who sets `development` in production re-enables header
   identity; deployment review must guard it.
3. **SQLite is single-host.** More than one host needs a server database (and a shared LangGraph
   saver) passing the contract suite. There is no migration tool, and data at rest is
   unencrypted.
4. **Not exactly-once.** See section 4. An uncertain outcome with a non-deduplicating provider
   needs HR.
5. **Cross-store crash windows** are detected by the reconciliation report, not repaired. In
   particular, a hold whose approval item was never written needs HR.
6. **Per-process locks.** Welcome drafting is serialized only within one process. A manual
   joining-day call racing the scheduler process could draft twice.
7. **Remaining existence oracle on offer events.** Another tenant's offer event id gives 404,
   while an unknown id gives 200 with `invalid_employee_data`. Event ids are opaque mailbox ids,
   so the impact is low.
8. **`apply_error` text** (written by `hr-onboarding-common`) is visible to the tenant through
   `/v1/approvals`. It can carry connector error text; it is not model text.
9. **Business decisions to confirm with HR:**
   - superseded conflict decisions (including a "cancel") are not applied, and HR is notified
     instead;
   - a replan cancels the original before booking the replacement;
   - catch-up runs draft late welcomes;
   - the policy has a single timezone, not one per tenant.
10. **Scheduler operations.** There is no health endpoint and no API listing jobs that exhausted
    their retries (they are visible in `onboarding.db`).

## 8. Commands run and results

All commands were run from `HR-Onboarding-Agent/` on Windows 11 with Python 3.13.1 and uv 0.13.6.
Every result below is from the final code. The mypy cache was kept in a temporary directory
outside the repository, and no cache or data directory was left in the repo.

| Command | Result |
|---|---|
| `.venv/Scripts/python -m pytest -p no:cacheprovider` (final run, after the last code change) | **469 passed** in 105 s, 0 failed, 0 xfail (Phase 3: 352 passed + 2 xfailed). An earlier identical run through `uv run --offline pytest -p no:cacheprovider`: 469 passed |
| `uv run --offline python evaluation/run_evals.py` | 36 scenarios: **35 pass, 0 fail, 0 error, 1 skip** (needs a live model) |
| `uvx ruff check --no-cache --select F,E9 src connectors context_layer evaluation tests` | All checks passed |
| `MYPYPATH=src uvx mypy --cache-dir <temp> --python-executable .venv/Scripts/python.exe --ignore-missing-imports --explicit-package-bases -p sub_agent -p connectors -p context_layer` | **Success: no issues found in 41 source files** (Phase 3: 1 pre-existing error) |
| `uv lock --offline` (after declaring python-dotenv) | Resolved 106 packages; lockfile gained only the direct-dependency entry |
| Mutation check (temporary script: re-introduce each defect, run its tests, restore and checksum the file) | **14/14 mutants caught**; all files restored byte-identical |
| OpenAPI contract diff against the earlier snapshot | additive only: 3 admin routes and the optional `responded_at`; nothing removed, no required field added |

During development, the first full run of `tests/test_persistence_and_recovery.py` hung. That was
the reconciliation deadlock in section 6, now fixed and covered.
