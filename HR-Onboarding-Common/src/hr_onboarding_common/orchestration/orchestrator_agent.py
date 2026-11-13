"""The central HR onboarding orchestrator: a `BaseAgent` subclass whose graph drives UC01-UC05
through their real public interfaces (see orchestration/adapters/), as a state-driven workflow
(docs/STATE_DRIVEN_ONBOARDING_WORKFLOW.md):

    START -> intake (UC01) -> intake_mismatch_proposal -[route_after_intake]-> plan_tasks | await_intake_review
    plan_tasks (UC02 initial planning) -[route_after_plan_tasks]-> document_collection | await_plan_tasks
    document_collection (UC03, for the plan version) -[route_after_document_collection]-> document_submission
                                                                                        | await_document_collection
    document_submission (UC03, the wait) -[route_after_document_submission]-> validate_documents
                                                                             | evaluate_document_result (HR changed
                                                                               the requirements: replanning)
                                                                             | await_documents
    validate_documents (UC03) -> evaluate_document_result -[route_after_document_result]->
            record_verification       (verified against the current plan version)
          | replan_tasks              (UC03 reported the plan must change; bounded by max_replan_rounds)
          | document_collection       (a stale result: revalidate under the current plan version)
          | await_document_correction (supply/correct)
          | await_document_review     (HR review decision)
          | await_validation_retry    (technical failure)
          | await_replanning          (replanning failed, or the loop's bound was reached)
    replan_tasks (UC02 replanning) -[route_after_replanning]-> apply_revised_plan | await_replanning
    apply_revised_plan (re-opens UC03's stages for the new version) -> document_collection
    record_verification -> dispatch_branches
    dispatch_branches -> equipment_access (UC04)                             -- in parallel, once the
                      -> orientation_scheduling -> buddy_assignment -> team_introduction (UC05)  -- documents are verified
                                    each -[route]-> next | uc05_branch_end
    [equipment_access, uc05_branch_end] -> merge_outcomes -> evaluate_overall_status
    evaluate_overall_status -[route_after_evaluation]-> day_one_readiness | await_branches
                                                      | dispatch_branches (a dependency-blocked branch is now
                                                        runnable; at most MAX_DISPATCH_ROUNDS per invocation)
    day_one_readiness -> day_one_signoff_proposal -[route_after_readiness]-> joining_day_welcome
                                                                           | await_day_one_blockers
    joining_day_welcome -[route_after_welcome]-> complete | await_joining_day

UC01 -> UC02 -> UC03 is sequential: UC02 plans first (the task plan and the plan's versioned document
requirements), UC03 collects, stores and validates exactly what the current plan version requires, and
reports back (`document_validation_feedback`). When UC03 finds the plan must change, UC02 replans (a new
plan version) and UC03 applies it and revalidates -- the UC03 -> UC02 -> UC03 loop, bounded per
invocation. Only verified documents under the current plan version reach `dispatch_branches`;
equipment_access and orientation_scheduling then start together; neither waits for the other (no UC04
or UC05 contract consumes the other's result). Inside the UC05 branch the
order is UC05's own (an approved induction proposes the buddy; an approved buddy drafts the team
introduction). A branch never writes the shared fields: it returns its outcome under its own key of
the `branch_outcomes` channel (reducer state.merge_branch_outcomes) and writes it through to the
case store under a lock; merge_outcomes folds every branch in (workflow.fold_branch_outcome).

The joining-day welcome follows HR's day-1 signoff and the joining-day event (an explicit,
documented prerequisite, checked again by the welcome adapter); until then it is WAITING.

The joining date is revisioned (joining_date.py). Before every run, results produced for an earlier
joining date are re-opened so the graph produces them again for the current date, and a day-1
signoff requested for an earlier revision is withdrawn (`_reconcile_joining_date`). Day-1 readiness
is refused while any date-dependent result is stale or UC05 holds another start date.

Every `await_*` node ends the turn (checkpointed). Resuming re-invokes the graph with the case's
last-known state from the CaseStateStore: completed stages are skipped (their UC is not called), and
so are failed ones until an authorized retry_stage re-opens them; the same routing functions
re-evaluate the (possibly now-changed) real condition, and the case either proceeds or parks again --
no polling loop inside the graph. ROUTING_TABLE below states, per stage status, what happens next.

Every stage runs through the standard UC contract (execution.py): its UCAgentRequest is validated,
its dependencies checked (dependencies.py -- an unmet required one means the UC is not called), the
adapter's update synchronized under field ownership (state_sync.py), its status transition checked
(agent_contract.py), and its normalized UCAgentResponse recorded as the stage's StageOutcome.
"""

from __future__ import annotations

import copy
import re
import threading
import uuid
from datetime import date
from typing import Any, ClassVar

from langgraph.graph import END, StateGraph
from langgraph.types import RunnableConfig

from hr_onboarding_common.agent import AgentContext, BaseAgent, run_context
from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.observability.logging import get_logger
from hr_onboarding_common.observability.tracing import span
from hr_onboarding_common.orchestration.case_store import CaseOwnershipError, StateVersionConflict
from hr_onboarding_common.orchestration import dependencies, execution, joining_date
from hr_onboarding_common.orchestration.agent_contract import (DEPENDENCY_WAIT_CODES, USE_CASE_OF,
                                                               StatusTransitionError, completed_stage_regressions)
from hr_onboarding_common.orchestration.adapters._shared import (OrchestratorAuthorizationError, add_blocker,
                                                                 add_pending_approval, clear_stage_items,
                                                                 record_stage_error, with_stage_status)
from hr_onboarding_common.orchestration.adapters.uc01_intake import call_uc01_intake
from hr_onboarding_common.orchestration.adapters.uc02_replanning import call_uc02_replan_tasks
from hr_onboarding_common.orchestration.adapters.uc02_tasks import (
    UC02_AGENT, call_uc02_plan_tasks, relay_uc02_decision, request_uc02_date_change,
)
from hr_onboarding_common.orchestration.adapters.uc03_document_collection import call_uc03_document_collection
from hr_onboarding_common.orchestration.adapters.uc03_documents import (build_feedback, call_uc03_validate_documents,
                                                                        collection_service, mark_replanning_required,
                                                                        pending_requirement_changes)
from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import (UC04_AGENT, UC04_ITEM_APPROVAL,
                                                                               call_uc04_equipment_access,
                                                                               uc04_approval_record)
from hr_onboarding_common.orchestration.adapters.uc05_orientation import (
    UC05_AGENT, PrerequisiteNotMetError, call_uc05_buddy_assignment, call_uc05_joining_day_welcome,
    call_uc05_orientation, call_uc05_team_introduction, refresh_uc05_optional_stages,
    read_welcome_message, refresh_uc05_details, relay_uc05_decision, relay_uc05_participant_response, relay_uc05_retry,
    request_uc05_operation,
)
from hr_onboarding_common.orchestration.contracts import OnboardingCaseInput
from hr_onboarding_common.orchestration.tenant_config import IdentityScheme
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import (BRANCH_OUTCOMES_RESET, BRANCH_STAGES, DOCUMENT_STAGES,
                                                      READINESS_STAGES, REPLANNING_WAIT_CODE, TECHNICAL_RETRY_CODES,
                                                      UC05_STAGES,
                                                      Branch, OnboardingCaseState, OverallStatus, Stage, StageStatus,
                                                      ensure_lifecycle_fields, new_state)
from hr_onboarding_common.orchestration.handoff_contracts import build_handoff
from hr_onboarding_common.orchestration.workflow import (STAGE_OPERATIONS, branch_delta, fold_branch_outcome,
                                                         refresh_workflow_view, stage_result_summary)
from hr_onboarding_common.orchestration.workflow_models import (DocumentReview, DocumentReviewRequest,
                                                                DocumentSubmission, OutstandingDocument,
                                                                RequirementChangeRequest, RetryMetadata, StageOutcome,
                                                                SubmittedDocument, validate_workflow_state)
from hr_onboarding_common.platform.hitl import Decision, HITLError

AGENT_ID = "onboarding-orchestrator"
DEFAULT_MAX_ATTEMPTS = 3
# How often one invocation may dispatch the parallel branches: the first fan-out plus re-dispatches of
# branches whose dependency-blocked stage became runnable during the same invocation (a tenant that
# requires UC04 readiness before UC05 scheduling). Bounds the only cycle in the graph.
MAX_DISPATCH_ROUNDS = 3
# Supersteps of one invocation: a full pass is ~20, each re-dispatch adds at most 8.
RECURSION_LIMIT = 64
# Stage statuses a node does not run again on its own: COMPLETE (never re-executed), FAILED (terminal
# until an explicit retry_stage re-opens it -- a plain resume must not spend the retry budget again).
_NOT_RERUN = (StageStatus.COMPLETE.value, StageStatus.FAILED.value)
# Intake completed in UC01, but the case-state store refused the case (no onboarding_cases row in its tenant).
CASE_NOT_STORED_CODE = "CASE_NOT_IN_DATABASE"
# Asynchronous events (handle_event): the types accepted, the event-id format, how many ids a case keeps.
EVENT_TYPES = ("documents_submitted", "joining_day", "participant_response", "resume")
EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_PROCESSED_EVENTS = 500
log = get_logger("orchestration.workflow")

# What each stage status means for routing. Pure, status-enum driven; never free text.
# (docs/STATE_DRIVEN_ONBOARDING_WORKFLOW.md reproduces this table.)
ROUTING_TABLE: dict[str, str] = {
    StageStatus.COMPLETE.value: "proceed to the next stage; on a later resume the stage is skipped, its side "
                                "effects are never re-executed",
    StageStatus.AWAITING_APPROVAL.value: "park at await_*; the owning UC's approval (or an HR document review) is "
                                         "surfaced in pending_approvals/pending_actions and decided by an "
                                         "authorized person",
    StageStatus.BLOCKED.value: "park at await_*; resumable once the named cause (stage_errors/blockers) is "
                               "corrected -- missing/invalid input, rejected approval, scheduling conflict, "
                               "retryable provider failure; PREREQUISITE_NOT_MET waits for an upstream stage "
                               "(dependencies.py) and is re-dispatched in the same run once it is met",
    StageStatus.WAITING.value: "park at await_joining_day; the joining-day event resumes it (not an error)",
    StageStatus.WAITING_FOR_INPUT.value: "park at await_documents/await_document_correction; supplying or "
                                         "correcting the named documents resumes it (not an error)",
    StageStatus.RETRY_PENDING.value: "park; a technical failure (never a document verdict); a resume or an "
                                     "authorized retry_stage runs it again, up to stage_max_attempts failures",
    StageStatus.UNCERTAIN.value: "park; no write is repeated until a fresh read of the UC is conclusive "
                                 "(operator reconciles); overall status at_risk",
    StageStatus.FAILED.value: "park; terminal (inactive case, contract violation, retry limit): a resume does not "
                              "run it again, only an authorized retry_stage re-opens it; overall failed",
    StageStatus.PENDING.value: "the stage has not run yet",
    StageStatus.IN_PROGRESS.value: "the stage is running, or was reset by an HR correction and re-derives on resume",
    StageStatus.NOT_REQUESTED.value: "optional stage nobody requested; never blocks",
}


class WorkflowRequestError(ValueError):
    """A case operation whose content is invalid for this case (unknown document type, unknown
    stage). Nothing is changed."""


class PrematureLifecycleEventError(PrerequisiteNotMetError):
    """A lifecycle event arrived before the case reached the point it applies to."""


# ---------------------------------------------------------------------------------- routing


def _status(state: OnboardingCaseState, stage: Stage) -> str:
    return state["stage_statuses"].get(stage.value, StageStatus.PENDING.value)


def _route(stage: Stage, proceed: str, park: str):
    def route(state: OnboardingCaseState) -> str:
        return proceed if _status(state, stage) == StageStatus.COMPLETE.value else park

    route.__name__ = f"route_after_{stage.value}"
    route.targets = {proceed: proceed, park: park}  # the conditional edge's path map
    return route


def _branch_route(branch: Branch, stage: Stage, proceed: str, park: str):
    """A route inside a parallel branch reads the branch's own view: its outcome of this run is in
    `branch_outcomes` until merge_outcomes folds it in."""

    def route(state: OnboardingCaseState) -> str:
        view = fold_branch_outcome(state, (state.get("branch_outcomes") or {}).get(branch.value))
        return proceed if _status(view, stage) == StageStatus.COMPLETE.value else park

    route.__name__ = f"route_after_{stage.value}"
    route.targets = {proceed: proceed, park: park}
    return route


route_after_intake = _route(Stage.INTAKE, "plan_tasks", "await_intake_review")
# UC01 -> UC02 -> UC03 is sequential: the plan (with its document requirements) exists before UC03 collects.
route_after_plan_tasks = _route(Stage.PLAN_TASKS, "document_collection", "await_plan_tasks")
route_after_document_collection = _route(Stage.DOCUMENT_COLLECTION, "document_submission",
                                         "await_document_collection")
route_after_replanning = _route(Stage.REPLAN_TASKS, "apply_revised_plan", "await_replanning")


def _code(state: OnboardingCaseState, stage: Stage) -> str | None:
    return ((state.get("stage_errors") or {}).get(stage.value) or {}).get("code")


def _waits_for_replan(state: OnboardingCaseState, stage: Stage) -> bool:
    return _status(state, stage) == StageStatus.BLOCKED.value and _code(state, stage) == REPLANNING_WAIT_CODE


def route_after_document_submission(state: OnboardingCaseState) -> str:
    """Every required document on file -> UC03 validates; an HR change to the requirements (UC03 reported
    that the plan must change) -> evaluate_document_result, which routes to UC02's replan; else wait."""
    if _status(state, Stage.DOCUMENT_SUBMISSION) == StageStatus.COMPLETE.value:
        return "validate_documents"
    return "evaluate_document_result" if _waits_for_replan(state, Stage.DOCUMENT_SUBMISSION) else "await_documents"


route_after_document_submission.targets = {t: t for t in ("validate_documents", "evaluate_document_result",
                                                          "await_documents")}
route_after_scheduling = _branch_route(Branch.ORIENTATION, Stage.ORIENTATION_SCHEDULING, "buddy_assignment",
                                       "uc05_branch_end")
route_after_buddy = _branch_route(Branch.ORIENTATION, Stage.BUDDY_ASSIGNMENT, "team_introduction", "uc05_branch_end")
route_after_readiness = _route(Stage.DAY_ONE_READINESS, "joining_day_welcome", "await_day_one_blockers")
route_after_welcome = _route(Stage.JOINING_DAY_WELCOME, "complete", "await_joining_day")

_VALIDATION_ROUTES = {
    StageStatus.COMPLETE.value: "record_verification",
    StageStatus.WAITING_FOR_INPUT.value: "await_document_correction",
    StageStatus.BLOCKED.value: "await_document_correction",
    StageStatus.AWAITING_APPROVAL.value: "await_document_review",
}


