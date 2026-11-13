# UC05: how it maps onto the shared Supabase tables

This document covers every table UC05 uses in Supabase mode, what it reads and writes there, and which schema facts are still unconfirmed. The design and setup are in [UC05_SUPABASE_INTEGRATION.md](UC05_SUPABASE_INTEGRATION.md). All mapping constants are in [mapping.py](../src/sub_agent/db/mapping.py), and the repository code is [db/repository.py](../src/sub_agent/db/repository.py).

## Where the schema facts come from

The columns, types and primary keys of the shared tables come from the captured live schema in `UC02_Onboarding_Task_Agent/tests/fixtures/live_schema.json` and `UC04_.../tests/fixtures/live_schema_subset.json`. Those cover `onboarding_cases`, `approval_requests`, `audit_events`, `agent_runs`, `client_configs`, `client_config_versions` and `client_holidays`. The `audit_events.actor_type` CHECK (`HUMAN`/`AGENT`/`SYSTEM`) was verified by UC02.

For `buddy_profiles`, `buddy_assignments`, `orientation_sessions`, `session_participants`, `communications` and `case_orchestration_state`, the column list comes from the task brief. Their unique constraints, foreign keys and RLS policies have still not been seen.

At startup, `factory.verify_schema` confirms that every listed column exists before UC05 accepts work. On 2026-11-04 this check passed against the live project.

## Verified CHECK constraints (live export, 2026-11-04)

The CHECK constraints were exported from `pg_constraint` on the live project. UC05 writes only these values (`mapping.py`), and the test fake (`tests/fake_postgrest.py`) enforces the same constraints, so CI rejects any value the database would.

