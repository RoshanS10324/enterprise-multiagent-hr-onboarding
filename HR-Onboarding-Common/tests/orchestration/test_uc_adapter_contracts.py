"""Contract tests: every UC adapter through the standard UC contract (orchestration/execution.py ->
agent_contract.UCAgentRequest / UCAgentResponse), the same seam the orchestrator's stage wrapper uses.

Per UC: valid input, missing required fields, an invalid tenant/case context, output normalization,
error propagation, canonical status mapping, and correlation/trace propagation to the UC's transport.

What is real in each test (the adapter is always the real one):
  UC01  FakeUc01Worker (UC01's OpenCaseResult shape) behind the real worker-call path of the adapter.
  UC02  operation A: the real orchestration adapter, UC03's real checklist, the mock notification outbox.
        operation B: the REAL UC02 package -- which fails to import (advisor.py:1 SyntaxError, owned by
        UC02's developer); output normalization of a successful plan uses PlanDouble, labelled as such.
  UC03  the REAL UC03 graph in-process. The timeout case replaces UC03's run with a slow coroutine.
  UC04  FakeUc04Worker / a scripted worker (UC04's OrchestratorResponse shape).
  UC05  FakeUc05Worker (replies in the real worker's shape, _uc05_fakes.py).
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from _helpers import UC_ROOTS, require_uc
from _lifecycle import FakeUc01Worker, FakeUc04Worker
from _uc05_fakes import FakeUc05Worker, Pool, approval, event_run, failure, preflight, session, snapshot
from _workflow import CANDIDATE, CLIENT, HR, OTHER_TENANT_HR, Flow, synthetic_document

from hr_onboarding_common.orchestration import execution
from hr_onboarding_common.orchestration.adapters import uc03_documents
from hr_onboarding_common.orchestration.adapters.uc01_intake import call_uc01_intake
from hr_onboarding_common.orchestration.adapters.uc03_document_collection import call_uc03_document_collection
from hr_onboarding_common.orchestration.adapters.uc02_tasks import call_uc02_plan_tasks
from hr_onboarding_common.orchestration.adapters.uc03_documents import applicable_documents, \
    call_uc03_validate_documents
from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import call_uc04_equipment_access
from hr_onboarding_common.orchestration.adapters.uc05_orientation import call_uc05_orientation
from hr_onboarding_common.orchestration.agent_contract import AgentStatus, ErrorCategory, current_execution
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state
from hr_onboarding_common.orchestration.workers.uc_worker_client import WorkerCrashedError
from hr_onboarding_common.orchestration.workflow_models import SubmittedDocument

ROLE = {"role_id": "software_engineer", "department": "Engineering", "manager_id": "TL-TEST-001",
        "location": "Dubai", "project": "platform"}
UC04_CONFIG = str(UC_ROOTS["uc04"] / "data" / "client_alpha_config.json")
UP_TO_VALIDATION = (Stage.INTAKE, Stage.DOCUMENT_COLLECTION, Stage.DOCUMENT_SUBMISSION, Stage.VALIDATE_DOCUMENTS)
VERIFIED = {"documents_verified": True, "verified_document_types": ["passport"], "verified_at": "2026-11-01T00:00:00Z",
            "case_id": "CASE-CT-1", "employee_id": "EMP-TEST-001", "joining_date": "2026-12-01"}


class Recording:
    """Wraps a worker double; records each call's correlation id and the stage execution in scope."""

    is_alive = True

    def __init__(self, inner):
        self.inner, self.seen = inner, []

    def call(self, op, payload, *, actor, run_id=None, idempotency_key=None, timeout_s=None):
        self.seen.append({"op": op, "run_id": run_id, "idempotency_key": idempotency_key,
                          "execution": current_execution.get(), "actor": actor})
        return self.inner.call(op, payload, actor=actor, run_id=run_id, idempotency_key=idempotency_key)


def _case(*complete: Stage, actor=HR, **fields):
    state = new_state(case_id="CASE-CT-1", client_id=CLIENT, correlation_id="corr-ct-1", actor=dict(actor),
                      intake_trigger={"offer_accepted": True, "offer_reference": "OFR-CT-1", "candidate_name": CANDIDATE,
                                      "event_id": "EVT-001"},
                      uc02_role_context=dict(ROLE), uc04_client_config_ref=UC04_CONFIG)
    for stage in complete:
        state["stage_statuses"][stage.value] = StageStatus.COMPLETE.value
    state.update(fields)
    return state


