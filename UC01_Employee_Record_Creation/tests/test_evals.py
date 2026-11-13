import json
from pathlib import Path

from evaluation.uc01.runner import run_scenario

SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "evaluation" / "uc01" / "scenarios"


def _scenario_paths():
    return sorted(SCENARIOS_DIR.glob("*.json"))


def test_at_least_fourteen_scenarios_exist():
    assert len(_scenario_paths()) >= 14


def test_every_scenario_passes():
    failures = []
    for path in _scenario_paths():
        scenario = json.loads(path.read_text())
        result = run_scenario(scenario)
        if not result["passed"]:
            failures.append((path.name, result["failures"]))

    assert not failures, f"eval scenarios failed: {failures}"
