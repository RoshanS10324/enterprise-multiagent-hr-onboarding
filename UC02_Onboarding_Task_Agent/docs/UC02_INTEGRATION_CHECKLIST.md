# UC-02 integration checklist

As of 2026-11-04. "Verified" means observed or tested as stated; nothing here is claimed beyond that. No credential appears in this file or
should ever be sent in chat or committed: the integration/IT team puts values into the deployment's secret store.

## 1. UC-02-owned work (complete and tested)

* Event intake (signed, idempotent), case/offer/form lookup from Supabase, approved-configuration retrieval, dynamic checklist, Fake LLM review,
  HR approval callback, team assignments, document workflow and gate, downstream triggers, task lifecycle (dependencies, parallel waves, due dates,
  overdue, sweep), restart recovery, duplicate-event handling.
* Supabase-only runtime; local environment/secret tooling (`scripts\setup_dev_environment.ps1`, `scripts\check_environment.ps1`).
* Mock Outlook (default), mock UC-03/UC-04/UC-05 counterparts, all labelled as mocks.
* Evidence: `docs/UC02_TRACEABILITY_MATRIX.md`, `docs/UC02_FINAL_ARCHITECTURE_AND_STATUS.md`.

## 2. Contracts (UC-02 side implemented; the other side is external)

| Contract | Direction | Shape and security | Idempotency key | Failure behaviour | Status |
|---|---|---|---|---|---|
| Employee created | UC-01 / coordinator -> UC-02 | `EmployeeCreatedEvent{event_id, client_id, employee_id, case_id, joining_date, occurred_at, correlation_id, signature}`; HMAC with `UC02_EVENT_SECRET` | `event_id` (ledger `ev:<id>`) | bad signature -> `invalid_signature`; replay -> `duplicate` and the existing onboarding is returned | UC-02 done; UC-01 must sign and send |
| Case lookup | UC-02 -> Supabase | read `onboarding_cases`, `offers`, `onboarding_forms`, `offer_forms`; never written | - | missing case/offer/form -> blocked with a stated reason | done |
| Configuration lookup | UC-02 -> Supabase | approved `client_config_versions` for the joining date (approver, timestamp and hash required; mock/draft/retired refused) | - | no approved version -> `MissingConfigurationError`, no plan | done; real approved configuration is **external** |
| Plan trigger (orchestrator) | coordinator -> UC-02 | `call_uc02_plan_tasks` / `UC02SubAgent.run`; see `UC02_INTEGRATION_CONTRACTS.md` section 1 | one live onboarding per employee per client | domain errors -> stage BLOCKED with code | done |
| HR approval callback | approval portal -> UC-02 | `ApprovalCallback{callback_id, onboarding_id, request_id, plan_version, decision, approver_id, approver_role, issued_at, reason, client_id, signature}`; HMAC with `UC02_APPROVAL_CALLBACK_SECRET`; decision `approved` / `rejected` / `changes_requested`; self-approval refused; rejection needs a reason | `callback_id` | stale plan version, wrong role, bad signature are refused and audited | UC-02 done; the portal is **external** |
| Message authorisation | HR lead -> UC-02 | `MessageDecision`, same secret; a different person from the requester | `decision_id` | unauthorised send is blocked (`blocked_no_approval`) | done |
| Document status | document source (UC-03) -> UC-02 | `DocumentUpdate{update_id, client_id, onboarding_id, plan_version, document_key, state, occurred_at, reference, signature}`; HMAC with `UC02_DOCUMENTS_SECRET` | `update_id` | invalid transition / stale version refused | UC-02 done; UC-03 sender is **external** (no agreed UC-03 payload) |
| UC-04 / UC-05 trigger | UC-02 -> UC-04 / UC-05 | signed POST to the team's https URL after the mandatory-document gate | deterministic trigger id | up to 3 attempts, then `requires_intervention`; no URL -> `not_configured` (never faked) | UC-02 done; team URLs and secrets are **external** |
| UC-04 / UC-05 status | UC-04 / UC-05 -> UC-02 | `DownstreamStatusEvent`; HMAC with `UC02_UC04_STATUS_SECRET` / `UC02_UC05_STATUS_SECRET` | `event_id` | stale, unknown or unverified source refused | UC-02 done; senders **external** |

Error responses: the HTTP API returns 503 with a stated reason when the Supabase runtime or a secret is not configured, and never starts a
memory/SQLite fallback. Business rejections carry stable codes and are audited.

## 3. Real email: exactly what the integration / IT team must provide

Development uses `MockOutlookConnector` (default; sends nothing). Real delivery is not complete until an authorised live test passes
(`docs/UC02_OUTLOOK_GRAPH_SETUP.md` section 7). Needed from IT, delivered as secrets/config, never in chat or git:

