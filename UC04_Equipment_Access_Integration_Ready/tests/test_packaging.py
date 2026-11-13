"""Repository and packaging safety (TC-39; GR-13; local-only rules)."""

import re
from pathlib import Path

import pytest

from sub_agent.calendar import is_working_day, next_working_day, subtract_working_days
from sub_agent.contracts import WorkCalendar
from sub_agent.policy import Policy

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", ".pytest_cache", "__pycache__"}


def _files(*suffixes):
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS) and (not suffixes or p.suffix in suffixes):
            yield p


def test_tc39_no_source_documents_or_git_data_inside_the_project():
    banned = {".pdf", ".docx", ".doc", ".zip"}
    assert [p for p in _files() if p.suffix.lower() in banned] == []
    assert "resourse" not in ROOT.parts and "resource" not in ROOT.parts


def test_tc39_no_network_or_cloud_or_model_libraries_are_imported_by_the_core():
    forbidden = re.compile(r"^\s*(?:import|from)\s+(socket|requests|httpx|urllib|http\.client|litellm|openai|anthropic|"
                           r"langgraph|langchain\w*|azure\w*|boto3|smtplib|subprocess)\b", re.M)
    # `integrations/` is the one place network code may live (the injected database layer). Everything else, the core included, stays clean.
    offenders = [str(p.relative_to(ROOT)) for p in _files(".py")
                 if "tests" not in p.parts and "integrations" not in p.relative_to(ROOT).parts[:1] and forbidden.search(p.read_text("utf-8"))]
    assert offenders == []


def test_the_core_never_imports_the_integrations_folder_and_integrations_hold_no_secrets():
    importing = re.compile(r"^\s*(?:import|from)\s+(integrations|connect|postgrest_table_client)\b", re.M)
    core = [p for p in (ROOT / "src").rglob("*.py") if importing.search(p.read_text("utf-8"))]
    assert core == []  # the injection runs one way: integrations call the core, never the reverse
    assert not any(p.suffix == ".env" or p.name.startswith(".env") for p in (ROOT / "integrations").rglob("*"))


def test_tc39_no_secrets_or_real_looking_data_in_project_files():
    secret = re.compile(r"(api[_-]?key|secret|password|bearer)\s*[:=]\s*\S{6,}|sk-[A-Za-z0-9]{10,}|BEGIN [A-Z ]*PRIVATE KEY", re.I)
    email = re.compile(r"[\w.+-]+@(?!example\.com)[\w-]+\.[\w.-]+")
    for p in _files(".py", ".json", ".md", ".toml"):
        if p.name == "test_privacy.py" or p.name == "test_packaging.py" or p.name == "test_failures.py":
            continue  # these files hold deliberate fake leak samples
        text = p.read_text("utf-8")
        assert not secret.search(text), p
        assert not email.search(text), p


def test_data_fixtures_use_synthetic_identifiers_only():
    for p in (ROOT / "data").glob("*.json"):
        text = p.read_text("utf-8")
        for value in re.findall(r'"(?:case_id|employee_ref)":\s*"([^"]+)"', text):
            assert value.startswith("SYN-"), (p.name, value)


def test_policy_is_l1_and_gates_every_write_tool():
    policy = Policy.load()
    assert policy.autonomy.value == "L1" and policy.ceiling.value == "L1" and policy.enabled
    assert policy.tools.write == [] and policy.tools.write_requires_approval == ["it_service_desk.submit_request"]


def test_policy_rejects_autonomy_above_its_ceiling(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text('{"policy":"x","agent":"x","version":1,"autonomy":"L2","ceiling":"L1"}', encoding="utf-8")
    with pytest.raises(ValueError):
        Policy.load(bad)


def test_calendar_arithmetic_uses_only_the_configured_calendar():
    from datetime import date
    cal = WorkCalendar(weekend_days=[5, 6], holidays=[date(2026, 12, 2)])
    assert not is_working_day(date(2026, 11, 14), cal) and not is_working_day(date(2026, 12, 2), cal)
    assert subtract_working_days(date(2026, 11, 16), 10, cal) == date(2026, 11, 2)
    assert subtract_working_days(date(2026, 11, 16), 0, cal) == date(2026, 11, 16)
    assert next_working_day(date(2026, 11, 14), cal) == date(2026, 11, 16)
    with pytest.raises(ValueError):
        WorkCalendar(weekend_days=[0, 1, 2, 3, 4, 5, 6])
