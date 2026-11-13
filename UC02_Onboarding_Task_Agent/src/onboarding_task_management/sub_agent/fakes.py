"""Deterministic fakes for the UC-02 sub-agent: LLM and Outlook.

This module **re-exports** the recovered implementations rather than
reimplementing them. There is one fake LLM in this repository
(``onboarding_task_management.agent.fake_llm``) and one mock Outlook
connector (``onboarding_task_management.sub_agent.connectors.outlook.mock``); a second copy would drift from
the first and both would claim to be authoritative.

What the fake LLM guarantees
----------------------------

* **No network, no API key, no provider SDK.** ``FakeLLMClient.structured``
  is pure Python over the prompt payload.
* **Stable output for identical input.** Scenario selection is a total
  function of the ``<input>`` JSON block, and the responses are templates
  filled from that payload.
* **A real contract.** It satisfies the same ``LLMClient`` shape as
  ``LiteLLMGateway`` (``model_for`` + ``structured``) and is driven through
  the same ``LiteLLMAdvisor``, so prompt assembly and schema validation are
  genuinely exercised.
* **It cannot overrule the business rules.** Advice passes through
  ``validate_advice`` before the agent acts; an unsupported recommendation is
  replaced by the deterministic one and the run is marked ``degraded``.
* **Malformed input is refused, not guessed.** A prompt with no ``<input>``
  block raises ``FakeLLMError`` instead of inventing an answer, and
  ``behaviour="invalid"`` reproduces schema-invalid model output.

Every response is tagged ``source="fake_llm:fake/uc02-advisor-v1"``, so no
stored run can be mistaken for a real model call.
"""

from __future__ import annotations

from typing import Any

from onboarding_task_management.agent.advisor import (
    Advisor,
    RECOMMENDABLE_ACTIONS,
    AgentAdvice,
    OfflineAdvisor,
)
from onboarding_task_management.agent.fake_llm import (
    FAKE_MODEL,
    FakeCall,
    FakeLLMAdvisor,
    FakeLLMClient,
    FakeLLMError,
    build_fake_advisor,
    classify,
    extract_input_block,
    respond,
)
from onboarding_task_management.sub_agent.connectors.outlook.mock import (
    MOCK_ID_PREFIX,
    MockOutlookConnector,
    message_from_draft,
)

#: Behaviours the fake LLM can be put into, for failure-path tests.
FAKE_BEHAVIOURS = ("scripted", "error", "invalid", "hallucinate")


def make_fake_llm(behaviour: str = "scripted") -> Advisor:
    """The deterministic fake LLM advisor.

    ``behaviour`` must be one of :data:`FAKE_BEHAVIOURS` or ``"offline"``.
    ``"offline"`` routes directly to :class:`OfflineAdvisor`.
    """
    if behaviour == "offline":
        return OfflineAdvisor()
    if behaviour not in FAKE_BEHAVIOURS:
        raise ValueError(
            f"unknown fake LLM behaviour {behaviour!r}; expected one of {FAKE_BEHAVIOURS} or 'offline'"
        )
    return build_fake_advisor(behaviour)


def make_mock_outlook(**kwargs: Any) -> MockOutlookConnector:
    """The mock Outlook connector. Sends nothing, ever."""
    return MockOutlookConnector(**kwargs)


def fake_llm_fingerprint() -> dict[str, Any]:
    """What the fake advertises about itself. Used by ``/health``."""
    return {
        "provider": "fake",
        "model": FAKE_MODEL,
        "real_api_calls": False,
        "requires_api_key": False,
        "deterministic": True,
        "behaviours": list(FAKE_BEHAVIOURS),
        "allowed_actions": list(RECOMMENDABLE_ACTIONS),
    }


__all__ = [
    "FAKE_BEHAVIOURS",
    "FAKE_MODEL",
    "MOCK_ID_PREFIX",
    "RECOMMENDABLE_ACTIONS",
    "AgentAdvice",
    "FakeCall",
    "FakeLLMAdvisor",
    "FakeLLMClient",
    "FakeLLMError",
    "MockOutlookConnector",
    "OfflineAdvisor",
    "build_fake_advisor",
    "classify",
    "extract_input_block",
    "fake_llm_fingerprint",
    "make_fake_llm",
    "make_mock_outlook",
    "message_from_draft",
    "respond",
]
