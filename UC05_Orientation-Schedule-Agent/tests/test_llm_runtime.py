"""LLM_MODE wiring of UC05's runtime (sub_agent.runtime.build_context): offline -> the deterministic
offline LLM, online -> hr_onboarding_common's LiteLLMGateway (OpenRouter routes), validated at
startup; and UC05's policy Q&A through that gateway with only the HTTP call replaced. No network:
the key below is fake and every model call goes to an injected completion function."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from context_layer.knowledge.policy_index import PolicyKnowledgeBase
from hr_onboarding_common.config import get_settings
from hr_onboarding_common.llm import FakeLLM, LiteLLMGateway, LLMError, ModelTier, validate_online_config
from hr_onboarding_common.platform.audit import AuditLog
from sub_agent.agent import AGENT_HOME
from sub_agent.fakes import offline_llm
from sub_agent.runtime import RuntimeConfigurationError, SanitizedLLM, build_context
from sub_agent.services.policy_qa import PolicyQAModelOutput, answer_policy_question

FAKE_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"
TIERS = {"LLM_MODEL_FRONTIER": "openrouter/acme/frontier-1", "LLM_MODEL_WORKHORSE": "openrouter/acme/workhorse-1",
         "LLM_MODEL_FAST": "openrouter/acme/fast-1"}


@pytest.fixture
def online(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "online")
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    for name, value in TIERS.items():
        monkeypatch.setenv(name, value)
    for name in ("LLM_FALLBACKS_FRONTIER", "LLM_FALLBACKS_WORKHORSE", "LLM_FALLBACKS_FAST"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_offline_mode_builds_the_deterministic_offline_llm():
    ctx, _ = build_context({"HR_AGENT_ENV": "development", "LLM_MODE": "offline"})
    assert isinstance(ctx.llm, SanitizedLLM) and isinstance(ctx.llm.inner, FakeLLM)
    assert {"onboarding.policy_qa", "onboarding.welcome_note"} <= set(ctx.llm.inner.handlers)


def test_online_mode_builds_the_gateway_on_the_configured_openrouter_tiers(online):
    ctx, _ = build_context()
    assert isinstance(ctx.llm, SanitizedLLM) and isinstance(ctx.llm.inner, LiteLLMGateway)
    assert ctx.llm.model_for(ModelTier.WORKHORSE) == "openrouter/acme/workhorse-1"
    assert ctx.llm.model_for(ModelTier.FAST) == "openrouter/acme/fast-1"
    assert ctx.llm.inner.fallbacks_for(ModelTier.WORKHORSE) == ()  # no unconfigured default fallback online


def test_online_mode_without_a_key_stops_startup_naming_the_variable(online):
    online.delenv("OPENROUTER_API_KEY")
    with pytest.raises(RuntimeConfigurationError, match="OPENROUTER_API_KEY is not set"):
        build_context()


def test_an_unknown_llm_mode_stops_startup(online):
    online.setenv("LLM_MODE", "maybe")
    with pytest.raises(RuntimeConfigurationError, match="LLM_MODE"):
        build_context()


def _gateway(completion):
    settings = get_settings(AGENT_HOME)
    validate_online_config(settings)
    return LiteLLMGateway(settings, completion_fn=completion, sleep=lambda s: None)


def test_policy_qa_goes_through_the_gateway_on_the_workhorse_route_with_its_guardrails(online):
    """The HR Policies Q&A, unchanged: the real service and grounding check, the real gateway on the
    configured OpenRouter route; only the HTTP call is replaced by the offline handler's answer."""
    handler = offline_llm().handlers["onboarding.policy_qa"]
    seen: list[dict] = []

    def completion(**kwargs):
        seen.append(kwargs)
        schema = kwargs["response_format"]
        out = handler(kwargs["messages"][0]["content"], kwargs["messages"][1]["content"], schema)
        content = (out if isinstance(out, BaseModel) else schema.model_validate(out)).model_dump_json()
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))],
                               usage=None)

    gateway = _gateway(completion)
    result = answer_policy_question(SanitizedLLM(gateway), PolicyKnowledgeBase(), AuditLog(),
                                    question="What is the dress code?", client_id="CLIENT-ACME",
                                    as_of=date(2026, 11, 1), employee_id="EMP-001")
    assert result.answer is not None and result.citations[0].doc_id == "hr-policy-consultants"
    (call,) = seen
    assert call["model"] == "openrouter/acme/workhorse-1" and call["response_format"] is PolicyQAModelOutput
    (record,) = gateway.calls
    assert (record.task, record.tier) == ("onboarding.policy_qa", ModelTier.WORKHORSE)


