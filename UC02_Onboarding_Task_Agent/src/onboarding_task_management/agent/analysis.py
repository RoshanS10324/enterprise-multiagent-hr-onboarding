"""Structured plan analysis: deterministic facts in, advisory sections out.

Division of labour (same rule as :mod:`advisor`): the rules engine and the client configuration decide what
exists and when; the analyst only explains and recommends. So:

* :func:`gather_facts` builds the *only* input the analyst sees, from the service (ids, keys, statuses, dates,
  counts; no name, email, nationality value or contact detail).
* :class:`FakeAnalyst` answers deterministically from those facts alone, labelled as fake. It is the stand-in
  for a future model behind the same :class:`Analyst` protocol; nothing here calls a network.
* :func:`validate_analysis` overrules anything the facts do not support. A suggested task is never added to a
  plan: it is flagged ``requires_configuration_confirmation`` (not in the client's configuration) or
  ``configured_but_not_applicable`` (configured, but the rules left it out for this employee).

Nothing in an analysis approves, completes, assigns, reschedules or creates anything. Re-planning
recommendations are proposals for HR; the existing gated tools are the only way to act on them.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from onboarding_task_management.contracts import OPEN_STATUSES, PRIORITIES, Actor, TaskStatus

FAKE_ANALYST_MODEL = "fake/uc02-analyst-v1"

STATUS_REQUIRES_CONFIRMATION = "requires_configuration_confirmation"
STATUS_CONFIGURED_NOT_APPLICABLE = "configured_but_not_applicable"


class PriorityNote(BaseModel):
    task_id: str
    template_key: str
    priority: str
    reason: str


class AssignmentSuggestion(BaseModel):
    task_id: str
    template_key: str
    owner_role: str
    team: str | None = None
    use_case: str = ""
    escalation_owner: str | None = None
    #: True when the client configured no rule for this task's category: reported, never guessed.
    assignment_gap: bool = False
    needs_named_individual: bool = False


class BlockerNote(BaseModel):
    task_id: str
    template_key: str
    reason: str
    dependent_task_ids: list[str] = Field(default_factory=list)


class DelayNote(BaseModel):
    task_id: str
    template_key: str
    dependent_task_ids: list[str] = Field(default_factory=list)
    days_to_joining: int = 0
    on_critical_path: bool = False
    statement: str = ""


class SuggestedTask(BaseModel):
    template_key: str
    reason: str
    status: str = STATUS_REQUIRES_CONFIRMATION
    #: Always False: a suggestion is never added to the plan by the advisor.
    added_to_plan: bool = False


class PlanAnalysis(BaseModel):
    onboarding_id: str
    client_id: str
    config_version: str
    as_of: date
    #: ``fake_llm:<model>`` or ``rules``. Never presented as a real model.
    source: str
    advisory_only: bool = True
    checklist_summary: str
    task_priorities: list[PriorityNote] = Field(default_factory=list)
    hr_review_summary: str
    missing_information: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    dependency_notes: list[str] = Field(default_factory=list)
    parallel_recommendations: list[list[str]] = Field(default_factory=list)
    critical_path: list[str] = Field(default_factory=list)
    team_assignment_suggestions: list[AssignmentSuggestion] = Field(default_factory=list)
    blocker_analysis: list[BlockerNote] = Field(default_factory=list)
    delay_impact: list[DelayNote] = Field(default_factory=list)
    replan_recommendations: list[str] = Field(default_factory=list)
    suggested_additional_tasks: list[SuggestedTask] = Field(default_factory=list)
    approval_summary: str = ""
    degraded: bool = False
    degraded_reason: str = ""


@runtime_checkable
class Analyst(Protocol):
    """Anything that turns facts into a :class:`PlanAnalysis`. A real model plugs in here later."""

    name: str

    def analyse(self, facts: dict[str, Any]) -> PlanAnalysis: ...


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def gather_facts(service, actor: Actor, onboarding_id: str, as_of: date) -> dict[str, Any]:
    """Everything the analyst may see, computed by the rules engine. Identifiers and counts only."""
    record = service.get(actor, onboarding_id)
    view = service.checklist(actor, onboarding_id, as_of)
    risk = service.joining_risk(actor, onboarding_id, as_of)
    config = service.registry.get(record.client_id, on=record.joining_date)
    path = service.critical_path(actor, onboarding_id)
    entries = {e.task_id: e for e in view.entries}
    tasks = []
    for t in record.tasks:
        e = entries.get(t.task_id)
        tasks.append({
            "task_id": t.task_id, "key": t.template_key, "category": t.category.value, "owner_role": t.owner.value,
            "status": t.status.value, "due": t.due_date.isoformat(), "priority": t.priority,
            "depends_on": list(t.depends_on), "blocked_by_keys": list(e.blocked_by_keys) if e else [],
            "overdue": bool(e and e.is_overdue), "days_overdue": e.days_overdue if e else 0,
            "assigned": t.is_assigned, "evidence_required": t.evidence_required,
            "externally_unresolved": t.is_externally_unresolved, "delayed": bool(t.delay_reason),
        })
    delayed = [t for t in tasks if t["status"] in {s.value for s in OPEN_STATUSES}
               and (t["overdue"] or t["delayed"] or t["externally_unresolved"])]
    impacts = [service.delay_impact(actor, onboarding_id, t["task_id"], as_of).model_dump(mode="json") for t in delayed]
    template_keys_all = sorted(t.key for t in config.templates)
    present = {t["key"] for t in tasks}
    plan_requests = [r for r in view.pending_gate_request_ids]
    location_status = next((a.split(":", 1)[1] for a in record.attributes if a.startswith("location_status:")), "")
    routed = sorted({a for t in config.templates for a in t.requires_attributes if a.startswith("location_status:")})
    return {
        "onboarding_id": record.onboarding_id, "client_id": record.client_id, "as_of": as_of.isoformat(),
        "employee": {
            "role_id": record.role_id, "grade": record.grade, "location": record.location, "project": record.project,
            "nationality_group": record.nationality_group, "attributes": sorted(record.attributes),
            "location_status": location_status, "joining_date": record.joining_date.isoformat(),
        },
        "config": {
            "version": config.version, "template_count": len(config.templates),
            "template_keys": template_keys_all, "excluded_keys": sorted(set(template_keys_all) - present),
            "has_assignment_rules": bool(config.assignments), "has_non_response_policy": config.non_response_policy is not None,
            "location_status_routes": routed,
        },
        "tasks": tasks,
        "assignment": {
            t["task_id"]: _assignment_for(config, t) for t in tasks if t["status"] in {s.value for s in OPEN_STATUSES}
        },
        "parallel_batches": view.parallel_batches,
        "critical_path": path.task_ids,
        "risk": {"at_risk": risk.at_risk, "reasons": list(risk.reasons), "days_to_joining": risk.days_to_joining,
                 "overdue": list(risk.overdue_task_ids)},
        "delay_impacts": impacts,
        "issues": [i.subject for i in view.issues],
        "review_required": view.review_required,
        "workflow": {"plan_status": record.plan_status.value, "final_status": record.final_status.value,
                     "revision": record.revision, "pending_request_ids": plan_requests},
    }


def _assignment_for(config, task: dict[str, Any]) -> dict[str, Any]:
    from onboarding_task_management.contracts import TaskCategory

    rule = config.assignment_for(TaskCategory(task["category"]))
    return {
        "team": rule.team if rule else None, "use_case": rule.use_case if rule else "",
        "mode": rule.mode if rule else None,
        "escalation_owner": rule.escalation_owner.value if rule and rule.escalation_owner else None,
        "gap": rule is None,
    }


# ---------------------------------------------------------------------------
# The fake analyst
# ---------------------------------------------------------------------------


class FakeAnalyst:
    """Deterministic scripted analyst. Same facts in, byte-identical analysis out. Never a real model.

    ``behaviour`` reproduces the unhappy paths: ``"hallucinate"`` returns unsupported content so the
    validator's overruling can be tested; ``"error"`` raises like a failed gateway call.
    """

    name = f"fake_llm:{FAKE_ANALYST_MODEL}"

    def __init__(self, behaviour: str = "scripted") -> None:
        if behaviour not in {"scripted", "hallucinate", "error"}:
            raise ValueError(f"unknown behaviour {behaviour!r}")
        self.behaviour = behaviour
        self.calls = 0

    def analyse(self, facts: dict[str, Any]) -> PlanAnalysis:
        self.calls += 1
        if self.behaviour == "error":
            raise RuntimeError("fake analyst failure (injected)")
        analysis = self._script(facts)
        if self.behaviour == "hallucinate":
            analysis.task_priorities.append(PriorityNote(
                task_id="ONB-NOT-THIS-CLIENT#invented", template_key="invented", priority="urgent!!", reason="made up"))
            analysis.suggested_additional_tasks.append(SuggestedTask(
                template_key="invented_task", reason="made up", status="auto_added", added_to_plan=True))
            analysis.parallel_recommendations = [["ONB-NOT-THIS-CLIENT#invented"]]
        return analysis

    # -- scripted narrative, filled only with facts ---------------------------------------------------------

    def _script(self, f: dict[str, Any]) -> PlanAnalysis:
        tasks, emp, cfg, risk = f["tasks"], f["employee"], f["config"], f["risk"]
        open_tasks = [t for t in tasks if t["status"] in {s.value for s in OPEN_STATUSES}]
        by_id = {t["task_id"]: t for t in tasks}

        def short(task_id: str) -> str:
            return by_id[task_id]["key"] if task_id in by_id else task_id

        counts = {s: sum(1 for t in tasks if t["status"] == s) for s in ("blocked", "ready", "in_progress", "complete")}
        route = emp["location_status"] or "not stated"
        summary = (
            f"{len(tasks)} task(s) for a {emp['grade'] or 'ungraded'} {emp['role_id']} in {emp['location']} "
            f"(location status: {route}), from client configuration version {cfg['version']}: "
            f"{counts['complete']} complete, {counts['ready']} ready, {counts['in_progress']} in progress, "
            f"{counts['blocked']} blocked; {risk['days_to_joining']} business day(s) to joining."
        )

        priorities = [
            PriorityNote(task_id=t["task_id"], template_key=t["key"], priority=t["priority"],
                         reason=("on the critical path" if t["task_id"] in f["critical_path"]
                                 else "configured priority; off the critical path"))
            for t in open_tasks if t["priority"] in ("high", "critical") or t["task_id"] in f["critical_path"]
        ]

        missing: list[str] = []
        if cfg["location_status_routes"] and not emp["location_status"]:
            missing.append("current location status (in_country / overseas): the client configures routes that depend on it")
        for t in open_tasks:
            if f["assignment"].get(t["task_id"], {}).get("gap"):
                missing.append(f"no assignment rule for category '{t['category']}' (task {t['key']})")
        if not cfg["has_non_response_policy"]:
            missing.append("no non-response reminder policy configured (DL-14); reminders cannot be scheduled")
        unassigned = [t["key"] for t in open_tasks if not t["assigned"]]
        if unassigned:
            missing.append("HR has not named an individual for: " + ", ".join(sorted(unassigned)))
        if f["review_required"]:
            missing.append("configuration issues need review: " + ", ".join(sorted(set(f["issues"]))))

        risks = list(risk["reasons"])
        if risk["at_risk"] and not risks:
            risks.append("day-1 readiness is at risk")
        for t in open_tasks:
            if t["overdue"]:
                risks.append(f"{t['key']} is overdue by {t['days_overdue']} business day(s)")
            if t["externally_unresolved"]:
                risks.append(f"{t['key']} has an unresolved outcome reported by another workflow")

        deps = [f"{t['key']} waits on {', '.join(t['blocked_by_keys'])}" for t in open_tasks if t["blocked_by_keys"]]

        batches = [[b for b in batch] for batch in f["parallel_batches"]]
        teams = [
            AssignmentSuggestion(
                task_id=tid, template_key=by_id[tid]["key"], owner_role=by_id[tid]["owner_role"], team=a["team"],
                use_case=a["use_case"], escalation_owner=a["escalation_owner"], assignment_gap=a["gap"],
                needs_named_individual=a["mode"] == "named_individual" and not by_id[tid]["assigned"])
            for tid, a in sorted(f["assignment"].items())
        ]

        blockers = [
            BlockerNote(task_id=t["task_id"], template_key=t["key"],
                        reason=("overdue" if t["overdue"] else "reported unresolved" if t["externally_unresolved"]
                                else "delay reported"),
                        dependent_task_ids=next((i["dependent_task_ids"] for i in f["delay_impacts"]
                                                 if i["task_id"] == t["task_id"]), []))
            for t in open_tasks if t["overdue"] or t["externally_unresolved"] or t["delayed"]
        ]
        delays = [
            DelayNote(task_id=i["task_id"], template_key=i["template_key"], dependent_task_ids=i["dependent_task_ids"],
                      days_to_joining=i["days_to_joining"], on_critical_path=i["on_critical_path"],
                      statement=(f"{i['template_key']} is late and holds up {len(i['dependent_task_ids'])} dependent task(s) "
                                 f"with {i['days_to_joining']} business day(s) to joining"
                                 + ("; it is on the critical path" if i["on_critical_path"] else "")))
            for i in f["delay_impacts"]
        ]

        replan: list[str] = []
        if risk["at_risk"]:
            replan.append("Day-1 readiness is at risk. HR may decide to raise a joining-date change request; UC-02 will not "
                          "change the date itself.")
        for d in delays:
            replan.append(f"Ask the owner of {d.template_key} for a recovery date; if none is credible, HR decides on a re-plan.")
        if emp["location_status"] == "" and cfg["location_status_routes"]:
            replan.append("Supply the current location status so the configured visa route can be selected.")

        suggestions = []
        if risk["at_risk"] and any(t["category"] == "it_provisioning" and t["overdue"] for t in open_tasks):
            suggestions.append(SuggestedTask(
                template_key="interim_equipment_workaround",
                reason="equipment is late; an interim arrangement may help, but no configured policy provides one"))

        pending = f["workflow"]["pending_request_ids"]
        approval = (
            f"Plan status: {f['workflow']['plan_status']}; final review: {f['workflow']['final_status']}; "
            f"revision {f['workflow']['revision']}; {len(pending)} approval request(s) pending. Only an authorised HR "
            "reviewer can approve or reject; this analysis is advisory."
        )
        review = (
            f"For HR review: {len(open_tasks)} open task(s) across {len({t['owner_role'] for t in open_tasks})} owner role(s); "
            f"{len(missing)} missing item(s); {len(risks)} risk(s); critical path of {len(f['critical_path'])} task(s)."
        )
        return PlanAnalysis(
            onboarding_id=f["onboarding_id"], client_id=f["client_id"], config_version=cfg["version"],
            as_of=date.fromisoformat(f["as_of"]), source=self.name, checklist_summary=summary,
            task_priorities=priorities, hr_review_summary=review, missing_information=missing, risks=risks,
            dependency_notes=deps, parallel_recommendations=batches,
            critical_path=[short(t) for t in f["critical_path"]], team_assignment_suggestions=teams,
            blocker_analysis=blockers, delay_impact=delays, replan_recommendations=replan,
            suggested_additional_tasks=suggestions, approval_summary=approval,
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_analysis(analysis: PlanAnalysis, facts: dict[str, Any]) -> PlanAnalysis:
    """Overrule whatever the facts do not support. The record and configuration stay authoritative."""
    problems: list[str] = []
    known = {t["task_id"] for t in facts["tasks"]}
    configured = set(facts["config"]["template_keys"])
    planned = {t["key"] for t in facts["tasks"]}

    kept = []
    for p in analysis.task_priorities:
        if p.task_id not in known:
            problems.append(f"priority for unknown task {p.task_id!r}")
        elif p.priority not in PRIORITIES:
            problems.append(f"unknown priority {p.priority!r}")
        else:
            kept.append(p)
    analysis.task_priorities = kept

    safe = []
    for s in analysis.suggested_additional_tasks:
        if s.added_to_plan or s.status == "auto_added":
            problems.append(f"suggestion {s.template_key!r} claimed to be added to the plan")
        if s.template_key in planned:
            problems.append(f"suggestion {s.template_key!r} is already in the plan")
            continue
        s.added_to_plan = False  # a suggestion is never an addition
        s.status = STATUS_CONFIGURED_NOT_APPLICABLE if s.template_key in configured else STATUS_REQUIRES_CONFIRMATION
        safe.append(s)
    analysis.suggested_additional_tasks = safe

    if analysis.parallel_recommendations != [list(b) for b in facts["parallel_batches"]]:
        problems.append("parallel recommendations differ from the rules engine's batches")
        analysis.parallel_recommendations = [list(b) for b in facts["parallel_batches"]]

    for b in analysis.blocker_analysis:
        if b.task_id not in known:
            problems.append(f"blocker on unknown task {b.task_id!r}")
    analysis.blocker_analysis = [b for b in analysis.blocker_analysis if b.task_id in known]
    analysis.advisory_only = True

    if problems:
        analysis.degraded = True
        analysis.degraded_reason = "analysis overruled by the rules engine: " + "; ".join(problems)
    return analysis


def rules_only_analysis(facts: dict[str, Any]) -> PlanAnalysis:
    """The same sections from the deterministic script, labelled ``rules``: the fallback when no analyst works."""
    analysis = FakeAnalyst()._script(facts)
    analysis.source = "rules"
    return analysis


def analyse_with_fallback(service, actor: Actor, onboarding_id: str, as_of: date,
                          analyst: Analyst | None = None) -> PlanAnalysis:
    """Facts -> analyst -> validation. An analyst failure degrades to the rules-only analysis, visibly."""
    facts = gather_facts(service, actor, onboarding_id, as_of)
    analyst = analyst or FakeAnalyst()
    try:
        analysis = analyst.analyse(facts)
    except Exception as exc:  # a gateway error must not stop the workflow
        analysis = rules_only_analysis(facts)
        analysis.degraded = True
        analysis.degraded_reason = f"analyst failed ({type(exc).__name__}); rules-only analysis used"
        return analysis
    return validate_analysis(analysis, facts)


__all__ = [
    "Analyst", "AssignmentSuggestion", "BlockerNote", "DelayNote", "FakeAnalyst", "PlanAnalysis", "PriorityNote",
    "STATUS_CONFIGURED_NOT_APPLICABLE", "STATUS_REQUIRES_CONFIRMATION", "SuggestedTask", "analyse_with_fallback",
    "gather_facts", "rules_only_analysis", "validate_analysis",
]
