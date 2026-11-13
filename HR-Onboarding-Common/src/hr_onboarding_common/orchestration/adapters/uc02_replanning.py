"""Stage adapter: UC-02 replanning on UC-03's document-validation feedback.

    orchestrator stage `replan_tasks` -> call_uc02_replan_tasks
        -> OnboardingService.replan_documents(DocumentFeedback)       (UC-02, sub_agent/service.py)
        -> RevisedPlan (the next plan version) -> UC-03 applies it (document_collection re-opened)

UC-03 reports through `document_validation_feedback` (workflow_models.DocumentValidationFeedback) that
the plan must change: a mandatory document UC-03's catalog requires that the plan does not list, an
applicability attribute the employee's documents establish (e.g. `visa_required`), or an HR change to
the document requirements. UC-02 replans -- adds or drops document requirements, switches on the task
templates of the new attribute, and creates the next plan version -- and this adapter records the
result as the new `onboarding_plan`, a `plan_history` entry and the `revised_plan` UC-03 applies next.

Guarantees:
  * Idempotent: UC-02 applies a feedback id once. A retry after a crash between UC-02's change and the
    orchestrator's write gets UC-02's first outcome back (`duplicate_replay`), never a second version.
  * Stale feedback (validated against another plan version than the current one) is never applied:
    BLOCKED with STALE_PLAN_VERSION; the documents are revalidated against the current plan first.
  * A failed or refused replan never pretends the plan changed: BLOCKED (REPLANNING_FAILED, a person
    resolves it) or RETRY_PENDING (UC02_REPLANNING_UNAVAILABLE, a technical failure); UC-03 keeps
    validating against the plan version it has, and UC-04/UC-05 do not start.
  * Restart-safe: UC-02's sub-agent is rebuilt over the configured store (adapters/uc02_runtime.py) when the
    orchestrator restarted; in Supabase mode UC-02 loads the plan from there and the replan continues. Only
    planning-impacting feedback reaches this stage -- a document that merely needs correcting stays in UC-03
    (route_after_document_result), so no plan version is created for it.
  * The new plan version is UC-02's: in Supabase mode it is a new `plan_revisions` row (revision = plan
    version; the earlier rows stay as they were) and the task rows of the new version; `task_plan` is refreshed
    from UC-02's record, and the execution is traced in UC-02's audit_events / agent_runs.
"""

from __future__ import annotations

import copy
from typing import Any

from hr_onboarding_common.orchestration.adapters import uc02_runtime
from hr_onboarding_common.orchestration.adapters._shared import (add_blocker, record_policy_denial,
                                                                 record_stage_error, require_role, with_stage_status)
from hr_onboarding_common.orchestration.adapters.uc02_tasks import (DOCUMENT_TASK_KEYS, plan_record, refresh_task_plan,
                                                                    uc02_sub_agent)
from hr_onboarding_common.orchestration.adapters.uc03_documents import document_catalog
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import OnboardingCaseState, Stage, StageStatus
from hr_onboarding_common.orchestration.workflow_models import PlanHistoryEntry, RevisedPlan
from hr_onboarding_common.platform.policy import PolicyViolation

STAGE = Stage.REPLAN_TASKS


def _stop(state: OnboardingCaseState, status: StageStatus, code: str, detail: str, *, owner: str,
          action: str) -> OnboardingCaseState:
    new_state = with_stage_status(state, STAGE, status)
    record_stage_error(new_state, STAGE, code=code, detail=detail, retryable=status != StageStatus.FAILED)
    new_state["next_action"] = f"{'retry' if status == StageStatus.RETRY_PENDING else 'hr_review'}:{STAGE.value}:" \
                               f"{code.lower()}"
    add_blocker(new_state, stage=STAGE, code=code, reason=detail, owner=owner, next_action=action)
    return new_state


def _changes(feedback: dict[str, Any], action: str) -> list[str]:
    return [c["target"] for c in feedback.get("recommended_task_changes") or [] if c["action"] == action]


def check_feedback(state: OnboardingCaseState) -> OnboardingCaseState | None:
    """The refusal when the feedback cannot be replanned on (missing, or for another plan version), else None."""
    feedback = state.get("document_validation_feedback") or {}
    plan = state.get("onboarding_plan") or {}
    if not feedback.get("replanning_required"):
        return _stop(state, StageStatus.BLOCKED, "VALIDATION_FEEDBACK_MISSING",
                     "UC03 reported no plan change to make", owner="ops",
                     action="Revalidate the documents in UC03, then resume")
    if feedback.get("plan_version") != plan.get("plan_version"):
        return _stop(state, StageStatus.BLOCKED, "STALE_PLAN_VERSION",
                     f"UC03's feedback is for plan version {feedback.get('plan_version')}, the plan is at version "
                     f"{plan.get('plan_version')}; it is not applied", owner="ops",
                     action="Revalidate the documents against the current plan version, then resume")
    return None


