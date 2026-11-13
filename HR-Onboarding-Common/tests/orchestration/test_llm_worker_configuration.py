"""The REAL UC01, UC04 and UC05 worker processes: what LLM each one runs with (`__llm_status__`) and
which provider credentials reach it. No model is called: online mode is configured with a fake key
and only constructed (UC05 builds its LiteLLMGateway at startup; a call would need a real key)."""

from __future__ import annotations

import os

import pytest

from _helpers import UC_ROOTS, require_uc, venv_python
from hr_onboarding_common.orchestration.contracts import Actor
from hr_onboarding_common.orchestration.workers.config import WorkerConfig
from hr_onboarding_common.orchestration.workers.uc_worker_client import UcWorker

FAKE_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"  # never valid: nothing here may reach OpenRouter
TIERS = {"LLM_MODEL_FRONTIER": "openrouter/acme/frontier-1", "LLM_MODEL_WORKHORSE": "openrouter/acme/workhorse-1",
         "LLM_MODEL_FAST": "openrouter/acme/fast-1"}
SYSTEM = Actor(on_behalf_of="svc:test", roles=["hr_admin"])


def _status(uc_id: str) -> dict:
    require_uc(uc_id)
    worker = UcWorker(WorkerConfig(uc_id=uc_id, python_executable=venv_python(UC_ROOTS[uc_id]),
                                   src_dir=UC_ROOTS[uc_id] / "src"))
    worker.start()
    try:
        response = worker.call("__llm_status__", {}, actor=SYSTEM)
    finally:
        worker.stop()
    assert response.ok, f"{response.error_type}: {response.error_detail}"
    return response.result


@pytest.fixture
def parent_env(monkeypatch):
    """The orchestrator's environment as if HR-Onboarding-Common/.env said online + OpenRouter."""
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    for name, value in TIERS.items():
        monkeypatch.setenv(name, value)
    for name in ("LLM_FALLBACKS_FRONTIER", "LLM_FALLBACKS_WORKHORSE", "LLM_FALLBACKS_FAST", "UC05_WORKER_LLM_MODE"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("uc_id", ["uc01", "uc04"])
def test_workers_that_make_no_model_calls_never_receive_the_key(parent_env, uc_id):
    parent_env.setenv("LLM_MODE", "online")
    status = _status(uc_id)
    assert status["implementation"] is None and "makes no model calls" in status["detail"]
    assert status["credentials_present"]["OPENROUTER_API_KEY"] is False
    assert status["llm_mode"] == "online"  # the rest of the configuration is shared


def test_the_uc05_worker_follows_llm_mode_offline(parent_env):
    parent_env.setenv("LLM_MODE", "offline")
    status = _status("uc05")
    assert status["implementation"] == "FakeLLM" and status["llm_mode"] == "offline"


def test_the_uc05_worker_builds_the_openrouter_gateway_online(parent_env):
    parent_env.setenv("LLM_MODE", "online")
    status = _status("uc05")
    assert status["implementation"] == "LiteLLMGateway"
    assert status["models"] == {"frontier": "openrouter/acme/frontier-1", "workhorse": "openrouter/acme/workhorse-1",
                                "fast": "openrouter/acme/fast-1"}
    assert status["credentials_present"]["OPENROUTER_API_KEY"] is True
    assert FAKE_KEY not in str(status)


def test_the_uc05_worker_runs_deepseek_through_openrouter_with_reasoning(parent_env):
    """The documented central configuration: one OpenRouter model id for every tier, reasoning on."""
    parent_env.setenv("LLM_MODE", "online")
    for name in TIERS:
        parent_env.delenv(name)
    parent_env.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash")
    parent_env.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    parent_env.setenv("LLM_REASONING_ENABLED", "true")
    status = _status("uc05")
    assert status["implementation"] == "LiteLLMGateway" and status["provider"] == "openrouter"
    assert set(status["models"].values()) == {"deepseek/deepseek-v4.1-flash"}  # the OpenRouter model id
    assert set(status["routes"].values()) == {"openrouter/deepseek/deepseek-v4.1-flash"}  # the LiteLLM route
    assert status["fallback_routes"] == {"frontier": [], "workhorse": [], "fast": []}
    assert status["base_url"] == "https://openrouter.ai/api/v1" and status["require_parameters"] is True
    assert status["reasoning"] == {"enabled": True, "effort": None, "token_headroom": 2048}
    assert FAKE_KEY not in str(status)


def test_uc05_worker_llm_mode_overrides_the_central_mode_for_that_worker_only(parent_env):
    parent_env.setenv("LLM_MODE", "online")
    parent_env.setenv("UC05_WORKER_LLM_MODE", "offline")
    assert _status("uc05")["implementation"] == "FakeLLM"
    assert os.environ["LLM_MODE"] == "online"  # the orchestrator's own mode is untouched


def test_an_invalid_online_configuration_fails_the_uc05_worker_clearly(parent_env):
    parent_env.setenv("LLM_MODE", "online")
    parent_env.delenv("LLM_MODEL_FAST")
    parent_env.delenv("OPENROUTER_MODEL", raising=False)  # "neither ... nor OPENROUTER_MODEL": an earlier load_environment() may have set it
    parent_env.setenv("OPENROUTER_API_KEY", "PASTE_YOUR_OPENROUTER_API_KEY_HERE")
    require_uc("uc05")
    worker = UcWorker(WorkerConfig(uc_id="uc05", python_executable=venv_python(UC_ROOTS["uc05"]),
                                   src_dir=UC_ROOTS["uc05"] / "src"))
    worker.start()
    try:
        response = worker.call("__llm_status__", {}, actor=SYSTEM)
    finally:
        worker.stop()
    assert not response.ok and response.error_type == "RuntimeConfigurationError"
    assert "neither LLM_MODEL_FAST nor OPENROUTER_MODEL is set" in response.error_detail
    assert "OPENROUTER_API_KEY still holds the placeholder" in response.error_detail
    assert "PASTE_YOUR" not in response.error_detail
