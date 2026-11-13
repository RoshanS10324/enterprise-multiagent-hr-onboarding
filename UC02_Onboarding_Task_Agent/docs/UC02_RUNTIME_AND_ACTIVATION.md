# UC-02 runtime, external contracts and activation

Everything here is implemented and tested against MOCKED externals only. No real Microsoft 365, Supabase write, UC-04/UC-05
endpoint or approval portal has been contacted. What remains when access exists is configuration, connection verification and
controlled end-to-end testing (sections 5 to 7), not development.

## 1. What runs

`runtime.build_flow(env)` builds a `flow_runtime.RuntimeFlow`; `sub_agent/api.py` serves it. Routes (all POST, JSON):

| Route | Sender | Authentication | Effect |
|---|---|---|---|
| `/v1/events/employee-created` | UC-01 / orchestrator | HMAC, `UC02_EVENT_SECRET` | Retrieves the employee, selects the approved configuration, builds the checklist and document list, Fake LLM review, submits the plan, prepares the HR email and saves its Outlook draft. A repeat for the same employee is a duplicate and creates nothing. |
| `/v1/approvals/callback` | approval portal / connector | HMAC, `UC02_APPROVAL_CALLBACK_SECRET`; client id is a signed field | Approve, reject (reason required) or request changes. Approval prepares team assignments, the document request and an HR notice. |
| `/v1/messages/decision` | approval portal / connector | HMAC, same secret as the approval callback | An HR lead authorises one prepared message; UC-02 then sends it through the Outlook connector. UC-04/UC-05 assignments stay held until documents are verified. |
| `/v1/documents/status` | document collection process | HMAC, `UC02_DOCUMENTS_SECRET` | Individual document status. The update that completes the last mandatory document opens the gate and triggers UC-04 and UC-05. |
| `/v1/downstream/status` | UC-04, UC-05 | HMAC per use case, `UC02_UC04_STATUS_SECRET` / `UC02_UC05_STATUS_SECRET` | Triggered/accepted/in progress/completed/blocked/failed/delayed/requires intervention. |

Each channel refuses everything (503) until its secret is set; nothing is accepted unauthenticated. Humans never authenticate
to UC-02 directly: the portal/connector authenticates them and relays a signed decision.

Automatic: employee event to submitted plan and HR email draft; approved plan to prepared assignments, document request and
notice; gate open to downstream triggers. Never automatic: approving or rejecting the plan, and authorising any send.

## 2. Signing (all inbound channels)

`signature` = hex HMAC-SHA256 of the canonical JSON of the body WITHOUT the `signature` field (keys sorted, no spaces),
using the channel secret. The approval callback signs a fixed pipe-joined field list (see `flow.ApprovalCallback.canonical`).
Both `client_id` fields are signed, so a decision for one client cannot be replayed against another.

## 3. Approval portal contract (external; not built, not invented)

