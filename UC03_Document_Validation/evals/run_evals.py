"""
UC-03 Eval Runner

Usage:
    uv run python evals/run_evals.py
    uv run python evals/run_evals.py --scenario SC-02
    uv run python evals/run_evals.py --fail-fast
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from evals.evaluators.metrics import compute_metrics, format_report
from evals.evaluators.uc03_evaluator import UC03Evaluator

DATASET_PATH = Path(__file__).parent / "datasets" / "uc03_scenarios.json"


async def main(scenario_filter: str | None = None, fail_fast: bool = False) -> int:
    with DATASET_PATH.open("r", encoding="utf-8") as fh:
        scenarios: list[dict] = json.load(fh)

    if scenario_filter:
        scenarios = [s for s in scenarios if s["scenario_id"] == scenario_filter]
        if not scenarios:
            print(f"No scenario found with ID '{scenario_filter}'")
            return 1

    evaluator = UC03Evaluator()
    results = []

    for scenario in scenarios:
        result = await evaluator.run_scenario(scenario)
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.scenario_id} — {result.description}")
        if not result.passed:
            for failure in result.failures:
                print(f"       [FAIL] {failure}")
            if fail_fast:
                print("\nFail-fast: stopping on first failure.")
                break

    metrics = compute_metrics(results)
    print()
    print(format_report(metrics, results))

    return 0 if metrics.failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run UC-03 evals")
    parser.add_argument("--scenario", help="Run a single scenario by ID (e.g. SC-02)")
    parser.add_argument("--fail-fast", action="store_true", help="Stop on first failure")
    args = parser.parse_args()

    exit_code = asyncio.run(main(scenario_filter=args.scenario, fail_fast=args.fail_fast))
    sys.exit(exit_code)
