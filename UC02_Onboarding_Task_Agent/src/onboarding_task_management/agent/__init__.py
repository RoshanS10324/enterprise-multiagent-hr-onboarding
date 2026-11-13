"""UC-02 agentic layer — one package, no second copy.

Core modules, in dependency order:

``tools``
    The allowlisted operations an agent may perform, each a thin wrapper over
    :class:`~onboarding_task_management.sub_agent.service.OnboardingService`. Names here
    must match ``guardrails/policy.yaml``.

``advisor``
    The LLM seam. :class:`~onboarding_task_management.agent.advisor.OfflineAdvisor`
    is deterministic; :class:`~onboarding_task_management.agent.advisor.LiteLLMAdvisor`
    calls the central ``hr_onboarding_common.llm`` gateway through an injected
    ``LLMClient`` (ADR-005). Model output is validated against the record before it can influence anything.

``loop``
    The fixed run sequence: retrieve, analyse, advise, decide, execute,
    verify, persist.

``fake_llm``
    The one deterministic fake LLM. No network, no key, no provider SDK.

``harness``
    Optional binding to the shared ``hr_recruitment_common`` harness.

The working process it drives — checklist generation, planning, tracking,
gates, message generation and sending — lives in the sibling package
``onboarding_task_management.sub_agent``, together with the HTTP adapter and
API over it. This package holds only the agentic layer: it reaches the rules
through ``sub_agent.service`` and an allowlist, and never reimplements one.

The scenario runner lives **outside** the installable package, at
``evaluation/`` in the repository root: it is a verification harness, not
shipped library code.

Importing this package pulls in **no** optional dependency. The central LLM
contract (``hr_onboarding_common.llm``) is imported when present and replaced
by local stand-ins when not; LiteLLM itself is loaded only by the gateway on
its first call. The shared harness (``hr_recruitment_common``, LangGraph) and
``fastapi`` are imported lazily, and only by the code paths that need them, so
UC-02 keeps running on pydantic alone.
"""

from onboarding_task_management.agent.advisor import (
    Advisor,
    AgentAdvice,
    OfflineAdvisor,
    build_advisor,
)
from onboarding_task_management.agent.loop import (
    AGENT_ID,
    AGENT_VERSION,
    AgentRunResult,
    RunStatus,
    UC02Agent,
)
from onboarding_task_management.agent.tools import ALL_TOOLS, ToolResult, UC02Tools

__all__ = [
    "AGENT_ID",
    "AGENT_VERSION",
    "ALL_TOOLS",
    "Advisor",
    "AgentAdvice",
    "AgentRunResult",
    "OfflineAdvisor",
    "RunStatus",
    "ToolResult",
    "UC02Agent",
    "UC02Tools",
    "build_advisor",
]
