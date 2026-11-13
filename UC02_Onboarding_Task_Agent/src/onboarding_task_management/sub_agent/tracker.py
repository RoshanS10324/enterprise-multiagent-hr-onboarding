"""Task status transitions, overdue detection and joining-date risk.

Authorisation and prerequisite enforcement live here because both are
preconditions of a state change — they must not be bypassable by writing
``task.status`` from elsewhere.
"""

from __future__ import annotations

from datetime import date

from onboarding_task_management.calendar_uae import (
    UAE_PUBLIC_HOLIDAYS,
    business_days_between,
)
from onboarding_task_management.config import (
    DAY_ONE_CRITICAL_CATEGORIES,
    ESCALATION_RESPONSE_DAYS,
    JOINING_RISK_DAYS_BEFORE,
)
from onboarding_task_management.contracts import (
    ADMIN_ROLES,
    OPEN_STATUSES,
    Actor,
    EscalationItem,
    EscalationLevel,
    FollowUpFlag,
    JoiningRisk,
    OnboardingRecord,
    OnboardingStatus,
    OutstandingActivity,
    Role,
    SourceUseCase,
    TaskRecord,
    TaskStatus,
    TaskSummary,
    utc_now,
)
from onboarding_task_management.errors import (
    DataIsolationError,
    EvidenceRequiredError,
    InvalidStateError,
    PrerequisiteNotMetError,
    UnauthorizedActionError,
)
from onboarding_task_management.sub_agent.planner import (
    recompute_readiness,
    unmet_prerequisites,
)


def assert_same_client(actor: Actor, record: OnboardingRecord) -> None:
    """Data isolation: an actor may only reach their own client's records."""
    if actor.client_id != record.client_id:
        raise DataIsolationError(
            "actor is not permitted to access another client's onboarding",
            actor_client_id=actor.client_id,
            record_client_id=record.client_id,
            onboarding_id=record.onboarding_id,
        )


def assert_can_act_on(actor: Actor, task: TaskRecord) -> None:
    """Only the task owner or an admin role may change a task's state."""
    if actor.role in ADMIN_ROLES or actor.role is task.owner:
        return
    raise UnauthorizedActionError(
        "actor role may not act on this task",
        actor_role=str(actor.role),
        task_owner=str(task.owner),
        task_id=task.task_id,
    )


def _get_task(record: OnboardingRecord, task_id: str) -> TaskRecord:
    task = record.task_by_id(task_id)
    if task is None:
        raise InvalidStateError("unknown task", task_id=task_id)
    return task


def start_task(actor: Actor, record: OnboardingRecord, task_id: str) -> TaskRecord:
    """Move a task to IN_PROGRESS, enforcing isolation, authz and prerequisites."""
    assert_same_client(actor, record)
    task = _get_task(record, task_id)
    assert_can_act_on(actor, task)

    if record.status is not OnboardingStatus.ACTIVE:
        raise InvalidStateError(
            "onboarding is not active", onboarding_status=str(record.status)
        )
    if task.status is TaskStatus.CANCELLED:
        raise InvalidStateError("task is cancelled", task_id=task_id)
    if task.status is TaskStatus.COMPLETE:
        raise InvalidStateError("task is already complete", task_id=task_id)

    unmet = unmet_prerequisites(record, task)
    if unmet:
        raise PrerequisiteNotMetError(
            "task cannot start until its prerequisites are complete",
            task_id=task_id,
            unmet=",".join(sorted(unmet)),
        )

    task.status = TaskStatus.IN_PROGRESS
    task.started_at = utc_now()
    record.updated_at = utc_now()
    return task