def test_a_provider_failure_reaches_uc05_as_a_generic_error_without_the_key(online):
    class AuthenticationError(Exception):
        pass

    def completion(**kwargs):
        raise AuthenticationError(f"Incorrect API key provided: {FAKE_KEY}")

    with pytest.raises(LLMError) as exc:
        answer_policy_question(SanitizedLLM(_gateway(completion)), PolicyKnowledgeBase(), AuditLog(),
                               question="What is the dress code?", client_id="CLIENT-ACME",
                               as_of=date(2026, 11, 1), employee_id="EMP-001")
    assert str(exc.value) == SanitizedLLM.GENERIC_MESSAGE and FAKE_KEY not in str(exc.value)


# ------------------------------------------------------------------ OpenRouter DeepSeek V4.1 Flash

DEEPSEEK = "deepseek/deepseek-v4.1-flash"
DEEPSEEK_ROUTE = "openrouter/deepseek/deepseek-v4.1-flash"
REASONING_TEXT = "PRIVATE-REASONING-TEXT"


@pytest.fixture
def deepseek(online):
    """The central configuration documented in HR-Onboarding-Common/.env.example (with a fake key)."""
    for name in TIERS:
        online.setenv(name, DEEPSEEK)
    online.setenv("OPENROUTER_MODEL", DEEPSEEK)
    online.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    online.setenv("LLM_REASONING_ENABLED", "true")
    return online


def _reasoned(handler):
    """A completion function answering with the offline handler, as a reasoning model does: the
    answer plus reasoning text and reasoning_details, which must go no further than the gateway."""
    seen: list[dict] = []

    def completion(**kwargs):
        seen.append(kwargs)
        schema = kwargs["response_format"]
        out = handler(kwargs["messages"][0]["content"], kwargs["messages"][-1]["content"], schema)
        content = (out if isinstance(out, BaseModel) else schema.model_validate(out)).model_dump_json()
        message = SimpleNamespace(content=content, reasoning_content=REASONING_TEXT, provider_specific_fields={
            "reasoning_details": [{"type": "reasoning.text", "text": REASONING_TEXT, "format": "unknown", "index": 0}]})
        usage = SimpleNamespace(prompt_tokens=40, completion_tokens=90,
                                completion_tokens_details=SimpleNamespace(reasoning_tokens=60))
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=message)], usage=usage)
    return completion, seen


def test_online_mode_runs_every_uc05_tier_on_deepseek_through_openrouter(deepseek):
    ctx, _ = build_context()
    gateway = ctx.llm.inner
    assert isinstance(gateway, LiteLLMGateway)
    assert {ctx.llm.model_for(tier) for tier in ModelTier} == {DEEPSEEK}  # the OpenRouter model id
    assert {gateway.route_for(gateway.model_for(tier)) for tier in ModelTier} == {DEEPSEEK_ROUTE}
    assert gateway.settings.llm_reasoning_enabled and all(gateway.fallbacks_for(t) == () for t in ModelTier)


def test_policy_qa_on_deepseek_sends_reasoning_and_returns_no_reasoning_text(deepseek, caplog):
    import logging

    completion, seen = _reasoned(offline_llm().handlers["onboarding.policy_qa"])
    with caplog.at_level(logging.DEBUG):
        result = answer_policy_question(SanitizedLLM(_gateway(completion)), PolicyKnowledgeBase(), AuditLog(),
                                        question="What is the dress code?", client_id="CLIENT-ACME",
                                        as_of=date(2026, 11, 1), employee_id="EMP-001")
    assert result.answer is not None and result.citations[0].doc_id == "hr-policy-consultants"
    (call,) = seen
    assert call["model"] == DEEPSEEK_ROUTE and call["api_base"] == "https://openrouter.ai/api/v1"
    assert call["extra_body"] == {"provider": {"require_parameters": True},
                                  "reasoning": {"enabled": True, "exclude": True}}
    assert call["max_tokens"] == 4000 + 2048  # UC05's answer budget, plus the central reasoning headroom
    assert REASONING_TEXT not in result.model_dump_json() + caplog.text


