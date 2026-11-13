# UI Data Presentation Audit — Onboarding Control Center

Date: 2026-11-05. Scope: every stage panel, Pending Approvals, case list and case header, activity, blockers and
pending actions.

**Method.** Each field was traced from its source to the screen, read-only, against the running backend and the
shared Supabase project:

- database (Supabase / UC05's repository) → UC worker → orchestrator adapter → `case_orchestration_state` (stage
  results and `stage_outcomes`) → API → frontend API client → component.

The live cases checked were `ea04bc4c-2981-47da-b249-0b0d54024f12` (suvedha, CLIENT-ALPHA) and
`3ff62657-e7dd-4f54-922c-e2a7f5162357` (Praveena). The fix column states what is proposed. **Nothing has been changed
yet.**

**Classes:**

| Class | Meaning |
|---|---|
| 1 | Backend data missing |
| 2 | Database data missing or invalid |
| 3 | Backend-to-orchestrator mapping missing |
| 4 | Orchestrator-to-API mapping missing |
| 5 | API-to-frontend mapping missing |
| 6 | Frontend rendering issue |
| 7 | Intentional privacy redaction |
| 8 | Test/mock placeholder |
| 9 | Configuration/data dependency |
| 10 | Genuine missing functionality |

## Findings

| # | Screen/Component | Stage | Current Display | Expected Display | Data Exists? | Source | Missing Mapping? | Recommended Fix | Priority | Class |
|---|---|---|---|---|---|---|---|---|---|---|
| A1 | Pending Approvals (`ApprovalsInbox.tsx`) | all | "Name withheld" for team-lead and hiring-manager identities | The joiner's identifiers the role may see | Yes. Name in `intake_trigger.candidate_name`. For HR roles the API returns it (verified: `suvedha`) | `api.approval_inbox` returns `employee_name` only for `case_identity_reader_roles` | No: deliberate | Keep the redaction. Replace the fallback with permitted identifiers: canonical employee number, role, department, joining date, case id, and "name shown to HR only". Showing the name to the approver on record is a policy decision (see Q1) | P1 | 7 + 6 |
| A2 | Pending Approvals; case list (`CaseList.tsx`); Intake output | all / intake | `EMP-000001` (UC01's in-memory development number, shared by several cases); the inbox doesn't render it at all | The canonical HRMS number used by every stage (`EMP008` for suvedha) | Yes, `employee_ref.employee_id` | `api.case_summary` / `approval_inbox` use `state["employee_id"]` (UC01's number) | Yes: the API maps the wrong field | API returns `employee_number` = `employee_ref.employee_id` (falls back to UC01's number only when no reference exists) and labels UC01's separately; the UI renders it | P1 | 4 + 5 |
| A3 | Pending Approvals | equipment_access, team_introduction | UC04 item approvals appear as identical "Uc04 Item Approval" rows (which item is missing). Team intro shows no person | The item ("standard approval: equipment item laptop-standard"), and the person on record for person-specific approvals | Yes: `description` and `approver_id` in the API response | API → UI | Yes: returned but not rendered | Render `description` and `approver_id` | P1 | 5 |
| A4 | Pending Approvals | non-form approvals | "Requested: —" | Who raised it (the agent) | Partly: the `agent` field; no timestamp on pending entries | `pending_approvals` | Yes | Show "raised by UC04 / UC05 / orchestrator" from `agent`. No timestamp is invented | P3 | 5 |
| A5 | Case header | all | Name from the profile (HR roles) | — | Yes | `/cases/{id}/profile` | No | None. Correct, and identity-gated | — | — |
| O1 | Orientation Scheduling (`StageSidebar` default `KeyValues`) | orientation_scheduling | "Scheduled Start: 2026-11-24T09:00:00+04:00" (raw ISO); no end, no timezone name, no session type label; nested values as "3 field(s)" | Session type, date, start, end, timezone (Asia/Dubai), status, approval | Start: stored. **End: in UC05** (`OnboardingSession.scheduled_end`) but **dropped by the worker snapshot**. Timezone: `case_timezone` (Asia/Dubai) | UC05 repo → worker `_case_snapshot` (projects `scheduled_start` only) → adapter → `orientation_schedule_result` | Yes, at the worker snapshot (end, attendees); the UI has no renderer | Snapshot adds `scheduled_end` and `attendees`. Adapter keeps `scheduled_end`, `session_type`, `timezone`. Dedicated UC05 renderer: date, start–end, timezone, status, approval | P1 | 3 + 6 |
| O2 | Orientation / Team introduction | UC05 sessions | Not shown | Meeting link, room/location | **No.** UC05's local model (`OnboardingSession`) has no link or room; the simulated calendar returns none | — | — | Not fixable without UC05 data. Show nothing (no placeholder) | — | 1 / 10 |
| B1 | Buddy Assignment | buddy_assignment | "Proposed Buddy Id: <colleague employee number>" | The real buddy | **The database row holds the placeholder text** (verified): `buddy_profiles` CLIENT-ALPHA `employee_id = '<colleague employee number>'`, `full_name = '<colleague full name>'`. It came from the SQL template given on 2026-11-05 and was run unedited. UC05 assigned it, and the UI shows it faithfully | Supabase `buddy_profiles` → worker buddy pool → UC05 assignment | No: the display is correct; the data is wrong | Data: HR replaces the row with a real colleague. Guard: the buddy pool ignores profiles whose `employee_id` is not a valid identifier, so a placeholder is never offered as a buddy again. Case `ea04…` already completed with it: re-assigning needs an HR decision (UC05 has no un-assign) | P1 | 2 (+ guard) |
| B2 | Buddy Assignment | buddy_assignment | No name, department or location | Buddy name, department, location, status, approval | Yes: `buddy_profiles.full_name/department/location` (Supabase source) or UC05's candidate `full_name/team` (local) | Worker snapshot passes only `buddy_id` | Yes, at the worker snapshot | Snapshot adds the buddy's profile from the active buddy source. Adapter keeps `buddy_name/department/location`. UC05 renderer | P1 | 3 + 6 |
| B3 | Buddy Assignment | buddy_assignment | — | `assignment_id`, `match_score` | **No** in UC05 local mode (only UC05's Supabase mode writes `buddy_assignments`); the HITL proposal has a confidence | — | — | Not shown. Approval id and status are already kept | — | 1 |
| T1 | Team Introduction | team_introduction | "Session Status: confirmed"; no participants | Participants: id, role, invitation status, RSVP | Yes: UC05 session `attendees` (ids), invitations (`list_invitations`: sent/pending/failed), RSVPs (orchestrator `participant_responses`). Roles are derivable from UC05's record (joiner / `team_lead_id` / `buddy_id`) | Worker snapshot omits attendees and invitations | Yes, at the worker snapshot; the UI has no renderer | Snapshot adds attendees and invitation statuses. Adapter keeps `participants[]` with `participant_id`, `role`, `invitation_status`, `rsvp`. UI renders them | P1 | 3 + 6 |
| T2 | Team Introduction | team_introduction | — | Participant name and email | Partly. Joiner name: state (identity-gated). Buddy name: profile (B2). **Team lead name: no source anywhere** (form and UC05 hold only `TL-TEST-001`). Emails: joiner only, identity-gated | — | — | Names where an authoritative source exists (buddy; joiner for identity readers); otherwise the id. No email shown (not needed; personal data) | P2 | 1 + 7 |
| T3 | Team Introduction | team_introduction | — | Proposed new time, response note | **No**: UC05's `ParticipantResponse` has only the response and time | — | — | Not shown | — | 1 |
| E1 | Equipment & Access (`StageOutputView` equipment_access) | equipment_access | Readiness and item states only | Plus what HR requested (items, who, when) | Yes: `equipment_access_requests` in CaseView | API → UI | Yes: returned, not rendered | Render the recorded requests | P2 | 5 |
| E2 | Equipment & Access | equipment_access | Not shown | Who approved or rejected each item, and when | Yes: `approval_decisions` (agent `dact-uc04-equipment-access`) in CaseView | API → UI | Yes | Render UC04 item decisions | P2 | 5 |
| E3 | Equipment & Access | equipment_access | UC04's advice appears only inside the AT_RISK blocker text | Advice list (loan device, joining date) | Yes: `equipment_access_result.advisories` | API → UI | Yes | Render the advice | P2 | 5 |
| E4 | Equipment & Access | equipment_access | Not shown | Configuration used (role code, version) | Yes: `role_code`, `config_version` | API → UI | Yes | Render | P3 | 5 |
| E5 | Equipment & Access | equipment_access | "Blockers: —" (a field UC04's result never has) | Remove; blockers have their own section | — | `StageOutputView` | Rendering bug | Remove the row | P3 | 6 |
| E6 | Equipment & Access | equipment_access | `INVALID_INPUT` / `POLICY_DENIED` / config errors shown in Activity (stage error) and Blockers; retries "x / y" | — | Yes | `stage_errors`, `blockers`, `retry_metadata` | No | None (verified) | — | — |
| W1 | Joining-Day Welcome | joining_day_welcome | (Fixed earlier today) preview / draft panel | — | Yes; CLIENT-ALPHA `uc05_welcome` is now present (verified) | — | — | None | — | — |
| D1 | Day-1 Readiness: sidebar approval card | day_one_readiness | "orchestrator gate day_one_signoff" | "Day-1 readiness signoff (HR)" | Yes (`gate`) | UI | Rendering | Label the gate as the inbox already does | P3 | 6 |
| D2 | Day-1 Readiness output | day_one_readiness | `verified_context` as "N field(s)" summaries | Readable summary | Yes | UI generic `KeyValues` | Rendering | Show the per-branch readiness facts (low priority) | P3 | 6 |
| F1 | Form approval panel | form_approval | "Withheld for your role" | — | — | Policy | No: deliberate | Keep | — | 7 |

## Where each missing value disappears

```
Orientation end time   UC05 OnboardingSession.scheduled_end ✔ → worker _case_snapshot ✘ (projects start only)
                       → adapter ✘ → orientation_schedule_result ✘ → API ✘ → UI ✘
Timezone               state.case_timezone ✔ (Asia/Dubai) → orientation result ✘ → UI renders raw ISO ✘
Buddy name/dept/loc    buddy_profiles ✔ (Supabase) → worker snapshot ✘ (buddy_id only) → adapter ✘ → UI ✘
Buddy id               buddy_profiles ✘ (placeholder text stored) → … → UI shows it faithfully
Team-intro participants UC05 session.attendees ✔ + invitations ✔ → worker snapshot ✘ → adapter ✘ → UI ✘
                       RSVPs: orchestrator participant_responses ✔ → CaseView ✔ → UI ✘ (not rendered)
Canonical employee no. employee_ref.employee_id ✔ → case_summary / approval_inbox ✘ (use UC01's number) → UI
Approval item / person pending_approvals.description, approver_id ✔ → API ✔ → ApprovalsInbox ✘ (not rendered)
UC04 requests / decisions / advice  CaseView ✔ → StageOutputView ✘ (not rendered)
```

## Not fixable from the orchestrator (data or functionality missing)

- **Meeting link, room** (O2): not in UC05's local data model.
- **Assignment id, match score** (B3): UC05's Supabase mode only.
- **Team lead's name, participant emails, proposed new time, response note** (T2, T3): no authoritative source.
- **The buddy on case `ea04…`** (B1): already assigned from the placeholder row. HR must correct
  `buddy_profiles`; re-assigning that case's buddy needs an HR decision.

## Questions for the product owner

- **Q1:** may the approver on record (team lead, hiring manager) see the joiner's name in the approvals they decide?
  Today only HR roles may (`case_identity_reader_roles`). Proposed default: keep it as is and show permitted
  identifiers.
