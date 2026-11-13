"""Shared LangGraph state for the onboarding orchestrator.

This is a projection, not a record store: each UC agent's own repository remains the source of
truth for its own business data (UC01 cases/forms, UC02 onboarding records, UC03 case/audit
stores, UC04 case/ticket state, UC05's OnboardingPort and operation ledger). This state carries
IDs, statuses, and small summaries sufficient to resume and to show HR what's outstanding -- never
raw documents, salary figures, message bodies, or more personal data than a stage's own blockers
require.

Three layers are kept distinct (docs/ONBOARDING_COMMON_ARCHITECTURE.md, Durability):
  - this state, checkpointed by LangGraph per node and written through to the CaseStateStore
    after every stage (orchestration/case_store.py) -- the orchestrator's own bookkeeping;
  - each UC's business records, held by that UC (UC05: OnboardingPort, HITL inbox, ledger);
  - external side effects (calendar holds, messages), made idempotent by each UC itself.
Persisting this state does not persist any UC's business data, and does not make any external
operation exactly-once.

Every field added after the first release is optional (TypedDict total=False) and initialised by
`new_state`; `ensure_lifecycle_fields` back-fills it on a state persisted by an older version.

The TypedDict below is the LangGraph channel schema (every adapter reads and writes it as a dict).
Its typed contract is `workflow_models.WorkflowStateModel` (pydantic), validated on every
write-through to the case store. Workflow status semantics (docs/STATE_DRIVEN_ONBOARDING_WORKFLOW.md):
`stage_statuses` say where each stage is; `workflow_status` and `branch_statuses` aggregate them
deterministically (`derive_workflow_status`) -- a wait is never a completion, and one finished
parallel branch never hides another branch's failure.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, NotRequired, TypedDict


class Stage(StrEnum):
    """Workflow order: UC01 -> UC02 initial planning -> UC03 document collection, submission and
    validation (with UC02 replanning when UC03 reports the plan must change) -> UC04 || UC05."""

    INTAKE = "intake"  # UC01 employee record creation
    # UC02 initial task planning: task checklist, owners, dependencies, deadlines, and the plan's
    # versioned document requirements (onboarding_plan). Runs right after intake.
    PLAN_TASKS = "plan_tasks"
    # UC03 document collection: opens the collection for the plan version's requirements and sends the
    # (mock) document request (adapters/uc03_document_collection.py).
    DOCUMENT_COLLECTION = "document_collection"
    DOCUMENT_SUBMISSION = "document_submission"  # UC03: every required document of the plan stored
    VALIDATE_DOCUMENTS = "validate_documents"  # UC03; its structured feedback routes the workflow
    # UC02 replanning on UC03's validation feedback (conditional: NOT_REQUESTED until UC03 asks for it).
    REPLAN_TASKS = "replan_tasks"
    # After successful verification, two parallel branches (Branch below):
    EQUIPMENT_ACCESS = "equipment_access"  # UC04
    # UC05 lifecycle (sequence taken from UC05's own workflow edges, agent.py: an approved
    # induction proposes the buddy; an approved buddy drafts the team introduction).
    ORIENTATION_SCHEDULING = "orientation_scheduling"  # UC05 induction (orientation) session
    BUDDY_ASSIGNMENT = "buddy_assignment"
    TEAM_INTRODUCTION = "team_introduction"
    BUDDY_INTRODUCTION = "buddy_introduction"  # optional: HR-requested, never blocks readiness
    DAY_ONE_READINESS = "day_one_readiness"
    JOINING_DAY_WELCOME = "joining_day_welcome"  # runs on the joining-day event, after readiness signoff
    COMPLETE = "complete"


class Branch(StrEnum):
    """The parallel successors of successful document validation (UC03). UC02's plan_tasks and
    UC03's document stages are sequential prerequisites (not branches) that must COMPLETE before
    either of these two starts (dependencies.py). Neither of the two waits for the other: no UC04 or
    UC05 contract consumes the other's result."""

    EQUIPMENT_ACCESS = "uc04_equipment_access"
    ORIENTATION = "uc05_orientation"  # UC05's own order inside the branch: induction -> buddy -> team intro


