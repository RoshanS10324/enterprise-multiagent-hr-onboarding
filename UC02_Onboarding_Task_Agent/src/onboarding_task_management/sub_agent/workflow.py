"""Pure calculations for the HR review workflow: readiness, final snapshot, team views.

No I/O, no clock reads, no storage: everything is derived from an
:class:`OnboardingRecord`, an explicit ``as_of`` date and the caller's facts about
pending approvals and escalations. The service owns state changes and audit.

"Mandatory" tasks. The client configuration carries no mandatory/optional flag, so
**every non-cancelled task counts as required** for readiness. That is the
conservative reading; a client that wants optional tasks needs that flag added to its
approved configuration (TL decision), not a silent exception here.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import date

from onboarding_task_management.calendar_uae import UAE_PUBLIC_HOLIDAYS
from onboarding_task_management.contracts import (
    OPEN_STATUSES,
    FinalStatus,
    JoiningReadiness,
    OnboardingRecord,
    PlanStatus,
    TaskRecord,
    TaskStatus,
    TeamSummary,
)
from onboarding_task_management.sub_agent import tracker


def _by_category(tasks: list[TaskRecord]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for task in tasks:
        out.setdefault(task.category.value, []).append(task.task_id)
    return {k: sorted(v) for k, v in sorted(out.items())}


def readiness(
    record: OnboardingRecord,
    as_of: date,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
    *,
    pending_approval_ids: Collection[str] = (),
    escalation_count: int = 0,
    require_final_approval: bool = True,
) -> JoiningReadiness:
    """Joining readiness from actual task and approval state. Never from a report existing."""
    live = [t for t in record.tasks if t.status is not TaskStatus.CANCELLED]
    open_tasks = [t for t in live if t.status in OPEN_STATUSES]
    complete = [t for t in live if t.status is TaskStatus.COMPLETE]
    blocked = [t for t in open_tasks if t.status is TaskStatus.BLOCKED]
    pending = [t for t in open_tasks if t.status is not TaskStatus.BLOCKED]
    overdue = [t for t in open_tasks if tracker.is_overdue(t, as_of)]
    risk = tracker.assess_joining_risk(record, as_of, holidays)

    missing_evidence = sorted(
        t.task_id for t in live if t.evidence_required and not (t.evidence_ref or "").strip()
    )
    unresolved_external = sorted(t.task_id for t in open_tasks if t.is_externally_unresolved)
    delayed = sorted(t.task_id for t in open_tasks if t.delay_reason)
    unassigned = sorted(t.task_id for t in open_tasks if not t.is_assigned)

    reasons: list[str] = []
    if record.plan_status is not PlanStatus.APPROVED:
        reasons.append(f"onboarding plan is {record.plan_status.value}, not approved by HR")
    if open_tasks:
        by_cat = _by_category(open_tasks)
        reasons.append(
            f"{len(open_tasks)} required task(s) not complete: "
            + ", ".join(f"{cat} ({len(ids)})" for cat, ids in by_cat.items())
        )
    if unresolved_external:
        reasons.append(f"{len(unresolved_external)} task(s) held by an unresolved status report")
    if missing_evidence:
        reasons.append(f"{len(missing_evidence)} evidence-required task(s) have no evidence reference")
    if pending_approval_ids:
        reasons.append(f"{len(pending_approval_ids)} approval(s) still pending")
    if risk.at_risk:
        reasons.append("joining-date risk: " + "; ".join(risk.reasons))
    if require_final_approval and record.final_status is not FinalStatus.APPROVED:
        reasons.append(f"final HR submission is {record.final_status.value}, not approved")

    return JoiningReadiness(
        onboarding_id=record.onboarding_id,
        as_of=as_of,
        status="not_ready" if reasons else "granted",
        plan_status=record.plan_status,
        plan_version=record.plan_version,
        final_status=record.final_status,
        total_tasks=len(live),
        complete=len(complete),
        pending=len(pending),
        blocked=len(blocked),
        overdue=len(overdue),
        outstanding_task_ids=sorted(t.task_id for t in open_tasks),
        outstanding_by_category=_by_category(open_tasks),
        missing_evidence_task_ids=missing_evidence,
        unresolved_external_task_ids=unresolved_external,
        delayed_task_ids=delayed,
        outstanding_hr_action_task_ids=unassigned,
        outstanding_approval_ids=sorted(pending_approval_ids),
        overdue_task_ids=sorted(t.task_id for t in overdue),
        escalation_count=escalation_count,
        joining_risk=risk.at_risk,
        joining_risk_reasons=list(risk.reasons),
        reasons=reasons,
    )


def team_summaries(
    record: OnboardingRecord,
    as_of: date,
) -> list[TeamSummary]:
    """Team-wise view: one row per configured owner role (the team)."""
    teams: dict[str, list[TaskRecord]] = {}
    for task in record.tasks:
        teams.setdefault(task.owner.value, []).append(task)
    out = []
    for team, tasks in sorted(teams.items()):
        live = [t for t in tasks if t.status is not TaskStatus.CANCELLED]
        open_tasks = [t for t in live if t.status in OPEN_STATUSES]
        out.append(
            TeamSummary(
                team=team,
                total=len(live),
                complete=sum(1 for t in live if t.status is TaskStatus.COMPLETE),
                in_progress=sum(1 for t in live if t.status is TaskStatus.IN_PROGRESS),
                ready=sum(1 for t in live if t.status is TaskStatus.READY),
                blocked=sum(1 for t in live if t.status is TaskStatus.BLOCKED),
                overdue=sum(1 for t in open_tasks if tracker.is_overdue(t, as_of)),
                unassigned=sum(1 for t in open_tasks if not t.is_assigned),
                assignees=sorted({t.assignee_id for t in live if t.assignee_id}),
                task_ids=sorted(t.task_id for t in live),
            )
        )
    return out


__all__ = ["readiness", "team_summaries"]
