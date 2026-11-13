"""LiteLLMGateway: the ONLY place in the codebase that calls a model.

- Provider-neutral: agents name a tier; the configured model is turned into a LiteLLM route by
  llm/routing.py (an OpenRouter model id "deepseek/deepseek-v4.1-flash" is called as
  "openrouter/deepseek/deepseek-v4.1-flash"). No provider SDK is imported by our code; LiteLLM reads
  the credential from the environment (OPENROUTER_API_KEY for openrouter/ routes).
- OpenRouter specifics, sent through LiteLLM's `extra_body` (verified against the installed LiteLLM:
  forwarded unchanged): `api_base` = OPENROUTER_BASE_URL; `provider.require_parameters` so OpenRouter
  only routes to providers that support every parameter (structured output, reasoning); and
  `reasoning` = {"enabled": true} (or {"effort": ...}) when LLM_REASONING_ENABLED, else
  {"enabled": false}. A single-turn call also sets `reasoning.exclude`, so no reasoning text is
  returned at all. Reasoning tokens count against max_tokens, so the budget is raised by
  LLM_REASONING_TOKEN_HEADROOM while reasoning is on.
- Retries with exponential backoff + jitter (tenacity) on transient errors. This is the only retry
  layer: LiteLLM and any SDK underneath are called with num_retries=0 / max_retries=0.
- Fallback chain per tier: when a model is exhausted, the next configured model is tried. A refusal
  (content filter or provider content-policy error) is final: no retry, no fallback.
- Callbacks: every attempt outcome is emitted as an `LLMEvent` (logging by default) -- counts, never
  prompt, answer or reasoning text.
- Structured output: the Pydantic schema is sent as `response_format` and the reply is validated.
- Multi-turn: with an `LLMConversation`, the earlier turns are sent -- each assistant turn with its
  `reasoning_details` unchanged -- and the new turn is added once it succeeds (llm/conversation.py).
- Failures are reported through `redact`: no credential, bearer token or long provider text.
Which processes construct a gateway (LLM_MODE=online) is decided in llm/factory.py.
"""

from __future__ import annotations

import copy
import time
from typing import Any, Callable

from pydantic import ValidationError
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from hr_onboarding_common.config import Settings
from hr_onboarding_common.llm.base import BaseLLM, LLMError, LLMOutputError, LLMRefusal, ModelTier, T
from hr_onboarding_common.llm.callbacks import LLMCallback, LLMEvent, logging_callback
from hr_onboarding_common.llm.conversation import LLMConversation
from hr_onboarding_common.llm.redact import redact
from hr_onboarding_common.llm.routing import is_openrouter, litellm_route
from hr_onboarding_common.observability.tracing import span

TRANSIENT = ("RateLimitError", "APIConnectionError", "Timeout", "InternalServerError", "ServiceUnavailableError",
             "BadGatewayError")
# Provider-side refusals on content grounds: final, like finish_reason == "content_filter".
REFUSAL = ("ContentPolicyViolationError",)

_litellm = None


def _load_litellm(settings: Settings):
    """Import LiteLLM lazily (the import is slow) and configure it once."""
    global _litellm
    if _litellm is None:
        import logging
        import os

        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # no network call at import
        import litellm

        litellm.drop_params = True  # ignore params a fallback provider does not support
        litellm.telemetry = False
        litellm.suppress_debug_info = True
        # LiteLLM's own debug logs carry whole requests and responses: prompts, answers, reasoning text.
        # The gateway reports every call itself (LLMEvent, redacted errors), so these stay off whatever
        # level the application logs at.
        for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
            logging.getLogger(name).setLevel(logging.CRITICAL)
        if settings.litellm_callbacks:
            litellm.callbacks = list(settings.litellm_callbacks)
        _litellm = litellm
    return _litellm


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, LLMOutputError) or type(exc).__name__ in TRANSIENT


