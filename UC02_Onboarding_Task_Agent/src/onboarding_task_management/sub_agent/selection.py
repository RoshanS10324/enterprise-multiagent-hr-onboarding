"""Explained checklist selection — the deterministic rules layer.

This module answers three questions about a checklist, and nothing else:

1. **Which configured templates apply to this employee context, and why?**
2. **Which do not, and why not?**
3. **What about the configuration or the calendar needs a human to look at
   it before the plan is acted on?**

It is pure: no stored state, no clock read, no I/O, no randomness, no LLM.
Every output is derived from (a) the client's :class:`ClientOnboardingConfig`,
(b) the context supplied by the caller, and (c) an explicit ``as_of`` date.
Running it twice on the same inputs produces byte-identical output.

Design notes
------------

* **One source of truth for the rules.** Inclusion is decided by
  :meth:`TaskTemplate.dimension_verdicts`, which :meth:`TaskTemplate.applies_to`
  also reduces to a boolean. The decision and its explanation therefore cannot
  drift apart.
* **Explanations are assembled from facts, not written.** A reason only ever
  restates the values the client configured and the values the caller supplied.
  No policy, legal requirement or client rule is inferred here.
* **Problems are reported, never resolved.** A missing or conflicting
  configuration yields a :class:`ChecklistIssue` and a ``review_required``
  flag; this module never substitutes a default owner, date or requirement.

Agentic AI readiness
--------------------

This is the **rules engine** seam. An agent may call it to obtain a structured,
explained checklist, but the selection itself is not negotiable by a model: an
LLM layer would consume :class:`ChecklistView` to phrase or summarise it, and
would not be permitted to change which tasks are in it. Tool/integration seams
live in :mod:`providers`; human approval lives in :mod:`approvals`.
"""

from __future__ import annotations

from datetime import date

from onboarding_task_management.calendar_uae import (
    UAE_PUBLIC_HOLIDAYS,
    business_days_between,
    calendar_status,
)
from onboarding_task_management.contracts import (
    OPEN_STATUSES,
    ChecklistContext,
    ChecklistEntry,
    ChecklistIssue,
    ChecklistIssueSeverity,
    ChecklistPreview,
    ChecklistView,
    DimensionVerdict,
    MilestoneState,
    OnboardingRecord,
    SelectionDimension,
    SelectionOutcome,
    TaskRecord,
    TaskStatus,
    TemplateDecision,
)
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    TaskTemplate,
    topological_order,
)
from onboarding_task_management.sub_agent.planner import (
    due_date_for,
    parallel_batches,
    unmet_prerequisites,
)
from onboarding_task_management.sub_agent.tracker import is_overdue

#: Issue codes, so callers can branch on a stable value rather than prose.
ISSUE_NO_APPLICABLE_TASKS = "no_applicable_tasks"
ISSUE_DROPPED_PREREQUISITE = "dropped_prerequisite"
ISSUE_OWNER_INVALID = "owner_invalid"
ISSUE_CALENDAR_UNAPPROVED = "calendar_unapproved"
ISSUE_MILESTONE_UNREACHABLE = "milestone_unreachable"


def context_from_record(record: OnboardingRecord) -> ChecklistContext:
    """The selection inputs that produced (and will reproduce) a plan."""
    return ChecklistContext(
        client_id=record.client_id,
        employee_id=record.employee_id,
        joining_date=record.joining_date,
        location=record.location,
        project=record.project,
        role_id=record.role_id,
        grade=record.grade,
        nationality_group=record.nationality_group,
        attributes=sorted(set(record.attributes)),
    )


# ---------------------------------------------------------------------------
# Reason assembly
# ---------------------------------------------------------------------------


def _values(verdict: DimensionVerdict) -> str:
    return "(" + ", ".join(verdict.configured) + ")"


def _describe_match(verdict: DimensionVerdict) -> str:
    if verdict.dimension is SelectionDimension.ATTRIBUTES:
        return (
            f"attributes {_values(verdict)} are all declared on the onboarding"
        )
    supplied = verdict.supplied[0] if verdict.supplied else ""
    return f"{verdict.dimension.value} {supplied!r} is in the configured set {_values(verdict)}"


def _describe_mismatch(verdict: DimensionVerdict) -> str:
    if verdict.dimension is SelectionDimension.ATTRIBUTES:
        declared = ", ".join(verdict.supplied) or "none"
        missing = ", ".join(verdict.missing)
        return (
            f"requires attributes ({missing}) which the onboarding does not "
            f"declare (declared: {declared})"
        )
    supplied = verdict.supplied[0] if verdict.supplied else ""
    shown = repr(supplied) if supplied else "no value supplied"
    return f"{verdict.dimension.value} {shown} is not in the configured set {_values(verdict)}"


