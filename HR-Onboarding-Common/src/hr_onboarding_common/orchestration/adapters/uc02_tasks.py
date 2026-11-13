"""The Common Agent's adapter for UC-02 initial task planning (`UC02SubAgent.run`).

    orchestrator stage `plan_tasks` -> call_uc02_plan_tasks -> UC02SubAgent.run -> OnboardingService
                                                            -> OnboardingService.define_document_requirements
                                                                              -> OnboardingStore (optional)

Real invocation: `UC02SubAgent(...).run(WorkflowRequest(trigger=...)) -> WorkflowResult`, synchronous,
in-process (the package name `onboarding_task_management` is unique, so no worker is needed). All
task-management behaviour -- checklist selection, owners, dependencies, due dates, risk, audit -- lives in
UC-02; this adapter only maps the orchestrator's state onto UC-02's trigger and the result back.

plan_tasks runs right after UC01 intake, before any document is collected. Besides the task checklist it
defines the plan's DOCUMENT REQUIREMENTS: UC-02 records them on its plan
(`define_document_requirements`), picked from UC-03's catalog of validatable documents for this employee
(UC-03 owns the catalog and its validation rules; UC-02 owns which documents the plan requires and the
plan version). The result is `onboarding_plan` (plan id = UC-02's onboarding id, plan version, required
documents with the plan's document-collection due date) and the first `plan_history` entry. UC-02 never
collects, stores or validates a document: UC-03 does (adapters/uc03_document_collection.py,
uc03_documents.py), and UC-02 replans on UC-03's feedback (adapters/uc02_replanning.py).

What the adapter carries (never assumes):
  * ids: the orchestrator's `case_id` and `correlation_id` travel with the trigger and come back;
  * offer acceptance / form approval: facts UC-01 verified, supplied through `uc02_role_context`
    (`offer_accepted_at`, `form_approved_at`, `approved_by_role`); absent -> BLOCKED, MISSING_DATA;
  * checklist inputs: role, department, manager, location, project, and -- when supplied -- grade,
    nationality group and the opt-in attributes that switch on visa / medical / insurance / travel work.

Persistence (adapters/uc02_runtime.py): UC02_STORE_MODE picks UC-02's store -- `supabase` (the runtime: the
UC-01 case and the approved client configuration are read from Supabase, the tasks, dependencies, plan revisions,
audit and agent runs written there, never a fallback), `sqlite`, or `memory` (the default; unit tests and demos).
The sub-agent is rebuilt over the same store after a restart. In Supabase mode the case must be UC-01's
`onboarding_cases` row of this tenant before anything is planned (`verify_case`). A failure is classified, never
turned into a success: configuration/case/eligibility problems BLOCK with a specific blocker (error code
MISSING_DATA, the stage's existing contract), an unreachable database is RETRY_PENDING, a case of another tenant
FAILED. Each execution is traced in UC-02's own audit_events / agent_runs (`StageRun`). The persisted `task_plan`
is UC-02's authoritative record projected (plan version, task and required-task counts, the dependency graph,
configuration version, store) -- the orchestrator keeps it in case_orchestration_state, so UC-03 continues from
the stored state, never from process memory. The HR plan gate is off here because the orchestrator has no
route for HR's UC-02 plan decision yet; that is a recorded decision, not an assumption.

UC-02 does NOT enforce caller identity at this entry point -- compensated by the orchestrator's own
boundary check (`task_planning_trigger_roles`).
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.orchestration import joining_date
from hr_onboarding_common.orchestration.adapters import uc02_runtime
from hr_onboarding_common.orchestration.adapters._shared import (
    add_blocker, add_pending_approval, record_policy_denial, require_role, with_stage_status,
)
from hr_onboarding_common.orchestration.adapters.uc05_orientation import PrerequisiteNotMetError
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import OnboardingCaseState, Stage, StageStatus
from hr_onboarding_common.orchestration.workflow_models import OnboardingPlanRecord, PlanHistoryEntry, RequiredDocument
from hr_onboarding_common.platform.policy import PolicyViolation

UC02_IMPORT_FAILURE_CODE = "UC02_PACKAGE_UNIMPORTABLE"
UC02_AGENT = "onboarding-task-management"  # UC02's agent id: the `agent` of the date-change approval it opens
# UC-02's advice goes through the orchestrator's own LLM (`AgentContext.llm`): `"gateway"` makes
# UC02SubAgent use the client passed as `llm_client`. Offline that client is the bootstrap FakeLLM
# with UC-02's `uc02.advise` handler; online it is the central LiteLLMGateway (OpenRouter). UC-02 still
# validates every recommendation (`validate_advice`) and degrades to its rules engine on any model failure.
UC02_LLM_BEHAVIOUR = "gateway"
# UC-02's task that collects the documents: its due date is the plan's document deadline.
DOCUMENT_TASK_KEYS = ("collect_documents", "document_collection")


def _orchestrator_llm(deps: OrchestratorDeps):
    """The orchestrator's LLM client, or None when this deps object carries no context (UC-02 then
    follows LLM_MODE itself, offline by default)."""
    return getattr(getattr(deps, "ctx", None), "llm", None)


def uc02_sub_agent(deps: OrchestratorDeps):
    """The one path into UC-02's agent: the orchestrator's cached UC02SubAgent, or -- first use, or after a
    restart -- a new one over the configured store (uc02_runtime.build_service: Supabase in the runtime). Raises
    when the store cannot be used; callers classify that (uc02_runtime.classify), never fall back."""
    if getattr(deps, "_uc02_sub_agent", None) is None:
        from onboarding_task_management.sub_agent.adapter import UC02SubAgent  # type: ignore

        deps._uc02_sub_agent = UC02SubAgent(service=uc02_runtime.build_service(deps), llm_client=_orchestrator_llm(deps))
    return deps._uc02_sub_agent


def refresh_task_plan(state: OnboardingCaseState, sub_agent, actor, record) -> None:
    """`task_plan` follows UC-02's record after a change that re-planned tasks (in place): the same task view initial
    planning persisted, and the projection. If UC-02 cannot be read right now the change itself still stands
    (UC-02 committed it); the projection is marked stale, not guessed."""
    from onboarding_task_management.sub_agent.adapter import UC02SubAgent  # type: ignore

    try:
        as_of = uc02_runtime.evaluation_date(state)
        checklist = UC02SubAgent._summarise(sub_agent.service.checklist(actor, record.onboarding_id, as_of))
        state["task_plan"] = uc02_runtime.refreshed_task_plan(
            state, checklist, uc02_runtime.plan_fields(sub_agent, actor, record, as_of))
    except Exception as exc:  # noqa: BLE001
        if state.get("task_plan") is not None:
            state["task_plan"] = {**state["task_plan"], "refresh_pending": True, "refresh_error": type(exc).__name__}
        return
    state["outstanding_tasks"] = list(state["task_plan"].get("blocked", []))


def catalog_requirements(state: OnboardingCaseState, deps: OrchestratorDeps) -> list[dict[str, Any]]:
    """UC-03's validatable documents applicable to this employee (type, label, mandatory, reason)."""
    from hr_onboarding_common.orchestration.adapters.uc03_documents import document_catalog

    return [{"document_type": g.document_type, "label": g.label, "required": g.mandatory, "reason": g.reason}
            for g in document_catalog(state, deps)]


