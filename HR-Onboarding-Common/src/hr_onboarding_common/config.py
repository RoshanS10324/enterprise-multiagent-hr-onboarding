"""Runtime settings, loaded from the environment and the project's one .env file (`load_environment`).

Forked from hr_recruitment_common.config, trimmed: `skills_dirs`, `context_dir`, and the
module-level `PACKAGE_DATA` constant are dropped -- they existed only to support `SkillLibrary`
and `Brain.load(...)`, neither of which is ported (see docs/ONBOARDING_COMMON_ARCHITECTURE.md A.3).

One .env for the whole system: `HR-Onboarding-Common/.env` (or the file `HR_ONBOARDING_ENV_FILE`
names). Real environment variables always win over it. Once loaded, its path is exported in
`HR_ONBOARDING_ENV_FILE`, so every UC worker process started afterwards reads the same file --
never a different .env found by searching from wherever that process happens to run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE_VAR = "HR_ONBOARDING_ENV_FILE"
# HR-Onboarding-Common/ when this package runs from its source tree (src/hr_onboarding_common/config.py);
# an installed copy (site-packages) has no project .env of its own and relies on ENV_FILE_VAR.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def project_env_file() -> Path | None:
    """The .env this process should read: the one ENV_FILE_VAR names, else the project's own."""
    explicit = os.environ.get(ENV_FILE_VAR, "").strip()
    if explicit:
        return Path(explicit)
    if (_PROJECT_ROOT / "src" / "hr_onboarding_common").is_dir():
        return _PROJECT_ROOT / ".env"
    return None


def load_environment() -> Path | None:
    """Load the project's .env into os.environ without overriding variables already set, and export
    its path so child processes load the same file. Returns the file loaded, or None when there is
    none (the real environment alone then configures the process)."""
    path = project_env_file()
    if path is None or not path.is_file():
        return None
    load_dotenv(path, override=False)
    os.environ[ENV_FILE_VAR] = str(path)
    return path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in _env(name, default).split(",") if x.strip())


def _first(*names: str, default: str) -> str:
    """The first of `names` set to a non-empty value, else `default`."""
    return next((os.environ[n].strip() for n in names if os.environ.get(n, "").strip()), default)


def _flag(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Agent project root: holds guardrails/. None = shared package only.
    agent_home: Path | None = None
    data_dir: Path = field(default_factory=lambda: Path(_env("HR_AGENT_DATA_DIR", ".dact")))

    # "offline" (FakeLLM / an agent's deterministic offline LLM; no network, no key) or "online"
    # (LiteLLMGateway; validated by llm.factory before use). "live" is accepted as "online".
    llm_mode: str = field(default_factory=lambda: _env("LLM_MODE", "offline").strip().lower())

    # Who serves the models online. "openrouter" (default): every model below is an OpenRouter model id
    # ("<provider>/<model>", e.g. "deepseek/deepseek-v4.1-flash"; an "openrouter/" prefix is accepted),
    # which the gateway calls through LiteLLM as "openrouter/<id>" (llm.routing). "litellm": every
    # model below is a LiteLLM route used as given ("anthropic/...", "gemini/...").
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "openrouter").strip().lower())
    openrouter_base_url: str = field(
        default_factory=lambda: _first("OPENROUTER_BASE_URL", default="https://openrouter.ai/api/v1"))
    # The model every tier uses unless LLM_MODEL_<TIER> names another one.
    openrouter_model: str = field(default_factory=lambda: _first("OPENROUTER_MODEL", default=""))
    # Only route to OpenRouter providers that support every parameter of the request (structured output
    # and reasoning): some providers of a model do not support JSON-schema output.
    openrouter_require_parameters: bool = field(default_factory=lambda: _flag("OPENROUTER_REQUIRE_PARAMETERS", True))

    # Model per tier; agents reference tiers, never models. The defaults below only label offline runs:
    # online mode requires each tier to be configured (llm.factory.validate_online_config).
    model_frontier: str = field(
        default_factory=lambda: _first("LLM_MODEL_FRONTIER", "OPENROUTER_MODEL", default="anthropic/claude-opus-5"))
    model_workhorse: str = field(
        default_factory=lambda: _first("LLM_MODEL_WORKHORSE", "OPENROUTER_MODEL", default="anthropic/claude-sonnet-5"))
    model_fast: str = field(
        default_factory=lambda: _first("LLM_MODEL_FAST", "OPENROUTER_MODEL", default="anthropic/claude-haiku-4-5"))
    # Fallback chains, tried in order when the primary model fails after retries.
    fallbacks_frontier: tuple[str, ...] = field(
        default_factory=lambda: _csv("LLM_FALLBACKS_FRONTIER", "anthropic/claude-sonnet-5"))
    fallbacks_workhorse: tuple[str, ...] = field(
        default_factory=lambda: _csv("LLM_FALLBACKS_WORKHORSE", "anthropic/claude-opus-5"))
    fallbacks_fast: tuple[str, ...] = field(
        default_factory=lambda: _csv("LLM_FALLBACKS_FAST", "anthropic/claude-sonnet-5"))

    llm_timeout_s: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT_S", "120")))
    llm_max_attempts: int = field(default_factory=lambda: int(_env("LLM_MAX_ATTEMPTS", "3")))
    llm_backoff_initial_s: float = field(default_factory=lambda: float(_env("LLM_BACKOFF_INITIAL_S", "1")))
    llm_backoff_max_s: float = field(default_factory=lambda: float(_env("LLM_BACKOFF_MAX_S", "30")))
    # OpenRouter reasoning (llm.gateway): sent as `reasoning: {"enabled": true}`, or `{"effort": ...}` when
    # LLM_REASONING_EFFORT is set. Reasoning tokens count against max_tokens, so each call's budget is
    # raised by LLM_REASONING_TOKEN_HEADROOM while reasoning is on.
    llm_reasoning_enabled: bool = field(default_factory=lambda: _flag("LLM_REASONING_ENABLED", True))
    llm_reasoning_effort: str = field(default_factory=lambda: _env("LLM_REASONING_EFFORT", "").strip().lower())
    llm_reasoning_token_headroom: int = field(
        default_factory=lambda: int(_env("LLM_REASONING_TOKEN_HEADROOM", "2048")))
    # Optional LiteLLM-native callbacks (e.g. "otel", "langfuse"), comma-separated.
    litellm_callbacks: tuple[str, ...] = field(default_factory=lambda: _csv("LITELLM_CALLBACKS", ""))

    default_timezone: str = field(default_factory=lambda: _env("HR_AGENT_TZ", "Asia/Dubai"))
    jurisdiction: str = "AE"

    @property
    def policy_dir(self) -> Path | None:
        return self.agent_home / "guardrails" if self.agent_home else None


def resolve_agent_home(module_file: str) -> Path:
    """Agent project root (holds guardrails/).

    AGENT_HOME wins when set: an installed agent (container) no longer sits next to that folder.
    Otherwise it is derived from the source layout <agent>/src/<package>/agent.py.
    """
    if os.environ.get("AGENT_HOME"):
        return Path(os.environ["AGENT_HOME"])
    return Path(module_file).resolve().parents[2]


def get_settings(agent_home: Path | None = None) -> Settings:
    load_environment()  # no-op when there is no .env; real environment variables always win
    return Settings(agent_home=agent_home)
