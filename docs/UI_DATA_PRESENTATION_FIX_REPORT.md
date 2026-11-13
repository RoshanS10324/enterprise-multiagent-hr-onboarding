# UI Data Presentation — Fix Report

Date: 2026-11-05. The audit is in `CONTROL_CENTER_DATA_FLOW_AUDIT.md` (the first pass is in
`UI_DATA_PRESENTATION_AUDIT.md`).

## 1. Audit summary

- **Findings:** 45 in total.
  - 23 traced directly (UC04, UC05, approvals, employee numbers).
  - 22 from the repository-wide sweep (UC01–UC03, the frontend, demo pages).
- **Fixed:** 20.
- **Not fixable without data:** 6.
- **Kept on purpose:** privacy masking, demo-only data, honest '—' fallbacks.
- **Reported for follow-up:** the remainder (section 3).

## 2. Issues fixed

| # | Screen / component | Root cause | Source of the data | Files / functions | Before | After |
|---|---|---|---|---|---|---|
| 1 | Pending Approvals (`ApprovalsInbox`) | The description, approver and item were returned (or available) but not rendered | `pending_approvals`, `equipment_access_result.items`, `equipment_access_requests` | `api.py`: `approval_inbox`, `_approval_request`, `_requester`, `_RAISED_BY`; `_shared.add_pending_approval` (`raised_at`); `ApprovalsInbox.tsx`: `requestLine`, `typeLabel` | Five identical "Name withheld / Uc04 Item Approval" rows | "Equipment & Access Approval · Equipment: laptop-standard · standard approval", the approver role (and the person on record), who raised it and when |
| 2 | Name masking | Intentional (`case_identity_reader_roles`). The fallback gave nothing useful | Policy | `ApprovalsInbox.tsx` | "Name withheld" | The name for HR roles. Otherwise "Employee EMP008 — name shown to HR roles only", plus role, department and joining date |
| 3 | Employee number | The API used UC01's number only | `employee_ref.employee_id` (HRMS), else the number entered on the form; UC01's `employee_no` | `api.py`: `employee_numbers`, `CaseView` / `CaseSummary` `hrms_employee_no`, `uc01_employee_no` (`employee_id` unchanged); `CaseList.tsx`; StageSidebar Intake | `EMP-000001` everywhere | "HRMS employee no. EMP008" and "UC01 development employee no. EMP-000001", labelled separately |
| 4 | Orientation | The worker snapshot projected the start only; the UI showed raw ISO | UC05 `OnboardingSession` | worker `_session_view`; adapter `Uc05Session`, `_session_fields`, `_case_tz`, the orchestration timezone; `StageSidebar.SessionView`, `labels.sessionTime` | "Scheduled Start 2026-11-24T09:00:00+04:00" | Session: Induction · Date 24 Nov 2026 · Time 09:00 – 10:30 GST · Timezone Asia/Dubai · Status Confirmed · Approval · Calendar reference |
| 5 | Buddy | The snapshot passed only the id. The database row holds template text | Supabase `buddy_profiles` / UC05 candidates | worker `valid_buddy_profile`, `_SupabaseBuddyPool.profile`, `ignored`, `_buddy_profiles`; adapter `_buddy_fields`; `StageSidebar.BuddyView` | "Proposed Buddy Id: <colleague employee number>" | Buddy employee no., name, department, location, status, approval. An invalid or placeholder profile: "Invalid buddy record …", never shown as a buddy. None eligible: "Buddy assignment pending — no eligible buddy profile configured." |
| 6 | Placeholder guard | No validation of profile values | `buddy_profiles` | worker `valid_buddy_profile` (used by the pool's listing) | A template row could be assigned | Template / placeholder / dummy / test-buddy profiles are skipped and counted in the no-candidates detail; NO_BUDDY_CANDIDATES is raised as before |
| 7 | Team Introduction | The snapshot dropped attendees, invitations and replies | UC05 session `attendees`, `list_invitations`, `list_participant_responses`; relayed `participant_responses` | worker `_session_view`; adapter `_session_fields` (`participants[]`); `SessionView` table (relayed replies merged) | "Session Status: confirmed" only | Date and time with timezone, plus a participants table: participant (the joiner's name for identity readers), role, invitation, RSVP and time |
| 8 | Equipment & Access | The view rendered only item states, plus a meaningless "Blockers: —" | `equipment_access_result` (items, advisories, `role_code`, `config_version`), `equipment_access_requests`, `approval_decisions` | `StageSidebar.EquipmentView` | Readiness plus raw states; "Blockers: —" | Each item with its kind, state label and decision (who); the approver role; requested by and when; UC04 advice; configuration. The blockers row is removed |
| 9 | Day-1 sign-off and approval cards | Raw description "orchestrator gate day_one_signoff" | `gate`, `proposal_type` | `labels.approvalLabel`; StageSidebar approval card | Internal code | "Day-1 Readiness Sign-off" (codes unchanged) |
| 10 | Results recorded before this change | Completed stages are never re-run | UC05 | adapter `refresh_uc05_details`; `orchestrator_agent.refresh_uc05_details`; `POST /cases/{id}/orientation/refresh-details`; UI `Uc05DetailsRefresh` | Old results stay reduced | HR clicks "Refresh details from UC05": display details only; statuses, approvals and decisions unchanged |
| 11 | Document Collection | Objects joined as strings | `document_collection.outstanding` `{document_type, reason}` | `onboardingApi.ts` type; StageSidebar | "[object Object]" once anything is outstanding | "passport (Hr review rejected)" |
| 12 | Branch status in the diagram | Raw status | `branch_statuses` | `WorkflowDiagram.tsx` | "(waiting_for_approval)" | "(Waiting for Approval)" |
| 13 | Terminal stage | Missing from the stage map | `current_stage = complete` | `stages.currentStageTitle` | "complete" | "Onboarding complete" |
| 14 | Form approval panel | A row for a field that does not exist | — | `FormApprovalPanel.tsx` | "Assigned approver email: —" | Removed |
| 15 | ChatBot | Fixed fake cases on the live dashboard | `ChatBot.tsx` canned data | `ChatBot.tsx` | Shown everywhere | Shown only when the backend runs in demo mode |
| 16 | Internal codes | Raw item / session / reply / decision / role codes | — | `lib/labels.ts` (`stateLabel`, `personRoleLabel`, `approvalLabel`, `humanizeCode`) | e.g. `ELIGIBLE_FOR_SIMULATED_SUBMISSION` | "Approved — ready for (simulated) IT submission" |

## 3. Issues not fixed

**Require data, configuration or integration**

| Issue | Requires |
|---|---|
| CLIENT-ALPHA `buddy_profiles` row holds template text | Database data: a real profile (below) |
| Case `ea04…` already assigned the template "buddy" | HR decision: re-assigning it (UC05 has no un-assign operation) |
| Meeting link, room | External integration: UC05's local model and the simulated calendar hold neither |
| Team lead name, participant emails, proposed new time, response note | Another UC / data: no authoritative source holds them (UC05 `ParticipantResponse` has only the response and time) |
| Buddy assignment id, match score, criteria, assigned_at; separate buddy employee number | UC05 Supabase mode writes `buddy_assignments`; `buddy_profiles` has no employee-number column (`employee_id` is the number) |
| UC04 quantity | UC04 does not model quantities |
| Team introduction with buddy / hiring manager | UC05's team introduction invites only the joiner and the team lead |

**Backend implementation, reported (not presentation)**

- UC02 `task_plan` goes stale after a replan or date change.
- UC01 LINKED_EXISTING completes intake without an employee number.
- UC02's own summary drops task titles.

**Lower-priority display gaps (listed in the audit)**

- UC01/UC02/UC03 fields returned but not rendered.
- Raw codes in UC01–UC03 views; the label module now exists for them.
- `/demo/*` pages reachable in live mode (a product decision).
- Dashboard client label.

## 4. Data-flow changes

```
Orientation / team intro  UC05 OnboardingSession(start, end, attendees, external_event_ref) + invitations + replies
  → worker _case_snapshot._session_view → adapter Uc05Session → _session_fields
  → orientation_schedule_result / team_introduction_result {start_utc, end_utc, timezone, session_type,
    calendar_event_id, participants[]} → case_orchestration_state → CaseView.orientation / team_introduction
  → StageSidebar.SessionView
Buddy  buddy_profiles (Supabase, read-only) or UC05 candidates → worker _buddy_profiles (+ valid_buddy_profile)
  → Uc05Snapshot.buddy_profiles → _buddy_fields → buddy_assignment_result {buddy_name, buddy_department,
    buddy_location, buddy_profile_valid} → CaseView.buddy_assignment → BuddyView
Employee numbers  employee_ref / intake_trigger.employee_id + UC01 employee_no → api.employee_numbers
  → CaseView / CaseSummary / approvals {hrms_employee_no, uc01_employee_no} → CaseList, Intake, ApprovalsInbox
Approvals  pending_approvals {description, proposal_type, record_id, approver_id, raised_at}
  + equipment_access_result.items → /approvals {request, raised_by, requested_at, requested_by*} → ApprovalsInbox
UC04  equipment_access_result / equipment_access_requests / approval_decisions (all already in CaseView)
  → EquipmentView
```

`*` identity-gated. No new store, no migration, no second source: every value is read from where it already lives.

## 5. Placeholder and fallback audit (what remains, and why)

| Text | Why it remains |
|---|---|
| "name shown to HR roles only" (inbox), "Withheld for your role" (form panel) | Privacy masking |
| '—' (`Row`, `scalar`, `fmtTime`) | The value is genuinely absent |
| "HRMS employee no. not resolved yet" / "HRMS no. not resolved" | The case has no resolved HRMS number yet; nothing is substituted |
| "Not invited yet", "No reply yet", "Not in the buddy directory", "Not reported yet" | Explicit missing-data states |
| "Invalid buddy record" | Invalid production data, shown as such |
| `ChatBot` canned cases, `/demo/*` pages, `data/mockData.ts` | Legitimate demo fixtures. The ChatBot now shows only in demo mode; the `/demo` pages are not guarded yet (section 3) |
| Test fixtures (`_lifecycle.py` etc.) | Legitimate test fixtures |

## 6. Test results

- **Backend** (`HR-Onboarding-Common`, `pytest tests`): 762 passed, 1 skipped, 0 failed. New or extended:
  - `test_control_center_presentation.py` (7):
    - orientation start/end/timezone end to end;
    - buddy name, department and status end to end;
    - team-intro participants, invitations and replies;
    - UC04 approvals distinguishable, with tenant isolation;
    - both numbers and privacy masking;
    - case views never invent a number;
    - the refresh changes no status.
  - `test_uc05_buddy_source.py`: the template and dummy profiles are skipped and flagged invalid.
- **Frontend:**
  - `npm run test:unit`: 4 passed, 0 failed (`labels.test.ts`: approval and Day-1 labels, state labels, placeholder
    recognition, timezone conversion including "no end, none invented").
  - `npm run build` (`tsc` + `vite`): clean.
  - The project has no component-test framework. Component rendering was checked by type-check and build only.
- **Real case `ea04bc4c…`**, verified on a copy of the live stores with the real UC05 worker and the read-only Supabase
  sources:
  - HRMS `EMP008` / UC01 `EMP-000001`;
  - induction 05:00–06:30Z (09:00–10:30 Asia/Dubai), confirmed;
  - team introduction 06:30–07:00Z (10:30–11:00 Asia/Dubai), confirmed;
  - participants EMP008 (joiner) and TL-TEST-001 (team lead), invitations sent;
  - buddy flagged invalid (template row);
  - stage statuses unchanged.
- **Not verified in the running UI:** the backend must be restarted to load this code.
