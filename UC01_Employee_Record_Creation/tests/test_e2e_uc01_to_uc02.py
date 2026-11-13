"""Mock extracted data -> UC-01 -> persistence -> read-back -> orchestrator -> UC-02.

Offline, no network, no Supabase write. Two kinds of UC-01 storage are used:

* in-memory repositories - by the strict database-first rule these must STOP
  before UC-02;
* the real UC-01 Supabase adapters over an in-process FAKE PostgREST
  (LiveLikeFake) - these pass the gate and are labelled MOCK PASS. They prove
  the code path, NOT live Supabase persistence.

UC-02 runs on its own SQLite file in tmp_path with its synthetic client
CLIENT-ALPHA (the only client its offline configuration has).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("hr_onboarding_common.orchestration")
pytest.importorskip("onboarding_task_management")

from sub_agent.crypto import generate_key_b64  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "e2e_uc01_to_uc02.py"
_spec = importlib.util.spec_from_file_location("e2e_uc01_to_uc02", SCRIPT)
e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e)

STEP = {s.split(" ", 1)[0]: s for s in e2e.STEPS}
#: UC-02's offline configuration knows only CLIENT-ALPHA, so runs that reach UC-02 use this fixture.
ALPHA_FIXTURE = e2e.UC01 / "data" / "mock" / "extracted_employee_data.json"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("UC02_STORE_MODE", "sqlite")
    monkeypatch.setenv("UC02_SQLITE_PATH", str(tmp_path / "uc02.sqlite"))
    monkeypatch.setenv("LLM_MODE", "offline")
    return tmp_path


def _fixture(tmp_path, mutate=None, source=None) -> Path:
    data = json.loads((source or e2e.FIXTURE).read_text(encoding="utf-8"))
    if mutate:
        mutate(data)
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _status(report, n):
    return report["steps"][STEP[n]]["status"]


def _fake_supabase():
    """UC-01's real Supabase adapters over a FAKE PostgREST (not the live database)."""
    from test_supabase_interim_storage import LiveLikeFake, _container

    fake = LiveLikeFake()
    return fake, _container(fake)


def _run_fake(container, **kw):
    kw.setdefault("fixture_path", ALPHA_FIXTURE)
    return e2e.run_pipeline(persistence="supabase", container=container, storage_label="MOCK PASS", **kw)


# ------------------------------------------------------------------ the sample employee fixture


def test_the_sample_employee_is_synthetic_and_for_an_existing_synthetic_client(env):
    from sub_agent.documents.providers import MockExtractionProvider

    data = json.loads(e2e.FIXTURE.read_text(encoding="utf-8"))
    assert e2e.FIXTURE.name == "sample_employee_record.json" and data["synthetic"] is True
    assert data["intake"]["client_id"] == "SYN-UAE-TEST"
    assert data["intake"]["candidate_email"].endswith("@example.invalid")
    x = MockExtractionProvider(e2e.FIXTURE).extract()
    assert x.metadata.template_recognised and not x.metadata.invalid_fields


# ------------------------------------------------------------------ strict database-first gate


def test_an_in_memory_employee_never_reaches_uc02(env):
    agent, deps = e2e.orchestrator()
    report = e2e.run_pipeline(agent=agent, deps=deps)  # the sample employee, UC-01 in memory
    assert report["steps"][STEP["4"]]["uc01_status"] == "CREATED"
    assert _status(report, "5") == _status(report, "6") == "MOCK PASS"
    assert report["uc02_gate"] == {"employee_saved_to_supabase": False, "employee_readback_verified": True}
    assert report["stopped"].startswith("STOP - DO NOT TRIGGER UC-02")
    assert all(_status(report, n) == "NOT VERIFIED" for n in ("7", "8", "9", "10", "11", "12"))
    assert deps._uc02_sub_agent is None  # the UC-02 adapter never ran


# ------------------------------------------------------------------ 1 + 8. valid employee -> UC-02 (fake DB)