def _verified(**fields):
    base = {"employee_id": "EMP-TEST-001", "joining_date": "2026-12-01", "verified_context": dict(VERIFIED)}
    return _case(*UP_TO_VALIDATION, **{**base, **fields})


def _planned(**fields):
    """`_verified()` plus plan_tasks COMPLETE: the baseline for UC04/UC05 adapter tests, now that
    plan_tasks is a required prerequisite of both (dependencies.py's `_TASK_PLAN`)."""
    base = {"task_plan": {"onboarding_id": "ONB-CT-1"}}
    state = _verified(**{**base, **fields})
    state["stage_statuses"][Stage.PLAN_TASKS.value] = StageStatus.COMPLETE.value
    return state


def _deps(flow: Flow, **workers):
    """The flow's wired dependencies (policy included), with these worker doubles and no write-ahead."""
    replace = {"persist_progress": None}
    if workers:
        replace["worker_pool"] = Pool(**workers)
    return dataclasses.replace(flow.deps, **replace)


def _envelope(response, *, uc: str, stage: Stage, status: AgentStatus, case_id: str = "CASE-CT-1"):
    assert (response.use_case_id, response.stage, response.tenant_id, response.case_id) == \
        (uc, stage.value, CLIENT, case_id)
    assert response.correlation_id == "corr-ct-1"
    assert len(response.trace_id) == 32 and response.execution_id.startswith("EXE-")
    assert response.status == status, (response.status, response.errors)


def _transported(recording: Recording, response):
    """Every UC call of this execution carried the case's correlation id and this execution's ids."""
    assert recording.seen
    for call in recording.seen:
        assert call["run_id"] == "corr-ct-1"
        assert (call["execution"].trace_id, call["execution"].execution_id) == \
            (response.trace_id, response.execution_id)


def _refused_before_the_uc(response, state, recording, *, code, status, category):
    assert response.errors[0].code == code and response.errors[0].category == category
    assert response.status == status and recording.seen == []
    assert response.output_data == {} and response.updated_fields == []


# ======================================================================================= UC01