def apply_revised_plan(state: OnboardingCaseState, *, plan_version: int, required_documents: list[dict[str, Any]],
                       added_tasks: list[str], removed_tasks: list[str], modified_tasks: list[str],
                       attributes: list[str], task_count: int, due_date: str | None, source: str,
                       duplicate_replay: bool = False) -> OnboardingCaseState:
    """The replan's result written to the state (a new dict): the next `onboarding_plan`, its history
    entry, the `revised_plan` for UC03, and the HR requirement changes it applied. COMPLETE."""
    feedback = state["document_validation_feedback"]
    plan = state["onboarding_plan"]
    reason = "; ".join(feedback.get("replanning_reasons") or []) or "document validation feedback"
    before = {d["document_type"] for d in plan["required_documents"]}
    after = {d["document_type"] for d in required_documents}
    new_plan = plan_record(state, plan_id=plan["plan_id"], plan_version=plan_version, required_documents=required_documents,
                           due_date=due_date, task_count=task_count, attributes=attributes, source=source,
                           previous_plan_version=plan["plan_version"], reason=reason[:500],
                           applied_feedback_ids=[*plan.get("applied_feedback_ids", []), feedback["feedback_id"]])
    new_state = copy.deepcopy(state)
    new_state["onboarding_plan"] = new_plan
    new_state["plan_history"] = [*(state.get("plan_history") or []), PlanHistoryEntry(
        plan_version=plan_version, previous_plan_version=plan["plan_version"], action="replanned", reason=reason[:500],
        feedback_id=feedback["feedback_id"], added_documents=sorted(after - before),
        removed_documents=sorted(before - after), added_tasks=added_tasks, removed_tasks=removed_tasks,
        modified_tasks=modified_tasks, at=new_plan["created_at"]).model_dump(mode="json")]
    new_state["revised_plan"] = RevisedPlan(
        plan_id=plan["plan_id"], plan_version=plan_version, previous_plan_version=plan["plan_version"],
        updated_required_documents=new_plan["required_documents"], added_tasks=added_tasks, removed_tasks=removed_tasks,
        modified_tasks=modified_tasks, effective_joining_date=new_plan["effective_joining_date"],
        replanning_reason=reason[:500], feedback_id=feedback["feedback_id"], duplicate_replay=duplicate_replay,
    ).model_dump(mode="json")
    applied = set(feedback.get("change_request_ids") or [])
    new_state["requirement_change_requests"] = [
        {**r, "status": "applied", "applied_in_plan_version": plan_version} if r["request_id"] in applied else r
        for r in state.get("requirement_change_requests") or []]
    return with_stage_status(new_state, STAGE, StageStatus.COMPLETE)