def document_due_date(tasks: list[dict[str, Any]], fallback: str | None) -> str | None:
    task = next((t for t in tasks if t.get("template_key") in DOCUMENT_TASK_KEYS), None)
    return str(task["due_date"])[:10] if task and task.get("due_date") else fallback


def plan_record(state: OnboardingCaseState, *, plan_id: str, plan_version: int, required_documents: list[dict],
                due_date: str | None, task_count: int, attributes: list[str], source: str,
                previous_plan_version: int | None = None, reason: str | None = None,
                applied_feedback_ids: list[str] | None = None) -> dict[str, Any]:
    """`onboarding_plan` (workflow_models.OnboardingPlanRecord): ids, versions and document requirements."""
    return OnboardingPlanRecord(
        plan_id=plan_id, plan_version=plan_version, previous_plan_version=previous_plan_version,
        required_documents=[RequiredDocument(document_type=d["document_type"], label=d.get("label") or d["document_type"],
                                             required=bool(d.get("required", True)), due_date=due_date,
                                             reason=d.get("reason")) for d in required_documents],
        effective_joining_date=state.get("joining_date"), task_count=task_count, attributes=sorted(attributes),
        source=source, created_at=now_utc().isoformat(), replanning_reason=reason,
        applied_feedback_ids=list(applied_feedback_ids or []),
    ).model_dump(mode="json")


