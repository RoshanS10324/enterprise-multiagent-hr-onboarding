from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

# The workspace that holds HR-Onboarding-Common and the five UC repos side by side. Derived from
# this file's location (tests/orchestration/_helpers.py), never a machine-specific absolute path;
# HR_ONBOARDING_WORKSPACE overrides it for a different checkout layout.
BASE = Path(os.environ.get("HR_ONBOARDING_WORKSPACE") or Path(__file__).resolve().parents[3])

UC_ROOTS = {
    "uc01": BASE / "UC01_Employee_Record_Creation",
    "uc02": BASE / "UC02_Onboarding_Task_Agent",
    "uc03": BASE / "UC03_Document_Validation",
    "uc04": BASE / "UC04_Equipment_Access_Integration_Ready",
    "uc05": BASE / "UC05_Orientation-Schedule-Agent",
}


def venv_python(root: Path) -> Path:
    """That UC's interpreter: `UC0X_WORKER_PYTHON` when set (an environment built outside the UC's
    own tree), else the UC's own `.venv`."""
    for uc_id, uc_root in UC_ROOTS.items():
        override = os.environ.get(f"{uc_id.upper()}_WORKER_PYTHON")
        if override and uc_root == root:
            return Path(override)
    win = root / ".venv" / "Scripts" / "python.exe"
    posix = root / ".venv" / "bin" / "python"
    return win if win.exists() else posix


@lru_cache(maxsize=None)
def _runs(python: Path) -> bool:
    """A copied `.venv` can hold a python.exe launcher whose base interpreter no longer exists, so
    existence alone is not enough: the interpreter must actually start."""
    if not python.exists():
        return False
    try:
        return subprocess.run([str(python), "-c", "import sys"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def uc_available(uc_id: str) -> bool:
    if uc_id == "uc03":  # in-process: runs in this interpreter, needs only UC03's source tree
        return (UC_ROOTS["uc03"] / "src" / "sub_agent" / "workflow.py").exists()
    return _runs(venv_python(UC_ROOTS[uc_id]))


def require_uc(uc_id: str):
    if not uc_available(uc_id):
        pytest.skip(f"{uc_id}: no runnable interpreter ({venv_python(UC_ROOTS[uc_id])}); set "
                    f"{uc_id.upper()}_WORKER_PYTHON -- see docs/ONBOARDING_COMMON_ARCHITECTURE.md")


def make_actor(roles=("hr_admin",), client_id="CLIENT-ALPHA", on_behalf_of="user:hr-admin-01"):
    return {"on_behalf_of": on_behalf_of, "roles": list(roles), "client_id": client_id}