UC-02 cannot turn an email click into a signed decision. A portal or approval connector must:
1. show HR the review (the HR email's content), authenticate the approver, and check their role;
2. POST a signed `ApprovalCallback` (`callback_id`, `onboarding_id`, `request_id`, `plan_version`, `decision`
   in `approved|rejected|changes_requested`, `approver_id`, `approver_role`, `issued_at`, `reason` (required unless approved),
   `client_id`, `signature`);
3. POST a signed `MessageDecision` when an HR lead authorises a message.
Callbacks older than 15 minutes, replays, stale plan versions, unauthorised roles and self-approval are refused.
Set `UC02_APPROVAL_CALLBACK_URL` to the endpoint the portal should post to; it is shown in the HR email as text.

## 4. Downstream trigger contract (UC-04 / UC-05 teams to implement)

UC-02 `POST`s the trigger payload to `UC02_UC04_TRIGGER_URL` / `UC02_UC05_TRIGGER_URL` (https) with header `X-UC02-Signature`
(HMAC-SHA256 of the exact body, secret `UC02_UC0x_TRIGGER_SECRET`). `trigger_id` is the idempotency key; delivery is at least
once, so a repeated `trigger_id` must be answered 2xx without redoing the work. 2xx = accepted (not completed). Completion is
reported later to `/v1/downstream/status`. No URL is assumed: without both settings that use case has no adapter and nothing is
triggered.

## 5. Environment variables

| Variable | Needed for |
|---|---|
| `UC02_STORE_MODE=supabase`, `NEXT_PUBLIC_SUPABASE_URL` or `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | persistence and the read-only employee source |
| `UC02_APPROVAL_CALLBACK_SECRET` | the approval and message-decision routes (required to start the flow) |
| `UC02_EVENT_SECRET`, `UC02_DOCUMENTS_SECRET`, `UC02_UC04_STATUS_SECRET`, `UC02_UC05_STATUS_SECRET` | signed inbound channels |
| `UC02_UC04_TRIGGER_URL/SECRET`, `UC02_UC05_TRIGGER_URL/SECRET` | outbound triggers |
| `UC02_OUTLOOK_CONNECTOR=graph`, `UC02_GRAPH_TENANT_ID`, `UC02_GRAPH_CLIENT_ID`, `UC02_GRAPH_CLIENT_SECRET`, `UC02_GRAPH_MAILBOX`, `UC02_RECIPIENTS_FILE` | real Outlook (see UC02_OUTLOOK_GRAPH_SETUP.md); `graph` never falls back to the mock |
| `UC02_APPROVAL_CALLBACK_URL`, `UC02_COORDINATOR_ID` | HR email text, coordinator identity |

Secrets come from a secret manager into the environment; none is committed or logged.

## 6. Recipient mapping

JSON file at `UC02_RECIPIENTS_FILE`: per client, `roles` (HR review email and notices), `teams` (one entry per approved team label,
used for assignment emails) and `allow_role_fallback` (default false). A team with no entry is refused unless the fallback is
explicitly allowed, and even then is refused while two teams in the plan share the owner role: the gap is reported and nothing is
sent. See `connectors/outlook/recipients.py`.

## 7. Microsoft 365 activation checklist

1. Entra: single-tenant app registration; record tenant id and client id.
2. Graph application permissions `Mail.ReadWrite` and `Mail.Send`; admin consent.
3. Restrict the app to the one HR mailbox (Exchange Online RBAC for Applications, or an Application Access Policy); confirm
   403 for any other mailbox. Confirm the current mechanism with your Microsoft 365 administrator.
4. Dedicated HR mailbox (test mailbox first); a client secret in the secret manager with a short expiry.
5. Approved recipient file (HR role entries; one entry per approved team). Use a sink recipient you control for tests.
6. Set the environment (section 5). Do NOT put secrets in `.env.example` or chat.
7. Verify, in order (`demo/uc02_graph_integration_check.py`):
   - `python demo/uc02_graph_integration_check.py connection`   (read-only: token and Drafts folder read)
   - `python demo/uc02_graph_integration_check.py draft --client-id <test client> --role hr_officer`   (one marked test draft)
   - `python demo/uc02_graph_integration_check.py send --client-id <test client> --role hr_officer --confirm-send UC02-GRAPH-INTEGRATION-TEST-001`
8. Then a controlled end-to-end run on a development database and an approved test case
   (`demo/uc02_supabase_live.py`, `docs/UC02_SUPABASE_VERIFICATION_CHECKLIST.md`), with the portal replaced by signed test calls.

## 8. Multi-worker and crash behaviour

Local protections: a per-onboarding lock (process-local); a write-ahead intent recorded BEFORE every external send/trigger; the
Graph connector looks the idempotency key up in Drafts and Sent Items; triggers carry one stable `trigger_id`; message bodies are
stored with a fingerprint.
Limits: the lock does not span workers, and a crash after an external call but before its result is saved means the next attempt
re-sends. Both are safe only if the receiver deduplicates (Graph does by key lookup; UC-04/UC-05 must by `trigger_id`).

Proposed database-level claim (NOT applied; needs approval): a table `uc02_dispatch_claims(idempotency_key text primary key,
onboarding_id uuid references onboarding_cases(case_id), kind text not null, claimed_by text not null, claimed_at timestamptz not
null default now(), state text not null default 'claimed')`. A worker inserts the key; a duplicate-key error means another worker
holds it, so it does not send; a claim older than a TTL with no result may be taken over. UC-02 would call it from `_trigger_downstream`
and `send_message` before the external call. The existing `onboarding_status_updates` table (primary key `update_id`) could serve as an
interim claim store but with the wrong meaning and mandatory case/task columns; a dedicated table is cleaner.

## 9. Snapshot growth and privacy

Each state change appends a full `UC02_CASE_STATE` snapshot (append-only, enforced by the database). Unchanged state adds none, and
prepared messages are stored once. Growth is therefore proportional to the number of state changes, not time. A compaction scheme
(periodic checkpoint plus references) would need a delete or archive path the audit trigger forbids; it is not implemented.
Free text typed by people (HR reasons, delay and blocking reasons) is filtered by `privacy.redact_free_text` before storage; it cannot
recognise a name.

## 10. Known gaps that need other teams or approvals

Approved client configurations and recipient mappings; the approval portal; real UC-04/UC-05 endpoints and service identities;
the orchestrator calling UC-02 through the event route (today the shared orchestrator uses its own in-process adapter, so a
deployment must route each employee through ONE of the two paths; both are idempotent per employee on the same store);
delivery evidence for email; the dispatch-claim table.


## 11. Supabase-only runtime and the synthetic demo

* Every agent workflow runtime (`runtime.build_runtime_service`, `build_flow`, the API's sub-agent, the demo) calls
  `require_supabase_runtime` first: `UC02_STORE_MODE` unset, `memory` or `sqlite` is a clear startup error, as are missing
  credentials, an unreachable database and an incomplete schema. `InMemoryTableClient` and SQLite remain only for unit tests that
  inject them (`api.reset_sub_agent()` and the older SQLite scenario scripts used by existing tests).
* The retired in-memory agent demos (`uc02_agentic_workflow.py`, `uc02_end_to_end.py`) now refuse to run.
* Authoritative schema: the SHARED public tables (`onboarding_cases`, `onboarding_tasks`, ...) that the other use cases use, through
  `SupabaseTableClient`. The separate `uc02` schema in `supabase/migrations/` was never applied and is not used.
* Synthetic fixture (`supabase_synthetic.py`, stages in `sub_agent/supabase_demo.py`, CLI `demo/uc02_supabase_agentic_workflow.py`):
  marked `data_origin = synthetic`, written only by the `setup` stage after `--confirm-write <host>` and the development opt-ins,
  insert-if-missing, and rejected by any runtime that did not opt in. It includes UC-01-owned rows (case, offer, form) because a
  made-up employee has no UC-01 to create them; a real deployment never has UC-02 write those.
* PROPOSED convention awaiting the configuration owners' approval: the team mapping is read from `client_service_policies`
  (`service_category = uc02_assignment`, `service_key` = task category, `policy_details` = team, use_case, mode, escalation_owner), because
  the schema has no team-mapping column. Without such rows every category is an assignment gap.
* Run order: `preflight` (read-only) -> `setup --confirm-write <host>` -> `run` -> `decide` -> `send` -> `documents` -> `verify`
  (read-only) -> `recover`. Add `--mock-downstream` for labelled mock UC-04/UC-05 triggers; otherwise they are recorded NOT CONFIGURED.

## 12. Environment files, secrets and the PowerShell scripts

One loader (`envfile.py`) serves the demo CLI, the API and the setup scripts. Files are found from the UC-02 folder (where `pyproject.toml` lives), not the working directory. Precedence: process environment, then `.env.local`, then `.env`. A value already set in the process is never overwritten. `UC02_DISABLE_DOTENV=1` turns loading off (tests set it). `.env` / `.env.local` are gitignored; `.env.example` holds placeholders.

From PowerShell, in the UC-02 folder:

```powershell
.\scripts\setup_dev_environment.ps1      # once: writes UC02_STORE_MODE/UC02_ENV and creates only the MISSING secrets in .env.local
.\scripts\check_environment.ps1          # read-only report (names only); -Stage run|decide|send|documents|recover|full
python demo\uc02_supabase_agentic_workflow.py preflight
```

Setup never touches the database, never prints a secret, is idempotent (existing secrets are reused, never regenerated) and refuses unless `UC02_ENV=development`. Secrets: `UC02_APPROVAL_CALLBACK_SECRET`, `UC02_EVENT_SECRET`, `UC02_DOCUMENTS_SECRET`, `UC02_UC04_STATUS_SECRET`, `UC02_UC05_STATUS_SECRET`.

Per stage, the CLI requires: preflight/verify/setup - Supabase settings only; run/recover - callback + event secrets; decide/send - callback secret; documents - callback + documents secrets; `full` (check script) - all five.