def complete_task(
    actor: Actor,
    record: OnboardingRecord,
    task_id: str,
    evidence_ref: str | None = None,
) -> TaskRecord:
    """Mark a task COMPLETE and unblock whatever depended on it.

    UC02-BR-12: when the task's configuration sets ``evidence_required``, a
    non-blank ``evidence_ref`` (document reference, ticket id or confirmation
    id) must be supplied. Completion is refused otherwise - the rule is
    "do not mark a task complete without required evidence or confirmation".
    """
    assert_same_client(actor, record)
    task = _get_task(record, task_id)
    assert_can_act_on(actor, task)

    if record.status is not OnboardingStatus.ACTIVE:
        raise InvalidStateError(
            "onboarding is not active", onboarding_status=str(record.status)
        )
    if task.status is TaskStatus.CANCELLED:
        raise InvalidStateError("task is cancelled", task_id=task_id)
    if task.status is TaskStatus.COMPLETE:
        # Re-completion overwrote completed_at and emitted a second
        # "task.complete" audit event, corrupting the record (finding M4).
        raise InvalidStateError("task is already complete", task_id=task_id)

    unmet = unmet_prerequisites(record, task)
    if unmet:
        raise PrerequisiteNotMetError(
            "task cannot complete until its prerequisites are complete",
            task_id=task_id,
            unmet=",".join(sorted(unmet)),
        )

    if task.evidence_required and not (evidence_ref or "").strip():
        raise EvidenceRequiredError(
            "task requires completion evidence or confirmation",
            task_id=task_id,
            template_key=task.template_key,
        )

    task.status = TaskStatus.COMPLETE
    task.completed_at = utc_now()
    task.delay_reason, task.delay_kind = "", ""  # resolved; the audit log keeps the history
    if evidence_ref and evidence_ref.strip():
        task.evidence_ref = evidence_ref.strip()
        task.evidence_recorded_at = utc_now()
    record.tasks = recompute_readiness(record.tasks)
    record.updated_at = utc_now()
    return task


# ---------------------------------------------------------------------------
# UC02-BR-03 - HR manual owner assignment
#
# HR assigns the responsible individual. There is no owner-assignment agent
# and no model involvement: the assignee is supplied by an HR actor and
# recorded verbatim. UC-02 validates and tracks; it never chooses.
# ---------------------------------------------------------------------------


def assign_owner(
    actor: Actor,
    record: OnboardingRecord,
    task_id: str,
    assignee_id: str,
    *,
    team: str = "",
) -> TaskRecord:
    """Record HR's assignment of a responsible individual to a task.

    Only an HR role may assign, because the agreed rule is that *HR* assigns
    owners. The task's configured ``owner`` role is untouched: it says which
    function the work belongs to, while ``assignee_id`` says who is doing it.

    Re-assignment is permitted while the task is still open, and each
    assignment is audited by the caller, so the history of who held a task is
    preserved rather than overwritten silently.
    """
    assert_same_client(actor, record)
    task = _get_task(record, task_id)

    if actor.role not in ADMIN_ROLES:
        raise UnauthorizedActionError(
            "only an HR role may assign a task owner",
            actor_role=str(actor.role),
            task_id=task_id,
        )
    if record.status is not OnboardingStatus.ACTIVE:
        raise InvalidStateError(
            "onboarding is not active", onboarding_status=str(record.status)
        )
    if task.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED):
        raise InvalidStateError(
            "a settled task cannot be reassigned",
            task_id=task_id,
            task_status=str(task.status),
        )

    cleaned = (assignee_id or "").strip()
    if not cleaned:
        raise UnauthorizedActionError(
            "an assignee identifier is required; UC-02 never invents one",
            task_id=task_id,
        )

    task.assignee_id = cleaned
    task.assignee_team = (team or "").strip()
    task.assigned_by = actor.actor_id
    task.assigned_at = utc_now()
    record.updated_at = utc_now()
    return task


def unassigned_tasks(record: OnboardingRecord) -> list[TaskRecord]:
    """Open tasks awaiting HR assignment, earliest due first.

    An open task always carries an ``owner`` role, so "has an owner role" is
    never sufficient: this looks only at whether HR named an individual.
    """
    return sorted(
        (
            task
            for task in record.tasks
            if task.status in OPEN_STATUSES and not task.is_assigned
        ),
        key=lambda t: (t.due_date, t.task_id),
    )