def _reasoning_details(message: Any) -> list[Any] | None:
    """The provider's `reasoning_details` of an answer, exactly as returned (LiteLLM keeps them in
    provider_specific_fields), or None."""
    details = getattr(message, "reasoning_details", None)
    if details is None:
        details = (getattr(message, "provider_specific_fields", None) or {}).get("reasoning_details")
    return copy.deepcopy(details) if isinstance(details, list) else None


class LiteLLMGateway(BaseLLM):
    def __init__(self, settings: Settings | None = None, *, callbacks: list[LLMCallback] | None = None,
                 completion_fn: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep):
        super().__init__(settings)
        self.callbacks: list[LLMCallback] = [logging_callback] if callbacks is None else list(callbacks)
        self._completion_fn = completion_fn  # injectable for tests
        self._sleep = sleep

    def route_for(self, model: str) -> str:
        """The LiteLLM route a configured model is called with (llm/routing.py)."""
        return litellm_route(model, self.settings)

    # ------------------------------------------------------------------ public
    def structured(self, *, task: str, tier: ModelTier, system: str, user: str, schema: type[T],
                   max_tokens: int = 8000, conversation: LLMConversation | None = None,
                   scope: tuple[str, ...] | None = None) -> T:
        if conversation is not None:
            conversation.check_scope(scope)  # before anything is sent
        self._record(task, tier, system, user)
        chain = [self.route_for(m) for m in (self.model_for(tier), *self.fallbacks_for(tier))]
        last: BaseException | None = None
        for i, route in enumerate(chain):
            try:
                return self._call_with_retry(route, task, tier, system, user, schema, max_tokens, conversation)
            except LLMRefusal:
                raise
            except Exception as e:  # noqa: BLE001 - provider errors are normalised below
                last = e
                nxt = chain[i + 1] if i + 1 < len(chain) else None
                self._emit(LLMEvent("fallback" if nxt else "failure", task, tier, route,
                                    error_type=type(e).__name__, next_model=nxt))
        # `from None`: the provider's own exception (and its message) is not chained into tracebacks.
        raise LLMError(f"{task}: all models failed ({', '.join(chain)}): {type(last).__name__}: "
                       f"{redact(last)}") from None

    # ------------------------------------------------------------------ internals
    def _call_with_retry(self, route: str, task: str, tier: ModelTier, system: str, user: str,
                         schema: type[T], max_tokens: int, conversation: LLMConversation | None) -> T:
        s = self.settings

        def before_sleep(state) -> None:
            self._emit(LLMEvent("retry", task, tier, route, attempt=state.attempt_number,
                                error_type=type(state.outcome.exception()).__name__))

        retrying = Retrying(stop=stop_after_attempt(s.llm_max_attempts),
                            wait=wait_exponential_jitter(initial=s.llm_backoff_initial_s, max=s.llm_backoff_max_s),
                            retry=retry_if_exception(_is_transient), before_sleep=before_sleep,
                            sleep=self._sleep, reraise=True)
        for attempt in retrying:
            with attempt:
                return self._call_once(route, task, tier, system, user, schema, max_tokens, conversation,
                                       attempt.retry_state.attempt_number)
        raise AssertionError("unreachable")  # pragma: no cover

    def _request(self, route: str, system: str, user: str, schema: type[T], max_tokens: int,
                 conversation: LLMConversation | None, task: str, tier: ModelTier) -> dict[str, Any]:
        """The keyword arguments of one LiteLLM call."""
        s = self.settings
        system_content: Any = system
        if route.startswith("anthropic/"):
            # system prompt = agent preamble: stable, so it is the cached prefix
            system_content = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        history = conversation.history() if conversation is not None else []
        request: dict[str, Any] = {
            "model": route,
            "messages": [{"role": "system", "content": system_content}, *history, {"role": "user", "content": user}],
            "response_format": schema, "max_tokens": max_tokens, "timeout": s.llm_timeout_s,
            "num_retries": 0, "max_retries": 0,  # retries and fallbacks are owned by this gateway
            "metadata": {"task": task, "tier": str(tier)},
        }
        if is_openrouter(route):
            extra: dict[str, Any] = {}
            request["api_base"] = s.openrouter_base_url
            if s.openrouter_require_parameters:
                extra["provider"] = {"require_parameters": True}
            if s.llm_reasoning_enabled:
                reasoning: dict[str, Any] = {"effort": s.llm_reasoning_effort} if s.llm_reasoning_effort \
                    else {"enabled": True}
                if conversation is None:
                    reasoning["exclude"] = True  # nothing to carry forward: no reasoning text is returned
                extra["reasoning"] = reasoning
                request["max_tokens"] = max_tokens + max(0, s.llm_reasoning_token_headroom)
            else:
                # Said explicitly: a model can reason by default (DeepSeek V4.1 Flash does), so leaving
                # the field out would not turn reasoning off.
                extra["reasoning"] = {"enabled": False}
            if extra:
                request["extra_body"] = extra
        return request

    def _call_once(self, route: str, task: str, tier: ModelTier, system: str, user: str, schema: type[T],
                   max_tokens: int, conversation: LLMConversation | None, attempt: int) -> T:
        completion = self._completion_fn or _load_litellm(self.settings).completion
        request = self._request(route, system, user, schema, max_tokens, conversation, task, tier)
        started = time.perf_counter()
        with span("llm.call", **{"gen_ai.request.model": route, "llm.task": task, "llm.tier": str(tier),
                                 "llm.attempt": attempt}) as sp:
            try:
                response = completion(**request)
            except Exception as e:  # noqa: BLE001 - only refusals are re-typed; the rest propagate as raised
                if type(e).__name__ in REFUSAL:
                    raise LLMRefusal(f"{task}: {route} declined the request") from None
                raise
            choice = response.choices[0]
            content = getattr(choice.message, "content", None) or ""
            usage = getattr(response, "usage", None)
            pt, ct = getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)
            reasoning_tokens = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
            if choice.finish_reason == "content_filter":
                raise LLMRefusal(f"{task}: {route} declined the request")
            if choice.finish_reason == "length":
                if not content.strip() and reasoning_tokens:
                    raise LLMError(f"{task}: {route}: reasoning used the whole token budget "
                                   f"(max_tokens={request['max_tokens']}); raise LLM_REASONING_TOKEN_HEADROOM "
                                   f"or set a lower LLM_REASONING_EFFORT")
                raise LLMError(f"{task}: {route} output truncated at max_tokens={request['max_tokens']}")
            try:
                parsed = schema.model_validate_json(content)
            except ValidationError as e:
                raise LLMOutputError(f"{task}: {route} returned output that does not match "
                                     f"{schema.__name__} ({e.error_count()} errors)") from None
            sp.set_attribute("gen_ai.usage.input_tokens", pt or 0)
            sp.set_attribute("gen_ai.usage.output_tokens", ct or 0)
        if conversation is not None:  # only a turn that succeeded becomes history
            conversation.record_turn(user, content, _reasoning_details(choice.message))
        self._emit(LLMEvent("success", task, tier, route, attempt=attempt,
                            latency_ms=int((time.perf_counter() - started) * 1000),
                            prompt_tokens=pt, completion_tokens=ct, cost_usd=self._cost(response),
                            reasoning_tokens=reasoning_tokens))
        return parsed

    def _cost(self, response: Any) -> float | None:
        if self._completion_fn is not None:
            return None
        try:
            return float(_load_litellm(self.settings).completion_cost(completion_response=response))
        except Exception:  # noqa: BLE001 - unknown model price is not an error
            return None

    def _emit(self, event: LLMEvent) -> None:
        for cb in self.callbacks:
            try:
                cb(event)
            except Exception:  # noqa: BLE001 - a broken callback must never break an agent run
                pass
