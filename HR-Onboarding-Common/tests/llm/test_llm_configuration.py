"""LLM configuration: LLM_MODE, the OpenRouter key, model ids and reasoning settings from the
environment, startup validation, and the one .env file. No network: online mode is only validated and
constructed here. All key values below are fake."""

from __future__ import annotations

import os

import pytest

from hr_onboarding_common.config import ENV_FILE_VAR, Settings, load_environment
from hr_onboarding_common.llm import (FakeLLM, LiteLLMGateway, LLMConfigurationError, ModelTier, build_llm, llm_mode,
                                      llm_summary, openrouter_model_id, validate_online_config)

FAKE_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"  # shaped like a key, not a key
TIERS = {"LLM_MODEL_FRONTIER": "openrouter/acme/frontier-1", "LLM_MODEL_WORKHORSE": "openrouter/acme/workhorse-1",
         "LLM_MODEL_FAST": "openrouter/acme/fast-1"}
DEEPSEEK = "deepseek/deepseek-v4.1-flash"  # the OpenRouter model id
DEEPSEEK_ROUTE = "openrouter/deepseek/deepseek-v4.1-flash"  # the LiteLLM route the gateway calls
LLM_VARIABLES = ("LLM_MODE", "OPENROUTER_API_KEY", *TIERS, "LLM_FALLBACKS_FRONTIER", "LLM_FALLBACKS_WORKHORSE",
                 "LLM_FALLBACKS_FAST", "LLM_TIMEOUT_S", "LLM_MAX_ATTEMPTS", "LLM_BACKOFF_INITIAL_S", "LLM_BACKOFF_MAX_S",
                 "LLM_PROVIDER", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL", "OPENROUTER_REQUIRE_PARAMETERS",
                 "LLM_REASONING_ENABLED", "LLM_REASONING_EFFORT", "LLM_REASONING_TOKEN_HEADROOM")


@pytest.fixture
def env(monkeypatch):
    """A clean LLM environment; `env(**vars)` sets variables for this test only."""
    for name in LLM_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    def set_vars(**values: str) -> None:
        for name, value in values.items():
            monkeypatch.setenv(name, value)
    return set_vars


def _online(env, **over):
    env(**{"LLM_MODE": "online", "OPENROUTER_API_KEY": FAKE_KEY, **TIERS, **over})


# ------------------------------------------------------------------ modes


def test_offline_is_the_default_and_needs_no_key(env):
    assert llm_mode() == "offline"
    offline = FakeLLM()
    assert build_llm(Settings(), offline=lambda: offline) is offline
    assert isinstance(build_llm(Settings()), FakeLLM)  # no offline LLM given: a bare FakeLLM


def test_online_and_its_live_alias_select_the_gateway(env):
    for mode in ("online", "ONLINE", "live"):
        _online(env, LLM_MODE=mode)
        assert isinstance(build_llm(Settings()), LiteLLMGateway)


def test_an_unknown_mode_is_refused(env):
    env(LLM_MODE="sometimes")
    with pytest.raises(LLMConfigurationError, match="LLM_MODE='sometimes'"):
        build_llm(Settings())


# ------------------------------------------------------------------ key and tiers


def test_the_openrouter_key_and_model_tiers_are_read_from_the_environment(env):
    _online(env, LLM_FALLBACKS_FAST="openrouter/acme/fast-2,openrouter/other/fast-3")
    gateway = build_llm(Settings())
    assert isinstance(gateway, LiteLLMGateway)
    assert {tier: gateway.model_for(tier) for tier in ModelTier} == {
        ModelTier.FRONTIER: "openrouter/acme/frontier-1", ModelTier.WORKHORSE: "openrouter/acme/workhorse-1",
        ModelTier.FAST: "openrouter/acme/fast-1"}
    assert gateway.fallbacks_for(ModelTier.FAST) == ("openrouter/acme/fast-2", "openrouter/other/fast-3")


