"""Approval gates (prototype seam).

Replace with ``hr_recruitment_common.platform.hitl.HITLInbox`` once ``uv sync``
works. ``HITLInbox`` provides a ``threading.RLock`` for atomic read-check-write
(review R-7); this in-memory stand-in is single-threaded and does not.

Gate definitions duplicate ``guardrails/policy.yaml: gates`` because ``pyyaml``
is not installed. They MUST stay identical to the YAML — CLAUDE.md rule 3 makes
``policy.yaml`` the single source of truth once it can be parsed.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from onboarding_task_management.contracts import (
    Actor,
    ApprovalRequest,
    DecisionOutcome,
    GateName,
    Role,
    utc_now,
)
from onboarding_task_management.errors import (
    ApprovalRequiredError,
    DataIsolationError,
    InvalidStateError,
    UnauthorizedActionError,
)


@dataclass(frozen=True)
class GateDef:
    name: GateName
    approver_role: Role
    description: str
    sla_hours: int


#: Mirrors guardrails/policy.yaml. Every one of these is `never_auto`.
GATES: dict[GateName, GateDef] = {
    GateName.DATE_CHANGE: GateDef(
        GateName.DATE_CHANGE,
        Role.HR_OPS,
        "Joining date change requires HR Ops approval before tasks are re-planned",
        24,
    ),
    GateName.LOCATION_CHANGE: GateDef(
        GateName.LOCATION_CHANGE,
        Role.HR_OPS,
        "Work location change requires HR Ops approval before re-planning",
        24,
    ),
    GateName.PROJECT_CHANGE: GateDef(
        GateName.PROJECT_CHANGE,
        Role.MANAGER,
        "Project reassignment requires the hiring manager's approval",
        24,
    ),
    GateName.CANCELLATION: GateDef(
        GateName.CANCELLATION,
        Role.HR_OPS,
        "Cancelling an onboarding requires HR Ops approval",
        4,
    ),
    GateName.JOINING_CONFIRMATION: GateDef(
        GateName.JOINING_CONFIRMATION,
        Role.HR_OPS,
        "Joining instructions must not be sent without HR Ops confirmation",
        4,
    ),
    # NEEDS TL CONFIRMATION: the approver for these two gates is HR Ops because every
    # other HR-facing gate here is HR Ops. No approved role mapping names one.
    GateName.PLAN_APPROVAL: GateDef(
        GateName.PLAN_APPROVAL,
        Role.HR_OPS,
        "An onboarding plan must be approved by HR Ops before teams execute it",
        24,
    ),
    GateName.FINAL_REVIEW: GateDef(
        GateName.FINAL_REVIEW,
        Role.HR_OPS,
        "The final onboarding submission must be reviewed by HR Ops",
        24,
    ),
}


#: Gates that may authorise an attribute re-plan.
CHANGE_GATES: frozenset[GateName] = frozenset(
    {GateName.DATE_CHANGE, GateName.LOCATION_CHANGE, GateName.PROJECT_CHANGE}
)

#: Gates that release a plan / accept a final submission.
PLAN_GATES: frozenset[GateName] = frozenset({GateName.PLAN_APPROVAL})
FINAL_GATES: frozenset[GateName] = frozenset({GateName.FINAL_REVIEW})

#: Gates that may authorise cancelling an onboarding.
CANCELLATION_GATES: frozenset[GateName] = frozenset({GateName.CANCELLATION})


class ApprovalInbox:
    """Pending and decided gate requests, partitioned by client."""

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._sequence = 0

    def open_request(
        self,
        actor: Actor,
        gate: GateName,
        onboarding_id: str,
        **payload: object,
    ) -> ApprovalRequest:
        gate_def = GATES[gate]
        self._sequence += 1
        request = ApprovalRequest(
            request_id=f"APR-{self._sequence:06d}",
            onboarding_id=onboarding_id,
            client_id=actor.client_id,
            gate=gate,
            requested_by=actor.actor_id,
            approver_role=gate_def.approver_role,
            payload={k: str(v) for k, v in sorted(payload.items())},
        )
        self._requests[request.request_id] = request
        return request

    def restore(self, request: ApprovalRequest) -> None:
        """Re-seat a request read back from durable storage.

        Used by ``OnboardingService.hydrate`` so a restarted process still
        knows about pending gates. The sequence counter is advanced past any
        restored id, otherwise the next ``open_request`` would mint an id that
        collides with a stored one and silently overwrite it.
        """
        self._requests[request.request_id] = request
        if request.request_id.startswith("APR-"):
            suffix = request.request_id.removeprefix("APR-")
            if suffix.isdigit():
                self._sequence = max(self._sequence, int(suffix))

    def discard(self, request_id: str) -> None:
        """Forget a request that could not be saved, so memory does not run ahead of storage."""
        self._requests.pop(request_id, None)

    def get(self, actor: Actor, request_id: str) -> ApprovalRequest:
        request = self._requests.get(request_id)
        if request is None:
            raise InvalidStateError("unknown approval request", request_id=request_id)
        if request.client_id != actor.client_id:
            raise DataIsolationError(
                "actor is not permitted to access another client's approval request",
                actor_client_id=actor.client_id,
                request_client_id=request.client_id,
            )
        return request

    def decide(
        self,
        actor: Actor,
        request_id: str,
        outcome: DecisionOutcome,
        reason: str = "",
    ) -> ApprovalRequest:
        """Approve or reject. Only the gate's approver role may decide.

        The requester may not decide their own request even if their role
        matches the approver role — that would make the gate self-serving.
        """
        request = self.get(actor, request_id)

        if request.outcome is not DecisionOutcome.PENDING:
            raise InvalidStateError(
                "approval request has already been decided",
                request_id=request_id,
                outcome=str(request.outcome),
            )
        if outcome not in (DecisionOutcome.APPROVED, DecisionOutcome.REJECTED):
            raise InvalidStateError("decision must be approved or rejected")
        if actor.role is not request.approver_role:
            raise UnauthorizedActionError(
                "actor role may not decide this gate",
                actor_role=str(actor.role),
                required_role=str(request.approver_role),
                gate=str(request.gate),
            )
        if actor.actor_id == request.requested_by:
            raise UnauthorizedActionError(
                "requester may not decide their own approval request",
                actor_id=actor.actor_id,
                request_id=request_id,
            )

        request.outcome = outcome
        request.decided_by = actor.actor_id
        request.decided_at = utc_now()
        request.reason = reason
        return request

    def require_approved(
        self,
        actor: Actor,
        request_id: str,
        expected_gates: AbstractSet[GateName],
    ) -> ApprovalRequest:
        """Fetch a request, insisting it is approved **and for the right gate**.

        ``expected_gates`` is mandatory. Without it an approval issued for one
        gate authorises any other gated operation: an approved joining-date
        change could be replayed into ``apply_cancellation`` and terminate the
        whole onboarding (finding C1).

        The gate is checked before the outcome so that a mismatched request is
        always reported as a gate violation, never leaked as "still pending".
        """
        request = self.get(actor, request_id)
        if request.gate not in expected_gates:
            raise UnauthorizedActionError(
                "approval was issued for a different gate",
                request_id=request_id,
                approved_gate=str(request.gate),
                expected_gates=",".join(sorted(g.value for g in expected_gates)),
            )
        if request.outcome is not DecisionOutcome.APPROVED:
            raise ApprovalRequiredError(
                "gated action requires an approved decision",
                request_id=request_id,
                gate=str(request.gate),
                outcome=str(request.outcome),
            )
        return request

    def pending(self, actor: Actor) -> list[ApprovalRequest]:
        return [
            r
            for r in self._requests.values()
            if r.client_id == actor.client_id
            and r.outcome is DecisionOutcome.PENDING
        ]
