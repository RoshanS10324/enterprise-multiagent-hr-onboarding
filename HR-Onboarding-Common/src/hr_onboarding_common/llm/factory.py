"""Which LLM a process uses, chosen once at startup from LLM_MODE.

  offline  the caller's deterministic offline LLM (FakeLLM with its handlers); no network, no key
  online   LiteLLMGateway -- the only production path to a model -- after `validate_online_config`

Online configuration is checked before anything runs, so a missing key, an unusable model id or a
malformed reasoning setting fails at startup with a clear message instead of on the first model call.
Messages name variables, never their values.

  LLM_PROVIDER=openrouter (default)  each tier's model is an OpenRouter model id, from LLM_MODEL_<TIER>
                                     or else OPENROUTER_MODEL; the key is OPENROUTER_API_KEY
  LLM_PROVIDER=litellm               each tier's model is a LiteLLM route, from LLM_MODEL_<TIER>; the key
                                     is the one its route prefix reads (PROVIDER_CREDENTIALS)
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from hr_onboarding_common.config import Settings, get_settings, project_env_file
from hr_onboarding_common.llm.base import FakeLLM, LLMClient, ModelTier
from hr_onboarding_common.llm.callbacks import LLMCallback
from hr_onboarding_common.llm.gateway import LiteLLMGateway
from hr_onboarding_common.llm.routing import (LITELLM, OPENROUTER, PROVIDERS, is_openrouter, litellm_route,
                                              openrouter_model_id)

OFFLINE, ONLINE = "offline", "online"
_MODES = {"offline": OFFLINE, "online": ONLINE, "live": ONLINE}
# With LLM_PROVIDER=litellm: the credential each LiteLLM route prefix reads from the environment. A
# route whose prefix is not listed is refused: add it here, with its variable, to allow another provider.
PROVIDER_CREDENTIALS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY", "azure": "AZURE_API_KEY",
}
TIER_VARIABLES: dict[ModelTier, tuple[str, str]] = {
    ModelTier.FRONTIER: ("LLM_MODEL_FRONTIER", "LLM_FALLBACKS_FRONTIER"),
    ModelTier.WORKHORSE: ("LLM_MODEL_WORKHORSE", "LLM_FALLBACKS_WORKHORSE"),
    ModelTier.FAST: ("LLM_MODEL_FAST", "LLM_FALLBACKS_FAST"),
}
# OpenRouter's `reasoning.effort` values. Which of them a model accepts is listed per model in the
# OpenRouter catalog (`reasoning.supported_efforts`); this list only rejects values OpenRouter never takes.
REASONING_EFFORTS = ("max", "xhigh", "high", "medium", "low", "minimal", "none")
_FLAGS = ("LLM_REASONING_ENABLED", "OPENROUTER_REQUIRE_PARAMETERS")
_FLAG_VALUES = {"1", "true", "yes", "on", "0", "false", "no", "off"}
_MODEL_ID = re.compile(r"[A-Za-z0-9][\w.\-]*/[\w.:\-]+")  # an OpenRouter catalog id: <provider>/<model>
_OPENROUTER_ROUTE = re.compile(r"openrouter/[A-Za-z0-9][\w.\-]*/[\w.:\-]+")
_PLACEHOLDER = re.compile(r"(?i)paste|your[_ -]|<[^>]*>|changeme|xxxx")


class LLMConfigurationError(RuntimeError):
    """The LLM configuration cannot work. The message names variables, never their values."""


def llm_mode(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    raw = env.get("LLM_MODE", OFFLINE).strip().lower() or OFFLINE
    if raw not in _MODES:
        raise LLMConfigurationError(f"LLM_MODE={raw!r} is not one of 'offline', 'online' ('live' = online)")
    return _MODES[raw]


def _models(settings: Settings) -> dict[ModelTier, tuple[str, tuple[str, ...]]]:
    return {ModelTier.FRONTIER: (settings.model_frontier, settings.fallbacks_frontier),
            ModelTier.WORKHORSE: (settings.model_workhorse, settings.fallbacks_workhorse),
            ModelTier.FAST: (settings.model_fast, settings.fallbacks_fast)}


def _model_problem(variable: str, model: str, provider: str) -> str | None:
    if _PLACEHOLDER.search(model):
        return f"{variable} still holds a placeholder; set a model id from the OpenRouter catalog"
    if provider == OPENROUTER:
        if not _MODEL_ID.fullmatch(openrouter_model_id(model)):
            return f"{variable}: an OpenRouter model id must read <provider>/<model>, e.g. deepseek/deepseek-v4.1-flash"
        return None
    prefix = model.split("/", 1)[0]
    if prefix not in PROVIDER_CREDENTIALS:
        return f"{variable}: provider route {prefix!r} is not supported (use one of {sorted(PROVIDER_CREDENTIALS)})"
    if prefix == "openrouter" and not _OPENROUTER_ROUTE.fullmatch(model):
        return f"{variable}: an OpenRouter route must read openrouter/<provider>/<model>"
    return None


def _primary_variable(model_var: str, provider: str, env: Mapping[str, str]) -> str | None:
    """The variable a tier's model comes from, or None when it is not configured."""
    if env.get(model_var, "").strip():
        return model_var
    if provider == OPENROUTER and env.get("OPENROUTER_MODEL", "").strip():
        return "OPENROUTER_MODEL"
    return None


