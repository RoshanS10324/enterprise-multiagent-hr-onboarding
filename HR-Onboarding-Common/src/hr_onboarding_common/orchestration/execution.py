"""One stage execution through the standard UC contract (agent_contract.py):

    build and validate the UCAgentRequest -> check the stage's dependencies (dependencies.py)
      -> call the adapter inside the request's execution scope (its ids reach the worker wire)
      -> synchronize the adapter's update into the case state (state_sync.py)
      -> check the stage's status transition -> normalize the UCAgentResponse

`invoke` does everything up to the synchronized state; `respond` normalizes the response from the
state the orchestrator finally keeps (after its retry accounting). `OnboardingOrchestratorAgent.
_execute_stage` runs the two around its own bookkeeping (history, attempts, retry budget); `execute`
runs them back to back for one stage outside the graph (contract tests, a single-stage run).

Nothing here knows a UC's native contract -- that stays in the adapters.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.observability.tracing import span
from hr_onboarding_common.orchestration import dependencies
from hr_onboarding_common.orchestration.adapters._shared import add_blocker, record_stage_error, with_stage_status
from hr_onboarding_common.orchestration.agent_contract import (USE_CASE_OF, AgentWarning, ContractViolation,
                                                               DependencyCheck, UCAgentRequest, UCAgentResponse,
                                                               agent_status, build_request, execution_scope,
                                                               normalize_response, stage_error, transition_error)
from hr_onboarding_common.orchestration.state import OnboardingCaseState, Stage, StageStatus
from hr_onboarding_common.orchestration.state_sync import synchronize
from hr_onboarding_common.orchestration.workflow import STAGE_OPERATIONS, stage_inputs
from hr_onboarding_common.orchestration.workflow_models import DATA_ORIGIN

Adapter = Callable[[OnboardingCaseState], OnboardingCaseState]

# Contract codes that are authorization refusals (the stage is BLOCKED, as for record_policy_denial);
# every other rejection is a contract failure (FAILED). None is retryable: an operator reconciles.
_AUTHORIZATION_CODES = frozenset({"TENANT_CONTEXT_MISMATCH", "TENANT_CONTEXT_CONFLICT"})


def new_execution_id() -> str:
    return f"EXE-{uuid.uuid4().hex[:16]}"


def new_trace_id() -> str:
    return uuid.uuid4().hex  # 32 hex characters, the OpenTelemetry trace-id format


@dataclass
class Invocation:
    """What one stage execution did, before the orchestrator's own bookkeeping."""

    stage: Stage
    operation: str
    result_key: str | None
    execution_id: str
    trace_id: str
    started_at: str
    state: OnboardingCaseState
    request: UCAgentRequest | None = None  # None: the request was refused before any UC was called
    dependencies: list[DependencyCheck] = field(default_factory=list)
    warnings: list[AgentWarning] = field(default_factory=list)
    updated_fields: list[str] = field(default_factory=list)
    adapter_called: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def reject(state: OnboardingCaseState, stage: Stage, code: str, detail: str) -> OnboardingCaseState:
    """`stage` refused by the contract layer: nothing the adapter returned (if it ran) is kept."""
    status = StageStatus.BLOCKED if code in _AUTHORIZATION_CODES else StageStatus.FAILED
    new_state = with_stage_status(copy.deepcopy(state), stage, status)
    record_stage_error(new_state, stage, code=code, detail=detail, retryable=False)
    new_state["next_action"] = f"ops_review:{stage.value}:{code.lower()}"
    add_blocker(new_state, stage=stage, code=code, reason=detail, owner="ops",
                next_action="Reconcile the case state with the UC's own records before retrying this stage")
    return new_state


