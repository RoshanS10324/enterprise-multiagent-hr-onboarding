"""The state-driven workflow's bookkeeping, kept out of the graph module so it can be tested on
plain dicts: which UC operation each stage is, what it is handed, how a parallel branch's outcome is
captured and folded back, and the derived view (workflow/branch status, completed stages, pending
actions, the stage that most needs attention).

Parallel branches (state.Branch) never write the shared fields directly. A branch node returns its
cumulative outcome under its own key of the `branch_outcomes` channel (reducer:
state.merge_branch_outcomes); `merge_outcomes` folds every branch's outcome into the shared fields.
A fold replaces only what the branch owns -- its stages' statuses, errors, attempts, outcomes,
blockers and approvals, and its result fields (state.BRANCH_RESULT_KEYS) -- and appends its new
history events, so no branch can overwrite another's results.
"""

from __future__ import annotations

import copy
from typing import Any

from hr_onboarding_common.orchestration import joining_date
from hr_onboarding_common.orchestration.state import (BRANCH_RESULT_KEYS, BRANCH_STAGES, OPTIONAL_STAGES,
                                                      REQUIRED_STAGES, STAGE_ORDER, WORKFLOW_STATUS_PRIORITY,
                                                      Branch, OnboardingCaseState, Stage, StageStatus, WorkflowStatus,
                                                      derive_branch_status, derive_overall_status,
                                                      derive_workflow_status, stage_workflow_status)
from hr_onboarding_common.orchestration.workflow_models import BranchStatusRecord, PendingAction

# stage -> (use case, operation, the state field holding its output)
STAGE_OPERATIONS: dict[Stage, tuple[str, str, str | None]] = {
    Stage.INTAKE: ("UC01", "create_employee_record", "employee_id"),
    # result keys kept from the previous workflow (API clients read them); the plan itself is in
    # onboarding_plan and UC03's feedback in document_validation_feedback.
    Stage.PLAN_TASKS: ("UC02", "initial_task_planning", "task_plan"),
    Stage.DOCUMENT_COLLECTION: ("UC03", "initiate_document_collection", "document_collection"),
    Stage.DOCUMENT_SUBMISSION: ("UC03", "await_required_documents", "submitted_documents"),
    Stage.VALIDATE_DOCUMENTS: ("UC03", "validate_documents", "document_validation_result"),
    Stage.REPLAN_TASKS: ("UC02", "replan_from_document_feedback", "onboarding_plan"),
    Stage.EQUIPMENT_ACCESS: ("UC04", "equipment_access_readiness", "equipment_access_result"),
    Stage.ORIENTATION_SCHEDULING: ("UC05", "orientation_scheduling", "orientation_schedule_result"),
    Stage.BUDDY_ASSIGNMENT: ("UC05", "buddy_assignment", "buddy_assignment_result"),
    Stage.TEAM_INTRODUCTION: ("UC05", "team_introduction", "team_introduction_result"),
    Stage.BUDDY_INTRODUCTION: ("UC05", "buddy_introduction", "buddy_introduction_result"),
    Stage.DAY_ONE_READINESS: ("orchestrator", "day_one_readiness_signoff", None),
    Stage.JOINING_DAY_WELCOME: ("UC05", "joining_day_welcome", "welcome_message_result"),
}

BRANCH_OF: dict[Stage, Branch] = {stage: branch for branch, stages in BRANCH_STAGES.items() for stage in stages}
# Every stage downstream of document verification that reads `verified_context`: the two parallel
# branches (UC02 plans before the documents now, so plan_tasks is not one of them).
_VERIFIED_CONTEXT_STAGES: frozenset[Stage] = frozenset(BRANCH_OF)


def _plan_ref(state: OnboardingCaseState) -> dict[str, Any]:
    plan = state.get("onboarding_plan") or {}
    return {"plan_id": plan.get("plan_id"), "plan_version": plan.get("plan_version")}