def record_initial_plan(state: OnboardingCaseState, plan: dict[str, Any]) -> None:
    """Set the initial plan and its history entry (in place). A plan already recorded for the same
    version is kept (a resumed stage never adds a second "initial" entry)."""
    current = state.get("onboarding_plan") or {}
    if current.get("plan_id") == plan["plan_id"] and current.get("plan_version") == plan["plan_version"]:
        return
    state["onboarding_plan"] = plan
    state["plan_history"] = [*(state.get("plan_history") or []), PlanHistoryEntry(
        plan_version=plan["plan_version"], action="initial", reason="initial task planning",
        added_documents=[d["document_type"] for d in plan["required_documents"]], at=plan["created_at"],
    ).model_dump(mode="json")]


def call_uc02_plan_tasks(state: OnboardingCaseState, deps: OrchestratorDeps) -> OnboardingCaseState:
    try:
        require_role(deps, state, settings_key="task_planning_trigger_roles")
    except PolicyViolation as exc:
        return record_policy_denial(state, Stage.PLAN_TASKS, exc)

    role_ctx = state.get("uc02_role_context", {})
    missing = [f for f in ("role_id", "department", "manager_id", "location", "project") if not role_ctx.get(f)]
    if missing:
        new_state = with_stage_status(state, Stage.PLAN_TASKS, StageStatus.BLOCKED)
        new_state["error_code"] = "MISSING_DATA"
        new_state["error_detail"] = f"uc02_role_context missing required fields: {missing}"
        new_state["next_action"] = "hr_review:plan_tasks:missing_role_context"
        return new_state

    try:
        from onboarding_task_management.sub_agent.adapter import WorkflowRequest
    except SyntaxError as exc:
        new_state = with_stage_status(state, Stage.PLAN_TASKS, StageStatus.FAILED)
        new_state["error_code"] = UC02_IMPORT_FAILURE_CODE
        new_state["error_detail"] = (
            f"onboarding_task_management could not be imported ({exc}); fix the UC-02 package, "
            "then retry this stage"
        )
        return new_state

    # Offer acceptance and form approval are facts UC01 verified when it opened the case. They
    # are carried, never assumed: the intake stage must be COMPLETE, the intake input must say the
    # offer was accepted, and the approval timestamps/role must have been supplied with the intake.
    intake_status = (state.get("stage_statuses") or {}).get(Stage.INTAKE.value)
    if intake_status != StageStatus.COMPLETE.value:
        new_state = with_stage_status(state, Stage.PLAN_TASKS, StageStatus.BLOCKED)
        new_state["error_code"] = "PREREQUISITE_NOT_MET"
        new_state["error_detail"] = "UC01 intake is not complete; offer and form approval are unverified"
        new_state["next_action"] = "hr_review:plan_tasks:intake_not_complete"
        return new_state
    offer_accepted = bool((state.get("intake_trigger") or {}).get("offer_accepted"))
    approved_by_role = role_ctx.get("approved_by_role")
    form_approved = bool(role_ctx.get("form_approved_at") and approved_by_role)

    trigger = {
        # Shared identifiers: the orchestrator's case id and correlation id, echoed by UC02.
        "case_id": state["case_id"],
        "correlation_id": state["correlation_id"],
        "client_id": state["client_id"],
        "employee_id": state["employee_id"],
        "joining_date": state["joining_date"],
        "role_id": role_ctx["role_id"],
        "department": role_ctx["department"],
        "manager_id": role_ctx["manager_id"],
        "location": role_ctx["location"],
        "project": role_ctx["project"],
        # Optional checklist inputs. Only what the caller stated is sent: UC-02 never infers them, and
        # an omitted grade / nationality group / attribute simply selects no task that needs it.
        **{k: role_ctx[k] for k in ("grade", "nationality_group") if role_ctx.get(k)},
        **({"attributes": list(role_ctx["attributes"])} if role_ctx.get("attributes") else {}),
        "offer": {
            "employee_id": state["employee_id"],
            "accepted": offer_accepted,
            "accepted_at": role_ctx.get("offer_accepted_at"),
            "offer_reference": state.get("offer_reference"),
        },
        "form": {
            "employee_id": state["employee_id"],
            "submitted": form_approved,
            "approved": form_approved,
            "approved_by_role": approved_by_role,
            "approved_at": role_ctx.get("form_approved_at"),
        },
    }

    from onboarding_task_management.contracts import Actor, DocumentSpec, Role  # type: ignore
    from onboarding_task_management.errors import InvalidStateError  # type: ignore

    stage = Stage.PLAN_TASKS
    try:
        sub_agent = uc02_sub_agent(deps)  # built over the configured store; rebuilt after a restart
    except Exception as exc:  # noqa: BLE001 -- the store cannot be used: reported, never replaced by memory
        return uc02_runtime.stop(state, stage, uc02_runtime.classify(exc, stage),
                                 f"UC-02 could not start: {uc02_runtime.describe(exc)}")
    trace = uc02_runtime.StageRun(sub_agent, state, stage=stage, operation="initial_task_planning")
    try:
        refused = uc02_runtime.verify_case(sub_agent, state, stage)
    except Exception as exc:  # noqa: BLE001 -- the case could not be read: classified, never assumed present
        refused = uc02_runtime.stop(state, stage, uc02_runtime.classify(exc, stage),
                                    f"UC-01's case could not be read: {uc02_runtime.describe(exc)}")
    if refused is not None:
        trace.finished_from(refused, record_exists=False)  # the case is not this tenant's stored case: a reference only
        return refused
    trace.started(record_exists=True)  # the case is UC-01's stored case of this tenant (or UC-02 is not in Supabase mode)

    # UC-02 never reads a clock. The evaluation date is the caller's `as_of` when given, else today --
    # NOT the joining date, which would make every case look like it is already on day one.
    as_of = uc02_runtime.evaluation_date(state)
    try:
        result = sub_agent.run(WorkflowRequest(
            trigger=trigger, as_of=as_of, actor_id=state["actor"]["on_behalf_of"],
            actor_role=Role.HR_OPS, llm_behaviour=UC02_LLM_BEHAVIOUR, prepare_reminders=False,
        ))
    except Exception as exc:  # noqa: BLE001 -- UC02 preconditions raise ValueError/domain errors; never a fake success
        new_state = uc02_runtime.stop(state, stage, uc02_runtime.classify(exc, stage), uc02_runtime.describe(exc))
        # The stage's existing contract: the refusal is also the case's last error (error_code / error_detail).
        new_state["error_code"] = new_state["stage_errors"][stage.value]["code"]
        new_state["error_detail"] = uc02_runtime.describe(exc)
        trace.finished_from(new_state, record_exists=True)
        return new_state

    # The plan's document requirements: UC-02 records them on its plan (idempotent per plan version).
    try:
        catalog = catalog_requirements(state, deps)
    except Exception as exc:  # noqa: BLE001 -- a technical failure, never "no documents needed"
        new_state = with_stage_status(state, Stage.PLAN_TASKS, StageStatus.RETRY_PENDING)
        new_state["error_code"] = "DOCUMENT_CHECKLIST_UNAVAILABLE"
        new_state["error_detail"] = f"UC-03's document catalog could not be loaded ({type(exc).__name__})"
        new_state["next_action"] = "retry:plan_tasks:document_checklist_unavailable"
        trace.finished("retry_pending", record_exists=True, code="DOCUMENT_CHECKLIST_UNAVAILABLE")
        return new_state

    uc02_actor = Actor(actor_id=state["actor"]["on_behalf_of"], role=Role.HR_OPS, client_id=state["client_id"])
    try:
        try:
            record = sub_agent.service.define_document_requirements(
                uc02_actor, result.onboarding_id,
                [DocumentSpec(document_type=d["document_type"], mandatory=d["required"], reason=d["reason"][:200])
                 for d in catalog])
        except InvalidStateError:
            # Defined earlier (a resumed stage, or a plan UC-02 already replanned): UC-02's record is authoritative.
            record = sub_agent.service.get(uc02_actor, result.onboarding_id)
    except Exception as exc:  # noqa: BLE001 -- the plan exists in UC-02 but its record could not be read or saved
        new_state = uc02_runtime.stop(state, stage, uc02_runtime.classify(exc, stage),
                                      f"UC-02 planned but its record could not be completed: {uc02_runtime.describe(exc)}")
        trace.finished_from(new_state, record_exists=True)
        return new_state
    projection = uc02_runtime.plan_fields(sub_agent, uc02_actor, record, as_of)  # informational, never blocks
    labels = {d["document_type"]: d["label"] for d in catalog}

    checklist = result.checklist
    review_required = bool(checklist.get("review_required"))
    new_state = dict(state)
    record_initial_plan(new_state, plan_record(
        state, plan_id=result.onboarding_id, plan_version=record.plan_version,
        required_documents=[{"document_type": d.key, "label": labels.get(d.key, d.key), "required": d.mandatory,
                             "reason": d.reason} for d in record.documents],
        due_date=document_due_date(checklist.get("tasks", []), state.get("joining_date")),
        task_count=len(checklist.get("tasks", [])), attributes=list(record.attributes), source="uc02"))
    new_state["task_plan"] = {
        "onboarding_id": result.onboarding_id, "created": result.created,
        "case_id": result.case_id, "correlation_id": result.correlation_id,
        "tasks": checklist.get("tasks", []),
        "ready": checklist.get("ready", []), "blocked": checklist.get("blocked", []),
        # What UC-02 itself concluded, so the orchestrator never sees a bare "done".
        "review_required": review_required, "issues": checklist.get("issues", []),
        "agent_status": result.agent_run.get("status"), "advice_degraded": bool(result.advice.get("degraded")),
        "advice_source": result.advice.get("source"), "agent_run_id": result.agent_run.get("run_id"),
        # UC-02's authoritative record, projected: plan version (= plan_revisions.revision in Supabase), task,
        # required-task and dependency counts, the dependency graph, configuration version and store.
        **projection,
        "plan_version": record.plan_version,
        "planned_at": now_utc().isoformat(),
    }
    new_state["outstanding_tasks"] = list(checklist.get("blocked", []))
    for proposal in result.agent_run.get("proposals", []) or []:
        add_pending_approval(new_state, agent=UC02_AGENT, reference=proposal.get("action", ""),
                              role=proposal.get("requires_approval_by", "hr_admin"),
                              description="UC02 restricted-action proposal (visibility only; decided via UC02's own gate)")
    if review_required:
        # UC-02 does not guess a client or legal requirement: unresolved configuration issues mean the
        # plan exists but a person must review it before it is treated as done.
        new_state = with_stage_status(new_state, Stage.PLAN_TASKS, StageStatus.BLOCKED)
        new_state["error_code"] = "CHECKLIST_REVIEW_REQUIRED"
        new_state["error_detail"] = (
            "UC02 flagged unresolved checklist configuration issues: "
            + ", ".join(sorted({i.get("code", "?") for i in checklist.get("issues", [])}))
        )
        new_state["next_action"] = "hr_review:plan_tasks:configuration_review"
        add_blocker(new_state, stage=stage, code="CHECKLIST_REVIEW_REQUIRED", reason=new_state["error_detail"][:500],
                    owner="hr_admin", next_action="Review the client's checklist configuration issues, then resume")
        trace.finished("blocked", record_exists=True, code="CHECKLIST_REVIEW_REQUIRED")
        return new_state
    new_state = with_stage_status(new_state, Stage.PLAN_TASKS, StageStatus.COMPLETE)
    trace.finished("completed", record_exists=True, task_count=int(projection.get("task_count") or 0),
                   dependency_count=int(projection.get("dependency_count") or 0), plan_version=record.plan_version)
    new_state["task_plan"]["trace_recorded"] = trace.recorded
    return new_state