BRANCH_STAGES: dict[Branch, tuple[Stage, ...]] = {
    Branch.EQUIPMENT_ACCESS: (Stage.EQUIPMENT_ACCESS,),
    Branch.ORIENTATION: (Stage.ORIENTATION_SCHEDULING, Stage.BUDDY_ASSIGNMENT, Stage.TEAM_INTRODUCTION,
                         Stage.BUDDY_INTRODUCTION),
}
# The state fields each branch owns (written only by that branch; folded in by merge_outcomes).
BRANCH_RESULT_KEYS: dict[Branch, tuple[str, ...]] = {
    Branch.EQUIPMENT_ACCESS: ("equipment_access_result",),
    Branch.ORIENTATION: ("orientation_schedule_result", "buddy_assignment_result", "team_introduction_result",
                         "buddy_introduction_result", "employee_ref"),
}

# Stages day-1 readiness requires, in workflow order.
READINESS_STAGES: tuple[Stage, ...] = (
    Stage.INTAKE, Stage.PLAN_TASKS, Stage.DOCUMENT_COLLECTION, Stage.DOCUMENT_SUBMISSION,
    Stage.VALIDATE_DOCUMENTS, Stage.EQUIPMENT_ACCESS, Stage.ORIENTATION_SCHEDULING, Stage.BUDDY_ASSIGNMENT,
    Stage.TEAM_INTRODUCTION,
)
# Stages that run only when asked for: NOT_REQUESTED until then (which counts as done), but once they
# run they are tracked like any required stage -- a blocked or failed replan holds the workflow.
CONDITIONAL_STAGES: frozenset[Stage] = frozenset({Stage.REPLAN_TASKS})
# Every stage the workflow needs before it is complete. The optional buddy introduction is not one.
REQUIRED_STAGES: tuple[Stage, ...] = READINESS_STAGES + (Stage.REPLAN_TASKS, Stage.DAY_ONE_READINESS,
                                                         Stage.JOINING_DAY_WELCOME)
UC05_STAGES: tuple[Stage, ...] = (
    Stage.ORIENTATION_SCHEDULING, Stage.BUDDY_ASSIGNMENT, Stage.TEAM_INTRODUCTION, Stage.BUDDY_INTRODUCTION,
    Stage.JOINING_DAY_WELCOME,
)
OPTIONAL_STAGES: frozenset[Stage] = frozenset({Stage.BUDDY_INTRODUCTION})
STAGE_ORDER: tuple[Stage, ...] = tuple(s for s in Stage if s != Stage.COMPLETE)
# The UC03 stages a revised UC02 plan re-opens (collection for the new version, then submission and
# validation against it).
DOCUMENT_STAGES: tuple[Stage, ...] = (Stage.DOCUMENT_COLLECTION, Stage.DOCUMENT_SUBMISSION, Stage.VALIDATE_DOCUMENTS)


class StageStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"  # resumable once the named cause is corrected
    AWAITING_APPROVAL = "awaiting_approval"
    WAITING = "waiting"  # a normal wait for a future lifecycle event (the joining day) -- not an error
    WAITING_FOR_INPUT = "waiting_for_input"  # documents to supply, or to correct; resumes when they arrive
    RETRY_PENDING = "retry_pending"  # a technical failure, eligible for retry (never an invalid document)
    UNCERTAIN = "uncertain"  # a write may or may not have taken effect; never auto-retried
    COMPLETE = "complete"
    FAILED = "failed"  # terminal for this run; distinct from BLOCKED (which is resumable)
    NOT_REQUESTED = "not_requested"  # an optional stage nobody asked for


class OverallStatus(StrEnum):
    """The coarse status kept for existing API clients; `WorkflowStatus` is the precise one."""

    IN_PROGRESS = "in_progress"
    AT_RISK = "at_risk"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    FAILED = "failed"


