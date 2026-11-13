"""The standard orchestrator <-> UC contract (orchestration/agent_contract.py): canonical statuses,
status transitions, error categories, request validation, and the execution ids that travel from a
stage execution onto the worker wire. Unit tests; the last one starts the real UC01 worker to prove
the worker runtime echoes those ids."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hr_onboarding_common.orchestration.agent_contract import (SHARED_CONTEXT_FIELDS, AgentStatus, ContractViolation,
                                                               ErrorCategory, StatusTransitionError, agent_status,
                                                               build_request, check_transition, classify_error,
                                                               completed_stage_regressions, current_execution,
                                                               execution_scope, shared_context, transition_error)
from hr_onboarding_common.orchestration.contracts import Actor
from hr_onboarding_common.orchestration.state import OPTIONAL_STAGES, Stage, StageStatus, new_state
from hr_onboarding_common.orchestration.workers.config import WorkerConfig
from hr_onboarding_common.orchestration.workers.uc_worker_client import UcWorker

CLIENT = "CLIENT-TEST-A"
HR = {"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": CLIENT}


def _state(**over):
    state = new_state(case_id="CASE-CT-1", client_id=CLIENT, correlation_id="corr-ct-1", actor=dict(HR),
                      intake_trigger={"candidate_name": "Alex Morgan", "event_id": "EVT-001"})
    state.update(employee_id="EMP-TEST-001", joining_date="2026-12-01")
    state.update(over)
    return state


def _request(state=None, stage=Stage.EQUIPMENT_ACCESS, **over):
    kwargs = dict(operation="equipment_access_readiness", attempt=1, trace_id="a" * 32, execution_id="EXE-1",
                  input_data={"case_id": "CASE-CT-1"}, dependencies=[])
    kwargs.update(over)
    return build_request(stage, state if state is not None else _state(), **kwargs)


# ----------------------------------------------------------------------------------- statuses


def test_every_stage_status_maps_to_one_canonical_status_and_every_canonical_status_is_used():
    mapped = {agent_status(s.value) for s in StageStatus}
    assert mapped <= set(AgentStatus)
    # The code-aware cases reach the rest: a dependency wait (PENDING) and an HR document review.
    mapped |= {agent_status("blocked", "PREREQUISITE_NOT_MET"), agent_status("awaiting_approval",
                                                                            "DOCUMENT_REVIEW_REQUIRED")}
    assert mapped == set(AgentStatus)


@pytest.mark.parametrize("status, code, expected", [
    ("pending", None, AgentStatus.NOT_STARTED),
    ("in_progress", None, AgentStatus.RUNNING),
    ("complete", None, AgentStatus.COMPLETED),
    ("awaiting_approval", None, AgentStatus.WAITING_FOR_APPROVAL),
    ("awaiting_approval", "DOCUMENT_REVIEW_REQUIRED", AgentStatus.REQUIRES_REVIEW),
    ("waiting", None, AgentStatus.PENDING),
    ("waiting_for_input", "DOCUMENTS_OUTSTANDING", AgentStatus.PENDING),
    ("blocked", "PREREQUISITE_NOT_MET", AgentStatus.PENDING),
    ("blocked", "SCHEDULING_CONFLICT", AgentStatus.BLOCKED),
    ("blocked", "UC05_UNAVAILABLE", AgentStatus.FAILED),  # a technical failure a retry can fix
    ("retry_pending", "UC03_TECHNICAL_ERROR", AgentStatus.FAILED),
    ("uncertain", "UNCERTAIN_OUTCOME", AgentStatus.REQUIRES_REVIEW),
    ("failed", "RETRY_LIMIT_REACHED", AgentStatus.FAILED),
    ("not_requested", None, AgentStatus.SKIPPED),
])
def test_canonical_status_of_each_stage_status(status, code, expected):
    assert agent_status(status, code) == expected


def test_a_completed_stage_is_only_left_through_an_explicit_reopen_or_an_uncertain_relay():
    for after in StageStatus:
        error = transition_error(Stage.EQUIPMENT_ACCESS, "complete", after.value)
        assert (error is None) == (after.value in ("complete", "in_progress", "uncertain")), after
    with pytest.raises(StatusTransitionError):
        check_transition(Stage.EQUIPMENT_ACCESS, "complete", "blocked")


def test_a_stage_that_has_run_never_becomes_not_started_again():
    assert transition_error(Stage.INTAKE, "blocked", "pending")
    assert transition_error(Stage.PLAN_TASKS, "in_progress", "not_requested")
    assert transition_error(Stage.INTAKE, "pending", "complete") is None
    assert transition_error(Stage.VALIDATE_DOCUMENTS, "retry_pending", "failed") is None
    assert transition_error(Stage.VALIDATE_DOCUMENTS, "failed", "in_progress") is None  # retry_stage re-opens


def test_the_optional_buddy_introduction_follows_uc05_alone():
    (optional,) = OPTIONAL_STAGES
    assert transition_error(optional, "complete", "blocked") is None
    assert transition_error(optional, "awaiting_approval", "not_requested") is None


def test_completed_stage_regressions_names_only_regressed_required_stages():
    stored = {"intake": "complete", "plan_tasks": "complete", "buddy_introduction": "complete",
              "equipment_access": "blocked", "orientation_scheduling": "complete"}
    new = {"intake": "in_progress", "plan_tasks": "blocked", "buddy_introduction": "blocked",
           "equipment_access": "complete", "orientation_scheduling": "uncertain"}
    assert completed_stage_regressions(stored, new) == ["plan_tasks: complete -> blocked"]


# ------------------------------------------------------------------------------------- errors


@pytest.mark.parametrize("code, category", [
    ("POLICY_BREACH", ErrorCategory.AUTHORIZATION), ("TENANT_CONTEXT_MISMATCH", ErrorCategory.AUTHORIZATION),
    ("PREREQUISITE_NOT_MET", ErrorCategory.DEPENDENCY),
    ("MISSING_DATA", ErrorCategory.VALIDATION), ("EMPLOYEE_ID_CONFLICT", ErrorCategory.VALIDATION),
    ("DOCUMENTS_NEED_CORRECTION", ErrorCategory.VALIDATION), ("INVALID_INPUT", ErrorCategory.VALIDATION),
    ("UC03_TECHNICAL_ERROR", ErrorCategory.INFRASTRUCTURE), ("UC02_PACKAGE_UNIMPORTABLE", ErrorCategory.INFRASTRUCTURE),
    ("RETRY_LIMIT_REACHED", ErrorCategory.INFRASTRUCTURE), ("UC05_UNAVAILABLE", ErrorCategory.INFRASTRUCTURE),
    ("UNCERTAIN_OUTCOME", ErrorCategory.UNCERTAIN), ("WELCOME_DELIVERY_UNCERTAIN", ErrorCategory.UNCERTAIN),
    ("DOCUMENT_REQUEST_DELIVERY_UNCERTAIN", ErrorCategory.UNCERTAIN),
    ("UC05_CONTRACT_VIOLATION", ErrorCategory.CONTRACT), ("STATE_OWNERSHIP_VIOLATION", ErrorCategory.CONTRACT),
    ("IDENTITY_CONFLICT", ErrorCategory.CONTRACT), ("INVALID_STATUS_TRANSITION", ErrorCategory.CONTRACT),
    ("SCHEDULING_CONFLICT", ErrorCategory.BUSINESS), ("DAY_ONE_SIGNOFF_REJECTED", ErrorCategory.BUSINESS),
    ("UC05_CASE_INACTIVE", ErrorCategory.BUSINESS), ("INTERNAL_ERROR", ErrorCategory.INTERNAL),
])
def test_error_codes_are_classified(code, category):
    assert classify_error(code) == category


# --------------------------------------------------------------------------- request envelope


def test_a_valid_request_carries_case_tenant_employee_correlation_and_trace():
    state = _state(employee_ref={"client_id": CLIENT, "employee_id": "EMP-TEST-001"})
    request = _request(state)
    assert (request.case_id, request.tenant_id, request.employee_id, request.canonical_employee_id) == \
        ("CASE-CT-1", CLIENT, "EMP-TEST-001", "EMP-TEST-001")
    assert (request.use_case_id, request.stage, request.correlation_id, request.trace_id, request.execution_id) == \
        ("UC04", "equipment_access", "corr-ct-1", "a" * 32, "EXE-1")
    assert request.actor == HR


@pytest.mark.parametrize("over", [{"correlation_id": ""}, {"case_id": ""}, {"client_id": ""}, {"actor": {}}])
def test_a_request_missing_a_required_field_is_refused(over):
    with pytest.raises(ContractViolation) as exc:
        _request(_state(**over))
    assert exc.value.code == "MISSING_REQUIRED_FIELD"


def test_an_actor_or_a_canonical_employee_of_another_tenant_is_refused():
    with pytest.raises(ContractViolation) as exc:
        _request(_state(actor={**HR, "client_id": "CLIENT-TEST-B"}))
    assert exc.value.code == "TENANT_CONTEXT_MISMATCH"
    with pytest.raises(ContractViolation) as exc:
        _request(_state(employee_ref={"client_id": "CLIENT-TEST-B", "employee_id": "EMP-TEST-001"}))
    assert exc.value.code == "TENANT_CONTEXT_MISMATCH"


def test_shared_context_is_the_cross_uc_allowlist_and_carries_no_personal_detail():
    state = _state(verified_context={"documents_verified": True, "verified_document_types": ["passport"]})
    context = shared_context(state)
    assert set(context) == set(SHARED_CONTEXT_FIELDS) | {
        "canonical_employee_id", "plan_id", "plan_version", "documents_verified", "verified_document_types",
        "task_plan_available", "equipment_access_ready", "day_one_signed_off"}
    assert "Alex Morgan" not in str(context) and context["documents_verified"] is True


# ---------------------------------------------------------------------------- wire propagation


def _stub_worker(captured: list) -> UcWorker:
    worker = UcWorker(WorkerConfig(uc_id="uc01", python_executable=Path(sys.executable), src_dir=Path(".")))
    worker._proc = SimpleNamespace(poll=lambda: None)  # "alive"; no process is started
    worker._raw_call = lambda wire, timeout_s=None: captured.append(wire) or {
        "contract_version": "1.0", "run_id": wire["run_id"], "ok": True, "result": {}}
    return worker


def test_the_execution_ids_travel_with_every_worker_request_made_inside_a_stage_execution():
    captured: list[dict] = []
    worker = _stub_worker(captured)
    request = _request()
    with execution_scope(request) as scope:
        assert current_execution.get() is scope
        worker.call("evaluate_orchestrated", {}, actor=Actor(on_behalf_of="user:hr-admin-01"), run_id="corr-ct-1")
    worker.call("evaluate_orchestrated", {}, actor=Actor(on_behalf_of="user:hr-admin-01"), run_id="corr-ct-1")
    assert current_execution.get() is None
    inside, outside = captured
    assert (inside["run_id"], inside["trace_id"], inside["execution_id"]) == ("corr-ct-1", "a" * 32, "EXE-1")
    assert outside["trace_id"] is None and outside["execution_id"] is None


def test_the_real_worker_runtime_echoes_the_execution_ids(uc01_worker):
    request = _request(stage=Stage.INTAKE, operation="create_employee_record")
    with execution_scope(request):
        response = uc01_worker.call("__identity_check__", {}, actor=Actor(on_behalf_of="system"),
                                    run_id=request.correlation_id)
    assert response.ok and response.run_id == "corr-ct-1"
    assert (response.trace_id, response.execution_id) == (request.trace_id, request.execution_id)