# ------------------------------------------------------------------------- UC02's date-change gate


def _available_sub_agent(deps: OrchestratorDeps):
    """UC-02's sub-agent for a gate operation: the cached one, or one rebuilt over the configured store after a
    restart. A store that cannot be used is a prerequisite the caller reports (409), never a silent fallback."""
    try:
        return uc02_sub_agent(deps)
    except Exception as exc:  # noqa: BLE001
        raise PrerequisiteNotMetError(f"UC02 is not available for this case: {uc02_runtime.describe(exc)}") from None


def _require_record(sub_agent, actor, onboarding_id: str) -> None:
    """UC-02 must actually hold the onboarding (a rebuilt in-memory UC-02 after a restart does not): reported as a
    prerequisite, never answered with a record UC-02 does not have. Another tenant's record still raises UC-02's
    own isolation error."""
    from onboarding_task_management.errors import InvalidStateError  # type: ignore

    try:
        sub_agent.service.get(actor, onboarding_id)
    except InvalidStateError as exc:
        raise PrerequisiteNotMetError(f"UC02 holds no onboarding record {onboarding_id} for this case "
                                      f"({exc.message}); plan the case again before changing its date") from None


def request_uc02_date_change(state: OnboardingCaseState, deps: OrchestratorDeps, *, joining_date: date,
                             actor: dict[str, Any]) -> tuple[OnboardingCaseState, dict[str, Any]]:
    """Open UC02's real DATE_CHANGE gate for this case's onboarding record (`service.request_change`).
    Nothing is re-planned yet, and `state["joining_date"]` is not changed here -- only a later
    approved, applied decision does that (`relay_uc02_decision`). Raises PrerequisiteNotMetError
    when plan_tasks has not completed (no onboarding_id to act on) or UC02's sub-agent instance is
    not available; propagates whatever UC02 itself raises for an invalid request (the onboarding is
    not active, the date does not change anything, or it falls on an uncovered day)."""
    plan = state.get("task_plan") or {}
    onboarding_id = plan.get("onboarding_id")
    if state["stage_statuses"].get(Stage.PLAN_TASKS.value) != StageStatus.COMPLETE.value or not onboarding_id:
        raise PrerequisiteNotMetError("UC02 has no onboarding record for this case yet (plan_tasks incomplete)")
    if state.get("joining_day_event"):
        raise PrerequisiteNotMetError("the joining-day event has already been recorded for this case; "
                                      "its joining date can no longer be changed")
    sub_agent = _available_sub_agent(deps)

    from onboarding_task_management.contracts import Actor, Role

    uc02_actor = Actor(actor_id=actor["on_behalf_of"], role=Role.HR_OPS, client_id=state["client_id"])
    _require_record(sub_agent, uc02_actor, onboarding_id)
    request = sub_agent.service.request_change(uc02_actor, onboarding_id, joining_date=joining_date)
    new_state = copy.deepcopy(state)
    add_pending_approval(new_state, agent=UC02_AGENT, reference=request.request_id, role="hr_ops",
                         description=f"UC02 joining-date change to {joining_date.isoformat()}",
                         stage=Stage.PLAN_TASKS, gate=request.gate.value, proposal_type="date_change",
                         record_id=onboarding_id)
    return new_state, {"request_id": request.request_id, "gate": request.gate.value,
                       "joining_date": joining_date.isoformat()}