def test_built_in_fallbacks_never_apply_online(env):
    """Settings' defaults name models nobody configured (anthropic/...); online, an unset
    LLM_FALLBACKS_<TIER> means no fallback, never a call to a provider without a key."""
    _online(env)
    assert Settings().fallbacks_frontier  # the offline label defaults are still there
    gateway = build_llm(Settings())
    assert all(gateway.fallbacks_for(tier) == () for tier in ModelTier)


@pytest.mark.parametrize("missing", ["OPENROUTER_API_KEY", "LLM_MODEL_FRONTIER", "LLM_MODEL_WORKHORSE",
                                     "LLM_MODEL_FAST"])
def test_online_without_the_key_or_a_tier_fails_at_startup_naming_the_variable(env, monkeypatch, missing):
    _online(env)
    monkeypatch.delenv(missing)
    with pytest.raises(LLMConfigurationError) as exc:
        build_llm(Settings())
    expected = "OPENROUTER_API_KEY is not set" if missing == "OPENROUTER_API_KEY" else \
        f"neither {missing} nor OPENROUTER_MODEL is set"
    assert expected in str(exc.value)
    assert FAKE_KEY not in str(exc.value)


def test_placeholders_from_the_example_file_are_refused(env):
    _online(env, OPENROUTER_API_KEY="PASTE_YOUR_OPENROUTER_API_KEY_HERE", LLM_MODEL_FAST="openrouter/<provider>/<model>")
    with pytest.raises(LLMConfigurationError) as exc:
        build_llm(Settings())
    message = str(exc.value)
    assert "OPENROUTER_API_KEY still holds the placeholder" in message
    assert "LLM_MODEL_FAST still holds a placeholder" in message
    assert "PASTE_YOUR" not in message  # the value itself is never repeated


@pytest.mark.parametrize("model", ["deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash/extra",
                                   "openrouter/deepseek/model with space", "openrouter/deepseek/", "/deepseek-v4.1-flash"])
def test_unusable_openrouter_model_ids_are_refused(env, model):
    _online(env, LLM_MODEL_WORKHORSE=model)
    with pytest.raises(LLMConfigurationError, match="LLM_MODEL_WORKHORSE") as exc:
        build_llm(Settings())
    assert "an OpenRouter model id must read <provider>/<model>" in str(exc.value)


@pytest.mark.parametrize("model, reason", [
    ("openrouter/gpt-4o-mini", "openrouter/<provider>/<model>"),
    ("bedrock/acme/model", "not supported"),
    ("openrouter/acme/model with space", "openrouter/<provider>/<model>"),
])
def test_unusable_litellm_routes_are_refused(env, model, reason):
    _online(env, LLM_PROVIDER="litellm", LLM_MODEL_WORKHORSE=model)
    with pytest.raises(LLMConfigurationError, match="LLM_MODEL_WORKHORSE") as exc:
        build_llm(Settings())
    assert reason in str(exc.value)


def test_a_key_with_whitespace_or_quotes_is_refused_without_echoing_it(env):
    _online(env, OPENROUTER_API_KEY=f'"{FAKE_KEY}"')
    with pytest.raises(LLMConfigurationError, match="contains whitespace or quotes") as exc:
        build_llm(Settings())
    assert FAKE_KEY not in str(exc.value)


def test_execution_settings_are_validated(env):
    _online(env, LLM_TIMEOUT_S="0", LLM_MAX_ATTEMPTS="0", LLM_BACKOFF_INITIAL_S="5", LLM_BACKOFF_MAX_S="1")
    with pytest.raises(LLMConfigurationError) as exc:
        validate_online_config(Settings())
    for variable in ("LLM_TIMEOUT_S", "LLM_MAX_ATTEMPTS", "LLM_BACKOFF_MAX_S"):
        assert variable in str(exc.value)


def test_other_litellm_providers_need_their_own_key(env):
    """With LLM_PROVIDER=litellm, models are LiteLLM routes and each prefix needs its own key. (With
    the default LLM_PROVIDER=openrouter, "anthropic/..." is an OpenRouter model id and needs only
    OPENROUTER_API_KEY.)"""
    _online(env, LLM_PROVIDER="litellm", LLM_MODEL_FAST="anthropic/claude-haiku-4-5")
    with pytest.raises(LLMConfigurationError, match="ANTHROPIC_API_KEY is not set"):
        build_llm(Settings())
    _online(env, LLM_PROVIDER="openrouter", LLM_MODEL_FAST="anthropic/claude-haiku-4.5")
    assert build_llm(Settings()).route_for("anthropic/claude-haiku-4.5") == "openrouter/anthropic/claude-haiku-4.5"


