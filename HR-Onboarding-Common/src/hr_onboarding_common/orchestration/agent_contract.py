"""The standard orchestrator <-> UC contract: one request and one response envelope for every stage,
whatever the UC's native interface (UC01/UC04/UC05 worker operations, UC02's `UC02SubAgent.run`,
UC03's compiled graph).

Three envelopes exist in this package, each with one job -- they layer, they do not compete:

  UCAgentRequest / UCAgentResponse (this module)
      What the orchestrator hands one UC stage and what it gets back, in the same shape for all five
      UCs. The stage wrapper (`OnboardingOrchestratorAgent._execute_stage`) builds the request,
      validates it, checks the stage's dependencies (dependencies.py), calls the adapter, and
      normalizes what came back into the response. The adapters stay the only code that knows a UC's
      native contract.
  WorkerRequest / WorkerResponse (contracts.py)
      The stdio JSON transport to the UC01/UC04/UC05 worker processes. It carries this envelope's
      correlation, trace and execution ids (`current_execution`), so a worker's log lines can be
      joined to the stage execution that caused them.
  hr_onboarding_common.contracts.AgentRequest / AgentResponse
      A BaseAgent's own run envelope (the orchestrator's own run, its HITL proposals). The
      orchestrator never hands it to a UC.

The persisted form of a response is the stage's `StageOutcome` (workflow_models.py): ids, codes and
statuses. The UC's output itself lives in the stage's result field of the case state, never twice.

Statuses: `StageStatus` (state.py) stays the orchestrator's precise, persisted status. `AgentStatus`
below is the canonical cross-UC vocabulary every response carries (`agent_status`), and
`check_transition` says which stage-status changes are valid.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hr_onboarding_common.orchestration.state import (DEPENDENCY_WAIT_CODE, OPTIONAL_STAGES, REPLANNING_WAIT_CODE,
                                                      TECHNICAL_RETRY_CODES, OnboardingCaseState, Stage, StageStatus)

CONTRACT_VERSION = "1.0"

# stage -> use case id. The orchestrator's own stages (the document wait, the day-1 signoff) are
# "orchestrator": they call no UC but go through the same envelope.
USE_CASE_OF: dict[Stage, str] = {
    Stage.INTAKE: "UC01",
    Stage.PLAN_TASKS: "UC02",  # initial planning, incl. the plan's document requirements
    Stage.DOCUMENT_COLLECTION: "UC03",  # UC03 owns the document lifecycle (collection, storage, validation)
    Stage.DOCUMENT_SUBMISSION: "UC03",
    Stage.VALIDATE_DOCUMENTS: "UC03",
    Stage.REPLAN_TASKS: "UC02",  # replanning on UC03's validation feedback
    Stage.EQUIPMENT_ACCESS: "UC04",
    Stage.ORIENTATION_SCHEDULING: "UC05",
    Stage.BUDDY_ASSIGNMENT: "UC05",
    Stage.TEAM_INTRODUCTION: "UC05",
    Stage.BUDDY_INTRODUCTION: "UC05",
    Stage.DAY_ONE_READINESS: "orchestrator",
    Stage.JOINING_DAY_WELCOME: "UC05",
}


# ------------------------------------------------------------------------------- statuses


class AgentStatus(StrEnum):
    """The canonical status of one UC stage, the same for every UC."""

    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"  # waiting for input, an event, or an upstream dependency -- not an error
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"  # a named cause must be corrected first
    FAILED = "FAILED"  # see AgentError.retryable: a technical failure is retryable, a terminal one is not
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"  # a person must review: an unknown write outcome, an HR document review
    SKIPPED = "SKIPPED"  # an optional stage nobody requested


# A stage blocked by one of these codes has not started because an upstream stage is not complete
# (dependencies.py). It is waiting, not broken.
DEPENDENCY_WAIT_CODES: frozenset[str] = frozenset({DEPENDENCY_WAIT_CODE})
# A UC03 stage that reported the plan must change waits for UC02's replan: PENDING, not a block.
REPLANNING_WAIT_CODES: frozenset[str] = frozenset({REPLANNING_WAIT_CODE})
REVIEW_CODES: frozenset[str] = frozenset({"DOCUMENT_REVIEW_REQUIRED"})

_STAGE_TO_AGENT: dict[str, AgentStatus] = {
    StageStatus.PENDING.value: AgentStatus.NOT_STARTED,
    StageStatus.IN_PROGRESS.value: AgentStatus.RUNNING,
    StageStatus.AWAITING_APPROVAL.value: AgentStatus.WAITING_FOR_APPROVAL,
    StageStatus.WAITING.value: AgentStatus.PENDING,
    StageStatus.WAITING_FOR_INPUT.value: AgentStatus.PENDING,
    StageStatus.RETRY_PENDING.value: AgentStatus.FAILED,
    StageStatus.UNCERTAIN.value: AgentStatus.REQUIRES_REVIEW,
    StageStatus.BLOCKED.value: AgentStatus.BLOCKED,
    StageStatus.COMPLETE.value: AgentStatus.COMPLETED,
    StageStatus.FAILED.value: AgentStatus.FAILED,
    StageStatus.NOT_REQUESTED.value: AgentStatus.SKIPPED,
}


def agent_status(stage_status: str, code: str | None = None) -> AgentStatus:
    """The canonical status of a stage status (and, where it changes the meaning, its error code)."""
    if stage_status == StageStatus.BLOCKED.value and code in DEPENDENCY_WAIT_CODES | REPLANNING_WAIT_CODES:
        return AgentStatus.PENDING
    if stage_status == StageStatus.BLOCKED.value and code in TECHNICAL_RETRY_CODES:
        return AgentStatus.FAILED
    if stage_status == StageStatus.AWAITING_APPROVAL.value and code in REVIEW_CODES:
        return AgentStatus.REQUIRES_REVIEW
    return _STAGE_TO_AGENT.get(stage_status, AgentStatus.BLOCKED)


class StatusTransitionError(RuntimeError):
    """A stage status change that the transition rules forbid (an orchestrator invariant)."""


# What a COMPLETE required stage may still become: re-opened for re-derivation (an HR correction, an
# attendance decision that cancelled a session) or UNCERTAIN (a relayed write on its follow-up whose
# outcome is unknown). Anything else would silently undo a completed stage.
LEAVE_COMPLETE: frozenset[str] = frozenset({StageStatus.IN_PROGRESS.value, StageStatus.UNCERTAIN.value})


def transition_error(stage: Stage, before: str, after: str) -> str | None:
    """Why `before -> after` is not a valid status change for `stage`, or None when it is.

    Rules (the optional buddy introduction is exempt -- UC05 alone decides it):
      - a stage that has run never returns to PENDING (not started) or NOT_REQUESTED;
      - a COMPLETE stage only becomes IN_PROGRESS (explicit re-open) or UNCERTAIN.
    Running a stage is otherwise free to end in any status: the routing reads it."""
    if before == after or stage in OPTIONAL_STAGES:
        return None
    if after in (StageStatus.PENDING.value, StageStatus.NOT_REQUESTED.value):
        return f"{stage.value}: {before} -> {after} (a stage that has run cannot become not started)"
    if before == StageStatus.COMPLETE.value and after not in LEAVE_COMPLETE:
        return f"{stage.value}: complete -> {after} (a completed stage is only re-opened explicitly)"
    return None


def check_transition(stage: Stage, before: str, after: str) -> None:
    error = transition_error(stage, before, after)
    if error is not None:
        raise StatusTransitionError(error)


def completed_stage_regressions(stored: dict[str, str], new: dict[str, str]) -> list[str]:
    """Required stages that were COMPLETE in `stored` and are neither COMPLETE nor explicitly re-opened
    (LEAVE_COMPLETE) in `new` -- checked on every write-through to the case store, whatever path
    produced the state."""
    regressions = []
    for stage in Stage:
        before, after = stored.get(stage.value), new.get(stage.value)
        if (stage not in OPTIONAL_STAGES and before == StageStatus.COMPLETE.value and after is not None
                and after != before and after not in LEAVE_COMPLETE):
            regressions.append(f"{stage.value}: complete -> {after}")
    return regressions


# --------------------------------------------------------------------------------- errors


class ErrorCategory(StrEnum):
    VALIDATION = "validation"  # input missing or malformed, a document needs correcting -- fixed by a person
    AUTHORIZATION = "authorization"  # role, tenant or approver refused
    DEPENDENCY = "dependency"  # an upstream stage is not complete yet
    BUSINESS = "business"  # a business rule holds the case: rejection, conflict, no slot, inactive case
    INFRASTRUCTURE = "infrastructure"  # a provider, worker or transport failed -- a retry can fix it
    UNCERTAIN = "uncertain"  # a write's outcome is unknown -- never retried automatically
    CONTRACT = "contract"  # a UC answered outside its contract, or an orchestrator invariant was broken
    INTERNAL = "internal"  # an adapter defect


_CATEGORY_BY_CODE: dict[str, ErrorCategory] = {
    **{c: ErrorCategory.AUTHORIZATION for c in (
        "POLICY_BREACH", "POLICY_DENIED", "UNAUTHORIZED_ACTOR", "UNAUTHORIZED_APPROVER", "TENANT_CONTEXT_MISMATCH",
        "TENANT_CONTEXT_CONFLICT")},
    **{c: ErrorCategory.DEPENDENCY for c in DEPENDENCY_WAIT_CODES | REPLANNING_WAIT_CODES},
    **{c: ErrorCategory.VALIDATION for c in (
        "MISSING_DATA", "INVALID_INPUT", "MISSING_REQUIRED_FIELD", "CASE_CONTEXT_MISMATCH", "INTAKE_INPUT_UNAVAILABLE",
        "IDENTITY_MISMATCH", "JOINING_DATE_MISMATCH", "INCOMPLETE_EMPLOYEE_DATA", "UC05_OFFER_EVENT_MISSING",
        "UC05_OFFER_EVENT_NOT_FOUND", "EMPLOYEE_NOT_IN_UC05_DIRECTORY", "UC05_INVALID_INPUT", "UC05_MISSING_DATA",
        "UC05_CLIENT_CONFIG_MISSING", "UC05_CLIENT_CONFIG_INVALID", "DOCUMENTS_OUTSTANDING",
        "DOCUMENTS_NEED_CORRECTION", "DOCUMENT_CHECKLIST_EMPTY", "NOT_FOUND", "STALE_PLAN_VERSION",
        "PLAN_DOCUMENTS_MISSING", "VALIDATION_FEEDBACK_MISSING")},
    **{c: ErrorCategory.INFRASTRUCTURE for c in (
        "RETRYABLE_PROVIDER_ERROR", "UC05_PROVIDER_UNAVAILABLE", "UC05_UNAVAILABLE", "UC03_TECHNICAL_ERROR",
        "DOCUMENT_CHECKLIST_UNAVAILABLE", "DOCUMENT_REQUEST_DELIVERY_FAILED", "DECISION_NOT_APPLIED",
        "RETRY_LIMIT_REACHED", "UC02_PACKAGE_UNIMPORTABLE", "STAGE_TIMEOUT", "UC02_REPLANNING_UNAVAILABLE",
        "UC03_COLLECTION_UNAVAILABLE")},
    **{c: ErrorCategory.UNCERTAIN for c in ("UNCERTAIN_OUTCOME",)},
    **{c: ErrorCategory.CONTRACT for c in (
        "UC05_CONTRACT_VIOLATION", "INVALID_STATUS_TRANSITION", "STATE_OWNERSHIP_VIOLATION", "IDENTITY_CONFLICT",
        "CONFLICT")},
    "INTERNAL_ERROR": ErrorCategory.INTERNAL,
}


def classify_error(code: str | None) -> ErrorCategory:
    """The category of a stage error code. Unlisted codes are business outcomes (rejections,
    scheduling conflicts, an inactive case, a UC03 finding to resolve); codes ending in `_UNCERTAIN`
    are unknown outcomes and `EMPLOYEE_ID_*` are identity input problems."""
    if not code:
        return ErrorCategory.INTERNAL
    if code in _CATEGORY_BY_CODE:
        return _CATEGORY_BY_CODE[code]
    if code.endswith("_UNCERTAIN"):
        return ErrorCategory.UNCERTAIN
    if code.startswith("EMPLOYEE_ID_"):
        return ErrorCategory.VALIDATION
    return ErrorCategory.BUSINESS


class AgentError(BaseModel):
    """A sanitized stage error: a stable code, its category, and a short detail of ids and codes."""

    model_config = ConfigDict(extra="forbid")

    code: str
    category: ErrorCategory
    detail: str = ""
    retryable: bool = False
    stage: str | None = None


class AgentWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    detail: str = ""


class DependencyCheck(BaseModel):
    """One prerequisite of a stage, as evaluated for one execution (dependencies.py)."""

    model_config = ConfigDict(extra="forbid")

    key: str
    upstream_stage: str
    use_case_id: str
    kind: str  # required | optional
    satisfied: bool
    code: str | None = None  # PREREQUISITE_NOT_MET (upstream not complete) | MISSING_DATA (completed without it)
    detail: str = ""


# ------------------------------------------------------------------------------- envelopes


# The shared context a stage may read: what downstream UCs need from upstream ones, and no more.
# Business detail stays in each UC's own records and in the stage result fields.
SHARED_CONTEXT_FIELDS: tuple[str, ...] = ("employee_id", "joining_date", "case_timezone", "offer_reference")


class ContractViolation(ValueError):
    """A request that must not reach a UC. `code` is stable (MISSING_REQUIRED_FIELD,
    TENANT_CONTEXT_MISMATCH, CASE_CONTEXT_MISMATCH)."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


