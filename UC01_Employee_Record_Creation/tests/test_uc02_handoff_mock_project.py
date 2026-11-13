"""UC-01 -> UC-02 handoff with the MOCK project value SYNTHETIC-TEST-PROJECT, fully isolated.

MOCKED, NOT LIVE. Labels here are MOCK_* only.
  * UC-01: its real Supabase adapters over an in-process FAKE PostgREST (LiveLikeFake);
  * UC-02 (unmodified): its real sub-agent on a SQLite file in pytest's tmp_path,
    with CLIENT-ALPHA's offline configuration (UC-02 has no offline configuration for
    SYN-UAE-TEST);
  * LLM offline; every non-loopback socket connection is refused, so nothing here
    can reach the live Supabase project. The live employee EMP-000003 is never
    read or written by these tests.

CLIENT-ALPHA's configuration filters two templates by project (access_apollo,
access_zephyr), which makes the project UC-02 actually received observable.
"""

from __future__ import annotations

import socket
import sqlite3

import pytest

pytest.importorskip("hr_onboarding_common.orchestration")
pytest.importorskip("onboarding_task_management")

from test_e2e_uc01_to_uc02 import env  # noqa: F401 - the shared offline UC-02 fixture, used below
from test_uc01_live_run_script import Db, _intake, _run, live

MOCK_PROJECT = "SYNTHETIC-TEST-PROJECT"
PROJECT_FILTERED = {"access_apollo", "access_zephyr"}  # CLIENT-ALPHA templates restricted to other projects


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, env):  # noqa: F811
    """Refuse every non-loopback connection: proves the run cannot touch live Supabase."""
    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"network access attempted in an offline test: {host}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    return env


def _mock_intake(*, caller_project=MOCK_PROJECT, case_project=None):
    data = _intake()  # the real intake file, moved to CLIENT-ALPHA (UC-02's offline client)
    data["intake"]["uc02"]["project"] = caller_project
    if case_project is not None:
        data["intake"]["placement"] = {**data["intake"]["placement"], "project_name": case_project}
    return data


def _fresh_uc02_view(client_id: str, onboarding_id: str):
    """Read the plan back through a NEW UC-02 service on the same SQLite file: persistence, not memory."""
    from onboarding_task_management.sub_agent.runtime import build_service

    e2e = live._e2e()
    return build_service().checklist(e2e.uc02_actor(client_id, "SYN-HR-OFFICER-01"), onboarding_id, e2e.AS_OF)


# ------------------------------------------------------------------ success


def test_the_mock_project_reaches_uc02_and_the_checklist_is_generated_and_persisted(offline_only):
    db = Db()
    agent, deps = live._e2e().orchestrator()
    report = _run(db, intake=_mock_intake(), agent=agent, deps=deps)

    assert "stopped" not in report, report.get("stopped")
    assert report["final"] == {"persistence": "MOCK_PERSISTENCE_VERIFIED", "readback": "MOCK_READBACK_VERIFIED",
                               "uc02": "MOCK_UC02_HANDOFF_VERIFIED"}
    step = report["steps"]["uc02 handoff + checklist"]
    assert step["status"] == "PASS" and all(step["checks"].values()), step.get("checks")
    assert step["uc02_field_sources"]["project"] == "intake.uc02.project"
    assert step["client_id"] == "CLIENT-ALPHA" and step["task_count"] > 0

    # Read back from UC-02's own store with a fresh service.
    view = _fresh_uc02_view("CLIENT-ALPHA", step["onboarding_id"])
    assert view.context.project == MOCK_PROJECT
    assert view.employee_id == report["identifiers"]["employee_no"]
    keys = {e.template_key for e in view.entries}
    assert keys == {t["template_key"] for t in step["tasks"]}
    # The project filter was applied to the value UC-01 sent: other projects' access tasks are excluded.
    assert not keys & PROJECT_FILTERED
    excluded = {d.template_key: d for d in view.excluded}
    assert PROJECT_FILTERED <= set(excluded)
    # Every task has owner/status/due date; every dependency points at a task in the same plan.
    ids = {e.task_id for e in view.entries}
    assert all(e.owner and e.status and e.due_date for e in view.entries)
    assert all(d in ids for e in view.entries for d in e.depends_on)
    assert any(e.depends_on for e in view.entries), "the plan should carry dependencies"


