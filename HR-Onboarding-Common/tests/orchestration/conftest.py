from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # lets test modules do `from _helpers import ...`

from _helpers import UC_ROOTS, require_uc, venv_python  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _configure_worker_env():
    """Point the colliding-UC worker config at each UC's own pre-existing `.venv` -- exactly the
    "clean environment" bootstrap described in docs/ONBOARDING_COMMON_ARCHITECTURE.md."""
    # The tests assume UC04's default identity profile (LOCAL_SIMULATION, synthetic ids). A developer's .env may enable
    # PLATFORM for their running backend; load_environment() (called by build_deps) would copy it into this process
    # for every later test. An existing (empty) value is never overridden by the .env loader, so pin it here; tests
    # that need PLATFORM set it explicitly.
    os.environ["UC04_IDENTITY_PROFILE"] = ""
    # Likewise UC05's offer-event source: the tests assume UC05's mailbox unless they choose orchestrator_case.
    os.environ["UC05_OFFER_EVENT_SOURCE"] = ""
    # And UC05's buddy source with its worker-scoped Supabase settings: no test reaches a live database.
    for name in ("UC05_BUDDY_SOURCE", "UC05_WELCOME_CONFIG_SOURCE", "UC05_WORKER_ENV__SUPABASE_URL",
                 "UC05_WORKER_ENV__SUPABASE_SERVICE_ROLE_KEY"):
        os.environ[name] = ""
    # Likewise UC02's store (adapters/uc02_runtime.py): in-memory unless a test selects Supabase mode itself, with an
    # in-process PostgREST (test_uc02_supabase_orchestration.py). A developer's UC02_STORE_MODE=supabase never leaks in.
    for name in ("UC02_STORE_MODE", "UC02_ENV", "UC02_ALLOW_SYNTHETIC_CONFIG", "UC02_WORKER_ENV__SUPABASE_URL",
                 "UC02_WORKER_ENV__SUPABASE_SERVICE_ROLE_KEY", "UC02_WORKER_ENV__UC02_STORE_MODE",
                 "UC02_WORKER_ENV__UC02_ENV", "UC02_WORKER_ENV__UC02_ALLOW_SYNTHETIC_CONFIG", "ORCHESTRATOR_STATE_STORE"):
        os.environ[name] = ""
    # The orchestrator's own Supabase settings (case state, UC03's tables, joining-day jobs) and the Supabase modes of
    # UC03/UC04/UC05: a test selects them itself, with an in-process fake. A developer's live settings never leak in.
    for name in ("ORCHESTRATOR_SUPABASE_URL", "ORCHESTRATOR_SUPABASE_SERVICE_ROLE_KEY", "ORCHESTRATOR_SUPABASE_SCHEMA",
                 "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "UC04_STATE_MODE", "UC04_CONFIG_SOURCE", "UC05_DATABASE_MODE",
                 "ORCHESTRATOR_JOINING_DAY_STORE"):
        os.environ[name] = ""
    # Worker-scoped variants are REMOVED, not pinned empty: a scoped value wins over the plain name inside the worker
    # (workers/config.child_environment), so an empty pin would silently defeat a test that sets the plain name.
    for name in ("UC04_WORKER_ENV__UC04_STATE_MODE", "UC04_WORKER_ENV__SUPABASE_URL",
                 "UC04_WORKER_ENV__SUPABASE_SERVICE_ROLE_KEY", "UC05_WORKER_ENV__UC05_DATABASE_MODE"):
        os.environ.pop(name, None)
    os.environ["UC03_STORE_MODE"] = "memory"
    for uc_id in ("uc01", "uc04", "uc05"):
        # setdefault: an interpreter the caller configured explicitly always wins.
        os.environ.setdefault(f"{uc_id.upper()}_WORKER_PYTHON", str(venv_python(UC_ROOTS[uc_id])))
        os.environ.setdefault(f"{uc_id.upper()}_WORKER_SRC_DIR", str(UC_ROOTS[uc_id] / "src"))
    yield


@pytest.fixture
def uc01_worker():
    require_uc("uc01")
    from hr_onboarding_common.orchestration.workers.config import WorkerConfig
    from hr_onboarding_common.orchestration.workers.uc_worker_client import UcWorker

    cfg = WorkerConfig(uc_id="uc01", python_executable=venv_python(UC_ROOTS["uc01"]), src_dir=UC_ROOTS["uc01"] / "src")
    worker = UcWorker(cfg)
    worker.start()
    yield worker
    worker.stop()


@pytest.fixture
def uc04_worker():
    require_uc("uc04")
    from hr_onboarding_common.orchestration.workers.config import WorkerConfig
    from hr_onboarding_common.orchestration.workers.uc_worker_client import UcWorker

    cfg = WorkerConfig(uc_id="uc04", python_executable=venv_python(UC_ROOTS["uc04"]), src_dir=UC_ROOTS["uc04"] / "src")
    worker = UcWorker(cfg)
    worker.start()
    yield worker
    worker.stop()


@pytest.fixture
def uc05_worker():
    require_uc("uc05")
    from hr_onboarding_common.orchestration.workers.config import WorkerConfig
    from hr_onboarding_common.orchestration.workers.uc_worker_client import UcWorker

    cfg = WorkerConfig(uc_id="uc05", python_executable=venv_python(UC_ROOTS["uc05"]), src_dir=UC_ROOTS["uc05"] / "src")
    worker = UcWorker(cfg)
    worker.start()
    yield worker
    worker.stop()


@pytest.fixture
def orchestrator():
    """A fully-wired OnboardingOrchestratorAgent: real UC01/UC04/UC05 workers, in-process UC02/
    UC03 (UC02's own pre-existing import defect surfaces naturally -- never papered over)."""
    require_uc("uc01")
    require_uc("uc04")
    require_uc("uc05")

    from hr_onboarding_common.orchestration.bootstrap import build_deps
    from hr_onboarding_common.orchestration.orchestrator_agent import OnboardingOrchestratorAgent

    ctx, deps = build_deps(agent_home=Path(__file__).parent.parent.parent, uc03_repo_root=UC_ROOTS["uc03"])
    agent = OnboardingOrchestratorAgent(ctx)
    yield agent
    deps.worker_pool.stop_all()
