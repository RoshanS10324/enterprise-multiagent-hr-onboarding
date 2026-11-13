"""UC-01 -> UC-02 handoff: where `project` comes from, and what happens when nothing supplies it.

MOCKED, NOT LIVE. The shared adapter (orchestration/adapters/uc02_tasks.py, not
modified) refuses to plan without role_id/department/manager_id/location/project.
UC-01 resolves `project` from the employee record's persisted project_name or the
intake caller's uc02.project, never from a default, and stops with
HR_INPUT_REQUIRED before UC-02 is called when neither exists.
"""

from __future__ import annotations

import pytest

pytest.importorskip("hr_onboarding_common.orchestration")
pytest.importorskip("onboarding_task_management")

from test_e2e_uc01_to_uc02 import (
    ALPHA_FIXTURE,
    STEP,
    _fake_supabase,
    _fixture,
    _run_fake,
    e2e,
    env,  # noqa: F401 - the shared offline UC-02 fixture, used below
)
from test_uc01_live_run_script import Db, _intake, _run, live

pytestmark = pytest.mark.usefixtures("env")

RB = {"client_id": "SYN-UAE-TEST", "case_id": "c-1", "employee_no": "EMP-1", "candidate_name": "Omar Synthetic",
      "candidate_email": "omar.synthetic@example.invalid", "designation": "Data Engineer",
      "joining_date": "2027-01-17", "work_location_type": "OFFICE", "department": "Data & Analytics",
      "grade": "G7", "nationality_group": None, "project_name": None,
      "form_approved_at": "2026-11-01T09:00:00+00:00"}


def _caller(**uc02):
    base = {"role_id": "data_engineer", "manager_id": "SYN-MGR-031", "location": "Dubai", "project": None}
    return {"hr_officer_id": "SYN-HR-OFFICER-31", "offer_reference": "SYN-OFFER-UAE-0031",
            "form_approval": {"approved_by_role": "hr_ops"}, "uc02": {**base, **uc02}}


# ------------------------------------------------------------------ resolution


def test_project_comes_from_the_employee_records_project_name():
    values, sources = e2e.uc02_fields({**RB, "project_name": "SYN-PROJ-A"}, _caller())
    assert values["project"] == "SYN-PROJ-A" and sources["project"] == "uc01_case.project_name"


def test_project_comes_from_the_intake_caller_when_the_record_has_none():
    values, sources = e2e.uc02_fields(RB, _caller(project="SYN-PROJ-B"))
    assert values["project"] == "SYN-PROJ-B" and sources["project"] == "intake.uc02.project"


def test_both_sources_agreeing_is_accepted_and_the_record_wins_as_source():
    values, sources = e2e.uc02_fields({**RB, "project_name": "SYN-PROJ-A"}, _caller(project="SYN-PROJ-A"))
    assert values["project"] == "SYN-PROJ-A" and sources["project"] == "uc01_case.project_name"


def test_conflicting_sources_need_hr_and_neither_value_is_chosen():
    with pytest.raises(e2e.HandoffInputRequired) as exc:
        e2e.uc02_fields({**RB, "project_name": "SYN-PROJ-A"}, _caller(project="SYN-PROJ-B"))
    assert exc.value.missing == [] and exc.value.conflicts == {
        "project": {"uc01_case.project_name": "SYN-PROJ-A", "intake.uc02.project": "SYN-PROJ-B"}}


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_missing_or_blank_project_is_reported_never_defaulted(blank):
    with pytest.raises(e2e.HandoffInputRequired) as exc:
        e2e.uc02_fields({**RB, "project_name": blank}, _caller(project=blank))
    assert exc.value.missing == ["project"]
    assert "HR input required" in str(exc.value) and "project" in str(exc.value)


def test_every_missing_required_field_is_reported_at_once_in_contract_order():
    with pytest.raises(e2e.HandoffInputRequired) as exc:
        e2e.uc02_fields({**RB, "department": None}, _caller(manager_id=None, location=""))
    assert exc.value.missing == ["department", "manager_id", "location", "project"]


def test_the_required_list_matches_the_shared_adapter():
    import inspect

    from hr_onboarding_common.orchestration.adapters import uc02_tasks

    src = inspect.getsource(uc02_tasks.call_uc02_plan_tasks)
    assert "for f in (" + ", ".join(f'"{f}"' for f in e2e.UC02_REQUIRED_FIELDS) + ")" in src


def test_the_contract_payload_carries_the_resolved_project():
    inp = e2e.handoff({**RB, "project_name": "SYN-PROJ-A"}, {"intake": _caller()})
    assert inp.project == "SYN-PROJ-A" and inp.department == "Data & Analytics" and inp.role_id == "data_engineer"


def test_the_contract_payload_is_never_built_without_a_project():
    with pytest.raises(e2e.HandoffInputRequired):
        e2e.handoff(RB, {"intake": _caller()})


# ------------------------------------------------------------------ the flows


class _NeverCalled:
    def __getattr__(self, name):
        raise AssertionError(f"UC-02/orchestrator touched ({name}) although the handoff input was incomplete")


def test_live_script_blocks_with_hr_input_required_and_never_calls_uc02():
    db = Db()
    intake = _intake()
    intake["intake"]["uc02"]["project"] = None
    report = _run(db, intake=intake, agent=_NeverCalled(), deps=_NeverCalled())
    assert report["final"]["persistence"] == "MOCK_PERSISTENCE_VERIFIED"
    assert report["final"]["readback"] == "MOCK_READBACK_VERIFIED"
    assert report["final"]["uc02"] == "BLOCKED"
    step = report["steps"]["uc02 handoff + checklist"]
    assert step["error_code"] == "HR_INPUT_REQUIRED" and step["hr_input_required"] == ["project"]
    assert step["uc02_called"] is False and "handoff --live --case-id" in step["next_action"]


def test_the_existing_case_is_retried_once_hr_supplies_the_project_and_no_employee_is_added():
    db = Db()
    intake = _intake()
    intake["intake"]["uc02"]["project"] = None
    first = _run(db, intake=intake, agent=_NeverCalled(), deps=_NeverCalled())
    case_id = first["identifiers"]["case_id"]

    intake["intake"]["uc02"]["project"] = "zephyr"  # HR's answer (CLIENT-ALPHA's offline configuration)
    retried = live.uc02_handoff(db.container(), case_id, intake)
    assert retried["status"] == "PASS" and retried["case_id"] == case_id and retried["task_count"] > 0
    assert retried["uc02_field_sources"]["project"] == "intake.uc02.project"
    assert len(db.fake.tables["onboarding_cases"]) == 1 and db.fake.deletes == []


def test_e2e_pipeline_stops_at_step_7_without_a_project(tmp_path):
    def drop_project(data):
        data["intake"]["uc02"]["project"] = None
        if data["intake"].get("placement"):
            data["intake"]["placement"]["project_name"] = None

    _, container = _fake_supabase()
    report = _run_fake(container, fixture_path=_fixture(tmp_path, drop_project, source=ALPHA_FIXTURE),
                       agent=_NeverCalled(), deps=_NeverCalled())
    step = report["steps"][STEP["7"]]
    assert step["status"] == "BLOCKED" and step["hr_input_required"] == ["project"]
    assert report["steps"][STEP["8"]]["status"] == "NOT VERIFIED" and "UC-02 NOT triggered" in report["stopped"]
