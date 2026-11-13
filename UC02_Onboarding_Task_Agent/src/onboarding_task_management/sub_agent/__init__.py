"""UC-02 sub-agent — the onboarding working process.

Everything the use case actually *does* lives here: deciding which tasks
apply, planning them, tracking them, gating them, composing the messages and
handing those messages to a connector.

Checklist generation
    ``checklist``   client task-template configuration: keys, bilingual
                    titles, dependencies, cycle detection.
    ``selection``   which templates apply to this hire, and why. Produces the
                    explained checklist, including what needs human review.
    ``planner``     turns the selected templates into dated tasks: dependency
                    resolution, parallel batches, UAE business-day due dates,
                    re-planning.
    ``milestones``  milestone state derived from the plan. Never stored.

Progress and authorisation
    ``tracker``     status transitions, owner authorisation, overdue and
                    joining-date risk.
    ``approvals``   approval gates and the gate-to-operation binding. An
                    approval is valid only for its own gate's operation.

Message generation and sending
    ``reminders``   bilingual reminder drafts. **No transport of any kind**;
                    every draft carries ``delivery_status="draft_not_sent"``.
    ``connectors.outlook``
                    the only delivery port, and its MOCK adapter. Simulated
                    delivery still requires an approval id, mirroring the real
                    gate. No mail server is ever contacted.

Orchestration and entry points
    ``service``     ``OnboardingService`` — every public method takes an
                    ``Actor`` and enforces per-client isolation and audit.
    ``adapter``     binds a request, the LLM advisor, the service and the mock
                    connector into one workflow run.
    ``api``         FastAPI surface over the adapter. **Not imported here**, so
                    ``fastapi`` stays an optional extra.
    ``fakes``       constructors over the deterministic advisor and the mock
                    connector. Re-exports only.

Shared primitives stay in the parent package: ``contracts``, ``errors``,
``config``, ``calendar_uae``, ``audit``, ``persistence``. The agentic layer
(``agent.loop``, ``agent.tools``, ``agent.advisor``) sits alongside and drives
``service`` through an allowlist; it is not imported here.
"""

# Re-exports are resolved lazily (PEP 562). An eager import here would make
# `import onboarding_task_management.sub_agent.checklist` pull in `adapter`,
# which imports `agent.loop` and `synthetic`, which import back into this
# package -- a genuine import cycle. Lazy lookup keeps the rules modules
# importable on their own while preserving the convenience names.

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from onboarding_task_management.sub_agent.adapter import (
        DEFAULT_AS_OF,
        UC02SubAgent,
        WorkflowError,
        WorkflowRequest,
        WorkflowResult,
        available_fixtures,
        load_fixture,
    )
    from onboarding_task_management.sub_agent.service import (
        OnboardingService,
        OnboardingTrigger,
    )

#: Exported name -> module it actually lives in.
_LAZY: dict[str, str] = {
    "DEFAULT_AS_OF": "adapter",
    "UC02SubAgent": "adapter",
    "WorkflowError": "adapter",
    "WorkflowRequest": "adapter",
    "WorkflowResult": "adapter",
    "available_fixtures": "adapter",
    "load_fixture": "adapter",
    "OnboardingService": "service",
    "OnboardingTrigger": "service",
}


def __getattr__(name: str) -> Any:
    try:
        module = _LAZY[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None
    from importlib import import_module

    return getattr(import_module(f"{__name__}.{module}"), name)


def __dir__() -> list[str]:
    return sorted(_LAZY)


# A literal, so tooling can verify it. Kept in step with _LAZY by the
# assertion below, which fails loudly if the two ever drift.
__all__ = [
    "DEFAULT_AS_OF",
    "OnboardingService",
    "OnboardingTrigger",
    "UC02SubAgent",
    "WorkflowError",
    "WorkflowRequest",
    "WorkflowResult",
    "available_fixtures",
    "load_fixture",
]

assert sorted(__all__) == sorted(_LAZY), "__all__ and _LAZY have drifted"