def test_a_saved_and_read_back_employee_triggers_uc02(env):
    fake, container = _fake_supabase()
    report = _run_fake(container)
    assert "stopped" not in report, report.get("stopped")
    assert report["uc02_gate"] == {"employee_saved_to_supabase": True, "employee_readback_verified": True}
    assert report["steps"][STEP["4"]]["uc01_status"] == "CREATED"
    assert [_status(report, n) for n in ("1", "2", "3", "4", "7", "8", "9", "12")] == ["PASS"] * 8
    # fake UC-01 database, UC-02 on its local SQLite store: never reported as a live pass
    assert all(_status(report, n) == "MOCK PASS" for n in ("5", "6", "10", "11"))
    ids = report["identifiers"]
    assert ids["uc02_employee_id"] == ids["employee_no"] == "DXB-000001"  # number from the database rpc
    assert fake.tables["onboarding_cases"][0]["case_id"] == ids["case_id"]  # written through PostgREST
    assert "17,000" not in json.dumps(fake.tables["onboarding_forms"])  # encrypted before it left UC-01
    uc02 = report["steps"][STEP["9"]]
    assert uc02["tasks"] and all(t["owner"] and t["due_date"] for t in uc02["tasks"])
    assert any(t["depends_on"] for t in uc02["tasks"]) and uc02["milestones"]
    assert fake.deletes == []


def test_the_handoff_comes_from_the_read_back_and_carries_no_sensitive_data(env):
    _, container = _fake_supabase()
    report = _run_fake(container)
    payload = report["steps"][STEP["7"]]["payload"]
    rb = report["steps"][STEP["6"]]["read_back"]
    assert payload["employee_id"] == rb["employee_no"] and payload["candidate_name"] == rb["candidate_name"]
    from hr_onboarding_common.orchestration.contracts import OnboardingCaseInput

    OnboardingCaseInput.model_validate(payload)
    text = json.dumps(payload).lower()
    for term in ("salary", "17,000", "passport", "emirates", "mobile", "father", "mother"):
        assert term not in text


# ------------------------------------------------------------------ 2. missing field


def test_missing_required_field_fails_validation_and_creates_nothing(env):
    path = _fixture(env, lambda d: d["extracted"]["employee"].update(employee_name=None))
    fake, container = _fake_supabase()
    report = _run_fake(container, fixture_path=path)
    assert _status(report, "3") == "FAIL" and report["steps"][STEP["3"]]["uc01_status"] == "VALIDATION_FAILED"
    assert "missing:employee.employee_name" in report["steps"][STEP["3"]]["errors"]
    assert fake.tables.get("onboarding_cases", []) == []
    assert _status(report, "8") == "NOT VERIFIED"


# ------------------------------------------------------------------ 3 + 9. duplicate / repeated run


def test_a_repeated_run_creates_no_duplicate_employee_or_tasks(env):
    fake, container = _fake_supabase()
    agent, deps = e2e.orchestrator()
    first = _run_fake(container, agent=agent, deps=deps)
    second = _run_fake(container, agent=agent, deps=deps)
    assert second["steps"][STEP["4"]]["uc01_status"] == "LINKED_EXISTING"
    assert second["identifiers"]["case_id"] == first["identifiers"]["case_id"]
    assert second["steps"][STEP["8"]]["uc02_created_new_plan"] is False
    assert second["steps"][STEP["9"]]["tasks"] == first["steps"][STEP["9"]]["tasks"]
    assert len(fake.tables["onboarding_cases"]) == 1


# ------------------------------------------------------------------ 4. missing approval


def test_missing_form_approval_is_held_by_the_existing_start_gate(env):
    path = _fixture(env, lambda d: d["intake"]["form_approval"].update(
        form_status="AWAITING_APPROVAL", approved_by=None, approved_at=None))
    fake, container = _fake_supabase()
    report = _run_fake(container, fixture_path=path)
    step = report["steps"][STEP["3"]]
    assert step["status"] == "BLOCKED" and step["uc01_status"] == "HELD" and "form not approved" in step["reasons"]
    assert fake.tables.get("onboarding_cases", []) == []
    assert _status(report, "8") == "NOT VERIFIED"


