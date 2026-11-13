# AI Onboarding Control Center — implementation report

Date: 2026-11-04. Scope: connect `onboarding-ui-prototype` to the real central orchestrator
(`HR-Onboarding-Common/orchestration`) for UC01–UC05, Supabase Storage document uploads, and live workflow state.
Nothing was committed.

**Summary.** The UI now drives the real orchestrator end to end. The orchestrator runs every transition, and the
browser only reads state and sends commands. The full workflow passes live in **demo mode** (real orchestrator and
UC03, UC stand-ins elsewhere) with real Supabase Storage. In **real mode**, the real UC01 → UC02 → UC03 path runs
through the mandatory-document gate for a tenant UC02 is configured for. The parallel branches then stop on
configuration and data gaps that predate this change (§13). Three backend defects were found and fixed along the
way (§12.4).

## 1. Frontend architecture

| Before | After |
|---|---|
| `Dashboard.tsx` showed a hard-coded `nodeDetails` map (Sarah Chen, Dubai…) for every node | `StageSidebar.tsx` renders the selected stage from the persisted case state |
| Any API failure silently swapped in `mockCases` ("Backend unreachable — showing synthetic demonstration data") | No mock fallback. Online/Offline from `/healthz`, Retry, last-known state marked **stale** |
| Diagram keys (`uc01`, `task_planning`, `document_validation`, `day1_readiness`) matched no backend stage, so statuses never coloured it. It still showed a "Branch: Tasks" fan-out | `WorkflowDiagram.tsx` uses the real stage keys and graph: UC01 → UC02 → UC03 collection → await documents → UC03 validation ↔ UC02 replanning → gate → UC04 ‖ UC05 → merge → Day-1 signoff → joining day → welcome |
| `OfferLetterUploadModal` / `DocumentUploadPanel` simulated parsing and validation with timers | Removed. Replaced by `StartOnboardingModal.tsx` (real UC01 intake) and `DocumentChecklist.tsx` (real uploads) |
| Called `GET /v1/orchestration/cases`, which did not exist; backend had no CORS | List route added; CORS configured on the server (the two causes of the "Offline" banner) |
| Prototype pages on mock data at `/cases`, `/agents/*`, `/monitor` | Moved under `/demo/*` (each already labelled synthetic). `/cases/:id` now opens the live case on the dashboard |

Visual style is unchanged: same cards, node borders and status dots, arrows, dashed wait nodes and sidebar header.
The selected case is in the URL (`/?case=<id>`), so a refresh reloads the persisted case and never starts a new one.

## 2. Backend architecture (as found, and what was added)

- `OnboardingOrchestratorAgent` (LangGraph) owns all transitions. UC01/UC04/UC05 run as worker processes; UC02 and
  UC03 run in-process. Case state is written through after every stage to a `CaseStateStore`: memory, SQLite, or
  Supabase `case_orchestration_state` with optimistic concurrency.
- Starting a case is already idempotent per tenant + offer reference. UC03 → UC02 replanning, plan revisions,
  stale-result reconciliation, the parallel fan-out and merge, and the Day-1 HITL gate all already existed.
- **Added:** a server entry point (`scripts/serve_orchestrator.py`), case list and profile routes, document
  upload and signed-URL routes (`orchestration/documents.py`), and per-tenant UC04 configuration.

## 3. API integration mapping

All routes are under `/v1/orchestration`. Identity comes from `X-User-Id`, `X-Client-Id` and `X-User-Roles`
(development header trust only; see §13). The tenant is always the verified actor's `client_id`, and another
tenant's case answers 404. Errors: 401 identity, 403 role/kill switch, 404 not found, 409 wrong state, 413/415
file, 422 validation, 503 storage not configured.

| Operation | Route | New? | Role setting (policy.yaml) | Idempotency |
|---|---|---|---|---|
| Create onboarding (UC01) | `POST /cases` | existing (+`grade`, `nationality_group`) | `case_operator_roles` | per tenant + offer reference |
| List cases | `GET /cases` | **new** | `case_reader_roles`; name only with `case_identity_reader_roles` | read |
| Case + workflow state | `GET /cases/{id}` | existing | `case_reader_roles` | read |
| Intake profile (UC01 input) | `GET /cases/{id}/profile` | **new** | as list | read |
| Stage executions / dependencies | `GET /cases/{id}/stages` | existing | `case_reader_roles` | read |
| Upload document | `POST /cases/{id}/documents/{type}/upload` (raw body) | **new** | `document_submitter_roles` (checked before storing) | content-addressed: same file → same object, no new revision |
| View document | `GET /cases/{id}/documents/{type}/url` | **new** | `document_viewer_roles` (**new**) | 60 s signed URL |
| HR document review | `POST /cases/{id}/documents/reviews` | existing | `document_review_roles` | already-decided → 409 |
| Approve / reject | `POST /cases/{id}/approvals/{aid}/decision` | existing | owning inbox checks approver role | already-decided → 409 |
| Retry stage | `POST /cases/{id}/stages/{stage}/retry` | existing | `stage_retry_roles` | technical failures only |
| Resume | `POST /cases/{id}/resume` | existing | `case_operator_roles` | resumes from the committed state |
| Health | `GET /healthz` | extended | none | reports mode, store, bucket, limits, extraction engine |

## 4. UC01–UC05 UI integration status

| UC | UI | Verified against |
|---|---|---|
| UC01 | Internal form (sections A–D, field validation, confirmation, single in-flight submit) → real intake; sidebar shows submitted details and output (case id, employee no., joining date and revision, intake blockers, store and version) | demo live E2E; **real UC01 worker** (`EMP-000001`, UUID case id) |
| UC02 | Sidebar: plan id and revision, required documents, task table (owner, due date, status, dependencies), revision history; dependency panel shows it consumed UC01's persisted result | demo; **real UC02** for `CLIENT-ALPHA` (9 tasks). Real UC02 has no config for `CLIENT-TEST-A` (§13) |
| UC03 | Checklist for the current plan revision: mandatory flag, due date, upload status, validation classification and findings, corrections, stale-revision marker, Upload/Replace, View (signed URL), HR Approve/Reject | demo; **real UC03** in both modes |
| UC03 → UC02 | Replanning node shows when it ran; plan revision shown on the diagram and in the checklist | demo live E2E (plan v1 → v2 on `visa_required`); an expired passport stays a UC03 correction |
| UC04 ‖ UC05 | Branch statuses, separate outcomes, independent failure (UC04 blocked while UC05 completed), merge → Day-1 signoff | demo live E2E; real mode reaches both branches and stops on config/data gaps (§13) |

## 5. Supabase table mapping (what is actually written)

| Table | Written by this change? | Detail |
|---|---|---|
| `case_orchestration_state` | Only with `ORCHESTRATOR_STATE_STORE=supabase` (existing store, unchanged) | Its FK needs an `onboarding_cases` row. UC01's Supabase writes are paused (B1–B9), so UC01-opened cases cannot be stored there. **The server defaults to SQLite**, which survives restarts. |
| `candidate_documents` | Yes, when the case has an `onboarding_cases` row in the same tenant | Otherwise skipped with `CASE_HAS_NO_ONBOARDING_CASES_ROW`, which is the case for every UC01 case today. The file is still stored and validated. Columns verified against the live schema; `status='RECEIVED'` uses UC03's vocabulary. |
| `document_validations` | **No** | The existing UC03 adapter keeps validation results in the case state only. Writing this table needs UC03's projection mapped to its 20+ NOT NULL columns, which is a UC03-owner decision. |
| UC01/UC02/UC04/UC05 business tables | Not by this change | Each UC owns its own persistence. Not verified here. |

## 6. Supabase Storage integration

- Bucket: **`Doc_Collection`** (existing, private, no bucket-level size or MIME limits). No bucket was created.
- Path: `<client_id>/<case_id>/<document_id>.<ext>`, UC01's documented convention (`storage_paths.py`), built only
  from server-controlled ids. `document_id = uuid5(tenant, case, type, sha256)`.
- Accepted types are UC03's own (PDF, JPEG, PNG, TIFF), checked by **file signature** as well as the declared type.
  Size limit `DOCUMENT_MAX_BYTES`, default 10 MiB. **This is a new value:** no limit existed anywhere.
- The service-role key lives only in the backend process. The browser never talks to Supabase. Objects are not
  publicly readable (verified: public URL → 400). Viewing uses a 60-second signed URL scoped to the case's prefix.

## 7. Document upload and retrieval flow

