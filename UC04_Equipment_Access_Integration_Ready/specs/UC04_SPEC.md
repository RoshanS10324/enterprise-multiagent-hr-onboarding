# UC-04 Equipment & Approved Access Management: implementation specification

Status: local simulation, awaiting manager review. Written in our own words from two reference documents kept outside this project
(the Enterprise HR Agent introduction deck and the Onboarding Agent Business Framework v0.1). They are cited by section or ID only and are never copied here.

## 1. Purpose and boundary
UC-04 coordinates the laptop, equipment, accounts, licences and client-site access an accepted joiner needs by the joining date.
It checks each request against the approved role/client lists, routes it to the configured approver, tracks a simulated IT ticket, and
reports readiness, risks, owners and next actions.

- It is a **reusable capability inside the single Employee Onboarding Agent**, not a separate autonomous agent. The deck describes onboarding
  as a single agent for now (deck p.9), and the framework says the agent can also be used on its own (framework "At a glance").
- Autonomy is **L1** (draft for approval; people approve). The policy file enforces this and gates every write tool.
- **Local simulation only.** Nothing is sent, ordered, created, granted or provisioned. All ports are mocks. No LLM is used.

Out of scope: real IT service-desk requests, accounts, licences, dispatch, badges, e-mail/Teams, spend, HRIS/payroll, visa, banking,
document collection, policy Q&A, cohort orchestration, and any other onboarding stage (those belong to the parent workflow).

## 2. One implementation, two entry paths
```
Direct caller ----------> entrypoints/direct.py -------------+
                                                             +--> Uc04Service --> rules / approvals / items / evidence
Parent onboarding flow -> entrypoints/orchestrated.py -------+         |              risk / readiness / lifecycle / audit
                                                                       +--> mock ports (IT desk, parent case registry)
```
- Both entry points only build the request envelope and call `Uc04Service.evaluate` or `submit_eligible`. They import no rule module
  (enforced by test TC-36). `CallerContext.invocation_mode` is carried for correlation and audit and never influences a decision (TC-33, TC-35).
- UC-04 never creates an onboarding case. The case must already exist in the parent registry (`CaseContextPort`); unknown cases are rejected (BR-24, TC-34).
- Idempotency: results are cached per `(case, idempotency key, payload hash)`; request records are keyed by `case_id:request_key`, so
  replays and orchestrator retries never create duplicates (BR-14, BR-21). A stored result is replayed only while the case state AND the
  parent case status are unchanged: a replay never hides a parent that has since been put ON_HOLD or WITHDRAWN (BR-22, EC-20).
- A failed evaluation (privacy guard, unexpected error) restores the stored case state to what it was before the call and is audited as
  `state.rolled_back`, so a retry behaves as a first attempt and no false progress or lost IT notice remains (BR-18).
- Client change (EC-35): when the parent case moves to another client (and the new form version is approved, so the start gate passes),
  every stored client-site request of the old client is cancelled locally and archived. Anything already raised gets a DRAFT notice to IT
  for HR approval (`NOTIFY_IT_SUPERSEDED_REQUEST`, never executed; IT owns any follow-on action). The old requests stay visible in the result as
  superseded and cancelled and never count towards readiness. Their request keys cannot be reused (blocker
  `REQUEST_KEY_CANCELLED_BY_CLIENT_CHANGE`, owner account manager): the new client's client-site requests are raised under new keys and
  start fresh. Non-client-site items are not cancelled; their approvals are scope-checked as before, so an approval given under the old
  client is rejected as `STALE_SCOPE` and a ticketed item without a valid approval goes to a person.
- The orchestrator envelope version is checked: only `contract_version` "1.0" is accepted; any other value is refused before any work.
- The orchestrated envelope mirrors the contract verified in the reference repository (`hr_recruitment_common.contracts`: AgentRequest,
  WorkflowRef, Actor, AgentResponse, Proposal, AgentException). It is a local copy; at integration it is replaced by the real contract.

