"""Mapping rules between UC-04 and the shared tables, in one place. Each rule marked PENDING is an assumption that needs the team's decision;
it is written down and tested so that changing it is a one-line, reviewable change."""

import re

from sub_agent.persistence.errors import UnsupportedParentStatus
from sub_agent.states import ParentCaseStatus

CASE_TABLE = "onboarding_cases"
AUDIT_TABLE = "audit_events"
RUN_TABLE = "agent_runs"

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# onboarding_cases.status -> what UC-04 acts on. The in-progress stages do not change UC-04's rules today. PLAN_PENDING (task planning
# pending) was observed in live onboarding_cases rows on 2026-11-06 and is an in-progress stage like its neighbours.
PARENT_STATUS: dict[str, ParentCaseStatus] = {
    "INTAKE_PENDING": ParentCaseStatus.ACTIVE, "PLAN_PENDING": ParentCaseStatus.ACTIVE, "DOCUMENTS_PENDING": ParentCaseStatus.ACTIVE, "VALIDATION_IN_PROGRESS": ParentCaseStatus.ACTIVE,
    "PROVISIONING_ACTIVE": ParentCaseStatus.ACTIVE, "ORIENTATION_SCHEDULED": ParentCaseStatus.ACTIVE, "READY_FOR_DAY_ONE": ParentCaseStatus.ACTIVE,
    "ON_HOLD": ParentCaseStatus.ON_HOLD, "WITHDRAWN": ParentCaseStatus.WITHDRAWN, "CANCELLED": ParentCaseStatus.CANCELLED,
}
# PENDING decision: COMPLETED has no defined UC-04 behaviour, so it is refused rather than guessed (fail closed). Any unknown value is too.


def parent_status(value: str | None) -> ParentCaseStatus:
    try:
        return PARENT_STATUS[value or ""]
    except KeyError:
        raise UnsupportedParentStatus("parent case status has no defined UC-04 behaviour") from None


def is_uuid(value: object) -> bool:
    return isinstance(value, str) and bool(_UUID.match(value))


# Audit events that record something going wrong. PENDING: the platform's outcome vocabulary beyond the column default (`SUCCESS`) is not defined.
FAILURE_EVENTS = frozenset({"state.rolled_back", "adapter.failed", "caller.rejected", "idempotency.conflict", "case.unknown", "case.mismatch",
                            "case.duplicate", "input.validation_failed", "status.refused"})


def audit_outcome(event_type: str) -> str:
    return "FAILURE" if event_type in FAILURE_EVENTS else "SUCCESS"


def audit_actor_type(context: dict) -> str:
    """PENDING: who counts as the actor of an audit entry. Rule used: a direct caller is a person (HUMAN), the parent workflow is an
    agent (AGENT). The allowed values in the table are HUMAN, AGENT, SYSTEM."""
    return "AGENT" if context.get("invocation_mode") == "PARENT_ORCHESTRATED" else "HUMAN"