class TestUC01EmployeeRecord:
    def run(self, worker, state=None):
        flow = Flow()
        recording = Recording(worker)
        deps = _deps(flow, uc01=recording)
        response, result = execution.execute(Stage.INTAKE, state or _case(), lambda s: call_uc01_intake(s, deps))
        return response, result, recording

    def test_a_created_record_is_normalized_with_the_shared_identity_it_produces(self):
        response, result, recording = self.run(FakeUc01Worker())
        # UC01 issues its own case id; the response is about that case.
        assert result["case_id"].startswith("UC01-REQ-")
        _envelope(response, uc="UC01", stage=Stage.INTAKE, status=AgentStatus.COMPLETED, case_id=result["case_id"])
        assert response.output_data == {"employee_id": "EMP-TEST-001"}
        assert response.updated_context["employee_id"] == "EMP-TEST-001"
        assert response.updated_context["joining_date"] == "2026-12-01"
        assert response.updated_context["case_id"] == result["case_id"]
        assert result["field_provenance"]["employee_id"]["use_case_id"] == "UC01"
        assert result["field_provenance"]["employee_id"]["execution_id"] == response.execution_id
        _transported(recording, response)
        assert recording.seen[0]["idempotency_key"] == "CASE-CT-1:intake:v1"

    def test_a_request_without_a_correlation_id_never_reaches_uc01(self):
        response, result, recording = self.run(FakeUc01Worker(), _case(correlation_id=""))
        _refused_before_the_uc(response, result, recording, code="MISSING_REQUIRED_FIELD", status=AgentStatus.FAILED,
                               category=ErrorCategory.VALIDATION)

    def test_an_actor_of_another_tenant_never_reaches_uc01(self):
        response, result, recording = self.run(FakeUc01Worker(), _case(actor=OTHER_TENANT_HR))
        _refused_before_the_uc(response, result, recording, code="TENANT_CONTEXT_MISMATCH",
                               status=AgentStatus.BLOCKED, category=ErrorCategory.AUTHORIZATION)
        assert result["stage_statuses"]["intake"] == StageStatus.BLOCKED.value

    def test_a_held_record_propagates_as_blocked_with_its_reasons(self):
        response, result, _ = self.run(FakeUc01Worker(), _case(intake_trigger={"offer_accepted": False}))
        _envelope(response, uc="UC01", stage=Stage.INTAKE, status=AgentStatus.BLOCKED)
        assert [a["code"] for a in response.pending_actions if a["kind"] == "blocker"] == ["HELD"]
        assert result["employee_id"] is None

    def test_a_uc01_failure_and_a_lost_worker_propagate_with_their_categories(self):
        rejected = FakeUc05Worker(open_onboarding_case=failure("INVALID_INPUT", "form fields missing"))
        response, _, _ = self.run(rejected)
        assert response.status == AgentStatus.FAILED
        assert (response.errors[0].code, response.errors[0].category) == ("INVALID_INPUT", ErrorCategory.VALIDATION)

        crashed = FakeUc05Worker(open_onboarding_case=WorkerCrashedError("pipe closed"))
        response, result, _ = self.run(crashed)
        assert response.status == AgentStatus.REQUIRES_REVIEW  # the write may or may not have happened
        assert (response.errors[0].code, response.errors[0].category) == ("UNCERTAIN_OUTCOME", ErrorCategory.UNCERTAIN)
        assert result["stage_statuses"]["intake"] == StageStatus.UNCERTAIN.value

    def test_a_record_of_another_tenant_is_never_taken_over(self):
        foreign = FakeUc05Worker(open_onboarding_case={"outcome": "CASE_OPENED", "approvals_created": [], "reasons": [],
                                                       "case": {"case_id": "UC01-X", "employee_no": "EMP-OTHER",
                                                                "joining_date": "2026-12-01",
                                                                "client_id": "CLIENT-TEST-B"}})
        response, result, _ = self.run(foreign)
        assert response.status == AgentStatus.FAILED
        assert (response.errors[0].code, response.errors[0].category) == \
            ("TENANT_CONTEXT_CONFLICT", ErrorCategory.AUTHORIZATION)
        assert result["employee_id"] is None and result["case_id"] == "CASE-CT-1"


# ============================================================================ UC02 operation A


class TestUC03DocumentCollection:
    """UC03 owns document collection: it opens the collection for the current UC02 plan version."""

    def run(self, state, flow=None):
        flow = flow or Flow()
        deps = _deps(flow)
        return execution.execute(Stage.DOCUMENT_COLLECTION, state,
                                 lambda s: call_uc03_document_collection(s, deps)) + (flow,)

    @staticmethod
    def planned(flow, **fields):
        """intake and UC02's plan_tasks COMPLETE, with the plan the plan double produces."""
        state = _case(Stage.INTAKE, **{"employee_id": "EMP-TEST-001", "joining_date": "2026-12-01", **fields})
        return flow.plan(state, flow.deps)

    def test_the_request_is_normalized_with_the_requirements_of_the_plan_version(self):
        flow = Flow()
        response, result, flow = self.run(self.planned(flow), flow)
        _envelope(response, uc="UC03", stage=Stage.DOCUMENT_COLLECTION, status=AgentStatus.COMPLETED)
        collection = response.output_data["document_collection"]
        assert collection["source"] == "uc02_plan" and collection["notification"]["status"] == "sent"
        assert (collection["plan_id"], collection["plan_version"]) == ("ONB-SYN-001", 1)
        request = collection["request"]  # the UC02 -> UC03 DocumentCollectionRequest
        assert request["required_documents"] == [d["document_type"] for d in result["onboarding_plan"]["required_documents"]]
        assert request["validation_rules"]["passport"]["check_expiry"] is True  # UC03's own rules
        assert response.updated_fields == ["document_collection"] and flow.notifier.sent_count() == 1
        assert flow.deps._uc03_collection_service.collection("CASE-CT-1").plan_version == 1  # opened in UC03

    def test_no_request_without_uc02s_plan_or_uc01s_employee_number(self):
        response, _, flow = self.run(_case(Stage.INTAKE, employee_id="EMP-TEST-001"))
        assert response.status == AgentStatus.PENDING  # waits for UC02's plan
        assert response.errors[0].code == "PREREQUISITE_NOT_MET"
        assert response.errors[0].category == ErrorCategory.DEPENDENCY
        response, _, flow = self.run(self.planned(flow, employee_id=None), flow)
        assert response.status == AgentStatus.BLOCKED and response.errors[0].code == "MISSING_DATA"
        assert flow.notifier.sent_count() == 0

    def test_an_actor_of_another_tenant_sends_nothing(self):
        flow = Flow()
        response, _, flow = self.run(self.planned(flow, actor=OTHER_TENANT_HR), flow)
        assert response.errors[0].code == "TENANT_CONTEXT_MISMATCH" and flow.notifier.sent_count() == 0

    def test_an_unknown_delivery_outcome_propagates_as_retryable_and_uncertain(self):
        flow = Flow()
        flow.notifier.raise_next = 1
        response, result, _ = self.run(self.planned(flow), flow)
        assert response.status == AgentStatus.FAILED
        error = response.errors[0]
        assert (error.code, error.category, error.retryable) ==             ("DOCUMENT_REQUEST_DELIVERY_UNCERTAIN", ErrorCategory.UNCERTAIN, True)
        assert result["document_collection"]["notification"]["status"] == "uncertain"