## 3. Inputs (validated, strict)
`Uc04Input`: `case_id`, `employee_ref`, `client_id`, `role_id`, `joining_date`, `offer_accepted`, `onboarding_form_approved`, `form_version`,
`work_location`, `work_mode` (OFFICE/REMOTE/OVERSEAS), `client_site_required`, `requested_access[]`, `requested_equipment[]`, `config`
(role access matrix, role equipment matrix, approver config, lead times, work calendar, handover rules, evidence roles, country rules,
client-site rules, client overrides), `approval_records[]`, `mock_it_ticket_updates[]`, `delivery_status[]`, `collection_status[]`,
`provisioning_status[]`, `client_site_clearance`, `unavailable_approver_roles[]`, `prior_equipment_held`, `internal_transfer`,
`display_label` (never an identifier, never echoed), `as_of_date`.
Rules: unknown fields rejected; enums and dates validated; flags are strict booleans; identifiers must match synthetic patterns
(`SYN-CASE-`, `SYN-EMP-`, `SYN-REQ-`, `SYN-USER-`, `SYN-EVD-`, `SYN-TKT-`) so real data cannot enter by accident (GR-13). The contract has no
field for pay, loan, bank, payroll, medical or passport data (GR-18; test_gr18).

**Configuration tri-state.** Each configuration block is `MISSING`, `VERIFIED_EMPTY` or `VERIFIED`. Missing never becomes an empty list or a
default; verified-empty is different from missing (nothing allowed, so every item is an exception).

## 4. Outputs
`Uc04Result`: `case_id` (the existing one), `implementation_mode = LOCAL_SIMULATION`, `overall_status`, `readiness_reason`, `items[]`
(request id, kind, item id, `in_matrix`, exception flag/reason, required approver, approval and exception state, item state, ticket id/status,
provisioning/delivery/collection state, due date, lead days, lead-time source, evidence refs), `blockers[]`, `risks[]`, `proposals[]`,
`cancellation_drafts[]`, `rejected_approvals[]`, `duplicates_ignored[]`, `next_actions[]` (each with an owner), `reassessment`, `remote_plan`,
`config_version`, `config_gaps[]`, `clearance_status`, `caller`, `audit_events[]`. Proposals always have `executed = false`.

## 5. Item lifecycle (BR-07). Names are a local proposal; the sources define none.
`DRAFT_PREPARED` -> `APPROVAL_PENDING` -> (`EXCEPTION_PENDING`) -> `APPROVAL_RECEIVED` / `EXCEPTION_RECEIVED` -> `ELIGIBLE_FOR_SIMULATED_SUBMISSION`
-> `MOCK_TICKET_CREATED` -> `IT_IN_PROGRESS` -> (`REJECTED_CORRECTION_REQUIRED`) -> `PROVISIONING_CONFIRMED` (access) or `EQUIPMENT_DELIVERED`
-> `EQUIPMENT_COLLECTED` (only when the client rule is COLLECT) -> `CLIENT_SITE_CLEARANCE_CONFIRMED` (client-site items) -> `ITEM_COMPLETED`;
plus `BLOCKED` and `CANCELLED`. Only legal moves are allowed; a backward move (for example withdrawn completion evidence) is refused, the
stored state is kept and a review blocker is raised. Approval, a created ticket, a completed ticket, delivery and collection are separate facts.

## 6. Case status (local proposal)
Precedence: `CANCELLED` > `BLOCKED` (any blocking blocker, such as the start gate or a configuration gap) > `NEEDS_HUMAN_REVIEW` (any review
blocker) > `AT_RISK` (any risk) > `AWAITING_APPROVAL` > `READY` (every active item completed, client-site clearance confirmed when required) >
`IN_PROGRESS`. READY is reachable only from authoritative completion evidence.

