"""Demo: mock extracted employee data -> UC-01 -> UC-02, end to end.

    python -m demo.uc01_to_uc02_mock_e2e [--persistence memory|supabase]

A thin wrapper over scripts/e2e_uc01_to_uc02.py (the pipeline and its step
labels are documented there). Runs twice in one process to show the duplicate
run creates nothing new, writes its reports to a fresh temporary directory,
and prints a short summary. UC-02 is triggered only for an employee saved in
and read back from Supabase: --persistence memory therefore stops before
UC-02, and --persistence supabase stops with BLOCKED while UC-01's Supabase
persistence is not ready - it never fakes a write.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "e2e_uc01_to_uc02.py"


def main(argv: list[str]) -> int:
    spec = importlib.util.spec_from_file_location("e2e_uc01_to_uc02", _SCRIPT)
    e2e = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(e2e)
    persistence = argv[argv.index("--persistence") + 1] if "--persistence" in argv else "memory"
    out = Path(tempfile.mkdtemp(prefix="uc01_uc02_demo_"))
    args = ["run", "--persistence", persistence, "--out", str(out)] + ["--twice"]
    with contextlib.redirect_stdout(io.StringIO()):
        code = e2e.main(args)
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    print(f"UC-01 -> UC-02 demo  persistence={persistence}  reports in {out}")
    for name, step in report["steps"].items():
        print(f"  [{step['status']:12s}] {name}")
    if "stopped" in report:
        print("STOPPED:", report["stopped"])
        for blocker in report["steps"]["5 persist employee"].get("blockers", []):
            print("   -", blocker)
    if "second_run" in report:
        second = report["second_run"]["steps"]
        print("second run:", second["4 UC-01 employee creation"].get("uc01_status"),
              "| UC-02 new plan:", second["8 trigger shared orchestrator (plan_tasks)"].get("uc02_created_new_plan"))
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