`browser → POST …/upload` → role and tenant check → plan-requirement check → type, signature and size checks →
Storage upload → `candidate_documents` row (or an explicit skip reason) → **download the stored object and verify
its SHA-256** → UC03's extractor on the downloaded bytes → `submit_documents` (UC03 stores the revision, validates,
and routes to correction, HR review, replanning, or the gate).

Extraction mapping is conservative: an injection-suspected result gets confidence 0. `attestation_confirmed`,
`visa_required` and sponsorship are never inferred. When no text was read at all, the LLM is not consulted (see
§12.4 #2).

## 8. Orchestration state synchronisation

Polling only; no Supabase Realtime is configured for the frontend. One request in flight per hook. The case
refreshes every 3 s while running, 8 s while waiting, 30 s when completed or failed. The list refreshes every 10 s
and health every 15 s. Polling pauses in background tabs and stops on unmount. Command responses (start, upload,
review, approve, retry, resume) are applied immediately. On a failed refresh the last state stays visible and is
marked stale.

## 9. Right-sidebar data mapping

| Section | Source |
|---|---|
| Header | stage metadata; `stage_statuses[stage]` mapped to a UI label and colour (stored value shown under Overview) |
| Overview | `stage_outcomes[stage]`: started/finished, duration, attempt, `execution_id` |
| Input | `stage_outcomes[stage].inputs`; UC01 adds `GET /profile`; `stages[stage].dependencies` gives the upstream source |
| Output | the stage's result field (`task_plan`/`onboarding_plan`, `document_collection`, `document_validation_feedback`, `equipment_access`, UC05 results…) plus raw JSON |
| Documents (UC03 stages) | `document_collection`, `documents`, `document_validation`, `correction_requirements` |
| Activity | `stage_history`, `stage_errors`, `blockers`, `retry_metadata`, approvals |
| Actions | only real routes, with the backend's rules: **Retry** for `retry_pending`/`failed`/technical blocks; **Resume** for business blocks; **Approve/Reject/Retry apply** for that stage's approvals |

Status colours: grey pending; blue running; green completed; yellow waiting, approval or review; red failed,
blocked, retry-pending or uncertain.

## 10. Completed UI functionality (verified in headless Edge against the live server)

1. Dashboard: Online and Demo-mode badges, case list (name, employee no., department and designation, joining
   date, current stage, status, pending/blocker counts, last updated), diagram coloured from persisted state, the
   replanning loop highlighted when it ran, "plan revision v2".
2. UC01 sidebar with the submitted form details and execution metadata. UC03 sidebar with the plan id/version it
   validated against.
3. Form: field-level errors in every section → confirmation → submit → the new case opens.
4. UC03 panel: a real upload through the file input → receipt (stored object, metadata-row reason, extraction
   result and engine warning) → Replace and View.

## 11. Files created and modified

**Backend (`HR-Onboarding-Common`)**

- New: `src/hr_onboarding_common/orchestration/documents.py`, `scripts/serve_orchestrator.py`,
  `tests/orchestration/test_control_center_api.py` (25 tests).
- Modified:
  - `orchestration/api.py`: routes, error mapping, CORS, health.
  - `orchestration/orchestrator_agent.py`: `designation` and `work_location_type` kept in the intake trigger.
  - `orchestration/adapters/uc03_documents.py`: checkpointer fix.
  - `orchestration/adapters/uc04_equipment_access.py`: tenant config fallback.
  - `orchestration/tenant_config.py`: `uc04_client_config_ref`.
  - `orchestration/workers/config.py`: storage key withheld from workers.
  - `guardrails/policy.yaml`: `case_identity_reader_roles`, `document_viewer_roles`.
  - `config/tenants.yaml`: `CLIENT-TEST-A` → UC04 alpha config, the pairing the demo and tests already use.
  - `.env.example`.
- Installed into the backend venv: `pymupdf==1.25.5`. UC03's extractor imports it optionally. Note its AGPL license.

**Frontend (`onboarding-ui-prototype/src`)**

- New: `components/StartOnboardingModal.tsx`, `components/StageSidebar.tsx`, `components/DocumentChecklist.tsx`,
  `components/CaseList.tsx`, `lib/stages.ts`.
- Rewritten: `api/apiClient.ts`, `api/onboardingApi.ts`, `api/useOnboardingData.ts`, `components/WorkflowDiagram.tsx`,
  `pages/Dashboard.tsx`, `App.tsx`.
- Modified: `vite-env.d.ts`, `pages/CaseWorkflow.tsx`, `pages/OnboardingCases.tsx`, `pages/WorkflowMonitor.tsx`
  (demo links).
- Deleted: `components/OfferLetterUploadModal.tsx`, `components/DocumentUploadPanel.tsx` (simulations).

## 12. Tests executed and results

### 12.1 Automated

- Backend `pytest`: **663 passed, 2 skipped**. The baseline before this change was 638 passed, 1 skipped. The
  skips are the opt-in live-Supabase test and the real-PyMuPDF test (environment, §13). `ruff` is clean.
- Frontend: `tsc --noEmit` and `vite build` are clean. There is no frontend test runner in the project; none was
  added.

### 12.2 Live E2E, demo mode, real Supabase Storage: 30/30 checks

CORS; start → UC01 → UC02 → UC03 collection; UC02 consumed UC01's persisted result; duplicate submission → same
case; list and tenant isolation; upload stored with identical bytes at the tenant path, not public; idempotent
re-upload; `hr_ops` and other-tenant upload refused; wrong type → 415; signed URL serves the file and is refused
cross-tenant or without the role; correction (expired passport) without replanning; replanning v1 → v2 and
collection on v2; `hr_ops` review refused; gate passed; UC04 ‖ UC05 completed with separate outcomes; merge → Day-1
signoff.

**Restart recovery:** the server was restarted on the same state store. The case was intact, a business block was
correctly refused a *retry*, and *Resume* completed UC04, kept UC05's result, signed off and reached `completed`
after the joining-day event.

### 12.3 Real mode (real UC01/UC04/UC05 workers, real UC02 and UC03)

| Tenant | Result |
|---|---|
| `CLIENT-TEST-A` | UC01 ✓ (`EMP-000002`) → UC02 `MISSING_DATA`: no UC02 configuration for this client |
| `CLIENT-ALPHA` | UC01 ✓ → UC02 ✓ (9 tasks) → UC03 collection ✓ → upload to Storage ✓ → validation ✓ → HR review → **gate passed** → UC04 `MISSING_DATA` (no UC04 config for this tenant) and UC05 `EMPLOYEE_NOT_IN_UC05_DIRECTORY` |

### 12.4 Defects found and fixed

1. **Every persisted orchestrator failed all document validation.** UC03's graph inherited the orchestrator's
   synchronous `SqliteSaver` as a subgraph; `ainvoke` raised `NotImplementedError`, recorded as
   `UC03_TECHNICAL_ERROR`. Existing tests used UC03 doubles or `MemorySaver`. Fix: UC03's graph is never
   checkpointed (`checkpointer = False`). A regression test was added.
2. **Unreadable documents were reported readable online.** With no text, UC03's hybrid extractor sent empty text
   to the LLM, and UC03's parser defaulted the confidence to 0.5. Fix: the LLM is consulted only when local
   extraction produced text. A regression test was added. The underlying behaviour in UC03's
   `HybridDocumentExtractor`/`_parse` is unchanged and should be fixed by UC03's owner.
3. **Document storage configuration broke real UC01.** A `SUPABASE_URL` in the orchestrator's environment is
   inherited by the workers and switches UC01 into its blocked Supabase mode. Fix: dedicated
   `DOCUMENT_STORAGE_*` variables, and the key is withheld from workers. A test was added.
4. Frontend: the hooks never left "Connecting…" under React StrictMode (dev). The form sent the access role
   (`hr_admin`) as UC02's form-approver role; it now asks for it using UC02's vocabulary. The Retry button showed
   for business blocks the backend refuses.

### 12.5 Spec scenarios

Done, live: 1 (demo), 3, 4, 5, 6, 8, 9, 10, 11 (UC04 blocked while UC05 completed), 13, 14, 16, 17, 18.

- **2 (UC01 validation failure):** covered by the form and by existing backend tests. Not run live.
- **7 (validation failure):** an expired document → correction, live.
- **12 (UC05 failure while UC04 succeeds):** existing backend test only.
- **15 (Supabase connection failure):** the existing store test only. The storage route maps it to 503 but was not
  exercised live.

## 13. Remaining blockers and missing integrations

1. **UC01 does not write `onboarding_cases`** (paused on decisions B1–B9). Until it does, `case_orchestration_state`
   (Supabase mode) and `candidate_documents` cannot hold UC01-opened cases. Do **not** work around this by
   writing `onboarding_cases` from the orchestrator.
2. **Real UC02 has no configuration for `CLIENT-TEST-A`** (UC02 registry: `CLIENT-ALPHA`, `CLIENT-BETA`,
   `CLIENT-EMPTY`).
3. **Real UC04:** no UC04 config for `CLIENT-ALPHA` (add `uc04_client_config_ref` in `config/tenants.yaml`). Real
   UC04 also refuses non-`SYN-` case ids (existing blocker).
4. **Real UC05:** the employee must exist in UC05's directory, and the offer-acceptance event must exist in its
   mailbox.
5. **Text extraction cannot run on this machine:** the Microsoft Visual C++ Redistributable (x64) is not
   installed, so PyMuPDF cannot load (UC03's own venv too). Install it with admin rights; `/healthz` and every
   upload report the issue until then.
6. **No identity provider.** `--dev-identity` trusts headers and is restricted to loopback. A real IdP or gateway
   must replace it before any shared deployment.
7. `document_validations` is not written (§5).
8. Observation, not changed: the same HR admin who started a case can sign off Day-1 readiness (no four-eyes
   rule on that gate).
9. Hygiene: `onboarding-ui-prototype/.env` contains `SUPABASE_SERVICE_ROLE_KEY`. Vite does not bundle it (no
   `VITE_` prefix), but it should be removed from the frontend folder.
10. Test artefacts: 5 synthetic PDFs written to `Doc_Collection` under `CLIENT-TEST-A/…` and `CLIENT-ALPHA/…`
    during the live tests.

## Running it

```powershell
# backend (HR-Onboarding-Common); set DOCUMENT_STORAGE_* in .env first (see .env.example)
.\.venv\Scripts\python.exe scripts\serve_orchestrator.py --dev-identity               # real agents
.\.venv\Scripts\python.exe scripts\serve_orchestrator.py --dev-identity --mode demo   # stand-ins, labelled
# frontend (onboarding-ui-prototype)
npm run dev
```

## Addendum (2026-11-05): internal onboarding form approval

**Why UC02 reported `MISSING_DATA`.** UC02 accepts only `hr_ops` / `hr_lead` as the role that approved the
onboarding form (`UC02 contracts.ADMIN_ROLES`); the form let the submitter pick `hr_officer`, which UC02
rejected. The root defect was upstream: `orchestrator_agent.intake_inputs_for` built UC01's form as
`APPROVED` by the **submitting user**, and the UI sent a self-declared approver role. Nothing was ever
actually approved.

**What changed.**
- **No more self-declared approval.** A person's form is never approved by submitting it. `POST /cases`
  refuses `form_approved_at` / `form_approved_by_role` from anyone not listed in
  `form_approval_attestor_roles` (empty = nobody, fail closed). The form field is removed.
- **New orchestrator gate `onboarding_form_approval`** (policy.yaml), reusing the existing HITL inbox,
  decision API, role and tenant checks, and audit. It is the graph's entry and holds **UC01**: UC01's own rule
  (`start_gate`, `open_case`) opens a case only from an APPROVED form, so the record cannot be created
  first. The flow is form submitted → HR approval → UC01 → UC02.
- **Approver role: `hr_manager`, with separation of duties.** This follows UC01's own form checkpoint
  `HC_01_FORM` (roles HR_MANAGER / CLIENT_USER, `separation_of_duties = true`). Separation of duties is a new
  generic gate option, enforced by `HITLInbox`. The requester cannot decide, even holding the role (403).
- **No approver directory exists**, so routing is role-based. The UI shows "Assignment pending
  configuration"; no individual is invented.
- **The decision feeds UC01 and UC02.** On approval, UC01 gets the form with `approved_by` = the approver and
  `approved_at` = the decision time. UC02 gets `form_approved_at` and `approved_by_role` from the decision.
  On rejection, UC01 is blocked (`ONBOARDING_FORM_APPROVAL_REJECTED`) and UC02 never runs.
- **Duplicates and corrections.** Resubmitting the identical form keeps the same request. A corrected form
  withdraws an undecided request and raises a new one.
- **Persistence.** Requests and decisions are written through to Supabase **`approval_requests`** (existing
  table): `outcome` uses UC02's upper-case vocabulary, and decision columns are written only after a real
  decision. `case_id` is NULL until an `onboarding_cases` row exists. The HITL inbox (SQLite) and case state
  survive restarts.
- **Privacy.** `CaseView` carries an approval summary without names. `GET /cases/{id}/form-approval` adds
  requester, decider and employee name for `case_identity_reader_roles`.
- **UI.** An **HR Form Approval** node sits before UC01. UC01 shows "Waiting for Form Approval" and UC02
  "Waiting for Approval". The sidebar shows the approval request, approver, status, workflow impact and
  persistence, with **no approve or reject action**.

**Evidence.**
- Backend `pytest`: 680 passed, 1 skipped, including 16 new tests in `test_form_approval_gate.py`. Two
  node-trace tests and the HTTP-start tests were updated for the new entry node and the approval step.
- Browser (real mode): the form submitted; HR Form Approval was pending for `hr_manager`; UC01 and UC02 were
  held; the sidebar had no approve or reject buttons; the state was unchanged after a reload and after a
  backend restart.
- Supabase: a `CLIENT_DUBAI` request was read back as `PENDING` with all decision columns NULL.

**Open items.**
1. **UC01 / UC02 role conflict.** UC01 says HR_MANAGER approves forms; UC02 accepts only hr_ops / hr_lead.
   Once an hr_manager approves, the real UC02 will refuse to plan until the owners align this (change the
   gate role in policy.yaml, or UC02's `ADMIN_ROLES`).
2. **Tenants missing from `client_configs`.** `CLIENT-TEST-A` and the other orchestrator test tenants are not
   in `client_configs`, so their requests cannot be written to `approval_requests`
   (`TENANT_NOT_IN_CLIENT_CONFIGS`, shown in the UI).
3. **No approver directory or individual assignment** exists.
4. **Email and id number are held in memory only (by design).** After a restart, an approved form must be
   resubmitted (same content keeps the approval) before UC01 can run.
5. **Test rows.** Synthetic rows from these tests remain in `approval_requests`
   (`request_id` `apr-a2d378fec417`, `CLIENT_DUBAI`).

### Update (2026-11-05, later): approver role changed to `hr_lead`; approvals in the UI

- **Problem.** A form approved by `hr_manager` reached the real UC02, which refused it with `MISSING_DATA` ("not
  approved by an HR role"). UC02 only accepts `hr_ops` / `hr_lead`.
- **Fix.** The gate's `approver_role` is now **`hr_lead`** (policy.yaml); `hr_lead` replaces `hr_manager` in
  the reader and identity-reader roles. Two tests keep it from drifting:
  - `test_the_form_approver_role_is_one_uc02_accepts` checks the role against UC02's `ADMIN_ROLES`.
  - `test_a_form_approved_through_the_gate_is_accepted_by_the_real_uc02` runs the real UC01 and UC02.
  - A direct run on the real agents: `hr_manager` blocks UC02 with `MISSING_DATA`; `hr_lead` completes it.
- **Approvals in the UI.** `GET /approvals` lists every pending approval of the tenant, with whether the current
  user may decide it. The dashboard's "Pending Approvals" panel and the HR Form Approval sidebar have
  Approve / Reject through the decision API. Approve asks for confirmation; Reject requires a reason.
- **Acting as.** A development-only "Acting as" selector (requester HR Admin / approver HR Lead) appears only
  when the backend reports header-trust identity.
- **Permissions.** `GET /me` reports what the policy allows the current identity. The UI disables Start, Resume,
  Retry, upload, view and HR review with the required role instead of letting the backend refuse. This fixes
  "Resume case" being offered to the approver identity, which got 403 `case_operator_roles`.
- **Cases approved earlier by `hr_manager` keep that approval.** It is not rewritten, so UC02 keeps refusing
  those cases (e.g. `fc8720bc-…`, `CASE-3ce5eaa3db23`).
- **Tests.** 686 passed, 1 skipped.

### Update (2026-11-05): UC04 / UC05 after the document gate

- **UC04 `INVALID_INPUT` (case_id must match `^SYN-CASE-\d{3,}$`).**
  - **Cause.** UC04's default identifier profile `LOCAL_SIMULATION` accepts synthetic ids only (GR-13).
  - **Fix.** UC04 already defines a `PLATFORM` profile. The UC04 worker now reads an opt-in
    `UC04_IDENTITY_PROFILE` and fixes it before UC04 loads, as UC04's own `docs/common_patches` Part A
    proposes. Unset keeps `LOCAL_SIMULATION`; an unknown name stops the worker.
  - **Governance.** Enabling `PLATFORM` is a shared-contract decision.
  - **Tests.** `test_uc04_identity_profile.py` runs the real worker: refused by default, evaluated with
    `PLATFORM`, unknown name refused.
- **UC04 `POLICY_DENIED` (next).** UC04 runs on behalf of the person who triggered the step and allows only
  `hr_onboarding_officer`, `hiring_manager` or `onboarding_orchestrator`. The dev requester identity now holds
  `hr_admin` + `hr_onboarding_officer`. No policy changed.
- **UC04 business blockers (remaining, not fabricated).**
  - **No UC04 client configuration for `CLIENT-ALPHA`.** UC04's `client_alpha` test config belongs to a
    different client id, so UC04 refuses it (`CONFIG_SCOPE_MISMATCH`); the wrong mapping was removed from
    `tenants.yaml`.
  - **No requested equipment or access** (`NO_ITEMS_REQUESTED`).
  - HR or UC04's owner must supply both.
- **UC05 `UC05_OFFER_EVENT_MISSING`.** UC05 needs the offer-acceptance event id and the employee in its
  employee directory.
  - In local mode those exist only in UC05's synthetic data (`CLIENT-TEST-A`/`B`, `EMP-TEST-*`).
  - In Supabase mode they come from shared tables that UC01 does not write yet.
  - Supplying an event id alone gives `EMPLOYEE_NOT_IN_UC05_DIRECTORY`; a real UC01 → directory integration is
    required.
- **Tests.** 689 passed, 1 skipped.

## UC04 / UC05 integration with the central orchestrator (2026-11-05)

Principle: a missing approval, configuration, directory record, event or request is shown as a **named, resumable
blocker**. It is never mocked, auto-approved or defaulted. Every blocker carries `required_from`, `required_action`,
`required_data`, `dependency`, `resolution` and `retryable`, persisted with the case and shown in the UI.

### Handoff contract (verified in code)
`UC01 → state(employee_id, joining_date, case_id) → UC02 (uc02_role_context + approval facts) → state(onboarding_plan,
task_plan) → UC03 (collection per plan version, validation) → UC02 replan when UC03 feedback requires it → mandatory
document gate → UC04 ‖ UC05` (graph fan-out; each branch persisted separately; merge waits for both).
Every stage writes through to the case store after it runs, and the next stage reads the committed state.

### IMPLEMENTED

| Area | Change | Where |
|---|---|---|
| Structured blockers | Catalogue of dependency codes. `add_blocker` enriches every blocker with who/what/how | `orchestration/dependency_blockers.py`; `adapters/_shared.add_blocker` |
| UC04 identity | The worker reads `UC04_IDENTITY_PROFILE` (UC04's own proposal; unset = synthetic profile). A platform case id under the synthetic profile is reported as `UC04_IDENTITY_PROFILE_NOT_ENABLED` (BLOCKED, resumable), no longer as a raw `INVALID_INPUT`. The UUID is never rewritten | `workers/uc_worker_runtime.py` (`main`, `_Uc04Handler.dispatch`); `adapters/uc04_equipment_access.py` |
| UC04 role | Server-side dev identity registry: roles and permitted clients resolved by the backend; `X-User-Roles` ignored. UC04 still enforces its own caller roles | `platform/identity.RegistryIdentityProvider`; `config/dev_identities.yaml`; `GET /v1/orchestration/dev/identities` |
| UC04 configuration | No configuration → `UC04_CONFIGURATION_MISSING` (stage error + blocker). No configuration is borrowed from another client; the wrong `client_alpha` ↔ `CLIENT-ALPHA` link was removed | `adapters/uc04_equipment_access.py`; `config/tenants.yaml` |
| UC04 requests | `NO_ITEMS_REQUESTED` is resolvable: HR records item ids (UC04's `ITEM_ID` pattern). The orchestrator sends them as UC04 `RequestedItem` objects, `requested_by` = the recording HR user. Only the blocked UC04 stage re-runs | `OnboardingOrchestratorAgent.record_equipment_access_requests`; `POST /cases/{id}/equipment-access/requests`; `_requested_items` |
| UC05 offer event | Mailbox mode: `UC05_OFFER_EVENT_MISSING` is resolvable by HR supplying the event reference, which UC05 verifies. UC05 Supabase mode: the case id is the event (UC05's own contract); the route is refused (409) | `record_offer_acceptance_event`; `POST /cases/{id}/orientation/offer-event`; `adapters/uc05_orientation.call_uc05_orientation` |
| UC05 directory | Worker-scoped settings `UC0X_WORKER_ENV__NAME` let UC05 run in its Supabase mode (directory = `onboarding_cases`) without switching UC01. Directory and event gaps are structured blockers naming UC01's `onboarding_cases` write as the dependency | `workers/config.WorkerConfig.child_environment`; `bootstrap.build_deps` (`uc05_offer_event_source`) |
| UI | Blocker card (reason, required from, action, data, dependency, retry). "Resolve blocker" forms only where a route exists. "Retry UC0X (resume)". Identity picker from the backend registry | `components/BlockerCard.tsx`, `StageSidebar.tsx`, `pages/Dashboard.tsx`, `api/apiClient.ts`, `api/onboardingApi.ts` |
| Config | `.env.example`: `UC04_IDENTITY_PROFILE=PLATFORM` (required for platform integration), `UC05_WORKER_ENV__*`, `ORCHESTRATOR_DEV_IDENTITIES` | `HR-Onboarding-Common/.env.example` |
| Test isolation | The test session pins UC04's default profile, so a developer's `.env` cannot change test outcomes | `tests/orchestration/conftest.py` |

**API added:**
- `POST /v1/orchestration/cases/{id}/equipment-access/requests`
- `POST /v1/orchestration/cases/{id}/orientation/offer-event`
- `GET /v1/orchestration/dev/identities` (registry provider only)

`/healthz` now reports `uc04_identity_profile` and `uc05_offer_event_source`.

**Live evidence (read-only):** a real UC05 worker in Supabase mode, through the worker-scoped settings, answers
`event_not_found` for a case absent from `onboarding_cases`, and resolves the employee (`ok`) for an existing row.
In the real UC04 worker, the `PLATFORM` profile accepts UUID case ids, a missing request yields `NO_ITEMS_REQUESTED`,
and catalogue requests are evaluated item by item.

### REQUIRES EXTERNAL CONFIGURATION / DATA

1. **UC01 writes to Supabase (`onboarding_cases`, `person_records`, `offers`): paused on UC01 decisions B1–B9.**
   This is the root dependency for:
   - `case_orchestration_state` in Supabase (FK to `onboarding_cases`); the server uses SQLite meanwhile;
   - `candidate_documents` rows and `approval_requests.case_id`;
   - UC05's directory and its case-as-event contract.
2. **UC04 client configuration.**
   - **Where it belongs:** the `uc04_equipment_access` section of the client configuration
     (`client_configs.settings_payload`, approved in `client_config_versions`).
   - **Current state:** no live client has one. UC04 still reads its configuration from a file; the Supabase
     mapping is UC04's pending D-1/D-2 decision.
   - **Missing entirely:** `CLIENT-ALPHA` and `CLIENT-TEST-A` have no configuration at all.
3. **UC04 identity profile `PLATFORM`.** A shared-contract decision (UC04 `docs/common_patches`). Your `.env`
   currently enables it.
4. **Tenants vs `client_configs`.** The orchestrator test clients (`CLIENT-ALPHA`, `CLIENT-TEST-A/B`) are not in
   `client_configs`, so `approval_requests` rows for them are refused by the database.
5. **UC02 configuration for `CLIENT-TEST-A`.** UC02's synthetic registry covers only `CLIENT-ALPHA`, `CLIENT-BETA`
   and `CLIENT-EMPTY`.
6. **UC05 calendar and orientation configuration** come from UC05's own client config. Session and participant
   persistence is UC05-owned and covered by UC05's tests; it was not re-implemented here.

### REQUIRES HUMAN APPROVAL

- Internal onboarding form (gate `onboarding_form_approval`): an HR lead other than the requester.
- HR document review (UC03), the day-1 readiness signoff, UC04 approvals and exceptions, and UC05 scheduling, buddy
  and welcome approvals.
- None of these is decided automatically; the UI shows each as pending with its required role.

### Tests (this round)

- **Backend `pytest`: 701 passed, 1 skipped** (the skip is the opt-in live-Supabase test). `ruff` is clean on the
  changed files.
- **New `test_branch_dependencies.py` (10 tests):**
  - blocker catalogue fields;
  - missing UC04 configuration → blocked while UC05 runs on, no merge;
  - resume after the configuration exists re-runs only UC04;
  - `NO_ITEMS_REQUESTED`: resolve through the route, with role, tenant and validation checks, then UC04 re-runs;
  - UC05: event missing / case-as-event / not in directory;
  - offer-event route: re-runs only UC05, refused in case mode;
  - registry identity: roles from the server, unknown user and ungranted client refused;
  - worker-scoped settings.
- **`test_uc04_identity_profile.py` (5 tests, real UC04 worker):**
  - default profile → `UC04_IDENTITY_PROFILE_NOT_ENABLED`;
  - `PLATFORM` → evaluated;
  - unknown profile → the worker refuses to start;
  - no requests → `NO_ITEMS_REQUESTED`;
  - catalogue requests → evaluated items.
- **Updated:** `test_uc04_adapter.py`, `test_integration_scenarios.py`, `test_orchestrator_e2e.py` and
  `test_control_center_api.py`, which pinned the old `MISSING_DATA` / `INVALID_INPUT` codes.
- **Frontend:** `tsc` and `vite build` are clean. Headless Edge screenshots of the UC04 and UC05 blocker cards and of
  the registry identity picker were taken against a throwaway test server.

### Update (2026-11-05): NDA and policy acknowledgements made optional

- **UC03 checklist** (`UC03_Document_Validation/knowledge/checklists/default.yaml`): `nda` and
  `policy_acknowledgements` are now `required: false` (a product decision, made in UC03's own configuration). New
  plans list them as optional; UC03 does not request or wait for them.
- **Mandatory-document gate** (`adapters/uc03_documents.apply_validation_results`): a supplied optional document is
  still validated and its correction is recorded (prefixed "optional document:"). Only mandatory documents decide
  the stage status and blockers, so an optional document never holds UC04 / UC05.
- **Existing cases** keep the plan they were planned with. For those, an HR admin can use **"Not required for this
  case…"** on a document. It calls the existing `POST /cases/{id}/documents/requirements` (`remove_documents` plus a
  recorded reason), and UC02 replans to a new plan version. `/me` now reports `change_document_requirements`.
- **UI:** progress counts mandatory documents only; optional documents are labelled and never shown as blocking.
- **Tests:** 3 new in `test_branch_dependencies.py` (optional in the plan; a failing optional document doesn't
  block; a failing mandatory one does). 4 assertions that pinned "every listed document is mandatory" were updated.
  Orchestrator 704 passed, 1 skipped; UC03 210 passed.

### Update (2026-11-05): UC04 configuration for CLIENT-ALPHA (per client and role)

UC04 scopes each `CaseConfig` to one client **and one role**. It refuses any other with `CONFIG_SCOPE_MISMATCH`.
CLIENT-ALPHA cases were blocked with `UC04_CONFIGURATION_MISSING` because no configuration existed for that client.

**Implemented**

- **Per-role lookup.** `config/tenants.yaml` now accepts `uc04_client_configs: {role_code: path}` per tenant
  (`TenantSettings`; keys must be role codes). `TenantConfig.uc04_config_ref_for(client, role)` returns the
  role's own entry, else the tenant's single `uc04_client_config_ref`. It never returns another role's or client's.
- **Role code.** `adapters/uc04_equipment_access.uc04_role_code` derives the code UC04 needs from the case's
  role: lower case, with other characters as `_` (`Power BI Developer` → `power_bi_developer`). The free-text
  role failed UC04's PLATFORM role pattern. A role that still isn't a code (e.g. it starts with a digit) gives the
  new blocker `UC04_ROLE_CODE_INVALID` (HR onboarding officer). A role without configuration gives
  `UC04_CONFIGURATION_MISSING`, and the blocker now names the client and role.
- **Traceability.** The UC04 result records `role_code` and `config_version`.
- **CLIENT-ALPHA configs.** `config/uc04/CLIENT-ALPHA.software_engineer.json` and
  `config/uc04/CLIENT-ALPHA.power_bi_developer.json` are referenced from `tenants.yaml`.

**Synthetic, not approved.** Each file is UC04's own synthetic Alpha test configuration (`data/client_alpha_config.json`,
`alpha-v1`). Only `client_id`, `role_id` and `version` (`alpha-v1-client-alpha-dev`) were changed, on the product
owner's instruction. All matrices, approvers, lead times and evidence roles are UC04's test values. Items outside a
role's lists still need UC04's EXCEPTION approval. Provenance is in `config/uc04/README.md`.

**Requires external configuration:** the client's approved configuration per role, which replaces these files. The
lasting fix, UC04 reading the central client configuration, is UC04 decisions D-1/D-2.

**Tests:** `test_uc04_client_role_config.py` (12) covers:

- the role-code mapping;
- every per-role entry being scoped to its own client and role;
- no cross-role lookup;
- a missing role naming the role without calling UC04;
- the invalid-code blocker;
- invalid keys being refused;
- the real UC04 worker (PLATFORM) evaluating a CLIENT-ALPHA "Power BI Developer" case against its own configuration,
  with no scope mismatch, and still asking HR for the requested items when none are recorded.

The orchestrator suite has 716 passed, 1 skipped and 0 failed. One existing assertion in
`test_branch_dependencies.py` pinned the old `UC04_CONFIGURATION_MISSING` dependency wording and was updated to the
role-scoped wording.

### Update (2026-11-05): UC04 skill `recommend_role_standard_items` (role-specific item recommendation)

When UC04 raises `NO_ITEMS_REQUESTED`, HR used to type the role's item ids by hand. UC04 now has a skill that
recommends the role's standard package. HR reviews it in the Resolve form and records it.

**Implemented**

- **UC04 skill.** The definition is `context_layer/skills/recommend_role_standard_items.md`, listed in the skills
  README. The logic is `src/sub_agent/role_packages.py` (`recommend_standard_items`):
  - It reads the role's `role_equipment_matrix` and `role_access_matrix` from the case's own configuration.
  - The lists are role-specific because each client and role has its own configuration file; the skill holds no lists
    of its own.
  - A configuration for another client or role gives no recommendation (`CONFIG_SCOPE_MISMATCH`).
  - A MISSING matrix gives no recommendation for that kind (never an empty list).
  - VERIFIED_EMPTY gives an empty one.
  - The skill is advisory only (L1, read-only; `requests_items: false`, `approves: false`).
  - `SkillRegistry.match_skills` selects it only when the result has `NO_ITEMS_REQUESTED`.
- **Worker.** `_Uc04Handler` applies the skill after UC04's evaluation, only for `NO_ITEMS_REQUESTED`, and returns
  `role_package_recommendation`. A UC04 without the module gives no recommendation.
- **Adapter.** The recommendation is attached to the `NO_ITEMS_REQUESTED` blocker as `recommendation`, but only if it
  is for this case's client and role. Item ids not in UC04's id format are dropped. It is never applied automatically.
- **UI (BlockerCard).**
  - The card shows "Recommended by UC04 for role …", with the skill name and configuration version.
  - The Resolve form is pre-filled from it and labelled as such. HR's own earlier entry wins over the recommendation.
  - HR edits and clicks **Save & retry UC04**. The requester is the HR user, and each item still needs its approval
    (`hiring_manager` in the CLIENT-ALPHA synthetic configuration).

**Unchanged:** DL-17 (UC04 asks HR) still holds. The stage stays BLOCKED until HR records the request, and nothing is
requested or approved on HR's behalf.

**Tests:**

- UC04 `tests/test_role_packages.py` (7):
  - order and version;
  - per-role lists;
  - scope mismatch;
  - MISSING vs VERIFIED_EMPTY;
  - no side effects;
  - registration and matching only on `NO_ITEMS_REQUESTED`.
- Orchestrator `test_uc04_client_role_config.py`:
  - the real UC04 worker (PLATFORM) recommends `power_bi_developer`'s package on the blocker, and the stage stays
    BLOCKED with no items;
  - a recommendation for another client or role, or with bad ids, is not passed on.

Results:

- **UC04:** 693 passed, 1 skipped. One existing UC04 test (`test_every_skill_can_now_be_selected_by_some_case`) checks
  that every registered skill is selected by some sample case. It was given a "nothing requested" sample for the new
  skill.
- **Orchestrator:** 717 passed, 1 skipped, 0 failed.
- **Frontend:** `tsc` and `vite build` are clean.

### Update (2026-11-05): UC05 offer acceptance from the approved form (`UC05_OFFER_EVENT_SOURCE=orchestrator_case`)

**Problem.** Orientation scheduling stopped with `UC05_OFFER_EVENT_NOT_FOUND`: "offer event Off-Act-006 is not in
UC05's mailbox". UC05 has no real mailbox integration. Its mailbox is simulated and holds only UC05's synthetic test
messages, so no real reference can ever be found there. UC05's Supabase mode reads `onboarding_cases`, but that path
doesn't work end to end today:

- these cases are not in that table, because UC01 ran in memory;
- UC01 never writes `manager_id` (UC01 decision D2), and UC05 needs it as the team lead.

**Decision (product owner, 2026-11-05):** UC05 reads the joiner from the orchestrator's HR-approved form.

**Implemented**

- **Case-backed source.** In the UC05 worker, `_CaseOfferSource` implements UC05's `EmailPort` and
  `EmployeeDirectoryPort`. The orchestrator case is the offer-acceptance event (event id = case id, as in UC05's
  Supabase mode).
  - It is registered by the new worker op `register_case_offer_event` before every UC05 pass, with the facts from
    the HR-approved form: name, email, designation, department (as team, as UC05's own Supabase mapping does),
    manager (as team lead), and joining date.
  - It knows only registered cases. UC05's synthetic mailbox and directory are not used, so no synthetic employee can
    be resolved for a real case.
  - Nothing is filled in. A form without a manager is UC05's own `INCOMPLETE_EMPLOYEE_DATA` (`team_lead_id`).
- **Configuration.** The source is opt-in: `UC05_OFFER_EVENT_SOURCE=orchestrator_case`, set in this workspace's `.env`
  and documented in `.env.example`. Both the worker and `bootstrap` refuse to start if it is combined with UC05's
  Supabase mode or given an unknown value. `/healthz` reports `uc05_offer_event_source`. The form's offer-event
  reference is not used in this mode, and `POST .../orientation/offer-event` is refused (409).
- **Email retention.** The candidate's email is kept on the case (`joiner_contact`) only in this mode, because UC05
  needs it to invite the joiner. It is never part of an API view. In every other mode it stays intake-only.
- **One UC05 record per employee.** UC05 keeps one record per (client, employee id). A second case under the same id
  is refused with `EMPLOYEE_ID_NOT_UNIQUE`, so two joiners are never merged:
  - by the orchestrator, before anything is sent, when another case already took the id into UC05;
  - by the worker's registration otherwise.
- **HRMS employee numbers.** UC01 numbers in memory in development, restarting at `EMP-000001` after every backend
  restart; all 5 CLIENT-ALPHA cases had `EMP-000001`. CLIENT-ALPHA now uses employee identity scheme `none`:
  - HR enters the HRMS employee number on the form, and starting without it is refused with a clear message (422);
  - the form label reads "HRMS employee number".
- **Messages.**
  - The not-found message now says the mailbox is simulated.
  - `UC05_OFFER_EVENT_NOT_FOUND` is now an external blocker: retyping a reference can't help.

**Existing cases:** Naveen's case and the other CLIENT-ALPHA cases can't be scheduled. They share `EMP-000001`, and
their email was dropped after intake (before this mode existed). Start a new case with the HRMS employee number.

**Tests:** `test_uc05_orchestrator_case_source.py` (6, real UC05 worker):

- the induction is scheduled from the form, and the typed reference is unused;
- the email is kept only in this mode and is not in the API view;
- no manager gives `INCOMPLETE_EMPLOYEE_DATA`;
- a second case under the same id is refused, by the orchestrator and by the worker;
- scheme `none` needs the HRMS number at start;
- the worker refuses to combine this source with UC05's Supabase mode.

`test_branch_dependencies.py` (+3) covers the cross-case guard, a different client or a case that never reached UC05,
and the simulated-mailbox message.

Results:

- **Orchestrator:** 726 passed, 1 skipped, 0 failed. 15 existing tests started CLIENT-ALPHA cases without an employee
  number, which scheme `none` now refuses. Their inputs were given an HRMS number (`HRMS-000900` etc.), as HR now
  must.
- **Frontend:** `tsc` and `vite build` are clean.

### Update (2026-11-05): UC05 buddy candidates from Supabase `buddy_profiles` (`UC05_BUDDY_SOURCE=supabase`)

**Problem.** Buddy assignment stopped with `NO_BUDDY_CANDIDATES` ("no buddy candidates are configured for
CLIENT-ALPHA").

- The UC05 worker runs UC05's local (mock) mode, so its buddy pool is UC05's own repository, which holds only
  synthetic test buddies.
- The Supabase `buddy_profiles` table is read only in UC05's Supabase mode, which can't be used together with the
  case-backed offer source.
- `buddy_profiles` has one row, for `CLIENT_INDIA`, and CLIENT-ALPHA is not in `client_configs`.
  `buddy_profiles.client_id` is a foreign key to `client_configs`.

**Implemented**

- **Buddy pool.** `_SupabaseBuddyPool` in the UC05 worker reads `buddy_profiles` read-only and replaces only the
  local repository's `list_buddy_candidates` (on that instance). It applies UC05's own rule:
  - this client's active profiles only;
  - department used as team, so a same-department buddy is preferred;
  - no profile beyond `max_active_buddies`, counting confirmed `buddy_assignments` and the buddies this UC05 assigned
    locally;
  - an empty result is UC05's `NoBuddyCandidatesError`, never a synthetic buddy;
  - HR still approves every proposal.
- **Configuration.** `UC05_BUDDY_SOURCE=supabase`, with the worker-scoped `UC05_WORKER_ENV__SUPABASE_URL` and
  `__SUPABASE_SERVICE_ROLE_KEY` (copied into this workspace's `.env` from UC05's `.env`; documented in
  `.env.example`).
  - An unknown value, missing settings, or the combination with UC05's Supabase mode stops the worker at startup.
  - The handler pings Supabase when it is built.
  - `/healthz` reports `uc05_buddy_source`.
- **Blocker guidance.** `NO_BUDDY_CANDIDATES` is now a structured blocker (HR / buddy programme owner;
  `buddy_profiles`). Its reason says where UC05 looked and what to add.
- **Live check (read-only).** The pool reaches the project: CLIENT-ALPHA has no candidates, and `CLIENT_INDIA` has
  `EMP001` (Engineering).

**Requires data from HR:**

1. A `client_configs` row for CLIENT-ALPHA (foreign key). This also lets CLIENT-ALPHA approval requests mirror into
   `approval_requests`.
2. At least one active `buddy_profiles` row for CLIENT-ALPHA: a real colleague, with department `IT` for a same-team
   match with the current joiner.

**Tests:** `test_uc05_buddy_source.py` (6):

- the pool, in UC05's interpreter against UC05's fake PostgREST: inactive profiles, full capacity (in Supabase and
  locally) and other clients are excluded; reads only (GET); every row read is filtered on the client;
- three misconfigurations stop the worker;
- the blocker text for each source.

`conftest` pins the new variables empty, so no test reaches the live database. Orchestrator: 732 passed, 1 skipped,
0 failed.

### Update (2026-11-05): UC05 records lost on backend restart (`UNCERTAIN_OUTCOME`), and HR's restart operation

**Problem.** After a backend restart, Praveena's buddy assignment showed `UNCERTAIN_OUTCOME`: "UC05 no longer holds
this employee's onboarding records". The orchestrator persists its cases (`.dact/orchestrator`), but the UC05 worker
had no data directory, so it ran in memory. The restart wiped UC05's case, induction session and approvals while the
orchestrator still remembered them. By design the orchestrator then stops instead of re-sending anything, and the
stage is not retryable. There was no way out short of starting a new case.

**Implemented**

- **UC05 persistence.** `scripts/serve_orchestrator.py` now gives the UC05 worker
  `UC05_WORKER_DATA_DIR=<persist-dir>/uc05`, unless it is set explicitly. UC05's SQLite repository, operation ledger,
  checkpoints and approval inbox now survive restarts. Documented in `.env.example`.
- **Recovery operation.** `restart_after_records_lost` (`POST /cases/{id}/orientation/operations`, roles
  `uc05_operation_roles`; `/me` reports `uc05_operations`). It is allowed only when all of these hold:
  - a UC05 stage of the case is UNCERTAIN;
  - UC05 itself, asked first, confirms it holds no records for the employee;
  - HR gives a reason (what was reconciled).

  It re-opens the orientation branch (completed stages to in_progress, others to their initial status) and clears that
  branch's results, blockers, UC05 approvals, errors and retry state. It records `uc05_restarts` (who, when, why) and
  resumes. UC05 then re-creates the case from the approved form, and the induction and every later UC05 step need HR
  approval again. Nothing from the lost records is replayed or counted as approved.
- **Blocker and UI.** The lost-records blocker now names HR admin and the resolution
  `restart_uc05_after_records_lost`. The blocker card offers **Restart UC05 for this case**, with a required "what did
  you check?" field.

**Tests:** `test_uc05_records_lost_restart.py` (2, real UC05 worker):

- the restart is refused without a reason;
- after the restart the induction waits for HR again, and after approval the buddy step continues;
- it is refused when nothing is uncertain, and when UC05 still holds the records (nothing is changed).

Orchestrator: 734 passed, 1 skipped, 0 failed. Frontend: `tsc` and `vite build` are clean.

### Update (2026-11-05): team-introduction approval — the team lead on record decides

**Problem.** Approving the team-introduction booking as HR returned "UnauthorizedApproverError: only the team_lead on
record for this employee may decide approval apr-…". That is UC05's own rule (`validate_decision`): a team-lead
approval is decided by the specific team lead on record, which here is the form's reporting manager (`TL-TEST-001`).
It is not a defect, and it is not bypassed. But no development identity existed for that person, a team lead could not
open the inbox, and the UI offered HR an Approve button that could only fail.

**Implemented**

- **Who decides is recorded.** UC05's snapshot now reports `team_lead_id` (an id; the worker projection stays ids and
  statuses). A person-specific UC05 approval carries `approver_id`: the team lead (or buddy) on record. It is set when
  the approval is surfaced, and updated on a later pass for approvals raised before this change.
- **Inbox.**
  - `GET /approvals` is now open to `approval_inbox_roles` (the case readers plus `team_lead` and `buddy`).
  - A team lead or buddy sees only the approvals naming them, and still lists and reads no cases.
  - `can_decide` checks the person, not just the role. HR sees "only the team_lead on record (TL-TEST-001) decides
    this".
- **Development identity.** `config/dev_identities.yaml` adds `TL-TEST-001` (role `team_lead`; CLIENT-ALPHA,
  CLIENT-TEST-A). The identity picker shows it. A person still makes the decision, and UC05 still verifies it.
- **UI.**
  - The stage sidebar disables Approve and Reject for anyone but the person on record, and says whom to switch to.
  - For an identity without case access, the dashboard explains that the inbox holds their approvals, and stops
    polling the case list.

**Tests:** `test_uc05_person_on_record.py` (2):

- real UC05 worker: the team-intro approval names `TL-TEST-001`; HR is refused by UC05; the team lead's approval
  completes the stage;
- inbox per identity: HR sees everything with reasons; the team lead on record sees only theirs; another team lead
  sees nothing; a team lead gets 403 on cases.

`test_form_approval_gate.py::test_the_inbox_needs_a_reader_role` now checks that an unrelated role is refused and that
a team lead sees no form approvals. Orchestrator: 736 passed, 1 skipped, 0 failed. Frontend: `tsc` and `vite build` are
clean.

**Follow-up (same day): approvals raised before the person was recorded; identity at page load.**

- **Read-time resolution.** Praveena's team-introduction approval was raised before `approver_id` existed. A waiting
  approval offers no resume, so it was never refreshed: the team lead's inbox stayed empty and HR saw only "the
  'team_lead' role". `uc05_orientation.approver_on_record` now resolves the person when the inbox or case view is
  read: the approval's own `approver_id`, else the buddy on record, else the team lead UC05 reported (now also kept in
  `orientation_schedule_result.team_lead_id`). For a case-backed UC05 case (offer event = the case, contact kept),
  UC05's team lead is by construction the form's reporting manager. Otherwise it is None: nothing is guessed. Checked
  read-only against Praveena's stored case: `TL-TEST-001`.
- **UI identity at load.** A registry-only identity such as `TL-TEST-001` is not among the UI's built-in identities,
  so after the reload the first requests went out as the default HR admin. The list loaded as HR, permissions were
  cached as HR, and later refreshes failed with 403. The switch now saves the chosen identity, and the page starts with
  it. For an identity without case access, case-detail polling stops as well.
- **Tests:** +1 (the fallback, including no guess for a case that isn't case-backed). Orchestrator: 737 passed,
  1 skipped, 0 failed. Frontend: `tsc` and `vite build` are clean.

### Update (2026-11-05): team-introduction decision not applied — `KeyError: 'sim-evt-…'` (simulated calendar lost on restart)

**Problem.** The team lead's approval was recorded, but applying it failed: `DECISION_NOT_APPLIED: KeyError:
'sim-evt-5cee874e9b61'`. With `UC05_WORKER_DATA_DIR`, UC05's sessions survived the backend restart, but its simulated
calendar (`MockCalendar`) did not. Confirming the booking asked the calendar for a hold it no longer had. Retrying could
not help, because the hold stayed missing, and the blocker offered no way to re-run the decision's follow-up.

**Implemented**

- **Persistent simulated calendar.** With a UC05 data directory, the worker's simulated calendar is saved to
  `simulated_calendar.json` after every booking, confirmation and cancellation.
- **Restore at start.** At start, a `SCHEDULED` or `CONFIRMED` session whose simulated (`sim-evt…`) hold the calendar
  doesn't hold, live or cancelled, is restored from UC05's own session record. This re-aligns the simulated provider
  with UC05's records only:
  - nothing is booked anew;
  - nobody is invited;
  - nothing is approved;
  - a hold the calendar itself cancelled is never revived;
  - a real provider is never touched.
- **Retry re-applies.** Retrying a stage blocked with `DECISION_NOT_APPLIED` now re-runs the recorded decision's
  follow-up through UC05 (`retry-apply`); nothing is decided again. The approval id comes from the blocker, or, for
  older blockers, from the stage error the adapter wrote. The blocker also offers **Retry applying the decision**
  (resolution `retry_apply_decision`).
- **Test-hook guard.** The guard now accepts the persistent subclass of UC05's simulated calendar. It is still refused
  for anything else.

**Tests:** `test_uc05_calendar_persistence.py` (3, real UC05 worker with a data directory):

- a booking made before a restart is confirmed after it;
- lost simulated holds are restored from UC05's records and confirmed by the team lead, with nothing booked and nobody
  invited by the restore;
- a failed confirmation (`DECISION_NOT_APPLIED`) is re-applied by retrying the stage, including when the blocker
  predates the approval id.

`test_uc05_lifecycle.py::test_orchestrator_and_uc05_restart_resume_the_same_case_without_duplicates` asserted
`calendar_live == 0` after a restart, which was only true because the calendar was lost. It now asserts that the same
single hold is still there.

Orchestrator: 740 passed, 1 skipped, 0 failed. Frontend: `tsc` and `vite build` are clean.

### Update (2026-11-05): UC04 for role "Developer" (CLIENT-ALPHA), and what follows it

**Configuration.** Praveena's case was blocked with `UC04_CONFIGURATION_MISSING` for client CLIENT-ALPHA, role
`developer` (the case's role "Developer"). Added `config/uc04/CLIENT-ALPHA.developer.json` and its `tenants.yaml`
entry. It is synthetic, made exactly like the other two: UC04's Alpha test configuration with only client, role and
version changed. Provenance is in `config/uc04/README.md`. The existing scope test now covers three per-role entries.

**What UC04 does next (checked with the real UC04 worker, PLATFORM profile):**

- With nothing requested, UC04 raises `NO_ITEMS_REQUESTED` and the role-package recommendation is attached.
- Once HR records the items, UC04 evaluates them. All requested items are `APPROVAL_PENDING`, awaiting the approver
  UC04's configuration names: `hiring_manager` for CLIENT-ALPHA.
- With joining date 2026-11-15, UC04 reports `AT_RISK` (inside its lead times) and proposes a loan device and a later
  joining date, for HR.

**Mapping fixes (adapter):**

- **Awaiting approval, not "blocked".** "At risk with items awaiting approval" was shown as BLOCKED with no blocker.
  It is now AWAITING_APPROVAL, with `UC04_APPROVALS_PENDING` naming the items and the approver role (from UC04's
  approver configuration), plus an `AT_RISK` warning.
- **Case-level proposals get a reference.** A proposal with no request as subject (UC04's joining-date suggestion) had
  the reference `None`. It now gets a stable reference of its own.

**Requires a decision / not yet built.** UC04 receives item approvals as `approval_records`, input facts from the
platform's approval system. The orchestrator does not accept UC04 approvals ("decided through those agents' own
mechanisms"), so the Control Center cannot record a hiring manager's approval yet. `UC04_APPROVALS_PENDING` says so
(resolution external).

**Tests:** +1 real-worker test of Praveena's scenario (role code `developer`; awaiting approval with the approver named;
every proposal has a reference).

**Results:** orchestrator 740 passed, 1 skipped, and 1 intermittent failure.

`test_module_isolation.py::test_second_worker_process_does_not_see_first_workers_state` failed once with UC04's own
`PrivacyViolation` ("phone_number") and passed 3 of 3 on rerun. This is a pre-existing UC04 false positive and is not
caused by these changes. UC04's detector (`guardrails/guardrails.py`, `(?<!\w)(?:\+|00)\d[\d\s-]{7,}`) can match a
randomly generated id such as a uuid segment `-0042-1234…`, so it depends on the ids generated in a run. It is reported
here for UC04's owners and was not changed.

### Update (2026-11-05): UC04 FAILED after HR recorded the items — idempotency key reused with a different payload

**Problem.** After the `developer` configuration was added, UC04 first raised `NO_ITEMS_REQUESTED`. HR then recorded
the items, and the stage went FAILED. The screen still showed the older `UC04_CONFIGURATION_MISSING` error, because
the failure path did not record a stage error. Reproduced with the real UC04 worker: `INTERNAL_ERROR —
IdempotencyConflict: idempotency key reused with a different payload`.

The orchestrator used one UC04 key per case and stage (`<case>.equipment_access.v1`), changed only by a joining-date
revision (`.jdN`). Every re-evaluation with new input therefore reused it: recorded requests, later approvals, a changed
configuration, and even the payload's `as_of_date` on another day. UC04 rightly refuses that (BR-21).

**Implemented**

- **One key per evaluation** (`uc04_equipment_access._evaluation_key`). The first evaluation under a base key keeps the
  base key, so the existing key formats are unchanged. A later evaluation with different content gets the base plus a
  12-character digest of the payload; an identical retry reuses its key and is still idempotent. Key, base and digest
  are kept on `equipment_access_result`, the only UC04 field the branch keeps. Keys stay within UC04's 80-character
  pattern.
- **Failure recorded on the stage.** A UC04 failure is recorded as the stage's own error, so the case shows its cause,
  not an earlier error.

**Tests:** +3:

- the key rule: first, replay, new input, new joining-date revision, and a result recorded before keys were kept;
- the real UC04 worker: no items, then items, then an identical retry. The second evaluation is no longer FAILED and
  gets a new key; the identical retry reuses it;
- a failure replaces an older stage error.

Orchestrator: 744 passed, 1 skipped, 0 failed.

### Update (2026-11-05): UC04 item approvals decided in the Control Center (hiring manager)

**Problem.** Equipment & Access was awaiting approval, but the only "approvals" offered were three `HR_OFFICER` items
that nobody could decide. They are UC04's advice to HR (a loan device, a later joining date), carrying UC04's own owner
code, not a user role. The approvals UC04 actually waits for, the hiring manager's approval of each item, had no way
in: UC04 takes them as `approval_records` from the platform's approval system, and the orchestrator refused all UC04
approvals.

**Implemented**

- **Each held item is an approval.** For each item UC04 holds (`APPROVAL_PENDING` / `EXCEPTION_PENDING`), the adapter
  raises a pending approval:
  - for the role UC04's approver configuration names for that item's class (`hiring_manager` for CLIENT-ALPHA);
  - reference `<request_id>:STANDARD|EXCEPTION`, `proposal_type uc04_item_approval`.

  `UC04_APPROVALS_PENDING` now points to the approvals inbox (resolution `decide_in_inbox`).
- **Advice stays advice.** UC04 proposals whose owner is one of UC04's own codes (upper-case, e.g. `HR_OFFICER`) are
  kept in `equipment_access_result.advisories` and quoted in the `AT_RISK` warning. They are never shown as approvals.
  Proposals naming a role still become approvals, as before.
- **Decisions** (`OnboardingOrchestratorAgent._decide_uc04_item`, via the existing decision route):
  - refused before anything is recorded when the decider lacks the configured role, when the decider is the person who
    recorded the requests, or when a rejection has no reason;
  - otherwise `uc04_approval_record` builds UC04's `ApprovalRecord`: the request and item UC04 evaluated, approver and
    role as decided, decision, today's date, client, UC04 role code and site requirement;
  - the record is added to the case's `approval_records` and the decision to `approval_decisions`;
  - the stage is re-opened and UC04 re-evaluates, applying its own validation (role, item, scope, date). A rejection
    never completes the stage.
- **Stage completion.** Once nothing awaits approval, UC04's at-risk status completes the stage (UC04 owns
  provisioning) and the `AT_RISK` warning is kept.
- **Access.** `approval_inbox_roles` includes `hiring_manager`. A caller without case access sees approvals naming them
  or, when no person is named, needing their role. Dev identity `user:hiring-manager-01` (role `hiring_manager`;
  CLIENT-ALPHA, CLIENT-TEST-A). The UI blocker card says where to decide.

**Checked with the real UC04 worker:** with records built this way, the three items moved from `APPROVAL_PENDING` to
`ELIGIBLE_FOR_SIMULATED_SUBMISSION`, and the stage completed with the at-risk warning.

**Tests:** `test_uc04_item_approvals.py` (4):

- held items become approvals for the configured role, and advice does not;
- the real UC04 accepts the recorded approvals (stage complete), and a rejection does not complete it;
- the decision guards (role, requester, rejection reason), the record's fields, and the re-evaluation;
- the hiring manager's inbox.

Orchestrator: 748 passed, 1 skipped, 0 failed. `test_uc04_client_role_config` now expects resolution `decide_in_inbox` for `UC04_APPROVALS_PENDING`. Frontend: `tsc` and `vite build` are clean.

**UI:** Equipment & Access offers **Retry UC04 (resume)** while awaiting approval as well. A resume re-runs any stage that is not complete or failed, so UC04 re-evaluates, re-reading the approvals recorded so far. A case raised before this change gets its hiring-manager item approvals this way.

### Update (2026-11-05): the joining-day welcome message in the Control Center

**Request.** Show the welcome message in the Joining-Day Welcome output. UC05 drafts the welcome only on the joining
day (the joining-day event), from the client's approved `welcome` template plus a short note written by its model, and
HR approves it before UC05 sends it. Before that day there is no message. Once drafted, the orchestrator deliberately
keeps no message text.

**Implemented**

- **Worker op `welcome_message`** (UC05 worker, read-only). Returns UC05's newest welcome draft (subject, body, status)
  once written; otherwise a preview composed the way `draft_welcome_communication` does it:
  - the client's approved template;
  - the facts filled in by code (name, role, start date, sender);
  - the confirmed session line and the buddy;
  - but not the personal note, which UC05 adds only when it drafts.

  Nothing is drafted, submitted or sent. Another tenant's employee reads as unknown.
- **Read restriction.** `GET /cases/{id}/welcome-message` (`OnboardingOrchestratorAgent.welcome_message`) is limited to
  `case_identity_reader_roles`, because the message names the joiner. The UI's `WelcomeMessage` panel in the
  Joining-Day Welcome output shows "preview — not sent" or the draft with its status, explains approval and sending,
  and names a missing template.
- **Welcome template source.** `UC05_WELCOME_CONFIG_SOURCE=supabase` makes the UC05 worker read the client's welcome
  configuration, read-only, from `client_configs.settings_payload.uc05_welcome`, using UC05's own
  `SupabaseOnboardingRepository.get_client_config`, the same reader as its Supabase mode. It shares one read-only
  client with the buddy source.
  - Without the section: `MissingClientConfigError` for the draft (`MISSING_DATA`), and the preview says so. Nothing is
    defaulted.
  - Enabled in this workspace's `.env`, documented in `.env.example`, and pinned empty in `conftest`.
  - An unknown value, combination with UC05's Supabase mode, or missing settings stops the worker.
  - `/healthz` reports `uc05_welcome_config_source`.

**Requires data:** CLIENT-ALPHA's `client_configs` row has no `uc05_welcome` section (checked read-only). Its welcome
template (sender name, subject and body, using only `{full_name}`, `{role_title}`, `{start_date}`, `{sender_name}`) is
the client's to provide.

**Tests:** `test_uc05_welcome_message.py` (7, real UC05 worker):

- the preview before the joining day, with nothing drafted or sent;
- the actual draft after the joining-day event;
- a missing template is named;
- a team lead cannot read the message;
- three misconfigurations stop the worker.

Orchestrator: 755 passed, 1 skipped, 0 failed. Frontend: `tsc` and `vite build` are clean.