def stage_inputs(stage: Stage, state: OnboardingCaseState) -> dict[str, Any]:
    """What a stage is handed from earlier stages -- ids, dates, flags and document types only. Recorded
    in its StageOutcome so the data handoff is visible per case."""
    role = state.get("uc02_role_context") or {}
    # The joining date and its revision travel together, so every outcome records which date it used
    # (joining_date.py compares them with the current date to find stale results).
    base = {"case_id": state.get("case_id"), "client_id": state.get("client_id"),
            "employee_id": state.get("employee_id"), "joining_date": state.get("joining_date"),
            "joining_date_revision": joining_date.revision(state)}
    collection = state.get("document_collection") or {}
    submitted = state.get("submitted_documents") or {}
    if stage == Stage.INTAKE:
        return {"client_id": state.get("client_id"), "offer_reference": state.get("offer_reference")}
    if stage == Stage.PLAN_TASKS:
        return base | {k: role.get(k) for k in ("role_id", "department", "manager_id", "location", "project")}
    if stage == Stage.DOCUMENT_COLLECTION:
        plan = state.get("onboarding_plan") or {}
        return base | _plan_ref(state) | {"required_documents": [d["document_type"] for d in
                                                                  plan.get("required_documents") or []]}
    if stage == Stage.DOCUMENT_SUBMISSION:
        return base | _plan_ref(state) | {
            "required_documents": [d["document_type"] for d in collection.get("required_documents", [])
                                   if d.get("required")],
            "submitted": {t: d.get("revision") for t, d in sorted(submitted.items())}}
    if stage == Stage.VALIDATE_DOCUMENTS:
        return base | _plan_ref(state) | {
            "documents": {t: {"revision": d.get("revision"), "document_ref": d.get("document_ref"),
                              "hr_review": (d.get("hr_review") or {}).get("decision")}
                          for t, d in sorted(submitted.items())}}
    if stage == Stage.REPLAN_TASKS:
        feedback = state.get("document_validation_feedback") or {}
        return base | _plan_ref(state) | {"feedback_id": feedback.get("feedback_id"),
                                          "feedback_plan_version": feedback.get("plan_version"),
                                          "replanning_reasons": list(feedback.get("replanning_reasons") or [])}
    if stage in _VERIFIED_CONTEXT_STAGES:
        verified = state.get("verified_context") or {}
        inputs = base | _plan_ref(state) | {
            "documents_verified": bool(verified.get("documents_verified")),
            "verified_document_types": list(verified.get("verified_document_types") or []),
            "verified_at": verified.get("verified_at"),
            "verified_plan_version": verified.get("plan_version")}
        if stage == Stage.EQUIPMENT_ACCESS:
            # UC04's input contract has no task-plan field: nothing of the plan is passed, and UC04 does
            # not wait for it. Recorded so the absence is explicit, not silent.
            inputs |= {"role_id": role.get("role_id"), "task_plan_available": bool(state.get("task_plan"))}
        if stage in (Stage.ORIENTATION_SCHEDULING, Stage.BUDDY_ASSIGNMENT, Stage.TEAM_INTRODUCTION):
            inputs |= {"canonical_employee_id": (state.get("employee_ref") or {}).get("employee_id"),
                       "offer_acceptance_event_id": (state.get("intake_trigger") or {}).get("event_id"),
                       "task_plan_available": bool(state.get("task_plan"))}
        return inputs
    if stage == Stage.JOINING_DAY_WELCOME:
        return base | {"joining_day_event": bool(state.get("joining_day_event"))}
    return base


