"""Agent harness: the onboarding agent subclasses BaseAgent.

BaseAgent owns what must be identical across runs: policy loading, the kill switch, the
policy-checked ToolGateway, provenance, audit, tracing, and turning failures into contract
exceptions (never silent). Agent logic itself is a LangGraph graph built by the subclass.

Forked from hr_recruitment_common.agent, trimmed per docs/ONBOARDING_COMMON_ARCHITECTURE.md A.3:
`AgentContext.brain`/`.skills`/`.ats` and `RunContext.llm()`/`.brain_search()` are dropped (zero
call sites in onboarding; `ctx.llm.structured()` is called directly by services instead). Also
carries the finding S1/S4 fix: `submit_for_review` now requires `client_id` and stamps it onto
every `ApprovalItem` it creates, since `ApprovalItem.client_id` is required in the forked `hitl.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel

from hr_onboarding_common.config import Settings, get_settings
from hr_onboarding_common.contracts import (ActionTaken, AgentException, AgentRequest, AgentResponse, BrainFeedback,
                                            ExceptionCode, PolicyRef, Proposal, Provenance, ResponseStatus, now_utc)
from hr_onboarding_common.llm import LLMClient, LLMError
from hr_onboarding_common.observability.tracing import span
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import ApprovalItem, HITLInbox
from hr_onboarding_common.platform.policy import Policy, PolicyStore, PolicyViolation
from hr_onboarding_common.platform.tools import ToolGateway

InputT = TypeVar("InputT", bound=BaseModel)
T = TypeVar("T", bound=BaseModel)


@dataclass
class AgentContext:
    settings: Settings
    llm: LLMClient
    policies: PolicyStore
    audit: AuditLog
    hitl: HITLInbox
    connector: Any = None  # the onboarding agent's own connector bundle (OnboardingPort, etc.)

    @classmethod
    def build(cls, *, agent_home: Path, llm: LLMClient, connector: Any = None,
              settings: Settings | None = None, persist: bool = False) -> "AgentContext":
        s = settings or get_settings(agent_home)
        audit = AuditLog(s.data_dir / "audit.jsonl" if persist else None)
        return cls(
            settings=s, llm=llm, policies=PolicyStore(s.policy_dir), audit=audit,
            hitl=HITLInbox(s.data_dir / "hitl.db" if persist else ":memory:", audit),
            connector=connector,
        )


class RunContext:
    """Per-run helpers handed to graph nodes via `config["configurable"]["rc"]`. Collects provenance."""

    def __init__(self, agent: "BaseAgent", request: AgentRequest, policy: Policy, prov: Provenance):
        self.agent, self.request, self.policy, self.prov = agent, request, policy, prov
        ctx = agent.ctx
        self.ctx = ctx
        self.tools = ToolGateway(policy, ctx.audit, ctx.hitl, request.actor)
        self.proposals: list[Proposal] = []
        self.exceptions: list[AgentException] = []
        self.actions: list[ActionTaken] = []
        self.feedback: list[BrainFeedback] = []

    # ---- outputs
    def propose(self, *, type: str, gate: str, payload: dict[str, Any], rationale: str, confidence: float,
                subject_id: str | None = None) -> Proposal:
        gate_def = self.policy.gate(gate)  # raises if the gate is not in the envelope
        p = Proposal(type=type, subject_id=subject_id, payload=payload, rationale=rationale,
                     confidence=round(max(0.0, min(1.0, confidence)), 3), gate=gate,
                     requires_approval_by=gate_def.approver_role)
        self.proposals.append(p)
        return p

    def exception(self, code: ExceptionCode, detail: str, subject_id: str | None = None) -> None:
        self.exceptions.append(AgentException(code=code, detail=detail, subject_id=subject_id))
        self.ctx.audit.record("agent.exception", agent=self.agent.agent_id, subject_id=subject_id,
                              code=code, detail=detail)

    def acted(self, tool: str, *, subject_id: str | None = None, approval_id: str | None = None,
              **detail: Any) -> None:
        self.actions.append(ActionTaken(tool=tool, subject_id=subject_id, approval_id=approval_id, detail=detail))

    def brain_feedback(self, type: str, subject_id: str | None = None, **data: Any) -> None:
        self.feedback.append(BrainFeedback(type=type, subject_id=subject_id, data=data))


def run_context(config: dict) -> RunContext:
    """Fetch the RunContext inside a LangGraph node."""
    return config["configurable"]["rc"]


class BaseAgent(Generic[InputT]):
    agent_id: ClassVar[str]
    version: ClassVar[str] = "0.1.0"
    input_model: ClassVar[type[BaseModel]]
    preamble: ClassVar[str] = ""

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx
        self.graph = self.build_graph()

    @property
    def policy(self) -> Policy:
        return self.ctx.policies.for_agent(self.agent_id)

    def build_graph(self):  # pragma: no cover - abstract
        """Return a compiled LangGraph graph. Nodes get the RunContext via `run_context(config)`."""
        raise NotImplementedError

    def initial_state(self, inp: InputT) -> dict[str, Any]:
        return {"input": inp}

    def run(self, request: AgentRequest[InputT]) -> AgentResponse:
        policy = self.policy
        request.policy = PolicyRef(ref=policy.policy, version=policy.version, autonomy=policy.autonomy,
                                   gates=list(policy.gates))
        prov = Provenance(agent=self.agent_id, agent_version=self.version,
                          policy=f"{policy.policy}@v{policy.version}")
        rc = RunContext(self, request, policy, prov)
        audit = self.ctx.audit
        with span("agent.run", **{"agent.id": self.agent_id, "agent.version": self.version,
                                  "run.id": request.run_id, "workflow.instance": request.workflow.instance}) as sp:
            audit.record("agent.started", actor=request.actor.on_behalf_of, agent=self.agent_id,
                         client_id=request.actor.client_id, run_id=request.run_id,
                         workflow=request.workflow.model_dump())
            if not policy.enabled:
                rc.exception(ExceptionCode.AGENT_DISABLED, f"{self.agent_id} is disabled by its policy (kill switch)")
            else:
                try:
                    inp = request.input if isinstance(request.input, self.input_model) else \
                        self.input_model.model_validate(request.input)
                    self.graph.invoke(self.initial_state(inp), config={"configurable": {"rc": rc}})
                except PolicyViolation as e:
                    rc.exception(ExceptionCode.POLICY_BREACH, str(e))
                except LLMError as e:
                    rc.exception(ExceptionCode.INTERNAL_ERROR, f"LLM failure: {e}")
                except KeyError as e:
                    rc.exception(ExceptionCode.MISSING_DATA, f"not found: {e}")

            prov.tool_calls = list(rc.tools.calls)
            prov.finished_at = now_utc()
            status = (ResponseStatus.PROPOSED if rc.proposals else
                      ResponseStatus.ACTED if rc.actions else ResponseStatus.EXCEPTION)
            sp.set_attribute("agent.status", str(status))
            sp.set_attribute("agent.proposals", len(rc.proposals))
            sp.set_attribute("agent.exceptions", len(rc.exceptions))
        response = AgentResponse(run_id=request.run_id, status=status, proposals=rc.proposals,
                                 actions_taken=rc.actions, exceptions=rc.exceptions,
                                 brain_feedback=rc.feedback, provenance=prov)
        audit.record("agent.finished", agent=self.agent_id, run_id=request.run_id, status=status,
                     proposals=len(rc.proposals), exceptions=len(rc.exceptions))
        return response

    def submit_for_review(self, response: AgentResponse, *, workflow: str, instance: str,
                          client_id: str) -> list[ApprovalItem]:
        """Turn the response's proposals into HITL inbox items (one per proposal).

        `client_id` is required (finding S1): every ApprovalItem must carry the tenant it belongs
        to so it can never be listed, read, or decided by a different tenant's actor. Callers (the
        API layer) must pass the verified actor's client_id, not a value taken from the request body.
        """
        items = []
        for p in response.proposals:
            gate = self.policy.gate(p.gate)
            items.append(self.ctx.hitl.submit(ApprovalItem(
                gate=p.gate, approver_role=gate.approver_role, sla_hours=gate.sla_hours, workflow=workflow,
                workflow_instance=instance, agent=self.agent_id, proposal_type=p.type, client_id=client_id,
                subject_id=p.subject_id, payload=p.payload, rationale=p.rationale, confidence=p.confidence,
                run_id=response.run_id, requested_by=p.payload.get("requested_by"),
                separation_of_duties=gate.separation_of_duties)))
        return items


def _lazy_orchestrator_agent():
    """`OnboardingOrchestratorAgent` lives in `orchestration/` (see that package's docstring for
    why) but is re-exported here so this module remains the documented public entry point.
    Imported lazily so plain `from hr_onboarding_common.agent import BaseAgent` (every other
    agent's import) never pulls in langgraph or any UC-specific plumbing."""
    from hr_onboarding_common.orchestration.orchestrator_agent import OnboardingOrchestratorAgent
    return OnboardingOrchestratorAgent


def __getattr__(name: str):
    if name == "OnboardingOrchestratorAgent":
        return _lazy_orchestrator_agent()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