## 7. Rules implemented (BR-01 to BR-24)
See TRACEABILITY.md. In short: start gate; client/role/location configuration selected by scope and never mixed; missing or invalid
configuration blocks the affected plan; matrix allowlists; out-of-matrix items need BOTH the standard approval and an explicit exception
approval; approvals count only when approver role, request id, item and approved scope (client, role, client-site flag) match and are not future dated;
delegate routing when the primary approver is unavailable, otherwise escalation to HR; IT rejections recorded with a sanitised reason and never
retried; delayed equipment gives a loan-device **proposal**; faulty equipment gives an exchange **draft** and is never assigned; client-site
prerequisites and clearance (block or flag as configured); remote/overseas rules add configured courier days; deduplication by id; fixed dates and explicit
lead times only; withdrawal or cancellation stops progress locally and drafts a notice to IT (`NOTIFY_IT_WITHDRAWAL`) for approval; UC-04 does not cancel, unassign, collect or revoke anything.

**Evidence rule (GR-07 analogue).** Provisioning, delivery, collection and clearance count only with a case-stored evidence reference asserted by a role
configured for that evidence type, and only after the ticket is complete. A rejected ticket never becomes active, delivered or complete.

## 8. Dates (BR-20, DL-07, AC-07)
Due date = joining date minus the configured lead time in **working days** from the configured calendar (weekend days and holidays). No default
calendar or lead time exists. Remote/overseas equipment adds the configured courier days. Risks: approval stalled past due, request not raised in time,
delivery expected after joining, joining date reached with open items, joining date on a non-working day (next working day only suggested). AC-07 "items allowed after joining" is not modelled: see section 12.

## 9. Audit and privacy (GR-09, BR-15)
Structured events with a fixed field list (`timestamp, case_id, request_id, rule_id, event_type, previous_status, new_status, reason, next_action, owner`),
an injected clock, sanitised reasons, and a scan of every result and event for sensitive content. Free text (item notes, IT rejection reasons) is
treated as untrusted data: it is never acted on, injection patterns are logged by label, sensitive text is replaced by a neutral "HR item" marker, and a final
guard fails closed if anything sensitive would still be returned.

## 10. Policy and ports
`policy/uc04_policy.json` mirrors the reference policy shape: autonomy L1 with ceiling L1, kill switch, allowed caller roles, tool allowlist, and
`it_service_desk.submit_request` as a write tool that requires approval. `ToolGateway` enforces the allowlist and the approval flag in code.
Ports: `ITServiceDeskPort` and `CaseContextPort` (mock adapters only). Adapter errors and timeouts fail closed: state is preserved, only the error class is recorded,
the request is not retried until a person acknowledges it.

## 11. Synthetic vocabulary and assumptions (labelled, not from the sources)
Role names (`hiring_manager`, `exception_approver`, `it_service_desk`, `account_manager`, ...), ticket statuses, matrix contents, lead times, calendars,
clients `client_alpha` / `client_bravo`, and caller roles in the policy file are synthetic fixtures. Owner roles HR officer, hiring manager, IT service desk,
account manager and HR operations lead come from the onboarding framework; `UC04_SERVICE` and `OWNER_CONFIG_MISSING` are local markers.

## 12. Open questions (not answered by the sources; nothing was invented)
Framework OQ-01 to OQ-08 (form owner, visa vs background check, when onboarding ends, who edits client knowledge, Arabic at launch, extra channels,
owner of client-site profiles, target market) remain open. UC-04 additionally needs real values for: access and equipment lists per role and client;
approver and delegate per request class, and who approves exceptions; the IT ticket status vocabulary and lifecycle; the evidence that proves activation,
delivery, collection and clearance, and which role may assert each; lead times; the working-calendar and holiday source and timezone; client-site NDA,
badge and clearance rules; courier and country rules; the state names the wider onboarding application expects; what "HC-08" (spend approval) means;
whether a corrected request after an IT rejection should reuse or replace the rejected one; the current project milestone and approved stack.

Pre-integration review (2026-10-27) added these questions for the business owner (none is answered by the two source documents; none is implemented):
- **HC-08** is mentioned once, in GR-20 ("approval at HC-08" before any spend), and defined nowhere. It is **not implemented**. UC-04 raises
  no spend-committing request and only proposes workarounds, which a person approves; a test pins that down (tests/test_integration_boundaries.py).
  Needed: what HC-08 is, who approves at it, and whether UC-04 is ever expected to raise a spend request (for example a loan device).