def is_overdue(task: TaskRecord, as_of: date) -> bool:
    """Open task whose due date has passed. Derived, never stored."""
    return task.status in OPEN_STATUSES and task.due_date < as_of


def overdue_tasks(record: OnboardingRecord, as_of: date) -> list[TaskRecord]:
    return [task for task in record.tasks if is_overdue(task, as_of)]


def summarise(
    record: OnboardingRecord,
    as_of: date,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
) -> TaskSummary:
    counts = {status: 0 for status in TaskStatus}
    for task in record.tasks:
        counts[task.status] += 1
    overdue = [task.task_id for task in overdue_tasks(record, as_of)]
    return TaskSummary(
        onboarding_id=record.onboarding_id,
        client_id=record.client_id,
        employee_id=record.employee_id,
        joining_date=record.joining_date,
        total_tasks=len(record.tasks),
        blocked=counts[TaskStatus.BLOCKED],
        ready=counts[TaskStatus.READY],
        in_progress=counts[TaskStatus.IN_PROGRESS],
        complete=counts[TaskStatus.COMPLETE],
        cancelled=counts[TaskStatus.CANCELLED],
        overdue_task_ids=overdue,
        days_to_joining=business_days_between(as_of, record.joining_date, holidays),
        is_on_track=not overdue,
    )


def assess_joining_risk(
    record: OnboardingRecord,
    as_of: date,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
) -> JoiningRisk:
    """Flag whether day-1 readiness is in doubt.

    Two independent triggers: anything overdue, or a day-1-critical task still
    open inside the pre-joining threshold.
    """
    days_to_joining = business_days_between(as_of, record.joining_date, holidays)
    overdue = [task.task_id for task in overdue_tasks(record, as_of)]

    critical_open = [
        task.task_id
        for task in record.tasks
        if task.status in OPEN_STATUSES
        and task.category.value in DAY_ONE_CRITICAL_CATEGORIES
    ]

    reasons: list[str] = []
    if overdue:
        reasons.append(f"{len(overdue)} task(s) overdue")
    inside_threshold = days_to_joining <= JOINING_RISK_DAYS_BEFORE
    if critical_open and inside_threshold:
        reasons.append(
            f"{len(critical_open)} day-1 critical task(s) open with "
            f"{days_to_joining} business day(s) to joining"
        )

    return JoiningRisk(
        onboarding_id=record.onboarding_id,
        joining_date=record.joining_date,
        days_to_joining=days_to_joining,
        at_risk=bool(reasons),
        critical_open_task_ids=critical_open if inside_threshold else [],
        overdue_task_ids=overdue,
        reasons=reasons,
    )


#: UC02-BR-10. Factual next step per status. These describe what the plan
#: says, not an invented instruction to a person.
_FOLLOW_UP_BY_STATUS = {
    TaskStatus.BLOCKED: "waiting on prerequisite task(s)",
    TaskStatus.READY: "ready to start - owner action required",
    TaskStatus.IN_PROGRESS: "in progress - owner to complete",
}


def outstanding_activities(
    record: OnboardingRecord,
    as_of: date,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
) -> list[OutstandingActivity]:
    """Outstanding activities **with responsible owner and follow-up** (BR-10).

    The task summary reports IDs and counts only, which does not satisfy the
    rule's requirement to name the owner and the required follow-up. This is
    planning information handed to UC-06; UC-02 does not act on it.
    """
    from onboarding_task_management.sub_agent.planner import unmet_prerequisites

    activities: list[OutstandingActivity] = []
    for task in record.tasks:
        if task.status not in OPEN_STATUSES:
            continue
        overdue = is_overdue(task, as_of)
        blocked_by = unmet_prerequisites(record, task)
        follow_up = _FOLLOW_UP_BY_STATUS.get(task.status, "")
        if overdue:
            follow_up = f"OVERDUE - {follow_up}"
        if task.evidence_required:
            follow_up = f"{follow_up}; completion evidence required"
        activities.append(
            OutstandingActivity(
                task_id=task.task_id,
                template_key=task.template_key,
                category=task.category,
                owner=task.owner,
                due_date=task.due_date,
                status=task.status,
                is_overdue=overdue,
                days_overdue=(
                    -business_days_between(as_of, task.due_date, holidays)
                    if overdue
                    else 0
                ),
                blocked_by_task_ids=blocked_by,
                required_follow_up=follow_up,
            )
        )
    return sorted(activities, key=lambda a: (a.due_date, a.task_id))


