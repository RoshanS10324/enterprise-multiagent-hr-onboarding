"""scripts/uc01_document_live_run.py, exercised end to end OFFLINE.

MOCKED, NOT LIVE: UC-01's real Supabase adapters run over an in-process FAKE
PostgREST, and UC-02 (unmodified) runs on its own local SQLite store with its
offline client CLIENT-ALPHA - so every label here is MOCK_*, never LIVE_*. This
proves the script's logic (independent read-back, duplicate reuse, partial-
write resume, the UC-02 gate); the live evidence is the script's own report.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
from pydantic import SecretStr

pytest.importorskip("hr_onboarding_common.orchestration")
pytest.importorskip("onboarding_task_management")

from sub_agent.crypto import generate_key_b64  # noqa: E402
from sub_agent.documents.service import DocumentIntakeService  # noqa: E402
from test_form_details_persistence import DetailsFake  # noqa: E402
from test_supabase_interim_storage import LiveLikeFake, _container  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("live_run", ROOT / "scripts" / "uc01_document_live_run.py")
live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live)
DOCUMENT = ROOT / "data" / "mock" / "documents" / "SYNTHETIC_onboarding_form_2026_populated.docx"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("UC02_STORE_MODE", "sqlite")
    monkeypatch.setenv("UC02_SQLITE_PATH", str(tmp_path / "uc02.sqlite"))
    monkeypatch.setenv("LLM_MODE", "offline")
    return tmp_path


def _intake():
    """The real intake file, moved to CLIENT-ALPHA: the only client UC-02's offline configuration has."""
    data = copy.deepcopy(live.load_intake())
    data["intake"]["client_id"] = "CLIENT-ALPHA"
    data["intake"]["uc02"].update(location="Abu Dhabi", project="zephyr")
    return data


class Db:
    """One fake database, any number of containers over it (same key)."""

    def __init__(self) -> None:
        self.fake = LiveLikeFake()
        self.fake.unique.update(DetailsFake().unique)
        self.key = generate_key_b64()

    def container(self):
        return _container(self.fake, field_encryption_key=SecretStr(self.key))

    def service(self, container):
        return DocumentIntakeService(container.onboarding_deps, details_repo=container.form_details,
                                     today=lambda: live._e2e().AS_OF)


def _run(db: Db, *, agent=None, deps=None, intake=None):
    container = db.container()
    report = {"precheck": {}, "steps": {}, "final": {}}
    return live.run(container, db.container, report, live=False, intake=intake or _intake(), document=DOCUMENT,
                    service=db.service(container), agent=agent, deps=deps)


def test_the_full_flow_creates_reads_back_and_hands_off_with_mock_labels_only(env):
    db = Db()
    report = _run(db)
    assert "stopped" not in report, report.get("stopped")
    assert report["final"] == {"persistence": "MOCK_PERSISTENCE_VERIFIED", "readback": "MOCK_READBACK_VERIFIED",
                               "uc02": "MOCK_UC02_HANDOFF_VERIFIED"}
    rb = report["steps"]["independent read-back"]
    assert rb["ok"] and not [k for k, ok in rb["checks"].items() if not ok]
    assert rb["budget"] == {"status": "COMPLETE", "currency": "AED", "total": "21500.00", "categories": {
        "VISA": "4800.00", "INSURANCE": "8000.00", "TRAVEL": "3550.00", "ACCOMMODATION": "3150.00",
        "OTHER": "2000.00"}}
    uc02 = report["steps"]["uc02 handoff + checklist"]
    assert uc02["status"] == "PASS" and uc02["task_count"] > 0 and all(uc02["checks"].values())
    assert uc02["client_id"] == "CLIENT-ALPHA" and report["identifiers"]["employee_no"] == uc02["employee_no"]
    assert len(db.fake.tables["onboarding_cases"]) == 1 and db.fake.deletes == []


def test_a_rerun_reuses_the_same_employee_and_creates_nothing_new(env):
    db = Db()
    agent, deps = live._e2e().orchestrator()
    first = _run(db, agent=agent, deps=deps)
    second = _run(db, agent=agent, deps=deps)
    assert second["steps"]["uc01 intake"]["status"] == "LINKED_EXISTING"
    assert second["identifiers"]["case_id"] == first["identifiers"]["case_id"]
    assert second["final"]["readback"] == "MOCK_READBACK_VERIFIED"
    assert second["steps"]["uc02 handoff + checklist"]["uc02_created_new_plan"] is False
    for table in ("onboarding_cases", "onboarding_forms", "person_records", "uc01_onboarding_budget"):
        assert len(db.fake.tables[table]) == 1, table


def test_an_interrupted_run_is_resumed_without_a_second_employee(env):
    db = Db()
    db.fake.unique.pop("uc01_onboarding_budget_lines")
    original = db.fake.handler
    calls = {"n": 0}

    def fail_budget_lines_once(request):
        import httpx

        if request.method == "POST" and request.url.path.endswith("uc01_onboarding_budget_lines") and not calls["n"]:
            calls["n"] += 1
            return httpx.Response(503, json={"message": "unavailable"})
        return original(request)

    db.fake.handler = fail_budget_lines_once
    first = _run(db)
    assert first["steps"]["uc01 intake"]["status"] == "PERSISTENCE_FAILED" and first["final"]["uc02"] != "MOCK_UC02_HANDOFF_VERIFIED"
    second = _run(db)
    assert "resumed_incomplete_record" in second["steps"]["uc01 intake"]["reasons"]
    assert second["final"]["readback"] == "MOCK_READBACK_VERIFIED"
    assert len(db.fake.tables["onboarding_cases"]) == 1


def test_a_case_without_its_person_record_stops_the_intake(env):
    db = Db()
    first = _run(db)
    case_id = first["identifiers"]["case_id"]
    db.fake.tables["person_records"] = [r for r in db.fake.tables["person_records"] if r["case_id"] != case_id]
    db.fake.tables["uc01_onboarding_form_details"] = []  # also not linkable as a duplicate document
    report = _run(db)
    intake = report["steps"]["uc01 intake"]
    assert intake["status"] == "PERSISTENCE_FAILED" and f"persistence:incomplete_case_exists:{case_id}" in intake["errors"]
    assert len(db.fake.tables["onboarding_cases"]) == 1


def test_missing_tables_block_everything_and_write_no_case(env):
    db = Db()
    db.fake.handler = DetailsFake(missing=("uc01_onboarding_form_details",)).handler  # a database without them
    report = {"precheck": {"form_details_tables": {"status": "BLOCKED"}}, "steps": {}, "final": {}}
    container = db.container()
    live.run(container, db.container, report, live=False, intake=_intake(), document=DOCUMENT,
             service=db.service(container))
    assert report["final"] == {"persistence": "BLOCKED", "readback": "BLOCKED", "uc02": "BLOCKED"}
    assert "UC-02 NOT triggered" in report["stopped"]


def test_labels_are_live_only_for_a_live_run():
    assert live.labels(True) == {"persistence": "LIVE_PERSISTENCE_VERIFIED", "readback": "LIVE_READBACK_VERIFIED",
                                 "uc02": "UC02_HANDOFF_VERIFIED"}
    assert all(v.startswith("MOCK_") for v in live.labels(False).values())
