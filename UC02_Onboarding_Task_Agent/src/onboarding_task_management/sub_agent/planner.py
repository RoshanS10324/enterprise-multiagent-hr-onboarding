"""Deterministic task-plan generation and re-planning.

Pure functions over records and config. No clock reads except for the
``updated_at`` stamps threaded in by the caller, no I/O, no LLM.
"""

from __future__ import annotations

from datetime import date

from onboarding_task_management.calendar_uae import (
    UAE_PUBLIC_HOLIDAYS,
    add_business_days,
    assert_covered,
)
from onboarding_task_management.contracts import (
    OPEN_STATUSES,
    ChangeDriver,
    OnboardingRecord,
    ReplanOutcome,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from onboarding_task_management.errors import MissingConfigurationError
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    TaskTemplate,
    topological_order,
)


def make_onboarding_id(client_id: str, employee_id: str, attempt: int = 1) -> str:
    """Deterministic onboarding ID for the ``attempt``-th onboarding of an employee.

    The first attempt keeps the bare ``ONB-<client>-<employee>`` form; later
    attempts gain an ``-R<n>`` suffix. This lets a withdrawn candidate be
    re-onboarded (finding M5) without colliding with, or overwriting, the
    cancelled record and its audit history.

    The suffix uses ``-R`` rather than ``#`` so that it cannot be confused with
    the ``<onboarding_id>#<template_key>`` task-ID separator.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    suffix = "" if attempt == 1 else f"-R{attempt}"
    return f"ONB-{client_id}-{employee_id}{suffix}"


def make_task_id(onboarding_id: str, template_key: str) -> str:
    """Deterministic task ID. Stable across re-runs and re-plans."""
    return f"{onboarding_id}#{template_key}"


def due_date_for(
    template: TaskTemplate,
    joining_date: date,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
) -> date:
    return add_business_days(joining_date, template.offset_days, holidays)


def build_tasks(
    record: OnboardingRecord,
    config: ClientOnboardingConfig,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
) -> list[TaskRecord]:
    """Generate the task list for ``record`` from explicit client config."""
    templates = config.applicable(
        location=record.location,
        project=record.project,
        role_id=record.role_id,
        grade=record.grade,
        nationality_group=record.nationality_group,
        attributes=record.attributes,
    )
    if not templates:
        raise MissingConfigurationError(
            "client configuration yields no applicable tasks for this onboarding",
            client_id=record.client_id,
            location=record.location,
            project=record.project,
            role_id=record.role_id,
        )

    in_scope = {t.key for t in templates}
    tasks: list[TaskRecord] = []
    for template in topological_order(templates):
        # Drop prerequisites that fell outside this onboarding's scope, so a
        # task is not blocked forever by a dependency that will never exist.
        dependencies = [
            make_task_id(record.onboarding_id, dep)
            for dep in template.depends_on
            if dep in in_scope
        ]
        tasks.append(
            TaskRecord(
                task_id=make_task_id(record.onboarding_id, template.key),
                template_key=template.key,
                onboarding_id=record.onboarding_id,
                client_id=record.client_id,
                employee_id=record.employee_id,
                category=template.category,
                title=template.title,
                title_ar=template.title_ar,
                owner=template.owner,
                due_date=due_date_for(template, record.joining_date, holidays),
                depends_on=dependencies,
                sensitive_to=list(template.sensitive_to),
                evidence_required=template.evidence_required,
                approval_role=template.approval_role,
                requires_attributes=list(template.requires_attributes),
                priority=template.priority,
                max_failed_reports=template.max_failed_reports,
            )
        )
    compute_planned_starts(tasks, {t.key: t for t in templates}, holidays)
    return recompute_readiness(tasks)


def compute_planned_starts(
    tasks: list[TaskRecord],
    templates: dict[str, TaskTemplate],
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
) -> None:
    """Set each task's earliest start: after its prerequisites are due, and
    ``duration_days`` business days before its own due date.

    Never later than the due date. Settled tasks keep what they had.
    """
    by_id = {t.task_id: t for t in tasks}
    for task in tasks:
        if task.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED):
            continue
        template = templates.get(task.template_key)
        duration = template.duration_days if template is not None else 0
        start = add_business_days(task.due_date, -duration, holidays) if duration else task.due_date
        prerequisite_due = [by_id[d].due_date for d in task.depends_on if d in by_id]
        if prerequisite_due:
            start = max(start, max(prerequisite_due))
        task.planned_start_date = min(start, task.due_date)


def recompute_readiness(tasks: list[TaskRecord]) -> list[TaskRecord]:
    """Set BLOCKED/READY from dependency completion.

    Only touches tasks in BLOCKED or READY. IN_PROGRESS, COMPLETE and CANCELLED
    are terminal-or-owned states that readiness must not silently rewrite.
    """
    by_id = {t.task_id: t for t in tasks}
    for task in tasks:
        if task.status not in (TaskStatus.BLOCKED, TaskStatus.READY):
            continue
        unmet = [
            dep
            for dep in task.depends_on
            if dep in by_id and by_id[dep].status is not TaskStatus.COMPLETE
        ]
        task.status = TaskStatus.BLOCKED if unmet else TaskStatus.READY
    return tasks


def unmet_prerequisites(record: OnboardingRecord, task: TaskRecord) -> list[str]:
    """``task_id`` values blocking ``task``. Empty means startable."""
    return [
        dep
        for dep in task.depends_on
        if (prerequisite := record.task_by_id(dep)) is not None
        and prerequisite.status is not TaskStatus.COMPLETE
    ]


def ready_tasks(record: OnboardingRecord) -> list[TaskRecord]:
    """Tasks whose prerequisites are all met.

    Independent tasks appear here simultaneously and may be progressed in any
    order or concurrently — nothing in this module serialises them.
    """
    return [
        task
        for task in record.tasks
        if task.status is TaskStatus.READY and not unmet_prerequisites(record, task)
    ]


def blocked_tasks(record: OnboardingRecord) -> list[TaskRecord]:
    return [task for task in record.tasks if task.status is TaskStatus.BLOCKED]


def parallel_batches(record: OnboardingRecord) -> list[list[str]]:
    """Dependency-depth layers. Every ``task_id`` within a layer may run in parallel."""
    by_id = {t.task_id: t for t in record.tasks}
    depth: dict[str, int] = {}

    def resolve(task_id: str, seen: frozenset[str] = frozenset()) -> int:
        if task_id in depth:
            return depth[task_id]
        if task_id in seen:  # defensive: config validation already rejects cycles
            return 0
        task = by_id[task_id]
        dependencies = [d for d in task.depends_on if d in by_id]
        value = (
            0
            if not dependencies
            else 1 + max(resolve(d, seen | {task_id}) for d in dependencies)
        )
        depth[task_id] = value
        return value

    for task in record.tasks:
        resolve(task.task_id)

    layers: dict[int, list[str]] = {}
    for task in record.tasks:
        if task.status is TaskStatus.CANCELLED:
            continue
        layers.setdefault(depth[task.task_id], []).append(task.task_id)
    return [layers[key] for key in sorted(layers)]


def _propagate_due_dates(
    tasks: list[TaskRecord], outcome: ReplanOutcome
) -> None:
    """Keep open dependents from falling due before a moved prerequisite.

    A re-plan moves only tasks that declare sensitivity to the changed driver.
    A dependent that does not would keep its old date and could then fall due
    before the prerequisite it waits on. Starting from the tasks this re-plan
    rescheduled or added, push each open dependent forward to its
    prerequisite's due date, transitively. Only dependents of changed tasks are
    touched, so a pre-existing ordering quirk elsewhere in the plan is not
    silently rewritten. Settled tasks never move.
    """
    by_id = {t.task_id: t for t in tasks}
    dependents: dict[str, list[str]] = {}
    for task in tasks:
        for dependency in task.depends_on:
            dependents.setdefault(dependency, []).append(task.task_id)

    queue = list(outcome.rescheduled_task_ids) + list(outcome.added_task_ids)
    while queue:
        prerequisite = by_id.get(queue.pop(0))
        if prerequisite is None or prerequisite.status is TaskStatus.CANCELLED:
            continue
        for dependent_id in dependents.get(prerequisite.task_id, []):
            dependent = by_id[dependent_id]
            if (
                dependent.status not in OPEN_STATUSES
                or dependent.due_date >= prerequisite.due_date
            ):
                continue
            dependent.due_date = prerequisite.due_date
            if dependent_id not in outcome.rescheduled_task_ids and (
                dependent_id not in outcome.added_task_ids
            ):
                outcome.rescheduled_task_ids.append(dependent_id)
                if dependent_id in outcome.retained_task_ids:
                    outcome.retained_task_ids.remove(dependent_id)
            queue.append(dependent_id)


LOCATION_STATUS_PREFIX = "location_status:"


def current_location_status(record: OnboardingRecord) -> str:
    """The location status the employee was declared with, or ``""`` if none was."""
    return next((a.split(":", 1)[1] for a in record.attributes if a.startswith(LOCATION_STATUS_PREFIX)), "")


def replan(
    record: OnboardingRecord,
    config: ClientOnboardingConfig,
    *,
    joining_date: date | None = None,
    location: str | None = None,
    project: str | None = None,
    location_status: str | None = None,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
    add_attributes: tuple[str, ...] = (),
) -> ReplanOutcome:
    """Apply an attribute change and re-plan the affected tasks in place.

    Rules:

    * Completed and cancelled tasks are never reopened, rescheduled or removed —
      work already done stands.
    * Open tasks are rescheduled only if they declare sensitivity to one of the
      drivers that actually changed.
    * Newly-applicable templates are added; open tasks whose template no longer
      applies are removed.

    ``add_attributes``: applicability flags established after planning (for
    example by UC-03's document validation: a passport whose holder needs a
    visa). They switch on the templates that require them; no date moves.
    """
    new_attributes = [a for a in dict.fromkeys(add_attributes) if a and a not in record.attributes]
    drivers: list[ChangeDriver] = []
    if joining_date is not None and joining_date != record.joining_date:
        drivers.append(ChangeDriver.JOINING_DATE)
    if location is not None and location != record.location:
        drivers.append(ChangeDriver.LOCATION)
    if project is not None and project != record.project:
        drivers.append(ChangeDriver.PROJECT)
    if location_status is not None and location_status != current_location_status(record):
        drivers.append(ChangeDriver.LOCATION_STATUS)

    outcome = ReplanOutcome(
        onboarding_id=record.onboarding_id, drivers=drivers, revision=record.revision
    )
    if not drivers and not new_attributes:
        outcome.retained_task_ids = [t.task_id for t in record.tasks]
        return outcome

    # Validate BEFORE mutating. Assigning the new joining date first meant an
    # unsupported date raised from due_date_for only after the record had
    # already been updated, leaving it holding a date the calendar cannot
    # schedule — a re-plan that failed unsafely (finding N3).
    if joining_date is not None:
        assert_covered(joining_date, holidays)

    if joining_date is not None:
        record.joining_date = joining_date
    if location is not None:
        record.location = location
    if project is not None:
        record.project = project
    if location_status is not None and ChangeDriver.LOCATION_STATUS in drivers:
        record.attributes = [a for a in record.attributes if not a.startswith(LOCATION_STATUS_PREFIX)]
        record.attributes.append(f"{LOCATION_STATUS_PREFIX}{location_status}")
    if new_attributes:
        record.attributes = [*record.attributes, *new_attributes]

    templates = {
        t.key: t
        for t in config.applicable(
            location=record.location,
            project=record.project,
            role_id=record.role_id,
            grade=record.grade,
            nationality_group=record.nationality_group,
            attributes=record.attributes,
        )
    }
    if not templates:
        raise MissingConfigurationError(
            "re-plan would leave the onboarding with no applicable tasks",
            client_id=record.client_id,
            location=record.location,
            project=record.project,
        )

    driver_set = set(drivers)
    surviving: list[TaskRecord] = []

    for task in record.tasks:
        settled = task.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED)
        if task.template_key not in templates:
            if settled:
                # Already done or already cancelled: keep the audit trail.
                surviving.append(task)
                outcome.retained_task_ids.append(task.task_id)
            else:
                outcome.removed_task_ids.append(task.task_id)
            continue

        surviving.append(task)
        template = templates[task.template_key]
        if settled or not (driver_set & set(task.sensitive_to)):
            outcome.retained_task_ids.append(task.task_id)
            continue

        new_due = due_date_for(template, record.joining_date, holidays)
        if new_due != task.due_date:
            task.due_date = new_due
            outcome.rescheduled_task_ids.append(task.task_id)
        else:
            outcome.retained_task_ids.append(task.task_id)

    existing_keys = {t.template_key for t in surviving}
    in_scope = set(templates)
    for template in topological_order(tuple(templates.values())):
        if template.key in existing_keys:
            continue
        task = TaskRecord(
            task_id=make_task_id(record.onboarding_id, template.key),
            template_key=template.key,
            onboarding_id=record.onboarding_id,
            client_id=record.client_id,
            employee_id=record.employee_id,
            category=template.category,
            title=template.title,
            title_ar=template.title_ar,
            owner=template.owner,
            due_date=due_date_for(template, record.joining_date, holidays),
            depends_on=[
                make_task_id(record.onboarding_id, dep)
                for dep in template.depends_on
                if dep in in_scope
            ],
            sensitive_to=list(template.sensitive_to),
            evidence_required=template.evidence_required,
            approval_role=template.approval_role,
            requires_attributes=list(template.requires_attributes),
            priority=template.priority,
            max_failed_reports=template.max_failed_reports,
        )
        surviving.append(task)
        outcome.added_task_ids.append(task.task_id)

    # Drop dependencies pointing at tasks that no longer exist.
    surviving_ids = {t.task_id for t in surviving}
    for task in surviving:
        if task.status in OPEN_STATUSES:
            task.depends_on = [d for d in task.depends_on if d in surviving_ids]

    _propagate_due_dates(surviving, outcome)
    compute_planned_starts(surviving, templates, holidays)

    record.tasks = recompute_readiness(surviving)
    record.revision += 1
    record.updated_at = utc_now()
    outcome.revision = record.revision
    return outcome