class WorkflowStatus(StrEnum):
    """Workflow- and branch-level status (derive_workflow_status / derive_branch_status)."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    RETRY_PENDING = "retry_pending"


# A BLOCKED stage with one of these codes is a technical failure a retry can fix, not a business
# block (codes of the existing UC05 adapter; the newer stages use RETRY_PENDING directly).
TECHNICAL_RETRY_CODES: frozenset[str] = frozenset({"UC05_PROVIDER_UNAVAILABLE", "UC05_UNAVAILABLE",
                                                   "DECISION_NOT_APPLIED"})
# A BLOCKED stage with this code has not started: an upstream stage it depends on is not complete yet
# (dependencies.py). It is pending, not a block -- the upstream stage is what needs attention.
DEPENDENCY_WAIT_CODE = "PREREQUISITE_NOT_MET"
# A UC03 stage BLOCKED with this code reported that the plan must change: it waits for UC02's replan
# (the replan_tasks stage carries the real status), so it is pending, not a business block.
REPLANNING_WAIT_CODE = "REPLANNING_REQUIRED"

# The aggregation order: the status needing the most intervention wins. COMPLETED only when every
# counted stage is complete.
WORKFLOW_STATUS_PRIORITY: tuple[WorkflowStatus, ...] = (
    WorkflowStatus.FAILED, WorkflowStatus.BLOCKED, WorkflowStatus.RETRY_PENDING,
    WorkflowStatus.WAITING_FOR_APPROVAL, WorkflowStatus.WAITING_FOR_INPUT, WorkflowStatus.RUNNING,
    WorkflowStatus.PENDING,
)
_STAGE_TO_WORKFLOW: dict[str, WorkflowStatus] = {
    StageStatus.PENDING.value: WorkflowStatus.PENDING,
    StageStatus.IN_PROGRESS.value: WorkflowStatus.RUNNING,
    StageStatus.AWAITING_APPROVAL.value: WorkflowStatus.WAITING_FOR_APPROVAL,
    StageStatus.WAITING.value: WorkflowStatus.WAITING_FOR_INPUT,
    StageStatus.WAITING_FOR_INPUT.value: WorkflowStatus.WAITING_FOR_INPUT,
    StageStatus.RETRY_PENDING.value: WorkflowStatus.RETRY_PENDING,
    # An unknown outcome needs an operator to reconcile before anything is written again.
    StageStatus.UNCERTAIN.value: WorkflowStatus.BLOCKED,
    StageStatus.BLOCKED.value: WorkflowStatus.BLOCKED,
    StageStatus.FAILED.value: WorkflowStatus.FAILED,
    StageStatus.COMPLETE.value: WorkflowStatus.COMPLETED,
    StageStatus.NOT_REQUESTED.value: WorkflowStatus.COMPLETED,
}

BRANCH_OUTCOMES_RESET = "__reset__"


def merge_branch_outcomes(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """LangGraph reducer of the `branch_outcomes` channel. Each parallel branch writes only its own
    key, so the two branches writing in the same step never overwrite each other. A write
    carrying BRANCH_OUTCOMES_RESET clears the channel (before fan-out and after fan-in)."""
    left, right = dict(left or {}), dict(right or {})
    if right.pop(BRANCH_OUTCOMES_RESET, False):
        return right
    return {**left, **right}


class Blocker(TypedDict):
    stage: str
    code: str
    reason: str
    owner: str
    next_action: str


class PendingApproval(TypedDict):
    agent: str  # which UC (or "onboarding-orchestrator") owns this approval
    reference: str  # that UC's own approval/gate id -- the orchestrator never decides it itself
    role: str
    description: str
    stage: NotRequired[str]  # the orchestrator stage this approval belongs to (cleared when it re-runs)
    gate: NotRequired[str]
    proposal_type: NotRequired[str]  # the UC's kind of approval, e.g. session_booking, attendee_declined
    record_id: NotRequired[str]  # the record it decides (session/draft id): a decision must match it
    approver_id: NotRequired[str]  # the one person who may decide it (team lead / buddy on record), when specific
    raised_at: NotRequired[str]  # when the orchestrator first surfaced it (ISO, UTC)


class StageEvent(TypedDict):
    stage: str
    status: str
    at: str
    code: NotRequired[str]


class StageError(TypedDict):
    code: str
    detail: str
    retryable: bool  # True: resume after the cause is fixed (or the provider recovers)


class OnboardingCaseState(TypedDict, total=False):
    # Identity
    case_id: str
    client_id: str
    employee_id: str | None  # as reported by UC01 (its employee_no); UC01-UC04 adapters use it
    # The canonical (HRMS) employee id every UC05 operation uses, resolved through the tenant's
    # identity scheme (tenant_config.EmployeeRef: employee_id, employee_no, source, scheme).
    employee_ref: dict[str, Any] | None
    offer_reference: str | None
    joining_date: str | None  # ISO date only -- never a datetime carrying a name/location
    # The joining date's revision (joining_date.py): 0 while unknown, 1 once UC01 sets it, +1 per
    # approved change; its history is append-only. `joining_date_consistency` is derived (whether the
    # date-dependent results were produced for the current date), recomputed by refresh_workflow_view.
    joining_date_revision: int
    joining_date_history: list[dict[str, Any]]
    joining_date_consistency: dict[str, Any] | None
    case_timezone: str | None  # the employee's IANA timezone, when supplied at intake

    # Workflow position
    current_stage: str  # Stage: the stage that most needs attention (workflow.primary_stage)
    overall_status: str  # OverallStatus (coarse, kept for existing clients)
    workflow_status: str  # WorkflowStatus (derive_workflow_status)
    stage_statuses: dict[str, str]  # Stage -> StageStatus
    branch_statuses: dict[str, dict[str, Any]]  # Branch -> {status, stages, next_action, error}
    completed_stages: list[str]  # COMPLETE stages in workflow order -- not the same as a complete workflow
    pending_actions: list[dict[str, Any]]  # what has to happen next and who acts (ids and codes only)
    created_at: str | None
    updated_at: str | None

    # UC02's onboarding plan (owner: plan_tasks / replan_tasks): plan id and version, the plan's
    # document requirements, the effective joining date (workflow_models.OnboardingPlanRecord).
    # Every replan is a new version; plan_history is its append-only audit trail.
    onboarding_plan: dict[str, Any] | None
    plan_history: list[dict[str, Any]]
    revised_plan: dict[str, Any] | None  # the last UC02 -> UC03 RevisedPlan
    # HR changes to the document requirements, raised to UC03 and applied by a UC02 replan.
    requirement_change_requests: list[dict[str, Any]]

    # UC03 document collection, submission and validation
    document_collection: dict[str, Any] | None  # plan version collected, required/outstanding documents, the request
    # document type -> reference, source, revision, extraction metadata UC03 needs, HR review decision.
    # A projection of UC03's document registry (UC03 stores every revision); references and metadata
    # only, never document contents; the metadata never reaches a log or response.
    submitted_documents: dict[str, dict[str, Any]]
    validation_issues: list[dict[str, Any]]  # per document: UC03 findings other than VALID
    correction_requirements: list[dict[str, Any]]  # per document: supply / resubmit / HR review
    # UC03 -> UC02 structured feedback for the plan version it validated (DocumentValidationFeedback).
    document_validation_feedback: dict[str, Any] | None
    # Per invocation: how often the UC03 -> UC02 replanning loop ran, and the route evaluate_document_result chose.
    replan_round: int
    document_route: str | None

    # Post-verification fan-out
    verified_context: dict[str, Any] | None  # what every branch receives (ids, dates, verified document types)
    # Transient: each branch's outcome of the current run, folded into the fields above by merge_outcomes.
    branch_outcomes: Annotated[dict[str, dict[str, Any]], merge_branch_outcomes]

    # Per-stage structured outcome (operation, inputs passed, result field, status) and retry bookkeeping
    stage_outcomes: dict[str, dict[str, Any]]
    retry_metadata: dict[str, dict[str, Any]]

    # Per-stage projections (ids/status/evidence refs only -- see module docstring)
    task_plan: dict[str, Any] | None
    document_validation_result: dict[str, Any] | None
    equipment_access_result: dict[str, Any] | None
    orientation_schedule_result: dict[str, Any] | None  # UC05 induction + the UC05 case reference
    buddy_assignment_result: dict[str, Any] | None
    team_introduction_result: dict[str, Any] | None
    buddy_introduction_result: dict[str, Any] | None
    welcome_message_result: dict[str, Any] | None

    # Cross-cutting tracking
    pending_approvals: list[PendingApproval]
    approval_decisions: list[dict[str, Any]]  # human decisions relayed to a UC (ids, decision, outcome)
    # RSVPs forwarded to UC05 (independent events, not a stage): session id/type, participant id,
    # response, UC05's outcome. Ids and statuses only.
    participant_responses: list[dict[str, Any]]
    outstanding_tasks: list[str]
    blockers: list[Blocker]
    evidence_references: list[str]
    correlation_id: str
    idempotency_keys: dict[str, str]  # stage -> stable key
    error_code: str | None  # the last error (of the stage that ran last); per stage: stage_errors
    error_detail: str | None
    next_action: str | None
    stage_history: list[StageEvent]  # status transitions, appended only when a status changes
    stage_errors: dict[str, StageError]  # cleared when the stage completes
    stage_attempts: dict[str, int]  # how many times each stage node actually ran (not skipped)

    # The joining-day lifecycle event, once received (see OnboardingOrchestratorAgent.trigger_joining_day).
    joining_day_event: dict[str, Any] | None

    # State synchronization (state_sync.py): which stage execution last produced each owned field --
    # {field: {stage, use_case_id, execution_id, at}}.
    field_provenance: dict[str, dict[str, Any]]
    # Asynchronous events already processed, by event id (OnboardingOrchestratorAgent.handle_event):
    # {event_id: {type, received_at, actor}}. A redelivered event is answered from here, not applied again.
    processed_events: dict[str, dict[str, Any]]
    # Incremented on every write-through to the case store (a monotonic version of the case).
    state_version: int
    # Fan-out bookkeeping of the current invocation: how often the branches were dispatched, which branches
    # this dispatch runs, and which branches a dependency-blocked stage can now resume in.
    dispatch_round: int
    active_branches: list[str]
    redispatch_branches: list[str]

    # Identity threaded through every adapter call -- the REAL verified initiating actor,
    # never a fabricated "orchestrator" principal (see orchestration/contracts.py Actor).
    actor: dict[str, Any]

    # The orchestrator's OWN two gates (intake_mismatch_review, day_one_signoff -- see policy.yaml
    # and docs). Flipped only by an external, authorized HITL decision applied through
    # `OnboardingOrchestratorAgent.decide_gate(...)` -- never by a node itself.
    intake_mismatch_approved: bool
    day_one_signoff_approved: bool
    gate_rejections: dict[str, str]  # gate -> rejected approval id; cleared only by an explicit resume
    # The internal onboarding form's approval request (orchestrator gate `onboarding_form_approval`):
    # {"required": False} for a trusted, attested start; otherwise the requester, required role, assignment,
    # status (not_requested | pending | approved | rejected), the actual decision, and its persistence.
    form_approval: dict[str, Any] | None
    # approval id -> the result of writing that request through to Supabase `approval_requests`.
    approval_persistence: dict[str, dict[str, Any]]
    # HR's record of the joiner's requested equipment/access for UC04 (who, when, what).
    equipment_access_requests: dict[str, Any] | None
    # The joiner's contact from the HR-approved form ({"email": ...}), kept ONLY while UC05 reads the joiner from the
    # orchestrator's case (UC05_OFFER_EVENT_SOURCE=orchestrator_case): UC05 needs the email to invite the joiner.
    # Never part of any API view; otherwise the email stays intake-only (dropped after UC01).
    joiner_contact: dict[str, Any] | None
    # The onboarding-form document the form was prefilled from, when there was one (OnboardingCaseInput.form_extraction):
    # UC01's extraction statuses, field names, the document digest, the fields HR changed, who confirmed the review.
    # Never extracted values.
    form_extraction: dict[str, Any] | None
    # HR restarts of UC05's part of the case after UC05 lost its records (who, when, why, which stages were uncertain).
    uc05_restarts: list[dict[str, Any]]

    # Minimal inputs each adapter needs, carried from intake rather than re-derived. Once intake
    # completes, the fields only UC01 needed (id_number, email, form) are removed.
    intake_trigger: dict[str, Any]
    uc02_role_context: dict[str, Any]  # role_id/department/manager_id/location/project -- see
    # the UC01<->UC02 field gap in docs/ONBOARDING_COMMON_ARCHITECTURE.md; UC01's own Case/
    # OnboardingForm do not carry these, so the orchestrator's own intake input supplies them.
    uc04_client_config_ref: str | None  # pointer to that tenant's CaseConfig, not the config itself


def _initial_statuses() -> dict[str, str]:
    return {s.value: (StageStatus.NOT_REQUESTED if s in OPTIONAL_STAGES | CONDITIONAL_STAGES
                      else StageStatus.PENDING).value
            for s in Stage if s != Stage.COMPLETE}


def new_state(
    *,
    case_id: str,
    client_id: str,
    correlation_id: str,
    actor: dict[str, Any],
    intake_trigger: dict[str, Any],
    uc02_role_context: dict[str, Any] | None = None,
    uc04_client_config_ref: str | None = None,
    created_at: str | None = None,
) -> OnboardingCaseState:
    return OnboardingCaseState(
        case_id=case_id,
        client_id=client_id,
        employee_id=None,
        employee_ref=None,
        offer_reference=intake_trigger.get("offer_reference"),
        joining_date=None,
        joining_date_revision=0,
        joining_date_history=[],
        joining_date_consistency=None,
        case_timezone=None,
        current_stage=Stage.INTAKE,
        overall_status=OverallStatus.IN_PROGRESS,
        workflow_status=WorkflowStatus.PENDING,
        stage_statuses=_initial_statuses(),
        branch_statuses={},
        completed_stages=[],
        pending_actions=[],
        created_at=created_at,
        updated_at=created_at,
        onboarding_plan=None,
        plan_history=[],
        revised_plan=None,
        requirement_change_requests=[],
        document_collection=None,
        submitted_documents={},
        validation_issues=[],
        correction_requirements=[],
        document_validation_feedback=None,
        replan_round=0,
        document_route=None,
        verified_context=None,
        branch_outcomes={},
        stage_outcomes={},
        retry_metadata={},
        task_plan=None,
        document_validation_result=None,
        equipment_access_result=None,
        orientation_schedule_result=None,
        buddy_assignment_result=None,
        team_introduction_result=None,
        buddy_introduction_result=None,
        welcome_message_result=None,
        pending_approvals=[],
        approval_decisions=[],
        participant_responses=[],
        outstanding_tasks=[],
        blockers=[],
        evidence_references=[],
        correlation_id=correlation_id,
        idempotency_keys={},
        error_code=None,
        error_detail=None,
        next_action=None,
        stage_history=[],
        stage_errors={},
        stage_attempts={},
        joining_day_event=None,
        field_provenance={},
        processed_events={},
        state_version=0,
        dispatch_round=0,
        active_branches=[],
        redispatch_branches=[],
        actor=actor,
        intake_mismatch_approved=False,
        day_one_signoff_approved=False,
        gate_rejections={},
        intake_trigger=intake_trigger,
        uc02_role_context=uc02_role_context or {},
        uc04_client_config_ref=uc04_client_config_ref,
    )


def ensure_lifecycle_fields(state: OnboardingCaseState) -> OnboardingCaseState:
    """Back-fill fields a state persisted by an earlier version does not have (in place)."""
    statuses = state.setdefault("stage_statuses", {})
    if statuses.get(Stage.VALIDATE_DOCUMENTS.value) == StageStatus.COMPLETE.value:
        # A case persisted by the sequential version had its documents validated without these two
        # stages; they are complete in substance and must not send a second collection request.
        for stage in (Stage.DOCUMENT_COLLECTION, Stage.DOCUMENT_SUBMISSION):
            statuses.setdefault(stage.value, StageStatus.COMPLETE.value)
    for stage, status in _initial_statuses().items():
        statuses.setdefault(stage, status)
    for key in ("buddy_assignment_result", "team_introduction_result", "buddy_introduction_result",
                "welcome_message_result", "joining_day_event", "employee_ref", "case_timezone",
                "document_collection", "verified_context", "created_at", "updated_at", "joining_date_consistency",
                "onboarding_plan", "revised_plan", "document_validation_feedback", "document_route",
                "form_approval", "equipment_access_requests", "joiner_contact", "form_extraction"):
        state.setdefault(key, None)
    state.setdefault("replan_round", 0)
    # A date persisted before revisions existed is the first revision (joining_date.revision).
    state.setdefault("joining_date_revision", 1 if state.get("joining_date") else 0)
    for key in ("approval_decisions", "stage_history", "pending_approvals", "blockers", "outstanding_tasks",
                "evidence_references", "participant_responses", "completed_stages", "pending_actions",
                "validation_issues", "correction_requirements", "active_branches", "redispatch_branches",
                "joining_date_history", "plan_history", "requirement_change_requests"):
        state.setdefault(key, [])
    for key in ("stage_errors", "stage_attempts", "idempotency_keys", "gate_rejections", "branch_statuses",
                "submitted_documents", "branch_outcomes", "stage_outcomes", "retry_metadata", "field_provenance",
                "processed_events", "approval_persistence"):
        state.setdefault(key, {})
    state.setdefault("workflow_status", WorkflowStatus.PENDING.value)
    state.setdefault("state_version", 0)
    state.setdefault("dispatch_round", 0)
    return state


def idempotency_key(state: OnboardingCaseState, stage: Stage) -> str:
    return state["idempotency_keys"].setdefault(stage.value, f"{state['case_id']}:{stage.value}:v1")


def derive_overall_status(state: OnboardingCaseState) -> str:
    """Overall status from the stage statuses alone (never from free text):
    any FAILED -> failed; any UNCERTAIN or RETRY_PENDING -> at_risk; the stage the case is parked on
    WAITING (a normal wait for the joining day) -> in_progress; otherwise parked -> blocked."""
    statuses = state["stage_statuses"]
    if statuses.get(Stage.COMPLETE.value) == StageStatus.COMPLETE.value:
        return OverallStatus.COMPLETE.value
    values = set(statuses.values())
    if StageStatus.FAILED.value in values:
        return OverallStatus.FAILED.value
    if values & {StageStatus.UNCERTAIN.value, StageStatus.RETRY_PENDING.value}:
        return OverallStatus.AT_RISK.value
    if statuses.get(state.get("current_stage", "")) == StageStatus.WAITING.value:
        return OverallStatus.IN_PROGRESS.value
    return OverallStatus.BLOCKED.value


def stage_workflow_status(state: OnboardingCaseState, stage: Stage) -> WorkflowStatus:
    """One stage's status in workflow terms. A BLOCKED stage whose error is a technical failure a
    retry can fix (TECHNICAL_RETRY_CODES) is RETRY_PENDING, not a business block; one that waits for an
    upstream stage (DEPENDENCY_WAIT_CODE) is PENDING -- the upstream stage carries the real status."""
    status = state["stage_statuses"].get(stage.value, StageStatus.PENDING.value)
    if status == StageStatus.BLOCKED.value:
        code = ((state.get("stage_errors") or {}).get(stage.value) or {}).get("code")
        if code in (DEPENDENCY_WAIT_CODE, REPLANNING_WAIT_CODE):
            return WorkflowStatus.PENDING
        return WorkflowStatus.RETRY_PENDING if code in TECHNICAL_RETRY_CODES else WorkflowStatus.BLOCKED
    return _STAGE_TO_WORKFLOW.get(status, WorkflowStatus.BLOCKED)


def aggregate_workflow_status(statuses: list[WorkflowStatus]) -> WorkflowStatus:
    """COMPLETED only when every status is COMPLETED; otherwise the first of
    WORKFLOW_STATUS_PRIORITY present (FAILED > BLOCKED > RETRY_PENDING > WAITING_FOR_APPROVAL >
    WAITING_FOR_INPUT > RUNNING > PENDING)."""
    if all(s == WorkflowStatus.COMPLETED for s in statuses):
        return WorkflowStatus.COMPLETED
    return next(s for s in WORKFLOW_STATUS_PRIORITY if s in statuses)


def derive_branch_status(state: OnboardingCaseState, branch: Branch) -> WorkflowStatus:
    """A branch's status over its required stages (an optional stage never affects it)."""
    return aggregate_workflow_status([stage_workflow_status(state, s) for s in BRANCH_STAGES[branch]
                                      if s not in OPTIONAL_STAGES])


def derive_workflow_status(state: OnboardingCaseState) -> WorkflowStatus:
    """The workflow's status over every required stage, all branches included. COMPLETED only once
    the case reached its terminal `complete` stage (every required stage complete, HR signoff and the
    joining-day welcome included)."""
    if state["stage_statuses"].get(Stage.COMPLETE.value) == StageStatus.COMPLETE.value:
        return WorkflowStatus.COMPLETED
    status = aggregate_workflow_status([stage_workflow_status(state, s) for s in REQUIRED_STAGES])
    return WorkflowStatus.RUNNING if status == WorkflowStatus.COMPLETED else status
