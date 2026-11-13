"""The real installed LiteLLM behind LiteLLMGateway, with only HTTP replaced (httpx.MockTransport):
what actually goes over the wire to OpenRouter for one structured call, and that a 429 is retried by
the gateway alone -- one HTTP request per attempt, no hidden retry layer. Dummy key; no network."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from hr_onboarding_common.config import Settings
from hr_onboarding_common.llm import LiteLLMGateway, LLMError, ModelTier

DUMMY_KEY = "sk-or-v1-DUMMY0000000000000000000000"


class Ping(BaseModel):
    ok: bool
    echo: str


@pytest.fixture
def openrouter(monkeypatch):
    """Every httpx client LiteLLM creates talks to `responses` instead of the network."""
    import litellm

    monkeypatch.setenv("OPENROUTER_API_KEY", DUMMY_KEY)
    cache = getattr(litellm, "in_memory_llm_clients_cache", None)
    if cache is not None and hasattr(cache, "flush_cache"):
        cache.flush_cache()  # no client created earlier without the mock transport
    requests: list[httpx.Request] = []
    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    original = httpx.Client.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", init)
    yield requests, responses
    if cache is not None and hasattr(cache, "flush_cache"):
        cache.flush_cache()


def ok(model: str) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": '{"ok": true, "echo": "pong"}'}}],
        "usage": {"prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30}})


def gateway(**over) -> LiteLLMGateway:
    settings = Settings(**{"model_fast": "openrouter/acme/fast-1", "fallbacks_fast": (), "llm_max_attempts": 3,
                           "llm_backoff_initial_s": 0.01, "llm_backoff_max_s": 0.02, **over})
    return LiteLLMGateway(settings, callbacks=[], sleep=lambda s: None)


def ask(gw: LiteLLMGateway) -> Ping:
    return gw.structured(task="test.ping", tier=ModelTier.FAST, system="s", user="u", schema=Ping, max_tokens=50)


def test_litellm_sends_the_structured_call_to_openrouter_with_the_environment_key(openrouter):
    requests, responses = openrouter
    responses.append(ok("acme/fast-1"))
    assert ask(gateway()) == Ping(ok=True, echo="pong")
    (request,) = requests
    body = json.loads(request.content)
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {DUMMY_KEY}"  # read by LiteLLM from OPENROUTER_API_KEY
    assert body["model"] == "acme/fast-1"  # the openrouter/ prefix is the route, not part of the id
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "Ping"
    assert body["max_tokens"] == 50 + 2048  # reasoning is on by default: LLM_REASONING_TOKEN_HEADROOM is added


def test_a_rate_limit_is_retried_by_the_gateway_only_one_request_per_attempt(openrouter):
    requests, responses = openrouter
    responses.extend([httpx.Response(429, json={"error": {"message": "rate limited", "code": 429}}), ok("acme/fast-1")])
    assert ask(gateway()) == Ping(ok=True, echo="pong")
    assert len(requests) == 2  # LiteLLM and the HTTP layer did not retry on their own


def test_an_authentication_failure_is_reported_without_the_key(openrouter):
    requests, responses = openrouter
    responses.append(httpx.Response(401, json={"error": {"message": f"No auth credentials found for {DUMMY_KEY}",
                                                         "code": 401}}))
    with pytest.raises(LLMError) as exc:
        ask(gateway())
    assert len(requests) == 1  # not transient: no retry
    assert DUMMY_KEY not in str(exc.value) and "AuthenticationError" in str(exc.value)


# ------------------------------------------------------------------ DeepSeek V4.1 Flash with reasoning

DEEPSEEK = "deepseek/deepseek-v4.1-flash"
REASONING_TEXT = "PRIVATE-REASONING-TEXT"
# As OpenRouter returns them; sent back unchanged on the next turn of a conversation.
DETAILS = [{"type": "reasoning.text", "text": REASONING_TEXT, "signature": "sig-123", "id": "rd-1",
            "format": "unknown", "index": 0},
           {"type": "reasoning.encrypted", "data": "b3BhcXVlLWJsb2I=", "id": "rd-2", "format": "unknown", "index": 1}]


def reasoned(content: str = '{"ok": true, "echo": "pong"}', finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": DEEPSEEK,
        "choices": [{"index": 0, "finish_reason": finish_reason,
                     "message": {"role": "assistant", "content": content, "reasoning": REASONING_TEXT,
                                 "reasoning_details": DETAILS}}],
        "usage": {"prompt_tokens": 21, "completion_tokens": 309, "total_tokens": 330,
                  "completion_tokens_details": {"reasoning_tokens": 300}}})


def deepseek(**over) -> LiteLLMGateway:
    return gateway(**{"model_fast": DEEPSEEK, **over})


def test_deepseek_goes_to_openrouter_with_reasoning_enabled_and_excluded(openrouter):
    requests, responses = openrouter
    responses.append(reasoned())
    assert ask(deepseek()) == Ping(ok=True, echo="pong")  # the answer only
    body = json.loads(requests[0].content)
    assert str(requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert body["model"] == DEEPSEEK  # the OpenRouter model id; LiteLLM was called with openrouter/<id>
    assert body["reasoning"] == {"enabled": True, "exclude": True}  # single turn: no reasoning text returned
    assert body["provider"] == {"require_parameters": True}  # only providers supporting json_schema + reasoning
    assert body["response_format"]["type"] == "json_schema" and body["max_tokens"] == 50 + 2048


def test_the_configured_base_url_and_effort_are_used(openrouter):
    requests, responses = openrouter
    responses.append(reasoned())
    ask(deepseek(openrouter_base_url="https://openrouter.example.test/api/v1", llm_reasoning_effort="low"))
    assert str(requests[0].url) == "https://openrouter.example.test/api/v1/chat/completions"
    assert json.loads(requests[0].content)["reasoning"] == {"effort": "low", "exclude": True}


def test_with_reasoning_off_it_is_turned_off_explicitly_and_no_headroom_is_added(openrouter):
    """DeepSeek V4.1 Flash reasons by default (catalog: default_enabled true): leaving `reasoning` out
    would not turn it off, so the gateway sends enabled=false."""
    requests, responses = openrouter
    responses.append(ok(DEEPSEEK))
    ask(deepseek(llm_reasoning_enabled=False))
    body = json.loads(requests[0].content)
    assert body["reasoning"] == {"enabled": False}
    assert body["max_tokens"] == 50 and body["provider"] == {"require_parameters": True}


def test_reasoning_details_go_back_unchanged_on_the_next_turn_and_nowhere_else(openrouter, caplog):
    import logging

    from hr_onboarding_common.llm import LLMConversation

    requests, responses = openrouter
    responses.extend([reasoned(), reasoned('{"ok": true, "echo": "second"}')])
    gw, scope = deepseek(), ("client-a", "case-1", "session-1")
    conversation = LLMConversation(scope=scope)
    with caplog.at_level(logging.DEBUG):
        first = gw.structured(task="test.ping", tier=ModelTier.FAST, system="s", user="first question", schema=Ping,
                              max_tokens=50, conversation=conversation, scope=scope)
        second = gw.structured(task="test.ping", tier=ModelTier.FAST, system="s", user="second question", schema=Ping,
                               max_tokens=50, conversation=conversation, scope=scope)
    assert (first.echo, second.echo, conversation.turns) == ("pong", "second", 2)
    turn1, turn2 = (json.loads(r.content) for r in requests)
    assert turn1["reasoning"] == {"enabled": True}  # a conversation needs reasoning_details back: no exclude
    assert [m["role"] for m in turn2["messages"]] == ["system", "user", "assistant", "user"]
    assistant = turn2["messages"][2]
    assert assistant["content"] == '{"ok": true, "echo": "pong"}'
    assert assistant["reasoning_details"] == DETAILS  # exactly as returned: nothing edited, added or dropped
    # LiteLLM's own debug logs would hold whole bodies; the gateway keeps them off even at DEBUG.
    assert REASONING_TEXT not in repr(first) + repr(second) + repr(conversation) + caplog.text
    assert "first question" not in caplog.text and DUMMY_KEY not in caplog.text