- **AC-07 "items allowed after joining":** the Framework allows completion after joining only for documents (EC-08 degree attestation; the client
  example "collect education certificates only after day one"). For IT, equipment and access it says "must all be ready by day one". No source
  defines an equipment or access item that may complete after joining, so none is modelled and open items at or after the joining date are
  flagged. Needed: does any client allow specific equipment or access to complete after joining, and how is that configured?
- **EC-35 scope:** a change of `client_id`, and a change of the optional `project_id` (section 16), are both detected. `project_id` is PROPOSED:
  still needed is confirmation of its source (the live `onboarding_cases.project_name` is the likely one) and whether a new form version is
  required to differ from the old one.
- **Separation of duties:** may a requester (`requested_by`) approve their own request? The sources are silent; nothing is enforced.
- **Evidence ordering:** delivery, collection and provisioning records carry no timestamp, so the last record listed for a request wins
  (this allows a faulty-then-replacement delivery). Needed: whether the parent supplies evidence timestamps.
- **Withdrawn requests:** how does the parent say that a single request was withdrawn (as opposed to a whole case)? An item missing from a later
  payload stays in the store unreported.
- **Orchestrator envelope:** `workflow.id` and `workflow.step` are accepted unvalidated because the real contract is not available.

## 13. Known limitations
Local, in-memory, single process (the local API serialises requests; the service itself is not thread-safe). Items dropped from a later payload remain in the store (not reported).
The correction flow after an IT rejection uses a new request key and supersedes the rejected item (a local design choice). Conflict detection between the form and the offer (DL-02)
is not implemented here. Approvals, evidence and caller roles are supplied by the caller and cannot be authenticated, and `as_of_date` is caller-supplied (it is never read from a clock, which keeps
decisions deterministic, but it also means a caller can choose it). Ticket status is never polled: it arrives as input. A desk failure sets an `ADAPTER_ERROR` blocker that only an
acknowledgment clears (`Uc04Service.acknowledge_adapter_error`, or `acknowledge_orchestrated` through the parent; a submission whose outcome is unknown is held as `SUBMISSION_IN_DOUBT` and never retried blindly).
Persistence is behind ports (`src/sub_agent/persistence/`); the default wiring is in memory, and the optional Supabase connector (`integrations/supabase/`) is off by default and has not been run against UC-04-owned tables, which do not exist yet. No concurrency control, authentication, tracing or live integrations. Nothing here proves production readiness.
Resolved in the 2026-10-27 review: a replay no longer ignores a parent status change, a failed evaluation no longer leaves half-applied state, and EC-35 client-change handling is implemented.

## 14. Future integration points (not built)
LangGraph/`BaseAgent` adapter and a production FastAPI route (reference `hr_recruitment_common`; a local development API with Swagger already exists in `src/sub_agent/api`, see docs/LOCAL_API.md; it is not production-ready). Boundaries for each integration: docs/INTEGRATION_BOUNDARIES.md. MCP server behind API Management for the IT service desk; Cosmos DB case state and audit;
approval routing (Logic Apps / Teams); Entra Agent ID; OpenTelemetry/Application Insights; evaluation in CI; Enterprise HR Agent package folders
(`brain/ skills/ mcp/ knowledge/ context_layer/ hooks/ monitoring/ optimization/ cicd/`, `Dockerfile`, `uv.lock`).
`policy/` already holds the policy file; `guardrails/` content is currently `src/sub_agent/guardrails.py` and `hooks/` behaviour lives in `approvals.py`, `audit.py` and `policy.ToolGateway`.

