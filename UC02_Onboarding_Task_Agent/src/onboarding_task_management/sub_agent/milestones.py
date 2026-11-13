"""Milestone tracking — derived, never stored.

A milestone is a named checkpoint declared in the client's configuration
(:class:`~onboarding_task_management.sub_agent.checklist.MilestoneTemplate`). Which tasks
constitute it is a client decision; this module only does the arithmetic.

State is computed on demand from the current plan plus an explicit ``as_of``
date, exactly like overdue detection in :mod:`tracker`. Nothing is persisted,
so a re-plan that moves the joining date or adds/removes tasks is reflected
immediately with no synchronisation step and no stale-state bug.

Pure functions: no clock reads, no I/O, no network.
"""

from __future__ import annotations

from datetime import date

from onboarding_task_management.calendar_uae import (
    UAE_PUBLIC_HOLIDAYS,
    add_business_days,
    business_days_between,
)
from onboarding_task_management.contracts import (
    MilestoneState,
    MilestoneStatus,
    OnboardingRecord,
    TaskRecord,
    TaskStatus,
)
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    MilestoneTemplate,
)
from onboarding_task_management.sub_agent.planner import make_task_id
from onboarding_task_management.sub_agent.tracker import is_overdue


def make_milestone_id(onboarding_id: str, template_key: str) -> str:
    """Deterministic milestone ID, mirroring ``make_task_id``."""
    return f"{onboarding_id}@{template_key}"


def target_date_for(
    template: MilestoneTemplate,
    joining_date: date,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
) -> date:
    return add_business_days(joining_date, template.offset_days, holidays)


def evaluate(
    record: OnboardingRecord,
    template: MilestoneTemplate,
    as_of: date,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
) -> MilestoneState:
    """Evaluate one milestone against the current plan.

    Status rules, in precedence order:

    * ``ACHIEVED`` — every required task is COMPLETE.
    * ``MISSED``   — not achieved and the target date has passed.
    * ``AT_RISK``  — not achieved, target not yet passed, but at least one
      required task is overdue, cancelled, or absent from the plan.
    * ``PENDING``  — otherwise.

    A required task that is missing from the plan (its template fell out of
    scope on a re-plan) blocks the milestone rather than being ignored: the
    client declared it necessary, so silently dropping it would overstate
    readiness.
    """
    target_date = target_date_for(template, record.joining_date, holidays)

    required_ids: list[str] = []
    completed: list[str] = []
    outstanding: list[str] = []
    overdue: list[str] = []
    blocking: list[str] = []
    missing: list[str] = []

    for key in template.requires:
        task_id = make_task_id(record.onboarding_id, key)
        required_ids.append(task_id)
        task: TaskRecord | None = record.task_by_id(task_id)

        if task is None:
            missing.append(task_id)
            outstanding.append(task_id)
            blocking.append(task_id)
            continue
        if task.status is TaskStatus.COMPLETE:
            completed.append(task_id)
            continue

        outstanding.append(task_id)
        if task.status is TaskStatus.CANCELLED:
            blocking.append(task_id)
        elif is_overdue(task, as_of):
            overdue.append(task_id)
            blocking.append(task_id)

    reasons: list[str] = []
    if not outstanding:
        status = MilestoneStatus.ACHIEVED
    elif target_date < as_of:
        status = MilestoneStatus.MISSED
        reasons.append(
            f"target date {target_date.isoformat()} passed with "
            f"{len(outstanding)} task(s) outstanding"
        )
    elif blocking:
        status = MilestoneStatus.AT_RISK
        if overdue:
            reasons.append(f"{len(overdue)} required task(s) overdue")
        if missing:
            reasons.append(f"{len(missing)} required task(s) not in the plan")
        cancelled = len(blocking) - len(overdue) - len(missing)
        if cancelled > 0:
            reasons.append(f"{cancelled} required task(s) cancelled")
    else:
        status = MilestoneStatus.PENDING

    return MilestoneState(
        milestone_id=make_milestone_id(record.onboarding_id, template.key),
        template_key=template.key,
        onboarding_id=record.onboarding_id,
        client_id=record.client_id,
        employee_id=record.employee_id,
        title=template.title,
        title_ar=template.title_ar,
        target_date=target_date,
        status=status,
        required_task_ids=required_ids,
        completed_task_ids=completed,
        outstanding_task_ids=outstanding,
        overdue_task_ids=overdue,
        blocking_task_ids=blocking,
        days_to_target=business_days_between(as_of, target_date, holidays),
        reasons=reasons,
    )


def evaluate_all(
    record: OnboardingRecord,
    config: ClientOnboardingConfig,
    as_of: date,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
) -> list[MilestoneState]:
    """Evaluate every in-scope milestone, ordered by target date then key."""
    templates = config.applicable_milestones(
        location=record.location,
        project=record.project,
        role_id=record.role_id,
        grade=record.grade,
        attributes=record.attributes,
    )
    states = [evaluate(record, t, as_of, holidays) for t in templates]
    return sorted(states, key=lambda m: (m.target_date, m.template_key))