def stage_result_summary(stage: Stage, state: OnboardingCaseState) -> dict[str, Any] | None:
    """The compact result of a stage run, kept in its StageOutcome (`stage_outcomes.<stage>.result_summary`, a
    column of case_orchestration_state): ids, versions, counts and codes only. UC-02's two stages carry what the
    next stage and an operator need without reading `orchestrator_context`; the other stages keep None."""
    status = (state.get("stage_statuses") or {}).get(stage.value)
    error = ((state.get("stage_errors") or {}).get(stage.value) or {}).get("code")
    blockers = sorted({b["code"] for b in state.get("blockers") or [] if b.get("stage") == stage.value})
    if stage == Stage.PLAN_TASKS:
        plan, tasks = state.get("onboarding_plan") or {}, state.get("task_plan") or {}
        if status != StageStatus.COMPLETE.value and not tasks:
            return {"status": status, "error_code": error, "blockers": blockers}
        return {
            "status": status, "error_code": error, "blockers": blockers,
            "plan_id": plan.get("plan_id") or tasks.get("onboarding_id"),
            "plan_revision": plan.get("plan_version") or tasks.get("plan_version"),
            "task_count": tasks.get("task_count", len(tasks.get("tasks") or [])),
            "required_task_count": tasks.get("required_task_count"),
            "dependency_count": tasks.get("dependency_count"),
            "ready_count": len(tasks.get("ready") or []), "blocked_count": len(tasks.get("blocked") or []),
            "required_documents": [d["document_type"] for d in plan.get("required_documents") or []],
            "config_version": tasks.get("config_version"), "store": tasks.get("store"),
            "created": tasks.get("created"), "agent_run_id": tasks.get("agent_run_id"),
            "planned_at": tasks.get("planned_at"),
        }
    if stage == Stage.REPLAN_TASKS:
        revised, plan, tasks = state.get("revised_plan") or {}, state.get("onboarding_plan") or {}, state.get("task_plan") or {}
        if status != StageStatus.COMPLETE.value:
            return {"status": status, "error_code": error, "blockers": blockers}
        return {
            "status": status, "error_code": error, "blockers": blockers,
            "plan_id": plan.get("plan_id"), "plan_revision": plan.get("plan_version"),
            "previous_plan_revision": plan.get("previous_plan_version"),
            "feedback_id": revised.get("feedback_id"), "duplicate_replay": revised.get("duplicate_replay"),
            "added_tasks": list(revised.get("added_tasks") or []),
            "removed_tasks": list(revised.get("removed_tasks") or []),
            "modified_tasks": list(revised.get("modified_tasks") or []),
            "required_documents": [d["document_type"] for d in plan.get("required_documents") or []],
            "task_count": tasks.get("task_count", len(tasks.get("tasks") or [])),
            "dependency_count": tasks.get("dependency_count"), "store": tasks.get("store"),
        }
    return None


# ------------------------------------------------------------------------- branch outcomes


def branch_delta(result: OnboardingCaseState, branch: Branch, *, history_from: int) -> dict[str, Any]:
    """What `branch` changed: the current values of everything it owns, and its history events from
    index `history_from` on (stage_history is append-only)."""
    stages = {s.value for s in BRANCH_STAGES[branch]}
    return {
        "branch": branch.value,
        "stage_statuses": {s: v for s, v in result["stage_statuses"].items() if s in stages},
        "stage_errors": {s: v for s, v in result["stage_errors"].items() if s in stages},
        "stage_attempts": {s: v for s, v in result["stage_attempts"].items() if s in stages},
        "stage_outcomes": {s: v for s, v in result.get("stage_outcomes", {}).items() if s in stages},
        "retry_metadata": {s: v for s, v in result.get("retry_metadata", {}).items() if s in stages},
        "idempotency_keys": {s: v for s, v in result["idempotency_keys"].items() if s in stages},
        "stage_history": list(result["stage_history"][history_from:]),
        "blockers": [b for b in result["blockers"] if b["stage"] in stages],
        "pending_approvals": [p for p in result["pending_approvals"] if p.get("stage") in stages],
        "results": {key: copy.deepcopy(result.get(key)) for key in BRANCH_RESULT_KEYS[branch]},
        "field_provenance": {key: copy.deepcopy(value) for key, value in (result.get("field_provenance") or {}).items()
                             if key in BRANCH_RESULT_KEYS[branch]},
    }


