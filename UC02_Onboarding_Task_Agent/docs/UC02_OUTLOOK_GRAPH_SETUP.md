# UC-02 Outlook (Microsoft Graph) connector: setup and operation

**Status:** the connector (`sub_agent/connectors/outlook/graph.py`) is implemented and tested against *mocked* Graph and
Entra responses only. It has never contacted Microsoft. Nothing below has been run against a real tenant. Treat every
Microsoft-side step as something to confirm with your Microsoft 365 / Entra administrator, because portal wording and
recommended practice change.

## What the connector does

| Operation | Graph call | Result reported to UC-02 |
|---|---|---|
| `create_draft(message)` | `POST /users/{mailbox}/messages` | `draft_created` (nothing sent) |
| `send(message)` (needs `approval_id`) | `POST /users/{mailbox}/messages/{id}/send` | `submitted_for_sending` = Graph returned 202 Accepted |
| repeat of either | lookup in Drafts / Sent Items by idempotency key | `duplicate_ignored` |

`submitted_for_sending` is **not** delivery. UC-02 never records "delivered": there is no delivery evidence source (see
"Not implemented").

Recipients are never in code or in a request. A message carries a role (for example `hr_officer`); a
`RecipientDirectory` built from approved configuration turns `(client_id, role)` into addresses. A role with no approved
address makes the connector refuse before any network call.

## 1. Microsoft Entra app registration

1. Entra admin center > **App registrations** > **New registration**. Single tenant. No redirect URI (it uses the
   client-credentials flow, no user sign-in).
2. Record the **Directory (tenant) ID** and **Application (client) ID**.
3. **API permissions** > Add > Microsoft Graph > **Application permissions**:
   - `Mail.ReadWrite` : create the draft and look drafts / Sent Items up by key.
   - `Mail.Send` : send the approved draft.
   Then **Grant admin consent**. No delegated permissions, and nothing else (no `User.Read.All`, no calendar, no files).
4. **Certificates & secrets** > New client secret with a short expiry. Copy the value once, into your secret manager.
   (Certificate credentials and managed identity are better; this connector currently supports a client secret only.)

## 2. Limit the app to the HR mailbox (important)

Application permissions `Mail.ReadWrite` and `Mail.Send` apply to **every mailbox in the tenant** until you restrict
them. Before using this connector, restrict the app registration to the single HR mailbox using Exchange Online's
application access controls (RBAC for Applications, or the legacy Application Access Policy with a mail-enabled security
group that contains only that mailbox). Verify the restriction by confirming the app gets **403** for any other mailbox.
Ask your Microsoft 365 administrator which mechanism your tenant should use.

## 3. Mailbox

Use a dedicated mailbox for UC-02 HR review mail (a shared mailbox is usually enough). Its address is
`UC02_GRAPH_MAILBOX`; drafts are created in, and sent from, that mailbox. Use a test mailbox first.

## 4. Environment variables (server side only)

| Variable | Meaning |
|---|---|
| `UC02_OUTLOOK_CONNECTOR` | `mock` (default, sends nothing) or `graph`. `graph` never falls back to the mock. |
| `UC02_GRAPH_TENANT_ID` | Directory (tenant) ID |
| `UC02_GRAPH_CLIENT_ID` | Application (client) ID |
| `UC02_GRAPH_CLIENT_SECRET` | The client secret value. From a secret store; never in git. |
| `UC02_GRAPH_MAILBOX` | The HR mailbox address |
| `UC02_RECIPIENTS_FILE` | Path to the approved recipient mapping (below) |
| `UC02_GRAPH_AUTHORITY`, `UC02_GRAPH_BASE_URL` | Optional, for sovereign clouds. Must be https on a Microsoft host. |

Approved recipient mapping (deployment-owned JSON; never taken from a request):

```json
{ "<client_id>": {
    "roles": { "hr_officer": ["address-approved-for-this-client@example.invalid"] },
    "teams": { "<approved team label>": ["team-address@example.invalid"] },
    "allow_role_fallback": false } }
```

`roles` is used for the HR review email and HR notices; `teams` for team assignment emails (one entry per approved team). A team
with no entry is refused unless `allow_role_fallback` is true, and is refused regardless while two teams in the plan share the
owner role. The older flat format (`{"<client_id>": {"<role>": [...]}}`) is read as role-level addresses only.

Where approved recipients should live long term (for example the client service policy) is a decision for the
configuration owners; no such field exists in the schema today and none is assumed.

## 5. Using it in the HR email workflow

```python
from onboarding_task_management.sub_agent.connectors.outlook.factory import build_outlook_connector
connector = build_outlook_connector()                      # reads the environment
email = flow.prepare_hr_email(coordinator, onboarding_id, as_of)
flow.save_draft_message(coordinator, onboarding_id, "hrmail", email.email_id, email.subject, email.body,
                        email.recipient_role, connector)   # draft appears in the HR mailbox; nothing is sent
flow.authorise_message(hr_lead, onboarding_id, "hrmail", email.email_id)   # a DIFFERENT HR lead
flow.send_message(coordinator, onboarding_id, "hrmail", email.email_id, email.subject, email.body,
                  email.recipient_role, connector)         # submitted for sending; recorded as such
```

The subject and body must equal what was prepared (a fingerprint is stored and checked), so what HR reviewed is what is
saved or sent. Each step is recorded in the audit trail (`hrmail.prepared`, `hrmail.draft_saved`, `hrmail.authorised`,
`hrmail.result`) with `real=True` only for a real connector.

## 6. Safety behaviour (all covered by tests with mocked Graph)

* Approval gate: `send` without `approval_id` returns `blocked_no_approval` and makes no network call.
* Idempotency: the idempotency key is stored on the draft as an Outlook extended property and looked up in Drafts and Sent
  Items before creating or sending, so retries, restarts and second processes cannot duplicate. A lost response followed
  by a retry is recognised as already sent.
* Errors: 401 refreshes the token once; 429/502/503/504 and network errors retry with capped backoff honouring
  `Retry-After` (3 attempts); 403 and other 4xx are permanent. Errors carry only a status and Graph's error code.
* Logging: operation, status, attempt and the Graph `request-id` only. Never tokens, secrets, addresses, subjects, bodies,
  tenant or client ids. The token lives only in memory.
* Hosts: https on Microsoft hosts only; redirects are never followed.

## 7. Verification plan with a real test tenant (not done)

1. Use a test tenant/mailbox and a sink recipient you control. Set `UC02_OUTLOOK_CONNECTOR=graph`.
2. Run only `save_draft_message` first; confirm the draft appears in the test mailbox and that re-running creates no second.
3. Confirm the app is refused (403) for a mailbox outside the restriction.
4. Authorise and `send_message` to the sink; confirm one message in Sent Items and that re-running sends nothing.
5. Expire the secret deliberately and confirm the failure is reported without leaking it.

## 8. Not implemented / limits

* **Delivery evidence.** Graph's 202 only says the message was accepted. Confirming delivery needs a separate signal
  (message trace, read receipt, or bounce handling). None exists here, so UC-02 cannot report "delivered".
* Certificate or managed-identity authentication; secret rotation tooling.
* Reading Sent Items beyond the idempotency lookup; attachments; HTML bodies (bodies are plain text).
* `outbox()` lists only what this process submitted; it is not a mailbox read.
* Shared HR approval link / callback endpoint (a separate item).
