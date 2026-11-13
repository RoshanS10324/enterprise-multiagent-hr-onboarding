"""scripts/isolated_e2e_10_employees.py: the dataset, and the guards that keep it off the shared project.

OFFLINE. No database, no network: the env files below are temporary files with made-up hosts and keys.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import socket
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("isolated_e2e", ROOT / "scripts" / "isolated_e2e_10_employees.py")
iso = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iso)

SHARED_URL = "https://sharedref123.supabase.co"
SHARED_KEY = "shared-service-role-key-not-real"
TEST_ENV = {
    "ISOLATED_TEST_DATABASE": "1", "SUPABASE_URL": "https://testref456.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "test-service-role-key-not-real",
    "UC01_FIELD_ENCRYPTION_KEY": "test-encryption-key-not-real", "UC01_FIELD_ENCRYPTION_KEY_ID": "dev-isolated-1",
    "UC01_DOCUMENT_BUCKET": "uc01-isolated-test",
}


@pytest.fixture(autouse=True)
def restore_process_state(monkeypatch):
    """apply_environment / mode_validate / network_guard change process-wide state: undo it after each test."""
    names = set(iso.REQUIRED_TEST_ENV) | set(iso.PINNED_EMPTY) | {
        "NEXT_PUBLIC_SUPABASE_URL", "APP_ENV", "UC01_SUPABASE_INTERIM_WRITES", "UC02_STORE_MODE", "UC02_ENV",
        "UC02_ALLOW_SYNTHETIC_CONFIG", "UC02_ALLOW_FIXTURE_WRITES", "UC02_ALLOW_CONFIG_SEEDING", "LLM_MODE",
        "ORCHESTRATOR_STORE_MODE", "SOMETHING_ELSE"}
    for name in names:
        monkeypatch.setenv(name, os.environ.get(name, ""))
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)


@pytest.fixture
def shared(tmp_path):
    f = tmp_path / "shared.env"
    f.write_text(f"SUPABASE_URL={SHARED_URL}\nSUPABASE_SERVICE_ROLE_KEY={SHARED_KEY}\n"
                 "DATABASE_URL=postgresql://postgres.sharedref123:pw@aws-0-x.pooler.supabase.com:6543/postgres\n",
                 encoding="utf-8")
    return iso.shared_fingerprint((f,))


# ------------------------------------------------------------------ dataset


def test_the_dataset_is_ten_unique_synthetic_employees_for_the_clients_vocabulary():
    ds = iso.load_dataset()
    assert iso.validate_dataset(ds, today=date(2026, 11, 5)) == []
    emps = ds["employees"]
    assert len({e["test_id"] for e in emps}) == len({e["email"] for e in emps}) == len({e["name"] for e in emps}) == 10
    assert {e["scenario"] for e in emps} == {"overseas", "in_country"}
    assert {e["role_id"] for e in emps} == {"software_engineer", "qa_engineer"}
    assert "employee_no" not in str(emps)  # numbers come from UC-01's counter, never from the dataset


def test_dataset_problems_are_reported():
    ds = copy.deepcopy(iso.load_dataset())
    ds["employees"][1]["email"] = ds["employees"][0]["email"]
    ds["employees"][2]["role_id"] = "data_engineer"
    ds["employees"][3]["joining_date"] = "2027-01-16"  # a Saturday
    ds["employees"][4]["nationality_code"] = "FR"
    ds["employees"][5]["project"] = ""
    problems = iso.validate_dataset(ds, today=date(2026, 11, 5))
    assert "duplicate email" in problems
    assert any("role data_engineer" in p for p in problems)
    assert any("future working day" in p for p in problems)
    assert any("nationality FR" in p for p in problems)
    assert any("project and manager are required" in p for p in problems)


def test_the_intake_carries_agreeing_project_sources_and_the_handoff_uses_them():
    ds = iso.load_dataset()
    emp = ds["employees"][0]
    intake = iso.intake_for(emp, ds)
    i = intake["intake"]
    assert i["placement"]["project_name"] == i["uc02"]["project"] == emp["project"]
    assert i["uc02"]["attributes"] == ["location_status:overseas", "needs_travel", "needs_accommodation"]
    e2e = iso._live_run()._e2e()
    rb = {"department": emp["department"], "project_name": emp["project"]}
    values, sources = e2e.uc02_fields(rb, i)
    assert values["project"] == emp["project"] and sources["project"] == "uc01_case.project_name"


def test_expected_plan_oracle_follows_scenario_and_grade():
    ds = iso.load_dataset()
    by = {e["test_id"]: e for e in ds["employees"]}
    assert iso.expected_plan(by["EMP-TEST-002"], ds) == {"tasks": 10, "dependencies": 9}  # in country
    assert iso.expected_plan(by["EMP-TEST-006"], ds) == {"tasks": 14, "dependencies": 13}  # overseas G3
    assert iso.expected_plan(by["EMP-TEST-001"], ds) == {"tasks": 15, "dependencies": 14}  # overseas G4


def test_every_employee_form_extracts_completely_with_a_complete_budget():
    report = iso.mode_validate(iso.load_dataset(), None)
    assert report["ok"], report
    for tid, e in report["employees"].items():
        assert e["uc01_in_memory"] == "CREATED" and e["budget"]["status"] == "COMPLETE", tid
    assert report["employees"]["EMP-TEST-001"]["budget"]["total"] == "20500.00"  # 4800+3500+3500+1250+2300+7x450+2000


def test_the_original_must_be_the_blank_2026_form(tmp_path):
    fake = tmp_path / "not_the_form.docx"
    fake.write_bytes(b"something else")
    with pytest.raises(SystemExit, match="not the blank 2026 form"):
        iso.read_original(fake)


# ------------------------------------------------------------------ isolation guards


def test_a_separate_https_test_project_passes(shared):
    cfg = iso.check_config(TEST_ENV, shared, "testref456.supabase.co")
    assert cfg["ok"], cfg["problems"]
    assert cfg["host"] == "testref456.supabase.co" and cfg["project_ref"] == "testref456"


@pytest.mark.parametrize("change, failed", [
    ({"SUPABASE_URL": SHARED_URL}, "host_not_shared"),
    ({"SUPABASE_SERVICE_ROLE_KEY": SHARED_KEY}, "service_key_not_shared"),
    ({"UC01_FIELD_ENCRYPTION_KEY": SHARED_KEY}, "encryption_key_not_shared"),
    ({"SUPABASE_URL": "http://testref456.supabase.co"}, "https"),
    ({"ISOLATED_TEST_DATABASE": "0"}, "marked_isolated"),
    ({"UC01_FIELD_ENCRYPTION_KEY_ID": "prod-1"}, "dev_encryption_key_id"),
    ({"DATABASE_URL": "postgresql://postgres.sharedref123:x@aws-0-x.pooler.supabase.com:6543/postgres"},
     "no_other_endpoint_points_at_shared"),
])
def test_anything_belonging_to_the_shared_project_is_refused(shared, change, failed):
    cfg = iso.check_config({**TEST_ENV, **change}, shared, None)
    assert not cfg["ok"] and cfg["checks"][failed] is False


def test_a_mistyped_confirm_host_and_missing_names_are_refused(shared):
    assert iso.check_config(TEST_ENV, shared, "other.supabase.co")["checks"]["confirm_host_matches"] is False
    cfg = iso.check_config({k: v for k, v in TEST_ENV.items() if k != "UC01_DOCUMENT_BUCKET"}, shared)
    assert not cfg["ok"] and any("UC01_DOCUMENT_BUCKET" in p for p in cfg["problems"])


def test_the_environment_is_pinned_and_shared_leftovers_are_detected(shared, monkeypatch):
    for name in ("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "DATABASE_URL",
                 "UC02_STORE_MODE", "LLM_MODE", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("SOMETHING_ELSE", SHARED_URL + "/rest/v1")
    for name in [n for n, v in os.environ.items() if v and (SHARED_KEY in v or "sharedref123" in v)
                 and n != "SOMETHING_ELSE"]:
        monkeypatch.setenv(name, "")
    iso.apply_environment(TEST_ENV)
    assert os.environ["SUPABASE_URL"] == os.environ["NEXT_PUBLIC_SUPABASE_URL"] == TEST_ENV["SUPABASE_URL"]
    assert os.environ["DATABASE_URL"] == "" and os.environ["ANTHROPIC_API_KEY"] == ""
    assert os.environ["UC02_STORE_MODE"] == "supabase" and os.environ["LLM_MODE"] == "offline"
    assert iso.environment_leaks(shared) == ["SOMETHING_ELSE"]


def test_the_network_guard_allows_only_the_test_host(monkeypatch):
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: calls.append(host) or [])
    iso.network_guard("testref456.supabase.co")
    socket.getaddrinfo("testref456.supabase.co", 443)
    with pytest.raises(ConnectionRefusedError):
        socket.getaddrinfo("sharedref123.supabase.co", 443)
    iso.network_guard(None)
    with pytest.raises(ConnectionRefusedError):
        socket.getaddrinfo("testref456.supabase.co", 443)
    assert calls == ["testref456.supabase.co"]


def test_the_cli_refuses_a_shared_env_file_and_writes_without_confirmation(tmp_path):
    for shared_file in iso.SHARED_ENV_FILES:
        with pytest.raises(SystemExit):
            iso.main(["preflight", "--env-file", str(shared_file), "--confirm-host", "x", "--out", str(tmp_path)])
    with pytest.raises(SystemExit):
        iso.main(["preflight", "--env-file", str(tmp_path / "test.env"), "--out", str(tmp_path)])  # no --confirm-host


def test_hosts_and_refs_parse_without_raising_or_echoing_values():
    url = "postgresql://postgres.sharedref123:p[w]@aws-0-x.pooler.supabase.com:6543/postgres"
    assert iso._host(url) == "aws-0-x.pooler.supabase.com" and iso._project_ref(url) == "sharedref123"
    assert iso._host("https://TestRef456.supabase.co/rest/v1") == "testref456.supabase.co"
    assert iso._host("not a url") == ""
