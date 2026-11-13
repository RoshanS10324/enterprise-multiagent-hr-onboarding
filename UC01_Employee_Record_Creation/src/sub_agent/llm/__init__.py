"""Advisory LLM support for UC-01.

UC-01 makes no decision with a model. An LLM may escalate to a human or
draft text for one to read -- nothing else. See `base.py` for the three
rules every client holds to, and `brain.py` for where the prompts live.

Two clients implement the same `LlmClient` protocol:

- `FakeLlm` -- deterministic, calls nothing (LLM_MODE=offline, the default);
- `GatewayLlm` -- the central hr_onboarding_common gateway (LLM_MODE=online,
  or an injected gateway). `build_uc01_llm` picks between them.

Both are attached only when UC01_LLM_ENABLED is set.
"""

from .base import (
    LlmAuthority,
    LlmClient,
    LlmError,
    LlmInputRefused,
    LlmModelRefused,
    LlmRequest,
    LlmResponse,
    LlmTaskSpec,
    LlmUnavailable,
    audit_action_for,
    draft_fingerprint,
)
from .brain import brain_path, cached_tasks, load_all_tasks, load_persona, load_task
from .fake import MODEL_NAME, FakeLlm
from .gateway_client import GATEWAY_AVAILABLE, GatewayLlm, build_uc01_llm, llm_mode

__all__ = [
    "GATEWAY_AVAILABLE",
    "MODEL_NAME",
    "FakeLlm",
    "GatewayLlm",
    "LlmAuthority",
    "LlmClient",
    "LlmError",
    "LlmInputRefused",
    "LlmModelRefused",
    "LlmRequest",
    "LlmResponse",
    "LlmTaskSpec",
    "LlmUnavailable",
    "audit_action_for",
    "brain_path",
    "build_uc01_llm",
    "draft_fingerprint",
    "cached_tasks",
    "load_all_tasks",
    "load_persona",
    "llm_mode",
    "load_task",
]