# ------------------------------------------------------------------ 6. insert failure


def test_an_insert_failure_never_triggers_uc02(env, monkeypatch):
    from sub_agent.adapters.supabase.client import SupabaseError

    _, container = _fake_supabase()
    agent, deps = e2e.orchestrator()

    def refuse(case):
        raise SupabaseError("simulated insert failure")

    monkeypatch.setattr(container.onboarding_deps.case_repo, "save", refuse)
    report = _run_fake(container, agent=agent, deps=deps)
    assert _status(report, "5") == "FAIL" and "UC-02 NOT triggered" in report["stopped"]
    assert deps._uc02_sub_agent is None  # the UC-02 adapter never ran


# ------------------------------------------------------------------ 7. read-back failure


def test_a_read_back_failure_never_triggers_uc02(env, monkeypatch):
    _, container = _fake_supabase()
    agent, deps = e2e.orchestrator()
    monkeypatch.setattr(container.onboarding_deps.case_repo, "get", lambda case_id: None)
    report = _run_fake(container, agent=agent, deps=deps)
    # UC-01 itself reads the case back before reporting success, so the
    # missing case is caught at creation (step 4), before the script's own
    # read-back (step 6) - and UC-02 is never reached.
    assert _status(report, "4") == "FAIL"
    assert "readback:case:<not found>" in report["steps"][STEP["4"]]["errors"]
    assert _status(report, "6") == "NOT VERIFIED"
    assert deps._uc02_sub_agent is None


# ------------------------------------------------------------------ UC-02 failure


def test_a_uc02_failure_is_reported_and_the_employee_stays_intact(env, monkeypatch):
    from onboarding_task_management.sub_agent.adapter import UC02SubAgent

    def boom(self, request):
        raise RuntimeError("simulated UC-02 failure")

    monkeypatch.setattr(UC02SubAgent, "run", boom)
    fake, container = _fake_supabase()
    report = _run_fake(container)
    assert _status(report, "8") == "FAIL" and report["employee_intact_after_uc02_failure"] is True
    assert len(fake.tables["onboarding_cases"]) == 1 and fake.deletes == []


# ------------------------------------------------------------------ 10. restart (fake DB)


def test_the_employee_is_read_from_the_database_by_a_new_container(env):
    from test_supabase_interim_storage import _container

    fake, container = _fake_supabase()
    report = _run_fake(container)
    restarted = _container(fake)  # a new container over the same (fake) database: nothing kept in memory
    case = restarted.onboarding_deps.case_repo.get(report["identifiers"]["case_id"])
    assert case is not None and case.employee_no == report["identifiers"]["employee_no"]


# ------------------------------------------------------------------ 5. missing key / readiness