class UCAgentRequest(BaseModel):
    """What the orchestrator hands one UC stage execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = CONTRACT_VERSION
    case_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    employee_id: str | None = None  # UC01's employee number
    canonical_employee_id: str | None = None  # the HRMS id UC05 uses (state.employee_ref)
    use_case_id: str
    stage: str
    operation: str
    correlation_id: str = Field(min_length=1)  # constant for the case's whole lifecycle
    trace_id: str = Field(min_length=1)  # one orchestrator invocation (OpenTelemetry trace id when tracing is on)
    execution_id: str = Field(min_length=1)  # this one stage attempt
    attempt: int = Field(ge=1)
    idempotency_key: str | None = None
    actor: dict[str, Any]
    input_data: dict[str, Any] = Field(default_factory=dict)  # stage_inputs: ids, dates, flags, document types
    shared_context: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[DependencyCheck] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class UCAgentResponse(BaseModel):
    """What one UC stage execution produced, normalized. `output_data` is the stage's result field
    as written to the case state; `updated_context` the shared-context fields it changed."""

    model_config = ConfigDict(extra="forbid")

    contract_version: str = CONTRACT_VERSION
    case_id: str
    tenant_id: str
    use_case_id: str
    stage: str
    operation: str
    execution_id: str
    trace_id: str
    correlation_id: str
    status: AgentStatus
    stage_status: StageStatus
    output_data: dict[str, Any] = Field(default_factory=dict)
    updated_context: dict[str, Any] = Field(default_factory=dict)
    updated_fields: list[str] = Field(default_factory=list)
    errors: list[AgentError] = Field(default_factory=list)
    warnings: list[AgentWarning] = Field(default_factory=list)
    pending_actions: list[dict[str, Any]] = Field(default_factory=list)
    started_at: str
    finished_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)


def build_request(stage: Stage, state: OnboardingCaseState, *, operation: str, attempt: int, trace_id: str,
                  execution_id: str, input_data: dict[str, Any], dependencies: list[DependencyCheck],
                  metadata: dict[str, Any] | None = None) -> UCAgentRequest:
    """The request for one stage execution, validated against the case it is for. Raises
    ContractViolation: a missing case/tenant/correlation id or actor (MISSING_REQUIRED_FIELD), an actor
    or canonical employee identity of another tenant (TENANT_CONTEXT_MISMATCH)."""
    missing = [key for key in ("case_id", "client_id", "correlation_id") if not state.get(key)]
    if missing:
        raise ContractViolation("MISSING_REQUIRED_FIELD", f"request field(s) missing or empty: {', '.join(missing)}")
    actor = dict(state.get("actor") or {})
    if not actor.get("on_behalf_of"):
        raise ContractViolation("MISSING_REQUIRED_FIELD", "the case carries no verified initiating actor")
    tenant = state.get("client_id")
    # An actor without a tenant is accepted as everywhere else in the orchestrator (_check_tenant,
    # the UC05 adapter's _actor_for); an actor of ANOTHER tenant never reaches a UC.
    if actor.get("client_id") not in (None, tenant):
        raise ContractViolation("TENANT_CONTEXT_MISMATCH",
                                f"actor {actor.get('on_behalf_of')} acts for another tenant than case "
                                f"{state.get('case_id')}")
    ref = state.get("employee_ref") or {}
    if ref and ref.get("client_id") not in (None, tenant):
        raise ContractViolation("TENANT_CONTEXT_MISMATCH",
                                f"the case's canonical employee identity belongs to another tenant than case "
                                f"{state.get('case_id')}")
    try:
        return UCAgentRequest(
            case_id=state.get("case_id") or "", tenant_id=tenant or "", employee_id=state.get("employee_id"),
            canonical_employee_id=ref.get("employee_id"), use_case_id=USE_CASE_OF[stage], stage=stage.value,
            operation=operation, correlation_id=state.get("correlation_id") or "", trace_id=trace_id,
            execution_id=execution_id, attempt=attempt, idempotency_key=(state.get("idempotency_keys") or {}).get(
                stage.value), actor={k: actor.get(k) for k in ("on_behalf_of", "roles", "client_id")},
            input_data=input_data, shared_context=shared_context(state), dependencies=dependencies,
            metadata=metadata or {})
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(x) for x in first.get("loc", ()))
        raise ContractViolation("MISSING_REQUIRED_FIELD", f"request field '{field}' is missing or empty") from None


def shared_context(state: OnboardingCaseState) -> dict[str, Any]:
    """The cross-UC context a stage is handed: identity, dates, verification and readiness flags."""
    context = {key: state.get(key) for key in SHARED_CONTEXT_FIELDS}
    verified = state.get("verified_context") or {}
    statuses = state.get("stage_statuses") or {}
    plan = state.get("onboarding_plan") or {}
    context.update(
        canonical_employee_id=(state.get("employee_ref") or {}).get("employee_id"),
        plan_id=plan.get("plan_id"), plan_version=plan.get("plan_version"),
        documents_verified=bool(verified.get("documents_verified")),
        verified_document_types=list(verified.get("verified_document_types") or []),
        task_plan_available=bool(state.get("task_plan")),
        equipment_access_ready=statuses.get(Stage.EQUIPMENT_ACCESS.value) == StageStatus.COMPLETE.value,
        day_one_signed_off=statuses.get(Stage.DAY_ONE_READINESS.value) == StageStatus.COMPLETE.value,
    )
    return context


def stage_error(state: OnboardingCaseState, stage: Stage) -> AgentError | None:
    """The stage's recorded error as a sanitized AgentError (its category derived from the code)."""
    error = (state.get("stage_errors") or {}).get(stage.value)
    if not error:
        return None
    return AgentError(code=error["code"], category=classify_error(error["code"]), detail=error.get("detail", ""),
                      retryable=bool(error.get("retryable")), stage=stage.value)


