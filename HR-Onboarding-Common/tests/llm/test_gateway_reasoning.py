"""LiteLLMGateway with OpenRouter reasoning, through an injected completion function (no network): the
DeepSeek model id is called as its OpenRouter route, reasoning options and the token headroom, a
budget used up by reasoning, multi-turn conversations that carry `reasoning_details` back unchanged,
scope isolation, and reasoning text never reaching results, events or logs."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from hr_onboarding_common.config import Settings
from hr_onboarding_common.llm import (ConversationScopeError, FakeLLM, LiteLLMGateway, LLMConversation, LLMError,
                                      ModelTier)
from hr_onboarding_common.llm.callbacks import logging_callback

DEEPSEEK = "deepseek/deepseek-v4.1-flash"
ROUTE = "openrouter/deepseek/deepseek-v4.1-flash"
REASONING_TEXT = "PRIVATE-REASONING-TEXT"
DETAILS = [{"type": "reasoning.text", "text": REASONING_TEXT, "signature": "sig-1", "id": "rd-1", "format": "unknown",
            "index": 0}]
SCOPE = ("client-a", "case-1", "session-1")


class Answer(BaseModel):
    ok: bool
    note: str


GOOD = '{"ok": true, "note": "fine"}'


def reply(content: str = GOOD, finish_reason: str = "stop", reasoning_tokens: int | None = 120, details=DETAILS):
    """A LiteLLM ModelResponse as the gateway reads it: reasoning text in `reasoning_content`,
    `reasoning_details` in provider_specific_fields."""
    message = SimpleNamespace(content=content, reasoning_content=REASONING_TEXT,
                              provider_specific_fields={"reasoning_details": details} if details is not None else {})
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7 + (reasoning_tokens or 0),
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens))
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason, message=message)], usage=usage)


class Script:
    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def gateway(script, events=None, **over):
    settings = Settings(**{"model_fast": DEEPSEEK, "fallbacks_fast": (), "llm_max_attempts": 2,
                           "llm_backoff_initial_s": 0.01, "llm_backoff_max_s": 0.02, **over})
    return LiteLLMGateway(settings, callbacks=[(events if events is not None else []).append], completion_fn=script,
                          sleep=lambda s: None)


def ask(gw, user="question", conversation=None, scope=None, max_tokens=300):
    return gw.structured(task="test.answer", tier=ModelTier.FAST, system="system preamble", user=user, schema=Answer,
                         max_tokens=max_tokens, conversation=conversation, scope=scope)


# ------------------------------------------------------------------ the request


def test_the_model_id_is_called_as_its_openrouter_route_with_reasoning_and_headroom():
    script = Script(reply())
    assert ask(gateway(script)) == Answer(ok=True, note="fine")
    (call,) = script.calls
    assert call["model"] == ROUTE and call["api_base"] == "https://openrouter.ai/api/v1"
    assert call["extra_body"] == {"provider": {"require_parameters": True},
                                  "reasoning": {"enabled": True, "exclude": True}}
    assert call["max_tokens"] == 300 + 2048  # the caller's budget for the answer, plus room to reason


@pytest.mark.parametrize("over, extra_body, max_tokens", [
    ({"llm_reasoning_effort": "high"}, {"provider": {"require_parameters": True},
                                        "reasoning": {"effort": "high", "exclude": True}}, 2348),
    ({"llm_reasoning_token_headroom": 500}, {"provider": {"require_parameters": True},
                                             "reasoning": {"enabled": True, "exclude": True}}, 800),
    # Off is said explicitly: DeepSeek V4.1 Flash reasons by default (catalog: default_enabled true).
    ({"llm_reasoning_enabled": False}, {"provider": {"require_parameters": True}, "reasoning": {"enabled": False}}, 300),
    ({"llm_reasoning_enabled": False, "openrouter_require_parameters": False}, {"reasoning": {"enabled": False}}, 300),
])
def test_reasoning_options(over, extra_body, max_tokens):
    script = Script(reply())
    ask(gateway(script, **over))
    assert script.calls[0].get("extra_body") == extra_body and script.calls[0]["max_tokens"] == max_tokens


def test_routes_outside_openrouter_get_no_openrouter_options():
    script = Script(reply())
    ask(gateway(script, llm_provider="litellm", model_fast="anthropic/claude-haiku-4-5"))
    (call,) = script.calls
    assert call["model"] == "anthropic/claude-haiku-4-5"
    assert "api_base" not in call and "extra_body" not in call and call["max_tokens"] == 300
    assert call["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


# ------------------------------------------------------------------ the answer


def test_reasoning_that_uses_the_whole_budget_is_reported_clearly():
    script = Script(reply(content="", finish_reason="length", reasoning_tokens=2348))
    with pytest.raises(LLMError, match="reasoning used the whole token budget") as exc:
        ask(gateway(script))
    assert "LLM_REASONING_TOKEN_HEADROOM" in str(exc.value) and len(script.calls) == 1  # not retried


def test_an_answer_cut_off_by_max_tokens_is_still_reported_as_truncated():
    with pytest.raises(LLMError, match="output truncated at max_tokens=2348"):
        ask(gateway(Script(reply(content='{"ok": tr', finish_reason="length"))))


def test_reasoning_text_never_reaches_the_result_events_or_logs(caplog):
    events = []
    with caplog.at_level(logging.DEBUG):
        result = ask(gateway(Script(reply()), events=events))
        for event in events:
            logging_callback(event)
    assert result == Answer(ok=True, note="fine")
    (event,) = events
    assert event.kind == "success" and event.model == ROUTE and event.reasoning_tokens == 120  # a count only
    assert REASONING_TEXT not in repr(result) + repr(events) + caplog.text
    assert "sig-1" not in repr(events) + caplog.text


# ------------------------------------------------------------------ conversations


def test_a_conversation_sends_earlier_turns_with_their_reasoning_details_unchanged():
    script = Script(reply(), reply('{"ok": false, "note": "second"}'))
    gw, conversation = gateway(script), LLMConversation(scope=SCOPE)
    ask(gw, "first", conversation, SCOPE)
    assert ask(gw, "second", conversation, SCOPE) == Answer(ok=False, note="second")
    first, second = script.calls
    assert first["extra_body"]["reasoning"] == {"enabled": True}  # no exclude: the details must come back
    assert first["messages"] == [{"role": "system", "content": "system preamble"}, {"role": "user", "content": "first"}]
    assert second["messages"] == [
        {"role": "system", "content": "system preamble"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": GOOD, "reasoning_details": DETAILS},
        {"role": "user", "content": "second"},
    ]
    assert conversation.turns == 2 and REASONING_TEXT not in repr(conversation)


def test_reasoning_details_are_copied_not_shared_with_the_provider_response():
    details = [{"type": "reasoning.encrypted", "data": "opaque", "id": "rd-9", "format": "unknown", "index": 0}]
    response = reply(details=details)
    gw, conversation = gateway(Script(response)), LLMConversation(scope=SCOPE)
    ask(gw, "first", conversation, SCOPE)
    details[0]["data"] = "changed afterwards"
    assert conversation.history()[1]["reasoning_details"] == [
        {"type": "reasoning.encrypted", "data": "opaque", "id": "rd-9", "format": "unknown", "index": 0}]
    conversation.history()[1]["reasoning_details"].clear()  # a copy: the conversation is unchanged
    assert conversation.history()[1]["reasoning_details"]


def test_an_answer_without_reasoning_details_is_recorded_without_inventing_them():
    gw, conversation = gateway(Script(reply(details=None))), LLMConversation(scope=SCOPE)
    ask(gw, "first", conversation, SCOPE)
    assert conversation.history()[1] == {"role": "assistant", "content": GOOD}


@pytest.mark.parametrize("scope", [("client-b", "case-1", "session-1"), ("client-a", "case-2", "session-1"), None])
def test_a_conversation_is_refused_under_another_scope_before_anything_is_sent(scope):
    script = Script(reply())
    gw, conversation = gateway(script), LLMConversation(scope=SCOPE)
    ask(gw, "first", conversation, SCOPE)
    with pytest.raises(ConversationScopeError):
        ask(gw, "other case's question", conversation, scope)
    assert len(script.calls) == 1 and conversation.turns == 1


def test_a_failed_turn_is_not_added_to_the_conversation():
    script = Script(reply("not json"), reply("still not json"), reply())
    gw, conversation = gateway(script), LLMConversation(scope=SCOPE)
    with pytest.raises(LLMError):
        ask(gw, "first", conversation, SCOPE)
    assert conversation.turns == 0
    ask(gw, "again", conversation, SCOPE)
    assert [m["content"] for m in conversation.history()] == ["again", GOOD]


def test_a_conversation_needs_a_scope():
    for scope in ((), ("",), ("client-a", " ")):
        with pytest.raises(ConversationScopeError):
            LLMConversation(scope=scope)


def test_the_offline_llm_keeps_the_same_conversation_contract():
    fake = FakeLLM(Settings()).on("test.answer", lambda system, user, schema: Answer(ok=True, note=user))
    conversation = LLMConversation(scope=SCOPE)
    ask(fake, "first", conversation, SCOPE)
    assert conversation.history() == [{"role": "user", "content": "first"},
                                      {"role": "assistant", "content": '{"ok":true,"note":"first"}'}]
    with pytest.raises(ConversationScopeError):
        ask(fake, "second", conversation, ("client-b", "case-1", "session-1"))
