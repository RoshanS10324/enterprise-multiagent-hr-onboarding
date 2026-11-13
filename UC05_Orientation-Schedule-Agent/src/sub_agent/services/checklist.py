"""Onboarding checklist service: deterministic business rules layered on top of OnboardingPort.
No LLM is involved anywhere in this module -- which required tasks exist and idempotency are
plain code, never a model's say-so.

Called through OnboardingToolRegistry (policy allowlist, kill switch, tenant checks, audit) by both
the onboarding workflow (agent.py, node prepare_checklist) and the /v1/onboarding/* routes. These
functions take their own AuditLog so they stay independently testable.
"""

from __future__ import annotations

from datetime import date

from hr_onboarding_common.platform.audit import AuditLog
from sub_agent.models import OnboardingTask, TaskStatus
from sub_agent.repository import OnboardingPort

# What is required is a business decision made in code, never inferred by a model. Per-client
# overrides can be layered on later (e.g. from ClientConfig) without changing any caller.
REQUIRED_TASK_TYPES: tuple[str, ...] = (
    "orientation_scheduling", "induction_scheduling", "team_introduction", "buddy_assignment",
    "it_setup",
)


# --------------------------------------------------------------------------- checklist (tasks)

def ensure_required_tasks(port: OnboardingPort, audit: AuditLog, *, employee_id: str, client_id: str,
                          due_date: date | None = None) -> list[OnboardingTask]:
    """Idempotent: create whichever required tasks are missing; leave existing ones untouched.

    Safe to call on every request for the same employee -- repeated calls never create duplicates.
    A cancelled task does not count as existing, so it is recreated (matches OnboardingPort.create_task).
    """
    existing_types = {t.task_type for t in port.list_tasks(employee_id) if t.status != TaskStatus.CANCELLED}
    for task_type in REQUIRED_TASK_TYPES:
        if task_type in existing_types:
            continue
        task = port.create_task(OnboardingTask(employee_id=employee_id, client_id=client_id,
                                               task_type=task_type, due_date=due_date))
        audit.record("checklist.task_created", agent="employee-onboarding", subject_id=employee_id,
                     task_id=task.id, task_type=task_type)
    return port.list_tasks(employee_id)


def task_status(port: OnboardingPort, employee_id: str) -> list[OnboardingTask]:
    return port.list_tasks(employee_id)


def pending_tasks(port: OnboardingPort, employee_id: str) -> list[OnboardingTask]:
    return [t for t in port.list_tasks(employee_id) if t.status == TaskStatus.PENDING]


def overdue_tasks(port: OnboardingPort, employee_id: str, *, as_of: date) -> list[OnboardingTask]:
    """Pending tasks whose due_date has passed as_of. Tasks without a due_date are never overdue."""
    return [t for t in pending_tasks(port, employee_id) if t.due_date is not None and t.due_date < as_of]
