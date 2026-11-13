"""Stage 0 adapter: UC01 Employee Record Creation (`open_onboarding_case`).

Real invocation: `open_onboarding_case(OpenCaseRequest, OnboardingDeps) -> OpenCaseResult`,
synchronous, never raises for business outcomes (returns an `outcome` enum: CASE_OPENED | HELD |
PAUSED | LINKED_EXISTING). Runs via the UC01 worker process (module-name collision with UC04/
UC05 on `sub_agent`) -- see orchestration/workers/.

Verified gap: UC01 does not implement "load client-specific knowledge / document checklist /
entitlements" -- not present in its DocumentStore/ports. Not fabricated here; UC02/UC03/UC04 each
load their own client config independently at their own stage.

Verified gap: UC01 has no real mailbox/webhook integration. This adapter accepts an
already-structured trigger only (`open_onboarding_case`); wiring `handle_acceptance_email` to a
live inbox is deployment work, out of scope.

Verified: UC01 does NOT enforce caller identity at this entry point -- the orchestrator's own
boundary check (`require_role`, settings key `intake_trigger_roles`) is the only check applied
before this call.
"""

from __future__ import annotations

import uuid

from hr_onboarding_common.orchestration.adapters._shared import (
    add_blocker, add_pending_approval, record_policy_denial, record_stage_error, record_uncertain, require_role,
    with_stage_status,
)
from hr_onboarding_common.orchestration.contracts import Actor as WorkerActor
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import OnboardingCaseState, Stage, StageStatus, idempotency_key
from hr_onboarding_common.orchestration.workers.uc_worker_client import WorkerCrashedError, WorkerTimeoutError
from hr_onboarding_common.platform.policy import PolicyViolation


def call_uc01_intake(state: OnboardingCaseState, deps: OrchestratorDeps) -> OnboardingCaseState:
    try:
        require_role(deps, state, settings_key="intake_trigger_roles")
    except PolicyViolation as exc:
        return record_policy_denial(state, Stage.INTAKE, exc)

    trigger = state["intake_trigger"]
    actor = state["actor"]
    key = idempotency_key(state, Stage.INTAKE)

    payload = {
        "request_id": f"REQ-{uuid.uuid5(uuid.NAMESPACE_URL, key).hex[:12]}",
        "client_id": state["client_id"],
        "hr_officer_id": actor["on_behalf_of"],
        "offer_accepted": trigger.get("offer_accepted", False),
        "form": trigger.get("form"),
        "hiring_freeze": trigger.get("hiring_freeze", False),
        "id_number": trigger.get("id_number"),
        # UC01 requires a placement (location_name; department/grade/nationality_group/project_name
        # optional) to open a case, and holds the case (reason `location_name_required`) without one.
        # Forwarded only as the trigger states it: the orchestrator never derives or defaults it.
        "placement": trigger.get("placement"),
    }

    worker = deps.worker_pool.get("uc01")
    try:
        response = worker.call(
            "open_onboarding_case", payload,
            actor=WorkerActor(on_behalf_of=actor["on_behalf_of"], roles=actor.get("roles", [])),
            run_id=state["correlation_id"], idempotency_key=key,
        )
    except (WorkerCrashedError, WorkerTimeoutError) as exc:
        return record_uncertain(state, Stage.INTAKE, f"uc01 worker unavailable mid-call: {exc}")

    if not response.ok:
        new_state = with_stage_status(state, Stage.INTAKE, StageStatus.FAILED)
        new_state["error_code"] = response.error_code
        new_state["error_detail"] = response.error_detail
        return new_state

    result = response.result
    outcome = result["outcome"]
    new_state = dict(state)

    case = result.get("case")
    if case is not None and case.get("client_id") not in (None, state["client_id"]):
        # UC01 answered with a case of another tenant: nothing of it enters this case's state.
        failed = with_stage_status(state, Stage.INTAKE, StageStatus.FAILED)
        record_stage_error(failed, Stage.INTAKE, code="TENANT_CONTEXT_CONFLICT", retryable=False,
                           detail="UC01 returned a case that belongs to another tenant")
        failed["next_action"] = "ops_review:intake:tenant_context_conflict"
        return failed

    if outcome in ("CASE_OPENED", "LINKED_EXISTING"):
        # LINKED_EXISTING carries only `existing_case_id` -- UC01's own OpenCaseResult.case is
        # None in that branch (verified); CASE_OPENED carries the full new `case` object.
        if case is not None:
            new_state["case_id"] = case["case_id"]
            new_state["employee_id"] = case.get("employee_no")
            new_state["joining_date"] = case.get("joining_date")
        elif result.get("existing_case_id"):
            new_state["case_id"] = result["existing_case_id"]
        new_state = with_stage_status(new_state, Stage.INTAKE, StageStatus.COMPLETE)
        for approval_id in result.get("approvals_created", []) or []:
            add_pending_approval(new_state, agent="uc01-employee-record", reference=approval_id,
                                  role="hr_admin", description="UC01 case-opening approval")
    else:  # HELD | PAUSED -- unverified acceptance, unapproved form, mismatch, or duplicate needing HR
        new_state = with_stage_status(new_state, Stage.INTAKE, StageStatus.BLOCKED)
        new_state["next_action"] = f"hr_review:intake:{outcome.lower()}"
        for reason in result.get("reasons", []) or ["unspecified"]:
            add_blocker(new_state, stage=Stage.INTAKE, code=outcome, reason=reason, owner="hr_admin",
                        next_action="HR reviews offer/form/duplicate before intake can proceed")
        existing = result.get("existing_case_id")
        if existing:
            new_state["case_id"] = existing  # link, never duplicate

    return new_state