def route_after_validation(state: OnboardingCaseState) -> str:
    """UC03's structured result routes the case (the stage status the UC03 adapter derived from UC03's
    enums -- never free text): verified -> the parallel fan-out; documents to supply or correct, or a
    UC03 finding to resolve -> wait for them; an HR review decision -> wait for the reviewer; anything
    else (a technical failure: RETRY_PENDING / FAILED) -> wait for a retry. A technical failure is never
    routed as invalid documentation. (Replanning and stale results are decided first, by
    evaluate_document_result.)"""
    return _VALIDATION_ROUTES.get(_status(state, Stage.VALIDATE_DOCUMENTS), "await_validation_retry")


route_after_validation.targets = {t: t for t in (*_VALIDATION_ROUTES.values(), "await_validation_retry")}


def route_after_document_result(state: OnboardingCaseState) -> str:
    """The route evaluate_document_result chose (it records it, with the bookkeeping that goes with it)."""
    return state.get("document_route") or "await_validation_retry"


route_after_document_result.targets = {t: t for t in (*route_after_validation.targets, "replan_tasks",
                                                      "document_collection", "await_replanning")}


def route_after_evaluation(state: OnboardingCaseState) -> str:
    """Day-1 readiness is evaluated only once every branch's required stage is complete. Otherwise,
    when a branch stage that waited for another branch can now run (`redispatch_branches`, set by
    evaluate_overall_status within MAX_DISPATCH_ROUNDS), those branches are dispatched again; else the
    case parks with the merged status of all branches."""
    complete = all(_status(state, s) == StageStatus.COMPLETE.value for s in READINESS_STAGES)
    if complete:
        return "day_one_readiness"
    return "dispatch_branches" if state.get("redispatch_branches") else "await_branches"


route_after_evaluation.targets = {"day_one_readiness": "day_one_readiness", "await_branches": "await_branches",
                                  "dispatch_branches": "dispatch_branches"}

# (node, stage it parks on -- None: whichever branch stage needs attention, next_action prefix)
AWAIT_NODES = (
    ("await_intake_review", Stage.INTAKE, "hr_review"),
    ("await_plan_tasks", Stage.PLAN_TASKS, "retry"),
    ("await_document_collection", Stage.DOCUMENT_COLLECTION, "retry"),
    ("await_documents", Stage.DOCUMENT_SUBMISSION, "supply_documents"),
    ("await_document_correction", Stage.VALIDATE_DOCUMENTS, "correction_required"),
    ("await_document_review", Stage.VALIDATE_DOCUMENTS, "hr_review"),
    ("await_validation_retry", Stage.VALIDATE_DOCUMENTS, "retry"),
    ("await_replanning", Stage.REPLAN_TASKS, "hr_review"),
    ("await_branches", None, "await"),
    ("await_day_one_blockers", Stage.DAY_ONE_READINESS, "hr_review"),
    ("await_joining_day", Stage.JOINING_DAY_WELCOME, "await"),
)

# ----------------------------------------------------------------------------------- plain nodes


def _await_node(stage: Stage | None, reason_prefix: str):
    def _node(state: OnboardingCaseState) -> OnboardingCaseState:
        new_state = refresh_workflow_view(dict(state))
        if stage is not None and not new_state.get("next_action"):
            new_state["next_action"] = f"{reason_prefix}:{stage.value}"
        return new_state

    return _node


def node_complete(state: OnboardingCaseState) -> OnboardingCaseState:
    new_state = dict(state)
    new_state["stage_statuses"] = {**state["stage_statuses"], Stage.COMPLETE.value: StageStatus.COMPLETE.value}
    refresh_workflow_view(new_state)
    new_state["overall_status"] = OverallStatus.COMPLETE.value
    new_state["current_stage"] = Stage.COMPLETE.value
    new_state["next_action"] = None
    return new_state


def node_uc05_branch_end(state: OnboardingCaseState) -> dict[str, Any]:
    """The UC05 branch's join point: reached whether the branch completed or parked part-way, so the
    fan-in always receives both branches."""
    return {}


def node_document_submission(state: OnboardingCaseState) -> OnboardingCaseState:
    """UC03's wait for documents. An HR change to the document requirements comes first: UC03 reports it
    to UC02 (feedback, BLOCKED with REPLANNING_REQUIRED) before anything else is asked for. Otherwise
    COMPLETE once every required document of the plan version has a current revision that does not
    still need correcting; WAITING_FOR_INPUT (DOCUMENTS_OUTSTANDING) otherwise, naming each outstanding
    document and why. It reads UC03's stored revisions (their projection) and what UC03 said about each."""
    if pending_requirement_changes(state):
        feedback = build_feedback(state, state.get("document_validation_result") or {})
        if feedback["replanning_required"]:
            new_state = copy.deepcopy(state)
            new_state["document_validation_feedback"] = feedback
            return mark_replanning_required(new_state, Stage.DOCUMENT_SUBMISSION, feedback)
    collection = dict(state.get("document_collection") or {})
    submitted = state.get("submitted_documents") or {}
    results = state.get("document_validation_result") or {}
    outstanding = []
    for doc_type in [d["document_type"] for d in collection.get("required_documents", []) if d.get("required")]:
        doc, result = submitted.get(doc_type), results.get(doc_type) or {}
        current = doc is not None and result.get("revision") == doc["revision"]
        if doc is None:
            reason = "not_submitted"
        elif current and result.get("classification") in ("missing", "correction_required"):
            reason = "correction_required"
        elif current and result.get("classification") == "hr_review_rejected":
            reason = "hr_review_rejected"
        else:
            continue
        outstanding.append(OutstandingDocument(document_type=doc_type, reason=reason).model_dump())
    new_state = copy.deepcopy(state)
    new_state["document_collection"] = {**collection, "outstanding": outstanding}
    if not outstanding:
        return with_stage_status(new_state, Stage.DOCUMENT_SUBMISSION, StageStatus.COMPLETE)
    types = [o["document_type"] for o in outstanding]
    new_state = with_stage_status(new_state, Stage.DOCUMENT_SUBMISSION, StageStatus.WAITING_FOR_INPUT)
    record_stage_error(new_state, Stage.DOCUMENT_SUBMISSION, code="DOCUMENTS_OUTSTANDING", retryable=True,
                       detail=f"{len(types)} required document(s) outstanding: {', '.join(types)}")
    new_state["next_action"] = f"supply_documents:{','.join(types)}"
    return new_state


def node_day_one_readiness(state: OnboardingCaseState) -> OnboardingCaseState:
    """Aggregates the stages before it. Ready is a PROPOSAL requiring HR signoff (the
    orchestrator's own `day_one_signoff` gate) -- never auto-completed just because every agent
    call returned successfully. The optional buddy introduction is not required. Every
    date-dependent result must have been produced for the current joining date, and UC05 must hold
    that date as the case's start date (joining_date.readiness_blocker)."""
    new_state = copy.deepcopy(state)
    outstanding = [s.value for s in READINESS_STAGES if _status(state, s) != StageStatus.COMPLETE.value]
    if outstanding:
        new_state["outstanding_tasks"] = sorted(set(new_state.get("outstanding_tasks", [])) | set(outstanding))
        new_state["stage_statuses"][Stage.DAY_ONE_READINESS.value] = StageStatus.BLOCKED.value
        new_state["overall_status"] = OverallStatus.AT_RISK.value if new_state.get("blockers") else OverallStatus.IN_PROGRESS.value
        return new_state

    unreconciled = joining_date.readiness_blocker(state)
    if unreconciled:
        new_state["stage_statuses"][Stage.DAY_ONE_READINESS.value] = StageStatus.BLOCKED.value
        record_stage_error(new_state, Stage.DAY_ONE_READINESS, code="JOINING_DATE_UNRECONCILED", detail=unreconciled,
                           retryable=True)
        add_blocker(new_state, stage=Stage.DAY_ONE_READINESS, code="JOINING_DATE_UNRECONCILED", reason=unreconciled,
                    owner="hr_admin", next_action="Bring every date-dependent result to the current joining date, "
                                                  "then resume")
        new_state["next_action"] = "hr_review:day_one_readiness:joining_date_unreconciled"
        return new_state

    if new_state.get("day_one_signoff_approved"):
        new_state["stage_statuses"][Stage.DAY_ONE_READINESS.value] = StageStatus.COMPLETE.value
        return new_state

    rejected = (new_state.get("gate_rejections") or {}).get("day_one_signoff")
    if rejected:  # held until HR explicitly resumes the case (resume_case clears it)
        new_state["stage_statuses"][Stage.DAY_ONE_READINESS.value] = StageStatus.BLOCKED.value
        record_stage_error(new_state, Stage.DAY_ONE_READINESS, code="DAY_ONE_SIGNOFF_REJECTED",
                           detail=f"HR rejected day-1 signoff ({rejected})", retryable=True)
        new_state["next_action"] = "hr_review:day_one_readiness:day_one_signoff_rejected"
        return new_state

    new_state["stage_statuses"][Stage.DAY_ONE_READINESS.value] = StageStatus.AWAITING_APPROVAL.value
    new_state["next_action"] = "hr_review:day_one_readiness:signoff_required"
    return new_state


def _gate_pending(config: RunnableConfig, gate: str, state: OnboardingCaseState) -> bool:
    """True when this case already has an undecided request at the orchestrator's own `gate` -- a
    repeated resume never raises a second one."""
    rc = run_context(config)
    return bool(rc.ctx.hitl.list(status=Decision.PENDING, gate=gate, workflow_instance=state["case_id"],
                                 client_id=state["client_id"]))


def node_day_one_signoff_proposal(state: OnboardingCaseState, config: RunnableConfig) -> OnboardingCaseState:
    """Runs right after readiness is found AWAITING_APPROVAL -- raises the orchestrator's own
    `day_one_signoff` gate exactly once via RunContext.propose (never self-approved). The request
    names the joining date and revision it is for; a later revision withdraws it."""
    if state["stage_statuses"].get(Stage.DAY_ONE_READINESS.value) != StageStatus.AWAITING_APPROVAL.value:
        return state
    if _gate_pending(config, "day_one_signoff", state):
        return state
    rc = run_context(config)
    rc.propose(type="day_one_signoff", gate="day_one_signoff",
               payload={"case_id": state["case_id"], "client_id": state["client_id"],
                        "joining_date": state.get("joining_date"),
                        "joining_date_revision": joining_date.revision(state)},
               rationale="Every required onboarding stage reports COMPLETE; HR signoff required before the "
                         "joining-day welcome and before the case is marked complete.",
               confidence=1.0, subject_id=state["case_id"])
    return state


def node_intake_mismatch_proposal(state: OnboardingCaseState, config: RunnableConfig) -> OnboardingCaseState:
    """Raises the orchestrator's own `intake_mismatch_review` gate when UC01 reports a
    HELD/PAUSED outcome that needs HR judgement (mismatch or ambiguous duplicate)."""
    if _status(state, Stage.INTAKE) != StageStatus.BLOCKED.value or state.get("intake_mismatch_approved"):
        return state
    if (state.get("gate_rejections") or {}).get("intake_mismatch_review"):
        return state  # HR rejected it; a fresh request only after an explicit resume
    if _gate_pending(config, "intake_mismatch_review", state):
        return state
    rc = run_context(config)
    rc.propose(type="intake_mismatch_review", gate="intake_mismatch_review",
               payload={"case_id": state.get("case_id", ""), "blockers": state.get("blockers", [])},
               rationale="UC01 intake reported an unresolved mismatch, unverified acceptance, or ambiguous duplicate.",
               confidence=1.0, subject_id=state.get("case_id"))
    return state


# How often one branch step is re-folded onto a state another process changed meanwhile.
BRANCH_WRITE_ATTEMPTS = 3

# Fields only UC01 needs; removed from case state once intake completes (minimisation).
_INTAKE_ONLY_FIELDS = ("id_number", "candidate_email", "form", "raw_acceptance_email", "placement")
def _origin(state: OnboardingCaseState) -> str:
    """The id a case was started under (UC01 may rename it at intake)."""
    return (state.get("intake_trigger") or {}).get("origin_case_id") or state["case_id"]


# The orchestrator's own gates and the stage each one holds. The internal onboarding form's approval holds
# INTAKE: UC01 opens a case only from an APPROVED form (UC01 start_gate / open_case), so UC01 cannot run first.
FORM_GATE = "onboarding_form_approval"
_GATE_STAGE = {"intake_mismatch_review": Stage.INTAKE, "day_one_signoff": Stage.DAY_ONE_READINESS,
               FORM_GATE: Stage.INTAKE}
# What the approver reviews (no email, no id number: those stay in process memory, intake_inputs).
_FORM_REVIEW_FIELDS = ("candidate_name", "designation", "joining_date", "work_location_type", "department", "role_id",
                       "grade", "manager_id", "location", "project", "nationality_group", "offer_reference",
                       "offer_accepted", "offer_accepted_at", "employee_id", "timezone")