#: UC02-BR-10 / GR-12. Escalation routing is the Business Framework §10 rule:
#: first the assigned HR officer, then the HR operations lead if the item has
#: been waiting beyond the response window.
_ESCALATION_ROUTE = {
    EscalationLevel.HR_OFFICER: Role.HR_OFFICER,
    EscalationLevel.HR_OPS_LEAD: Role.HR_LEAD,
}


def _level_for(days_waiting: int) -> EscalationLevel:
    """First responder, then the operations lead past the response window."""
    if days_waiting > ESCALATION_RESPONSE_DAYS:
        return EscalationLevel.HR_OPS_LEAD
    return EscalationLevel.HR_OFFICER


def escalations(
    record: OnboardingRecord,
    as_of: date,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
    owner_issues: list | None = None,
    milestone_states: list | None = None,
) -> list[EscalationItem]:
    """Items requiring escalation under GR-12.

    GR-12: "Escalate any item at risk of missing the start date, and any
    approval stalled past its due date." Stalled approvals are added by the
    service, which owns the approval inbox.

    Nothing is sent: this returns the escalation list for UC-06 to action.
    """
    items: list[EscalationItem] = []
    days_to_joining = business_days_between(as_of, record.joining_date, holidays)

    for task in record.tasks:
        if task.status not in OPEN_STATUSES:
            continue
        overdue_days = (
            -business_days_between(as_of, task.due_date, holidays)
            if is_overdue(task, as_of)
            else 0
        )
        at_risk_of_start_date = (
            overdue_days > 0
            or (
                days_to_joining <= JOINING_RISK_DAYS_BEFORE
                and task.category.value in DAY_ONE_CRITICAL_CATEGORIES
            )
        )
        if not at_risk_of_start_date:
            continue
        level = _level_for(overdue_days)
        reason = (
            f"task overdue by {overdue_days} business day(s)"
            if overdue_days > 0
            else f"day-1 critical task open with {days_to_joining} business day(s) to joining"
        )
        items.append(
            EscalationItem(
                subject_type="task",
                subject_id=task.task_id,
                level=level,
                escalate_to=_ESCALATION_ROUTE[level],
                reason=reason,
                owner=task.owner,
                days_waiting=overdue_days,
            )
        )

    # A task that has used up its client-configured failed-report ceiling goes straight to the
    # HR operations lead: repeated correction by the same owners is not working.
    for task in record.tasks:
        if task.status in OPEN_STATUSES and task.retry_limit_reached:
            items.append(
                EscalationItem(
                    subject_type="task_retry_limit",
                    subject_id=task.task_id,
                    level=EscalationLevel.HR_OPS_LEAD,
                    escalate_to=_ESCALATION_ROUTE[EscalationLevel.HR_OPS_LEAD],
                    reason=(
                        f"{task.failed_report_count} failed report(s) reached the configured "
                        f"limit of {task.max_failed_reports}"
                    ),
                    owner=task.owner,
                )
            )

    for state in milestone_states or []:
        if state.status.value in {"at_risk", "missed"}:
            level = (
                EscalationLevel.HR_OPS_LEAD
                if state.status.value == "missed"
                else EscalationLevel.HR_OFFICER
            )
            items.append(
                EscalationItem(
                    subject_type="milestone",
                    subject_id=state.milestone_id,
                    level=level,
                    escalate_to=_ESCALATION_ROUTE[level],
                    reason=f"milestone {state.status.value}: "
                    + ("; ".join(state.reasons) or "outstanding required tasks"),
                    days_waiting=max(0, -state.days_to_target),
                )
            )

    # A task with no legitimate owner cannot be chased, so it escalates to the
    # operations lead for configuration correction.
    for issue in owner_issues or []:
        items.append(
            EscalationItem(
                subject_type="configuration",
                subject_id=issue.template_key,
                level=EscalationLevel.HR_OPS_LEAD,
                escalate_to=Role.HR_LEAD,
                reason=f"invalid task owner: {issue.problem}",
            )
        )

    return items


