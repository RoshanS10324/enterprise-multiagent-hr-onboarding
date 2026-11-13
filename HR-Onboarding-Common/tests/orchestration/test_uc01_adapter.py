from __future__ import annotations

import pytest

from _helpers import make_actor
from hr_onboarding_common.orchestration.adapters.uc01_intake import call_uc01_intake
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state


class _FakePolicy:
    settings = {"intake_trigger_roles": ["hr_admin"]}


class _FakeDeps:
    def __init__(self, worker_pool):
        self.policy = _FakePolicy()
        self.worker_pool = worker_pool


def _state(roles=("hr_admin",), form=True, placement=True):
    trigger = {"offer_accepted": True, "hiring_freeze": False, "id_number": "ID-1"}
    if placement:  # UC01 opens a case only with a stated location (CasePlacement.location_name)
        trigger["placement"] = {"location_name": "Dubai", "department": "Engineering"}
    if form:
        trigger["form"] = {
            "form_id": "F1", "client_id": "CL1", "version": 1, "candidate_name": "Arun Kumar",
            "candidate_email": "arun@example.com", "designation": "Engineer", "joining_date": "2026-11-16",
            "work_location_type": "OFFICE", "salary_and_terms": "x", "status": "APPROVED",
            "approved_by": "hr", "approved_at": "2026-10-20T00:00:00Z",
        }
    return new_state(case_id="C1", client_id="CL1", correlation_id="corr1",
                      actor=make_actor(roles=roles), intake_trigger=trigger)


def test_unauthorized_actor_is_rejected_before_uc01_is_called():
    """Orchestrator boundary check: UC01 does not enforce caller identity itself (verified), so
    the orchestrator's own policy must reject an unauthorized role BEFORE the worker is touched."""
    calls = []

    class _RecordingWorkerPool:
        def get(self, uc_id):
            calls.append(uc_id)
            raise AssertionError("uc01 worker must never be called for an unauthorized actor")

    deps = _FakeDeps(_RecordingWorkerPool())
    state = _state(roles=("random_unaffiliated_role",))

    result = call_uc01_intake(state, deps)

    assert calls == []
    assert result["stage_statuses"][Stage.INTAKE.value] == StageStatus.BLOCKED.value
    assert result["error_code"] == "POLICY_BREACH"


def test_case_opened_outcome_completes_intake_stage(uc01_worker):
    from hr_onboarding_common.orchestration.workers.config import EXPECTED_ENTRY  # noqa: F401 (sanity import)

    deps = _FakeDeps(worker_pool=_SingleWorkerPool(uc01_worker))
    state = _state()
    result = call_uc01_intake(state, deps)
    assert result["stage_statuses"][Stage.INTAKE.value] == StageStatus.COMPLETE.value
    assert result["case_id"]  # replaced with UC01's real case id
    assert result.get("error_code") is None


def test_missing_form_is_held_not_treated_as_approved(uc01_worker):
    deps = _FakeDeps(worker_pool=_SingleWorkerPool(uc01_worker))
    state = _state(form=False)
    result = call_uc01_intake(state, deps)
    assert result["stage_statuses"][Stage.INTAKE.value] == StageStatus.BLOCKED.value
    assert result["blockers"], "a HELD/PAUSED outcome must record a reason, not silently proceed"


def test_missing_placement_is_held_by_uc01_not_defaulted(uc01_worker):
    """UC01 requires location_name. The orchestrator forwards only a stated placement, so without one
    UC01 itself holds the case (reason location_name_required) -- nothing is invented on the way."""
    deps = _FakeDeps(worker_pool=_SingleWorkerPool(uc01_worker))
    result = call_uc01_intake(_state(placement=False), deps)
    assert result["stage_statuses"][Stage.INTAKE.value] == StageStatus.BLOCKED.value
    assert [b["reason"] for b in result["blockers"]] == ["location_name_required"]


class _SingleWorkerPool:
    def __init__(self, worker):
        self._worker = worker

    def get(self, uc_id):
        assert uc_id == "uc01"
        return self._worker