def evaluate_template(
    template: TaskTemplate, context: ChecklistContext
) -> TemplateDecision:
    """Decide one template against one context, with the reason it decided so.

    The reason restates configured and supplied values only. Where a template
    is excluded by more than one dimension every failing dimension is named,
    because fixing only the first would not change the outcome.
    """
    verdicts = list(
        template.dimension_verdicts(
            location=context.location,
            project=context.project,
            role_id=context.role_id,
            grade=context.grade,
            nationality_group=context.nationality_group,
            attributes=context.attributes,
        )
    )
    failed = [v for v in verdicts if not v.matched]

    if failed:
        reason = "excluded: " + "; ".join(_describe_mismatch(v) for v in failed)
        return TemplateDecision(
            template_key=template.key,
            category=template.category,
            outcome=SelectionOutcome.EXCLUDED,
            reason_code=f"excluded_by_{failed[0].dimension.value}",
            reason=reason,
            verdicts=verdicts,
        )

    restricted = [v for v in verdicts if v.configured]
    if not restricted:
        return TemplateDecision(
            template_key=template.key,
            category=template.category,
            outcome=SelectionOutcome.INCLUDED,
            reason_code="applies_to_all",
            reason=(
                "included: the client configured no applicability filter, so "
                "this task applies to every onboarding for this client"
            ),
            verdicts=verdicts,
        )
    return TemplateDecision(
        template_key=template.key,
        category=template.category,
        outcome=SelectionOutcome.INCLUDED,
        reason_code="matched_all_filters",
        reason=(
            "included: " + "; ".join(_describe_match(v) for v in restricted)
        ),
        verdicts=verdicts,
    )


def evaluate_config(
    config: ClientOnboardingConfig, context: ChecklistContext
) -> tuple[list[TemplateDecision], list[TemplateDecision]]:
    """``(included, excluded)`` decisions for every template the client configured."""
    included: list[TemplateDecision] = []
    excluded: list[TemplateDecision] = []
    for template in config.templates:
        decision = evaluate_template(template, context)
        if decision.outcome is SelectionOutcome.INCLUDED:
            included.append(decision)
        else:
            excluded.append(decision)
    return included, excluded


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------


def dropped_prerequisite_issues(
    config: ClientOnboardingConfig, in_scope_keys: set[str]
) -> list[ChecklistIssue]:
    """Prerequisites the plan silently drops because they fell out of scope.

    ``planner.build_tasks`` removes a dependency on a template that is not in
    scope, so the dependent is startable rather than permanently blocked. That
    is the right plan-time behaviour, but it discards a sequencing requirement
    the client wrote down (for example "verify documents before visa
    processing"), so it is surfaced for review rather than passing silently.
    """
    by_key = {t.key: t for t in config.templates}
    issues: list[ChecklistIssue] = []
    for key in sorted(in_scope_keys):
        template = by_key.get(key)
        if template is None:
            continue
        dropped = [d for d in template.depends_on if d not in in_scope_keys]
        for dependency in sorted(dropped):
            issues.append(
                ChecklistIssue(
                    code=ISSUE_DROPPED_PREREQUISITE,
                    severity=ChecklistIssueSeverity.REVIEW_REQUIRED,
                    subject=key,
                    detail=(
                        f"task {key!r} is in scope but its configured "
                        f"prerequisite {dependency!r} is not, so the "
                        "prerequisite is not enforced for this onboarding; "
                        "confirm the configuration or the employee context"
                    ),
                )
            )
    return issues


def _owner_issues(config: ClientOnboardingConfig, in_scope_keys: set[str]) -> list[ChecklistIssue]:
    return [
        ChecklistIssue(
            code=ISSUE_OWNER_INVALID,
            severity=ChecklistIssueSeverity.REVIEW_REQUIRED,
            subject=issue.template_key,
            detail=issue.problem,
        )
        for issue in config.owner_issues()
        if issue.template_key in in_scope_keys
    ]


def _calendar_issue(holidays: frozenset[date]) -> list[ChecklistIssue]:
    status = calendar_status(holidays)
    if status.approved:
        return []
    return [
        ChecklistIssue(
            code=ISSUE_CALENDAR_UNAPPROVED,
            severity=ChecklistIssueSeverity.INFORMATIONAL,
            subject="calendar",
            detail=(
                f"due dates were computed from {status.source!r}, which is not "
                "approved holiday data; the dates are provisional"
            ),
        )
    ]