def test_supabase_mode_without_a_key_is_blocked_before_any_write(env, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example-project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-only")
    monkeypatch.setenv("UC01_FIELD_ENCRYPTION_KEY", "")
    report = e2e.run_pipeline(persistence="supabase")
    assert _status(report, "5") == "BLOCKED" and "UC01_FIELD_ENCRYPTION_KEY" in report["steps"][STEP["5"]]["blockers"][0]
    assert _status(report, "3") == "NOT VERIFIED" and _status(report, "8") == "NOT VERIFIED"


def test_supabase_mode_with_a_key_names_every_missing_repository(env, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example-project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-only")
    monkeypatch.setenv("UC01_FIELD_ENCRYPTION_KEY", generate_key_b64())
    report = e2e.run_pipeline(persistence="supabase")  # no network: the container makes no call
    blockers = report["steps"][STEP["5"]]["blockers"]
    # Offers (B3) are only needed by the email path, so the document path lists the other five.
    for name in ("UnsupportedCaseRepository", "UnsupportedAuditLog", "UnsupportedApprovalRepository",
                 "UnsupportedEmployeeNumberGenerator", "UnsupportedDocumentStore"):
        assert any(name in b for b in blockers)
    assert not any("UnsupportedOfferRepository" in b for b in blockers)
    assert "UC-02 was NOT triggered" in report["stopped"]


# ------------------------------------------------------------------ mock extraction provider


def test_the_mock_provider_returns_the_real_extraction_model(env):
    from sub_agent.documents.models import FieldStatus, OnboardingDocumentExtraction
    from sub_agent.documents.providers import MockExtractionProvider

    x = MockExtractionProvider(e2e.FIXTURE).extract()
    assert isinstance(x, OnboardingDocumentExtraction) and x.metadata.template_recognised
    assert x.employee.final_salary.value.get_secret_value().startswith("SYNTHETIC")
    assert x.documents.visa_copy.status == FieldStatus.MISSING  # null stays missing
    assert x.employee.designation.source == "mock:extracted.employee.designation"


def test_the_mock_provider_type_checks_like_extraction(env):
    from sub_agent.documents.models import FieldStatus
    from sub_agent.documents.providers import MockExtractionProvider

    def mutate(d):
        d["extracted"]["employee"]["joining_date"] = "11/01/2027"
        d["extracted"]["employee"]["probation_months"] = 4
        d["extracted"]["employee"]["reporting_to"] = {"value": "SYN Lead", "status": "UNCERTAIN", "note": "two names"}

    x = MockExtractionProvider(_fixture(env, mutate)).extract()
    assert x.employee.joining_date.status == FieldStatus.INVALID
    assert x.employee.probation_months.status == FieldStatus.INVALID
    assert x.employee.reporting_to.status == FieldStatus.UNCERTAIN


def test_the_mock_provider_refuses_unmarked_or_malformed_fixtures(env):
    from sub_agent.documents.providers import MockExtractionError, MockExtractionProvider

    with pytest.raises(MockExtractionError, match="synthetic"):
        MockExtractionProvider(_fixture(env, lambda d: d.update(synthetic=False))).extract()
    with pytest.raises(MockExtractionError, match="unknown fields"):
        MockExtractionProvider(_fixture(env, lambda d: d["extracted"]["employee"].update(passport_no="X"))).extract()


def test_mock_and_docx_providers_share_one_downstream_path(env):
    report_mock = e2e.run_pipeline(source="mock")
    report_docx = e2e.run_pipeline(source="docx", fixture_path=_fixture(env, lambda d: (
        d["extracted"]["employee"].update(employee_name="Aisha Synthetic", joining_date="2027-01-11"),
        d["intake"].update(candidate_email="aisha.synthetic@example.invalid")), source=ALPHA_FIXTURE),
        docx=e2e.UC01 / "data" / "mock" / "documents" / "SYNTHETIC_onboarding_complete.docx")
    assert report_mock["steps"][STEP["2"]]["provider"] == "MockExtractionProvider"
    assert report_docx["steps"][STEP["2"]]["provider"] == "DocxExtractionProvider"
    assert _status(report_docx, "4") == _status(report_mock, "4") == "PASS"
    # Both in memory: both stop at the database-first rule in the same place.
    assert report_mock["stopped"] == report_docx["stopped"]


# ------------------------------------------------------------------ restart (new process)


def test_a_memory_run_stops_before_uc02_and_restart_has_nothing_to_verify(tmp_path):
    out = tmp_path / "run"
    run = subprocess.run([sys.executable, str(SCRIPT), "run", "--out", str(out)],
                         capture_output=True, text=True, timeout=600)
    assert run.returncode == 2, run.stderr[-2000:]  # stopped by the database-first rule
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert report["stopped"].startswith("STOP - DO NOT TRIGGER UC-02")
    again = subprocess.run([sys.executable, str(SCRIPT), "verify-restart", "--out", str(out)],
                           capture_output=True, text=True, timeout=600)
    assert again.returncode == 0, again.stderr[-2000:]
    restart = json.loads((out / "restart_report.json").read_text(encoding="utf-8"))
    assert restart["status"] == "NOT VERIFIED"