def relay_uc02_decision(state: OnboardingCaseState, deps: OrchestratorDeps, *, approval_id: str, decision: str,
                        approver: dict[str, Any], reason_code: str | None = None,
                        note: str | None = None) -> OnboardingCaseState:
    """Decide UC02's date-change gate (`service.decide_change`) and, if approved, apply it
    (`service.apply_change` -- UC02 re-plans the affected tasks). Idempotent: no matching pending
    entry (already decided) is a no-op, never a second call to UC02. Rejected: recorded, no change
    to `state["joining_date"]`. Approved and applied: `state["joining_date"]` is set to the exact
    date originally requested, as the next revision, with provenance and history recorded. The
    orchestrator reconciles the results produced for the earlier date on its next run."""
    entry = next((p for p in state["pending_approvals"]
                 if p["agent"] == UC02_AGENT and p["reference"] == approval_id), None)
    if entry is None:
        return state
    sub_agent = _available_sub_agent(deps)

    from onboarding_task_management.contracts import Actor, DecisionOutcome, Role

    uc02_actor = Actor(actor_id=approver["on_behalf_of"], role=Role.HR_OPS, client_id=state["client_id"])
    outcome = DecisionOutcome.APPROVED if decision == "approved" else DecisionOutcome.REJECTED
    request = sub_agent.service.decide_change(uc02_actor, approval_id, outcome, reason=note or reason_code or "")
    new_state = copy.deepcopy(state)
    new_state["pending_approvals"] = [p for p in new_state["pending_approvals"] if p["reference"] != approval_id]
    decision_entry = {"agent": UC02_AGENT, "reference": approval_id, "stage": Stage.PLAN_TASKS.value,
                      "kind": "decision", "proposal_type": "date_change", "decision": decision,
                      "approver": approver.get("on_behalf_of"), "decided_at": now_utc().isoformat(),
                      "applied": False}
    if outcome is DecisionOutcome.REJECTED:
        new_state.setdefault("approval_decisions", []).append(decision_entry)
        return new_state

    replanned = sub_agent.service.apply_change(uc02_actor, approval_id)
    # The exact date originally requested, as the next joining-date revision (provenance and history).
    joining_date.record_change(new_state, str(request.payload["joining_date"])[:10], stage=Stage.PLAN_TASKS,
                               use_case_id="UC02", source="date_change_approval", approval_id=approval_id,
                               at=now_utc().isoformat())
    # UC-02 versioned its plan for the change: the workflow's plan projection follows, so UC-03's
    # collection and any later replan refer to the current version (never a stale one).
    plan = new_state.get("onboarding_plan")
    onboarding_id = (new_state.get("task_plan") or {}).get("onboarding_id")
    if plan and onboarding_id:
        current = sub_agent.service.get(uc02_actor, onboarding_id)
        if current.plan_version != plan["plan_version"]:
            at = now_utc().isoformat()
            task = next((t for t in current.tasks if t.template_key in DOCUMENT_TASK_KEYS), None)
            due = task.due_date.isoformat() if task else new_state.get("joining_date")
            new_state["onboarding_plan"] = {
                **plan, "plan_version": current.plan_version, "previous_plan_version": plan["plan_version"],
                "effective_joining_date": new_state.get("joining_date"), "replanning_reason": "joining_date_change",
                "required_documents": [{**d, "due_date": due} for d in plan["required_documents"]], "created_at": at}
            new_state["plan_history"] = [*(new_state.get("plan_history") or []), PlanHistoryEntry(
                plan_version=current.plan_version, previous_plan_version=plan["plan_version"], action="date_change",
                reason=f"joining date changed (approval {approval_id})",
                modified_tasks=list(replanned.rescheduled_task_ids), added_tasks=list(replanned.added_task_ids),
                removed_tasks=list(replanned.removed_task_ids), at=at).model_dump(mode="json")]
            refresh_task_plan(new_state, sub_agent, uc02_actor, current)
    decision_entry["applied"] = True
    decision_entry["joining_date_revision"] = joining_date.revision(new_state)
    new_state.setdefault("approval_decisions", []).append(decision_entry)
    return new_state
