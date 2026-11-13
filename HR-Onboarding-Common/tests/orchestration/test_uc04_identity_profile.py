"""UC04's identifier profile through the real UC04 worker (UC04 guardrails/identity.py).

Unset (the default) keeps LOCAL_SIMULATION: synthetic SYN-* identifiers only (GR-13), so the platform's uuid case
ids are refused as INVALID_INPUT. UC04_IDENTITY_PROFILE=PLATFORM -- read by the worker only and fixed before UC04's
contracts load, as UC04's own proposal describes (docs/common_patches/README.md) -- accepts the platform's
identifiers, and UC04 then evaluates the case under its own rules. An unknown profile name is refused."""

from __future__ import annotations

import pytest

from _helpers import UC_ROOTS, require_uc, venv_python
from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import call_uc04_equipment_access
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state

CONFIG = UC_ROOTS["uc04"] / "data" / "client_alpha_config.json"
PLATFORM_CASE = "8bb710fb-f37a-4c88-bcd2-093132cf9c6f"  # the shape of a UC01 case id


class _Pool:
    def __init__(self, worker):
        self.worker = worker

    def get(self, uc_id):
        return self.worker


def _platform_state():
    state = new_state(case_id=PLATFORM_CASE, client_id="client_alpha", correlation_id="corr-uc04-profile",
                      actor={"on_behalf_of": "user:hr-officer-01", "roles": ["hr_onboarding_officer"]},
                      intake_trigger={}, uc04_client_config_ref=str(CONFIG),
                      uc02_role_context={"role_id": "software_engineer", "department": "Engineering",
                                         "manager_id": "TL-TEST-001", "location": "Dubai", "project": "apollo"})
    state["employee_id"] = "EMP-000123"
    state["joining_date"] = "2026-11-16"
    return state


def _worker(monkeypatch, profile):
    require_uc("uc04")
    from hr_onboarding_common.orchestration.workers.config import WorkerConfig
    from hr_onboarding_common.orchestration.workers.uc_worker_client import UcWorker

    if profile is None:
        monkeypatch.setenv("UC04_IDENTITY_PROFILE", "")  # default profile (an empty value keeps .env from setting it)
    else:
        monkeypatch.setenv("UC04_IDENTITY_PROFILE", profile)
    worker = UcWorker(WorkerConfig(uc_id="uc04", python_executable=venv_python(UC_ROOTS["uc04"]),
                                   src_dir=UC_ROOTS["uc04"] / "src"))
    worker.start()
    return worker


def _run(worker):
    deps = OrchestratorDeps(ctx=None, worker_pool=_Pool(worker), uc03_repo_root=UC_ROOTS["uc03"])
    return call_uc04_equipment_access(_platform_state(), deps)


def test_by_default_uc04_still_refuses_platform_identifiers_as_a_named_dependency(monkeypatch):
    worker = _worker(monkeypatch, None)
    try:
        result = _run(worker)
    finally:
        worker.stop()
    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.BLOCKED.value
    assert result["stage_errors"][Stage.EQUIPMENT_ACCESS.value]["code"] == "UC04_IDENTITY_PROFILE_NOT_ENABLED"
    (blocker,) = result["blockers"]
    assert blocker["required_from"] == "Platform / integration owner" and blocker["required_data"] == "UC04_IDENTITY_PROFILE"
    assert result.get("equipment_access_result") is None  # UC04 evaluated nothing


def test_the_platform_profile_lets_uc04_evaluate_a_platform_case(monkeypatch):
    worker = _worker(monkeypatch, "PLATFORM")
    try:
        result = _run(worker)
    finally:
        worker.stop()
    assert result.get("error_code") != "INVALID_INPUT", result.get("error_detail")
    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] != StageStatus.FAILED.value
    assert result.get("equipment_access_result") is not None  # UC04's own verdict, under its own rules


def test_an_unknown_profile_stops_the_worker_instead_of_widening(monkeypatch):
    from hr_onboarding_common.orchestration.workers.uc_worker_client import WorkerCrashedError

    with pytest.raises(WorkerCrashedError):  # the worker refuses to start: fail closed
        worker = _worker(monkeypatch, "EVERYTHING")
        worker.stop()


def test_real_uc04_without_requested_items_blocks_with_a_resolvable_blocker(monkeypatch):
    worker = _worker(monkeypatch, "PLATFORM")
    try:
        result = _run(worker)
    finally:
        worker.stop()
    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.BLOCKED.value
    (b,) = [x for x in result["blockers"] if x["code"] == "NO_ITEMS_REQUESTED"]
    assert b["resolution"] == "provide_equipment_access_requests" and b["required_from"] == "HR onboarding officer"


def test_real_uc04_evaluates_requested_catalogue_items(monkeypatch):
    worker = _worker(monkeypatch, "PLATFORM")
    state = _platform_state()
    state["uc02_role_context"].update(requested_equipment=["laptop-standard"], requested_access=["email-teams-account"])
    try:
        deps = OrchestratorDeps(ctx=None, worker_pool=_Pool(worker), uc03_repo_root=UC_ROOTS["uc03"])
        result = call_uc04_equipment_access(state, deps)
    finally:
        worker.stop()
    assert not [b for b in result["blockers"] if b["code"] == "NO_ITEMS_REQUESTED"]
    items = {i["item_id"] for i in result["equipment_access_result"]["items"]}
    assert {"laptop-standard", "email-teams-account"} <= items  # UC04's own evaluation of each request
