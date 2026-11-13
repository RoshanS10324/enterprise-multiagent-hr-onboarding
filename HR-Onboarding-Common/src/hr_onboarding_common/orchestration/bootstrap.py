"""Convenience construction of `OrchestratorDeps` + `AgentContext` for tests and local runs.
Not required in production: a real deployment can construct these directly."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.config import Settings, load_environment
from hr_onboarding_common.llm import FakeLLM, build_llm
from hr_onboarding_common.orchestration.case_store import InMemoryCaseStateStore, SqliteCaseStateStore
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.tenant_config import TenantConfig
from hr_onboarding_common.orchestration.workers.config import load_worker_config
from hr_onboarding_common.orchestration.workers.uc_worker_client import DEFAULT_TIMEOUT_S, WorkerPool



def offline_llm() -> FakeLLM:
    """The orchestrator's offline LLM: a FakeLLM that answers the in-process UCs' tasks.
    UC-02's `uc02.advise` handler is registered when UC-02 is importable; without it UC-02's advice
    degrades to its rules engine (still a valid plan, marked degraded)."""
    llm = FakeLLM()
    try:
        from onboarding_task_management.agent.fake_llm import register_fake_handler  # type: ignore
    except (ImportError, SyntaxError):  # UC-02 absent or broken: plan_tasks reports it on its own
        return llm
    return register_fake_handler(llm)

def build_deps(*, agent_home: Path, uc03_repo_root: Path, worker_config_path: Path | None = None,
               checkpointer=None, ucs: Iterable[str] = ("uc01", "uc04", "uc05"), persist_dir: Path | None = None,
               stage_adapters: dict[str, Callable[..., Any]] | None = None,
               worker_timeout_s: float = DEFAULT_TIMEOUT_S,
               tenant_config_path: Path | None = None,
               state_store: str | None = None) -> tuple[AgentContext, OrchestratorDeps]:
    """`ucs`: which colliding-UC workers to start (a test that doubles UC01/UC04 needs only uc05).
    `persist_dir`: the orchestrator's own durable stores -- case states (case_states.db), LangGraph
    checkpoints (checkpoints.db), its HITL inbox (hitl.db) and audit log -- so a restarted
    orchestrator resumes the same cases and still sees its own pending gates. UC business data is
    persisted (or not) by each UC itself. `tenant_config_path`: tenant settings (timezone, joining-day
    run time, employee identity scheme); default `<agent_home>/config/tenants.yaml` when it exists,
    else ORCHESTRATOR_TENANT_CONFIG, else code defaults. `state_store`: where case states live --
    "memory" / "sqlite" (the defaults without / with `persist_dir`) or "supabase" (the shared
    `case_orchestration_state` table, from SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / SUPABASE_SCHEMA);
    default ORCHESTRATOR_STATE_STORE. Supabase mode never falls back: a missing or refused configuration
    fails here. Its provisional (pre-intake) states are kept in SQLite under `persist_dir`, or memory.

    The project's .env is loaded first, so the workers started here inherit the same configuration
    (LLM_MODE, model tiers; provider credentials only for workers with LLM access). The
    orchestrator's own LLM follows LLM_MODE (llm.build_llm): online, the configuration is validated
    before any worker starts."""
    load_environment()
    settings = Settings(agent_home=agent_home, data_dir=persist_dir) if persist_dir else Settings(agent_home=agent_home)
    # Online: the central LiteLLMGateway (OpenRouter), validated before any worker starts -- it is the
    # client UC-02 advice uses in-process (adapters/uc02_tasks.py). Offline: FakeLLM with UC-02's handler.
    llm = build_llm(settings, offline=offline_llm)

    # The case-state store is built (and a Supabase configuration checked) before any worker starts.
    mode = (state_store or os.environ.get("ORCHESTRATOR_STATE_STORE") or "").strip().lower()
    mode = mode or ("sqlite" if persist_dir is not None else "memory")
    if mode not in ("memory", "sqlite", "supabase"):
        raise ValueError(f"ORCHESTRATOR_STATE_STORE={mode!r} is not one of memory, sqlite, supabase")
    if mode == "sqlite" and persist_dir is None:
        raise ValueError("ORCHESTRATOR_STATE_STORE=sqlite needs a persist_dir")
    if persist_dir is not None:
        persist_dir.mkdir(parents=True, exist_ok=True)
    case_states: Any = InMemoryCaseStateStore()
    if mode == "sqlite":
        case_states = SqliteCaseStateStore(persist_dir / "case_states.db")
    elif mode == "supabase":
        from hr_onboarding_common.orchestration.supabase_state_store import SupabaseCaseStateStore

        provisional = (SqliteCaseStateStore(persist_dir / "provisional_case_states.db") if persist_dir is not None
                       else InMemoryCaseStateStore())
        case_states = SupabaseCaseStateStore.from_env(os.environ, provisional=provisional)
    pool = WorkerPool({uc_id: load_worker_config(uc_id, config_path=worker_config_path) for uc_id in ucs},
                      timeout_s=worker_timeout_s)
    pool.start_all()
    if persist_dir is not None:
        if checkpointer is None:
            from langgraph.checkpoint.sqlite import SqliteSaver

            checkpointer = SqliteSaver(sqlite3.connect(str(persist_dir / "checkpoints.db"), check_same_thread=False))
            checkpointer.setup()

    default_tenants = agent_home / "config" / "tenants.yaml"
    tenants = TenantConfig.load(tenant_config_path or (default_tenants if default_tenants.exists() else None))
    # UC05's own database mode, as its worker will see it (worker-scoped setting first): in its Supabase mode the
    # onboarding case is the offer-acceptance event (adapters/uc05_orientation.py).
    uc05_mode = (os.environ.get("UC05_WORKER_ENV__UC05_DATABASE_MODE") or os.environ.get("UC05_DATABASE_MODE")
                 or "").strip().lower()
    # UC05_OFFER_EVENT_SOURCE=orchestrator_case: UC05 reads the joiner from this orchestrator's case (the HR-approved
    # form), the case id being the event -- the UC05 worker reads the same variable (workers/uc_worker_runtime.py).
    uc05_source = (os.environ.get("UC05_OFFER_EVENT_SOURCE") or "").strip().lower()
    if uc05_source not in ("", "mailbox", "orchestrator_case"):
        raise ValueError(f"UC05_OFFER_EVENT_SOURCE={uc05_source!r}: expected mailbox or orchestrator_case")
    if uc05_source == "orchestrator_case" and uc05_mode == "supabase":
        raise ValueError("UC05_OFFER_EVENT_SOURCE=orchestrator_case cannot be combined with UC05's Supabase mode")
    deps = OrchestratorDeps(ctx=None, worker_pool=pool, uc03_repo_root=uc03_repo_root, checkpointer=checkpointer,
                            uc05_offer_event_source=("case" if uc05_mode == "supabase" else
                                                     "orchestrator_case" if uc05_source == "orchestrator_case"
                                                     else "mailbox"),
                            case_states=case_states, stage_adapters=dict(stage_adapters or {}), tenants=tenants,
                            # UC03's document registry (collections per plan version, stored revisions)
                            # survives a restart alongside the case store.
                            uc03_document_store_path=persist_dir / "uc03_documents.db" if persist_dir else None)
    ctx = AgentContext.build(agent_home=agent_home, llm=llm, connector=deps, settings=settings,
                             persist=persist_dir is not None)
    deps.ctx = ctx
    return ctx, deps