def invoke(stage: Stage, state: OnboardingCaseState, adapter: Adapter, *, attempt: int, trace_id: str,
           tenants: Any = None, run: Callable[[OnboardingCaseState, Adapter], OnboardingCaseState] | None = None,
           metadata: dict[str, Any] | None = None) -> Invocation:
    """Run `adapter` for `stage` on `state` through the contract. `run(state, adapter)` lets the caller
    wrap the call itself (the orchestrator merges UC01's intake-only inputs there). An adapter
    exception becomes a FAILED stage (INTERNAL_ERROR), never a crash and never a success."""
    _uc, operation, result_key = STAGE_OPERATIONS[stage]
    started_at = now_utc().isoformat()
    meta = {"data_origin": DATA_ORIGIN, **(metadata or {})}
    invocation = Invocation(stage=stage, operation=operation, result_key=result_key, execution_id=new_execution_id(),
                            trace_id=trace_id, started_at=started_at, state=state, metadata=meta)
    checks = dependencies.evaluate(stage, state, tenants)
    invocation.dependencies = checks
    invocation.warnings = dependencies.optional_warnings(checks)
    try:
        request = build_request(stage, state, operation=operation, attempt=attempt, trace_id=trace_id,
                                execution_id=invocation.execution_id, input_data=stage_inputs(stage, state),
                                dependencies=checks, metadata=meta)
    except ContractViolation as exc:
        invocation.state = reject(state, stage, exc.code, exc.detail)
        return invocation
    invocation.request = request
    unmet = dependencies.blocking(checks)
    if unmet:
        invocation.state = dependencies.dependency_block(state, stage, unmet)
        return invocation

    # The adapter works on its own copy: several adapters shallow-copy the state and append to its lists,
    # which would otherwise change the snapshot the update is validated against (and kept on rejection).
    snapshot, working = state, copy.deepcopy(state)
    with execution_scope(request), span("orchestration.stage", **{
            "case.id": request.case_id, "tenant.id": request.tenant_id, "stage": stage.value,
            "use_case.id": request.use_case_id, "execution.id": request.execution_id,
            "correlation.id": request.correlation_id, "orchestration.trace_id": trace_id}):
        invocation.adapter_called = True
        try:
            raw = run(working, adapter) if run is not None else adapter(working)
        except Exception as exc:  # noqa: BLE001 -- an adapter defect must surface, not crash the graph
            raw = with_stage_status(snapshot, stage, StageStatus.FAILED)
            record_stage_error(raw, stage, code="INTERNAL_ERROR", detail=f"{type(exc).__name__}: {exc}",
                               retryable=False)

    sync = synchronize(snapshot, raw, stage, execution_id=invocation.execution_id, at=now_utc().isoformat())
    invocation.warnings += sync.warnings
    if not sync.accepted:
        invocation.state = reject(snapshot, stage, sync.violations[0].code,
                                  "; ".join(f"{v.field}: {v.detail}" for v in sync.violations))
        return invocation
    before = (snapshot.get("stage_statuses") or {}).get(stage.value, StageStatus.PENDING.value)
    after = (sync.state.get("stage_statuses") or {}).get(stage.value, StageStatus.PENDING.value)
    invalid = transition_error(stage, before, after)
    if invalid is not None:
        invocation.state = reject(snapshot, stage, "INVALID_STATUS_TRANSITION", invalid)
        return invocation
    invocation.state, invocation.updated_fields = sync.state, sync.updated_fields
    _normalize_reported_items(snapshot, invocation.state, stage)
    return invocation


def _normalize_reported_items(before: OnboardingCaseState, state: OnboardingCaseState, stage: Stage) -> None:
    """What some adapters report less precisely, made uniform so every response carries it the same way
    (in place): approvals they add without a stage (UC01, UC02 planning, UC04) are tagged with this
    stage, and a failure reported only through `error_code`/`error_detail` becomes the stage's recorded
    error. A COMPLETE stage has no error."""
    known = {(p["agent"], p["reference"]) for p in before.get("pending_approvals") or []}
    for approval in state.get("pending_approvals") or []:
        if "stage" not in approval and (approval["agent"], approval["reference"]) not in known:
            approval["stage"] = stage.value
    errors = state.setdefault("stage_errors", {})
    status = (state.get("stage_statuses") or {}).get(stage.value)
    if status == StageStatus.COMPLETE.value:
        errors.pop(stage.value, None)
    elif state.get("error_code") and stage.value not in errors:
        record_stage_error(state, stage, code=state["error_code"], detail=state.get("error_detail") or "",
                           retryable=status in (StageStatus.BLOCKED.value, StageStatus.AWAITING_APPROVAL.value))


