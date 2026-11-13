"""How the orchestrator gets its LLM (orchestration.bootstrap.build_deps) and what each UC worker
process is started with (WorkerConfig.child_environment). No worker is started and nothing reaches
the network here; tests/orchestration/test_llm_worker_configuration.py starts the real workers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hr_onboarding_common.config import ENV_FILE_VAR
from hr_onboarding_common.llm import FakeLLM, LiteLLMGateway, LLMConfigurationError, ModelTier
from hr_onboarding_common.orchestration.bootstrap import build_deps
from hr_onboarding_common.orchestration.workers.config import WorkerConfig, load_worker_config

COMMON = Path(__file__).resolve().parents[2]
WORKSPACE = COMMON.parent
FAKE_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"
ONLINE = {"LLM_MODE": "online", "OPENROUTER_API_KEY": FAKE_KEY, "LLM_MODEL_FRONTIER": "openrouter/acme/frontier-1",
          "LLM_MODEL_WORKHORSE": "openrouter/acme/workhorse-1", "LLM_MODEL_FAST": "openrouter/acme/fast-1"}


@pytest.fixture
def online(monkeypatch):
    for name, value in ONLINE.items():
        monkeypatch.setenv(name, value)
    for name in ("LLM_FALLBACKS_FRONTIER", "LLM_FALLBACKS_WORKHORSE", "LLM_FALLBACKS_FAST"):
        monkeypatch.delenv(name, raising=False)


def _deps(tmp_path, **kw):
    return build_deps(agent_home=COMMON, uc03_repo_root=WORKSPACE / "UC03_Document_Validation", ucs=(), **kw)


def test_the_orchestrator_is_offline_by_default(tmp_path):
    ctx, deps = _deps(tmp_path)
    assert isinstance(ctx.llm, FakeLLM) and deps.ctx is ctx


def test_online_the_orchestrator_gets_the_validated_gateway(online, tmp_path):
    ctx, _ = _deps(tmp_path)
    assert isinstance(ctx.llm, LiteLLMGateway)
    assert ctx.llm.model_for(ModelTier.FRONTIER) == "openrouter/acme/frontier-1"


def test_online_the_orchestrator_runs_every_tier_on_deepseek_with_reasoning(online, monkeypatch, tmp_path):
    """The documented configuration: OPENROUTER_MODEL for every tier, reasoning on."""
    for name in ("LLM_MODEL_FRONTIER", "LLM_MODEL_WORKHORSE", "LLM_MODEL_FAST"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash")
    monkeypatch.setenv("LLM_REASONING_ENABLED", "true")
    ctx, _ = _deps(tmp_path)
    assert isinstance(ctx.llm, LiteLLMGateway) and ctx.llm.settings.llm_reasoning_enabled
    assert {ctx.llm.route_for(ctx.llm.model_for(tier)) for tier in ModelTier} == {
        "openrouter/deepseek/deepseek-v4.1-flash"}


def test_invalid_online_configuration_stops_startup_before_any_worker(online, monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setenv("UC05_WORKER_PYTHON", str(tmp_path / "no-python.exe"))  # would fail if it were reached
    with pytest.raises(LLMConfigurationError, match="OPENROUTER_API_KEY is not set"):
        build_deps(agent_home=COMMON, uc03_repo_root=WORKSPACE / "UC03_Document_Validation", ucs=("uc05",))


# ------------------------------------------------------------------ worker environments


def _config(uc_id: str, **kw) -> WorkerConfig:
    src = {"uc01": "UC01_Employee_Record_Creation", "uc04": "UC04_Equipment_Access_Integration_Ready",
           "uc05": "UC05_Orientation-Schedule-Agent"}[uc_id]
    return WorkerConfig(uc_id=uc_id, python_executable=Path(sys.executable), src_dir=WORKSPACE / src / "src", **kw)


def test_only_workers_that_call_a_model_receive_the_provider_credentials():
    parent = {**ONLINE, "ANTHROPIC_API_KEY": "anthropic-fake-value", ENV_FILE_VAR: "x/.env", "HR_AGENT_TZ": "Asia/Dubai"}
    uc01, uc04, uc05 = (_config(uc).child_environment(parent) for uc in ("uc01", "uc04", "uc05"))
    for env in (uc01, uc04):
        assert "OPENROUTER_API_KEY" not in env and "ANTHROPIC_API_KEY" not in env
        assert env["LLM_MODE"] == "online" and env["HR_AGENT_TZ"] == "Asia/Dubai"  # everything else is shared
    assert uc05["OPENROUTER_API_KEY"] == FAKE_KEY and uc05[ENV_FILE_VAR] == "x/.env"
    assert parent["OPENROUTER_API_KEY"] == FAKE_KEY  # the orchestrator's own environment is not changed


def test_llm_access_is_configurable_per_worker(monkeypatch):
    assert _config("uc01", llm_access=True).child_environment(ONLINE)["OPENROUTER_API_KEY"] == FAKE_KEY
    assert "OPENROUTER_API_KEY" not in _config("uc05", llm_access=False).child_environment(ONLINE)
    monkeypatch.setenv("UC04_WORKER_PYTHON", sys.executable)
    monkeypatch.setenv("UC04_WORKER_SRC_DIR", str(WORKSPACE / "UC04_Equipment_Access_Integration_Ready" / "src"))
    assert load_worker_config("uc04").has_llm_access is False
    monkeypatch.setenv("UC04_WORKER_LLM_ACCESS", "1")
    assert load_worker_config("uc04").has_llm_access is True
