from __future__ import annotations

from dataclasses import dataclass

from evals.evaluators.uc03_evaluator import ScenarioResult


@dataclass
class EvalMetrics:
    total: int
    passed: int
    failed: int
    pass_rate: float
    failed_scenarios: list[str]
    failure_details: dict[str, list[str]]


def compute_metrics(results: list[ScenarioResult]) -> EvalMetrics:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    failed_scenarios = [r.scenario_id for r in results if not r.passed]
    failure_details = {
        r.scenario_id: r.failures for r in results if not r.passed
    }
    return EvalMetrics(
        total=total,
        passed=passed,
        failed=failed,
        pass_rate=round(passed / total * 100, 1) if total else 0.0,
        failed_scenarios=failed_scenarios,
        failure_details=failure_details,
    )


def format_report(metrics: EvalMetrics, results: list[ScenarioResult]) -> str:
    lines = [
        "=" * 60,
        "UC-03 EVAL REPORT",
        "=" * 60,
        f"Total scenarios : {metrics.total}",
        f"Passed          : {metrics.passed}",
        f"Failed          : {metrics.failed}",
        f"Pass rate       : {metrics.pass_rate}%",
        "",
    ]

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        lines.append(f"[{status}] {result.scenario_id} - {result.description}")
        if not result.passed:
            for failure in result.failures:
                lines.append(f"        [FAIL] {failure}")

    lines += ["", "=" * 60]
    if metrics.failed == 0:
        lines.append("All scenarios passed.")
    else:
        lines.append(f"{metrics.failed} scenario(s) failed. Review failures above.")
    lines.append("=" * 60)

    return "\n".join(lines)
