"""UC-01's LlmClient over the central LLM gateway (hr_onboarding_common.llm).

UC-01 keeps its own contract (`LlmClient.complete(LlmRequest) -> LlmResponse`)
and its own guard; this module only changes WHERE a guarded prompt goes. The
gateway owns provider routing, model choice per tier, retries, fallbacks and
callbacks -- UC-01 imports no provider SDK, reads no provider key and names
no model.

Order of operations, and why:

1. `guard.build_prompt` runs FIRST. An undeclared, forbidden or secret input
   raises `LlmInputRefused` before the gateway is touched, so salary, ID
   numbers and contact details can never reach a model.
2. One `structured()` call with a fixed schema (`_Label` or `_Draft`).
3. The reply is validated here as well: a label outside the task's declared
   `output_values`, or an empty/oversized draft, is refused.
4. Every gateway failure becomes a UC-01 `LlmError` with a GENERIC message
   (the gateway's text can echo provider responses). Callers already treat
   `LlmError` as "take the deterministic path", so a model outage, timeout or
   refusal never blocks a case.

`hr_onboarding_common` is optional for UC-01: it runs as its own worker
process and stays runnable on its own dependencies. When the package is not
installed, `build_uc01_llm` refuses `LLM_MODE=online` with a clear error
rather than silently pretending to be online.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

from .base import (
    LlmAuthority,
    LlmClient,
    LlmError,
    LlmModelRefused,
    LlmRequest,
    LlmResponse,
    LlmTaskSpec,
    LlmUnavailable,
)
from .fake import FakeLlm
from .guard import build_prompt

try:
    from hr_onboarding_common.llm import LLMError as _GatewayError  # type: ignore[import-untyped]
    from hr_onboarding_common.llm import LLMRefusal as _GatewayRefusal  # type: ignore[import-untyped]
    from hr_onboarding_common.llm import ModelTier as _ModelTier  # type: ignore[import-untyped]

    GATEWAY_AVAILABLE = True
except ImportError:  # standalone UC-01 without the shared package
    GATEWAY_AVAILABLE = False
    _GatewayError = _GatewayRefusal = None  # type: ignore[assignment,misc]
    _ModelTier = None  # type: ignore[assignment]

#: Prefix of every gateway task name, so UC-01 calls are identifiable in the
#: gateway's logs, traces and FakeLLM handlers ("uc01.intake_intent", ...).
TASK_PREFIX = "uc01."
#: Longest draft accepted from a model. A draft is a short note for HR.
MAX_DRAFT_CHARS = 2000
#: Upper bound on completion tokens per call (drafts are short).
MAX_TOKENS = 800

OFFLINE, ONLINE = "offline", "online"


class _Label(BaseModel):
    """Reply schema for ADVISORY_ESCALATE_ONLY tasks."""

    label: str
    confidence: float = Field(ge=0.0, le=1.0)


class _Draft(BaseModel):
    """Reply schema for DRAFT_FOR_HUMAN tasks."""

    text: str
    confidence: float = Field(ge=0.0, le=1.0)


def gateway_task_name(task: str) -> str:
    return f"{TASK_PREFIX}{task}"


def _tier_for(spec: LlmTaskSpec) -> Any:
    # Drafting is WORKHORSE work; classifying one subject line is FAST.
    return _ModelTier.WORKHORSE if spec.authority == LlmAuthority.DRAFT_FOR_HUMAN else _ModelTier.FAST


class GatewayLlm(LlmClient):
    """UC-01's LlmClient over an injected `hr_onboarding_common.llm.LLMClient`.

    `gateway` is normally `LiteLLMGateway` (online) or a common `FakeLLM`
    (tests). The guard always runs before it is called.
    """

    def __init__(self, gateway: Any, tasks: Mapping[str, LlmTaskSpec], persona: str) -> None:
        if not GATEWAY_AVAILABLE:
            raise LlmError("hr_onboarding_common is not installed; the central gateway is unavailable")
        self._gateway = gateway
        self._tasks = dict(tasks)
        self._persona = persona
        # Task names only, never prompts or replies (mirrors FakeLlm.calls).
        self.calls: list[str] = []

    def complete(self, request: LlmRequest) -> LlmResponse:
        spec = self._tasks.get(request.task)
        if spec is None:
            raise LlmUnavailable(f"no task spec loaded for {request.task!r}")

        # Raises LlmInputRefused (names only) before any model work.
        prompt = build_prompt(spec, request.inputs)

        draft = spec.authority == LlmAuthority.DRAFT_FOR_HUMAN
        tier = _tier_for(spec)
        try:
            out = self._gateway.structured(
                task=gateway_task_name(spec.task),
                tier=tier,
                system=self._persona,
                user=prompt,
                schema=_Draft if draft else _Label,
                max_tokens=MAX_TOKENS,
            )
        except _GatewayRefusal:
            # Declined on content grounds. Never retried (gateway contract).
            raise LlmModelRefused(spec.task) from None
        except _GatewayError as exc:
            # Type only: the gateway message can carry provider text.
            raise LlmUnavailable(f"gateway call failed ({type(exc).__name__})") from None
        except ValidationError:
            raise LlmUnavailable("gateway reply did not match the task schema") from None

        self.calls.append(spec.task)

        if draft:
            text = (out.text or "").strip()
            if not text or len(text) > MAX_DRAFT_CHARS:
                raise LlmUnavailable("draft outside the accepted length")
            label, body = None, text
        else:
            if out.label not in spec.output_values:
                raise LlmUnavailable("label outside the task's declared output_values")
            label, body = out.label, None

        return LlmResponse(
            task=spec.task,
            task_version=spec.version,
            label=label,
            text=body,
            confidence=out.confidence,
            model=self._gateway.model_for(tier),
        )


def llm_mode(environ: Optional[Mapping[str, str]] = None) -> str:
    """`LLM_MODE` as the platform reads it: offline by default.

    Uses `hr_onboarding_common.llm.llm_mode` when the shared package has it
    (it validates the value); otherwise the UC-05 rule -- anything other than
    "offline" is online.
    """
    if GATEWAY_AVAILABLE:
        from hr_onboarding_common import llm as common_llm  # type: ignore[import-untyped]

        shared = getattr(common_llm, "llm_mode", None)
        if shared is not None:
            return str(shared(environ))
    env = os.environ if environ is None else environ
    return ONLINE if (env.get("LLM_MODE", OFFLINE).strip().lower() or OFFLINE) != OFFLINE else OFFLINE


def _build_gateway(environ: Optional[Mapping[str, str]] = None) -> Any:
    from hr_onboarding_common import llm as common_llm  # type: ignore[import-untyped]
    from hr_onboarding_common.config import get_settings  # type: ignore[import-untyped]

    factory = getattr(common_llm, "build_llm", None)
    if factory is not None:
        # The platform factory validates the online configuration and owns
        # provider routing; it raises a clear error when misconfigured. It is
        # given the SAME environment the mode was decided from: re-reading
        # os.environ here could build an offline FakeLLM for a caller that
        # asked for online (or validate a different key than the one decided on).
        return factory(get_settings(), environ=environ)
    return common_llm.LiteLLMGateway(get_settings())


def build_uc01_llm(
    tasks: Mapping[str, LlmTaskSpec],
    persona: str,
    *,
    gateway: Any = None,
    environ: Optional[Mapping[str, str]] = None,
) -> LlmClient:
    """The advisory LLM this UC-01 process should use.

    - an injected `gateway` (an `hr_onboarding_common` LLMClient) is used as
      given -- whoever built it already applied LLM_MODE;
    - LLM_MODE=offline (the default): the deterministic `FakeLlm`;
    - LLM_MODE=online: `GatewayLlm` over the central gateway. Refuses, rather
      than degrading silently, when the shared package is not installed.
    """
    if gateway is not None:
        return GatewayLlm(gateway, tasks, persona)
    if llm_mode(environ) == OFFLINE:
        return FakeLlm(dict(tasks))
    if not GATEWAY_AVAILABLE:
        raise LlmError(
            "LLM_MODE=online but hr_onboarding_common is not installed in this environment; "
            "install it or set LLM_MODE=offline"
        )
    return GatewayLlm(_build_gateway(environ), tasks, persona)


__all__ = [
    "GATEWAY_AVAILABLE",
    "MAX_DRAFT_CHARS",
    "OFFLINE",
    "ONLINE",
    "TASK_PREFIX",
    "GatewayLlm",
    "build_uc01_llm",
    "gateway_task_name",
    "llm_mode",
]
