"""Optional binding to the shared Enterprise HR Agent harness (``hr-recruitment-common``).

ADR-005 makes the shared harness the approved way to run an agent: policy
loading, the kill switch, the policy-checked ``ToolGateway``, HITL approvals,
audit, tracing and the LangGraph pipeline all live there, so they cannot drift
between use cases.

This module is **optional by construction**. Every harness import happens
inside a function, so:

* ``import onboarding_task_management.agent`` works with pydantic alone;
* the 484 pre-existing tests and both demos run unchanged;
* :func:`harness_available` reports the truth rather than a guess.

What the binding adds over :class:`~onboarding_task_management.agent.loop.UC02Agent`
----------------------------------------------------------------------------------

``UC02Agent`` already enforces UC-02's own rules. The harness adds the
*platform* rules on top:

``ToolGateway``
    Refuses any tool not on ``guardrails/policy.yaml``'s allowlist, refuses
    every write tool at autonomy L0, and refuses a gated write without an
    approved HITL item. This is what makes the ``tools: enforced`` claim in
    the policy envelope true.

``LangGraph``
    The run sequence is expressed as a ``StateGraph``, which is the shape the
    other Enterprise HR Agent agents use.

Honest limits
-------------
* No ``ATSPort`` adapter is wired: UC-02 consumes an onboarding trigger from
  UC-01, not an ATS requisition.
* ``Brain``/``SkillLibrary`` are loaded from the harness defaults; UC-02 ships
  no skills or knowledge content of its own.
* With no model credential the advisor is the deterministic one, and the run
  records ``advisor=offline``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, TypedDict

from onboarding_task_management.agent.loop import (
    AGENT_ID,
    AgentRunResult,
    RunStatus,
    UC02Agent,
)
from onboarding_task_management.agent.tools import ALL_TOOLS, READ_TOOLS, WRITE_TOOLS
from onboarding_task_management.config import AGENT_HOME, POLICY_PATH


def harness_available() -> bool:
    """Whether ``hr_recruitment_common`` can actually be imported right now."""
    try:
        import hr_recruitment_common  # noqa: F401
    except ImportError:
        return False
    return True


def litellm_available() -> bool:
    """Whether the LiteLLM package is importable. Says nothing about credentials."""
    try:
        import litellm  # noqa: F401
    except ImportError:
        return False
    return True


def load_policy() -> Any:
    """Load ``guardrails/policy.yaml`` through the harness ``PolicyStore``.

    The UC-02 envelope validates against the harness ``Policy`` model with no
    changes, which is the point: the envelope was written to the platform
    contract, not to a local one.
    """
    from hr_recruitment_common.platform.policy import PolicyStore

    return PolicyStore(POLICY_PATH.parent).for_agent(AGENT_ID)


def assert_tools_allowlisted(policy: Any | None = None) -> list[str]:
    """Check every UC-02 tool against the envelope. Returns their modes.

    Raises ``PolicyViolation`` on the first tool the envelope does not permit,
    which is exactly what would happen at call time — surfaced at start-up
    instead.
    """
    policy = policy or load_policy()
    return [policy.tool_mode(tool) for tool in ALL_TOOLS]


class GatedToolRunner:
    """Routes UC-02 tool calls through the harness ``ToolGateway``.

    The gateway owns the allowlist, the autonomy check and the approval check;
    this class owns nothing but the wiring. A tool that the envelope does not
    name raises before the underlying service is ever reached.
    """

    def __init__(self, policy: Any, audit: Any, hitl: Any, actor: Any) -> None:
        from hr_recruitment_common.platform.tools import ToolGateway

        self.gateway = ToolGateway(policy, audit, hitl, actor)

    def call(self, tool: str, fn, *args, **kwargs):
        return self.gateway.call(tool, fn, *args, **kwargs)

    @property
    def calls(self) -> list[str]:
        return list(self.gateway.calls)


class GraphState(TypedDict, total=False):
    """LangGraph state. ``result`` is the run output the nodes fill in."""

    result: AgentRunResult
    onboarding_id: str
    as_of: date
    view: Any
    risk: Any
    advice: Any
    halted: bool


def build_graph(agent: UC02Agent):
    """Express the UC-02 run sequence as a LangGraph ``StateGraph``.

    The graph is linear and the nodes delegate to the same methods
    :class:`UC02Agent` already uses, so the graph and the plain loop cannot
    diverge in behaviour — only in how they are driven.
    """
    from langgraph.graph import END, START, StateGraph

    # Each node returns the whole state. With an untyped ``dict`` schema
    # LangGraph merges only the keys a node returns, so returning a partial
    # dict silently drops ``result`` and the run loses its own output object.

    def retrieve(state: GraphState) -> GraphState:
        outcome = agent._retrieve(
            state["result"], state["onboarding_id"], state["as_of"]
        )
        if outcome is None:
            return {**state, "halted": True}
        view, risk = outcome
        return {**state, "view": view, "risk": risk, "halted": False}

    def advise(state: GraphState) -> GraphState:
        if state.get("halted"):
            return state
        from onboarding_task_management.agent.advisor import advise_with_fallback

        result = state["result"]
        advice = advise_with_fallback(agent.advisor, state["view"], state["risk"])
        result.advice = advice
        result.degraded = advice.degraded
        result.degraded_reason = advice.degraded_reason
        if ":" in advice.source:
            result.model = advice.source.split(":", 1)[1]
        from onboarding_task_management.agent.loop import AgentStep

        result.steps.append(
            AgentStep(
                name="advise",
                ok=not advice.degraded,
                detail=f"{advice.recommended_action} (source={advice.source})",
            )
        )
        return {**state, "advice": advice}

    def act(state: GraphState) -> GraphState:
        if state.get("halted") or state.get("advice") is None:
            return state
        agent._decide_and_act(
            state["result"], state["advice"], state["onboarding_id"], state["as_of"]
        )
        return state

    graph = StateGraph(GraphState)
    graph.add_node("retrieve", retrieve)
    graph.add_node("advise", advise)
    graph.add_node("act", act)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "advise")
    graph.add_edge("advise", "act")
    graph.add_edge("act", END)
    return graph.compile()


class HarnessBoundAgent:
    """UC-02 agent run under the harness policy envelope and LangGraph.

    Construction fails fast if the harness is missing or if any UC-02 tool is
    not allowlisted, rather than discovering it mid-run.
    """

    agent_id = AGENT_ID

    def __init__(self, agent: UC02Agent) -> None:
        if not harness_available():
            raise RuntimeError(
                "hr_recruitment_common is not installed; use UC02Agent directly "
                "or install the shared harness (see README 'Shared harness')"
            )
        self.agent = agent
        self.policy = load_policy()
        assert_tools_allowlisted(self.policy)
        self.graph = build_graph(agent)

    @property
    def enabled(self) -> bool:
        """The per-agent kill switch from the policy envelope."""
        return bool(self.policy.enabled)

    def run(self, onboarding_id: str, as_of: date) -> AgentRunResult:
        from onboarding_task_management.contracts import utc_now  # noqa: F401

        agent = self.agent
        agent._sequence += 1
        from onboarding_task_management.agent.loop import make_run_id

        result = AgentRunResult(
            run_id=make_run_id(onboarding_id, as_of, agent._sequence),
            onboarding_id=onboarding_id,
            client_id=agent.actor.client_id,
            actor_id=agent.actor.actor_id,
            as_of=as_of,
            status=RunStatus.OBSERVED,
            advisor=getattr(agent.advisor, "name", "unknown"),
        )

        if not self.enabled:
            result.status = RunStatus.EXCEPTION
            result.exceptions.append(
                f"{AGENT_ID} is disabled by its policy envelope (kill switch)"
            )
            return agent._finish(result)

        self.graph.invoke(
            {
                "result": result,
                "onboarding_id": onboarding_id,
                "as_of": as_of,
                "halted": False,
                "advice": None,
            }
        )
        return agent._finish(result)


def integration_status() -> dict[str, Any]:
    """What is actually wired right now. Facts, not intentions.

    Used by the demo and the documentation so neither can claim an
    integration that this environment does not have.
    """
    from onboarding_task_management.agent.advisor import advisor_mode, llm_configured

    status: dict[str, Any] = {
        "harness_installed": harness_available(),
        "litellm_installed": litellm_available(),
        "llm_credential_configured": llm_configured(),
        # "fake" = deterministic scripted model, "offline" = rule-based
        # advisor, "litellm" = a real gateway call would be attempted.
        "advisor_mode": advisor_mode(),
        "real_llm_call_verified": False,
        "agent_home": str(AGENT_HOME),
        "policy_path": str(POLICY_PATH),
        "read_tools": list(READ_TOOLS),
        "write_tools": list(WRITE_TOOLS),
        "mcp_server": False,
    }
    if status["harness_installed"]:
        try:
            policy = load_policy()
            status["policy_loaded"] = True
            status["autonomy"] = str(policy.autonomy)
            status["ceiling"] = str(policy.ceiling)
            status["tool_modes"] = dict(
                zip(ALL_TOOLS, assert_tools_allowlisted(policy), strict=True)
            )
        except Exception as exc:  # noqa: BLE001 - report, never pretend
            status["policy_loaded"] = False
            status["policy_error"] = f"{type(exc).__name__}: {exc}"
    else:
        status["policy_loaded"] = False
    return status


__all__ = [
    "GatedToolRunner",
    "HarnessBoundAgent",
    "assert_tools_allowlisted",
    "build_graph",
    "harness_available",
    "integration_status",
    "litellm_available",
    "load_policy",
]