# ============================================================================ UC02 planning


class TestUC02TaskPlanning:
    def test_the_real_uc02_refuses_to_plan_without_uc01s_offer_and_form_facts(self):
        """The real UC02 package (the advisor import defect is fixed): this intake carries no offer-acceptance
        or form-approval timestamps, so UC02's own start condition refuses -- never a fabricated plan."""
        flow = Flow()
        deps = _deps(flow)
        state = _case(Stage.INTAKE, employee_id="EMP-TEST-001", joining_date="2026-12-01")
        response, result = execution.execute(Stage.PLAN_TASKS, state, lambda s: call_uc02_plan_tasks(s, deps))
        _envelope(response, uc="UC02", stage=Stage.PLAN_TASKS, status=AgentStatus.BLOCKED)
        assert (response.errors[0].code, response.errors[0].category) == ("MISSING_DATA", ErrorCategory.VALIDATION)
        assert result["task_plan"] is None and result["onboarding_plan"] is None

    def test_planning_waits_for_uc01_only_and_needs_the_role_context(self):
        flow = Flow()
        deps = _deps(flow)
        response, _ = execution.execute(Stage.PLAN_TASKS, _case(employee_id="EMP-TEST-001", joining_date="2026-12-01"),
                                        lambda s: call_uc02_plan_tasks(s, deps))
        assert response.status == AgentStatus.PENDING and response.errors[0].code == "PREREQUISITE_NOT_MET"
        assert {c.upstream_stage for c in response.metadata.get("dependencies", [])} <= {"intake"}
        # No verified documents are needed: UC02 plans before UC03 collects.
        response, _ = execution.execute(Stage.PLAN_TASKS, _case(Stage.INTAKE, employee_id="EMP-TEST-001",
                                                                joining_date="2026-12-01",
                                                                uc02_role_context={"role_id": "x"}),
                                        lambda s: call_uc02_plan_tasks(s, deps))
        assert response.status == AgentStatus.BLOCKED and response.errors[0].code == "MISSING_DATA"

    def test_a_plan_is_normalized_with_its_output__contract_double(self):
        """PlanDouble returns the real adapter's output shape -- NOT a UC02 result (UC02 cannot import)."""
        flow = Flow()
        state = _case(Stage.INTAKE, employee_id="EMP-TEST-001", joining_date="2026-12-01")
        response, result = execution.execute(Stage.PLAN_TASKS, state, lambda s: flow.plan(s, flow.deps))
        _envelope(response, uc="UC02", stage=Stage.PLAN_TASKS, status=AgentStatus.COMPLETED)
        assert response.output_data["task_plan"]["onboarding_id"] == "ONB-SYN-001"
        assert {"task_plan", "onboarding_plan", "plan_history"} <= set(response.updated_fields)
        assert result["field_provenance"]["onboarding_plan"]["use_case_id"] == "UC02"
        assert result["field_provenance"]["task_plan"]["use_case_id"] == "UC02"


