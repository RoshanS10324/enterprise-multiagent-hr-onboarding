"""Choose UC-04's advisor from the shared `LLM_MODE`, the way UC05 chooses its LLM (`sub_agent.runtime.build_context` on the `Prasanna` branch).

  LLM_MODE=offline (default)  `FakeLlmAdvisor`: deterministic, no network, no key, no gateway needed.
  LLM_MODE=online             `LlmAdvisor` over `GatewayModelPort(build_llm(...))`: the shared `LiteLLMGateway`, after the shared configuration
                              (key, tier models, reasoning settings) has been validated. A bad configuration stops here with a message that
                              names the variables to fix and never their values. There is no silent fall back to the offline advisor.

Nothing here reads a provider credential: `build_llm` and the gateway do, from the one shared `.env` (`HR_ONBOARDING_ENV_FILE`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from sub_agent.advisor import FakeLlmAdvisor
from sub_agent.advisory_providers import AdvisorPort, build_advisor

from .model_port import GatewayModelPort


class GatewayConfigurationError(RuntimeError):
    """The online gateway configuration cannot work. The message comes from the shared package and names variables, never their values."""


def advisor_for_client(llm: Any, *, fallback_to_template: bool = False, timeout_s: float = 20.0) -> AdvisorPort:
    """An advisor over a shared `LLMClient` the caller already has (for example the orchestrator's `AgentContext.llm`).

    `fallback_to_template=True` is an explicit choice: when the model fails, the report is the template one and says so
    (`provider="fake-llm"`, `fallback_from="litellm"`, `degraded=True`). Without it the report still carries the deterministic decision
    fields and names the failure, but no template text is substituted.
    """
    return build_advisor("litellm", model=GatewayModelPort(llm), timeout_s=timeout_s, fallback_to_template=fallback_to_template)


def build_gateway_advisor(environ: Mapping[str, str] | None = None, *, fallback_to_template: bool = False, timeout_s: float = 20.0) -> AdvisorPort:
    """The advisor this process should use: offline by default, the shared gateway when `LLM_MODE=online`.

    With `environ=None` the shared `.env` is loaded first (without overriding real environment variables), as UC05's API does. Passing an
    explicit mapping (tests) skips that and reads only the mapping.
    """
    try:  # lazy: UC-04 itself needs no shared package; only the gateway wiring does
        from hr_onboarding_common.config import get_settings, load_environment
        from hr_onboarding_common.llm import ONLINE, LLMConfigurationError, build_llm, llm_mode
    except ImportError:
        raise GatewayConfigurationError(
            "the shared gateway factory (hr_onboarding_common.llm.build_llm, with load_environment and llm_mode) is not available: install a "
            "hr-onboarding-common that has it (the Prasanna branch's), or use the default offline advisor") from None

    if environ is None:
        load_environment()
    env = os.environ if environ is None else environ
    try:
        if llm_mode(env) != ONLINE:
            return FakeLlmAdvisor()
        llm = build_llm(get_settings(), environ=env)
    except LLMConfigurationError as exc:
        raise GatewayConfigurationError(str(exc)) from None
    return advisor_for_client(llm, fallback_to_template=fallback_to_template, timeout_s=timeout_s)
