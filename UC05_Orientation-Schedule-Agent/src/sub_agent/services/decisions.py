"""Validation of a human decision BEFORE it is recorded in the HITL inbox.

HITLInbox.decide (hr_onboarding_common) checks the decider's role and tenant and then records the
decision for good; the decision's listener (OnboardingAgent.handle_approval_decision) applies it.
Every rule the listener would refuse a decision on -- the specific team lead or buddy on record,
an ineligible HR-edited buddy, an edited replan time outside office hours, an edited welcome with no
subject or body, a booking or draft that is no longer pending -- used to be found only after the
decision was recorded, so a wrong person (or a malformed edit) consumed the approval and the
legitimate approver could no longer decide it.

validate_decision runs those same rules, with no side effect, before the decision is recorded. Every
entry point that records a decision calls it first: the API's POST /v1/approvals/{id}/decision
(through create_app's decision_validator) and the central orchestrator's decision relay. A refused
decision leaves the approval PENDING and usable. The listener keeps its own checks as a last line of
defence for any caller that records a decision directly on the inbox.

Errors (messages name roles and ids, never a person's name or contact details):
  UnauthorizedApproverError (PermissionError)  not the specific person this approval needs
  InvalidDecisionError (ValueError)            the decision or its edited payload cannot be applied
  DecisionNotApplicableError                   what it decides is no longer pending (cancelled or
                                               timed out, already assigned, already sent)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from hr_onboarding_common.platform.hitl import ApprovalItem, Decision

from sub_agent.models import CommunicationStatus, SessionStatus
from sub_agent.repository import OnboardingPort
from sub_agent.services.scheduling import (
    SESSION_DURATION_MINUTES,
    InvalidMeetingTimeError,
    check_decider,
    validate_session_time,
)

AGENT_ID = "employee-onboarding"


class InvalidDecisionError(ValueError):
    pass


class DecisionNotApplicableError(RuntimeError):
    pass


def validate_decision(port: OnboardingPort, settings: dict, item: ApprovalItem, *, decision: Decision,
                      approver: str, edited_payload: dict[str, Any] | None) -> None:
    """Raises if recording `decision` by `approver` on `item` would be refused when applied. Returns
    None for an item that is not this agent's or not pending (HITLInbox reports those itself)."""
    if item.agent != AGENT_ID or item.status != Decision.PENDING or decision == Decision.PENDING:
        return
    if item.proposal_type == "session_booking":
        check_decider(port, item, approver)
        session = port.get_session(item.payload["id"])
        if session.status != SessionStatus.SCHEDULED:
            raise DecisionNotApplicableError(f"approval {item.id}: the booking is no longer pending "
                                             f"({session.status.value})")
    elif item.proposal_type == "buddy_assignment":
        record = port.get_onboarding_record(item.payload["employee_id"])
        if record.buddy_id:
            raise DecisionNotApplicableError(f"approval {item.id}: a buddy is already assigned")
        if decision == Decision.EDITED:
            edited = edited_payload or {}
            buddy_id = edited.get("buddy_id")
            if edited.get("employee_id", item.payload["employee_id"]) != item.payload["employee_id"]:
                raise InvalidDecisionError(f"approval {item.id}: an edit cannot change the employee")
            eligible = {c.employee_id for c in port.list_buddy_candidates(record.client_id)}
            if not isinstance(buddy_id, str) or buddy_id not in eligible:
                raise InvalidDecisionError(f"approval {item.id}: the edited buddy is not an active buddy candidate "
                                           f"for this client")
    elif item.proposal_type == "welcome_message":
        draft = port.get_communication_draft(item.payload["id"])
        if draft.status != CommunicationStatus.DRAFT:
            raise DecisionNotApplicableError(f"approval {item.id}: the welcome draft is already {draft.status.value}")
        if decision == Decision.EDITED:
            merged = {**item.payload, **(edited_payload or {})}
            if merged.get("id") != item.payload["id"]:
                raise InvalidDecisionError(f"approval {item.id}: an edit cannot point at another draft")
            if not all(isinstance(merged.get(k), str) and merged[k].strip() for k in ("subject", "body")):
                raise InvalidDecisionError(f"approval {item.id}: the edited welcome needs a subject and a body")
    elif item.proposal_type == "attendee_declined" and decision == Decision.EDITED:
        # keep/cancel always apply (a superseded conflict is recorded and ignored by design); a
        # replan needs a valid new start, checked here exactly as apply_attendance_decision would.
        session = port.get_session(item.payload["session_id"])
        try:
            new_start = datetime.fromisoformat(str((edited_payload or {})["new_start"]))
            validate_session_time(settings, new_start, new_start + timedelta(
                minutes=SESSION_DURATION_MINUTES[session.session_type]),
                joining_date=port.get_onboarding_record(session.employee_id).start_date)
        except (KeyError, ValueError, InvalidMeetingTimeError) as e:
            raise InvalidDecisionError(f"approval {item.id}: a replan needs a valid new_start "
                                       f"({type(e).__name__}: {e})") from None