# ======================================================================================= UC03


def _with_documents(flow, state, **meta):
    required = [d for d in applicable_documents(state, flow.deps) if d.required]
    state["document_collection"] = {"required_documents": [{"document_type": d.id, "label": d.label, "required": True}
                                                           for d in required],
                                    "source": "uc03_checklist", "outstanding": []}
    state["submitted_documents"] = {
        d.id: SubmittedDocument(revision=1, submitted_at="2026-11-01T00:00:00Z", submitted_by=HR["on_behalf_of"],
                                **synthetic_document(d.id, **meta)).model_dump(mode="json") for d in required}
    return state


class TestUC03DocumentValidation:
    def setup_method(self):
        require_uc("uc03")

    def test_valid_documents_are_normalized_as_awaiting_hr_review(self):
        flow = Flow()
        deps = _deps(flow)
        state = _with_documents(flow, _case(Stage.INTAKE, Stage.DOCUMENT_COLLECTION, Stage.DOCUMENT_SUBMISSION,
                                            employee_id="EMP-TEST-001", joining_date="2026-12-01"))
        response, result = execution.execute(Stage.VALIDATE_DOCUMENTS, state,
                                             lambda s: call_uc03_validate_documents(s, deps))
        _envelope(response, uc="UC03", stage=Stage.VALIDATE_DOCUMENTS, status=AgentStatus.REQUIRES_REVIEW)
        assert response.errors[0].code == "DOCUMENT_REVIEW_REQUIRED"
        validation = response.output_data["document_validation_result"]
        assert set(validation) == set(state["submitted_documents"])
        assert {r["classification"] for r in validation.values()} == {"hr_review_required"}
        assert "document_validation_result" in response.updated_fields
        assert {a["kind"] for a in response.pending_actions} == {"blocker"}
        # The case's correlation id reached UC03 itself: UC03 records it on each of its own audit events.
        events = asyncio.run(deps._uc03_audit_store.get_events("CASE-CT-1"))
        assert events and {e.correlation_id for e in events} == {"corr-ct-1"}

    def test_validation_waits_until_every_required_document_is_supplied(self):
        flow = Flow()
        deps = _deps(flow)
        state = _with_documents(flow, _case(Stage.INTAKE, Stage.DOCUMENT_COLLECTION, employee_id="EMP-TEST-001"))
        response, _ = execution.execute(Stage.VALIDATE_DOCUMENTS, state, lambda s: call_uc03_validate_documents(s, deps))
        assert response.status == AgentStatus.PENDING and response.errors[0].code == "PREREQUISITE_NOT_MET"
        assert "document_submission" in response.errors[0].detail
        # UC02 operation A completed without a requirement list: the requirements are missing data.
        state = _case(Stage.INTAKE, Stage.DOCUMENT_COLLECTION, Stage.DOCUMENT_SUBMISSION, employee_id="EMP-TEST-001")
        response, _ = execution.execute(Stage.VALIDATE_DOCUMENTS, state, lambda s: call_uc03_validate_documents(s, deps))
        assert response.status == AgentStatus.BLOCKED and response.errors[0].code == "MISSING_DATA"
        assert deps._uc03_graph is None  # the adapter never ran, so UC03's graph was never even built on deps

    def test_a_timed_out_validation_is_a_retryable_technical_failure_not_a_document_verdict(self, monkeypatch):
        """UC03's run is replaced by a slow coroutine to time out deterministically."""
        async def slow(state, deps):
            await asyncio.sleep(5)

        monkeypatch.setattr(uc03_documents, "_validate_all", slow)
        flow = Flow()
        deps = dataclasses.replace(_deps(flow), uc03_timeout_s=0.05)
        state = _with_documents(flow, _case(Stage.INTAKE, Stage.DOCUMENT_COLLECTION, Stage.DOCUMENT_SUBMISSION,
                                            employee_id="EMP-TEST-001"))
        response, result = execution.execute(Stage.VALIDATE_DOCUMENTS, state,
                                             lambda s: call_uc03_validate_documents(s, deps))
        assert response.status == AgentStatus.FAILED
        error = response.errors[0]
        assert (error.code, error.category, error.retryable) == ("UC03_TECHNICAL_ERROR", ErrorCategory.INFRASTRUCTURE,
                                                                 True)
        assert "TimeoutError" in error.detail and result["correction_requirements"] == []