def fold_branch_outcome(state: OnboardingCaseState, delta: dict[str, Any] | None) -> OnboardingCaseState:
    """`state` with one branch's outcome applied (a new dict; `state` is not changed)."""
    if not delta:
        return state
    stages = {s.value for s in BRANCH_STAGES[Branch(delta["branch"])]}
    new_state = dict(state)
    new_state["stage_statuses"] = {**state["stage_statuses"], **delta["stage_statuses"]}
    for key in ("stage_errors", "stage_outcomes", "retry_metadata"):
        new_state[key] = {s: v for s, v in (state.get(key) or {}).items() if s not in stages} | delta[key]
    new_state["stage_attempts"] = {**state["stage_attempts"], **delta["stage_attempts"]}
    new_state["idempotency_keys"] = {**state["idempotency_keys"], **delta["idempotency_keys"]}
    new_state["stage_history"] = list(state["stage_history"]) + list(delta["stage_history"])
    new_state["blockers"] = [b for b in state["blockers"] if b["stage"] not in stages] + list(delta["blockers"])
    new_state["pending_approvals"] = [p for p in state["pending_approvals"]
                                      if p.get("stage") not in stages] + list(delta["pending_approvals"])
    for key, value in delta["results"].items():
        new_state[key] = copy.deepcopy(value)
    if delta.get("field_provenance"):  # absent on a delta written before provenance existed
        new_state["field_provenance"] = {**(state.get("field_provenance") or {}), **delta["field_provenance"]}
    return new_state


# ------------------------------------------------------------------------- the derived view


def _rank(status: WorkflowStatus) -> int:
    priority = WORKFLOW_STATUS_PRIORITY
    return priority.index(status) if status in priority else len(priority)


def primary_stage(state: OnboardingCaseState) -> Stage:
    """The required stage that most needs attention: the most severe workflow status, then the
    earliest in workflow order. Deterministic whatever order parallel branches finished in."""
    open_stages = [s for s in REQUIRED_STAGES if stage_workflow_status(state, s) != WorkflowStatus.COMPLETED]
    if not open_stages:
        return Stage.COMPLETE if state["stage_statuses"].get(Stage.COMPLETE.value) == StageStatus.COMPLETE.value \
            else REQUIRED_STAGES[-1]
    return min(open_stages, key=lambda s: (_rank(stage_workflow_status(state, s)), STAGE_ORDER.index(s)))