## 15. Confirmed scope and recent changes (verified against the code)
- **Day-1 scope:** standard equipment and approved basic access, from the client configuration that UC02 supplies. The IT team, not UC-04, decides the laptop model, specification and asset, and performs the allocation. UC-04 requests, tracks and reports status to the parent orchestrator. HR remains the authority for case closure. Project-specific subscriptions, database permissions and advanced developer tools are out of scope.
- **Withdrawal:** UC-04 stops local progress and drafts a notice to IT for HR approval (`NOTIFY_IT_WITHDRAWAL`; `NOTIFY_IT_SUPERSEDED_REQUEST` for a client change). It does not cancel, unassign, collect or revoke anything. The result field keeps its name `cancellation_drafts` for contract stability.
- **Configuration:** it arrives with the request; UC-04 reads no path and no database for it. Every result carries `config_version` and `config_digest` (a content digest). A change after the first evaluation triggers a reassessment (`config_version` or `config_digest` in `changed_fields`) and a next action for HR to re-check approvals; nothing is changed silently. Still open: who verifies the config against a stored version or hash (D-2), and the exact supply key from UC02 (D-1).
- **UC-02 onboarding plan link (orchestrator side, `HR-Onboarding-Common/.../adapters/uc04_uc02_plan.py`):** before the orchestrator calls UC-04, it reads the case's UC-02 plan from UC-02's own service (`OnboardingService.get`, read-only, under UC-02's client isolation) and uses it only when its onboarding id, case id, client id and employee id are all the case's. Otherwise the stage is blocked without calling UC-04: `UC02_PLAN_MISSING`, `UC02_PLAN_CASE_MISMATCH` (another case's plan is never attached, even for the same employee number), `UC02_PLAN_UNAVAILABLE` (retryable). The plan's `it_provisioning`, `access_management` and `workplace` tasks (identifiers, category, owner role, status, due date; `workplace` for traceability only) and its plan version, status and configuration version are recorded on the stage result as `uc02_plan`. Two comparisons are advisory and never change the stage: `UC02_CONFIG_VERSION_MISMATCH` and `UC02_DUE_BEFORE_UC04_LEAD_TIME` (a task due before UC-04's earliest lead-time date; no task-to-item mapping exists), plus `UC02_PLAN_NO_SERVICE_TASKS`. UC-04's core contract (`Uc04Input`) and its item configuration are unchanged, so D-1 stays open.
- **Advisory providers:** `AdvisorPort` with `FakeLlmAdvisor` (default, offline, deterministic) and `LlmAdvisor` over an injected `ModelPort` (the shared LiteLLM gateway, supplied from outside; UC-04 imports no model library and reads no environment). `build_advisor(provider, model=...)` is the only place a provider is chosen; every report names its provider. The model text goes only into `narrative`, after the privacy guard; every decision field comes from the deterministic template. A failure reports the error class and never changes a decision; fallback to the template happens only when configured explicitly and is then reported (`fallback_from`). Every report that could not get its model text carries `degraded=true` and `degraded_reason` (an error class name). The shared-gateway wiring (`integrations/gateway/`, task `uc04.advisory`, tier WORKHORSE, one-field schema, pattern from UC05 on `origin/Prasanna`) is built and tested offline; no live gateway call was made, and orchestrated runs do not call the advisor (see `docs/INTEGRATION_BOUNDARIES.md`).
- **Not built:** a production IT Service Desk adapter (needs an IT-approved API or shared MCP contract and the asset id, model and technician-reference response fields) or any project-request flow. A database client exists only as the optional, off-by-default connector in `integrations/supabase/`; its tables (`docs/proposed_migrations/uc04_state_tables.sql`) are not applied. HR approves or rejects proposals manually outside UC-04; UC-04 reads recorded approvals only. Common-side changes are proposals in `docs/common_patches/`.

## 16. Edge cases EC-29 to EC-36, EC-40 and EC-42: what UC-04 now prepares (verified by `tests/test_scenarios_complete.py`)
UC-04 detects, drafts and tracks. It sends, orders, assigns, reserves, books, removes and cancels nothing; IT or HR executes. Every draft is a `Proposal` with a structured `payload`, `requires_approval_by`, `executed` always false and `status` DRAFT_NOT_SENT.
- **EC-29 delay:** employee and manager message drafts (HR approval) plus the `LOAN_DEVICE` proposal with an IT request draft. Availability is `false` until IT confirms. The delay is detected from IT's `expected_date` or from a recorded `delivered_on`, when either is after the joining date (a date equal to the joining date is on time). A late delivery of an item that is still open raises `EQUIPMENT_DELAYED_BEYOND_JOINING` with the same drafts; once the item is complete, it stays on the audit trail (`delivery.after_joining`) and does not turn a finished case `AT_RISK`. Decision for the business owner: whether a completed-but-late delivery should also change the case status.
- **EC-30 IT rejection:** a manager message draft that quotes IT's recorded reason as data and asks for corrected details. The rejected request is never retried; a corrected request needs a new request key.
- **EC-31:** unchanged and complete; the exception approver role comes from client configuration. The real role names (HC-08) are still unconfirmed.
- **EC-32 missed slot:** employee and manager reminder drafts, `REQUEST_COLLECTION_SLOT` (confirmed slot stays null until IT confirms) and `RETAIN_RESERVATION_REQUEST` when the client rule keeps the device reserved.
- **EC-33 faulty or wrong model:** a structured `EXCHANGE_REQUEST_DRAFT` (employee and case reference, expected item, reported status, asset and model details, fault note as data). A delivered item whose reported model differs from the model IT assigned is not recorded as delivered. The optional record fields `asset_ref`, `assigned_model`, `reported_model`, `fault_note` are PROPOSED and pending the IT contract.
- **EC-34:** optional `deployment_date` replaces the joining date as the clearance target; the proposal carries both dates and the account manager role.
- **EC-35:** optional `project_id` (PROPOSED; the live `onboarding_cases.project_name` is the likely source). A change from one project to another cancels the old client-site requests like a client change and drafts notices to IT; a first assignment is a reassessment only. `reassessment` keeps what the case was assessed against before a change (`previous_client_id`, `previous_project_id`, `previous_config_version`, `previous_config_digest`; each only for a field that changed), so the earlier configuration stays on record.
- **EC-36:** optional country calendar and IANA timezone inside `country_rules` (configuration, nothing hard-coded). The country's working days and holidays apply to the equipment lead time and to the joining-day check; `as_of_utc` becomes the local date; a `COURIER_REQUEST_DRAFT` and a remote first-day plan are prepared. Missing country rules remain an explicit gap.
- **EC-40 cohort:** each person keeps a separate case. `Uc04Agent.evaluate_cohort` returns a `CohortView` (per-person rows, status counts, delayed or blocked, failed cases). `plan_slots` recommends collection slots within the capacity IT reports, never books a request twice and never exceeds capacity; a booking is recorded only through `confirm` after IT confirms. UC-05 keeps orientation.
- **EC-42 internal transfer:** optional `transfer` (an existing employee, a distinct `transfer_ref`, held access and equipment, IT confirmations). The plan keeps, requests, removes and replaces against the new role's configuration; removals and equipment decisions are proposals and count only after IT confirms. No case or identity is created, no joining documents are generated, and a cross-client transfer goes to HR.
- **EC-41 (agency contractor) is not part of this work.**

### Live schema observations (read-only metadata, 2026-11-03)
The live project now has 38 tables (earlier audit: 32). `onboarding_cases` has `project_name`, `current_form_version`, `variant_is_remote`, `variant_is_internal_transfer`, `grade`; `client_config_versions` holds version, `config_hash`, status and effective dates; `client_service_policies` holds per-client service policy; `role_access_matrix`, `provisioning_*` and `equipment_handovers` hold no rows. `onboarding_cases` also holds candidate name and email, so UC-04 must always select named columns. No UC-04 write to the live project has been made.

### State storage decision (2026-11-03)
UC-04 uses no SQLite. Its persistent store is Supabase, in UC-04-owned tables (`uc04_case_state`, `uc04_replay_results`), behind `StateBackend` (`TableStateBackend`, injected `TableClient`). The tables are a proposal: the migration is unapplied, so UC-04 state is still held in memory at runtime until the migration and the Common wiring (docs/common_patches, Part C) are approved. Writes use compare-and-swap on the case version; the replay results are written after the state, so a failure between them loses only a replay entry. A later move to the production database changes only the injected client and the migration target.
