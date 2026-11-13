"""Builds the onboarding runtime (AgentContext + OnboardingDependencies + joining-day job store)
from the environment. Shared by the API (sub_agent.api) and the joining-day scheduler process
(sub_agent.scheduler), so both run against exactly the same stores.

HR_AGENT_ENV (default "production") selects how strict the wiring is:
  development, test    in-memory onboarding data allowed (HR_AGENT_PERSIST=0); with HR_AGENT_PERSIST=1 an
                       empty database is seeded with the synthetic demo data
  staging              HR_AGENT_PERSIST=1 required; fake connectors allowed (logged at startup)
  production           HR_AGENT_PERSIST=1 required, and fake connectors are refused. No real email,
                       HRMS, calendar or notification adapter exists in this repository yet, so
                       production mode cannot start -- deliberately.

With HR_AGENT_PERSIST=1 every durable store lives in HR_AGENT_DATA_DIR: onboarding.db (repository +
joining-day jobs, see sub_agent.persistence), hitl.db (approvals), checkpoints.db (workflow
checkpoints) and audit.jsonl.

LLM_MODE (default "offline") selects the model access, through hr_onboarding_common.llm.build_llm:
  offline   sub_agent.fakes.offline_llm -- deterministic, no network, no key
  online    hr_onboarding_common's LiteLLMGateway (OpenRouter, e.g. deepseek/deepseek-v4.1-flash with
            reasoning; provider options are set there, not here), its configuration validated here,
            at startup: missing key or model -> RuntimeConfigurationError
Either way the client is wrapped in SanitizedLLM.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, TypeVar

from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.config import get_settings
from hr_onboarding_common.llm import LLMClient, LLMConfigurationError, LLMConversation, LLMError, build_llm
from hr_onboarding_common.llm.base import ModelTier
from hr_onboarding_common.observability.logging import get_logger
from hr_onboarding_common.platform.policy import PolicyStore
from sub_agent.agent import AGENT_HOME, build_checkpointer
from sub_agent.db.config import DatabaseMode, SupabaseConfigurationError, database_mode, load_supabase_settings
from sub_agent.db.postgrest import PersistenceError
from sub_agent.fakes import InMemoryJobStore, build_fake_dependencies, offline_llm, seed_synthetic_data
from sub_agent.identity import DEV_ENVIRONMENTS
from sub_agent.persistence import Database, SqliteJobStore, SqliteOnboardingRepository

log = get_logger("runtime")
ENVIRONMENTS = frozenset({"development", "test", "staging", "production"})
T = TypeVar("T")


class RuntimeConfigurationError(RuntimeError):
    pass


class SanitizedLLM:
    """Wraps the LLM client so a provider failure never carries provider text upward.
    hr-onboarding-common's BaseAgent copies `"LLM failure: {error}"` into the API response; through
    this wrapper that text is always generic. The error type is logged, the provider's message is
    not (it can echo request content)."""

    GENERIC_MESSAGE = "the language model is unavailable"

    def __init__(self, inner: LLMClient):
        self.inner = inner

    @property
    def calls(self):
        return self.inner.calls

    def model_for(self, tier: ModelTier) -> str:
        return self.inner.model_for(tier)

    def structured(self, *, task: str, tier: ModelTier, system: str, user: str, schema: type[T],
                   max_tokens: int = 8000, conversation: LLMConversation | None = None,
                   scope: tuple[str, ...] | None = None) -> T:
        try:
            return self.inner.structured(task=task, tier=tier, system=system, user=user, schema=schema,
                                         max_tokens=max_tokens, conversation=conversation, scope=scope)
        except LLMError as e:
            log.warning("llm.call_failed", extra={"llm": {"task": task, "error_type": type(e).__name__}})
            raise type(e)(self.GENERIC_MESSAGE) from None

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


def dact_environment(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    value = env.get("HR_AGENT_ENV", "production").strip().lower()
    if value not in ENVIRONMENTS:
        raise RuntimeConfigurationError(f"HR_AGENT_ENV={value!r} is not one of {sorted(ENVIRONMENTS)}")
    return value


def build_context(environ: Mapping[str, str] | None = None) -> tuple[AgentContext, Any]:
    """Returns (AgentContext, joining-day job store). AgentContext.connector is the
    OnboardingDependencies bundle."""
    env = os.environ if environ is None else environ
    dact_env = dact_environment(env)
    persist = env.get("HR_AGENT_PERSIST", "0") == "1"
    connector_mode = env.get("CONNECTOR_MODE", "offline").lower()
    if connector_mode != "offline":
        raise RuntimeConfigurationError(
            f"CONNECTOR_MODE={connector_mode!r} is not supported. Only 'offline' (the in-memory fakes in "
            f"sub_agent.fakes) exists: no real email, HRMS, calendar or notification/Teams adapter is "
            f"implemented or configured in this repository.")
    if dact_env not in DEV_ENVIRONMENTS and not persist:
        raise RuntimeConfigurationError(
            f"HR_AGENT_ENV={dact_env} requires HR_AGENT_PERSIST=1: onboarding cases, approvals and workflow checkpoints "
            f"must survive a restart outside development and test")
    if dact_env == "production":
        raise RuntimeConfigurationError(
            "HR_AGENT_ENV=production refuses fake connectors, and no real email, HRMS, calendar or notification "
            "adapter exists in this repository yet. HR_AGENT_ENV=staging runs the service on the fakes.")

    try:
        db_mode = database_mode(env)
        db_settings = load_supabase_settings(env) if db_mode == DatabaseMode.SUPABASE else None
    except SupabaseConfigurationError as e:  # names the variables to fix, never their values
        raise RuntimeConfigurationError(str(e)) from None

    settings = get_settings(AGENT_HOME)
    try:
        llm: LLMClient = build_llm(settings, offline=offline_llm, environ=env)
    except LLMConfigurationError as e:  # names the variables to fix, never their values
        raise RuntimeConfigurationError(str(e)) from None
    if db_settings is not None:
        return _build_supabase_context(settings, llm, db_settings, persist=persist, dact_env=dact_env)
    overrides: dict[str, Any] = {"checkpointer": build_checkpointer(settings.data_dir if persist else None)}
    jobs: Any = InMemoryJobStore()
    if persist:
        database = Database(settings.data_dir / "onboarding.db")
        repository = SqliteOnboardingRepository(database)
        if dact_env in DEV_ENVIRONMENTS and repository.is_empty():
            # UC05_SEED_SAMPLE_PEOPLE=0 (set by the orchestrator's UC05 worker): the client configurations only, no
            # sample employees or buddies. Unset: the full synthetic world, as before.
            seed_synthetic_data(repository, people=(env.get("UC05_SEED_SAMPLE_PEOPLE") or "1").strip().lower()
                                not in ("0", "false", "no", "off"))
        overrides["repository"], jobs = repository, SqliteJobStore(database)
    log.warning("runtime.fake_connectors_enabled", extra={"security": {
        "environment": dact_env, "repository": "sqlite" if persist else "in-memory",
        "detail": "email, employee directory, calendar and notifications are in-memory fakes from "
                  "sub_agent.fakes; nothing is delivered outside this process"}})
    ctx = AgentContext.build(agent_home=AGENT_HOME, llm=SanitizedLLM(llm), connector=build_fake_dependencies(**overrides),
                             settings=settings, persist=persist)
    return ctx, jobs


def _build_supabase_context(settings, llm: LLMClient, db_settings, *, persist: bool,
                            dact_env: str) -> tuple[AgentContext, Any]:
    """UC05_DATABASE_MODE=supabase: cases, buddies, sessions, participants, communications, approvals,
    audit events and agent runs live in the shared Supabase database. Startup fails (never falls back
    to local or synthetic data) if the credential is refused, the database is unreachable or a table
    UC05 needs is missing. What stays local -- and why -- is listed in docs/UC05_SUPABASE_INTEGRATION.md:
    LangGraph checkpoints and joining-day job leases (HR_AGENT_DATA_DIR), and the calendar/notification
    connectors, which are still the simulated ones."""
    from sub_agent.db.factory import build_supabase_stores

    try:
        stores = build_supabase_stores(db_settings, audit_path=settings.data_dir / "audit.jsonl" if persist else None)
    except PersistenceError as e:  # carries no credential and no row value
        raise RuntimeConfigurationError(f"UC05_DATABASE_MODE=supabase but the database is not usable: {e}") from None
    jobs: Any = SqliteJobStore(Database(settings.data_dir / "uc05_jobs.db")) if persist else InMemoryJobStore()
    deps = build_fake_dependencies(
        checkpointer=build_checkpointer(settings.data_dir if persist else None), repository=stores.repository,
        directory=stores.directory, email=stores.events, run_recorder=stores.runs)
    log.warning("runtime.supabase_mode", extra={"security": {
        "environment": dact_env, "repository": "supabase", "schema": db_settings.schema,
        "detail": "UC05 business records are read from and written to Supabase; calendar and notifications are "
                  "still the simulated connectors (nothing is delivered outside this process)"}})
    ctx = AgentContext(settings=settings, llm=SanitizedLLM(llm), policies=PolicyStore(settings.policy_dir),
                       audit=stores.audit, hitl=stores.hitl, connector=deps)
    return ctx, jobs