# ======================================================================================= UC04


class TestUC04EquipmentAccess:
    def run(self, worker, state=None):
        recording = Recording(worker)
        deps = _deps(Flow(), uc04=recording)
        response, result = execution.execute(Stage.EQUIPMENT_ACCESS, state or _planned(),
                                             lambda s: call_uc04_equipment_access(s, deps))
        return response, result, recording

    def test_readiness_is_normalized_and_the_call_carries_the_execution(self):
        response, _, recording = self.run(FakeUc04Worker("READY"))
        _envelope(response, uc="UC04", stage=Stage.EQUIPMENT_ACCESS, status=AgentStatus.COMPLETED)
        assert response.output_data["equipment_access_result"]["overall_status"] == "READY"
        _transported(recording, response)
        assert recording.seen[0]["idempotency_key"] == "CASE-CT-1.equipment_access.v1"  # UC04's key pattern
        assert recording.seen[0]["actor"].on_behalf_of == HR["on_behalf_of"]  # the real actor, never a synthetic one

    def test_a_pending_approval_is_surfaced_for_uc04s_own_approver(self):
        response, _, _ = self.run(FakeUc04Worker("AWAITING_APPROVAL"))
        assert response.status == AgentStatus.WAITING_FOR_APPROVAL
        (approval_action,) = [a for a in response.pending_actions if a["kind"] == "approval"]
        assert approval_action["agent"] == "dact-uc04-equipment-access" and approval_action["role"] == "hiring_manager"

    def test_uc04_failures_propagate_with_their_categories(self):
        refused = FakeUc05Worker(evaluate_orchestrated=failure("INVALID_INPUT", "Uc04Input: case_id pattern"))
        response, _, _ = self.run(refused)
        assert (response.status, response.errors[0].code, response.errors[0].category) == \
            (AgentStatus.FAILED, "INVALID_INPUT", ErrorCategory.VALIDATION)
        crashed = FakeUc05Worker(evaluate_orchestrated=WorkerCrashedError("pipe closed"))
        response, _, _ = self.run(crashed)
        assert (response.status, response.errors[0].category) == (AgentStatus.REQUIRES_REVIEW, ErrorCategory.UNCERTAIN)

    def test_an_answer_for_another_case_is_an_identity_conflict(self):
        other = FakeUc05Worker(evaluate_orchestrated={"result": {"case_id": "CASE-OTHER", "overall_status": "READY",
                                                                 "items": [], "blockers": []}, "proposals": []})
        response, result, _ = self.run(other)
        assert (response.status, response.errors[0].code, response.errors[0].category) == \
            (AgentStatus.FAILED, "IDENTITY_CONFLICT", ErrorCategory.CONTRACT)
        assert result["equipment_access_result"] is None

    def test_no_uc04_call_before_document_clearance_or_for_another_tenant(self):
        response, result, recording = self.run(FakeUc04Worker(), _case(Stage.INTAKE, employee_id="EMP-TEST-001",
                                                                       joining_date="2026-12-01"))
        _refused_before_the_uc(response, result, recording, code="PREREQUISITE_NOT_MET", status=AgentStatus.PENDING,
                               category=ErrorCategory.DEPENDENCY)
        response, result, recording = self.run(FakeUc04Worker(), _verified(actor=OTHER_TENANT_HR))
        _refused_before_the_uc(response, result, recording, code="TENANT_CONTEXT_MISMATCH",
                               status=AgentStatus.BLOCKED, category=ErrorCategory.AUTHORIZATION)


# ======================================================================================= UC05


INDUCTION_PENDING = snapshot(stage="induction_awaiting_approval", sessions=[session("induction", "scheduled")],
                             approvals=[approval("apr-ind", "session_booking", record_id="session-1",
                                                 session_type="induction")])