def derive_pending_actions(state: OnboardingCaseState) -> list[dict[str, Any]]:
    """What has to happen next and who acts, in workflow order. Ids, codes and document types only."""
    actions: list[PendingAction] = []
    collection = state.get("document_collection") or {}
    for stage in STAGE_ORDER:
        status = stage_workflow_status(state, stage)
        error = (state.get("stage_errors") or {}).get(stage.value) or {}
        if status == WorkflowStatus.COMPLETED or stage in OPTIONAL_STAGES:
            continue  # an optional stage's approvals are still listed below
        if stage == Stage.DOCUMENT_SUBMISSION and status == WorkflowStatus.WAITING_FOR_INPUT:
            actions.append(PendingAction(action="supply_documents", stage=stage.value, owner="hr_admin",
                                         documents=[o["document_type"] for o in collection.get("outstanding", [])],
                                         reason_code=error.get("code")))
        elif stage == Stage.VALIDATE_DOCUMENTS and status in (WorkflowStatus.WAITING_FOR_INPUT,
                                                              WorkflowStatus.WAITING_FOR_APPROVAL,
                                                              WorkflowStatus.BLOCKED):
            by_action: dict[str, list[str]] = {}
            for req in state.get("correction_requirements") or []:
                by_action.setdefault(req["action"], []).append(req["document_type"])
            for kind, action in (("supply", "supply_documents"), ("resubmit", "correct_documents"),
                                 ("hr_review", "review_documents"), ("resolve", "resolve_blocker")):
                if by_action.get(kind):
                    actions.append(PendingAction(action=action, stage=stage.value, owner="hr_admin",
                                                 documents=by_action[kind], reason_code=error.get("code")))
        elif stage == Stage.JOINING_DAY_WELCOME and \
                state["stage_statuses"].get(stage.value) == StageStatus.WAITING.value:
            actions.append(PendingAction(action="await_event", stage=stage.value, owner="onboarding_scheduler",
                                         reason_code="JOINING_DAY", due=state.get("joining_date")))
        elif status == WorkflowStatus.RETRY_PENDING:
            actions.append(PendingAction(action="retry_stage", stage=stage.value, owner="ops",
                                         reason_code=error.get("code"), detail=error.get("detail")))
        elif status == WorkflowStatus.FAILED:
            actions.append(PendingAction(action="escalate", stage=stage.value, owner="ops",
                                         reason_code=error.get("code"), detail=error.get("detail")))
        elif status == WorkflowStatus.BLOCKED:
            blockers = [b for b in state.get("blockers") or [] if b["stage"] == stage.value]
            for b in blockers or [{"code": error.get("code"), "owner": "hr_admin", "next_action": None}]:
                actions.append(PendingAction(action="resolve_blocker", stage=stage.value, owner=b.get("owner"),
                                             reason_code=b.get("code"), detail=b.get("next_action")))
    for approval in state.get("pending_approvals") or []:
        actions.append(PendingAction(action="decide_approval", stage=approval.get("stage") or "unknown",
                                     owner=approval.get("role"), reference=approval.get("reference"),
                                     agent=approval.get("agent"), reason_code=approval.get("gate")))
    order = {s.value: i for i, s in enumerate(STAGE_ORDER)}
    return [a.model_dump(mode="json") for a in sorted(actions, key=lambda a: order.get(a.stage, len(order)))]


def refresh_workflow_view(state: OnboardingCaseState) -> OnboardingCaseState:
    """Recompute every derived field from the stage statuses (in place; returns `state`)."""
    statuses = state["stage_statuses"]
    branch_statuses = {}
    for branch, stages in BRANCH_STAGES.items():
        open_stage = next((s for s in stages if s not in OPTIONAL_STAGES
                           and stage_workflow_status(state, s) != WorkflowStatus.COMPLETED), None)
        outcome = (state.get("stage_outcomes") or {}).get(open_stage.value) if open_stage else None
        branch_statuses[branch.value] = BranchStatusRecord(
            status=derive_branch_status(state, branch),
            stages={s.value: statuses.get(s.value, StageStatus.PENDING.value) for s in stages},
            next_action=(outcome or {}).get("next_action") if open_stage else None,
            error=(state.get("stage_errors") or {}).get(open_stage.value) if open_stage else None,
        ).model_dump(mode="json")
    state["branch_statuses"] = branch_statuses
    state["completed_stages"] = [s.value for s in STAGE_ORDER if statuses.get(s.value) == StageStatus.COMPLETE.value]
    state["workflow_status"] = derive_workflow_status(state).value
    primary = primary_stage(state)
    state["current_stage"] = primary.value
    if primary == Stage.COMPLETE:
        state["next_action"] = state["error_code"] = state["error_detail"] = None
    else:
        error = (state.get("stage_errors") or {}).get(primary.value)
        outcome = (state.get("stage_outcomes") or {}).get(primary.value) or {}
        state["error_code"] = (error or {}).get("code")
        state["error_detail"] = (error or {}).get("detail")
        state["next_action"] = outcome.get("next_action") or state.get("next_action")
    state["overall_status"] = derive_overall_status(state)
    state["pending_actions"] = derive_pending_actions(state)
    state["joining_date_consistency"] = joining_date.consistency(state)
    return state
