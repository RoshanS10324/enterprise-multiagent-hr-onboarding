"""Single entry point for the UC-01 eval suite.

Delegates to `evaluation.uc01.runner`, which owns the scenarios and the
scoring; this script only resolves the repo root so it runs from any cwd
and exposes a couple of filtering flags.

    uv run python evaluation/run_evals.py
    uv run python evaluation/run_evals.py --list
    uv run python evaluation/run_evals.py --scenario t01_happy_path

Exit code is 0 only when every selected scenario passes, so CI can gate
on it. Results for a full run are written to `evaluation/uc01/results.json`
by the runner; filtered runs print to stdout only.
"""

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evaluation.uc01 import runner  # noqa: E402

SCENARIOS_DIR = _REPO_ROOT / "evaluation" / "uc01" / "scenarios"


def _scenario_paths(names: list[str]) -> list[Path]:
    paths = sorted(SCENARIOS_DIR.glob("*.json"))
    if not names:
        return paths
    wanted = {n.removesuffix(".json") for n in names}
    selected = [p for p in paths if p.stem in wanted]
    missing = wanted - {p.stem for p in selected}
    if missing:
        raise SystemExit(f"no such scenario(s): {', '.join(sorted(missing))}")
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the UC-01 eval scenarios.")
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        metavar="NAME",
        help="run only this scenario (filename stem); repeatable",
    )
    parser.add_argument("--list", action="store_true", help="list scenario names and exit")
    args = parser.parse_args(argv)

    if args.list:
        for path in sorted(SCENARIOS_DIR.glob("*.json")):
            print(path.stem)
        return 0

    # No filter: hand the whole run to the runner so results.json is
    # written exactly as CI expects.
    if not args.scenario:
        return runner.main()

    failed = 0
    for path in _scenario_paths(args.scenario):
        result = runner.run_scenario(json.loads(path.read_text()))
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[{status}] {result['id']}: {result['title']}")
        for failure in result["failures"]:
            print(f"    - {failure}")
        if not result["passed"]:
            failed += 1

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