class TestUC05Orientation:
    def run(self, worker, state=None):
        recording = Recording(worker)
        deps = _deps(Flow(), uc05=recording)
        response, result = execution.execute(Stage.ORIENTATION_SCHEDULING, state or _planned(),
                                             lambda s: call_uc05_orientation(s, deps))
        return response, result, recording

    def test_a_drafted_induction_is_normalized_with_the_canonical_employee_identity(self):
        worker = FakeUc05Worker(preflight_offer_event=preflight(), run_event=event_run("induction_awaiting_approval"),
                                case_snapshot=INDUCTION_PENDING)
        response, result, recording = self.run(worker)
        _envelope(response, uc="UC05", stage=Stage.ORIENTATION_SCHEDULING, status=AgentStatus.WAITING_FOR_APPROVAL)
        assert response.output_data["orientation_schedule_result"]["approval_id"] == "apr-ind"
        ref = response.updated_context["employee_ref"]
        assert (ref["client_id"], ref["employee_id"], ref["employee_no"]) == (CLIENT, "EMP-TEST-001", "EMP-TEST-001")
        assert result["field_provenance"]["employee_ref"]["use_case_id"] == "UC05"
        _transported(recording, response)
        assert [c["op"] for c in recording.seen] == ["preflight_offer_event", "run_event", "case_snapshot"]
        assert {c["idempotency_key"] for c in recording.seen} == {
            f"CASE-CT-1:orientation_scheduling:v1:{op}" for op in ("preflight_offer_event", "run_event", "case_snapshot")}

    def test_a_reply_outside_uc05s_documented_contract_fails_the_stage(self):
        worker = FakeUc05Worker(preflight_offer_event=preflight(), run_event=event_run("induction_awaiting_approval"),
                                case_snapshot=snapshot(sessions=[{**session("induction", "scheduled"),
                                                                  "status": "teleported"}]))
        response, _, _ = self.run(worker)
        assert (response.status, response.errors[0].code, response.errors[0].category) == \
            (AgentStatus.FAILED, "UC05_CONTRACT_VIOLATION", ErrorCategory.CONTRACT)

    def test_a_provider_outage_is_a_retryable_infrastructure_failure(self):
        worker = FakeUc05Worker(preflight_offer_event=preflight(), run_event=failure("RETRYABLE_PROVIDER_ERROR"))
        response, result, _ = self.run(worker)
        error = response.errors[0]
        assert response.status == AgentStatus.FAILED and result["stage_statuses"]["orientation_scheduling"] == "blocked"
        assert (error.code, error.category, error.retryable) == \
            ("UC05_PROVIDER_UNAVAILABLE", ErrorCategory.INFRASTRUCTURE, True)

    def test_no_uc05_call_before_verification_without_a_joining_date_or_for_another_tenant(self):
        worker = FakeUc05Worker(preflight_offer_event=preflight())
        response, result, recording = self.run(worker, _case(Stage.INTAKE, employee_id="EMP-TEST-001",
                                                             joining_date="2026-12-01"))
        _refused_before_the_uc(response, result, recording, code="PREREQUISITE_NOT_MET", status=AgentStatus.PENDING,
                               category=ErrorCategory.DEPENDENCY)
        response, result, recording = self.run(worker, _verified(joining_date=None))
        _refused_before_the_uc(response, result, recording, code="MISSING_DATA", status=AgentStatus.BLOCKED,
                               category=ErrorCategory.VALIDATION)
        response, result, recording = self.run(worker, _verified(
            employee_ref={"client_id": "CLIENT-TEST-B", "employee_id": "EMP-TEST-001"}))
        _refused_before_the_uc(response, result, recording, code="TENANT_CONTEXT_MISMATCH",
                               status=AgentStatus.BLOCKED, category=ErrorCategory.AUTHORIZATION)


@pytest.mark.parametrize("stage", [s for s in Stage if s != Stage.COMPLETE])
def test_every_stage_belongs_to_exactly_one_use_case(stage):
    from hr_onboarding_common.orchestration.agent_contract import USE_CASE_OF
    assert USE_CASE_OF[stage] in ("UC01", "UC02", "UC03", "UC04", "UC05", "orchestrator")
