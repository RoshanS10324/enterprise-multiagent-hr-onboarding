"""UC04 and UC05 in their Supabase modes, at the orchestrator's side. No network, no database.

  * UC04_CONFIG_SOURCE=supabase: UC04 is evaluated only on configuration from Supabase. The client's section exists but
    its projection to UC04's CaseConfig is undecided -> BLOCKED naming the table, record and configuration; a missing
    section -> UC04_CONFIGURATION_MISSING with the same fields; an unreadable Supabase -> UC04_CONFIG_UNAVAILABLE.
    The synthetic files are never read and UC04 is never called.
  * UC04_STATE_MODE=supabase stops the UC04 worker at startup when it cannot work (no PLATFORM identity profile, no
    Supabase settings, an unknown mode) -- never a silent fallback to memory.
  * UC05: a simulated welcome send is SENT_SIMULATED, never "SENT"; every scheduled session carries id, type, status,
    start/end (UTC), timezone, link and location (None when UC05 holds none -- never invented).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
import pytest

from _helpers import UC_ROOTS, require_uc, venv_python
from hr_onboarding_common.orchestration.adapters import uc05_orientation as uc05
from hr_onboarding_common.orchestration.contracts import Actor
from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import call_uc04_equipment_access
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state
from hr_onboarding_common.orchestration.uc04_config import (SupabaseUc04ConfigReader, Uc04ConfigUnavailable,
                                                            config_source)
from hr_onboarding_common.orchestration.workers.config import WorkerConfig
from hr_onboarding_common.orchestration.workers.uc_worker_client import UcWorker, WorkerCrashedError

CASE = "d3c1b8e2-6a4f-4f0e-9b7a-2c5d8e1f0a93"


class _NoWorker:
    def get(self, uc_id):
        raise AssertionError("UC04 must not be called on an unusable configuration")


def _state(client_id="CLIENT_DUBAI"):
    state = new_state(case_id=CASE, client_id=client_id, correlation_id="corr-uc04-supabase",
                      actor={"on_behalf_of": "user:hr-admin-01", "roles": ["hr_onboarding_officer"]}, intake_trigger={},
                      uc02_role_context={"role_id": "software_engineer", "department": "IT", "manager_id": "TL-1"})
    state["employee_id"] = "EMP-000123"
    state["joining_date"] = "2026-12-14"
    return state


def _deps(reader):
    return OrchestratorDeps(ctx=None, worker_pool=_NoWorker(), uc03_repo_root=UC_ROOTS["uc03"],
                            uc04_config_source="supabase", uc04_config_reader=reader)


def _blocker(state, code):
    return next(b for b in state["blockers"] if b["code"] == code)


# ----------------------------------------------------------------------------------------- UC04 configuration
def test_an_existing_section_without_an_approved_projection_blocks_and_names_what_is_missing():
    seen = []
    state = call_uc04_equipment_access(_state(), _deps(lambda c: seen.append(c) or {"config_version": 2, "is_active": True}))
    assert seen == ["CLIENT_DUBAI"]  # read for the case's own tenant only
    assert state["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.BLOCKED.value
    blocker = _blocker(state, "UC04_CONFIG_PROJECTION_UNDECIDED")
    assert blocker["required_table"] == "client_service_policies"
    assert "service_key=uc04_equipment_access" in blocker["required_record"] and "client_id=CLIENT_DUBAI" in blocker["required_record"]
    assert "CaseConfig" in blocker["required_configuration"] and "config_version 2" in blocker["reason"]


def test_a_missing_section_is_configuration_missing_with_the_exact_record():
    state = call_uc04_equipment_access(_state("CLIENT-NONE"), _deps(lambda c: None))
    blocker = _blocker(state, "UC04_CONFIGURATION_MISSING")
    assert blocker["required_table"] == "client_service_policies" and "client_id=CLIENT-NONE" in blocker["required_record"]


def test_an_unreadable_supabase_is_unavailable_not_missing():
    def down(client_id):
        raise Uc04ConfigUnavailable("Supabase unreachable")

    state = call_uc04_equipment_access(_state(), _deps(down))
    assert _blocker(state, "UC04_CONFIG_UNAVAILABLE")["owner"] == "ops"
    assert not [b for b in state["blockers"] if b["code"] == "UC04_CONFIGURATION_MISSING"]


def test_the_reader_queries_the_tenants_active_section_and_hides_the_key():
    calls = []

    def handler(request: httpx.Request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json=[{"config_version": 2, "is_active": True}])

    reader = SupabaseUc04ConfigReader(url="https://x.supabase.co", service_role_key="k-secret",
                                      transport=httpx.MockTransport(handler))
    assert reader("CLIENT_DUBAI") == {"config_version": 2, "is_active": True}
    assert calls == [{"select": "config_version,is_active", "client_id": "eq.CLIENT_DUBAI",
                      "service_category": "eq.cfg_section", "service_key": "eq.uc04_equipment_access",
                      "is_active": "is.true", "order": "config_version.desc", "limit": "1"}]
    failing = SupabaseUc04ConfigReader(url="https://x.supabase.co", service_role_key="k-secret",
                                       transport=httpx.MockTransport(lambda r: httpx.Response(503, text="k-secret")))
    with pytest.raises(Uc04ConfigUnavailable) as exc:
        failing("CLIENT_DUBAI")
    assert "k-secret" not in str(exc.value)


def test_the_configuration_source_is_explicit():
    assert config_source({}) == "file"
    assert config_source({"UC04_CONFIG_SOURCE": "supabase"}) == "supabase"
    with pytest.raises(ValueError):
        config_source({"UC04_CONFIG_SOURCE": "somewhere"})


# --------------------------------------------------------------------------------------------- UC04 worker
def _start_uc04(**settings):
    """A real UC04 worker; `settings` reach it through the worker-scoped names (UC04_WORKER_ENV__NAME), exactly as a
    deployment configures it (a scoped value wins over the plain name in the worker's environment)."""
    require_uc("uc04")
    env = {f"UC04_WORKER_ENV__{k}": v for k, v in settings.items()}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        worker = UcWorker(WorkerConfig(uc_id="uc04", python_executable=venv_python(UC_ROOTS["uc04"]),
                                       src_dir=UC_ROOTS["uc04"] / "src"))
        worker.start()
        return worker
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k)
            else:
                os.environ[k] = v


