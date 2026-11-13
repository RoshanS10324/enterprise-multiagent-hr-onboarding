"""Helpers shared by every adapter: the orchestrator-boundary authorization check (compensating
for UC01/UC02, which do not enforce caller identity at their own entry points -- verified, not
assumed; see docs/ONBOARDING_COMMON_ARCHITECTURE.md), and small state-mutation helpers.
"""

from __future__ import annotations

import copy

from hr_onboarding_common.contracts import ExceptionCode
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import (Blocker, OnboardingCaseState, PendingApproval, Stage, StageError,
                                                      StageStatus)
from hr_onboarding_common.platform.policy import PolicyViolation


class OrchestratorAuthorizationError(PolicyViolation):
    pass


def require_role(deps: OrchestratorDeps, state: OnboardingCaseState, *, settings_key: str) -> None:
    """Defense-in-depth check the orchestrator owns itself, run BEFORE calling into a UC agent
    that does not enforce caller identity at its own entry point (UC01, UC02 -- verified). Never
    bypasses that UC's own checks where they exist (UC03/UC04/UC05) -- this is additive, not a
    substitute."""
    actor_roles = set(state["actor"].get("roles", []))
    allowed = set(deps.policy.settings.get(settings_key, []))
    if not allowed:
        raise OrchestratorAuthorizationError(
            f"onboarding-orchestrator: no allowlist configured for '{settings_key}' -- fails closed"
        )
    if not actor_roles & allowed:
        raise OrchestratorAuthorizationError(
            f"onboarding-orchestrator: actor roles {sorted(actor_roles)} do not satisfy '{settings_key}' "
            f"(requires one of {sorted(allowed)})"
        )


def with_stage_status(state: OnboardingCaseState, stage: Stage, status: StageStatus) -> OnboardingCaseState:
    new_state = copy.deepcopy(state)
    new_state["stage_statuses"][stage.value] = status.value
    new_state["current_stage"] = stage.value
    return new_state


def add_blocker(state: OnboardingCaseState, *, stage: Stage, code: str, reason: str, owner: str,
                 next_action: str, extra: dict | None = None) -> None:
    from hr_onboarding_common.orchestration.dependency_blockers import enrich

    # Structured fields (required_from, required_action, required_data, dependency, resolution, retryable) for a
    # known dependency code; the plain fields otherwise. `extra`: stage-specific detail (e.g. a UC's recommendation).
    blocker = enrich(dict(Blocker(stage=stage.value, code=code, reason=reason, owner=owner, next_action=next_action)))
    blocker.update(extra or {})
    if blocker not in state["blockers"]:  # a resumed stage reporting the same cause adds nothing
        state["blockers"].append(blocker)


def add_pending_approval(state: OnboardingCaseState, *, agent: str, reference: str, role: str,
                          description: str, stage: Stage | None = None, gate: str | None = None,
                          proposal_type: str | None = None, record_id: str | None = None,
                          approver_id: str | None = None) -> None:
    for existing in state["pending_approvals"]:
        if existing["agent"] == agent and existing["reference"] == reference:
            # One entry per approval, however often the stage re-queries it; the person on record may be learnt later.
            if approver_id and existing.get("approver_id") != approver_id:
                existing["approver_id"] = approver_id
            return
    approval = PendingApproval(agent=agent, reference=reference, role=role, description=description)
    # When the orchestrator first surfaced it -- a recorded time, kept across re-queries (the early return above).
    from hr_onboarding_common.contracts import now_utc

    approval["raised_at"] = now_utc().isoformat()
    for key, value in (("stage", stage.value if stage is not None else None), ("gate", gate),
                       ("proposal_type", proposal_type), ("record_id", record_id), ("approver_id", approver_id)):
        if value is not None:
            approval[key] = value
    state["pending_approvals"].append(approval)


def clear_stage_items(state: OnboardingCaseState, stage: Stage) -> None:
    """Before a stage re-runs: drop the blockers and stage-tagged pending approvals it reported
    last time, so what remains afterwards is exactly what is true now (in place)."""
    state["blockers"] = [b for b in state["blockers"] if b["stage"] != stage.value]
    state["pending_approvals"] = [p for p in state["pending_approvals"] if p.get("stage") != stage.value]


def record_stage_error(state: OnboardingCaseState, stage: Stage, *, code: str, detail: str,
                       retryable: bool) -> None:
    detail = detail[:500]
    state["error_code"], state["error_detail"] = code, detail
    state.setdefault("stage_errors", {})[stage.value] = StageError(code=code, detail=detail, retryable=retryable)


def record_policy_denial(state: OnboardingCaseState, stage: Stage, exc: Exception) -> OnboardingCaseState:
    new_state = with_stage_status(state, stage, StageStatus.BLOCKED)
    record_stage_error(new_state, stage, code=ExceptionCode.POLICY_BREACH.value, detail=str(exc), retryable=False)
    new_state["next_action"] = f"hr_review:{stage.value}:unauthorized"
    add_blocker(new_state, stage=stage, code="UNAUTHORIZED_ACTOR", reason=str(exc), owner="hr_admin",
                next_action="Verify actor role and resubmit")
    return new_state


def record_uncertain(state: OnboardingCaseState, stage: Stage, detail: str) -> OnboardingCaseState:
    """A write may or may not have taken effect (worker crash/timeout and re-query inconclusive).
    Never auto-retried, never marked complete -- routed for human review."""
    new_state = with_stage_status(state, stage, StageStatus.UNCERTAIN)
    record_stage_error(new_state, stage, code="UNCERTAIN_OUTCOME", detail=detail, retryable=False)
    new_state["next_action"] = f"ops_review:{stage.value}:uncertain_outcome"
    add_blocker(new_state, stage=stage, code="UNCERTAIN_OUTCOME", reason=detail, owner="ops",
                next_action="Reconcile manually before resuming this stage")
    return new_state
