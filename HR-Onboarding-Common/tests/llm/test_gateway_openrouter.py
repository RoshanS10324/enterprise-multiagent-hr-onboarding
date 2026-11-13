"""LiteLLMGateway with OpenRouter routes, through an injected completion function (no network):
the route and call parameters LiteLLM receives, Pydantic validation, retries with bounded backoff,
fallbacks, refusals, and redaction of provider errors. Key values below are fake."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from hr_onboarding_common.config import Settings
from hr_onboarding_common.llm import LiteLLMGateway, LLMError, LLMRefusal, ModelTier, redact
from hr_onboarding_common.llm.callbacks import logging_callback

FAKE_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"
PRIMARY, FALLBACK = "openrouter/acme/fast-1", "openrouter/other/fast-2"


class Answer(BaseModel):
    ok: bool
    note: str


# Provider exceptions are matched by class name (gateway.TRANSIENT / REFUSAL), as LiteLLM raises them.
class RateLimitError(Exception): ...
class BadGatewayError(Exception): ...
class Timeout(Exception): ...
class AuthenticationError(Exception): ...
class ContentPolicyViolationError(Exception): ...


def reply(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))],
                           usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7))


GOOD = '{"ok": true, "note": "fine"}'


class Script:
    """A completion function answering from a per-model script of replies/exceptions."""

    def __init__(self, **by_model):
        self.by_model = {PRIMARY: by_model.get("primary", []), FALLBACK: by_model.get("fallback", [])}
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        step = self.by_model[kwargs["model"]].pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def models(self) -> list[str]:
        return [c["model"] for c in self.calls]


def gateway(script, *, attempts=3, events=None, sleeps=None):
    settings = Settings(model_fast=PRIMARY, fallbacks_fast=(FALLBACK,), llm_max_attempts=attempts,
                        llm_backoff_initial_s=1.0, llm_backoff_max_s=4.0, llm_timeout_s=42.0)
    return LiteLLMGateway(settings, callbacks=[(events if events is not None else []).append],
                          completion_fn=script, sleep=(sleeps if sleeps is not None else []).append)


def ask(gw):
    return gw.structured(task="test.answer", tier=ModelTier.FAST, system="system preamble",
                         user="<input>user text</input>", schema=Answer)


def test_the_openrouter_route_and_call_parameters_reach_litellm_unchanged():
    script = Script(primary=[reply(GOOD)])
    assert ask(gateway(script)) == Answer(ok=True, note="fine")
    (call,) = script.calls
    assert call["model"] == PRIMARY  # LiteLLM routes openrouter/<provider>/<model> to OpenRouter
    assert call["response_format"] is Answer  # structured output requested with the Pydantic schema
    assert (call["num_retries"], call["max_retries"]) == (0, 0)  # the gateway is the only retry layer
    assert call["timeout"] == 42.0 and call["metadata"] == {"task": "test.answer", "tier": "fast"}
    assert [m["role"] for m in call["messages"]] == ["system", "user"]
    assert call["messages"][0]["content"] == "system preamble"  # no anthropic cache block on openrouter routes


def test_output_that_does_not_match_the_schema_is_retried_then_falls_back():
    events = []
    script = Script(primary=[reply('{"ok": "maybe"}'), reply("not json"), reply("{}")], fallback=[reply(GOOD)])
    assert ask(gateway(script, events=events)) == Answer(ok=True, note="fine")
    assert script.models() == [PRIMARY] * 3 + [FALLBACK]
    assert [e.kind for e in events] == ["retry", "retry", "fallback", "success"]
    assert events[-1].model == FALLBACK and events[-1].prompt_tokens == 11


def test_transient_errors_are_retried_with_bounded_exponential_backoff():
    sleeps = []
    script = Script(primary=[RateLimitError("429"), BadGatewayError("502"), reply(GOOD)])
    assert ask(gateway(script, sleeps=sleeps)) == Answer(ok=True, note="fine")
    assert script.models() == [PRIMARY] * 3
    assert len(sleeps) == 2 and all(0 <= s <= 4.0 for s in sleeps)


def test_a_timeout_is_retried_and_the_configured_timeout_is_sent_on_every_attempt():
    script = Script(primary=[Timeout("request timed out"), reply(GOOD)])
    assert ask(gateway(script)) == Answer(ok=True, note="fine")
    assert script.models() == [PRIMARY] * 2 and [c["timeout"] for c in script.calls] == [42.0, 42.0]


def test_retries_stop_at_the_attempt_limit_and_the_fallback_model_is_tried():
    script = Script(primary=[RateLimitError("429")] * 3, fallback=[reply(GOOD)])
    assert ask(gateway(script, attempts=3)) == Answer(ok=True, note="fine")
    assert script.models() == [PRIMARY, PRIMARY, PRIMARY, FALLBACK]


def test_a_non_transient_error_is_not_retried_on_the_same_model():
    script = Script(primary=[AuthenticationError("401")], fallback=[reply(GOOD)])
    ask(gateway(script))
    assert script.models() == [PRIMARY, FALLBACK]


@pytest.mark.parametrize("step", [reply("", finish_reason="content_filter"), ContentPolicyViolationError("policy")])
def test_a_refusal_is_final_no_retry_and_no_fallback(step):
    script = Script(primary=[step], fallback=[reply(GOOD)])
    with pytest.raises(LLMRefusal):
        ask(gateway(script))
    assert script.models() == [PRIMARY]


def test_a_provider_error_is_reported_without_the_key_or_the_request(monkeypatch, caplog):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    leak = (f"Incorrect API key provided: {FAKE_KEY}. headers={{'Authorization': 'Bearer {FAKE_KEY}'}} "
            f"api_key={FAKE_KEY} request={{'messages': '<input>user text</input>'}} " + "x" * 600)
    script = Script(primary=[AuthenticationError(leak)], fallback=[AuthenticationError(leak)])
    production_like = LiteLLMGateway(Settings(model_fast=PRIMARY, fallbacks_fast=(FALLBACK,)), completion_fn=script,
                                     sleep=lambda s: None)  # default callbacks: the logging callback
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LLMError) as exc:
            ask(production_like)
    message = str(exc.value)
    assert FAKE_KEY not in message and "Bearer sk-or" not in message
    assert "AuthenticationError" in message and len(message) < 500
    assert exc.value.__cause__ is None and exc.value.__suppress_context__  # the raw provider error is not chained
    assert FAKE_KEY not in caplog.text and "user text" not in caplog.text


def test_events_carry_no_prompt_or_completion_text(caplog):
    events = []
    with caplog.at_level(logging.INFO):
        ask(gateway(Script(primary=[reply(GOOD)]), events=events))
        for event in events:
            logging_callback(event)
    assert "user text" not in caplog.text and "system preamble" not in caplog.text and "fine" not in caplog.text


@pytest.mark.parametrize("text, hidden", [
    ("Incorrect API key provided: sk-or-v1-abcdef0123456789", "sk-or-v1-abcdef0123456789"),
    ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
    ('{"api_key": "plain-secret-value"}', "plain-secret-value"),
    ("x-api-key=zzzzzzzzzzzz", "zzzzzzzzzzzz"),
])
def test_redact_masks_credentials(text, hidden):
    assert hidden not in redact(text, environ={})


def test_redact_masks_configured_secret_values_anywhere_and_bounds_length():
    secret = "a-secret-without-any-key-shape"
    out = redact(f"failed for {secret}: " + "y" * 1000, environ={"OPENROUTER_API_KEY": secret})
    assert secret not in out and len(out) <= 300