def respond(invocation: Invocation, result: OnboardingCaseState) -> UCAgentResponse:
    """The normalized response of `invocation`, from the state finally kept for it."""
    return normalize_response(invocation.stage, result, operation=invocation.operation,
                              execution_id=invocation.execution_id, trace_id=invocation.trace_id,
                              result_key=invocation.result_key, started_at=invocation.started_at,
                              finished_at=now_utc().isoformat(), updated_fields=invocation.updated_fields,
                              warnings=invocation.warnings, metadata=invocation.metadata)


def execute(stage: Stage, state: OnboardingCaseState, adapter: Adapter, *, attempt: int = 1,
            trace_id: str | None = None, tenants: Any = None) -> tuple[UCAgentResponse, OnboardingCaseState]:
    """One stage through the contract, outside the graph: (the normalized response, the state to keep).
    The previous run's last-error fields are cleared first, as the orchestrator's stage wrapper does."""
    working = copy.deepcopy(state)
    working["error_code"] = working["error_detail"] = working["next_action"] = None
    invocation = invoke(stage, working, adapter, attempt=attempt, trace_id=trace_id or new_trace_id(), tenants=tenants)
    return respond(invocation, invocation.state), invocation.state


def stage_execution_view(state: OnboardingCaseState, stage: Stage, tenants: Any = None) -> dict[str, Any]:
    """One stage's execution status for an operator: its canonical and native status, the last
    execution's ids and timing (from its StageOutcome), its dependencies evaluated against the current
    state, its sanitized error with the error's category, warnings, retry bookkeeping and the stage's
    open approvals. Ids, codes and statuses only."""
    status = (state.get("stage_statuses") or {}).get(stage.value, StageStatus.PENDING.value)
    error = stage_error(state, stage)
    outcome = (state.get("stage_outcomes") or {}).get(stage.value) or {}
    _uc, operation, result_key = STAGE_OPERATIONS[stage]
    return {
        "stage": stage.value, "use_case_id": USE_CASE_OF[stage], "operation": operation, "result_key": result_key,
        "status": agent_status(status, error.code if error else None).value, "stage_status": status,
        "attempts": (state.get("stage_attempts") or {}).get(stage.value, 0),
        "last_execution": {k: outcome.get(k) for k in ("execution_id", "trace_id", "correlation_id", "attempt",
                                                        "started_at", "finished_at", "canonical_status")}
        if outcome else None,
        "dependencies": [c.model_dump(mode="json") for c in dependencies.evaluate(stage, state, tenants)],
        "error": error.model_dump(mode="json") if error else None,
        "warnings": list(outcome.get("warnings") or []),
        "updated_fields": list(outcome.get("updated_fields") or []),
        "next_action": outcome.get("next_action"),
        "retry": (state.get("retry_metadata") or {}).get(stage.value),
        "pending_approvals": [p for p in state.get("pending_approvals") or [] if p.get("stage") == stage.value],
    }


def case_errors(state: OnboardingCaseState) -> list[dict[str, Any]]:
    """Every stage's current error, sanitized and categorized, in workflow order."""
    errors = []
    for stage in Stage:
        if stage == Stage.COMPLETE:
            continue
        error = stage_error(state, stage)
        if error is not None:
            errors.append({**error.model_dump(mode="json"), "use_case_id": USE_CASE_OF[stage],
                           "stage_status": (state.get("stage_statuses") or {}).get(stage.value)})
    return errors


__all__ = ["USE_CASE_OF", "Invocation", "case_errors", "execute", "invoke", "new_execution_id", "new_trace_id",
           "reject", "respond", "stage_execution_view"]