def _milestone_issues(
    config: ClientOnboardingConfig, context: ChecklistContext, in_scope_keys: set[str]
) -> list[ChecklistIssue]:
    """Applicable milestones that require a task this context will not get.

    Such a milestone can never be achieved, so it would report the employee as
    permanently at risk for work that correctly does not exist.
    """
    issues: list[ChecklistIssue] = []
    for milestone in config.applicable_milestones(
        location=context.location,
        project=context.project,
        role_id=context.role_id,
        grade=context.grade,
        attributes=context.attributes,
    ):
        unreachable = sorted(set(milestone.requires) - in_scope_keys)
        if unreachable:
            issues.append(
                ChecklistIssue(
                    code=ISSUE_MILESTONE_UNREACHABLE,
                    severity=ChecklistIssueSeverity.REVIEW_REQUIRED,
                    subject=milestone.key,
                    detail=(
                        f"milestone {milestone.key!r} applies to this "
                        "onboarding but requires task(s) "
                        f"({', '.join(unreachable)}) that are out of scope, so "
                        "it can never be achieved"
                    ),
                )
            )
    return issues


def selection_issues(
    config: ClientOnboardingConfig,
    context: ChecklistContext,
    in_scope_keys: set[str],
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
) -> list[ChecklistIssue]:
    """Everything about this selection a human should confirm, in a stable order."""
    issues: list[ChecklistIssue] = []
    if not in_scope_keys:
        issues.append(
            ChecklistIssue(
                code=ISSUE_NO_APPLICABLE_TASKS,
                severity=ChecklistIssueSeverity.BLOCKING,
                subject=config.client_id,
                detail=(
                    "no configured template applies to this context "
                    f"(location={context.location!r}, project={context.project!r}, "
                    f"role_id={context.role_id!r}, grade={context.grade!r}); "
                    "UC-02 does not generate tasks that the client has not configured"
                ),
            )
        )
    issues.extend(dropped_prerequisite_issues(config, in_scope_keys))
    issues.extend(_milestone_issues(config, context, in_scope_keys))
    issues.extend(_owner_issues(config, in_scope_keys))
    issues.extend(attribute_issues(config, context))
    issues.extend(_calendar_issue(holidays))
    return issues


ISSUE_ATTRIBUTE_UNSTATED = "attribute_unstated"
ISSUE_ATTRIBUTE_VALUE_UNKNOWN = "attribute_value_unknown"
ISSUE_ELIGIBILITY_NOT_CONFIGURED = "eligibility_not_configured"


def _families_referenced(config: ClientOnboardingConfig) -> dict[str, set[str]]:
    """``family -> {keys}`` of templates and documents whose filters need ``<family>:<value>``."""
    out: dict[str, set[str]] = {}
    pairs = [(t.key, t.requires_attributes) for t in config.templates] + [(d.key, d.requires_attributes) for d in config.document_requirements]
    for key, required in pairs:
        for attribute in required:
            if ":" in attribute:
                out.setdefault(attribute.split(":", 1)[0], set()).add(key)
    return out


def eligibility_errors(config: ClientOnboardingConfig, context: ChecklistContext) -> list[str]:
    """Why this employee is outside the client's declared catalogue. Empty = eligible (or no catalogue declared)."""
    cat = config.eligibility
    if cat is None:
        return []
    errors = []
    for label, allowed, value in (("role", cat.roles, context.role_id), ("work location", cat.locations, context.location),
                                  ("nationality group", cat.nationality_groups, context.nationality_group),
                                  ("grade", cat.grades, context.grade)):
        if allowed and value and value not in allowed:
            errors.append(f"{label} {value!r} is not supported by client {config.client_id} (supported: {', '.join(allowed)})")
    types = [a.split(":", 1)[1] for a in context.attributes if a.startswith("employment_type:")]
    if cat.employment_types:
        for t in types:
            if t not in cat.employment_types:
                errors.append(f"employment type {t!r} is not supported by client {config.client_id} "
                              f"(supported: {', '.join(cat.employment_types)})")
    if cat.roles and not context.role_id:
        errors.append("role is missing")
    if cat.locations and not context.location:
        errors.append("work location is missing")
    return errors