# ------------------------------------------------------------------ OpenRouter DeepSeek V4.1 Flash


def _deepseek(env, **over):
    """The documented configuration (.env.example), with a fake key."""
    env(**{"LLM_MODE": "online", "OPENROUTER_API_KEY": FAKE_KEY, "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
           "OPENROUTER_MODEL": DEEPSEEK, "LLM_MODEL_FRONTIER": DEEPSEEK, "LLM_MODEL_WORKHORSE": DEEPSEEK,
           "LLM_MODEL_FAST": DEEPSEEK, "LLM_REASONING_ENABLED": "true", **over})


def test_the_documented_deepseek_configuration_is_valid_and_keeps_model_id_and_route_apart(env):
    _deepseek(env)
    gateway = build_llm(Settings())
    assert isinstance(gateway, LiteLLMGateway)
    assert {gateway.model_for(tier) for tier in ModelTier} == {DEEPSEEK}  # what is configured: the model id
    assert {gateway.route_for(gateway.model_for(tier)) for tier in ModelTier} == {DEEPSEEK_ROUTE}  # what is called
    s = gateway.settings
    assert (s.llm_provider, s.openrouter_base_url, s.openrouter_model) == ("openrouter", "https://openrouter.ai/api/v1",
                                                                           DEEPSEEK)
    assert s.llm_reasoning_enabled and s.openrouter_require_parameters and s.llm_reasoning_effort == ""
    assert all(gateway.fallbacks_for(tier) == () for tier in ModelTier)  # no fallback unless configured


def test_openrouter_model_alone_configures_every_tier_and_a_tier_variable_overrides_it(env):
    env(LLM_MODE="online", OPENROUTER_API_KEY=FAKE_KEY, OPENROUTER_MODEL=DEEPSEEK)
    gateway = build_llm(Settings())
    assert {gateway.model_for(tier) for tier in ModelTier} == {DEEPSEEK}
    env(LLM_MODEL_FRONTIER="anthropic/claude-opus-5")
    assert build_llm(Settings()).model_for(ModelTier.FRONTIER) == "anthropic/claude-opus-5"


def test_the_route_prefix_is_optional_and_openrouters_own_ids_are_kept():
    assert openrouter_model_id(DEEPSEEK) == openrouter_model_id(DEEPSEEK_ROUTE) == DEEPSEEK
    assert openrouter_model_id("openrouter/auto") == "openrouter/auto"  # an OpenRouter catalog id itself


def test_the_status_summary_names_ids_routes_and_reasoning_but_no_credential(env):
    _deepseek(env, LLM_FALLBACKS_FAST="acme/fallback-1")
    summary = llm_summary(build_llm(Settings()).settings)
    assert summary["provider"] == "openrouter" and summary["base_url"] == "https://openrouter.ai/api/v1"
    assert summary["models"] == {"frontier": DEEPSEEK, "workhorse": DEEPSEEK, "fast": DEEPSEEK}
    assert summary["routes"] == {"frontier": DEEPSEEK_ROUTE, "workhorse": DEEPSEEK_ROUTE, "fast": DEEPSEEK_ROUTE}
    assert summary["fallback_routes"]["fast"] == ["openrouter/acme/fallback-1"]
    assert summary["reasoning"] == {"enabled": True, "effort": None, "token_headroom": 2048}
    assert FAKE_KEY not in repr(summary)


@pytest.mark.parametrize("url", ["http://openrouter.ai/api/v1", "openrouter.ai/api/v1",
                                 "https://user:secret-part@openrouter.ai/api/v1", "https://openrouter.ai/api/v1?k=v"])
