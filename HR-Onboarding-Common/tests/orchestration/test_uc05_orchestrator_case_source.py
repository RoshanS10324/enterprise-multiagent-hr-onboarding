"""UC05_OFFER_EVENT_SOURCE=orchestrator_case, through the real UC05 worker.

UC05's mailbox is simulated (only its synthetic test messages), so a real offer reference is never found there. In
this mode UC05 reads the joiner from the orchestrator's case instead -- the HR-approved form -- and the case id is the
offer-acceptance event, as in UC05's own Supabase mode. Pinned here:
  * UC05's own intake creates the case and schedules the induction from the form's facts (name, email, department as
    team, manager as team lead, joining date); the reference typed on the form is not used;
  * nothing is filled in: a form without a manager is UC05's INCOMPLETE_EMPLOYEE_DATA, not a guessed team lead;
  * UC05 keeps one record per (client, employee id), so a second case under the same id is refused -- by the
    orchestrator when it knows the other case, by the UC05 worker otherwise -- never merged;
  * the candidate's email is kept on the case only in this mode, and never shown by the API;
  * a tenant with employee identity scheme `none` must give the HRMS employee number at start.
"""

from __future__ import annotations

import json

import pytest
from _lifecycle import BUDDY_CANDIDATE, CLIENT, CLIENT_CONFIG, World, code, start_uc05_worker, status

from hr_onboarding_common.orchestration.api import case_view
from hr_onboarding_common.orchestration.orchestrator_agent import WorkflowRequestError
from hr_onboarding_common.orchestration.state import Stage, StageStatus
from hr_onboarding_common.orchestration.tenant_config import TenantConfig
from hr_onboarding_common.orchestration.workers.uc_worker_client import WorkerCrashedError

ORIENTATION = Stage.ORIENTATION_SCHEDULING


@pytest.fixture(scope="module")
def uc05_case_source():
    worker = start_uc05_worker(UC05_OFFER_EVENT_SOURCE="orchestrator_case")
    yield worker
    worker.stop()


def _world(worker, **kw) -> World:
    w = World(worker, **kw)
    w.deps.uc05_offer_event_source = "orchestrator_case"
    return w


@pytest.fixture
def world(uc05_case_source):
    w = _world(uc05_case_source)
    w.hook("reset")  # a fresh UC05 (and a fresh case source: nothing registered)
    w.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    w.hook("add_client_config", config=CLIENT_CONFIG)
    return w


def _blocker(state, code_):
    (b,) = [b for b in state["blockers"] if b["code"] == code_]
    return b


def test_uc05_schedules_the_induction_for_the_joiner_the_approved_form_describes(world):
    state = world.start(offer_acceptance_event_id="Off-Act-006")  # no mailbox holds it; not used in this mode
    assert status(state, ORIENTATION) == StageStatus.AWAITING_APPROVAL.value, state["blockers"]
    result = state["orientation_schedule_result"]
    assert result["offer_event"]["event_id"] == state["case_id"]  # the case is the event
    assert result["session_status"] == "scheduled" and result["uc05_case"] is True
    assert not [b for b in state["blockers"] if b["stage"] == ORIENTATION.value]


def test_the_email_is_kept_for_uc05_only_and_never_shown_by_the_api(world, uc05_case_source):
    state = world.start()
    assert state["joiner_contact"] == {"email": "alex.morgan@example.com"}
    assert "alex.morgan@example.com" not in json.dumps(case_view(state).model_dump(mode="json"))
    mailbox_world = World(uc05_case_source)  # the default source: the email stays intake-only, as before
    assert mailbox_world.agent.initial_state(mailbox_world.case_input())["joiner_contact"] is None


def test_a_form_without_a_manager_is_incomplete_never_a_guessed_team_lead(world):
    state = world.start(manager_id=None)
    assert code(state, ORIENTATION) == "INCOMPLETE_EMPLOYEE_DATA"
    assert "team_lead_id" in _blocker(state, "INCOMPLETE_EMPLOYEE_DATA")["reason"]
    assert not (state.get("orientation_schedule_result") or {}).get("uc05_case")  # UC05 created nothing


def test_a_second_case_under_the_same_employee_id_is_refused_never_merged(world, uc05_case_source):
    first = world.start()
    assert status(first, ORIENTATION) == StageStatus.AWAITING_APPROVAL.value

    # The same orchestrator: it knows the first case already took this employee id into UC05.
    second = world.start(offer_reference="OFR-T-002", candidate_name="Sam Lee", candidate_email="sam.lee@example.com")
    assert code(second, ORIENTATION) == "EMPLOYEE_ID_NOT_UNIQUE"
    assert first["case_id"] in _blocker(second, "EMPLOYEE_ID_NOT_UNIQUE")["reason"]

    # Another orchestrator process (its own case store): UC05's worker itself refuses the second registration.
    elsewhere = _world(uc05_case_source)
    third = elsewhere.start(offer_reference="OFR-T-003", candidate_name="Kim Ray", candidate_email="kim.ray@example.com")
    assert code(third, ORIENTATION) == "EMPLOYEE_ID_NOT_UNIQUE"
    assert "registered in UC05" in _blocker(third, "EMPLOYEE_ID_NOT_UNIQUE")["reason"]
    assert not (third.get("orientation_schedule_result") or {}).get("uc05_case")


def test_a_tenant_identified_by_hrms_numbers_needs_the_number_at_start(uc05_case_source):
    tenants = TenantConfig.model_validate({"tenants": {CLIENT: {"employee_identity": {"scheme": "none"}}}})
    w = _world(uc05_case_source, tenants=tenants)
    with pytest.raises(WorkflowRequestError, match="HRMS employee number"):
        w.agent.advance_case(w.case_input(), case_id=None, actor={"on_behalf_of": "user:hr-admin-01",
                                                                  "roles": ["hr_admin"], "client_id": CLIENT})
    w.hook("reset")
    w.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    w.hook("add_client_config", config=CLIENT_CONFIG)
    state = w.start(employee_id="HRMS-104233")  # the HRMS number HR entered; UC01's own number is not used
    assert state["employee_ref"]["employee_id"] == "HRMS-104233" and state["employee_ref"]["source"] == "explicit"
    assert status(state, ORIENTATION) == StageStatus.AWAITING_APPROVAL.value, state["blockers"]


def test_the_case_source_cannot_be_combined_with_uc05_supabase_mode():
    with pytest.raises(WorkerCrashedError):  # the worker refuses to start: one source of truth
        worker = start_uc05_worker(UC05_OFFER_EVENT_SOURCE="orchestrator_case", UC05_DATABASE_MODE="supabase")
        worker.stop()