def _key_problem(variable: str, value: str, needed_by: str) -> str | None:
    if not value.strip():
        return f"{variable} is not set (needed by {needed_by})"
    if _PLACEHOLDER.search(value):
        return f"{variable} still holds the placeholder from .env.example"
    if value != value.strip() or any(c.isspace() or c in "\"'" for c in value):
        return f"{variable} contains whitespace or quotes"
    return None


def _base_url_problem(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return "OPENROUTER_BASE_URL must be an https:// URL, e.g. https://openrouter.ai/api/v1"
    if parts.username or parts.password or parts.query or parts.fragment:
        return "OPENROUTER_BASE_URL must not contain credentials, a query or a fragment"
    return None


def validate_online_config(settings: Settings, environ: Mapping[str, str] | None = None) -> None:
    """Raises LLMConfigurationError listing every problem with the online configuration."""
    env = os.environ if environ is None else environ
    problems: list[str] = []
    provider = settings.llm_provider
    routes: list[str] = []
    credentials: dict[str, str] = {}  # variable -> what needs it
    if provider not in PROVIDERS:
        problems.append(f"LLM_PROVIDER must be one of {list(PROVIDERS)}")
    else:
        if provider == OPENROUTER:
            credentials["OPENROUTER_API_KEY"] = "LLM_PROVIDER=openrouter"
        for tier, (model_var, fallback_var) in TIER_VARIABLES.items():
            primary, fallbacks = _models(settings)[tier]
            variable = _primary_variable(model_var, provider, env)
            if variable is None:
                problems.append(f"neither {model_var} nor OPENROUTER_MODEL is set" if provider == OPENROUTER
                                else f"{model_var} is not set (OPENROUTER_MODEL applies to LLM_PROVIDER=openrouter only)")
                continue
            # Default fallbacks never apply online: only the ones configured explicitly are checked (and used).
            explicit = fallbacks if env.get(fallback_var, "").strip() else ()
            for var, model in ((variable, primary), *((fallback_var, m) for m in explicit)):
                problem = _model_problem(var, model, provider)
                if problem:
                    problems.append(problem)
                    continue
                routes.append(litellm_route(model, settings))
                if provider == LITELLM:
                    prefix = model.split("/", 1)[0]
                    credentials.setdefault(PROVIDER_CREDENTIALS[prefix], f"the {prefix}/ model routes")
    for variable, needed_by in sorted(credentials.items()):
        problem = _key_problem(variable, env.get(variable, ""), needed_by)
        if problem:
            problems.append(problem)
    if provider == OPENROUTER or any(is_openrouter(r) for r in routes):
        problem = _base_url_problem(settings.openrouter_base_url)
        if problem:
            problems.append(problem)
    problems += _reasoning_problems(settings, env, routes)
    if settings.llm_timeout_s <= 0:
        problems.append("LLM_TIMEOUT_S must be greater than 0")
    if settings.llm_max_attempts < 1:
        problems.append("LLM_MAX_ATTEMPTS must be at least 1")
    if settings.llm_backoff_initial_s <= 0 or settings.llm_backoff_max_s < settings.llm_backoff_initial_s:
        problems.append("LLM_BACKOFF_INITIAL_S must be > 0 and LLM_BACKOFF_MAX_S at least as large")
    if problems:
        env_file = project_env_file()
        where = f" (environment and {env_file})" if env_file and env_file.is_file() else " (environment; no .env file)"
        raise LLMConfigurationError(f"online LLM configuration is invalid{where}: " + "; ".join(problems))


def _reasoning_problems(settings: Settings, env: Mapping[str, str], routes: list[str]) -> list[str]:
    problems = [f"{name} must be true or false" for name in _FLAGS
                if env.get(name, "").strip() and env[name].strip().lower() not in _FLAG_VALUES]
    effort = settings.llm_reasoning_effort
    if effort and effort not in REASONING_EFFORTS:
        problems.append(f"LLM_REASONING_EFFORT must be one of {list(REASONING_EFFORTS)}")
    if effort and not settings.llm_reasoning_enabled:
        problems.append("LLM_REASONING_EFFORT is set but LLM_REASONING_ENABLED is false")
    if settings.llm_reasoning_token_headroom < 0:
        problems.append("LLM_REASONING_TOKEN_HEADROOM must be 0 or more")
    requested = settings.llm_reasoning_enabled and (env.get("LLM_REASONING_ENABLED", "").strip() or effort)
    if requested and any(not is_openrouter(r) for r in routes):
        problems.append("LLM_REASONING_ENABLED/LLM_REASONING_EFFORT apply to OpenRouter routes only; set "
                        "LLM_REASONING_ENABLED=false for routes outside OpenRouter")
    return problems


def online_fallbacks(settings: Settings, environ: Mapping[str, str] | None = None) -> Settings:
    """`settings` with each tier's fallback chain cleared unless LLM_FALLBACKS_<TIER> is set: the
    built-in defaults name models nobody configured, and online they would be called."""
    from dataclasses import replace

    env = os.environ if environ is None else environ
    cleared = {f"fallbacks_{tier.value}": () for tier, (_, var) in TIER_VARIABLES.items()
               if not env.get(var, "").strip()}
    return replace(settings, **cleared) if cleared else settings


def llm_summary(settings: Settings) -> dict[str, Any]:
    """What a process calls, for status reports and the smoke test: model ids, LiteLLM routes and
    request options. Never a credential."""
    tiers = _models(settings)
    routes = {tier.value: [litellm_route(m, settings) for m in (primary, *fallbacks)]
              for tier, (primary, fallbacks) in tiers.items()}
    via_openrouter = any(is_openrouter(r) for chain in routes.values() for r in chain)
    return {
        "provider": settings.llm_provider,
        "base_url": settings.openrouter_base_url if via_openrouter else None,
        "models": {tier.value: primary for tier, (primary, _) in tiers.items()},
        "routes": {tier: chain[0] for tier, chain in routes.items()},
        "fallback_routes": {tier: chain[1:] for tier, chain in routes.items()},
        "reasoning": {"enabled": settings.llm_reasoning_enabled, "effort": settings.llm_reasoning_effort or None,
                      "token_headroom": settings.llm_reasoning_token_headroom} if via_openrouter else None,
        "require_parameters": settings.openrouter_require_parameters if via_openrouter else None,
    }


def build_llm(settings: Settings | None = None, *, offline: Callable[[], LLMClient] | None = None,
              environ: Mapping[str, str] | None = None, callbacks: list[LLMCallback] | None = None) -> LLMClient:
    """The LLM this process should use. Offline: `offline()` (or a bare FakeLLM). Online: a
    LiteLLMGateway over the validated settings, with only the fallbacks configured explicitly."""
    settings = settings or get_settings()
    if llm_mode(environ) == OFFLINE:
        return offline() if offline is not None else FakeLLM(settings)
    validate_online_config(settings, environ)
    return LiteLLMGateway(online_fallbacks(settings, environ), callbacks=callbacks)
