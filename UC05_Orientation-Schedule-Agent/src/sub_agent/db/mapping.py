"""One place for every fact about how UC05 maps onto the shared Supabase tables: the columns it
relies on (checked at startup), the status vocabularies it writes, how its ids become database ids,
and what may never be written to a shared audit row.

Status values follow the live CHECK constraints, exported from pg_constraint on 2026-11-04 (see
docs/UC05_DATABASE_MAPPING.md). If the database owner changes a constraint, this module is the only
file to change; a mismatch fails loudly with CheckViolation, never silently. Reads accept either case.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

WORKFLOW_MODULE = "uc05_orientation_scheduling"  # approval_requests.workflow_module for UC05 rows
AGENT_ID = "employee-onboarding"
REF_PREFIX = "uc05:"  # audit_events.reference prefix of every row UC05 writes

# Audit actions UC05 uses as durable state where no table exists (see UC05_DATABASE_MAPPING.md).
ACTION_CASE_LINKED = "UC05_CASE_LINKED"
ACTION_OPERATION_STATE = "UC05_OPERATION_STATE"
ACTION_TASK_STATE = "UC05_TASK_STATE"
ACTION_NOTIFICATION = "UC05_NOTIFICATION_RECORDED"

_NS = uuid.UUID("0c05a7e1-5b0d-4f3e-9c55-0c05000c0505")


def stable_uuid(*parts: object) -> str:
    """Deterministic uuid for a UC05 identity, so a retried insert collides on the primary key
    instead of creating a second row."""
    return str(uuid.uuid5(_NS, "|".join(str(p) for p in parts)))


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------- statuses (UC05 value -> column value)
# Verified against the live CHECK constraints (pg_constraint export, 2026-11-04). Where the table's
# vocabulary is coarser than UC05's, the column holds the closest TRUTHFUL value and UC05's exact
# value is kept beside it (orientation_sessions.session_metadata / the communication's audit record).
SESSION_TYPE = {"orientation": "HR_INDUCTION", "induction": "HR_INDUCTION", "team_intro": "DEPARTMENT_WELCOME",
                "buddy_intro": "BUDDY_SYNC"}
SESSION_STATUS = {"scheduled": "SCHEDULED", "confirmed": "SCHEDULED", "cancelled": "CANCELLED"}
PARTICIPANT_ROLE = {"employee": "NEW_HIRE", "team_lead": "MANAGER", "buddy": "BUDDY", "participant": "OBSERVER"}
RSVP_STATUS = {"accepted": "ACCEPTED", "tentative": "TENTATIVE", "declined": "DECLINED", "no_response": "PENDING"}
COMM_TYPE = {"welcome": "WELCOME_EMAIL"}  # UC05 template id -> communications.comm_type
# a SENT draft is DISPATCHED only after a real provider accepted it; a simulated delivery stays APPROVED
COMM_STATUS = {"draft": "DRAFT", "approved": "APPROVED", "rejected": "CANCELLED", "sent": "DISPATCHED"}
COMM_AWAITING_REVIEW = "AWAITING_REVIEW"  # DRAFT once its HR approval request exists
APPROVAL_OUTCOME = {"pending": "PENDING", "approved": "APPROVED", "edited": "EDITED_AND_APPROVED",
                    "rejected": "REJECTED", "withdrawn": "CANCELLED"}
APPROVAL_OUTCOME_READ = {"PENDING": "pending", "APPROVED": "approved", "EDITED_AND_APPROVED": "edited",
                         "REJECTED": "rejected", "CANCELLED": "withdrawn", "EXPIRED": "withdrawn"}
AGENT_RUN_STATUS = {"proposed": "proposed", "acted": "acted", "exception": "exception", "failed": "exception"}
BUDDY_PROPOSED, BUDDY_CONFIRMED, BUDDY_REJECTED = "PROPOSED", "CONFIRMED", "DECLINED"
ACTION_COMMUNICATION_STATE = "UC05_COMMUNICATION_STATE"  # template, language, exact status of a UC05 draft
BUDDY_ACTIVE_STATUSES = (BUDDY_CONFIRMED,)  # what counts against a buddy's max_active_buddies
EFFECTIVE_VERSION_STATUSES = frozenset({"approved", "active", "published"})


def to_db(mapping: Mapping[str, str], value: object) -> str:
    return mapping[str(value)]


def from_db(mapping: Mapping[str, str], value: object) -> str:
    """Column value -> UC05 value (case-insensitive). Unknown values raise ValueError: a row whose
    status UC05 does not understand is never guessed."""
    text = str(value or "")
    reverse = {v.upper(): k for k, v in mapping.items()}
    if text.upper() in reverse:
        return reverse[text.upper()]
    if text.lower() in mapping:
        return text.lower()
    raise ValueError(f"unrecognised status value {text!r}")


# ---------------------------------------------------------------- the columns UC05 depends on
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "onboarding_cases": ("case_id", "client_id", "employee_no", "candidate_name", "candidate_email", "joining_date",
                         "work_location_type", "location_name", "project_name", "role_id", "department",
                         "manager_id", "status", "current_stage", "is_deleted"),
    "case_orchestration_state": ("case_id", "client_id", "workflow_status", "overall_status", "stage_statuses",
                                 "branch_statuses", "pending_actions", "blockers", "stage_outcomes",
                                 "verified_context", "version", "updated_at"),
    "client_configs": ("client_id", "company_name", "working_days", "timezone", "settings_payload", "is_active"),
    "client_holidays": ("client_id", "holiday_date", "name", "is_observed"),
    "client_config_versions": ("client_id", "version", "effective_from", "effective_to", "status", "config_hash"),
    "buddy_profiles": ("buddy_id", "client_id", "employee_id", "full_name", "department", "location",
                       "max_active_buddies", "is_active", "profile_payload"),
    "buddy_assignments": ("assignment_id", "case_id", "client_id", "buddy_employee_id", "status", "match_score",
                          "matching_criteria", "assigned_at", "confirmed_at"),
    "orientation_sessions": ("session_id", "case_id", "client_id", "session_type", "start_utc", "end_utc",
                             "meeting_link", "room_location", "status", "calendar_event_id", "session_metadata",
                             "created_at", "updated_at"),
    "session_participants": ("participant_id", "session_id", "actor_id", "email", "role", "rsvp_status",
                             "proposed_new_time", "response_note", "responded_at"),
    "communications": ("comm_id", "case_id", "client_id", "comm_type", "recipient_email", "subject", "body_text",
                       "body_html", "status", "review_gate_id", "dispatched_at", "created_at"),
    "approval_requests": ("request_id", "case_id", "client_id", "gate", "approver_role", "requested_by",
                          "workflow_module", "subject_ref", "payload", "rationale", "confidence_score", "outcome",
                          "decided_by", "decided_at", "decision_reason_code", "decision_notes", "final_payload",
                          "apply_error", "sla_due_at", "created_at"),
    "audit_events": ("event_id", "case_id", "client_id", "actor_type", "actor_id", "actor_role", "action",
                     "outcome", "detail", "before_state", "after_state", "correlation_id", "reference",
                     "occurred_at"),
    "agent_runs": ("run_id", "case_id", "client_id", "agent_id", "agent_version", "actor_id", "status",
                   "model_name", "prompt_tokens", "completion_tokens", "proposal_count", "action_count",
                   "exception_count", "tool_calls", "error_summary", "started_at", "finished_at"),
}


# ---------------------------------------------------------------- what a shared audit row may carry
SENSITIVE_DETAIL_KEYS = frozenset({
    "body", "subject", "email", "full_name", "name", "recipient_address", "raw_subject", "raw_body", "question",
    "answer", "citations", "note", "payload", "final_payload", "edited_payload", "rationale", "detail_text",
    "token", "access_token", "authorization", "password", "secret", "api_key", "key_value", "candidate_name",
    "candidate_email", "personal_note",
})
_MAX_TEXT = 300


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """Ids, statuses, counts and short codes only: drops personal-data and free-text keys, truncates
    long strings, and bounds nesting. Used on every audit/agent-run detail written to Supabase."""
    if depth > 4:
        return "[truncated]"
    if isinstance(value, Mapping):
        return {str(k): sanitize(v, depth=depth + 1) for k, v in value.items()
                if str(k).lower() not in SENSITIVE_DETAIL_KEYS}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize(v, depth=depth + 1) for v in list(value)[:50]]
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize(str(value), depth=depth + 1)