def _form_digest(inp: OnboardingCaseInput) -> str:
    """Identifies the exact form content an approval is for (sensitive fields included only as a hash)."""
    import hashlib
    import json as _json

    fields = {k: getattr(inp, k) for k in (*_FORM_REVIEW_FIELDS, "candidate_email", "id_number")}
    return hashlib.sha256(_json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def route_after_form_approval(state: OnboardingCaseState) -> str:
    """UC01 runs only when no form approval is required, the required approval was granted, or intake is
    already complete (a later resume)."""
    fa = state.get("form_approval") or {}
    if not fa.get("required") or fa.get("status") == "approved" or _status(state, Stage.INTAKE) == StageStatus.COMPLETE.value:
        return "intake"
    return "await_form_approval"


route_after_form_approval.targets = {"intake": "intake", "await_form_approval": "await_form_approval"}


# --------------------------------------------------------------------------------- the agent


class OnboardingOrchestratorAgent(BaseAgent[OnboardingCaseInput]):
    agent_id: ClassVar[str] = AGENT_ID
    version: ClassVar[str] = "0.3.0"
    input_model: ClassVar[type] = OnboardingCaseInput
    preamble: ClassVar[str] = (
        "You orchestrate the real UC01-UC05 onboarding agents to Day-1 readiness. You never "
        "decide an approval yourself; you never fabricate a successful result."
    )
    _locks: ClassVar[list[threading.RLock]] = [threading.RLock() for _ in range(64)]
    # Branch write-through: parallel branches run on graph worker threads while the caller holds the
    # case lock, so their load-fold-save of the case store is serialized by a lock of its own.
    _store_locks: ClassVar[list[threading.Lock]] = [threading.Lock() for _ in range(64)]

    def __init__(self, ctx: AgentContext):
        if not isinstance(ctx.connector, OrchestratorDeps):
            raise TypeError("AgentContext.connector must be an OrchestratorDeps bundle")
        self.deps: OrchestratorDeps = ctx.connector
        super().__init__(ctx)
        self.deps.policy = self.policy
        self.deps.persist_progress = self._persist
        # Cases whose stored state another process changed during the current run (_persist_branch).
        self._foreign_writes: set[str] = set()

    # ------------------------------------------------------------------ graph

    def _adapter(self, stage: Stage, default):
        override = self.deps.stage_adapters.get(stage.value)
        return (lambda s: override(s, self.deps)) if override is not None else (lambda s: default(s, self.deps))

    def _max_attempts(self) -> int:
        return int(self.policy.settings.get("stage_max_attempts", DEFAULT_MAX_ATTEMPTS))

    def _execute_stage(self, state: OnboardingCaseState, stage: Stage, fn, trace_id: str) -> OnboardingCaseState:
        """Runs one stage and returns the resulting state (not persisted): its previous blockers/
        approvals are cleared, the attempt counted, the stage run through the standard UC contract
        (execution.invoke: request validated, dependencies checked -- an unmet required one means the
        UC is not called --, the adapter's update synchronized and its status transition checked, an
        adapter exception turned into a FAILED stage), status transitions appended to stage_history, its
        error kept in stage_errors (cleared on COMPLETE), a technical failure counted against the retry
        budget, and a StageOutcome recorded from the normalized UCAgentResponse."""
        before = _status(state, stage)
        working = ensure_lifecycle_fields(copy.deepcopy(state))
        clear_stage_items(working, stage)
        working["error_code"] = working["error_detail"] = working["next_action"] = None
        attempt = working["stage_attempts"][stage.value] = working["stage_attempts"].get(stage.value, 0) + 1
        if before in (StageStatus.PENDING.value, StageStatus.NOT_REQUESTED.value):
            working["stage_history"].append({"stage": stage.value, "status": StageStatus.IN_PROGRESS.value,
                                             "at": now_utc().isoformat()})
        invocation = execution.invoke(
            stage, working, fn, attempt=attempt, trace_id=trace_id, tenants=self.deps.tenants,
            run=(lambda s, adapter: self._run_intake(s, adapter)) if stage == Stage.INTAKE else None,
            metadata={"orchestrator": AGENT_ID, "orchestrator_version": self.version})
        started_at, inputs = invocation.started_at, (invocation.request.input_data if invocation.request else {})
        # The contract layer has already tagged this execution's approvals with the stage and recorded an
        # error an adapter reported only through error_code (execution._normalize_reported_items).
        result = ensure_lifecycle_fields(invocation.state)
        if result.get("joining_date") != state.get("joining_date"):  # only an owner may change it (state_sync)
            joining_date.record_change(result, result.get("joining_date"), stage=stage, use_case_id=USE_CASE_OF[stage],
                                       source=f"{stage.value}_result", execution_id=invocation.execution_id,
                                       at=now_utc().isoformat(), previous=state.get("joining_date"))
        errors = result["stage_errors"]
        if _status(result, stage) == StageStatus.RETRY_PENDING.value:
            self._count_technical_failure(result, stage)
        after = _status(result, stage)
        if after != before:
            event = {"stage": stage.value, "status": after, "at": now_utc().isoformat()}
            if stage.value in errors:
                event["code"] = errors[stage.value]["code"]
            result["stage_history"].append(event)
        if stage == Stage.INTAKE:
            result["intake_trigger"] = {k: v for k, v in result["intake_trigger"].items()
                                        if k not in _INTAKE_ONLY_FIELDS}
            # The intake-only inputs are dropped by _stage_node once the completed intake is STORED: a refused write
            # (no onboarding_cases row for UC01's case) keeps them, so the intake can run again for the same case.
        uc, operation, result_key = STAGE_OPERATIONS[stage]
        response = execution.respond(invocation, result)
        result["stage_outcomes"][stage.value] = StageOutcome(
            stage=stage.value, uc=uc, operation=operation, status=after, attempt=attempt, started_at=started_at,
            finished_at=response.finished_at, inputs=inputs, result_key=result_key,
            next_action=result.get("next_action"), error=errors.get(stage.value),
            canonical_status=response.status, execution_id=response.execution_id, trace_id=response.trace_id,
            correlation_id=response.correlation_id, dependencies=invocation.dependencies, warnings=response.warnings,
            updated_fields=response.updated_fields,
            result_summary=(summary := stage_result_summary(stage, result)),
            handoff=build_handoff(uc=uc, stage=stage.value, status=after, result_key=result_key, state=result,
                                  warnings=response.warnings, attempt=attempt, finished_at=response.finished_at,
                                  execution_id=response.execution_id, summary=summary)).model_dump(mode="json")
        # Structured, ids and codes only: never document metadata, names or contact details.
        log.info("orchestration.stage_finished", extra={
            "case_id": result["case_id"], "client_id": result["client_id"], "stage": stage.value, "status": after,
            "canonical_status": response.status.value, "code": (errors.get(stage.value) or {}).get("code"),
            "attempt": attempt, "correlation_id": result.get("correlation_id"), "trace_id": trace_id,
            "execution_id": response.execution_id, "uc_called": invocation.adapter_called})
        return result

    def _count_technical_failure(self, result: OnboardingCaseState, stage: Stage) -> None:
        """The retry policy: every RETRY_PENDING outcome counts; at stage_max_attempts failures the
        stage becomes FAILED (RETRY_LIMIT_REACHED) -- an explicit retry_stage resets the budget."""
        meta = RetryMetadata.model_validate(result["retry_metadata"].get(stage.value)
                                            or {"max_attempts": self._max_attempts()})
        error = result["stage_errors"].get(stage.value) or {}
        meta.failures += 1
        meta.last_code, meta.last_failure_at = error.get("code"), now_utc().isoformat()
        if meta.failures >= meta.max_attempts:
            meta.exhausted = True
            result["stage_statuses"][stage.value] = StageStatus.FAILED.value
            record_stage_error(result, stage, code="RETRY_LIMIT_REACHED", retryable=False,
                               detail=f"{meta.failures} technical failure(s), last {meta.last_code}: "
                                      f"{error.get('detail', '')}")
            result["next_action"] = f"ops_review:{stage.value}:retry_limit_reached"
        result["retry_metadata"][stage.value] = meta.model_dump(mode="json")

    @staticmethod
    def _trace_id(config: RunnableConfig | None) -> str:
        return ((config or {}).get("configurable") or {}).get("trace_id") or execution.new_trace_id()

    def _stage_node(self, stage: Stage, fn):
        """A sequential stage: skipped once COMPLETE, and while FAILED (terminal until retry_stage
        re-opens it); otherwise run and written through."""

        def _node(state: OnboardingCaseState, config: RunnableConfig) -> OnboardingCaseState:
            if _status(state, stage) in _NOT_RERUN:
                return state
            result = self._execute_stage(state, stage, fn, self._trace_id(config))
            self._persist(result)
            if stage == Stage.INTAKE and _status(result, stage) == StageStatus.COMPLETE.value:
                self.deps.intake_inputs.pop(_origin(result), None)  # only once the completed intake is stored
            return result

        return _node

    def _branch_node(self, branch: Branch, stage: Stage, fn):
        """A stage inside a parallel branch. It works on the branch's own view (the shared state plus
        the branch's outcome so far this run), returns only its branch's cumulative outcome -- never a
        shared field another branch could also be writing -- and writes the step through to the case
        store (so a crash cannot lose a branch that finished). A branch this dispatch does not run
        (`active_branches`, a re-dispatch of other branches) and a COMPLETE or FAILED stage are skipped."""

        def _node(state: OnboardingCaseState, config: RunnableConfig) -> dict[str, Any]:
            active = state.get("active_branches")
            if active and branch.value not in active:
                return {}
            view = fold_branch_outcome(state, (state.get("branch_outcomes") or {}).get(branch.value))
            if _status(view, stage) in _NOT_RERUN:
                return {}
            result = self._execute_stage(view, stage, fn, self._trace_id(config))
            self._persist_branch(result["case_id"], branch_delta(result, branch,
                                                                 history_from=len(view["stage_history"])))
            return {"branch_outcomes": {branch.value: branch_delta(result, branch,
                                                                   history_from=len(state["stage_history"]))}}

        return _node

    def _record_verified_context(self, state: OnboardingCaseState) -> OnboardingCaseState:
        """Runs once, right after UC03 verified every document of the current plan version: records
        what both parallel branches are handed (their `_DOCUMENTS_VERIFIED` dependency reads it), with
        the plan id and version the documents were verified against. Idempotent: a later pass (a
        resume) never recomputes it once set."""
        new_state = copy.deepcopy(state)
        validation = state.get("document_validation_result") or {}
        outcome = (state.get("stage_outcomes") or {}).get(Stage.VALIDATE_DOCUMENTS.value) or {}
        plan = state.get("onboarding_plan") or {}
        new_state["verified_context"] = new_state.get("verified_context") or {
            "documents_verified": True, "verified_document_types": sorted(validation),
            "verified_at": outcome.get("finished_at") or now_utc().isoformat(),
            "case_id": state["case_id"], "employee_id": state.get("employee_id"),
            "joining_date": state.get("joining_date"), "plan_id": plan.get("plan_id"),
            "plan_version": plan.get("plan_version"),
        }
        self._persist(new_state)
        return new_state

    def _evaluate_document_result(self, state: OnboardingCaseState) -> OnboardingCaseState:
        """Decides where UC03's result takes the case (route_after_document_result reads the decision):

          replan_tasks          UC03 (validation, or the submission wait for an HR change) reported that
                                the plan must change, for the current plan version
          document_collection   a stale result -- validated against another plan version than the
                                current one (UC02 versioned the plan since): UC03's stages re-open and
                                revalidate under the current version; never accepted as verified
          record_verification   every document verified against the current plan version
          await_*               corrections, an HR review, a technical failure (route_after_validation)
          await_replanning      replanning failed terminally, or this invocation already ran the
                                UC03 -> UC02 loop max_replan_rounds times (REPLAN_LIMIT_REACHED)

        Re-opening a stage it routes back to happens here, so the next node runs it again."""
        new_state = copy.deepcopy(state)
        plan_version = (state.get("onboarding_plan") or {}).get("plan_version")
        feedback = state.get("document_validation_feedback") or {}
        collection_version = (state.get("document_collection") or {}).get("plan_version")
        rounds = int(state.get("replan_round") or 0)
        replanning = (feedback.get("replanning_required") and
                      any(_waits_for_replan(state, s) for s in (Stage.DOCUMENT_SUBMISSION, Stage.VALIDATE_DOCUMENTS)))
        stale = (feedback.get("plan_version") not in (None, plan_version)
                 or collection_version not in (None, plan_version))
        if replanning and not stale:
            route = "replan_tasks"
            if _status(state, Stage.REPLAN_TASKS) == StageStatus.FAILED.value:
                route = "await_replanning"
        elif stale and (replanning or _status(state, Stage.VALIDATE_DOCUMENTS) == StageStatus.COMPLETE.value):
            route = "document_collection"
        elif _status(state, Stage.VALIDATE_DOCUMENTS) == StageStatus.COMPLETE.value:
            route = "record_verification"
        else:
            route = route_after_validation(state)
        if route in ("replan_tasks", "document_collection"):
            if rounds >= self.deps.max_replan_rounds:
                route = "await_replanning"
                new_state = with_stage_status(new_state, Stage.REPLAN_TASKS, StageStatus.BLOCKED)
                record_stage_error(new_state, Stage.REPLAN_TASKS, code="REPLAN_LIMIT_REACHED", retryable=True,
                                   detail=f"the UC03 -> UC02 replanning loop ran {rounds} time(s) in one run "
                                          f"without reaching a stable plan; a person reviews the plan")
                add_blocker(new_state, stage=Stage.REPLAN_TASKS, code="REPLAN_LIMIT_REACHED", owner="hr_admin",
                            reason="replanning did not converge", next_action="Review the plan and the document "
                                                                              "requirements, then resume")
                new_state["next_action"] = "hr_review:replan_tasks:replan_limit_reached"
            else:
                new_state["replan_round"] = rounds + 1
                if route == "replan_tasks" and _status(state, Stage.REPLAN_TASKS) in (
                        StageStatus.NOT_REQUESTED.value, StageStatus.COMPLETE.value):
                    new_state["stage_statuses"][Stage.REPLAN_TASKS.value] = StageStatus.IN_PROGRESS.value
                    new_state["stage_history"].append({"stage": Stage.REPLAN_TASKS.value,
                                                       "status": StageStatus.IN_PROGRESS.value,
                                                       "at": now_utc().isoformat(), "code": REPLANNING_WAIT_CODE})
                if route == "document_collection":
                    self._reopen(new_state, DOCUMENT_STAGES, code="STALE_PLAN_VERSION")
        new_state["document_route"] = route
        self._persist(new_state)
        return new_state

    def _apply_revised_plan(self, state: OnboardingCaseState) -> OnboardingCaseState:
        """UC02 produced the next plan version: UC03's stages re-open so UC03 opens the collection for
        it, asks for what is new, and revalidates against it (unchanged revisions keep their verdicts)."""
        new_state = copy.deepcopy(state)
        self._reopen(new_state, DOCUMENT_STAGES, code="PLAN_REVISED")
        self._persist(new_state)
        return new_state

    def _dispatch_branches(self, state: OnboardingCaseState) -> OnboardingCaseState:
        """The fan-out point after plan_tasks has completed: records which branches this dispatch
        runs and clears the branch channel. The two branch entry nodes are its successors. The first
        dispatch of an invocation runs every branch; a re-dispatch (route_after_evaluation) only the
        branches in `redispatch_branches`."""
        new_state = copy.deepcopy(state)
        new_state["dispatch_round"] = int(state.get("dispatch_round") or 0) + 1
        new_state["active_branches"] = (list(state.get("redispatch_branches") or []) if new_state["dispatch_round"] > 1
                                        else [b.value for b in Branch])
        new_state["redispatch_branches"] = []
        new_state["branch_outcomes"] = {}
        self._persist(new_state)  # the base every branch's write-through folds onto
        return {**new_state, "branch_outcomes": {BRANCH_OUTCOMES_RESET: True}}

    def _merge_outcomes(self, state: OnboardingCaseState) -> OnboardingCaseState:
        """The fan-in: every branch's outcome folded into the shared fields (each replaces only what it
        owns), then written through."""
        merged = copy.deepcopy(state)
        for branch in Branch:
            merged = fold_branch_outcome(merged, (state.get("branch_outcomes") or {}).get(branch.value))
        merged["branch_outcomes"] = {}
        self._persist(merged)
        return {**merged, "branch_outcomes": {BRANCH_OUTCOMES_RESET: True}}

    def _evaluate_overall_status(self, state: OnboardingCaseState) -> OnboardingCaseState:
        """Workflow and branch status from every stage (state.derive_workflow_status): one completed
        branch never hides another's failure, block or wait. Also decides whether a branch must be
        dispatched again in this invocation: one of its stages waited for another branch
        (PREREQUISITE_NOT_MET) and every required dependency of it is now met."""
        evaluated = refresh_workflow_view(copy.deepcopy(state))
        evaluated["redispatch_branches"] = self._runnable_waiting_branches(evaluated) \
            if int(evaluated.get("dispatch_round") or 0) < MAX_DISPATCH_ROUNDS else []
        self._persist(evaluated)
        return evaluated

    def _runnable_waiting_branches(self, state: OnboardingCaseState) -> list[str]:
        branches = []
        for branch, stages in BRANCH_STAGES.items():
            for stage in stages:
                code = ((state.get("stage_errors") or {}).get(stage.value) or {}).get("code")
                if (_status(state, stage) == StageStatus.BLOCKED.value and code in DEPENDENCY_WAIT_CODES
                        and not dependencies.unmet(stage, state, self.deps.tenants)):
                    branches.append(branch.value)
                    break
        return branches

    def _run_intake(self, state: OnboardingCaseState, fn) -> OnboardingCaseState:
        """UC01 is the only stage that needs the intake-only inputs (id number, email, the form).
        They are merged into a copy for this one call and never returned into graph state, so no
        LangGraph checkpoint and no case store holds them (deps.intake_inputs is process memory)."""
        inputs = self.deps.intake_inputs.get(_origin(state))
        legacy = any(k in state["intake_trigger"] for k in _INTAKE_ONLY_FIELDS)  # a state from before this change
        if inputs is None and not legacy:
            blocked = with_stage_status(state, Stage.INTAKE, StageStatus.BLOCKED)
            record_stage_error(blocked, Stage.INTAKE, code="INTAKE_INPUT_UNAVAILABLE", retryable=True,
                               detail="the intake inputs are held in memory only and are no longer available "
                                      "(the orchestrator restarted before intake completed); resubmit the start request")
            blocked["next_action"] = "resubmit:intake"
            return blocked
        merged = copy.deepcopy(state)
        merged["intake_trigger"] = {**merged["intake_trigger"], **(inputs or {})}
        fa = state.get("form_approval") or {}
        if fa.get("required"):
            decision = fa.get("decision") or {}
            if fa.get("status") != "approved" or decision.get("decision") != "approved":
                held = with_stage_status(state, Stage.INTAKE, StageStatus.AWAITING_APPROVAL)
                held["next_action"] = f"hr_approval:intake:{FORM_GATE}"
                return held  # defensive: the graph never routes here without the approval
            form = merged["intake_trigger"].get("form")
            if form is not None:
                merged["intake_trigger"]["form"] = {**form, "status": "APPROVED", "approved_by": decision["decided_by"],
                                                    "approved_at": decision["decided_at"]}
        return fn(merged)

    def _form_approval_node(self, state: OnboardingCaseState, config: RunnableConfig) -> OnboardingCaseState:
        """Graph entry. For an internal onboarding form: raise the `onboarding_form_approval` request once
        (never self-approved) and hold UC01 until an authorized approver decides it."""
        fa = state.get("form_approval") or {}
        if (not fa.get("required") or fa.get("status") == "approved"
                or _status(state, Stage.INTAKE) == StageStatus.COMPLETE.value):
            return state
        if (state.get("gate_rejections") or {}).get(FORM_GATE):
            return state  # rejected: held until HR corrects the form (resubmission) or resumes the case
        new_state = with_stage_status(state, Stage.INTAKE, StageStatus.AWAITING_APPROVAL)
        if not _gate_pending(config, FORM_GATE, state):
            rc = run_context(config)
            rc.propose(type="internal_onboarding_form", gate=FORM_GATE, subject_id=state["case_id"], confidence=1.0,
                       payload={"approval_type": "INTERNAL_ONBOARDING_FORM", "case_id": state["case_id"],
                                "client_id": state["client_id"], "requested_by": fa.get("requested_by"),
                                "employee_name": (fa.get("form") or {}).get("candidate_name"),
                                "employee_id": state.get("employee_id"), "form": fa.get("form"),
                                "form_digest": fa.get("form_digest")},
                       rationale="An internal onboarding form was submitted. UC01 opens the employee record only from "
                                 "an approved form, and UC02 plans only after that approval.")
            new_state["form_approval"] = {**fa, "status": "pending", "decision": None}
        new_state["next_action"] = f"hr_approval:intake:{FORM_GATE}"
        if not any(h.get("stage") == Stage.INTAKE.value and h.get("status") == StageStatus.AWAITING_APPROVAL.value
                   for h in new_state["stage_history"][-1:]):
            new_state["stage_history"].append({"stage": Stage.INTAKE.value, "at": now_utc().isoformat(),
                                               "status": StageStatus.AWAITING_APPROVAL.value, "code": "FORM_APPROVAL_PENDING"})
        return new_state

    def _sync_approval(self, state: OnboardingCaseState, item: Any) -> None:
        """Write one of the orchestrator's own requests through to `approval_requests` and record the result."""
        mirror = self.deps.approval_mirror
        if mirror is None:
            result = {"persisted": False, "reason": "APPROVAL_REQUESTS_NOT_CONFIGURED", "outcome": str(item.status).upper()}
        else:
            result = mirror.sync(item)
        result = {**result, "table": "approval_requests", "at": now_utc().isoformat()}
        state.setdefault("approval_persistence", {})[item.id] = result
        fa = state.get("form_approval") or {}
        if item.gate == FORM_GATE and fa.get("approval_id") == item.id:
            fa["persistence"] = result

    def build_graph(self):
        graph = StateGraph(OnboardingCaseState)
        for name, stage, default in (
            ("intake", Stage.INTAKE, call_uc01_intake),
            ("plan_tasks", Stage.PLAN_TASKS, call_uc02_plan_tasks),
            ("document_collection", Stage.DOCUMENT_COLLECTION, call_uc03_document_collection),
            ("validate_documents", Stage.VALIDATE_DOCUMENTS, call_uc03_validate_documents),
            ("replan_tasks", Stage.REPLAN_TASKS, call_uc02_replan_tasks),
            ("joining_day_welcome", Stage.JOINING_DAY_WELCOME, call_uc05_joining_day_welcome),
        ):
            graph.add_node(name, self._stage_node(stage, self._adapter(stage, default)))
        for name, branch, stage, default in (
            ("equipment_access", Branch.EQUIPMENT_ACCESS, Stage.EQUIPMENT_ACCESS, call_uc04_equipment_access),
            ("orientation_scheduling", Branch.ORIENTATION, Stage.ORIENTATION_SCHEDULING, call_uc05_orientation),
            ("buddy_assignment", Branch.ORIENTATION, Stage.BUDDY_ASSIGNMENT, call_uc05_buddy_assignment),
            ("team_introduction", Branch.ORIENTATION, Stage.TEAM_INTRODUCTION, call_uc05_team_introduction),
        ):
            graph.add_node(name, self._branch_node(branch, stage, self._adapter(stage, default)))
        graph.add_node("intake_mismatch_proposal", node_intake_mismatch_proposal)
        graph.add_node("document_submission", self._stage_node(Stage.DOCUMENT_SUBMISSION, node_document_submission))
        graph.add_node("evaluate_document_result", self._evaluate_document_result)
        graph.add_node("apply_revised_plan", self._apply_revised_plan)
        graph.add_node("record_verification", self._record_verified_context)
        graph.add_node("dispatch_branches", self._dispatch_branches)
        graph.add_node("uc05_branch_end", node_uc05_branch_end)
        graph.add_node("merge_outcomes", self._merge_outcomes)
        graph.add_node("evaluate_overall_status", self._evaluate_overall_status)
        graph.add_node("day_one_readiness", self._stage_node(Stage.DAY_ONE_READINESS, node_day_one_readiness))
        graph.add_node("day_one_signoff_proposal", node_day_one_signoff_proposal)
        graph.add_node("complete", node_complete)
        for name, stage, prefix in AWAIT_NODES:
            graph.add_node(name, _await_node(stage, prefix))

        graph.add_node("form_approval", self._form_approval_node)
        graph.add_node("await_form_approval", _await_node(Stage.INTAKE, "hr_approval"))
        graph.add_edge("await_form_approval", END)
        graph.set_entry_point("form_approval")
        graph.add_conditional_edges("form_approval", route_after_form_approval, route_after_form_approval.targets)
        graph.add_edge("intake", "intake_mismatch_proposal")
        for source, route in (
            ("intake_mismatch_proposal", route_after_intake),
            ("plan_tasks", route_after_plan_tasks),
            ("document_collection", route_after_document_collection),
            ("document_submission", route_after_document_submission),
            ("evaluate_document_result", route_after_document_result),
            ("replan_tasks", route_after_replanning),
            ("orientation_scheduling", route_after_scheduling), ("buddy_assignment", route_after_buddy),
            ("evaluate_overall_status", route_after_evaluation),
            ("day_one_signoff_proposal", route_after_readiness), ("joining_day_welcome", route_after_welcome),
        ):
            graph.add_conditional_edges(source, route, route.targets)
        graph.add_edge("validate_documents", "evaluate_document_result")
        graph.add_edge("apply_revised_plan", "document_collection")  # UC03 applies the revised plan
        graph.add_edge("record_verification", "dispatch_branches")
        # Fan-out: the two branches are successors of the same node and run in the same step, once
        # UC03 verified every document of the current plan version (the only path to dispatch_branches).
        for entry in ("equipment_access", "orientation_scheduling"):
            graph.add_edge("dispatch_branches", entry)
        graph.add_edge("team_introduction", "uc05_branch_end")
        # Fan-in: merge_outcomes waits for both branches, however many steps each one takes.
        graph.add_edge(["equipment_access", "uc05_branch_end"], "merge_outcomes")
        graph.add_edge("merge_outcomes", "evaluate_overall_status")
        graph.add_edge("day_one_readiness", "day_one_signoff_proposal")
        for name, _stage, _prefix in AWAIT_NODES:
            graph.add_edge(name, END)
        graph.add_edge("complete", END)

        if self.deps.checkpointer is None:
            from langgraph.checkpoint.memory import MemorySaver
            self.deps.checkpointer = MemorySaver()
        return graph.compile(checkpointer=self.deps.checkpointer)

    def initial_state(self, inp: OnboardingCaseInput) -> OnboardingCaseState:
        case_id = self.case_id_for(inp)
        return new_state(
            case_id=case_id, client_id=inp.client_id, correlation_id=str(uuid.uuid4()),
            actor={"on_behalf_of": inp.hr_officer_id, "roles": ["hr_admin"], "client_id": inp.client_id},
            # Only what later stages need. The intake-only inputs (intake_inputs_for) are kept out of
            # graph state altogether -- see _run_intake.
            intake_trigger={
                "offer_accepted": inp.offer_accepted, "offer_reference": inp.offer_reference,
                "candidate_name": inp.candidate_name, "event_id": inp.offer_acceptance_event_id,
                "employee_id": inp.employee_id,
                # Job facts (not personal data) the case summary shows; UC01 still receives them in its form.
                "designation": inp.designation, "work_location_type": inp.work_location_type,
            },
            uc02_role_context={
                "role_id": inp.role_id, "department": inp.department, "manager_id": inp.manager_id,
                "location": inp.location, "project": inp.project,
                "offer_accepted_at": inp.offer_accepted_at, "form_approved_at": inp.form_approved_at,
                "approved_by_role": inp.form_approved_by_role,
                # Only what the caller stated: other adapters (UC03) read this dict and must see the same
                # shape as before when these optional inputs are not given.
                **{k: v for k, v in (("grade", inp.grade), ("nationality_group", inp.nationality_group),
                                      ("attributes", inp.attributes)) if v},
            },
            uc04_client_config_ref=inp.uc04_client_config_ref, created_at=now_utc().isoformat(),
        ) | {"case_timezone": inp.timezone, "form_approval": self._form_approval_record(inp),
             "form_extraction": inp.form_extraction,
             # UC05 reads the joiner from this case (UC05_OFFER_EVENT_SOURCE=orchestrator_case): keep the email it
             # invites them at. Any other source: the email stays intake-only, as before.
             "joiner_contact": ({"email": inp.candidate_email}
                                if self.deps.uc05_offer_event_source == "orchestrator_case" and inp.candidate_email
                                else None)}

    def _form_approval_record(self, inp: OnboardingCaseInput) -> dict[str, Any]:
        if not inp.form_approval_required:
            return {"required": False}
        gate = self.policy.gate(FORM_GATE)
        return {
            "required": True, "approval_type": "INTERNAL_ONBOARDING_FORM", "gate": FORM_GATE,
            "status": "not_requested", "approval_id": None, "requested_at": None,
            "requested_by": inp.hr_officer_id, "required_approver_role": gate.approver_role,
            "separation_of_duties": gate.separation_of_duties,
            # Role-based routing: the gate names a role, and no approver directory exists to resolve it to
            # a person. Never filled with a guessed individual.
            "assignment": {"mode": "role", "assigned_approver_id": None, "status": "assignment_pending",
                           "detail": f"no individual approver is assigned (no approver directory is configured); "
                                     f"any user of this tenant holding '{gate.approver_role}'"
                                     + (", other than the requester," if gate.separation_of_duties else "")
                                     + " may decide"},
            "form": {k: getattr(inp, k) for k in _FORM_REVIEW_FIELDS},
            "form_digest": _form_digest(inp), "decision": None, "persistence": None,
        }

    @staticmethod
    def intake_inputs_for(inp: OnboardingCaseInput, case_id: str) -> dict[str, Any]:
        """What only UC01 needs: never part of graph state (see _run_intake)."""
        return {
            "candidate_email": inp.candidate_email, "id_number": inp.id_number,
            "form": {
                # A uuid: UC01 stores the form in onboarding_forms, whose form_id is a uuid column. Derived from the case
                # id, so a resubmitted intake names the same form (UC01 upserts on form_id) instead of a second one.
                "form_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"dact:onboarding-form:{case_id}")),
                "client_id": inp.client_id, "version": 1,
                "candidate_name": inp.candidate_name, "candidate_email": inp.candidate_email,
                "designation": inp.designation, "joining_date": inp.joining_date,
                "work_location_type": inp.work_location_type or "OFFICE",
                "salary_and_terms": "withheld",
                # An internal form submitted by a person waits for the gate's decision; the approval facts
                # are added from that decision in _run_intake, never from the submitter.
                **({"status": "AWAITING_APPROVAL", "approved_by": None, "approved_at": None}
                   if inp.form_approval_required else
                   {"status": "APPROVED", "approved_by": inp.hr_officer_id, "approved_at": now_utc().isoformat()}),
            } if inp.designation and inp.joining_date else None,
            # UC01's CasePlacement, from what the caller stated -- only when a location is given (UC01
            # holds the case with `location_name_required` otherwise; nothing is defaulted here).
            "placement": {k: v for k, v in (
                ("location_name", inp.location), ("department", inp.department), ("project_name", inp.project),
                ("grade", inp.grade), ("nationality_group", inp.nationality_group)) if v} if inp.location else None,
        }

    @staticmethod
    def case_id_for(inp: OnboardingCaseInput) -> str:
        return f"CASE-{uuid.uuid5(uuid.NAMESPACE_URL, f'{inp.client_id}:{inp.offer_reference or inp.candidate_email}').hex[:12]}"

    # ---------------------------------------------------------------- case store

    def _lock(self, case_id: str) -> threading.RLock:
        """Serializes every operation on one case within this process (striped). Several
        orchestrator processes on one case need a shared lock the CaseStateStore does not provide."""
        return self._locks[hash(case_id) % len(self._locks)]

    def _case_lock(self, case_id: str, client_id: str | None) -> threading.RLock:
        """The lock of the case's real id (a case UC01 renamed is also reachable by its original
        id); KeyError -- before anything is locked or changed -- for an unknown or foreign case."""
        return self._lock(self.get_case(case_id, client_id=client_id)["case_id"])

    def list_case_states(self) -> list[OnboardingCaseState]:
        """Every case in the store (aliases skipped) -- for internal callers such as the joining-day
        scheduler, which then acts per case under that case's own tenant."""
        store = self.deps.case_states
        states = []
        for case_id in list(store):
            state = store[case_id]
            if "alias_of" not in state:
                states.append(ensure_lifecycle_fields(copy.deepcopy(state)))
        return states

    def _persist(self, state: OnboardingCaseState, *, folded: bool = False) -> None:
        """Write-through, after checking the state against its typed contract
        (workflow_models.WorkflowStateModel) and against the stored version: a malformed state is never
        persisted, and neither is one that silently undoes a completed stage (StatusTransitionError --
        a COMPLETE stage only leaves COMPLETE through an explicit re-open or an UNCERTAIN relay).
        `state_version` increases by one on every write.

        After another process changed the case during this run (a branch step had to be folded onto
        its write, _persist_branch), this run's whole-state writes -- anything but a `folded` branch
        step -- would overwrite that change: they are refused with StateVersionConflict instead, and a
        resume continues from the committed state."""
        if not folded and state["case_id"] in self._foreign_writes:
            self._foreign_writes.discard(state["case_id"])
            raise StateVersionConflict(f"case {state['case_id']}: another process changed the case during this "
                                       f"run; this run's branch results are saved -- resume the case")
        state["updated_at"] = now_utc().isoformat()
        validate_workflow_state(state)
        store = self.deps.case_states
        stored = store.get(state["case_id"])
        if stored is not None and "alias_of" not in stored:
            regressions = completed_stage_regressions(stored.get("stage_statuses") or {}, state["stage_statuses"])
            if regressions:
                raise StatusTransitionError(f"case {state['case_id']}: {'; '.join(regressions)}")
        state["state_version"] = max(int((stored or {}).get("state_version") or 0),
                                     int(state.get("state_version") or 0)) + 1
        store[state["case_id"]] = copy.deepcopy(state)
        origin = state.get("intake_trigger", {}).get("origin_case_id")
        if origin and origin != state["case_id"]:
            store[origin] = {"alias_of": state["case_id"], "client_id": state["client_id"]}

    def _persist_branch(self, case_id: str, delta: dict[str, Any]) -> None:
        """One branch step written through: folded onto the stored state, which may already hold the
        other branches' steps -- so concurrent branches never overwrite each other in the store. A
        shared store (Supabase) refuses a write when another process changed the case since it was read
        (StateVersionConflict): the step is then folded onto the newly stored state and written again,
        so neither this branch's result nor the other writer's is lost."""
        with self._store_locks[hash(case_id) % len(self._store_locks)]:
            for attempt in range(1, BRANCH_WRITE_ATTEMPTS + 1):
                stored = self._load(case_id)
                if stored is None:
                    return
                try:
                    self._persist(fold_branch_outcome(stored, delta), folded=True)
                    if attempt > 1:
                        # This run's own view no longer includes what the other writer stored: its final
                        # whole-state write must not overwrite it (see _invoke).
                        self._foreign_writes.add(stored["case_id"])
                    return
                except StateVersionConflict:
                    if attempt == BRANCH_WRITE_ATTEMPTS:
                        raise
                    log.info("orchestration.branch_write_conflict", extra={"case_id": case_id, "attempt": attempt})

    def _load(self, case_id: str, *, client_id: str | None = None) -> OnboardingCaseState | None:
        """The stored state (an alias entry resolved). With `client_id`, the store is asked for that
        tenant's case only (a shared store filters the query itself). A store with `load` records the
        version read, which its next write of the case expects (optimistic concurrency)."""
        store = self.deps.case_states
        loader = getattr(store, "load", None)
        if loader is not None:
            state = loader(case_id, client_id=client_id)
        else:
            state = store.get(case_id)
        if state is None:
            return None
        if "alias_of" in state:  # a case UC01 renamed at intake, looked up by its original id
            return self._load(state["alias_of"], client_id=client_id)
        return ensure_lifecycle_fields(copy.deepcopy(state))

    def get_case(self, case_id: str, *, client_id: str) -> OnboardingCaseState:
        """Tenant-scoped read. Another tenant's case is reported exactly like a missing one."""
        state = self._load(case_id, client_id=client_id)
        if state is None or state.get("client_id") != client_id:
            raise KeyError(case_id)
        return state

    def require_role(self, actor: dict[str, Any], settings_key: str) -> None:
        allowed = set(self.policy.settings.get(settings_key, []))
        if not allowed:
            raise OrchestratorAuthorizationError(f"no allowlist configured for '{settings_key}' -- fails closed")
        if not set(actor.get("roles", [])) & allowed:
            raise OrchestratorAuthorizationError(
                f"{actor.get('on_behalf_of')} lacks a role for '{settings_key}' (requires one of {sorted(allowed)})")

    # ---------------------------------------------------------------- primary entry point

    def advance_case(self, inp: OnboardingCaseInput | None, *, case_id: str | None,
                      actor: dict[str, Any] | None = None) -> OnboardingCaseState:
        """Start a new case (`inp` given) or resume an existing one (`case_id` given, `inp`
        None). Mirrors UC05's own `run_event` pattern: drives the graph directly (not through
        `BaseAgent.run()`, which discards the state return) and returns the full
        `OnboardingCaseState`, not just an `AgentResponse`.

        Starting is idempotent: a case is identified by tenant + offer reference. A repeated start
        for a case whose intake has completed resumes that case (a duplicate offer acceptance);
        before intake completes, the newer intake input replaces the older one."""
        if inp is not None and not inp.employee_id and \
                self.deps.tenants.identity_for(inp.client_id).scheme == IdentityScheme.NONE:
            # The tenant identifies employees only by their HRMS number (scheme none): without it no stage could
            # address this joiner, and UC01's own number is not used.
            raise WorkflowRequestError(f"client {inp.client_id} identifies employees by their HRMS employee number "
                                       f"(employee identity scheme 'none'): enter the employee number on the form")
        lock_id = self.case_id_for(inp) if inp is not None else case_id
        with self._lock(lock_id):
            if inp is not None:
                existing = self._load(lock_id)
                if existing is not None and _status(existing, Stage.INTAKE) == StageStatus.COMPLETE.value:
                    self._check_tenant(existing, actor)
                    return self._invoke_or_block(existing, actor)
                state_input = self.initial_state(inp)
                state_input["intake_trigger"]["origin_case_id"] = state_input["case_id"]
                self.deps.intake_inputs[state_input["case_id"]] = self.intake_inputs_for(inp, state_input["case_id"])
                if existing is not None:  # keep what a re-submitted intake must not lose
                    self._check_tenant(existing, actor)
                    for key in ("approval_decisions", "stage_history", "stage_attempts", "correlation_id",
                                "created_at", "stage_outcomes", "approval_persistence"):
                        if key in existing:
                            state_input[key] = existing[key]
                    self._carry_form_approval(existing, state_input, actor)
                if actor is not None:
                    # The real, already-verified initiating actor always wins over initial_state's
                    # placeholder -- never silently dropped in favor of a hardcoded role.
                    state_input["actor"] = actor
                return self._invoke_or_block(state_input, None, actor_for_audit=actor)
            state_input = self._load(case_id, client_id=(actor or {}).get("client_id"))
            if state_input is None:
                raise KeyError(f"no known state for case {case_id} -- cannot resume")
            self._check_tenant(state_input, actor)
            return self._invoke_or_block(state_input, actor)

    def _carry_form_approval(self, existing: OnboardingCaseState, state_input: OnboardingCaseState,
                             actor: dict[str, Any] | None) -> None:
        """A resubmitted form (same offer reference) before intake completed. The same content keeps its
        request -- pending, approved or rejected -- so a duplicate submission never raises a second one. Changed
        content is a corrected form: an undecided request for the old content is withdrawn, a new one raised."""
        old, new = existing.get("form_approval") or {}, state_input.get("form_approval") or {}
        if not old.get("required") or not new.get("required"):
            return
        if old.get("form_digest") == new.get("form_digest"):
            state_input["form_approval"] = old
            if (existing.get("gate_rejections") or {}).get(FORM_GATE):
                state_input["gate_rejections"] = {FORM_GATE: existing["gate_rejections"][FORM_GATE]}
            return
        if old.get("status") == "pending" and old.get("approval_id"):
            try:
                item = self.ctx.hitl.withdraw(old["approval_id"], client_id=existing["client_id"],
                                              by=(actor or {}).get("on_behalf_of", "system"),
                                              reason="the internal onboarding form was corrected and resubmitted")
                self._sync_approval(state_input, item)
            except (KeyError, HITLError):
                pass  # already decided or gone: nothing to withdraw
        state_input.setdefault("approval_decisions", []).append({
            "agent": AGENT_ID, "reference": old.get("approval_id"), "stage": Stage.INTAKE.value, "kind": "superseded",
            "proposal_type": "internal_onboarding_form", "decision": "withdrawn" if old.get("status") == "pending"
            else old.get("status"), "approver": None, "decided_at": now_utc().isoformat(), "applied": True})

    @staticmethod
    def _check_tenant(state: OnboardingCaseState, actor: dict[str, Any] | None) -> None:
        if actor is not None and actor.get("client_id") not in (None, state["client_id"]):
            raise KeyError(state["case_id"])  # never confirm that another tenant's case exists

    def _invoke(self, state_input: OnboardingCaseState, actor: dict[str, Any] | None) -> OnboardingCaseState:
        from hr_onboarding_common.agent import RunContext
        from hr_onboarding_common.contracts import Actor, AgentRequest, AgentResponse, PolicyRef, Provenance, \
            ResponseStatus, WorkflowRef

        if actor is not None:
            state_input["actor"] = actor
        policy = self.policy
        thread_id = state_input["case_id"]
        request = AgentRequest(
            workflow=WorkflowRef(id=AGENT_ID, instance=thread_id, step="advance"),
            actor=Actor(**{k: v for k, v in state_input["actor"].items() if k in ("on_behalf_of", "roles", "client_id")}),
            input=OnboardingCaseInput(client_id=state_input["client_id"], hr_officer_id="system", offer_accepted=True),
        )
        request.policy = PolicyRef(ref=policy.policy, version=policy.version, autonomy=policy.autonomy,
                                    gates=list(policy.gates))
        prov = Provenance(agent=self.agent_id, agent_version=self.version, policy=f"{policy.policy}@v{policy.version}")
        rc = RunContext(self, request, policy, prov)
        # Per-invocation bookkeeping starts afresh (MAX_DISPATCH_ROUNDS and deps.max_replan_rounds bound
        # the graph's two cycles within one invocation).
        state_input.update(dispatch_round=0, active_branches=[], redispatch_branches=[], replan_round=0,
                           document_route=None)
        state_input = self._reconcile_joining_date(ensure_lifecycle_fields(state_input))

        with span("orchestration.invoke", **{"case.id": thread_id, "tenant.id": state_input["client_id"],
                                             "correlation.id": state_input.get("correlation_id")}) as sp:
            context = sp.get_span_context()
            # One trace id per invocation, handed to every stage execution (and through it to the worker
            # wire): the OpenTelemetry trace id when tracing is configured, a fresh one otherwise.
            trace_id = format(context.trace_id, "032x") if context.is_valid else execution.new_trace_id()
            config = {"configurable": {"rc": rc, "thread_id": thread_id, "trace_id": trace_id},
                      "recursion_limit": RECURSION_LIMIT}
            final_state = ensure_lifecycle_fields(self.graph.invoke(state_input, config=config))
        if final_state["case_id"] in self._foreign_writes:
            self._foreign_writes.discard(final_state["case_id"])
            # Every branch step of this run is already stored, folded onto the other writer's state. This
            # run's own whole state predates that write, so it is not written over it: resuming the case
            # continues from the committed state.
            raise StateVersionConflict(f"case {final_state['case_id']}: another process changed the case during "
                                       f"this run; this run's branch results are saved -- resume the case")
        final_state = refresh_uc05_optional_stages(final_state, self.deps)
        self._persist(final_state)

        prov.tool_calls = list(rc.tools.calls)
        prov.finished_at = now_utc()
        status = (ResponseStatus.PROPOSED if rc.proposals else
                  ResponseStatus.ACTED if rc.actions else ResponseStatus.EXCEPTION)
        response = AgentResponse(run_id=request.run_id, status=status, proposals=rc.proposals,
                                  actions_taken=rc.actions, exceptions=rc.exceptions,
                                  brain_feedback=rc.feedback, provenance=prov)
        if rc.proposals:
            submitted = self.submit_for_review(response, workflow=AGENT_ID, instance=final_state["case_id"],
                                               client_id=final_state.get("client_id", "unknown"))
            for item in submitted:
                if item.gate == FORM_GATE and (final_state.get("form_approval") or {}).get("required"):
                    final_state["form_approval"].update(approval_id=item.id, status="pending",
                                                        requested_at=item.created_at.isoformat())
                self._sync_approval(final_state, item)
        # The orchestrator's own undecided gates, read back from its inbox (so a request raised by
        # an earlier run is still listed after later runs), surfaced like every UC's approvals.
        for item in self.ctx.hitl.list(status=Decision.PENDING, workflow_instance=final_state["case_id"],
                                       client_id=final_state["client_id"]):
            if item.agent == AGENT_ID:
                add_pending_approval(final_state, agent=AGENT_ID, reference=item.id, role=item.approver_role,
                                     description=f"orchestrator gate {item.gate}", stage=_GATE_STAGE[item.gate],
                                     gate=item.gate)
        if final_state["stage_statuses"].get(Stage.COMPLETE.value) != StageStatus.COMPLETE.value:
            refresh_workflow_view(final_state)
        self._persist(final_state)
        return final_state

    def _reconcile_joining_date(self, state: OnboardingCaseState) -> OnboardingCaseState:
        """Before a run (in place): results produced for an earlier joining date are re-opened so this
        run produces them again for the current date (joining_date.reconcile), and a day-1 signoff
        requested for an earlier revision is withdrawn. Written through before the run, so a process
        that stops part-way resumes the reconciliation rather than losing it. A no-op when every
        result is current."""
        actions = joining_date.reconcile(state, at=now_utc().isoformat())
        withdrawn = self._withdraw_superseded_signoff(state)
        if not actions and not withdrawn:
            return state
        self._audit("orchestrator.joining_date_reconciliation", state, {"on_behalf_of": f"agent:{AGENT_ID}"},
                    joining_date=state.get("joining_date"), revision=joining_date.revision(state),
                    actions=[{k: a[k] for k in ("stage", "status", "used_joining_date", "action")} for a in actions],
                    withdrawn_approvals=withdrawn or None)
        if state["case_id"] in self.deps.case_states:
            self._persist(state)
        return state

    def _withdraw_superseded_signoff(self, state: OnboardingCaseState) -> list[str]:
        """Withdraw undecided day-1 signoff requests raised for another joining-date revision (a
        request without one predates revisions: revision 1). Not a decision -- nobody approved or
        rejected it; the next readiness evaluation raises a request for the current revision."""
        current = joining_date.revision(state)
        withdrawn = []
        for item in self.ctx.hitl.list(status=Decision.PENDING, gate="day_one_signoff",
                                       workflow_instance=state["case_id"], client_id=state["client_id"]):
            requested = item.payload.get("joining_date_revision", 1)
            if requested == current:
                continue
            self.ctx.hitl.withdraw(item.id, client_id=state["client_id"], by=f"agent:{AGENT_ID}",
                                   reason=f"requested for joining-date revision {requested}; the joining date is now "
                                          f"{state.get('joining_date')} (revision {current})")
            withdrawn.append(item.id)
            state.setdefault("approval_decisions", []).append({
                "agent": AGENT_ID, "reference": item.id, "stage": Stage.DAY_ONE_READINESS.value, "kind": "withdrawal",
                "proposal_type": item.proposal_type, "decision": Decision.WITHDRAWN.value, "approver": None,
                "decided_at": now_utc().isoformat(), "applied": False, "joining_date_revision": requested})
        if withdrawn:
            state["pending_approvals"] = [p for p in state["pending_approvals"] if p["reference"] not in withdrawn]
        return withdrawn

    def resume_case(self, case_id: str, *, actor: dict[str, Any] | None = None) -> OnboardingCaseState:
        """An explicit resume (HR corrected something). It is also what re-opens a rejected
        orchestrator gate: the next pass raises a fresh request for it. A caller without an operator
        role is refused before anything is read or changed."""
        if actor is not None:
            self.require_role(actor, "case_operator_roles")
        with self._lock(case_id):
            state = self._load(case_id)
            if state is not None and state.get("gate_rejections"):
                self._check_tenant(state, actor)
                state["gate_rejections"] = {}
                self._persist(state)
            return self.advance_case(None, case_id=case_id, actor=actor)

    # ---------------------------------------------------------------- documents and retries

    def submit_documents(self, case_id: str, *, documents: list[dict[str, Any]],
                         actor: dict[str, Any]) -> OnboardingCaseState:
        """Supply required documents, or mark them available from records already on file, then
        resume. References and extraction metadata only -- never document contents. Each submission
        of a document type is a new revision; validation resumes with only the changed revisions
        (UC01 is not re-run and the collection request is not re-sent). Refused, with nothing changed:
        an unauthorized caller (403), another tenant's case (404), a case not yet collecting or
        already verified (409), a document type this case does not require (422)."""
        self.require_role(actor, "document_submitter_roles")
        parsed = [DocumentSubmission.model_validate(d) for d in documents]
        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            collection = state.get("document_collection") or {}
            if _status(state, Stage.DOCUMENT_COLLECTION) != StageStatus.COMPLETE.value:
                raise PrerequisiteNotMetError("document collection has not been initiated for this case")
            if _status(state, Stage.VALIDATE_DOCUMENTS) == StageStatus.COMPLETE.value:
                raise PrerequisiteNotMetError("the documents are already verified; nothing is re-opened")
            required = {d["document_type"] for d in collection.get("required_documents", [])}
            types = [d.document_type for d in parsed]
            if not parsed or len(set(types)) != len(types):
                raise WorkflowRequestError("supply each document type once per request")
            unknown = sorted(set(types) - required)
            if unknown:
                raise WorkflowRequestError(f"not a document this case requires: {', '.join(unknown)}")
            # UC03 stores every revision (its own registry); the case keeps the projection. A document
            # identical to its latest revision is a duplicate: no new revision, nothing re-opened.
            service = self._uc03_collection(state)
            now = now_utc().isoformat()
            receipts, changed = [], []
            for doc in parsed:
                metadata = doc.metadata.model_dump(mode="json")
                try:
                    receipt = service.submit(case_id=state["case_id"], tenant_id=state["client_id"],
                                             document_type=doc.document_type, document_ref=doc.document_ref,
                                             source=doc.source, metadata=metadata, submitted_by=actor["on_behalf_of"])
                except ValueError as exc:  # UC03's CollectionError: refused, nothing stored
                    raise WorkflowRequestError(f"UC03 refused {doc.document_type}: {getattr(exc, 'detail', exc)}") \
                        from None
                receipts.append(receipt)
                known = state["submitted_documents"].get(doc.document_type) or {}
                if not receipt.duplicate or known.get("revision") != receipt.revision:
                    # a new revision -- or one UC03 stored before a crash kept the case from recording it
                    changed.append(doc.document_type)
                    state["submitted_documents"][doc.document_type] = SubmittedDocument(
                        document_type=doc.document_type, document_ref=doc.document_ref, source=doc.source,
                        revision=receipt.revision, submitted_at=now, submitted_by=actor["on_behalf_of"],
                        metadata=doc.metadata, storage_id=receipt.storage_id,
                        plan_version=receipt.plan_version).model_dump(mode="json")
            self._audit("orchestrator.documents_submitted", state, actor, documents=[
                {"document_type": r.document_type, "revision": r.revision, "storage_id": r.storage_id,
                 "plan_version": r.plan_version, "duplicate": r.duplicate,
                 "document_ref": state["submitted_documents"][r.document_type]["document_ref"],
                 "source": state["submitted_documents"][r.document_type]["source"]} for r in receipts])
            if not changed:
                return state  # a redelivered submission: nothing new to validate
            self._reopen(state, (Stage.DOCUMENT_SUBMISSION, Stage.VALIDATE_DOCUMENTS))
            self._persist(state)
            return self._invoke(state, None)

    def _uc03_collection(self, state: OnboardingCaseState):
        """UC03's collection service, with the case's collection open in it. A collection UC03 does not
        hold (an in-memory registry after a restart, or a case collected before UC03 owned storage) is
        re-opened from the case's own record of the request -- the same plan version, so idempotent."""
        service = collection_service(self.deps)
        if service.collection(state["case_id"]) is None:
            from hr_onboarding_common.orchestration.adapters.uc03_document_collection import _open_in_uc03
            from hr_onboarding_common.orchestration.workflow_models import DocumentCollectionRequest, RequiredDocument

            collection = state.get("document_collection") or {}
            request = collection.get("request")
            if request is None:  # a legacy collection: its required documents, as plan version 1
                documents = [RequiredDocument.model_validate(d) for d in collection.get("required_documents", [])]
                request = DocumentCollectionRequest(
                    case_id=state["case_id"], tenant_id=state["client_id"], employee_id=state.get("employee_id") or "",
                    correlation_id=state["correlation_id"], plan_id=(state.get("onboarding_plan") or {}).get(
                        "plan_id") or state["case_id"], plan_version=collection.get("plan_version") or 1,
                    required_documents=[d.document_type for d in documents], document_requirements=documents,
                    mandatory_documents=[d.document_type for d in documents if d.required]).model_dump(mode="json")
            _open_in_uc03(DocumentCollectionRequest.model_validate(request), self.deps)
        return service

    # ---------------------------------------------------------------- resolving branch dependencies

    _ITEM_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")  # UC04's own ITEM_ID pattern (sub_agent/contracts.py)
    _EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

    def record_equipment_access_requests(self, case_id: str, *, requested_equipment: list[str],
                                         requested_access: list[str], actor: dict[str, Any]) -> OnboardingCaseState:
        """HR records what equipment and access the joiner needs (UC04's requested_equipment / requested_access,
        item ids of the client's catalogue -- UC04 validates them against it). Resolves NO_ITEMS_REQUESTED; a blocked
        UC04 stage is then re-run, and only that stage (completed stages are skipped)."""
        self.require_role(actor, "case_operator_roles")
        equipment = list(dict.fromkeys(i.strip() for i in requested_equipment if i and i.strip()))
        access = list(dict.fromkeys(i.strip() for i in requested_access if i and i.strip()))
        bad = [i for i in (*equipment, *access) if not self._ITEM_ID.match(i)]
        if bad:
            raise WorkflowRequestError(f"item ids must be catalogue ids (lower-case letters, digits and -): {bad[:5]}")
        if not equipment and not access:
            raise WorkflowRequestError("record at least one requested equipment or access item")
        if len(equipment) > 30 or len(access) > 30:
            raise WorkflowRequestError("at most 30 equipment and 30 access items")
        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            if _status(state, Stage.EQUIPMENT_ACCESS) == StageStatus.COMPLETE.value:
                raise PrerequisiteNotMetError("UC04 already completed for this case; its requests are not changed here")
            role_ctx = state.setdefault("uc02_role_context", {})
            role_ctx["requested_equipment"], role_ctx["requested_access"] = equipment, access
            state["equipment_access_requests"] = {"requested_equipment": equipment, "requested_access": access,
                                                  "recorded_by": actor.get("on_behalf_of"),
                                                  "recorded_at": now_utc().isoformat()}
            self._audit("orchestrator.equipment_access_requests_recorded", state, actor,
                        equipment=len(equipment), access=len(access))
            self._persist(state)
            if _status(state, Stage.EQUIPMENT_ACCESS) in (StageStatus.BLOCKED.value, StageStatus.FAILED.value):
                return self._invoke(state, actor)
            return state

    def record_offer_acceptance_event(self, case_id: str, *, event_id: str, actor: dict[str, Any]) -> OnboardingCaseState:
        """HR supplies the offer-acceptance event reference UC05 resolves the employee from (UC05's mailbox mode).
        UC05 checks the event exists and belongs to this employee; nothing is assumed here. In UC05's Supabase mode the
        case itself is the event, so no reference is accepted."""
        self.require_role(actor, "case_operator_roles")
        if self.deps.uc05_offer_event_source in ("case", "orchestrator_case"):
            raise PrerequisiteNotMetError("UC05 reads the offer acceptance from the case itself; no separate event "
                                          "reference is used")
        if not self._EVENT_ID.match(event_id or ""):
            raise WorkflowRequestError("offer_acceptance_event_id must be a message/event id (letters, digits, . _ : -)")
        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            if (state.get("orientation_schedule_result") or {}).get("uc05_case"):
                raise PrerequisiteNotMetError("UC05 has already linked this case to an offer-acceptance event")
            state.setdefault("intake_trigger", {})["event_id"] = event_id
            self._audit("orchestrator.offer_event_recorded", state, actor)
            self._persist(state)
            if _status(state, Stage.ORIENTATION_SCHEDULING) in (StageStatus.BLOCKED.value, StageStatus.FAILED.value):
                return self._invoke(state, actor)
            return state

    def request_document_requirement_change(self, case_id: str, *, add_documents: list[str],
                                            remove_documents: list[str], reason: str,
                                            actor: dict[str, Any]) -> OnboardingCaseState:
        """HR changes the document requirements (Scenario C). The change is raised to UC03, which reports
        it to UC02 as feedback; UC02 replans (a new plan version) and UC03 applies it. Idempotent: the
        same change against the same plan version is recorded once. Refused, with nothing changed: an
        unauthorized caller (403), another tenant's case (404), no plan yet or documents already
        verified (409), a change that changes nothing or names a document UC03 cannot validate (422)."""
        self.require_role(actor, "document_requirement_change_roles")
        add, remove = sorted(set(add_documents)), sorted(set(remove_documents))
        if not reason.strip() or len(reason) > 200:
            raise WorkflowRequestError("a requirement change needs a reason of at most 200 characters")
        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            plan = state.get("onboarding_plan") or {}
            if _status(state, Stage.PLAN_TASKS) != StageStatus.COMPLETE.value or not plan:
                raise PrerequisiteNotMetError("UC02 has not planned this case yet")
            if _status(state, Stage.VALIDATE_DOCUMENTS) == StageStatus.COMPLETE.value:
                raise PrerequisiteNotMetError("the documents are already verified; the requirements are final")
            planned = {d["document_type"] for d in plan["required_documents"]}
            known = set(collection_service(self.deps).known_document_types())
            if set(add) & set(remove) or not (set(add) - planned or set(remove) & planned):
                raise WorkflowRequestError("the change does not change the plan's document requirements")
            if set(add) - known:
                raise WorkflowRequestError(f"UC03 cannot validate: {', '.join(sorted(set(add) - known))}")
            if set(remove) - planned:
                raise WorkflowRequestError(f"not required by the plan: {', '.join(sorted(set(remove) - planned))}")
            digest = uuid.uuid5(uuid.NAMESPACE_URL, f"{state['case_id']}:{plan['plan_version']}:{add}:{remove}").hex
            request_id = f"RCR-{digest[:16]}"
            if any(r["request_id"] == request_id for r in state.get("requirement_change_requests") or []):
                return state
            state["requirement_change_requests"].append(RequirementChangeRequest(
                request_id=request_id, add_documents=add, remove_documents=remove, reason=reason.strip(),
                requested_by=actor["on_behalf_of"], requested_at=now_utc().isoformat(),
                plan_version=plan["plan_version"]).model_dump(mode="json"))
            self._reopen(state, (Stage.DOCUMENT_SUBMISSION, Stage.VALIDATE_DOCUMENTS))
            self._audit("orchestrator.document_requirements_change_requested", state, actor, request_id=request_id,
                        add_documents=add, remove_documents=remove, plan_version=plan["plan_version"])
            self._persist(state)
            return self._invoke(state, None)

    def review_documents(self, case_id: str, *, reviews: list[dict[str, Any]],
                         actor: dict[str, Any]) -> OnboardingCaseState:
        """An HR reviewer's decision on documents UC03 found valid but cannot verify without HR
        approval (UC03's hr_review_status input), then resume. Approved: UC03 is asked again with the
        decision. Rejected: a corrected revision is required. The decision is the authenticated
        reviewer's; the orchestrator only records and relays it. Only a document whose current
        revision awaits review can be decided (409 otherwise)."""
        self.require_role(actor, "document_review_roles")
        parsed = [DocumentReviewRequest.model_validate(r) for r in reviews]
        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            types = [r.document_type for r in parsed]
            if not parsed or len(set(types)) != len(types):
                raise WorkflowRequestError("decide each document type once per request")
            results = state.get("document_validation_result") or {}
            for review in parsed:
                doc = state["submitted_documents"].get(review.document_type)
                result = results.get(review.document_type) or {}
                if (_status(state, Stage.VALIDATE_DOCUMENTS) == StageStatus.COMPLETE.value or doc is None
                        or result.get("classification") != "hr_review_required"
                        or result.get("revision") != doc["revision"]):
                    raise PrerequisiteNotMetError(f"{review.document_type} is not awaiting an HR review decision")
            now = now_utc().isoformat()
            for review in parsed:
                doc = state["submitted_documents"][review.document_type]
                doc["hr_review"] = DocumentReview(decision=review.decision, revision=doc["revision"],
                                                  reviewer=actor["on_behalf_of"], reviewed_at=now,
                                                  note=review.note).model_dump(mode="json")
            self._reopen(state, (Stage.VALIDATE_DOCUMENTS,))
            self._audit("orchestrator.documents_reviewed", state, actor, reviews=[
                {"document_type": r.document_type, "decision": r.decision,
                 "revision": state["submitted_documents"][r.document_type]["revision"]} for r in parsed])
            self._persist(state)
            return self._invoke(state, None)

    def retry_stage(self, case_id: str, stage: str, *, actor: dict[str, Any]) -> OnboardingCaseState:
        """Retry one stage that failed technically (RETRY_PENDING, a BLOCKED stage with a technical
        code, or FAILED), then resume. Completed stages are skipped as on every resume; the retry
        budget of this stage is reset. For an uncertain document request this is the authorization to
        send again -- under the same idempotency key. Anything else (a wait, an approval, a business
        block, an uncertain UC05 write) is refused with 409: retrying cannot fix it."""
        self.require_role(actor, "stage_retry_roles")
        try:
            target = Stage(stage)
        except ValueError:
            raise WorkflowRequestError(f"unknown stage '{stage}'") from None
        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            status = _status(state, target)
            code = ((state.get("stage_errors") or {}).get(target.value) or {}).get("code")
            if not (status in (StageStatus.RETRY_PENDING.value, StageStatus.FAILED.value)
                    or (status == StageStatus.BLOCKED.value and code in TECHNICAL_RETRY_CODES)):
                raise PrerequisiteNotMetError(f"stage {target.value} is {status}; only a technical failure can be "
                                              f"retried")
            approval_id = self._unapplied_decision(state, target, code)
            if approval_id is not None:
                # A recorded decision whose follow-up failed: retrying the stage IS re-running that follow-up
                # (UC05 HITLInbox.retry_apply) -- the decision stands, nothing is decided again.
                self._audit("orchestrator.stage_retry_requested", state, actor, stage=target.value,
                            previous_status=status, code=code, approval_id=approval_id)
                state = relay_uc05_retry(state, self.deps, approval_id=approval_id, actor=actor)
                self._persist(state)
                return self._invoke(state, None)
            now = now_utc().isoformat()
            meta = RetryMetadata.model_validate(state["retry_metadata"].get(target.value)
                                                or {"max_attempts": self._max_attempts()})
            meta.retries_authorized += 1
            meta.failures, meta.exhausted, meta.last_retry_at = 0, False, now
            state["retry_metadata"][target.value] = meta.model_dump(mode="json")
            notification = (state.get("document_collection") or {}).get("notification")
            if target == Stage.DOCUMENT_COLLECTION and notification:
                notification["retry_authorized"] = True
            self._reopen(state, (target,))
            self._audit("orchestrator.stage_retry_requested", state, actor, stage=target.value, previous_status=status,
                        code=code)
            self._persist(state)
            return self._invoke(state, None)

    @staticmethod
    def _unapplied_decision(state: OnboardingCaseState, stage: Stage, code: str | None) -> str | None:
        """The UC05 approval whose recorded decision failed to apply (DECISION_NOT_APPLIED) on this stage: from the
        blocker, or -- for a blocker recorded before it carried the id -- from the stage error the UC05 adapter wrote
        ("approval <id>: <error>"). None for any other block."""
        if code != "DECISION_NOT_APPLIED" or stage not in UC05_STAGES:
            return None
        for blocker in state.get("blockers") or []:
            if blocker.get("stage") == stage.value and blocker.get("code") == code and blocker.get("approval_id"):
                return blocker["approval_id"]
        detail = ((state.get("stage_errors") or {}).get(stage.value) or {}).get("detail") or ""
        match = re.match(r"approval ([A-Za-z0-9._:-]+): ", detail)
        return match.group(1) if match else None

    @staticmethod
    def _reopen(state: OnboardingCaseState, stages: tuple[Stage, ...], *, code: str = "REOPENED") -> None:
        """Mark stages for re-derivation on the next run (an input changed); history records it."""
        for stage in stages:
            if _status(state, stage) not in (StageStatus.PENDING.value, StageStatus.IN_PROGRESS.value):
                state["stage_statuses"][stage.value] = StageStatus.IN_PROGRESS.value
                state["stage_history"].append({"stage": stage.value, "status": StageStatus.IN_PROGRESS.value,
                                               "at": now_utc().isoformat(), "code": code})

    # ---------------------------------------------------------------- approvals

    def decide_gate(self, case_id: str, gate: str, *, decision: str, approver: str, approver_roles: list[str],
                     client_id: str, reason_code: str | None = None, note: str | None = None) -> OnboardingCaseState:
        """Apply a real HITL decision for one of the orchestrator's OWN gates
        (`intake_mismatch_review`, `day_one_signoff`) and resume the SAME case. Never flips a
        UC-owned approval -- those go through that UC's own mechanism (`decide_approval`)."""
        pending = self.ctx.hitl.list(status=Decision.PENDING, gate=gate, workflow_instance=case_id,
                                      client_id=client_id)
        if not pending:
            raise KeyError(f"no pending '{gate}' approval for case {case_id}")
        return self.decide_approval(case_id, pending[0].id, decision=decision,
                                    actor={"on_behalf_of": approver, "roles": approver_roles, "client_id": client_id},
                                    reason_code=reason_code, note=note)

    def decide_approval(self, case_id: str, approval_id: str, *, decision: str, actor: dict[str, Any],
                        reason_code: str | None = None, note: str | None = None,
                        edited_payload: dict[str, Any] | None = None) -> OnboardingCaseState:
        """One entry point for every approval a case surfaces. The orchestrator's own gates are
        decided in its HITL inbox; a UC05 approval is relayed to UC05's own inbox (which checks the
        approver -- role, tenant, the specific team lead/buddy). Either way the decider is the
        authenticated person in `actor`; the orchestrator decides nothing itself. Approvals of
        UC01-UC04 are decided through those agents' own mechanisms and are not accepted here."""
        from hr_onboarding_common.platform.hitl import ReasonCode

        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            case_id = state["case_id"]
            self._audit("orchestrator.decision_requested", state, actor, approval_id=approval_id, decision=decision)
            own = None
            try:
                own = self.ctx.hitl.get(approval_id, client_id=state["client_id"])
            except KeyError:
                pass
            if own is not None and own.agent == AGENT_ID and own.workflow_instance == case_id:
                if own.gate == FORM_GATE and own.id != (state.get("form_approval") or {}).get("approval_id"):
                    raise PrerequisiteNotMetError(f"approval {approval_id} is not the case's current form approval "
                                                  f"request (it was superseded by a corrected form)")
                if own.gate == "day_one_signoff" and own.status == Decision.PENDING \
                        and own.payload.get("joining_date_revision", 1) != joining_date.revision(state):
                    # A signoff for another joining date is never applied (normally already withdrawn
                    # by the run that reconciled the change).
                    self._withdraw_superseded_signoff(state)
                    self._persist(state)
                    raise PrerequisiteNotMetError(
                        f"approval {approval_id} was requested for joining-date revision "
                        f"{own.payload.get('joining_date_revision', 1)}; the joining date is now "
                        f"{state.get('joining_date')} (revision {joining_date.revision(state)}). It was withdrawn; "
                        f"a new signoff is requested once day-1 readiness is evaluated for the current date")
                if self._decided_not_applied(state, own, decision, actor):
                    # The inbox holds this decision but the case never moved on (its follow-up failed, e.g. the case
                    # could not be stored). The same decision by someone entitled to make it re-runs only the
                    # follow-up: nothing is decided twice, and a different decision is still refused by the inbox.
                    item = own
                    self._audit("orchestrator.decision_reapplied", state, actor, approval_id=item.id, gate=item.gate)
                else:
                    item = self.ctx.hitl.decide(approval_id, Decision(decision), approver=actor["on_behalf_of"],
                                                approver_roles=list(actor.get("roles", [])),
                                                client_id=state["client_id"],
                                                reason_code=ReasonCode(reason_code) if reason_code else None,
                                                note=note, edited_payload=edited_payload)
                    state.setdefault("approval_decisions", []).append({
                        "agent": AGENT_ID, "reference": item.id, "stage": _GATE_STAGE[item.gate].value,
                        "kind": "decision", "proposal_type": item.proposal_type,
                        "decision": str(item.status), "approver": item.approver,
                        "decided_at": item.decided_at.isoformat() if item.decided_at else None, "applied": True})
                if item.gate == FORM_GATE:
                    self._record_form_decision(state, item, actor)
                    self._sync_approval(state, item)
                # Decided: no longer offered as a pending decision, whatever happens to its follow-up.
                state["pending_approvals"] = [p for p in state["pending_approvals"] if p["reference"] != item.id]
                if item.is_approved:
                    if item.gate != FORM_GATE:  # the form gate's approval is recorded in form_approval
                        flag = {"intake_mismatch_review": "intake_mismatch_approved",
                                "day_one_signoff": "day_one_signoff_approved"}[item.gate]
                        state[flag] = True
                    self._persist(state)
                    return self._invoke_or_block(state, None, actor_for_audit=actor)
                stage = _GATE_STAGE[item.gate]
                # Held: no other invocation (a joining-day event, another decision) re-raises the
                # request -- only an explicit resume_case does (it clears gate_rejections).
                state.setdefault("gate_rejections", {})[item.gate] = item.id
                state["pending_approvals"] = [p for p in state["pending_approvals"] if p["reference"] != item.id]
                state = with_stage_status(state, stage, StageStatus.BLOCKED)
                record_stage_error(state, stage, code=f"{item.gate.upper()}_REJECTED",
                                   detail=f"{item.approver} rejected {item.gate} ({reason_code})", retryable=True)
                state["stage_history"].append({"stage": stage.value, "status": StageStatus.BLOCKED.value,
                                               "at": now_utc().isoformat(), "code": f"{item.gate.upper()}_REJECTED"})
                state["next_action"] = f"hr_review:{stage.value}:{item.gate}_rejected"
                outcome = state["stage_outcomes"].get(stage.value)
                if outcome is not None:
                    outcome.update(status=StageStatus.BLOCKED.value, next_action=state["next_action"],
                                   error=state["stage_errors"][stage.value])
                refresh_workflow_view(state)
                self._persist(state)  # not resumed: HR resumes explicitly once the cause is addressed
                return state
            # A UC02 date-change approval is routed by reference, not only by current pending-ness:
            # once decided it leaves pending_approvals, and a repeated decide_approval call for the
            # same id must still reach relay_uc02_decision (which no-ops on it) rather than falling
            # through to UC05's relay for an id UC05 never issued.
            is_uc02 = (any(p["agent"] == UC02_AGENT and p["reference"] == approval_id
                          for p in state["pending_approvals"])
                      or any(d.get("agent") == UC02_AGENT and d.get("reference") == approval_id
                             for d in state.get("approval_decisions") or []))
            if is_uc02:
                state = relay_uc02_decision(state, self.deps, approval_id=approval_id, decision=decision,
                                            approver=actor, reason_code=reason_code, note=note)
                self._persist(state)
                return self._invoke(state, None)
            uc04_item = next((p for p in state["pending_approvals"] if p["agent"] == UC04_AGENT
                              and p["reference"] == approval_id and p.get("proposal_type") == UC04_ITEM_APPROVAL), None)
            if uc04_item is not None:
                return self._decide_uc04_item(state, uc04_item, decision=decision, actor=actor, note=note,
                                              reason_code=reason_code)
            if any(p["agent"] not in (UC05_AGENT, UC02_AGENT) and p["reference"] == approval_id
                   for p in state["pending_approvals"]):
                raise PrerequisiteNotMetError(f"approval {approval_id} belongs to another agent; decide it through "
                                              f"that agent's own approval mechanism")
            state = relay_uc05_decision(state, self.deps, approval_id=approval_id, decision=decision, approver=actor,
                                        reason_code=reason_code, note=note, edited_payload=edited_payload)
            self._persist(state)
            return self._invoke(state, None)

    def _decide_uc04_item(self, state: OnboardingCaseState, entry: dict[str, Any], *, decision: str,
                          actor: dict[str, Any], note: str | None, reason_code: str | None) -> OnboardingCaseState:
        """A person's decision on one item UC04 holds for approval. UC04 takes approvals as approval records -- input
        facts from the platform's approval system -- and decides itself whether one counts (approver role, request,
        item, client/role/site scope, not future-dated: UC04 approvals.py). The orchestrator is that approval system
        here: it records who decided what, as given by the authenticated decider, and has UC04 re-evaluate. Refused
        before anything is recorded: a decider without the role UC04's configuration names, the person who recorded
        the requests (another person approves them), a rejection without a reason."""
        if decision not in ("approved", "rejected"):
            raise WorkflowRequestError("a UC04 item approval is approved or rejected (no edit)")
        who = actor.get("on_behalf_of")
        if entry["role"] not in set(actor.get("roles") or []):
            raise OrchestratorAuthorizationError(f"{who} lacks the '{entry['role']}' role this UC04 approval requires")
        if (state.get("equipment_access_requests") or {}).get("recorded_by") == who:
            raise OrchestratorAuthorizationError(f"{who} recorded these requests; another person approves them")
        if decision == "rejected" and not (note or reason_code):
            raise WorkflowRequestError("a rejection needs a reason")
        record = uc04_approval_record(state, entry, decision=decision, approver_id=who)
        if record is None:
            raise PrerequisiteNotMetError("UC04 no longer holds this request; retry the stage to re-evaluate it")
        role_ctx = state.setdefault("uc02_role_context", {})
        role_ctx["approval_records"] = [*(role_ctx.get("approval_records") or []), record]
        state["pending_approvals"] = [p for p in state["pending_approvals"] if p["reference"] != entry["reference"]]
        state.setdefault("approval_decisions", []).append({
            "agent": UC04_AGENT, "reference": entry["reference"], "stage": Stage.EQUIPMENT_ACCESS.value,
            "kind": "decision", "proposal_type": UC04_ITEM_APPROVAL, "decision": decision, "approver": who,
            "decided_at": now_utc().isoformat(), "applied": True, "note": note, "reason_code": reason_code,
            "approval_record_id": record["approval_id"]})
        self._audit("orchestrator.uc04_item_decided", state, actor, approval_id=entry["reference"], decision=decision,
                    item_id=record["item_id"])
        self._reopen(state, (Stage.EQUIPMENT_ACCESS,), code="UC04_APPROVAL_RECORDED")  # UC04 re-evaluates
        self._persist(state)
        return self._invoke(state, None)

    @staticmethod
    def _record_form_decision(state: OnboardingCaseState, item: Any, actor: dict[str, Any]) -> None:
        """The form approval as actually decided: who (verified by the inbox to hold the gate's role, and not
        the requester), when, and the outcome. UC02 receives these facts -- never the submitter's claim."""
        decided_at = item.decided_at.isoformat() if item.decided_at else now_utc().isoformat()
        fa = state.setdefault("form_approval", {"required": True})
        fa.update(status="approved" if item.is_approved else "rejected", decision={
            "decision": "approved" if item.is_approved else "rejected", "decided_by": item.approver,
            "decided_by_role": item.approver_role, "decided_at": decided_at,
            "reason_code": str(item.reason_code) if item.reason_code else None, "note": item.note})
        if item.is_approved:
            role_ctx = state.setdefault("uc02_role_context", {})
            role_ctx["form_approved_at"] = decided_at
            role_ctx["approved_by_role"] = item.approver_role

    @staticmethod
    def _decided_not_applied(state: OnboardingCaseState, item: Any, decision: str, actor: dict[str, Any]) -> bool:
        """An orchestrator gate the inbox already decided while the case still lists it as pending: the decision was
        recorded, its follow-up never completed. True only for the SAME decision, by someone who may decide the gate
        (its approver role; for the form gate, not the requester -- separation of duties)."""
        if item.status == Decision.PENDING or str(item.status) != decision:
            return False
        if not any(p["agent"] == AGENT_ID and p["reference"] == item.id for p in state.get("pending_approvals") or []):
            return False
        if item.approver_role not in set(actor.get("roles") or []):
            return False
        requester = (state.get("form_approval") or {}).get("requested_by") if item.gate == FORM_GATE else None
        return actor.get("on_behalf_of") != requester

    def _invoke_or_block(self, state: OnboardingCaseState, actor: dict[str, Any] | None, *,
                         actor_for_audit: dict[str, Any] | None = None) -> OnboardingCaseState:
        """`_invoke`, except that a case whose state cannot be stored where it belongs is not left in limbo. The
        Supabase case-state store refuses a case without an onboarding_cases row in its tenant (CaseOwnershipError:
        UC01 opened the case but has not stored it); the run is then recorded on the last stored state as intake
        BLOCKED (CASE_NOT_IN_DATABASE), and the case resumes once UC01 stores it. Nothing is written elsewhere instead."""
        snapshot = copy.deepcopy(state)
        try:
            return self._invoke(state, actor)
        except CaseOwnershipError as exc:
            if _status(snapshot, Stage.INTAKE) == StageStatus.COMPLETE.value:
                raise  # a stored case: not the intake hand-off -- reported as it is
            if actor is not None:
                snapshot["actor"] = actor
            return self._record_case_not_stored(snapshot, exc, actor_for_audit or actor or snapshot.get("actor") or {})

    def _record_case_not_stored(self, state: OnboardingCaseState, exc: Exception,
                                actor: dict[str, Any]) -> OnboardingCaseState:
        state = ensure_lifecycle_fields(state)
        before = _status(state, Stage.INTAKE)
        state = with_stage_status(state, Stage.INTAKE, StageStatus.BLOCKED)
        detail = str(exc)[:500]
        record_stage_error(state, Stage.INTAKE, code=CASE_NOT_STORED_CODE, detail=detail, retryable=True)
        state["next_action"] = "ops_review:intake:case_not_in_database"
        add_blocker(state, stage=Stage.INTAKE, code=CASE_NOT_STORED_CODE, reason=detail, owner="ops",
                    next_action="UC01 must store the case in onboarding_cases (its Supabase mode), or keep the case "
                                "state locally (ORCHESTRATOR_STATE_STORE=sqlite); then resume the case")
        if before != StageStatus.BLOCKED.value:
            state["stage_history"].append({"stage": Stage.INTAKE.value, "status": StageStatus.BLOCKED.value,
                                           "at": now_utc().isoformat(), "code": CASE_NOT_STORED_CODE})
        refresh_workflow_view(state)
        self._audit("orchestrator.case_not_stored", state, actor, code=CASE_NOT_STORED_CODE)
        self._persist(state)
        return state

    def retry_approval(self, case_id: str, approval_id: str, *, actor: dict[str, Any]) -> OnboardingCaseState:
        """UC05's recovery path for a decision whose follow-up failed (retry-apply), then resume."""
        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            self._audit("orchestrator.retry_apply_requested", state, actor, approval_id=approval_id)
            state = relay_uc05_retry(state, self.deps, approval_id=approval_id, actor=actor)
            self._persist(state)
            return self._invoke(state, None)

    # ---------------------------------------------------------------- lifecycle events and HR operations

    def trigger_joining_day(self, case_id: str, *, as_of: date, actor: dict[str, Any]) -> OnboardingCaseState:
        """The joining-day lifecycle event for one case (from a scheduler or an operator). Refused
        before the joining date; recorded once (a repeat changes nothing -- UC05 also drafts at
        most one welcome per employee); the welcome stage then runs as soon as day-1 readiness is
        signed off."""
        self.require_role(actor, "joining_day_trigger_roles")
        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            joining = state.get("joining_date")  # the authoritative date (joining_date.py)
            if not joining:
                raise PrematureLifecycleEventError("the case has no confirmed joining date")
            conflict = joining_date.uc05_conflict(state)
            if conflict:
                raise PrematureLifecycleEventError(
                    f"UC05 holds start date {conflict['uc05_start_date']} for this case but its joining date is "
                    f"{joining}; the joining-date change is not reconciled, so no joining-day event is recorded")
            joining_day = date.fromisoformat(str(joining)[:10])
            if as_of < joining_day:
                raise PrematureLifecycleEventError(f"joining-day event for {as_of} is before the joining date "
                                                   f"{joining_day}; the welcome is not drafted early")
            if not state.get("joining_day_event"):
                state["joining_day_event"] = {"as_of": joining_day.isoformat(), "received_on": as_of.isoformat(),
                                              "received_at": now_utc().isoformat(),
                                              "triggered_by": actor["on_behalf_of"]}
                if _status(state, Stage.JOINING_DAY_WELCOME) == StageStatus.WAITING.value:
                    state["stage_statuses"][Stage.JOINING_DAY_WELCOME.value] = StageStatus.IN_PROGRESS.value
                self._audit("orchestrator.joining_day_recorded", state, actor, as_of=joining_day.isoformat(),
                            received_on=as_of.isoformat())
                self._persist(state)
            # The case's own actor carries on; the trigger's identity is recorded in the event.
            return self._invoke(state, None)

    def request_uc02_date_change(self, case_id: str, *, joining_date: date,
                                 actor: dict[str, Any]) -> tuple[OnboardingCaseState, dict[str, Any]]:
        """HR requests a joining-date change through UC02's real DATE_CHANGE gate (opens its
        ApprovalRequest; nothing is re-planned yet). Requires plan_tasks COMPLETE. Decided through
        the generic `decide_approval` like any other approval -- its apply, if approved, is what
        updates `state["joining_date"]`."""
        self.require_role(actor, "uc02_date_change_roles")
        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            try:
                state, outcome = request_uc02_date_change(state, self.deps, joining_date=joining_date, actor=actor)
            except Exception as exc:
                self._audit("orchestrator.uc02_date_change_refused", state, actor,
                            joining_date=joining_date.isoformat(), error_type=type(exc).__name__)
                raise
            self._audit("orchestrator.uc02_date_change_requested", state, actor, request_id=outcome.get("request_id"))
            # Refresh the derived view (pending_actions, branch_statuses) to show the new approval --
            # never self._invoke here: unlike every other write, nothing became newly runnable, and
            # invoking the graph would resume any stage still genuinely pending for an unrelated
            # reason before this decision is made (reading the date it is about to change).
            refresh_workflow_view(state)
            self._persist(state)
            return state, outcome

    def refresh_uc05_details(self, case_id: str, *, actor: dict[str, Any]) -> tuple[OnboardingCaseState, dict]:
        """HR refreshes the display details of a case's UC05 stage results from UC05 (adapter refresh_uc05_details):
        no status, approval or decision changes, nothing sent to UC05."""
        self.require_role(actor, "uc05_operation_roles")
        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            state, outcome = refresh_uc05_details(state, self.deps, actor=actor)
            self._audit("orchestrator.uc05_details_refreshed", state, actor, refreshed=outcome["refreshed"])
            self._persist(state)
            return state, outcome

    def welcome_message(self, case_id: str, *, actor: dict[str, Any]) -> dict[str, Any]:
        """The joining-day welcome for HR to read: UC05's draft once written (on the joining day), before that a
        preview from the client's approved template. Read-only; names the joiner, so case_identity_reader_roles."""
        self.require_role(actor, "case_identity_reader_roles")
        state = self.get_case(case_id, client_id=actor.get("client_id"))
        return read_welcome_message(state, self.deps, actor=actor)

    def request_uc05_operation(self, case_id: str, *, operation: str, params: dict[str, Any],
                               actor: dict[str, Any]) -> tuple[OnboardingCaseState, dict[str, Any]]:
        """An HR correction or request on the UC05 lifecycle (reschedule a session, schedule the
        optional buddy introduction, re-propose a buddy, redraft a rejected welcome, resolve an
        uncertain ledger operation), then resume. Each is an existing UC05 capability."""
        self.require_role(actor, "uc05_operation_roles")
        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            try:
                state, outcome = request_uc05_operation(state, self.deps, operation=operation, params=params,
                                                        actor=actor)
            except Exception as exc:
                self._audit("orchestrator.uc05_operation_refused", state, actor, operation=operation,
                            reason=params.get("reason"), error_type=type(exc).__name__)
                raise
            self._audit("orchestrator.uc05_operation", state, actor, operation=operation, reason=params.get("reason"),
                        resolution=params.get("resolution"), outcome=outcome.get("outcome"))
            self._persist(state)
            return self._invoke(state, None), outcome

    def record_participant_response(self, case_id: str, *, session_id: str, participant_id: str, response: str,
                                    responded_at: str | None, actor: dict[str, Any]
                                    ) -> tuple[OnboardingCaseState, dict[str, Any]]:
        """An RSVP for one of this case's UC05 sessions -- an independent event forwarded to UC05,
        not a stage (UC05 decides who may answer: the participant, or hr_admin on their behalf). A
        decline opens an HR attendance decision, surfaced in pending_approvals; a cancel or replan
        decided on it re-opens that session's stage if it was complete."""
        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            state, outcome = relay_uc05_participant_response(state, self.deps, session_id=session_id,
                                                             participant_id=participant_id, response=response,
                                                             responded_at=responded_at, actor=actor)
            self._audit("orchestrator.participant_response", state, actor, session_id=session_id,
                        participant_id=participant_id, response=response, outcome=outcome.get("outcome"))
            self._persist(state)
            return self._invoke(state, None), outcome

    # ---------------------------------------------------------------- asynchronous events

    def handle_event(self, case_id: str, *, event_id: str, event_type: str, payload: dict[str, Any],
                     actor: dict[str, Any]) -> tuple[OnboardingCaseState, dict[str, Any]]:
        """An asynchronous event for one case (a document-intake webhook, the joining-day trigger, a
        calendar RSVP, a resume request), applied at most once per `event_id`: a redelivered event is
        answered from `processed_events` and changes nothing -- no second document revision, no
        second RSVP relay. Each type is the existing case operation, with its own role check:

          documents_submitted   {"documents": [...]}                         submit_documents
          joining_day           {"as_of": "YYYY-MM-DD"}                      trigger_joining_day
          participant_response  {"session_id", "participant_id", "response", "responded_at"?}
          resume                {}                                           resume_case

        An event that is refused (403/404/409/422) is not recorded: its redelivery is evaluated again
        (a joining-day event that arrived early is processed when it is redelivered on the day). An
        event id reused for a different event type is refused (409).

        The event id is recorded right after the operation, under the same case lock. A process crash
        between the two writes means a redelivery is applied again (at-least-once in that window) --
        chosen over losing the event: a repeated document submission becomes an HR review in UC03, a
        lost one would leave the case waiting."""
        if event_type not in EVENT_TYPES:
            raise WorkflowRequestError(f"unknown event type '{event_type}'")
        if not EVENT_ID.fullmatch(event_id or ""):
            raise WorkflowRequestError("event_id must be 1-128 letters, digits or . _ : -")
        with self._case_lock(case_id, actor.get("client_id")):
            state = self.get_case(case_id, client_id=actor.get("client_id"))
            real_id = state["case_id"]
            seen = (state.get("processed_events") or {}).get(event_id)
            if seen is not None:
                if seen.get("type") != event_type:
                    raise PrerequisiteNotMetError(f"event {event_id} was already processed as a {seen.get('type')} "
                                                  f"event")
                self._audit("orchestrator.event_duplicate", state, actor, event_id=event_id, event_type=event_type)
                return state, {"event_id": event_id, "type": event_type, "duplicate": True,
                               "first_received_at": seen.get("received_at")}
            outcome: dict[str, Any] = {}
            if event_type == "documents_submitted":
                self.submit_documents(real_id, documents=list(payload.get("documents") or []), actor=actor)
            elif event_type == "joining_day":
                try:
                    as_of = date.fromisoformat(str(payload.get("as_of")))
                except ValueError:
                    raise WorkflowRequestError("joining_day needs as_of as an ISO date") from None
                self.trigger_joining_day(real_id, as_of=as_of, actor=actor)
            elif event_type == "participant_response":
                missing = [k for k in ("session_id", "participant_id", "response") if not payload.get(k)]
                if missing:
                    raise WorkflowRequestError(f"participant_response needs {', '.join(missing)}")
                _, outcome = self.record_participant_response(
                    real_id, session_id=str(payload["session_id"]), participant_id=str(payload["participant_id"]),
                    response=str(payload["response"]), responded_at=payload.get("responded_at"), actor=actor)
            else:
                self.resume_case(real_id, actor=actor)
            state = self._load(real_id)
            events = state.setdefault("processed_events", {})
            events[event_id] = {"type": event_type, "received_at": now_utc().isoformat(),
                                "actor": actor.get("on_behalf_of")}
            for stale in list(events)[:-MAX_PROCESSED_EVENTS]:  # oldest first: dicts keep insertion order
                events.pop(stale)
            self._audit("orchestrator.event_processed", state, actor, event_id=event_id, event_type=event_type)
            self._persist(state)
            return state, {"event_id": event_id, "type": event_type, "duplicate": False, **outcome}

    def stage_executions(self, case_id: str, *, client_id: str) -> dict[str, Any]:
        """Every stage's execution status through the standard contract (execution.stage_execution_view):
        canonical and native status, the last execution's ids, its dependencies evaluated now, the
        sanitized error with its category, warnings and the stage's open approvals."""
        state = self.get_case(case_id, client_id=client_id)
        return {"case_id": state["case_id"], "workflow_status": state.get("workflow_status"),
                "stages": {s.value: execution.stage_execution_view(state, s, self.deps.tenants)
                           for s in Stage if s != Stage.COMPLETE}}

    def _audit(self, event: str, state: OnboardingCaseState, actor: dict[str, Any], **detail: Any) -> None:
        """The orchestrator's own audit trail of who asked for what (ids and reasons, no personal data)."""
        self.ctx.audit.record(event, actor=actor.get("on_behalf_of"), agent=AGENT_ID, client_id=state["client_id"],
                              case_id=state["case_id"], correlation_id=state.get("correlation_id"),
                              **{k: v for k, v in detail.items() if v is not None})
