from enum import StrEnum
from typing import Optional

from pydantic import BaseModel

from .models import ActorType, AuditEntry, FormStatus, OnboardingForm
from .ports import AuditLog


class StartDecision(StrEnum):
    STARTED = "STARTED"
    HELD = "HELD"
    PAUSED = "PAUSED"


class StartRequest(BaseModel):
    request_id: str
    client_id: str
    candidate_email: str
    offer_accepted: bool
    form: Optional[OnboardingForm] = None
    hiring_freeze: bool = False


class StartGateResult(BaseModel):
    decision: StartDecision
    reasons: list[str]
    may_contact_candidate: bool


def evaluate_start_gate(request: StartRequest, audit_log: AuditLog) -> StartGateResult:
    reasons: list[str] = []

    if request.hiring_freeze:
        # EC-20: a hiring freeze pauses the case outright, regardless of
        # how the rest of the gate conditions evaluate.
        result = StartGateResult(
            decision=StartDecision.PAUSED,
            reasons=["hiring freeze is in effect for this client"],
            may_contact_candidate=False,
        )
    else:
        if not request.offer_accepted:
            # EC-19
            reasons.append("offer not accepted")
        if request.form is None:
            reasons.append("no onboarding form")
        else:
            if request.form.status != FormStatus.APPROVED:
                reasons.append("form not approved")
            if request.form.client_id != request.client_id:
                reasons.append("form client mismatch")

        if not reasons:
            result = StartGateResult(
                decision=StartDecision.STARTED,
                reasons=[],
                # UC-01 never contacts the candidate itself, even once
                # started; that always waits for a human/HR approval in a
                # later step (DL-01).
                may_contact_candidate=False,
            )
        else:
            result = StartGateResult(
                decision=StartDecision.HELD,
                reasons=reasons,
                may_contact_candidate=False,
            )

    audit_log.append(
        AuditEntry(
            request_id=request.request_id,
            actor_type=ActorType.AGENT,
            actor_id="uc01-start-gate",
            action="START_GATE_EVALUATED",
            after={"decision": result.decision.value, "reasons": result.reasons},
            reference=request.request_id,
        )
    )

    return result
