"""The UC-02 agent loop.

One run is a fixed, auditable sequence:

``retrieve`` → ``analyse`` → ``advise`` → ``decide`` → ``execute`` → ``verify`` → ``persist``

The sequence is fixed on purpose. A free-running loop that decides its own
next step is not auditable, and UC-02 touches employment paperwork, work
permits and system access — every step has to be explainable after the fact.
The model contributes at ``advise`` only; everything else is code.

Guarantees
----------

* **No unapproved write.** ``decide`` maps advice onto an allowlisted tool and
  refuses anything restricted without an approved gate. A restricted
  recommendation becomes a *proposal* for a human, never an action.
* **No unverified success.** Every write is followed by ``verify``, which
  re-reads the stored record. If the re-read disagrees, the run is reported
  as ``EXCEPTION`` — not as a success.
* **Degradation is visible.** If the model fails or is overruled, the run
  still completes deterministically and says so.
* **Everything is audited.** Each step appends an audit entry carrying ids,
  counts and outcomes only.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field

from onboarding_task_management.agent import tools as uc02_tools
from onboarding_task_management.agent.advisor import (
    Advisor,
    AgentAdvice,
    OfflineAdvisor,
    advise_with_fallback,
)
from onboarding_task_management.agent.tools import UC02Tools
from onboarding_task_management.contracts import Actor, utc_now
from onboarding_task_management.persistence import OnboardingStore
from onboarding_task_management.sub_agent.service import OnboardingService

AGENT_ID = "onboarding-task-management"
AGENT_VERSION = "0.2.0"


class RunStatus(StrEnum):
    """Outcome of one agent run. Mirrors the harness ``ResponseStatus``.

    ``OBSERVED`` is UC-02's addition: a read-only run that correctly found
    nothing to do. Reporting that as ``ACTED`` would overstate what happened.
    """

    OBSERVED = "observed"
    PROPOSED = "proposed"
    ACTED = "acted"
    EXCEPTION = "exception"


#: Recommended actions that may be carried out without a new human decision.
#: Everything here is a read, a draft, or progressing a task the rules engine
#: has already declared READY — each still fully checked by the service.
AUTONOMOUS_ACTIONS = frozenset(
    {"inspect_blockers", "prepare_followup", "no_action_required"}
)

#: Actions that change the plan. These always become a proposal for a human.
RESTRICTED_ACTIONS = frozenset(
    {"request_date_change", "escalate_configuration_review"}
)


class AgentStep(BaseModel):
    """One step of the run, recorded whether it succeeded or not."""

    name: str
    ok: bool = True
    tool: str | None = None
    detail: str = ""


class AgentProposal(BaseModel):
    """Something the agent wants a human to decide. Never self-approved."""

    action: str
    subject_id: str | None = None
    rationale: str
    requires_approval_by: str | None = None


class AgentRunResult(BaseModel):
    """Everything one run did, and everything it refused to do."""

    run_id: str
    agent_id: str = AGENT_ID
    agent_version: str = AGENT_VERSION
    onboarding_id: str
    client_id: str
    actor_id: str
    as_of: date
    status: RunStatus
    advisor: str = "offline"
    model: str | None = None
    degraded: bool = False
    degraded_reason: str = ""
    advice: AgentAdvice | None = None
    steps: list[AgentStep] = Field(default_factory=list)
    proposals: list[AgentProposal] = Field(default_factory=list)
    actions_taken: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    verified: bool | None = None

    def to_row(self) -> dict:
        """Row for ``uc02.agent_run``. Ids, counts and outcomes only."""
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "client_id": self.client_id,
            "onboarding_id": self.onboarding_id,
            "actor_id": self.actor_id,
            # The table constrains status to the three harness values.
            "status": (
                "acted"
                if self.status is RunStatus.ACTED
                else "exception"
                if self.status is RunStatus.EXCEPTION
                else "proposed"
            ),
            "advisor": self.advisor,
            "model": self.model,
            "proposal_count": len(self.proposals),
            "action_count": len(self.actions_taken),
            "exception_count": len(self.exceptions),
            "tool_calls": list(self.tool_calls),
            "finished_at": utc_now().isoformat(),
        }


def make_run_id(onboarding_id: str, as_of: date, sequence: int) -> str:
    """Deterministic run id, so a replayed run is recognisable as the same run."""
    return f"RUN-{onboarding_id}-{as_of.isoformat()}-{sequence:03d}"


class UC02Agent:
    """Deterministic agent over the UC-02 service.

    Runs with no model at all (``OfflineAdvisor``), with the LiteLLM gateway,
    or with any object satisfying :class:`Advisor`. The surrounding workflow
    is identical in every case, which is what makes the model swappable and
    the behaviour testable.
    """

    def __init__(
        self,
        service: OnboardingService,
        actor: Actor,
        *,
        advisor: Advisor | None = None,
        store: OnboardingStore | None = None,
    ) -> None:
        self.service = service
        self.actor = actor
        self.advisor = advisor or OfflineAdvisor()
        self.store = store
        self.tools = UC02Tools(service, actor)
        self._sequence = 0

    # -- the loop ----------------------------------------------------------

    def run(self, onboarding_id: str, as_of: date) -> AgentRunResult:
        self._sequence += 1
        result = AgentRunResult(
            run_id=make_run_id(onboarding_id, as_of, self._sequence),
            onboarding_id=onboarding_id,
            client_id=self.actor.client_id,
            actor_id=self.actor.actor_id,
            as_of=as_of,
            status=RunStatus.OBSERVED,
            advisor=getattr(self.advisor, "name", "unknown"),
        )
        self._audit(result, "agent.started", "started")

        view_result = self._retrieve(result, onboarding_id, as_of)
        if view_result is None:
            return self._finish(result)
        view, risk = view_result

        advice = advise_with_fallback(self.advisor, view, risk)
        result.advice = advice
        result.degraded = advice.degraded
        result.degraded_reason = advice.degraded_reason
        if ":" in advice.source:
            result.model = advice.source.split(":", 1)[1]
        result.steps.append(
            AgentStep(
                name="advise",
                ok=not advice.degraded,
                detail=f"{advice.recommended_action} (source={advice.source})",
            )
        )
        if advice.degraded:
            self._audit(
                result, "agent.advice_degraded", "degraded",
                reason=advice.degraded_reason[:200],
            )

        self._decide_and_act(result, advice, onboarding_id, as_of)
        return self._finish(result)

    # -- steps -------------------------------------------------------------

    def _retrieve(self, result: AgentRunResult, onboarding_id: str, as_of: date):
        """Fetch the checklist and the risk assessment, or fail the run."""
        checklist = self.tools.get_checklist(onboarding_id, as_of)
        result.steps.append(
            AgentStep(
                name="retrieve",
                ok=checklist.ok,
                tool=checklist.tool,
                detail=checklist.error or "checklist read",
            )
        )
        if checklist.failed:
            result.exceptions.append(f"{checklist.error_code}: {checklist.error}")
            result.status = RunStatus.EXCEPTION
            return None

        risk = self.tools.assess_risk(onboarding_id, as_of)
        result.steps.append(
            AgentStep(
                name="analyse",
                ok=risk.ok,
                tool=risk.tool,
                detail=risk.error or "risk assessed",
            )
        )
        if risk.failed:
            result.exceptions.append(f"{risk.error_code}: {risk.error}")
            result.status = RunStatus.EXCEPTION
            return None
        return checklist.data, risk.data

    def _decide_and_act(
        self,
        result: AgentRunResult,
        advice: AgentAdvice,
        onboarding_id: str,
        as_of: date,
    ) -> None:
        """Map advice onto a permitted tool call, or onto a human proposal."""
        action = advice.recommended_action

        if action in RESTRICTED_ACTIONS:
            result.proposals.append(
                AgentProposal(
                    action=action,
                    subject_id=advice.target_task_id or onboarding_id,
                    rationale=advice.rationale,
                    requires_approval_by="hr_ops",
                )
            )
            result.status = RunStatus.PROPOSED
            result.steps.append(
                AgentStep(
                    name="decide",
                    detail=f"{action} is restricted; raised for human approval",
                )
            )
            self._audit(
                result, "agent.proposed", "proposed", recommended=action
            )
            return

        if action not in AUTONOMOUS_ACTIONS and action != "progress_ready_task":
            result.exceptions.append(f"unsupported recommended action: {action}")
            result.status = RunStatus.EXCEPTION
            return

        if action == "no_action_required":
            result.status = RunStatus.OBSERVED
            result.steps.append(AgentStep(name="decide", detail="nothing to do"))
            return

        if action == "inspect_blockers":
            call = self.tools.inspect_blockers(onboarding_id, as_of)
            self._record_read(result, call, f"{len(call.data or [])} outstanding")
            return

        if action == "prepare_followup":
            call = self.tools.prepare_followup(onboarding_id, as_of)
            if call.ok:
                drafts = call.data["drafts"]
                escalations = call.data["escalations"]
                # Drafts are the terminal output. Nothing is sent by UC-02.
                self._record_read(
                    result,
                    call,
                    f"{len(drafts)} draft(s) prepared (not sent), "
                    f"{len(escalations)} escalation(s)",
                )
            else:
                self._record_read(result, call, call.error)
            return

        # progress_ready_task: a real write, fully checked by the service.
        self._progress_task(result, advice, onboarding_id)

    def _record_read(self, result: AgentRunResult, call, detail: str) -> None:
        result.tool_calls.append(call.tool)
        result.steps.append(
            AgentStep(name="execute", ok=call.ok, tool=call.tool, detail=detail)
        )
        if call.failed:
            result.exceptions.append(f"{call.error_code}: {call.error}")
            result.status = RunStatus.EXCEPTION
        else:
            result.status = RunStatus.OBSERVED

    def _progress_task(
        self, result: AgentRunResult, advice: AgentAdvice, onboarding_id: str
    ) -> None:
        """Start a READY task, then prove it actually started."""
        task_id = advice.target_task_id
        if task_id is None:
            result.exceptions.append("progress_ready_task without a target task")
            result.status = RunStatus.EXCEPTION
            return

        call = self.tools.update_task_state(onboarding_id, task_id, action="start")
        result.tool_calls.append(call.tool)
        result.steps.append(
            AgentStep(
                name="execute",
                ok=call.ok,
                tool=call.tool,
                detail=call.error or f"started {task_id}",
            )
        )
        if call.failed:
            # The service refused — unauthorised owner, unmet prerequisite,
            # inactive onboarding. That is the correct outcome, reported as is.
            result.exceptions.append(f"{call.error_code}: {call.error}")
            result.status = RunStatus.EXCEPTION
            self._audit(result, "agent.action_denied", "denied", tool=call.tool)
            return

        verification = self.tools.verify_task_state(
            onboarding_id, task_id, "in_progress"
        )
        result.tool_calls.append(verification.tool)
        matched = bool(verification.ok and verification.data["matches_expected"])
        result.verified = matched
        result.steps.append(
            AgentStep(
                name="verify",
                ok=matched,
                tool=verification.tool,
                detail=(
                    "stored status matches expected"
                    if matched
                    else f"verification failed: {verification.data if verification.ok else verification.error}"
                ),
            )
        )
        if not matched:
            # The write appeared to succeed but the record disagrees. Never
            # report that as an action taken.
            result.exceptions.append(
                "post-write verification failed; the stored state does not match"
            )
            result.status = RunStatus.EXCEPTION
            self._audit(result, "agent.verification_failed", "failed", task_id=task_id)
            return

        result.actions_taken.append(f"{uc02_tools.UPDATE_TASK_STATE}:start:{task_id}")
        result.status = RunStatus.ACTED
        self._audit(result, "agent.acted", "acted", task_id=task_id)

    # -- finish ------------------------------------------------------------

    def _finish(self, result: AgentRunResult) -> AgentRunResult:
        self._audit(
            result,
            "agent.finished",
            result.status.value,
            proposals=len(result.proposals),
            exceptions=len(result.exceptions),
        )
        if self.store is not None:
            self.store.save_agent_run(result.to_row())
        return result

    def _audit(
        self, result: AgentRunResult, action: str, outcome: str, **detail: object
    ) -> None:
        """Append one audit entry. ``detail`` carries ids, counts and outcomes only."""
        self.service.audit.record(
            self.actor,
            action,
            outcome,
            onboarding_id=result.onboarding_id,
            run_id=result.run_id,
            **detail,
        )


__all__ = [
    "AGENT_ID",
    "AGENT_VERSION",
    "AUTONOMOUS_ACTIONS",
    "RESTRICTED_ACTIONS",
    "AgentProposal",
    "AgentRunResult",
    "AgentStep",
    "RunStatus",
    "UC02Agent",
    "make_run_id",
]