def test_an_unusable_base_url_is_refused_without_echoing_it(env, url):
    _deepseek(env, OPENROUTER_BASE_URL=url)
    with pytest.raises(LLMConfigurationError, match="OPENROUTER_BASE_URL must") as exc:
        build_llm(Settings())
    assert not any(part in str(exc.value) for part in ("secret-part", "?k=v", "http://"))  # the value is not echoed


@pytest.mark.parametrize("over, problem", [
    ({"LLM_REASONING_ENABLED": "maybe"}, "LLM_REASONING_ENABLED must be true or false"),
    ({"OPENROUTER_REQUIRE_PARAMETERS": "sometimes"}, "OPENROUTER_REQUIRE_PARAMETERS must be true or false"),
    ({"LLM_REASONING_EFFORT": "extreme"}, "LLM_REASONING_EFFORT must be one of"),
    ({"LLM_REASONING_EFFORT": "high", "LLM_REASONING_ENABLED": "false"}, "LLM_REASONING_ENABLED is false"),
    ({"LLM_REASONING_TOKEN_HEADROOM": "-1"}, "LLM_REASONING_TOKEN_HEADROOM must be 0 or more"),
    ({"LLM_PROVIDER": "deepseek"}, "LLM_PROVIDER must be one of"),
])
def test_malformed_reasoning_and_provider_settings_are_refused(env, over, problem):
    _deepseek(env, **over)
    with pytest.raises(LLMConfigurationError, match=problem):
        build_llm(Settings())


@pytest.mark.parametrize("over, enabled, effort", [
    ({}, True, ""), ({"LLM_REASONING_ENABLED": "false"}, False, ""), ({"LLM_REASONING_ENABLED": "0"}, False, ""),
    ({"LLM_REASONING_EFFORT": "HIGH"}, True, "high"), ({"LLM_REASONING_EFFORT": "low"}, True, "low"),
])
def test_reasoning_can_be_switched_off_or_given_an_effort(env, over, enabled, effort):
    _deepseek(env, **over)
    s = build_llm(Settings()).settings
    assert (s.llm_reasoning_enabled, s.llm_reasoning_effort) == (enabled, effort)


def test_reasoning_requested_for_routes_outside_openrouter_is_refused(env, monkeypatch):
    """Reasoning is sent on OpenRouter routes only: asking for it on a direct provider route fails at
    startup instead of being silently dropped. Left at its default it does not block such a route."""
    litellm = {"LLM_PROVIDER": "litellm", "ANTHROPIC_API_KEY": FAKE_KEY, "LLM_MODEL_FAST": "anthropic/claude-haiku-4-5"}
    _online(env, **litellm, LLM_REASONING_ENABLED="true")
    with pytest.raises(LLMConfigurationError, match="apply to OpenRouter routes only"):
        build_llm(Settings())
    monkeypatch.delenv("LLM_REASONING_ENABLED")
    assert isinstance(build_llm(Settings()), LiteLLMGateway)


def test_explicit_fallbacks_are_validated_like_primary_models(env):
    _deepseek(env, LLM_FALLBACKS_WORKHORSE="acme/fallback-1,not-a-model-id")
    with pytest.raises(LLMConfigurationError, match="LLM_FALLBACKS_WORKHORSE: an OpenRouter model id must read"):
        build_llm(Settings())


# ------------------------------------------------------------------ the .env file


def test_environment_variables_override_the_env_file_and_its_path_is_exported(env, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_MODEL_FAST=openrouter/file/fast\nLLM_MODEL_FRONTIER=openrouter/file/frontier\n",
                        encoding="utf-8")
    monkeypatch.setenv(ENV_FILE_VAR, str(env_file))
    env(LLM_MODEL_FAST="openrouter/environment/fast")
    assert load_environment() == env_file
    assert os.environ["LLM_MODEL_FAST"] == "openrouter/environment/fast"  # the real environment wins
    assert os.environ["LLM_MODEL_FRONTIER"] == "openrouter/file/frontier"  # the file fills the rest
    assert os.environ[ENV_FILE_VAR] == str(env_file)  # inherited by every worker started afterwards


def test_a_missing_env_file_loads_nothing(env, monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_FILE_VAR, str(tmp_path / "absent.env"))
    assert load_environment() is None
    assert llm_mode() == "offline"
