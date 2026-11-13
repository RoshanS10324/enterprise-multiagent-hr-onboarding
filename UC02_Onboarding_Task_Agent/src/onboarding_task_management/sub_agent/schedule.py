"""Critical path and downstream impact of a delay. Pure functions over a stored record; nothing is changed.

Definitions (stated so they can be checked, not assumed)
* A task is *open* when its status is in ``OPEN_STATUSES``.
* The **critical path** is the chain of open tasks that leaves the least room before day one: among open
  tasks due on or before joining that no other open task depends on (terminals), take the one with the fewest business days between its
  due date and the joining date (its *slack*; negative = due after joining), then walk back, at each step to the
  open prerequisite with the latest due date. Ties break on ``task_id`` so the answer is stable.
* The **impact** of a late task is every open task that depends on it, directly or transitively, with the
  business days left before joining. It describes the consequence; it does not move any date.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from onboarding_task_management.calendar_uae import UAE_PUBLIC_HOLIDAYS, business_days_between
from onboarding_task_management.contracts import OPEN_STATUSES, OnboardingRecord, TaskRecord


class CriticalPath(BaseModel):
    onboarding_id: str
    task_ids: list[str] = Field(default_factory=list)
    #: Business days between the terminal task's due date and joining (negative = due after joining).
    slack_days: int | None = None


class DelayImpact(BaseModel):
    task_id: str
    template_key: str
    dependent_task_ids: list[str] = Field(default_factory=list)
    days_to_joining: int = 0
    #: True when a dependent is a day-one-critical category or the delayed task itself sits on the critical path.
    on_critical_path: bool = False


def _open(record: OnboardingRecord) -> dict[str, TaskRecord]:
    return {t.task_id: t for t in record.tasks if t.status in OPEN_STATUSES}


def _dependents(record: OnboardingRecord) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {t.task_id: [] for t in record.tasks}
    for t in record.tasks:
        for dep in t.depends_on:
            out.setdefault(dep, []).append(t.task_id)
    return out


def critical_path(record: OnboardingRecord, holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS) -> CriticalPath:
    open_tasks = _open(record)
    if not open_tasks:
        return CriticalPath(onboarding_id=record.onboarding_id)
    dependents = _dependents(record)
    terminals = [t for t in open_tasks.values() if not any(d in open_tasks for d in dependents.get(t.task_id, []))]

    # Work due after joining cannot put day one at risk, so it is not a candidate end of the critical path
    # (unless nothing is due before joining).
    terminals = [t for t in terminals if t.due_date <= record.joining_date] or terminals

    def slack(task: TaskRecord) -> int:
        return business_days_between(task.due_date, record.joining_date, holidays)

    end = min(terminals, key=lambda t: (slack(t), -t.due_date.toordinal(), t.task_id))
    chain = [end]
    while True:
        prereqs = [open_tasks[d] for d in chain[-1].depends_on if d in open_tasks]
        if not prereqs:
            break
        chain.append(max(prereqs, key=lambda t: (t.due_date.toordinal(), t.task_id)))
    chain.reverse()
    return CriticalPath(onboarding_id=record.onboarding_id, task_ids=[t.task_id for t in chain],
                        slack_days=slack(end))


def delay_impact(record: OnboardingRecord, task_id: str, as_of: date,
                 holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS) -> DelayImpact:
    task = record.task_by_id(task_id)
    if task is None:
        raise KeyError(task_id)
    open_tasks = _open(record)
    dependents = _dependents(record)
    seen: list[str] = []
    stack = list(dependents.get(task_id, []))
    while stack:
        nxt = stack.pop()
        if nxt in open_tasks and nxt not in seen:
            seen.append(nxt)
            stack.extend(dependents.get(nxt, []))
    path = critical_path(record, holidays)
    return DelayImpact(
        task_id=task_id, template_key=task.template_key, dependent_task_ids=sorted(seen),
        days_to_joining=business_days_between(as_of, record.joining_date, holidays),
        on_critical_path=task_id in path.task_ids,
    )


__all__ = ["CriticalPath", "DelayImpact", "critical_path", "delay_impact"]