def attribute_issues(config: ClientOnboardingConfig, context: ChecklistContext) -> list[ChecklistIssue]:
    """A required or referenced attribute family the employee has not stated is flagged for HR review: the tasks that depend on it are
    listed as potentially required, never silently dropped. A value outside the declared list is flagged too."""
    referenced = _families_referenced(config)
    declared = {f.family: f for f in (config.eligibility.attribute_families if config.eligibility else ())}
    if config.eligibility and config.eligibility.employment_types and "employment_type" not in declared:
        declared["employment_type"] = None  # type: ignore[assignment]
    if config.eligibility and config.eligibility.grades and not context.grade:
        declared.setdefault("grade", None)  # type: ignore[arg-type]
    stated: dict[str, list[str]] = {}
    for attribute in context.attributes:
        if ":" in attribute:
            family, value = attribute.split(":", 1)
            stated.setdefault(family, []).append(value)
    issues: list[ChecklistIssue] = []
    optional = {f for f, spec in declared.items() if spec is not None and not spec.required}
    families = sorted((set(referenced) - optional) | {f for f, spec in declared.items() if spec is None or spec.required})
    for family in families:
        if family == "grade":
            issues.append(ChecklistIssue(code=ISSUE_ATTRIBUTE_UNSTATED, severity=ChecklistIssueSeverity.REVIEW_REQUIRED, subject="grade",
                                         detail="the client declares grades but the employee's grade is not stated; HR must confirm it"))
            continue
        if family in stated:
            spec = declared.get(family)
            unknown = [v for v in stated[family] if spec is not None and spec.values and v not in spec.values]
            if unknown and spec is not None:
                issues.append(ChecklistIssue(
                    code=ISSUE_ATTRIBUTE_VALUE_UNKNOWN, severity=ChecklistIssueSeverity.REVIEW_REQUIRED, subject=family,
                    detail=f"{family} value(s) {', '.join(unknown)} are not among the configured values ({', '.join(spec.values)})"))
            continue
        affected = sorted(referenced.get(family, set()))
        issues.append(ChecklistIssue(
            code=ISSUE_ATTRIBUTE_UNSTATED, severity=ChecklistIssueSeverity.REVIEW_REQUIRED, subject=family,
            detail=(f"the employee's {family} is not stated; HR must confirm it before the plan is acted on"
                    + (f". Potentially required, not yet included: {', '.join(affected)}" if affected else ""))))
    if config.eligibility is None:
        issues.append(ChecklistIssue(
            code=ISSUE_ELIGIBILITY_NOT_CONFIGURED, severity=ChecklistIssueSeverity.INFORMATIONAL, subject=config.client_id,
            detail="the client's configuration declares no role/location/grade catalogue, so those values were not validated"))
    return issues


def needs_review(issues: list[ChecklistIssue]) -> bool:
    return any(
        issue.severity
        in (ChecklistIssueSeverity.REVIEW_REQUIRED, ChecklistIssueSeverity.BLOCKING)
        for issue in issues
    )


# ---------------------------------------------------------------------------
# Preview — selection for a context with no stored record
# ---------------------------------------------------------------------------


def _template_batches(templates: tuple[TaskTemplate, ...]) -> list[list[str]]:
    """Dependency layers of ``template_key``, prerequisites first."""
    in_scope = {t.key for t in templates}
    depth: dict[str, int] = {}
    for template in topological_order(templates):
        dependencies = [d for d in template.depends_on if d in in_scope]
        depth[template.key] = (
            0 if not dependencies else 1 + max(depth[d] for d in dependencies)
        )
    layers: dict[int, list[str]] = {}
    for template in templates:
        layers.setdefault(depth[template.key], []).append(template.key)
    return [sorted(layers[level]) for level in sorted(layers)]


def preview(
    config: ClientOnboardingConfig,
    context: ChecklistContext,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
) -> ChecklistPreview:
    """What the checklist *would* be for ``context``. Creates nothing.

    Safe to call before preconditions are met, which is what makes it usable
    for "what changes if this employee relocates?" questions.
    """
    included, excluded = evaluate_config(config, context)
    in_scope_keys = {d.template_key for d in included}
    templates = tuple(t for t in config.templates if t.key in in_scope_keys)
    issues = selection_issues(config, context, in_scope_keys, holidays)

    due_dates: dict[str, date] = {}
    batches: list[list[str]] = []
    if templates:
        due_dates = {
            t.key: due_date_for(t, context.joining_date, holidays) for t in templates
        }
        batches = _template_batches(templates)

    return ChecklistPreview(
        context=context,
        included=included,
        excluded=excluded,
        due_dates=due_dates,
        parallel_batches=batches,
        issues=issues,
        review_required=needs_review(issues),
        calendar=calendar_status(holidays),
    )


# ---------------------------------------------------------------------------
# View — the explained checklist for a stored record
# ---------------------------------------------------------------------------