@pytest.mark.parametrize("env", [
    {"UC04_STATE_MODE": "somewhere"},
    {"UC04_STATE_MODE": "supabase", "UC04_IDENTITY_PROFILE": ""},  # LOCAL_SIMULATION cannot key shared rows
    {"UC04_STATE_MODE": "supabase", "UC04_IDENTITY_PROFILE": "PLATFORM", "SUPABASE_URL": "", "SUPABASE_SERVICE_ROLE_KEY": ""},
])
def test_a_misconfigured_uc04_state_mode_stops_the_worker_instead_of_falling_back(env):
    with pytest.raises(WorkerCrashedError):
        worker = _start_uc04(**env)
        worker.call("evaluate_orchestrated", {"input": {}}, actor=Actor(on_behalf_of="user:t", roles=["hr_admin"]),
                    run_id="r", idempotency_key="k")
        worker.stop()


# --------------------------------------------------------------------------------------------------- UC05
def _operation(simulated):
    return uc05.Uc05Operation(key="welcome:C:D", kind="message", status="succeeded", simulated=simulated)


def test_a_simulated_welcome_is_sent_simulated_never_sent():
    assert uc05.delivery_status("delivered", _operation(True)) == "SENT_SIMULATED"
    assert uc05.delivery_status("delivered", _operation(False)) == "SENT"
    assert uc05.delivery_status("delivered", None) == "SENT"
    assert uc05.delivery_status("uncertain", _operation(None)) == "UNCERTAIN"
    assert uc05.delivery_status("confirmed_by_operator", _operation(None)) == "CONFIRMED_BY_OPERATOR"


def test_a_scheduled_session_carries_the_full_schedule_and_invents_nothing():
    start = datetime(2026, 12, 14, 5, 0, tzinfo=timezone.utc)
    live = uc05.Uc05Session(session_id="S-1", session_type="orientation", status="scheduled", scheduled_start=start,
                            scheduled_end=datetime(2026, 12, 14, 6, 0, tzinfo=timezone.utc), attendees=["EMP-1", "TL-1"],
                            calendar_event_id="sim-evt-1",
                            stored_participants=[{"participant_id": "EMP-1", "role": "NEW_HIRE", "rsvp_status": None,
                                                  "responded_at": None}])
    snap = uc05.Uc05Snapshot(found=True, employee_id="EMP-1", client_id="CLIENT_DUBAI", team_lead_id="TL-1")
    state = _state()
    state["orientation_schedule_result"] = {"timezone": "Asia/Dubai"}
    fields = uc05._session_fields(state, snap, live)
    assert {k: fields[k] for k in ("session_id", "session_type", "session_status", "start_utc", "end_utc", "timezone",
                                   "meeting_link", "location")} == {
        "session_id": "S-1", "session_type": "orientation", "session_status": "scheduled", "start_utc": "2026-12-14T05:00:00Z",
        "end_utc": "2026-12-14T06:00:00Z", "timezone": "Asia/Dubai", "meeting_link": None, "location": None}
    assert [p["role"] for p in fields["participants"]] == ["joiner", "team_lead"]
    assert fields["stored_participants"][0]["role"] == "NEW_HIRE"