def normalize_response(stage: Stage, result: OnboardingCaseState, *, operation: str, execution_id: str, trace_id: str,
                       result_key: str | None, started_at: str, finished_at: str, updated_fields: list[str],
                       warnings: list[AgentWarning], metadata: dict[str, Any] | None = None) -> UCAgentResponse:
    """The response for one stage execution, from the state the execution produced (after state
    synchronization and the wrapper's retry accounting). Errors carry their category; pending actions
    are the stage's own approvals and blockers; `updated_context` holds only the shared-context fields
    that changed; `output_data` the stage's result field when the execution changed it."""
    status = result["stage_statuses"].get(stage.value, StageStatus.PENDING.value)
    error = stage_error(result, stage)
    pending = [{"kind": "approval", **p} for p in result.get("pending_approvals") or [] if p.get("stage") == stage.value]
    pending += [{"kind": "blocker", **b} for b in result.get("blockers") or [] if b.get("stage") == stage.value]
    context_fields = set(SHARED_CONTEXT_FIELDS) | {"case_id", "employee_ref"}
    return UCAgentResponse(
        case_id=result["case_id"], tenant_id=result["client_id"], use_case_id=USE_CASE_OF[stage], stage=stage.value,
        operation=operation, execution_id=execution_id, trace_id=trace_id,
        correlation_id=result.get("correlation_id") or "", status=agent_status(status, error.code if error else None),
        stage_status=StageStatus(status),
        output_data={result_key: result.get(result_key)} if result_key and result_key in updated_fields else {},
        updated_context={k: result.get(k) for k in updated_fields if k in context_fields},
        updated_fields=sorted(updated_fields), errors=[error] if error else [], warnings=warnings,
        pending_actions=pending, started_at=started_at, finished_at=finished_at, metadata=dict(metadata or {}))


# ------------------------------------------------------------------------- execution context


@dataclass(frozen=True)
class ExecutionContext:
    """The ids of the stage execution in progress on this thread (or asyncio task)."""

    correlation_id: str
    trace_id: str
    execution_id: str
    use_case_id: str
    stage: str


current_execution: ContextVar[ExecutionContext | None] = ContextVar("orchestration_execution", default=None)


@contextlib.contextmanager
def execution_scope(request: UCAgentRequest) -> Iterator[ExecutionContext]:
    """Makes the request's ids visible to the transport (UcWorker.call) while the adapter runs."""
    context = ExecutionContext(correlation_id=request.correlation_id, trace_id=request.trace_id,
                               execution_id=request.execution_id, use_case_id=request.use_case_id,
                               stage=request.stage)
    token = current_execution.set(context)
    try:
        yield context
    finally:
        current_execution.reset(token)
