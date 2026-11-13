# Control Center Data-Flow Audit

Date: 2026-11-05. Written before any change. It supersedes the first pass in `UI_DATA_PRESENTATION_AUDIT.md`.

**Path checked for each field:** source (Supabase table / UC repository) → UC output → orchestrator adapter →
orchestrator state (`case_orchestration_state`: stage results and `stage_outcomes`) → API (`api.py`) → frontend type
(`onboardingApi.ts`) → component.

**How it was checked**, all read-only:

- the running backend, for cases `ea04bc4c-2981-47da-b249-0b0d54024f12` (suvedha) and
  `3ff62657-e7dd-4f54-922c-e2a7f5162357` (Praveena);
- the shared Supabase project;
- the code.

**Classes:** `DATA_MISSING` (no source holds it), `DATA_DROPPED` (a source holds it, and it is lost before the
orchestrator state), `DATA_NOT_EXPOSED` (in state, not in the API), `UI_MAPPING_MISSING` (in the API, not rendered or
rendered raw), `PRIVACY_RULE`, `INVALID_TEST_DATA`, `CONFIGURATION_MISSING`, `EXPECTED_BEHAVIOR`.

## UC04 / UC05 / approvals / employee number (traced directly)

| Area | Source | Backend | Orchestrator | API | Frontend | Problem | Fix |
|---|---|---|---|---|---|---|---|
| Pending Approvals — employee name | `intake_trigger.candidate_name` (form) | — | in state | `/approvals` returns `employee_name` only for `case_identity_reader_roles` (HR admin/officer/lead); verified `suvedha` for HR | `ApprovalsInbox` shows "Name withheld" when absent | **PRIVACY_RULE** (correct for team lead / hiring manager). **UI_MAPPING_MISSING**: no useful identifier is shown instead | Keep the rule. Show the permitted identifiers: HRMS employee no., role, department, joining date, case id |
| Pending Approvals — what is approved | `pending_approvals[].description`, `record_id`, `approver_id`; `equipment_access_result.items` | UC04 result (item, request id) | stored | `description` and `approver_id` returned; no request/item field | not rendered: five identical "Uc04 Item Approval" rows | **UI_MAPPING_MISSING**, **DATA_NOT_EXPOSED** (the item) | API adds `request` (item id, item kind, approval type) for UC04 items. UI renders the description, request and person on record |
| Pending Approvals — requested by / at | form: `form_approval.requested_by/at`; UC04: `equipment_access_requests.recorded_by`; others: the raising agent | — | no timestamp on pending entries | `requested_by` only for forms | "—" | **DATA_MISSING** (no time on non-form entries); **DATA_NOT_EXPOSED** (UC04 requester) | Record `raised_at` when the orchestrator first surfaces an approval (a true time, never invented). Return `raised_by` (agent label), and `requested_by` for UC04 items (identity-gated, like the form's) |
| Pending Approvals — status / decision | inbox lists pending items only; decisions in `approval_decisions` | — | stored | returned (`CaseView.approval_decisions`) | not shown in the inbox | **EXPECTED_BEHAVIOR** (the inbox is the pending list); decisions belong in the stage panels | Show decisions in the stage panels (UC04 below) |
| Employee number | HRMS/canonical: `employee_ref.employee_id` (EMP008, entered on the form; scheme `none`); UC01: `state.employee_id` (EMP-000001, UC01's development numbering) | — | both stored | `case_summary`, `/approvals` and `CaseView.employee_id` all give UC01's number | Case list, Intake panel and inbox show EMP-000001; the inbox doesn't render it | **DATA_NOT_EXPOSED** (HRMS no. not in summary/inbox), **UI_MAPPING_MISSING** | API adds explicit `hrms_employee_no` (from `employee_ref`, else the number entered on the form) and `uc01_employee_no`. `employee_id` stays unchanged for compatibility. UI labels both |
| UC04 — requested items | `equipment_access_requests` (recorded by HR) | UC04 items (`item_id`, `state`, `request_id`) | stored | returned | only item id and state; kind, requester and time missing | **UI_MAPPING_MISSING** | Render each item with its kind (from the request id) and state, plus who recorded and when |
| UC04 — quantity | — | UC04 does not model quantities (brain: "Quantities are not modelled") | — | — | — | **DATA_MISSING** | Not shown |
| UC04 — approvals and decisions | approver role from UC04 config; decisions as `approval_records` + `approval_decisions` | UC04 validates the records | stored | `approval_decisions` returned | not shown in the panel | **UI_MAPPING_MISSING** | Render the approver role and each item's decision (who, when, outcome) |
| UC04 — advice | UC04 proposals with owner codes | ProposalOut | `equipment_access_result.advisories` | returned | only inside the AT_RISK blocker text | **UI_MAPPING_MISSING** | Render the list |
| UC04 — configuration | `config/uc04/*.json` (synthetic dev) | — | `role_code`, `config_version` | returned | not shown | **UI_MAPPING_MISSING** | Render |
| UC04 — "Blockers: —" | — | UC04 result has no `blockers` field in the stored result | — | — | always "—" | **UI_MAPPING_MISSING** (meaningless row) | Remove. Blockers have their own section |
| UC04 — errors / retry | `stage_errors`, `blockers`, `retry_metadata` | — | stored | returned | rendered (Activity, Blockers, Retries) | **EXPECTED_BEHAVIOR** | None |
| UC05 Orientation — times | UC05 `OnboardingSession.scheduled_start` / `scheduled_end` | worker `_case_snapshot` projects **start only** | `scheduled_start` only | returned | raw ISO "2026-11-24T09:00:00+04:00" | **DATA_DROPPED** (end), **UI_MAPPING_MISSING** (formatting) | Snapshot adds `scheduled_end`, `attendees`, invitations, replies and the calendar ref. Adapter stores `start_utc`, `end_utc`, `timezone` (case timezone, else the tenant's), `session_type`, `calendar_event_id`. UI formats date, start–end and timezone |
| UC05 Orientation — meeting link / room | — | not in UC05's local data model; the simulated calendar returns none | — | — | — | **DATA_MISSING** | Shown only if a value ever exists; no placeholder |
| UC05 Buddy — identity | Supabase `buddy_profiles` (buddy source `supabase`) / UC05 local candidates | snapshot passes `buddy_id` only | `buddy_id`, `proposed_buddy_id` | returned | id only | **DATA_DROPPED** (name, department, location) | Snapshot adds the buddy's profile from the active buddy source. Adapter stores `buddy_name`, `buddy_department`, `buddy_location`. UI renders |
| UC05 Buddy — CLIENT-ALPHA profile | `buddy_profiles` row `employee_id='<colleague employee number>'`, `full_name='<colleague full name>'` (verified) | assigned by UC05 | stored | returned | displayed as if valid | **INVALID_TEST_DATA** (an unedited SQL template from this project's own instructions) | Guard: the buddy pool never offers a profile with placeholder/invalid values. The UI never presents such a value as a valid buddy. Data blocker: a real profile |
| UC05 Buddy — employee number, assignment id, match score, criteria, assigned_at | `buddy_profiles` has no separate employee-number column (`employee_id` is the number); assignment id / score / criteria / assigned_at exist only in `buddy_assignments`, written by UC05's Supabase mode | — | — | — | — | **DATA_MISSING** in this deployment (UC05 local mode) | Not shown. Approval id and status are shown |
| UC05 Team Introduction — participants | UC05 session `attendees`, `list_invitations` (pending/sent/failed), `list_participant_responses` (RSVP + time) | snapshot projects none | none | none | "Session Status: confirmed" only | **DATA_DROPPED** | Snapshot adds them. Adapter stores `participants[]` with `participant_id`, `role` (joiner / team lead / buddy, from UC05's record), `invitation_status`, `rsvp`, `responded_at`, and a name only where a source exists (buddy profile). UI table; the joiner's name only for identity readers |
| UC05 Team Introduction — name/email/proposed time/note | team lead: an id only in every source; email: joiner only (identity-gated); proposed time / note: not in `ParticipantResponse` | — | — | — | — | **DATA_MISSING** / **PRIVACY_RULE** | Not shown; the role and id are shown |
| Day-1 sign-off — label | `gate: day_one_signoff` | — | description "orchestrator gate day_one_signoff" | returned | sidebar approval card shows the raw description | **UI_MAPPING_MISSING** | Presentation label map (gate / proposal type → label). Codes unchanged |
| Joining-Day Welcome | UC05 draft / client template | worker `welcome_message` | — | `/welcome-message` | `WelcomeMessage` panel | **EXPECTED_BEHAVIOR** (fixed earlier today); CLIENT-ALPHA `uc05_welcome` now present (verified) | None |
| Internal codes in UI | approval types, UC04 item states, UC05 statuses | — | — | — | `humanize()` of raw codes in the inbox and generic `KeyValues` | **UI_MAPPING_MISSING** | Label maps at the presentation boundary for approval types, gates, UC04 item states and UC05 session statuses |

## UC01 / UC02 / UC03 and repository-wide scan

This section comes from a read-only sweep of every adapter, the API models and every component. Items marked
**FIXED** were also verified directly before changing anything.

| Area | Source | Backend | Orchestrator | API | Frontend | Problem | Fix |
|---|---|---|---|---|---|---|---|
| UC03 Document Collection — outstanding | `document_collection.outstanding` entries are `{document_type, reason}` (orchestrator_agent.py) | — | stored as objects | returned | `outstanding.join(', ')`: "[object Object]" as soon as one is outstanding; TS type `string[]` | **UI_MAPPING_MISSING** | **FIXED**: type corrected; renders the type and reason |
| UC01 — result fields | `OpenCaseResult` / `Case`: `event_id`, `reasons` (CASE_OPENED, LINKED_EXISTING), status, variants, `opened_at`, `folder_ref`, reactivation and placement fields | returned | adapter keeps `case_id`, `employee_no`, `joining_date` | — | — | **DATA_DROPPED** (dropping the candidate's name/email and the HR officer is correct: **PRIVACY_RULE**) | Not fixed: store a non-sensitive `uc01_result` summary (variants and reactivation explain the approvals UC01 creates) |
| UC01 — LINKED_EXISTING | linked case | returns the existing case | intake COMPLETE without `employee_id` / `joining_date` | — | "Not yet allocated" | **DATA_DROPPED** (functional) | Not fixed (UC01 contract, not presentation): fetch the linked case or block for HR |
| UC02 — task details | `ChecklistEntry` `title`, `category`, `depends_on`, `is_overdue`, `evidence_required` | UC02's own `_summarise` drops them | not stored | — | TaskTable shows `template_key` | **DATA_DROPPED** (inside UC02) | Not fixed: needs UC02's summary to include them (UC02 change) |
| UC02 — advice, agent run, checklist counts | advice `summary`, `recommended_action`, `confidence`; checklist `total`, `overdue`, ...; agent-run ids | returned | dropped by the adapter | — | — | **DATA_DROPPED** | Not fixed (lower priority) |
| UC02 — plan after a replan or date change | revised plan | — | `onboarding_plan` rewritten, `task_plan.tasks` not | returned | TaskTable can show the first version's tasks | **DATA_DROPPED** (stale) | Not fixed: functional UC02 adapter change (re-read the checklist after a replan); reported |
| UC03 — result fields | `outstanding_requirements`, `visa_pack_status`, `approval_status`, `injection_detected` | returned | dropped | — | — | **DATA_DROPPED** | Not fixed (lower priority) |
| State not in the API | `evidence_references`, `replan_round`, `document_route`, `gate_rejections`, `error_detail`, some `uc02_role_context` facts, collection `notification.detail` (delivery-failure reason) | — | stored | not returned | — | **DATA_NOT_EXPOSED** | Not fixed (lower priority) |
| Returned, never rendered | `joining_date_history`, `field_provenance`, `requirement_change_requests`, `validation_issues`, `outstanding_tasks`, `stage_outcomes[].warnings`, `task_plan.issues`, `invalid_documents`, `expired_documents`, document `evidence_status` / `hr_decision` | — | — | returned | not rendered | **UI_MAPPING_MISSING** | Not fixed (each needs its own view); listed for follow-up |
| Raw codes in UC01–UC03 views | TaskTable owner/status, plan-history reasons, replanning reasons, document reason/source codes, "HELD: location_name_required", `state_store` class name | — | — | — | raw | **UI_MAPPING_MISSING** | Partly: the label module (`lib/labels.ts`) is in place; these views are not yet switched to it |
| Branch status in the diagram | `branch_statuses[].status` | — | — | returned | raw `waiting_for_approval` | **UI_MAPPING_MISSING** | **FIXED** (`workflowStatusView`) |
| "complete" stage | orchestrator terminal stage `complete` | — | — | returned | raw "complete" in the case list / sidebar | **UI_MAPPING_MISSING** | **FIXED**: "Onboarding complete" |
| Form approval panel | — | no approver email field exists | — | — | "Assigned approver email: —" even when one is assigned | **UI_MAPPING_MISSING** (misleading row) | **FIXED**: row removed |
| ChatBot | fixed fake cases (Sarah Chen, CASE-00x, ...) in `ChatBot.tsx` | — | — | — | mounted on every page, including the live dashboard | **INVALID_TEST_DATA** in the live UI | **FIXED**: rendered only when the backend runs in demo mode |
| `/demo/*` pages | `data/mockData.ts`, inline arrays | — | — | — | reachable by URL in live mode (labelled "Synthetic data"; two texts mislead) | **EXPECTED_BEHAVIOR** (demo pages) with a gap | Not fixed: guard `/demo/*` behind demo mode (product decision) |
| Masking | "Name withheld", "Withheld for your role" | policy `case_identity_reader_roles` | — | — | — | **PRIVACY_RULE** | Kept |
| Generic '—' fallbacks | `Row` / `scalar` / `fmtTime` for absent values | — | — | — | — | **EXPECTED_BEHAVIOR** | Kept |
| Dashboard client label | dev identity default | — | — | case list returns `client_id` | shows the identity's client | **UI_MAPPING_MISSING** (minor) | Not fixed (low) |