def test_uc02_rows_are_in_the_isolated_sqlite_file_only(offline_only):
    db = Db()
    report = _run(db, intake=_mock_intake())
    assert report["final"]["uc02"] == "MOCK_UC02_HANDOFF_VERIFIED"
    path = offline_only / "uc02.sqlite"
    assert path.exists()
    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
        dumped = "\n".join(conn.iterdump())
    assert tables and MOCK_PROJECT in dumped and report["identifiers"]["employee_no"] in dumped
    # UC-01's side lived only in the fake PostgREST: exactly one synthetic employee, nothing deleted.
    assert len(db.fake.tables["onboarding_cases"]) == 1 and db.fake.deletes == []


def test_the_case_project_name_is_used_when_the_caller_gives_none(offline_only):
    db = Db()
    report = _run(db, intake=_mock_intake(caller_project=None, case_project=MOCK_PROJECT))
    assert db.fake.tables["onboarding_cases"][0]["project_name"] == MOCK_PROJECT
    step = report["steps"]["uc02 handoff + checklist"]
    assert step["status"] == "PASS" and step["uc02_field_sources"]["project"] == "uc01_case.project_name"
    assert _fresh_uc02_view("CLIENT-ALPHA", step["onboarding_id"]).context.project == MOCK_PROJECT


def test_matching_case_and_caller_projects_hand_off_once(offline_only):
    db = Db()
    report = _run(db, intake=_mock_intake(case_project=MOCK_PROJECT))
    assert report["final"]["uc02"] == "MOCK_UC02_HANDOFF_VERIFIED"


# ------------------------------------------------------------------ refusals (UC-02 never called)


class _NeverCalled:
    def __getattr__(self, name):
        raise AssertionError(f"UC-02/orchestrator touched ({name}) although the handoff input was refused")


def test_missing_project_is_hr_input_required_and_uc02_is_not_called(offline_only):
    db = Db()
    report = _run(db, intake=_mock_intake(caller_project=None), agent=_NeverCalled(), deps=_NeverCalled())
    assert report["final"]["readback"] == "MOCK_READBACK_VERIFIED" and report["final"]["uc02"] == "BLOCKED"
    step = report["steps"]["uc02 handoff + checklist"]
    assert step["error_code"] == "HR_INPUT_REQUIRED" and step["hr_input_required"] == ["project"]
    assert step["uc02_called"] is False and step["conflicts"] == {}
    assert not (offline_only / "uc02.sqlite").exists() or "SYNTHETIC" not in "\n".join(
        sqlite3.connect(offline_only / "uc02.sqlite").iterdump())


def test_conflicting_projects_are_hr_input_required_and_neither_is_used(offline_only):
    db = Db()
    report = _run(db, intake=_mock_intake(case_project="SYNTHETIC-OTHER-PROJECT"),
                  agent=_NeverCalled(), deps=_NeverCalled())
    step = report["steps"]["uc02 handoff + checklist"]
    assert report["final"]["uc02"] == "BLOCKED" and step["error_code"] == "HR_INPUT_REQUIRED"
    assert step["hr_input_required"] == [] and step["uc02_called"] is False
    assert step["conflicts"] == {"project": {"uc01_case.project_name": "SYNTHETIC-OTHER-PROJECT",
                                             "intake.uc02.project": MOCK_PROJECT}}
    # Neither value was overwritten: the case keeps its own project, the caller file is untouched.
    assert db.fake.tables["onboarding_cases"][0]["project_name"] == "SYNTHETIC-OTHER-PROJECT"


def test_after_hr_resolves_the_conflict_the_same_case_hands_off_without_a_second_employee(offline_only):
    db = Db()
    intake = _mock_intake(case_project=MOCK_PROJECT, caller_project="SYNTHETIC-OTHER-PROJECT")
    first = _run(db, intake=intake, agent=_NeverCalled(), deps=_NeverCalled())
    assert first["final"]["uc02"] == "BLOCKED"
    intake["intake"]["uc02"]["project"] = MOCK_PROJECT  # HR's correction of the caller value
    retried = live.uc02_handoff(db.container(), first["identifiers"]["case_id"], intake)
    assert retried["status"] == "PASS" and retried["case_id"] == first["identifiers"]["case_id"]
    assert len(db.fake.tables["onboarding_cases"]) == 1 and db.fake.deletes == []
