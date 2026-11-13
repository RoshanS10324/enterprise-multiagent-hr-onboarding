"""The synthetic mock-employee dataset and its seeder (scripts/seed_mock_employees.py).

Runs against a fresh in-memory dev container only; nothing is persisted.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("seed_mock_employees", ROOT / "scripts" / "seed_mock_employees.py")
seeder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seeder)

REQUIRED_SCENARIOS = {
    "valid_new_employee", "pending_form_approval", "rejected_form", "duplicate_candidate", "rehire",
    "internal_transfer", "missing_placement", "hiring_freeze", "pending_hr_approval", "invalid_employee_details",
}


def test_the_dataset_is_synthetic_and_covers_every_scenario():
    data = seeder.load_dataset()
    assert data["mock"] is True and data["client_id"].startswith("SYN-")
    assert {e["scenario"] for e in data["employees"]} == REQUIRED_SCENARIOS
    assert all(e["candidate_email"].endswith(".invalid") for e in data["employees"])


def test_every_scenario_produces_its_expected_outcome():
    report = seeder.seed(seeder.build_target("memory"), seeder.load_dataset())
    assert [r["scenario"] for r in report if not r["ok"]] == []
    assert seeder.summarise(report) == {"inserted": 4, "skipped": 1, "held": 3, "paused": 1, "failed": 1}


def test_seeding_twice_inserts_nothing_new_and_numbers_stay_unique():
    container = seeder.build_target("memory")
    data = seeder.load_dataset()
    first = seeder.seed(container, data)
    second = seeder.seed(container, data)
    assert seeder.summarise(second)["inserted"] == 0
    numbers = [r["employee_no"] for r in first if r["result"] == "inserted"]
    assert len(numbers) == len(set(numbers)) == 4
    assert len(container.onboarding_deps.case_repo.list_for_client(data["client_id"])) == 4


def test_missing_placement_is_held_not_invented():
    report = seeder.seed(seeder.build_target("memory"), seeder.load_dataset())
    row = next(r for r in report if r["scenario"] == "missing_placement")
    assert row["result"] == "held" and row["reasons"] == ["location_name_required"]


def test_no_salary_text_in_the_report():
    data = seeder.load_dataset()
    report = seeder.seed(seeder.build_target("memory"), data)
    assert data["salary_placeholder"] not in json.dumps(report, default=str)


def test_a_dataset_not_marked_mock_is_refused(tmp_path):
    data = seeder.load_dataset()
    data["mock"] = False
    path = tmp_path / "employees.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit, match="not marked mock"):
        seeder.load_dataset(path)
