"""A deterministic offline `LLMClient` for UC-04's one gateway task, in the same way UC05 supplies `sub_agent.fakes.offline_llm`.

It is the shared package's `FakeLLM` with one handler for `uc04.advisory`. It writes a short sentence from the facts it is given and nothing
else, so tests and local runs exercise the real `GatewayModelPort` path with no network and no key. Pass it as `build_llm(settings, offline=offline_llm)`.
"""

from __future__ import annotations

from typing import Any

from .model_port import TASK


def offline_llm(settings: Any = None) -> Any:
    from hr_onboarding_common.llm import FakeLLM, extract_input_block

    def advisory(system: str, user: str, schema: Any) -> Any:
        facts = extract_input_block(user).get("facts", {})
        status = facts.get("overall_status", "unknown")
        blockers = facts.get("blocker_codes", "none")
        return schema(narrative=f"The deterministic check reports {status}. Blocker codes: {blockers}. HR decides the next step.")

    return FakeLLM(settings).on(TASK, advisory)