1. **Entra app registration** (single tenant, no redirect URI, client-credentials flow): tenant ID, application (client) ID.
2. **Microsoft Graph application permissions, admin-consented:** `Mail.ReadWrite` and `Mail.Send` only.
3. **Client secret** (short expiry) in the secret manager -> `UC02_GRAPH_CLIENT_SECRET`.
4. **Mailbox restriction:** the app limited to the one HR mailbox (RBAC for Applications / Application Access Policy); confirm 403 for other mailboxes.
5. **A dedicated HR mailbox** (a shared mailbox is fine; test mailbox first) -> `UC02_GRAPH_MAILBOX`.
6. **Approved recipient mapping** JSON (per client: HR role addresses, per-team addresses) at `UC02_RECIPIENTS_FILE`, owned by the deployment.
   Which team/role addresses are approved is for the configuration owners to decide; UC-02 refuses to guess.
7. **A test tenant/mailbox and a sink recipient** for the verification plan; and a decision on delivery evidence (message trace / bounce handling) if
   "delivered" must ever be reported, because Graph's 202 only means "submitted for sending".
8. Optional: sovereign-cloud endpoints `UC02_GRAPH_AUTHORITY`, `UC02_GRAPH_BASE_URL`.

Enabling later (after step 1-7): set `UC02_OUTLOOK_CONNECTOR=graph` and the variables above through the secret store, run
`demo/uc02_graph_integration_check.py` (read-only connection check) and then the verification plan. `graph` never falls back to the mock.

What UC-02 already does for email: stores each prepared message with a content fingerprint, requires a different HR lead to authorise a send,
records every result (`draft_created`, `submitted_for_sending`, `failed`, `duplicate_ignored`), retries 3 times with backoff, and persists message status.

## 4. Other external dependencies (awaiting other teams)

| Item | Owner | Blocks |
|---|---|---|
| Approved client configuration for real clients; approval of the `uc02_assignment` team-mapping convention in `client_service_policies` | configuration owners | real (non-synthetic) plans |
| HR approval portal that signs and posts the callback | portal / orchestration team | real HR decisions |
| UC-04 and UC-05 trigger URLs, shared secrets, and their status events | UC-04 / UC-05 teams | real downstream |
| UC-03 -> UC-02 document status payload and sender | UC-03 team | real document verification |
| Item-to-task mapping for UC-04 / UC-05 reports; service identities | UC-04 / UC-05 | production use of status updates |
| Reminder delivery and the scheduler for sweeps | UC-06 / platform | reminders actually reaching people |
| Optional live columns for delay/failed-report counters (proposed migration, not applied) | DB owner / Tech Lead | none (state is carried in the snapshot) |
| Production secret management and a production Supabase project | platform | production |

## 5. How to run (PowerShell, from the UC-02 folder)

**A. Isolated local mock demo (nothing real; no `.env`, no Supabase, no network, no files):**

```powershell
python demo\uc02_local_mock_demo.py             # 22 checks; exit 0 only if all pass; add --verbose for full stage output
```

**B. Supabase Development integration demo (writes UC-02 records for the synthetic case; mock Outlook and mock downstream):**

```powershell
.\scripts\setup_dev_environment.ps1                 # once; local secrets only; no database access
.\scripts\check_environment.ps1 -Stage full
python demo\uc02_supabase_agentic_workflow.py preflight        # read-only
python demo\uc02_supabase_agentic_workflow.py run --mock-downstream
python demo\uc02_supabase_agentic_workflow.py decide --decision approved --mock-downstream
python demo\uc02_supabase_agentic_workflow.py send --mock-downstream
python demo\uc02_supabase_agentic_workflow.py documents --mock-downstream
python demo\uc02_supabase_agentic_workflow.py tasks --mock-downstream --waves 2
python demo\uc02_supabase_agentic_workflow.py tasks --mock-downstream --waves 0 --as-of 2026-11-20   # overdue + sweep only
python demo\uc02_supabase_agentic_workflow.py recover --mock-downstream
python demo\uc02_supabase_agentic_workflow.py verify           # read-only read-back
python -m pytest tests -o addopts="" -p no:cacheprovider -q
python evaluation\run_evals.py
```

**State to expect on Development:** 13 tasks - 4 complete, 7 ready, 2 blocked. The 7 ready tasks (`background_verification`, `visa_status_change`, `payroll_setup`,
`insurance_enrolment`, `system_access`, `senior_briefing`, `policy_ack`) wait for their owner or the external use case; `medical_test` waits for `visa_status_change`
and `biometrics` for `medical_test`. `tasks --waves N` can progress them; the onboarding is **not** complete and is not reported as complete.

**Reminders and monitoring:** overdue is derived from due date and clock; the sweep produces reminder drafts and escalation records only. No scheduler exists in the
code and nothing is sent automatically. Future requirement: a scheduler (platform/UC-06) that calls `monitoring_sweep` per active onboarding and a delivery channel
(the Graph connector once configured) that sends approved reminders.