| Table.column | Allowed values | What UC05 writes |
|---|---|---|
| `orientation_sessions.session_type` | HR_INDUCTION, IT_BOOTCAMP, BUDDY_SYNC, DEPARTMENT_WELCOME, LEADERSHIP_MEET | orientation and induction → HR_INDUCTION; team_intro → DEPARTMENT_WELCOME; buddy_intro → BUDDY_SYNC. The exact type is kept in `session_metadata.uc05_session_type`. |
| `orientation_sessions.status` | SCHEDULED, IN_PROGRESS, COMPLETED, CANCELLED, RESCHEDULED | scheduled and HR-confirmed → SCHEDULED (there is no "confirmed" value); cancelled → CANCELLED. The exact status is kept in `session_metadata.uc05_status`. |
| `orientation_sessions` (`chk_session_times`) | `end_utc > start_utc` | always (UC05's own model enforces it) |
| `session_participants.role` | NEW_HIRE, HOST, PRESENTER, BUDDY, MANAGER, OBSERVER | NEW_HIRE, MANAGER (team lead), BUDDY, OBSERVER (anyone else) |
| `session_participants.rsvp_status` | PENDING, ACCEPTED, TENTATIVE, DECLINED, DELEGATED | ACCEPTED, TENTATIVE, DECLINED, PENDING (no response) |
| `buddy_assignments.status` | PROPOSED, CONFIRMED, DECLINED, REPLACED, COMPLETED | PROPOSED, CONFIRMED, DECLINED (HR rejected the proposal) |
| `buddy_profiles.max_active_buddies` | ≥ 1 | read only |
| `communications.comm_type` | WELCOME_EMAIL, SESSION_INVITE, SLACK_INTRO, TEAMS_BROADCAST, SURVEY_PROMPT, URGENT_NOTICE | WELCOME_EMAIL (the `welcome` template) |
| `communications.status` | DRAFT, AWAITING_REVIEW, APPROVED, DISPATCHED, FAILED, CANCELLED | DRAFT → AWAITING_REVIEW (once its HR approval exists) → APPROVED → DISPATCHED (only after a real provider accepted it; a simulated delivery stays APPROVED, with no `dispatched_at`); rejected → CANCELLED. Template, language and exact status are kept in a `UC05_COMMUNICATION_STATE` record in `audit_events`. |
| `approval_requests.outcome` | PENDING, APPROVED, EDITED_AND_APPROVED, REJECTED, CANCELLED, EXPIRED | PENDING, APPROVED, EDITED_AND_APPROVED, REJECTED, CANCELLED (withdrawn) |
| `approval_requests` (`chk_decided_has_timestamp`, `chk_no_self_approval`) | a decided row has `decided_at`; `decided_by` ≠ `requested_by` | `decided_at` is set on every decision; `requested_by` is the agent and `decided_by` the person |
| `audit_events.actor_type` | HUMAN, AGENT, SYSTEM | as listed |
| `audit_events.action` | non-empty | always |
| `agent_runs.status` | proposed, acted, exception (lowercase) | proposed, acted, exception (a run that raised → exception) |

## Rules that apply to every table

- **Tenant filter.** Every read, update and delete on a tenant table carries `client_id=eq.<verified tenant>`.
- **UC05's own rows.** Only rows UC05 marked as its own are read back. The markers, by table:

  | Table | Marker |
  |---|---|
  | `orientation_sessions` | `session_metadata.owner = 'uc05'` |
  | `communications` | `comm_type` starting `uc05:` |
  | `approval_requests` | `workflow_module = 'uc05_orientation_scheduling'` |
  | `audit_events` | `reference` starting `uc05:` |

- **Status values.** UC05 writes the uppercase values listed in `mapping.py`. They have not been checked against the CHECK constraints (see the open questions).

## Tables

### `onboarding_cases` (owned by UC01; UC05 only reads it)

- **Columns:** `case_id`, `client_id`, `employee_no`, `candidate_name`, `candidate_email`, `joining_date`, `role_id`, `department`, `manager_id`, `status`, `current_stage`, `is_deleted`, `location_name`, `opened_at`.
- **Methods:**
  - `get_onboarding_record`
  - `list_onboarding_records` (only the cases UC05 has linked)
  - `case_id_for`
  - `create_onboarding_case`: links UC05 to an existing case and never inserts one
  - `update_case_status`: always refused with `CaseOwnershipError`
  - `SupabaseEmployeeDirectory.get_employee`
  - `SupabaseCaseEventSource.get_event`
- **Reads:** by (`client_id`, `employee_no`), or by (`client_id`, `case_id`). The joining date is read fresh on every call.
- **Writes:** none.
- **Tenant filter:** always.
- **Idempotency:** not applicable.
- **How UC05's employee fields are filled:**

  | UC05 field | Column |
  |---|---|
  | `employee_id` | `employee_no` (the canonical id under the shared identity scheme) |
  | `team` | `department` |
  | `team_lead_id` | `manager_id` |
  | `role_title` | `role_id` |
  | `start_date` | `joining_date` |

  The case status becomes UC05's case status as follows: `is_deleted` → WITHDRAWN; `CANCELLED`/`CANCELED` → CANCELLED; `WITHDRAWN`/`OFFER_WITHDRAWN`/`OFFER_DECLINED` → WITHDRAWN; anything else → ACTIVE. A missing value stays empty, and intake then flags the case as incomplete.
- **Open questions:**
  - Is `employee_no` unique per client (UC01's design proposes `UNIQUE(client_id, employee_no)`)? UC05 refuses to guess when two live cases match (`AmbiguousCaseError`).
  - The full `status` vocabulary.
  - There is no `team` column, so `department` stands in for it.

### `case_orchestration_state` (owned by the orchestrator; UC05 only reads it)

- **Columns:** the ones listed in the brief.
- **Methods:** `get_orchestration_state`.
- **Reads:** by (`case_id`, `client_id`).
- **Writes:** none. The orchestrator records UC05's outcome from the structured result UC05 returns.
- **Open question:** the orchestrator in this repository persists to its own SQLite store, so rows may not exist yet.

### `client_configs`

- **Columns:** `client_id`, `company_name`, `working_days` (1 = Monday … 7 = Sunday), `timezone`, `settings_payload`, `is_active`.
- **Methods:**
  - `get_client_config`: builds the welcome configuration from `company_name` plus `settings_payload.uc05_welcome`
  - `get_client_calendar`
  - `list_client_ids`: active tenants, for the scheduler
- **Reads:** by `client_id`.
- **Writes:** none.
- **Missing or invalid data:** an inactive or missing client means no welcome and no calendar. Nothing is defaulted.
- **Open question:** no welcome templates exist in any client's configuration, because the client-configuration profiles have no UC05 welcome section. Until `settings_payload.uc05_welcome` is populated, the welcome step stops at `welcome_not_configured`. The expected shape is UC05's `ClientConfig`: `enabled_languages`, `default_language`, `branding`, and `templates`.

### `client_config_versions`

- **Columns:** `client_id`, `version`, `effective_from`, `effective_to`, `status`, `config_hash`.
- **Methods:** `get_client_calendar`.
- **Read rule:** a calendar is used only while a version with status `approved`, `active` or `published` is in effect today. Otherwise `ClientCalendarUnavailableError` is raised and scheduling goes to HR.
- **Writes:** none.
- **Open questions:**
  - The status vocabulary. UC02 observed only `draft`.
  - Whether the configuration owners approve any versions. Until they do, live scheduling is blocked by design.

### `client_holidays`

- **Columns:** `client_id`, `holiday_date`, `name`, `is_observed`.
- **Methods:** `get_client_calendar`.
- **Reads:** every holiday of the tenant where `is_observed` is not false.
- **Writes:** none.
- **Open question:** a holiday row has no version, so a holiday applies to every configuration version.

### `buddy_profiles`

- **Columns:** `buddy_id`, `client_id`, `employee_id`, `full_name`, `department`, `location`, `max_active_buddies`, `is_active`, `profile_payload`.
- **Methods:**
  - `list_buddy_candidates`: active profiles that still have capacity
  - `assign_buddy`: confirms the profile is still active
- **Writes:** none.
- **Matching:** UC05's existing rule is unchanged: same team (`department`) first, then any eligible candidate.
- **Open question:** `max_active_buddies` set to null is treated as no limit.

### `buddy_assignments`

- **Columns:** `assignment_id`, `case_id`, `client_id`, `buddy_employee_id`, `status`, `match_score`, `matching_criteria`, `assigned_at`, `confirmed_at`.
- **Methods:** `record_buddy_proposal`, `release_buddy_proposal`, `assign_buddy`, `get_buddy_assignment`, and the buddy shown on the employee's record.
- **Writes:**

  | Event | Row |
  |---|---|
  | Buddy proposed | `PROPOSED`, with `match_score` set to the existing rule's confidence (0.9 same team, 0.6 otherwise) and `matching_criteria` = `{rule, same_team, approval_id}` |
  | HR approves | `CONFIRMED`, with `assigned_at` and `confirmed_at` |
  | HR rejects | `REJECTED` |

  A confirmed assignment is never downgraded, and reassignment only happens through a conditional update.
- **Idempotency:** the unique `case_id` gives one row per case. The insert id is deterministic, and updates are conditional on the status and buddy UC05 read.
- **Capacity:** after writing, UC05 counts the buddy's active (`CONFIRMED`) assignments. If that exceeds `max_active_buddies`, the change is rolled back and `BuddyCapacityExceededError` is raised.
- **Open questions:** the status CHECK values, and whether `case_id` is a foreign key to `onboarding_cases`.

### `orientation_sessions`

- **Columns:** `session_id`, `case_id`, `client_id`, `session_type`, `start_utc`, `end_utc`, `meeting_link`, `room_location`, `status`, `calendar_event_id`, `session_metadata`, `created_at`, `updated_at`.
- **Methods:**
  - Create and read: `create_session`, `get_session`, `list_sessions`
  - Change: `update_session_status`, `cancel_session`, `update_session_time`
  - Queries: `list_sessions_in_range`, `find_conflicting_sessions`, `stale_sessions`
- **Writes:**
  - `start_utc` and `end_utc` are stored in UTC. Scheduling validates them in the client's timezone.
  - Statuses are `SCHEDULED`, `CONFIRMED` and `CANCELLED`. Session types are stored as UC05's values (`orientation`, `induction`, `team_intro`, `buddy_intro`).
  - `session_metadata` holds `{owner: 'uc05', uc05_session_id, employee_id, attendees, booking_operation_key, joining_date_basis, invitations}`.
- **Idempotency and conflicts:**
  - `session_id` is derived deterministically from UC05's id.
  - The same type at the same start raises `DuplicateSessionError`.
  - Any overlap with the employee's live sessions raises `SessionConflictError`.
  - Updates are conditional on `updated_at`.
- **Open questions:**
  - No database constraint prevents conflicts, so the checks run in the application. A partial unique index `(case_id, session_type, start_utc) WHERE status <> 'CANCELLED'` is recommended.
  - The status and session type CHECK values.
  - `meeting_link` and `room_location` are left empty, because the simulated calendar provides neither.

### `session_participants`

- **Columns:** `participant_id`, `session_id`, `actor_id`, `email`, `role`, `rsvp_status`, `proposed_new_time`, `response_note`, `responded_at`.
- **Methods:** `record_invitation`, `record_participant_response`, `list_participant_responses`, `list_session_participants`.
- **Writes:**
  - A participant row is created when the invitation is recorded. Its id is derived from the session and actor.
  - `role` is `employee`, `team_lead`, `buddy` or `participant`.
  - `email` is set for the new hire only.
  - RSVPs are kept newest-first: the update applies only if `responded_at` is unchanged since it was read.
- **Tenant isolation:** the table has no `client_id`, so the session is always read with the tenant filter first.
- **Open question:** the `rsvp_status` CHECK values, and whether an unanswered row needs a default for `rsvp_status`. UC05 leaves it null until someone replies.

### `communications`

- **Columns:** `comm_id`, `case_id`, `client_id`, `comm_type`, `recipient_email`, `subject`, `body_text`, `body_html`, `status`, `review_gate_id`, `dispatched_at`, `created_at`.
- **Methods:** `create_communication_draft`, `get_communication_draft`, `list_communication_drafts`, `update_communication_status`, `link_review_gate`.
- **Writes:**
  - `comm_type` is `uc05:<template>:<language>`.
  - Statuses are `DRAFT`, `APPROVED`, `REJECTED` and `SENT`, plus `SENT_SIMULATED` (below).
  - `review_gate_id` is set to the `approval_requests.request_id` that gates the message.
- **Dispatch:** a message is marked `SENT` with `dispatched_at` only when the delivery ledger shows a real, non-simulated provider accepted it. While delivery is simulated it is `SENT_SIMULATED` with no `dispatched_at`, so UC05 never resends it and the table never claims an email left the system.
- **Idempotency:** `comm_id` is deterministic, and a second live (not rejected) message of the same template for the case raises `DuplicateCommunicationError`.
- **Open questions:**
  - The `status` and `comm_type` CHECK values. `SENT_SIMULATED` may need to be added.
  - There is no language column, so the language is carried in `comm_type`.

### `approval_requests` (written through `SupabaseHITLInbox`)

- **Columns:** all of the listed columns.
- **Mapping:**

  | Column | Value |
  |---|---|
  | `request_id` | the approval id |
  | `requested_by` | the agent id |
  | `subject_ref` | the employee id |
  | `outcome` | `PENDING` / `APPROVED` / `REJECTED` / `WITHDRAWN`; an edited decision is stored as `APPROVED`, with the edit in `final_payload` |
  | `sla_due_at` | `created_at` + `sla_hours` |
  | `payload` | `{proposal, uc05: {workflow_instance, agent, proposal_type, run_id, sla_hours, status, …}}` |

- **Reads:** by `request_id`, `client_id` and `workflow_module`.
- **Decide once:** a decision is written only if `outcome` is still `PENDING`. A decision another process already recorded raises `HITLError`, and the approval is never applied twice.
- **Open questions:**
  - The `outcome` and `gate` CHECK values. `WITHDRAWN` may not be allowed, though UC05 itself never withdraws.
  - Whether `case_id` is a foreign key.

### `audit_events`

- **Methods:**
  - `SupabaseAuditLog.record`: every tenant-scoped event
  - The repository's state rows: `UC05_CASE_LINKED`, `UC05_TASK_STATE`, `UC05_OPERATION_STATE` and `UC05_NOTIFICATION_RECORDED`
- **What a row holds:**
  - `actor_type`: `HUMAN`, `AGENT` or `SYSTEM`
  - `action`: the event name
  - `outcome`: `SUCCESS`, `FAILURE` or `DENIED`
  - `correlation_id`: the run, approval or workflow id
  - `detail`: sanitised, so ids, statuses and counts only
- **Versioned state:** the state rows carry `detail = {entity_id, version}` and the record in `after_state`. Version *n* has a deterministic `event_id`, so two writers can never both write version *n*.
- **Events in the trail:**

  | Event | Name |
  |---|---|
  | Buddy proposed | `scheduling.buddy_assignment_proposed` |
  | Buddy assigned | `scheduling.buddy_assigned` |
  | Session booked | `scheduling.session_booked` |
  | Reschedule | `scheduling.attendance_decision_applied` |
  | RSVP | `scheduling.participant_response_recorded` |
  | Approval requested and decided | `hitl.submitted`, `hitl.decided` |
  | Welcome drafted | `welcome.draft_submitted` |
  | Run completed or failed | `workflow.completed`, `workflow.failed` |

- **Open question:** UC01 needs hash-chain columns (`sequence`, `prev_hash`, `entry_hash`) for its own audit. UC05 does not use them.

### `agent_runs` (`AgentRunRecorder`)

- **What is recorded:** one row per run, from the API, the orchestrator worker or the approval listener. It holds `run_id`, `case_id`, `client_id`, agent id and version, actor, `status` (`PROPOSED`/`ACTED`/`EXCEPTION`/`FAILED`), `model_name`, `tool_calls`, proposal/action/exception counts, an `error_summary` that has been sanitised, and the start and finish times.
- **Token counts:** not recorded. Common's LLM client does not expose them.
- **Idempotency:** `run_id` is the primary key, so a duplicate is ignored.

## UC05 state with no table of its own

| UC05 record | Home | Recommended table if the database owner approves |
|---|---|---|
| Checklist tasks | `audit_events` `UC05_TASK_STATE` (versioned) | `uc05_checklist_tasks` (`onboarding_tasks` belongs to UC02 and requires its templates) |
| Idempotency ledger (calendar bookings, notifications) | `audit_events` `UC05_OPERATION_STATE` | `uc05_operations(key PK, client_id, status, …)` |
| Notification delivery records | `audit_events` `UC05_NOTIFICATION_RECORDED` | none needed |
| Invitation send status | `orientation_sessions.session_metadata.invitations` | an `invitation_status` column on `session_participants` |
| LangGraph checkpoints, joining-day job leases | local SQLite (`HR_AGENT_DATA_DIR`) | a shared checkpoint store or table, needed only for more than one host |
