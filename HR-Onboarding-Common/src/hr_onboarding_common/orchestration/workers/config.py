"""Explicit, validated configuration for the three colliding UC worker processes.

The orchestrator's own Python environment is never assumed to contain UC01/UC04/UC05's
dependencies. Each worker's interpreter and source directory are explicit configuration -- never
a hardcoded machine-specific path, and never a silent fallback to the orchestrator's own
interpreter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from hr_onboarding_common.llm.redact import SECRET_ENV_VARS

COLLIDING_UCS = ("uc01", "uc04", "uc05")
# Whether a worker is given the LLM provider credentials. Only UC05 calls a model through the gateway
# today; UC01 and UC04 make no model calls (docs/UC_LLM_GATEWAY_MIGRATION_GUIDE.md), so their
# processes never receive a key. Overridable per worker (UC0X_WORKER_LLM_ACCESS, worker_config.yaml).
DEFAULT_LLM_ACCESS = {"uc01": False, "uc04": False, "uc05": True}

# uc_id -> (entry module, expected attribute -- a function for UC01, a class for UC04/UC05) --
# used by the startup identity check to prove the worker loaded the right code from the right
# place, not merely that "an import succeeded".
EXPECTED_ENTRY = {
    "uc01": ("sub_agent.onboarding_entry", "open_onboarding_case"),
    "uc04": ("sub_agent.agent", "Uc04Agent"),
    "uc05": ("sub_agent.agent", "OnboardingAgent"),
}


class WorkerConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerConfig:
    uc_id: str
    python_executable: Path
    src_dir: Path
    # Per-call timeout for this UC's worker (seconds); None: the pool's default. A call that exceeds
    # it kills the worker and its outcome is UNCERTAIN (uc_worker_client.UcWorker._raw_call).
    timeout_s: float | None = None
    # Whether the worker process receives the LLM provider credentials; None: DEFAULT_LLM_ACCESS.
    llm_access: bool | None = None

    @property
    def has_llm_access(self) -> bool:
        return DEFAULT_LLM_ACCESS.get(self.uc_id, False) if self.llm_access is None else self.llm_access

    def child_environment(self, environ: dict[str, str] | None = None) -> dict[str, str]:
        """The environment the worker process starts with: the orchestrator's own (LLM_MODE, models,
        the .env path, everything else), minus the provider credentials unless it has LLM access."""
        env = dict(os.environ if environ is None else environ)
        # The orchestrator's own document-storage key (orchestration/documents.py) and case-state store key
        # (orchestration/supabase_state_store.py): no worker needs them.
        env.pop("DOCUMENT_STORAGE_SERVICE_ROLE_KEY", None)
        env.pop("ORCHESTRATOR_SUPABASE_SERVICE_ROLE_KEY", None)
        # Worker-scoped settings: UC0X_WORKER_ENV__NAME=value reaches only that worker, as NAME. The workers share
        # one environment otherwise, and a variable one UC needs can switch another into a different mode (a bare
        # SUPABASE_URL puts UC01 into its blocked Supabase mode). No worker sees another worker's scoped settings.
        own = f"{self.uc_id.upper()}_WORKER_ENV__"
        scoped = {k: v for k, v in env.items() if "_WORKER_ENV__" in k}
        for key in scoped:
            env.pop(key, None)
        env.update({k[len(own):]: v for k, v in scoped.items() if k.startswith(own) and k[len(own):]})
        if not self.has_llm_access:
            for name in SECRET_ENV_VARS:
                env.pop(name, None)
        return env

    def validate(self) -> None:
        if self.timeout_s is not None and self.timeout_s <= 0:
            raise WorkerConfigurationError(f"{self.uc_id} worker: timeout_s must be positive, got {self.timeout_s}")
        if not self.python_executable.exists():
            raise WorkerConfigurationError(
                f"{self.uc_id} worker: configured python_executable does not exist: "
                f"{self.python_executable}. Set {self.uc_id.upper()}_WORKER_PYTHON or "
                f"worker_config.yaml -- see docs/ONBOARDING_COMMON_ARCHITECTURE.md."
            )
        if not self.src_dir.exists():
            raise WorkerConfigurationError(
                f"{self.uc_id} worker: configured src_dir does not exist: {self.src_dir}. Set "
                f"{self.uc_id.upper()}_WORKER_SRC_DIR or worker_config.yaml."
            )
        entry_module = EXPECTED_ENTRY[self.uc_id][0]
        rel_path = Path(*entry_module.split(".")).with_suffix(".py")
        entry_file = self.src_dir / rel_path
        if not entry_file.exists():
            raise WorkerConfigurationError(
                f"{self.uc_id} worker: src_dir {self.src_dir} does not contain the expected "
                f"entry file {rel_path} -- is this really {self.uc_id}'s source directory?"
            )


def _default_venv_python(uc_root: Path) -> Path | None:
    """Convenience default only: that UC's OWN existing virtualenv, if present. Never used as a
    silent fallback when explicit configuration is present but invalid -- see WorkerConfig.validate."""
    win = uc_root / ".venv" / "Scripts" / "python.exe"
    posix = uc_root / ".venv" / "bin" / "python"
    if win.exists():
        return win
    if posix.exists():
        return posix
    return None


def load_worker_config(uc_id: str, *, config_path: Path | None = None) -> WorkerConfig:
    """Resolve a single UC's worker config from (in order): an explicit `worker_config.yaml`,
    environment variables, or that UC's own detected `.venv`. Raises WorkerConfigurationError
    with an actionable message if nothing resolves -- never falls back to `sys.executable`."""
    uc_id = uc_id.lower()
    if uc_id not in COLLIDING_UCS:
        raise WorkerConfigurationError(f"'{uc_id}' is not a colliding UC (expected one of {COLLIDING_UCS})")

    python_exe: Path | None = None
    src_dir: Path | None = None
    timeout_s: float | None = None
    llm_access: bool | None = None

    if config_path and config_path.exists():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        entry = data.get(uc_id, {})
        if entry.get("python"):
            python_exe = Path(entry["python"])
        if entry.get("src_dir"):
            src_dir = Path(entry["src_dir"])
        if entry.get("timeout_s") is not None:
            timeout_s = float(entry["timeout_s"])
        if entry.get("llm_access") is not None:
            llm_access = bool(entry["llm_access"])

    env_python = os.environ.get(f"{uc_id.upper()}_WORKER_PYTHON")
    env_src = os.environ.get(f"{uc_id.upper()}_WORKER_SRC_DIR")
    env_timeout = os.environ.get(f"{uc_id.upper()}_WORKER_TIMEOUT_S")
    env_llm_access = os.environ.get(f"{uc_id.upper()}_WORKER_LLM_ACCESS", "").strip()
    if env_python:
        python_exe = Path(env_python)
    if env_src:
        src_dir = Path(env_src)
    if env_timeout:
        timeout_s = float(env_timeout)
    if env_llm_access:
        llm_access = env_llm_access.lower() in ("1", "true", "yes")

    if src_dir is not None and python_exe is None:
        uc_root = src_dir.parent if src_dir.name == "src" else src_dir
        python_exe = _default_venv_python(uc_root)

    if python_exe is None or src_dir is None:
        raise WorkerConfigurationError(
            f"{uc_id} worker: no python_executable/src_dir configured and no .venv auto-detected. "
            f"Set {uc_id.upper()}_WORKER_PYTHON and {uc_id.upper()}_WORKER_SRC_DIR (env vars) or "
            f"add a '{uc_id}:' entry to worker_config.yaml -- see worker_config.example.yaml."
        )

    cfg = WorkerConfig(uc_id=uc_id, python_executable=python_exe, src_dir=src_dir, timeout_s=timeout_s,
                       llm_access=llm_access)
    cfg.validate()
    return cfg