# ---------------------------------------------------------------------------
# Follow-up flags (UC02-BR-10)
#
# A flag says "someone needs to chase this, and here is who". It is NOT a
# notification: UC-02 has no transport, so nothing is delivered. Flags are
# derived from the record and an explicit ``as_of``, never stored.
# ---------------------------------------------------------------------------

#: Flag codes, so callers branch on a stable value rather than prose.
FLAG_UNASSIGNED = "unassigned"
FLAG_EXTERNAL_UNRESOLVED = "external_unresolved"
FLAG_OVERDUE = "overdue"
FLAG_DELAYED = "delayed"
FLAG_RETRY_LIMIT = "retry_limit_reached"


def _follow_up_with(task: TaskRecord) -> str:
    """Who should be chased: the assignee if HR named one, otherwise HR."""
    if task.is_assigned and task.assignee_id is not None:
        return task.assignee_id
    return f"hr_ops (task is {task.assignment_status.value})"


def _flag(
    task: TaskRecord,
    code: str,
    reason: str,
    source: SourceUseCase | None = None,
) -> FollowUpFlag:
    return FollowUpFlag(
        source=source,
        task_id=task.task_id,
        template_key=task.template_key,
        onboarding_id=task.onboarding_id,
        client_id=task.client_id,
        code=code,
        reason=reason,
        owner_role=task.owner,
        assignee_id=task.assignee_id,
        assignee_team=task.assignee_team,
        follow_up_with=_follow_up_with(task),
    )


def follow_up_flags(
    record: OnboardingRecord,
    as_of: date,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
) -> list[FollowUpFlag]:
    """Everything that needs chasing, with the person or function to chase.

    Three independent triggers, each reported separately so a task can carry
    more than one flag:

    * ``unassigned``          - open, and HR has not named an individual;
    * ``external_unresolved`` - another workflow reported an outcome that
      leaves the prerequisite unresolved (recorded in ``notes``);
    * ``overdue``             - open and past its due date.
    """
    flags: list[FollowUpFlag] = []
    for task in record.tasks:
        if task.status not in OPEN_STATUSES:
            continue

        if not task.is_assigned:
            flags.append(
                _flag(
                    task,
                    FLAG_UNASSIGNED,
                    "awaiting HR assignment; the configured owner role "
                    f"({task.owner.value}) is not an assigned individual",
                )
            )

        if task.is_externally_unresolved:
            # ``external_outcome`` is set only by the status-update handler,
            # so this reports what another workflow actually said, attributed
            # to the workflow that said it.
            outcome = task.external_outcome
            assert outcome is not None  # implied by is_externally_unresolved
            flags.append(
                _flag(
                    task,
                    FLAG_EXTERNAL_UNRESOLVED,
                    f"{outcome.value}: "
                    f"{task.external_blocking_reason or 'no reason given'}",
                    source=task.external_source,
                )
            )

        if task.retry_limit_reached:
            flags.append(
                _flag(
                    task,
                    FLAG_RETRY_LIMIT,
                    f"{task.failed_report_count} failed report(s), configured limit "
                    f"{task.max_failed_reports}; correction needs HR attention",
                )
            )

        if task.delay_reason:
            flags.append(
                _flag(
                    task,
                    FLAG_DELAYED,
                    f"{task.delay_kind or 'delay'} reported: {task.delay_reason}",
                )
            )

        if is_overdue(task, as_of):
            days = -business_days_between(as_of, task.due_date, holidays)
            flags.append(
                _flag(
                    task,
                    FLAG_OVERDUE,
                    f"overdue by {days} business day(s)",
                )
            )

    return sorted(flags, key=lambda f: (f.task_id, f.code))