def test_the_welcome_note_budget_leaves_room_for_reasoning(deepseek):
    """The welcome note asks for 300 tokens; with reasoning on, the gateway adds the headroom so
    reasoning cannot use up the whole budget (OpenRouter counts reasoning tokens in max_tokens)."""
    from hr_onboarding_common.llm import input_block
    from sub_agent.services.welcome import PersonalNoteOutput

    completion, seen = _reasoned(offline_llm().handlers["onboarding.welcome_note"])
    note = SanitizedLLM(_gateway(completion)).structured(
        task="onboarding.welcome_note", tier=ModelTier.FAST, schema=PersonalNoteOutput, system="s",
        user=input_block({"full_name": "Asha Rao", "role_title": "Analyst"}), max_tokens=300)
    assert isinstance(note, PersonalNoteOutput)
    assert seen[0]["model"] == DEEPSEEK_ROUTE and seen[0]["max_tokens"] == 300 + 2048


def test_reasoning_using_the_whole_budget_reaches_uc05_as_the_generic_error(deepseek):
    def completion(**kwargs):
        message = SimpleNamespace(content="", reasoning_content=REASONING_TEXT, provider_specific_fields={})
        usage = SimpleNamespace(prompt_tokens=40, completion_tokens=2348,
                                completion_tokens_details=SimpleNamespace(reasoning_tokens=2348))
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length", message=message)], usage=usage)

    with pytest.raises(LLMError) as exc:
        answer_policy_question(SanitizedLLM(_gateway(completion)), PolicyKnowledgeBase(), AuditLog(),
                               question="What is the dress code?", client_id="CLIENT-ACME",
                               as_of=date(2026, 11, 1), employee_id="EMP-001")
    assert str(exc.value) == SanitizedLLM.GENERIC_MESSAGE and REASONING_TEXT not in str(exc.value)


def test_a_conversation_passes_through_uc05s_wrapper_and_stays_in_its_scope(deepseek):
    from hr_onboarding_common.llm import ConversationScopeError, LLMConversation
    from sub_agent.services.welcome import PersonalNoteOutput

    completion, seen = _reasoned(offline_llm().handlers["onboarding.welcome_note"])
    llm, scope = SanitizedLLM(_gateway(completion)), ("CLIENT-ACME", "EMP-001", "welcome")
    conversation = LLMConversation(scope=scope)
    ask = dict(task="onboarding.welcome_note", tier=ModelTier.FAST, schema=PersonalNoteOutput, system="s",
               user='<input>\n{"full_name": "Asha Rao", "role_title": "Analyst"}\n</input>', max_tokens=300)
    llm.structured(**ask, conversation=conversation, scope=scope)
    llm.structured(**ask, conversation=conversation, scope=scope)
    assert seen[1]["messages"][2]["reasoning_details"][0]["text"] == REASONING_TEXT  # back to the provider only
    with pytest.raises(ConversationScopeError):
        llm.structured(**ask, conversation=conversation, scope=("CLIENT-GLOBEX", "EMP-101", "welcome"))
    assert len(seen) == 2


def test_uc05s_own_litellm_sends_deepseek_with_reasoning_to_openrouter(deepseek, monkeypatch):
    """UC05's runtime as built by build_context, on the LiteLLM installed in UC05's environment, with
    only HTTP replaced: what reaches OpenRouter for the policy Q&A."""
    import json

    import httpx
    import litellm

    handler = offline_llm().handlers["onboarding.policy_qa"]
    requests: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        content = handler(body["messages"][0]["content"], body["messages"][-1]["content"], PolicyQAModelOutput)
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": DEEPSEEK,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": PolicyQAModelOutput.model_validate(content).model_dump_json(),
                "reasoning": REASONING_TEXT}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 90, "total_tokens": 130,
                      "completion_tokens_details": {"reasoning_tokens": 60}}})

    original = httpx.Client.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(reply)
        original(self, *args, **kwargs)

    cache = getattr(litellm, "in_memory_llm_clients_cache", None)
    if cache is not None and hasattr(cache, "flush_cache"):
        cache.flush_cache()
    monkeypatch.setattr(httpx.Client, "__init__", init)
    ctx, _ = build_context()
    result = answer_policy_question(ctx.llm, PolicyKnowledgeBase(), AuditLog(),
                                    question="What is the dress code?", client_id="CLIENT-ACME",
                                    as_of=date(2026, 11, 1), employee_id="EMP-001")
    if cache is not None and hasattr(cache, "flush_cache"):
        cache.flush_cache()
    assert result.answer is not None and REASONING_TEXT not in result.model_dump_json()
    (request,) = requests
    body = json.loads(request.content)
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert body["model"] == DEEPSEEK and body["max_tokens"] == 4000 + 2048
    assert body["reasoning"] == {"enabled": True, "exclude": True}
    assert body["provider"] == {"require_parameters": True} and body["response_format"]["type"] == "json_schema"