def call_uc02_replan_tasks(state: OnboardingCaseState, deps: OrchestratorDeps) -> OnboardingCaseState:
    try:
        require_role(deps, state, settings_key="task_planning_trigger_roles")
    except PolicyViolation as exc:
        return record_policy_denial(state, STAGE, exc)
    refused = check_feedback(state)
    if refused is not None:
        return refused
    try:
        from onboarding_task_management.contracts import Actor, DocumentFeedback, DocumentSpec, Role  # type: ignore
        from onboarding_task_management.errors import OnboardingError, StalePlanVersionError  # type: ignore
        from onboarding_task_management.supabase_rest import SupabaseUnavailableError  # type: ignore
    except Exception as exc:  # noqa: BLE001 -- UC02's package cannot be imported
        return _stop(state, StageStatus.RETRY_PENDING, "UC02_REPLANNING_UNAVAILABLE",
                     f"UC02 could not be imported ({type(exc).__name__}); the plan was not changed", owner="ops",
                     action="Fix the UC02 package, then retry this stage")
    try:
        sub_agent = uc02_sub_agent(deps)  # the cached one, or rebuilt over the same store after a restart
    except Exception as exc:  # noqa: BLE001 -- UC02's store cannot be used: the plan was not changed
        failure = uc02_runtime.classify(exc, STAGE)
        if failure.status == StageStatus.RETRY_PENDING:
            return _stop(state, StageStatus.RETRY_PENDING, "UC02_REPLANNING_UNAVAILABLE",
                         f"UC02 is not available ({uc02_runtime.describe(exc)}); the plan was not changed",
                         owner="ops", action="Retry once UC02's database is reachable")
        return _stop(state, StageStatus.BLOCKED, "REPLANNING_FAILED",
                     f"UC02 could not start ({uc02_runtime.describe(exc)}); the plan was not changed",
                     owner=failure.owner, action=failure.action)
    trace = uc02_runtime.StageRun(sub_agent, state, stage=STAGE, operation="replan_from_document_feedback")
    trace.started(record_exists=True)

    feedback = state["document_validation_feedback"]
    plan = state["onboarding_plan"]
    labels = {g["target"]: g.get("reason", "") for g in feedback.get("recommended_task_changes") or []}
    actor = Actor(actor_id=state["actor"]["on_behalf_of"], role=Role.HR_OPS, client_id=state["client_id"])
    try:
        outcome = sub_agent.service.replan_documents(actor, plan["plan_id"], DocumentFeedback(
            feedback_id=feedback["feedback_id"], plan_version=plan["plan_version"],
            add_documents=[DocumentSpec(document_type=t, mandatory=True, reason=labels.get(t, "")[:200])
                           for t in _changes(feedback, "add_document")],
            remove_documents=_changes(feedback, "remove_document"),
            add_attributes=_changes(feedback, "add_attribute"),
            reasons=list(feedback.get("replanning_reasons") or [])))
        record = sub_agent.service.get(actor, plan["plan_id"])
    except StalePlanVersionError as exc:
        refused = _stop(state, StageStatus.BLOCKED, "STALE_PLAN_VERSION", f"UC02 refused the feedback: {exc.message}",
                        owner="ops", action="Revalidate the documents against UC02's current plan version, then resume")
        trace.finished_from(refused, record_exists=True)
        return refused
    except SupabaseUnavailableError as exc:  # technical: retried; UC-02's ledger makes the retry idempotent
        refused = _stop(state, StageStatus.RETRY_PENDING, "UC02_REPLANNING_UNAVAILABLE",
                        f"UC02's database is unavailable ({uc02_runtime.describe(exc)}); retry this stage",
                        owner="ops", action="Retry once UC02's database is reachable")
        trace.finished_from(refused, record_exists=True)
        return refused
    except OnboardingError as exc:
        refused = _stop(state, StageStatus.BLOCKED, "REPLANNING_FAILED", f"UC02 could not replan ({exc.code}): "
                        f"{exc.message}", owner="hr_admin",
                        action="Resolve the cause in UC02 (or withdraw the requirement change), then resume")
        trace.finished_from(refused, record_exists=True)
        return refused
    except Exception as exc:  # noqa: BLE001 -- a technical failure: the plan is known not to have changed here
        refused = _stop(state, StageStatus.RETRY_PENDING, "UC02_REPLANNING_UNAVAILABLE",
                        f"UC02 replanning failed ({type(exc).__name__}); the plan was not changed", owner="ops",
                        action="Retry this stage")
        trace.finished_from(refused, record_exists=True)
        return refused

    known = {d["document_type"]: d.get("label") for d in plan["required_documents"]}
    try:
        known = {**{g.document_type: g.label for g in document_catalog(state, deps)}, **known}
    except Exception:  # noqa: BLE001 -- labels only: a document without one is shown by its type
        pass
    task = next((t for t in record.tasks if t.template_key in DOCUMENT_TASK_KEYS), None)
    new_state = apply_revised_plan(
        state, plan_version=outcome.plan_version,
        required_documents=[{"document_type": d.key, "label": known.get(d.key) or d.key,
                             "required": d.mandatory, "reason": d.reason} for d in record.documents],
        added_tasks=list(outcome.added_task_ids), removed_tasks=list(outcome.removed_task_ids),
        modified_tasks=list(outcome.rescheduled_task_ids), attributes=list(record.attributes),
        task_count=len(record.tasks), due_date=task.due_date.isoformat() if task else state.get("joining_date"),
        source="uc02", duplicate_replay=outcome.duplicate_replay)
    # The task view follows UC-02's new plan version (the replan committed in UC-02 either way).
    refresh_task_plan(new_state, sub_agent, actor, record)
    trace.finished("replanned", record_exists=True, plan_version=outcome.plan_version,
                   added_tasks=len(outcome.added_task_ids), removed_tasks=len(outcome.removed_task_ids),
                   duplicate_replay=int(outcome.duplicate_replay))
    return new_state


__all__ = ["STAGE", "apply_revised_plan", "call_uc02_replan_tasks", "check_feedback"]