def _entry(
    record: OnboardingRecord,
    task: TaskRecord,
    decision: TemplateDecision,
    as_of: date,
    holidays: frozenset[date],
) -> ChecklistEntry:
    overdue = is_overdue(task, as_of)
    blocked_by = unmet_prerequisites(record, task)
    by_id = {t.task_id: t for t in record.tasks}
    return ChecklistEntry(
        task_id=task.task_id,
        template_key=task.template_key,
        category=task.category,
        title=task.title,
        title_ar=task.title_ar,
        owner=task.owner,
        status=task.status,
        due_date=task.due_date,
        depends_on=list(task.depends_on),
        blocked_by=blocked_by,
        blocked_by_keys=[
            by_id[dep].template_key for dep in blocked_by if dep in by_id
        ],
        is_overdue=overdue,
        days_overdue=(
            -business_days_between(as_of, task.due_date, holidays) if overdue else 0
        ),
        evidence_required=task.evidence_required,
        evidence_recorded=bool(task.evidence_ref),
        approval_role=task.approval_role,
        awaiting_approval=(
            task.approval_role is not None and task.status in OPEN_STATUSES
        ),
        decision=decision,
    )


def build_view(
    record: OnboardingRecord,
    config: ClientOnboardingConfig,
    as_of: date,
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
    *,
    milestone_states: list[MilestoneState] | None = None,
    pending_gate_request_ids: list[str] | None = None,
) -> ChecklistView:
    """The structured, explained checklist for a stored onboarding.

    Derived on demand, so it always reflects the current revision: a re-plan
    needs no synchronisation step and cannot leave a stale view behind.

    ``awaiting_approval`` means the task is still open **and** its
    configuration names an approver (UC02-BR-11) — it is a statement that the
    action may not be taken unilaterally, not a claim that an approval request
    exists. Requests that do exist are listed in ``pending_gate_request_ids``.
    """
    context = context_from_record(record)
    included, excluded = evaluate_config(config, context)
    decisions = {d.template_key: d for d in included}

    entries: list[ChecklistEntry] = []
    for task in record.tasks:
        decision = decisions.get(task.template_key)
        if decision is None:
            # A task retained from an earlier revision whose template no longer
            # applies: planner keeps completed/cancelled work (never deletes
            # it), so the checklist must explain it rather than hide it.
            retained = next(
                (d for d in excluded if d.template_key == task.template_key), None
            )
            decision = TemplateDecision(
                template_key=task.template_key,
                category=task.category,
                outcome=SelectionOutcome.INCLUDED,
                reason_code="retained_from_earlier_revision",
                reason=(
                    "retained: this task no longer matches the current context "
                    "but is kept because the work is already settled"
                    + (f" ({retained.reason})" if retained is not None else "")
                ),
                verdicts=retained.verdicts if retained is not None else [],
            )
        entries.append(_entry(record, task, decision, as_of, holidays))

    entries.sort(key=lambda e: (e.due_date, e.task_id))
    planned_keys = {t.template_key for t in record.tasks}
    issues = selection_issues(config, context, set(decisions), holidays)

    def ids(predicate) -> list[str]:
        return [e.task_id for e in entries if predicate(e)]

    return ChecklistView(
        onboarding_id=record.onboarding_id,
        client_id=record.client_id,
        employee_id=record.employee_id,
        joining_date=record.joining_date,
        as_of=as_of,
        revision=record.revision,
        onboarding_status=record.status,
        context=context,
        entries=entries,
        excluded=[d for d in excluded if d.template_key not in planned_keys],
        ready_task_ids=ids(lambda e: e.status is TaskStatus.READY),
        blocked_task_ids=ids(lambda e: e.status is TaskStatus.BLOCKED),
        in_progress_task_ids=ids(lambda e: e.status is TaskStatus.IN_PROGRESS),
        complete_task_ids=ids(lambda e: e.status is TaskStatus.COMPLETE),
        cancelled_task_ids=ids(lambda e: e.status is TaskStatus.CANCELLED),
        overdue_task_ids=ids(lambda e: e.is_overdue),
        awaiting_approval_task_ids=ids(lambda e: e.awaiting_approval),
        parallel_batches=parallel_batches(record),
        pending_gate_request_ids=sorted(pending_gate_request_ids or []),
        milestones=list(milestone_states or []),
        issues=issues,
        review_required=needs_review(issues),
        calendar=calendar_status(holidays),
    )


__all__ = [
    "ISSUE_CALENDAR_UNAPPROVED",
    "ISSUE_DROPPED_PREREQUISITE",
    "ISSUE_MILESTONE_UNREACHABLE",
    "ISSUE_NO_APPLICABLE_TASKS",
    "ISSUE_OWNER_INVALID",
    "build_view",
    "context_from_record",
    "dropped_prerequisite_issues",
    "evaluate_config",
    "evaluate_template",
    "needs_review",
    "preview",
    "selection_issues",
]
