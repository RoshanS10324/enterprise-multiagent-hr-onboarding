"""The end-to-end integration scenarios of the UC01-UC05 integration, through the REAL orchestrator
graph, routing, standard UC contract, dependency registry, state synchronization, case store and HITL
inbox. Each test's docstring says which agents are real and which are contract-compatible doubles.

  Flow   (_workflow.py)   UC01/UC04 fakes behind the real adapters; the real UC03 document-collection
                          adapter (UC03's real collection service) and the mock notification outbox; UC02
                          planning/replanning, UC03 validation and UC05 doubles -- unless real_uc02 /
                          real_uc03 is set.
  World  (_lifecycle.py)  the REAL UC05 worker (UC05's own code, its simulated connectors), UC01/UC04
                          fakes and UC02/UC03 doubles.
  orchestrator fixture    the REAL UC01, UC04 and UC05 workers, the REAL UC03 in-process and the REAL
                          UC02 package (conftest.py).

All data is synthetic; nothing contacts a real service. A double's result is never presented as the
UC's own.
"""

from __future__ import annotations

from datetime import date

import pytest
from _helpers import UC_ROOTS
from _lifecycle import BUDDY_CANDIDATE, CLIENT_CONFIG, TEAM_LEAD, FakeUc01Worker, World, start_uc05_worker
from _workflow import (CLIENT, HR, HR_2, HR_OPS, JOINING, OTHER_TENANT_HR, SCHEDULER, Flow, code, status,
                       synthetic_document)
from fastapi.testclient import TestClient

from hr_onboarding_common.orchestration.adapters.uc05_orientation import PrerequisiteNotMetError
from hr_onboarding_common.orchestration.agent_contract import AgentStatus, ErrorCategory, classify_error
from hr_onboarding_common.orchestration.api import create_orchestrator_app
from hr_onboarding_common.orchestration.case_store import SqliteCaseStateStore
from hr_onboarding_common.orchestration.contracts import OnboardingCaseInput, WorkerResponse
from hr_onboarding_common.orchestration.orchestrator_agent import PrematureLifecycleEventError
from hr_onboarding_common.orchestration.state import CONDITIONAL_STAGES, REQUIRED_STAGES, Stage, StageStatus
from hr_onboarding_common.orchestration.tenant_config import TenantConfig
from hr_onboarding_common.platform.hitl import Decision, HITLError
from hr_onboarding_common.platform.identity import InsecureHeaderIdentityProvider

S, ST = Stage, StageStatus


def canonical(state, stage: Stage) -> str:
    return state["stage_outcomes"][stage.value]["canonical_status"]


def error_category(state, stage: Stage) -> ErrorCategory:
    return classify_error(code(state, stage))


@pytest.fixture(scope="module")
def uc05():
    worker = start_uc05_worker()
    yield worker
    worker.stop()


@pytest.fixture
def world(uc05):
    w = World(uc05)
    w.hook("reset")
    w.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    w.hook("add_client_config", config=CLIENT_CONFIG)
    return w


# ===================================================================== 1. successful onboarding


def test_01_complete_successful_onboarding():
    """Flow: real graph and contract; UC01/UC04 fakes via the real adapters; UC02 planning, UC03 and
    UC05 doubles. Every required stage completes once, in order, through the standard contract."""
    flow = Flow()
    state = flow.start()
    assert state["workflow_status"] == "waiting_for_input" and canonical(state, S.DOCUMENT_SUBMISSION) == "PENDING"
    state = flow.supply(state)
    assert all(status(state, s) == ST.COMPLETE.value for s in (S.PLAN_TASKS, S.EQUIPMENT_ACCESS,
                                                                S.ORIENTATION_SCHEDULING, S.BUDDY_ASSIGNMENT,
                                                                S.TEAM_INTRODUCTION))
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value  # never auto-completed
    state = flow.approve_signoff(state)
    assert canonical(state, S.JOINING_DAY_WELCOME) == "PENDING"  # waiting for the joining day, not an error
    state = flow.agent.trigger_joining_day(state["case_id"], as_of=JOINING, actor=SCHEDULER)

    assert status(state, S.COMPLETE) == ST.COMPLETE.value and state["workflow_status"] == "completed"
    for stage in REQUIRED_STAGES:
        if stage in CONDITIONAL_STAGES:  # replanning was never needed: it never ran
            assert status(state, stage) == ST.NOT_REQUESTED.value
            continue
        assert canonical(state, stage) == AgentStatus.COMPLETED.value, stage
    assert flow.uc01.calls == 1 and flow.notifier.sent_count() == 1 and flow.uc04.calls == 1
    assert flow.plan.calls == 1 and dict(flow.uc05.calls) == {"orientation": 1, "buddy": 1, "team_intro": 1,
                                                                "welcome": 2}  # 2: waiting, then the event
    assert state["pending_actions"] == [] and state["blockers"] == []


def test_01_real_agents_run_through_the_contract_as_far_as_each_can_today(orchestrator):
    """The REAL UC01/UC04/UC05 workers, the REAL UC02 package and the REAL UC03, in the new order:
    UC01 -> UC02 plans -> UC03 collects and validates (HR reviews) -> UC04 || UC05. Shows where each real
    agent stops today and that the contract normalizes it: the real UC04 refuses UC01's non-SYN case id
    (GR-13) and the real UC05 does not complete for this case -- both are attempted exactly once, only
    after verification."""
    inp = OnboardingCaseInput(
        client_id="CLIENT-ALPHA", hr_officer_id="user:hr-admin-01", offer_accepted=True, offer_reference="OFR-REAL-1",
        candidate_name="Arun Kumar", candidate_email="arun.kumar@example.com", designation="Backend Engineer",
        joining_date="2026-11-16", work_location_type="OFFICE", id_number="ID-901", role_id="backend_engineer",
        department="Technology", manager_id="MGR-001", location="Dubai", project="apollo",
        employee_id="HRMS-000901",  # CLIENT-ALPHA: HRMS numbers (config/tenants.yaml, scheme none)
        offer_accepted_at="2026-10-20T10:00:00Z", form_approved_at="2026-10-21T10:00:00Z",
        form_approved_by_role="hr_ops",
        uc04_client_config_ref=str(UC_ROOTS["uc04"] / "data" / "client_alpha_config.json"))
    actor = {"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": "CLIENT-ALPHA"}
    state = orchestrator.advance_case(inp, case_id=None, actor=actor)
    assert canonical(state, S.INTAKE) == "COMPLETED" and state["field_provenance"]["employee_id"]["use_case_id"] == "UC01"
    assert canonical(state, S.PLAN_TASKS) == "COMPLETED"  # the real UC02, before any document
    assert state["field_provenance"]["onboarding_plan"]["use_case_id"] == "UC02"
    assert S.EQUIPMENT_ACCESS.value not in state["stage_outcomes"]
    documents = [{"document_type": o["document_type"], "document_ref": f"SYN-DOC-{o['document_type']}",
                  "metadata": {"readable": True, "required_fields_present": True, "expiry_date": "2031-01-01",
                               "evidence_stored": True, "extracted_name": "Arun Kumar", "attestation_confirmed": True}}
                 for o in state["document_collection"]["outstanding"]]
    state = orchestrator.submit_documents(state["case_id"], documents=documents, actor=actor)
    assert canonical(state, S.VALIDATE_DOCUMENTS) == "REQUIRES_REVIEW"  # real UC03: HR reviews valid documents
    assert S.EQUIPMENT_ACCESS.value not in state["stage_outcomes"]  # no branch before verification
    state = orchestrator.review_documents(state["case_id"], reviews=[
        {"document_type": d["document_type"], "decision": "approved"} for d in documents], actor=actor)
    assert canonical(state, S.VALIDATE_DOCUMENTS) == "COMPLETED"

    assert state["stage_outcomes"][S.PLAN_TASKS.value]["attempt"] == 1  # never re-planned
    for stage in (S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING):
        assert state["stage_outcomes"][stage.value]["attempt"] == 1
        assert state["stage_outcomes"][stage.value]["started_at"] >= \
            state["stage_outcomes"][S.VALIDATE_DOCUMENTS.value]["finished_at"]
    # UC04's SYN-id contract gap, reported as a named, resumable dependency (UC04_IDENTITY_PROFILE=PLATFORM unset)
    assert code(state, S.EQUIPMENT_ACCESS) == "UC04_IDENTITY_PROFILE_NOT_ENABLED"
    assert status(state, S.ORIENTATION_SCHEDULING) != ST.COMPLETE.value
    assert len({o["correlation_id"] for o in state["stage_outcomes"].values()}) == 1


def test_01_real_uc01_uc03_and_uc05_complete_an_onboarding_end_to_end(uc05, uc01_worker):
    """The REAL UC01 worker, the REAL UC03 in-process and the REAL UC05 worker carry one case from offer
    acceptance to a delivered welcome. Doubles only where the real agent cannot run today: UC02 task
    planning (World's plan double -- UC02's package does not import) and UC04 (FakeUc04Worker -- the real
    UC04 refuses non-SYN identifiers, GR-13). UC01's employee number reaches UC05 through the tenant's
    `mapped` identity scheme, configured once UC01 issued it -- the documented way HR links an upstream
    number to the HRMS id UC05's directory uses."""
    world = World(uc05, uc01=uc01_worker)
    world.hook("reset")
    world.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    world.hook("add_client_config", config=CLIENT_CONFIG)
    del world.deps.stage_adapters[S.VALIDATE_DOCUMENTS.value]  # the real UC03, not World's document double
    world.agent.graph = world.agent.build_graph()

    state = world.agent.advance_case(world.case_input(offer_reference="OFR-REAL-E2E"), case_id=None, actor=HR)
    assert canonical(state, S.INTAKE) == "COMPLETED" and state["employee_id"]  # issued by the real UC01
    world.deps.tenants = TenantConfig.model_validate({"tenants": {CLIENT: {"employee_identity": {
        "scheme": "mapped", "employee_no_map": [{"employee_no": state["employee_id"], "employee_id": "EMP-TEST-001"}]}}}})

    documents = [synthetic_document(o["document_type"]) for o in state["document_collection"]["outstanding"]]
    state = world.agent.submit_documents(state["case_id"], documents=documents, actor=HR)
    assert canonical(state, S.VALIDATE_DOCUMENTS) == "REQUIRES_REVIEW"  # the real UC03
    state = world.agent.review_documents(state["case_id"], reviews=[
        {"document_type": d["document_type"], "decision": "approved"} for d in documents], actor=HR)
    assert canonical(state, S.VALIDATE_DOCUMENTS) == "COMPLETED"
    assert canonical(state, S.ORIENTATION_SCHEDULING) == "WAITING_FOR_APPROVAL"  # the real UC05 drafted it
    assert state["employee_ref"] == {"client_id": CLIENT, "employee_id": "EMP-TEST-001",
                                     "employee_no": state["employee_id"], "source": "mapped", "scheme": "mapped"}

    state = world.decide(state, S.ORIENTATION_SCHEDULING)
    state = world.decide(state, S.BUDDY_ASSIGNMENT)
    state = world.decide(state, S.TEAM_INTRODUCTION, actor=TEAM_LEAD)
    assert canonical(state, S.DAY_ONE_READINESS) == "WAITING_FOR_APPROVAL"
    state = world.agent.decide_approval(state["case_id"], world.gate_item(state["case_id"], "day_one_signoff"),
                                        decision="approved", actor=HR_2)
    state = world.agent.trigger_joining_day(state["case_id"], as_of=JOINING, actor=SCHEDULER)
    state = world.decide(state, S.JOINING_DAY_WELCOME)

    assert status(state, S.COMPLETE) == ST.COMPLETE.value and state["workflow_status"] == "completed"
    assert state["welcome_message_result"]["delivery"] == "delivered"
    assert world.hook("counts")["messages_by_kind"]["welcome"] == 1
    provenance = state["field_provenance"]
    assert (provenance["employee_id"]["use_case_id"], provenance["document_validation_result"]["use_case_id"],
            provenance["welcome_message_result"]["use_case_id"]) == ("UC01", "UC03", "UC05")
    assert {o["correlation_id"] for o in state["stage_outcomes"].values()} == {state["correlation_id"]}


# ===================================================================== 2. invalid employee record


def test_02_invalid_employee_record_holds_the_case_at_intake():
    """Flow: UC01 fake via the real adapter. An unverified acceptance is HELD by UC01: nothing downstream
    runs, the orchestrator's own intake review gate is raised. A record without an employee number
    blocks UC02's planning (and so every document request) with MISSING_DATA instead of guessing one."""
    flow = Flow()
    state = flow.start(offer_accepted=False)
    assert status(state, S.INTAKE) == ST.BLOCKED.value and canonical(state, S.INTAKE) == "BLOCKED"
    assert [b["code"] for b in state["blockers"] if b["stage"] == "intake"] == ["HELD"]
    assert flow.ctx.hitl.list(status=Decision.PENDING, gate="intake_mismatch_review", client_id=CLIENT)
    assert flow.notifier.sent_count() == 0 and status(state, S.DOCUMENT_COLLECTION) == ST.PENDING.value

    flow = Flow()
    flow.uc01.employee_id = None
    state = flow.start(offer_reference="OFR-NO-EMPNO")
    assert code(state, S.PLAN_TASKS) == "MISSING_DATA" and flow.notifier.sent_count() == 0
    assert error_category(state, S.PLAN_TASKS) == ErrorCategory.VALIDATION and flow.plan.calls == 0
    assert status(state, S.DOCUMENT_COLLECTION) == ST.PENDING.value


# ===================================================================== 3. duplicate employee record


class _LinkingUc01(FakeUc01Worker):
    """UC01 recognizing an existing record for this offer: LINKED_EXISTING carries only the existing case id."""

    def call(self, op, payload, **kwargs):
        self.calls += 1
        return WorkerResponse(run_id="r", ok=True, result={"outcome": "LINKED_EXISTING", "case": None,
                                                           "existing_case_id": "UC01-EXISTING-7",
                                                           "approvals_created": [], "reasons": []})


def test_03_duplicate_employee_record_reuses_the_existing_record_and_never_creates_a_second():
    """Flow. A repeated offer acceptance resumes the same case (UC01 is not called again); a duplicate
    UC01 recognizes is linked to its existing record, and since UC01's link carries no employee number,
    UC02's planning (and so the document request) waits for HR (MISSING_DATA) rather than guessing one."""
    flow = Flow()
    first = flow.start()
    second = flow.start()
    assert first["case_id"] == second["case_id"] and flow.uc01.calls == 1 and flow.notifier.sent_count() == 1

    flow = Flow()
    flow.deps.worker_pool.workers["uc01"] = linking = _LinkingUc01()
    state = flow.start()
    assert state["case_id"] == "UC01-EXISTING-7" and status(state, S.INTAKE) == ST.COMPLETE.value
    assert code(state, S.PLAN_TASKS) == "MISSING_DATA" and linking.calls == 1 and flow.notifier.sent_count() == 0
    # The original id still reaches the linked case.
    assert flow.agent.get_case(flow.agent.case_id_for(flow.case_input()), client_id=CLIENT)["case_id"] == \
        "UC01-EXISTING-7"


# ===================================================================== 4. task creation failure


def test_04_task_creation_failure_with_the_real_uc02_blocks_everything_after_it():
    """Flow with the REAL UC02 package (real_uc02=True): this intake carries no offer/form approval facts,
    so UC02 refuses to plan (BLOCKED, MISSING_DATA). UC02 is first after intake: UC03 collects nothing and
    UC04/UC05 are never called. A resume asks UC02 again (a block, not a terminal failure) and changes
    nothing downstream; a retry_stage is refused -- it is not a technical failure."""
    flow = Flow(real_uc02=True)
    state = flow.start()
    assert (status(state, S.PLAN_TASKS), code(state, S.PLAN_TASKS)) == (ST.BLOCKED.value, "MISSING_DATA")
    assert status(state, S.DOCUMENT_COLLECTION) == ST.PENDING.value and flow.notifier.sent_count() == 0
    assert flow.uc04.calls == 0 and flow.uc05.calls["orientation"] == 0
    assert state["workflow_status"] == "blocked" and state["stage_attempts"]["plan_tasks"] == 1
    state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert state["stage_attempts"]["plan_tasks"] == 2 and flow.notifier.sent_count() == 0 and flow.uc04.calls == 0
    with pytest.raises(PrerequisiteNotMetError):
        flow.agent.retry_stage(state["case_id"], "plan_tasks", actor=HR)


def test_04_task_creation_recovers_through_retry__contract_double():
    """Flow with PlanDouble (UC02 planning's output shape -- NOT a UC02 result): a terminal planning failure
    holds the case before any document is requested; after the cause is fixed, retry_stage completes
    planning, UC03 collects, and once the documents are verified the branches run; nothing re-runs."""
    flow = Flow()
    flow.plan.status = ST.FAILED
    state = flow.start()
    assert status(state, S.PLAN_TASKS) == ST.FAILED.value and flow.plan.calls == 1 and flow.notifier.sent_count() == 0
    flow.plan.status = ST.COMPLETE
    state = flow.agent.retry_stage(state["case_id"], "plan_tasks", actor=HR)
    assert status(state, S.PLAN_TASKS) == ST.COMPLETE.value and flow.plan.calls == 2
    assert status(state, S.DOCUMENT_SUBMISSION) == ST.WAITING_FOR_INPUT.value and flow.notifier.sent_count() == 1
    state = flow.supply(state)
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value
    assert flow.uc04.calls == 1 and flow.uc05.calls["orientation"] == 1 and flow.uc01.calls == 1
    assert flow.plan.calls == 2


# ===================================================================== 5. document validation failure


def test_05_document_validation_failure_and_correction_with_the_real_uc03():
    """Flow with the REAL UC03 in-process. An expired passport waits for a correction; UC03 validates the
    corrected revision again (revision-aware, no longer a duplicate), and -- valid -- routes it to the HR
    review UC03 requires; after the review the documents are verified and the branches start. UC01, UC02's
    plan and the request are never repeated, and no replanning was needed for a plain resubmission."""
    flow = Flow(real_uc03=True)
    state = flow.start()
    documents = [synthetic_document(t) if t != "passport" else synthetic_document(t, expiry_date="2020-01-01")
                 for t in flow.outstanding(state)]
    state = flow.agent.submit_documents(state["case_id"], documents=documents, actor=HR)
    assert canonical(state, S.VALIDATE_DOCUMENTS) == "PENDING"  # waiting for corrected input
    assert any(r["document_type"] == "passport" and r["action"] == "resubmit" for r in state["correction_requirements"])
    assert all(status(state, s) == ST.PENDING.value for s in (S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING))

    state = flow.supply(state, ["passport"], tag="2")
    assert canonical(state, S.VALIDATE_DOCUMENTS) == "REQUIRES_REVIEW"
    assert "DUPLICATE_REQUEST" not in state["document_validation_result"]["passport"]["findings"]
    state = flow.agent.review_documents(state["case_id"], reviews=[
        {"document_type": t, "decision": "approved"} for t in state["submitted_documents"]], actor=HR)
    assert canonical(state, S.VALIDATE_DOCUMENTS) == "COMPLETED"
    assert status(state, S.EQUIPMENT_ACCESS) == ST.COMPLETE.value
    assert flow.uc01.calls == 1 and flow.notifier.sent_count() == 1 and flow.plan.calls == 1
    assert status(state, S.REPLAN_TASKS) == ST.NOT_REQUESTED.value and flow.replan.calls == 0


# ===================================================================== 6-7. equipment / access


def test_06_equipment_access_approval_pending_waits_for_uc04s_own_approver():
    """Flow: UC04 fake via the real adapter. UC04's approval is surfaced, never decided by the
    orchestrator; day 1 waits for it; once UC04 applied it, a resume completes the stage."""
    flow = Flow(uc04_status="AWAITING_APPROVAL")
    state = flow.to_branches()
    assert canonical(state, S.EQUIPMENT_ACCESS) == "WAITING_FOR_APPROVAL"
    (pending,) = [p for p in state["pending_approvals"] if p["stage"] == "equipment_access"]
    assert pending["agent"] == "dact-uc04-equipment-access"
    with pytest.raises(PrerequisiteNotMetError):  # the orchestrator never decides a UC04 approval
        flow.agent.decide_approval(state["case_id"], pending["reference"], decision="approved", actor=HR)
    assert status(state, S.DAY_ONE_READINESS) == ST.PENDING.value and status(state, S.ORIENTATION_SCHEDULING) == \
        ST.COMPLETE.value
    flow.uc04.overall_status = "READY"
    state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert canonical(state, S.EQUIPMENT_ACCESS) == "COMPLETED" and flow.uc04.calls == 2
    assert flow.uc05.calls["orientation"] == 1 and status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value


class _RejectingUc04:
    is_alive = True

    def __init__(self):
        self.calls, self.rejected = 0, True

    def call(self, op, payload, **kwargs):
        self.calls += 1
        result = {"case_id": payload["input"]["case_id"], "items": [],
                  "overall_status": "BLOCKED" if self.rejected else "READY",
                  "blockers": [{"code": "REQUEST_REJECTED", "reason": "the laptop request was rejected",
                                "owner": "hiring_manager", "next_action": "Raise a corrected request"}]
                  if self.rejected else []}
        return WorkerResponse(run_id="r", ok=True, result={"result": result, "proposals": []})


def test_07_equipment_access_rejection_blocks_day_one_until_corrected():
    """Flow with a UC04 stand-in that rejects the request (UC04's OrchestratorResponse shape)."""
    flow = Flow()
    flow.deps.worker_pool.workers["uc04"] = uc04 = _RejectingUc04()
    state = flow.to_branches()
    assert canonical(state, S.EQUIPMENT_ACCESS) == "BLOCKED"
    assert [b["code"] for b in state["blockers"] if b["stage"] == "equipment_access"] == ["REQUEST_REJECTED"]
    assert state["workflow_status"] == "blocked" and status(state, S.DAY_ONE_READINESS) == ST.PENDING.value
    uc04.rejected = False
    state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert canonical(state, S.EQUIPMENT_ACCESS) == "COMPLETED" and uc04.calls == 2
    assert [b for b in state["blockers"] if b["stage"] == "equipment_access"] == []


# ===================================================================== 8. scheduling conflict


def test_08_orientation_scheduling_conflict_with_the_real_uc05(world):
    """World: the REAL UC05 worker. A busy calendar blocks the induction (a business outcome, retryable)
    with nothing booked; once the calendar is free a resume drafts exactly one induction."""
    world.hook("calendar_busy", attendee="TL-TEST-001", start="2026-12-01T00:00:00+04:00",
               end="2026-12-31T00:00:00+04:00")
    state = world.start()
    assert (canonical(state, S.ORIENTATION_SCHEDULING), code(state, S.ORIENTATION_SCHEDULING)) == \
        ("BLOCKED", "NO_AVAILABLE_SLOT")
    assert error_category(state, S.ORIENTATION_SCHEDULING) == ErrorCategory.BUSINESS
    assert world.hook("counts")["calendar_live"] == 0
    world.hook("calendar_clear_busy", attendee="TL-TEST-001")
    state = world.agent.resume_case(state["case_id"], actor=HR)
    assert canonical(state, S.ORIENTATION_SCHEDULING) == "WAITING_FOR_APPROVAL"
    assert len(world.uc05_snapshot()["sessions"]) == 1


# ===================================================================== 9. unauthorized approval


def test_09_unauthorized_approval_attempts_are_refused_and_consume_nothing():
    """Flow. Day-1 signoff needs hr_admin: an hr_ops user and another tenant's admin are refused, the
    approval stays pending, and an authorized HR admin then signs off. Over HTTP: 403 and 404."""
    flow = Flow()
    state = flow.to_branches()
    (item,) = flow.ctx.hitl.list(status=Decision.PENDING, gate="day_one_signoff", client_id=CLIENT)
    with pytest.raises(HITLError):
        flow.agent.decide_approval(state["case_id"], item.id, decision="approved", actor=HR_OPS)
    with pytest.raises(KeyError):
        flow.agent.decide_approval(state["case_id"], item.id, decision="approved", actor=OTHER_TENANT_HR)
    client = TestClient(create_orchestrator_app(agent=flow.agent,
                                                identity_provider=InsecureHeaderIdentityProvider(enabled=True)))
    url = f"/v1/orchestration/cases/{state['case_id']}/approvals/{item.id}/decision"
    assert client.post(url, json={"decision": "approved"}, headers=_h(HR_OPS)).status_code == 403
    assert client.post(url, json={"decision": "approved"}, headers=_h(OTHER_TENANT_HR)).status_code == 404
    assert flow.ctx.hitl.get(item.id, client_id=CLIENT).status == Decision.PENDING
    state = flow.approve_signoff(state)
    assert canonical(state, S.DAY_ONE_READINESS) == "COMPLETED"


# ===================================================================== 10. restart while paused


def test_10_process_restart_during_a_paused_workflow_resumes_from_durable_state(tmp_path):
    """Flow with the SQLite case store and HITL inbox (persist_dir). The case pauses at HR's day-1 signoff;
    a NEW orchestrator instance on the same stores still sees the pending approval, applies it, and
    continues -- no UC is called again for a completed stage."""
    first = Flow(case_states=SqliteCaseStateStore(tmp_path / "case_states.db"), persist_dir=tmp_path)
    state = first.to_branches()
    case_id = state["case_id"]
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value

    restarted = Flow(case_states=SqliteCaseStateStore(tmp_path / "case_states.db"), persist_dir=tmp_path)
    paused = restarted.agent.get_case(case_id, client_id=CLIENT)
    assert [p["gate"] for p in paused["pending_approvals"] if p["agent"] == "onboarding-orchestrator"] == \
        ["day_one_signoff"]
    state = restarted.approve_signoff(paused)
    assert canonical(state, S.DAY_ONE_READINESS) == "COMPLETED"
    assert restarted.uc01.calls == restarted.uc04.calls == restarted.notifier.sent_count() == 0
    assert restarted.uc05.calls["orientation"] == 0 and restarted.plan.calls == 0


# ===================================================================== 11. retry after a failure


def test_11_retry_after_a_recoverable_failure_never_duplicates_a_side_effect():
    """Flow. A UC03 technical failure and a refused (known undelivered) document request are both retried
    by a plain resume; the record is created once and the request delivered once."""
    flow = Flow()
    flow.notifier.fail_next = 1
    state = flow.start()
    assert (canonical(state, S.DOCUMENT_COLLECTION), code(state, S.DOCUMENT_COLLECTION)) == \
        ("FAILED", "DOCUMENT_REQUEST_DELIVERY_FAILED")
    state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.DOCUMENT_COLLECTION) == ST.COMPLETE.value and flow.notifier.sent_count() == 1

    flow.uc03.fail_next = 1
    state = flow.supply(state)
    assert (canonical(state, S.VALIDATE_DOCUMENTS), code(state, S.VALIDATE_DOCUMENTS)) == \
        ("FAILED", "UC03_TECHNICAL_ERROR")
    assert error_category(state, S.VALIDATE_DOCUMENTS) == ErrorCategory.INFRASTRUCTURE
    state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert canonical(state, S.VALIDATE_DOCUMENTS) == "COMPLETED"
    assert state["retry_metadata"]["validate_documents"]["failures"] == 1
    assert flow.uc01.calls == 1 and flow.notifier.sent_count() == 1


# ===================================================================== 12. duplicate trigger / event


def test_12_duplicate_triggers_and_events_are_applied_once():
    """Flow. A repeated start resumes the same case; a redelivered document event (same event id) adds no
    second revision and no second UC03 call; a repeated joining-day event is recorded once; an event id
    reused for another event type is refused."""
    flow = Flow()
    state = flow.start()
    assert flow.start()["case_id"] == state["case_id"] and flow.uc01.calls == 1
    documents = [synthetic_document(t) for t in flow.outstanding(state)]
    state, first = flow.agent.handle_event(state["case_id"], event_id="WEBHOOK-DOCS-1", event_type="documents_submitted",
                                           payload={"documents": documents}, actor=HR)
    state, again = flow.agent.handle_event(state["case_id"], event_id="WEBHOOK-DOCS-1", event_type="documents_submitted",
                                           payload={"documents": documents}, actor=HR)
    assert first["duplicate"] is False and again["duplicate"] is True
    assert {d["revision"] for d in state["submitted_documents"].values()} == {1} and flow.uc03.calls == 1
    with pytest.raises(PrerequisiteNotMetError):
        flow.agent.handle_event(state["case_id"], event_id="WEBHOOK-DOCS-1", event_type="resume", payload={}, actor=HR)

    state = flow.approve_signoff(state)
    for event_id in ("SCHED-JD-1", "SCHED-JD-2"):  # the scheduler fires twice
        state, _ = flow.agent.handle_event(state["case_id"], event_id=event_id, event_type="joining_day",
                                           payload={"as_of": JOINING.isoformat()}, actor=SCHEDULER)
    assert status(state, S.COMPLETE) == ST.COMPLETE.value
    assert len(flow.ctx.audit.find("orchestrator.joining_day_recorded")) == 1


# ===================================================================== 13-14. joining day (real UC05)


def test_13_joining_day_welcome_with_the_real_uc05(world):
    """World: the REAL UC05 worker. The welcome is drafted only on the joining day after HR's signoff,
    approved by HR, and delivered once; an early event is refused."""
    state = world.to_welcome_waiting()
    assert canonical(state, S.JOINING_DAY_WELCOME) == "PENDING"
    with pytest.raises(PrematureLifecycleEventError):
        world.agent.trigger_joining_day(state["case_id"], as_of=date(2026, 11, 30), actor=SCHEDULER)
    state = world.agent.trigger_joining_day(state["case_id"], as_of=JOINING, actor=SCHEDULER)
    assert canonical(state, S.JOINING_DAY_WELCOME) == "WAITING_FOR_APPROVAL"
    state = world.decide(state, S.JOINING_DAY_WELCOME)
    assert canonical(state, S.JOINING_DAY_WELCOME) == "COMPLETED" and state["workflow_status"] == "completed"
    assert state["welcome_message_result"]["delivery"] == "delivered"
    assert world.hook("counts")["messages_by_kind"]["welcome"] == 1


def test_14_uncertain_welcome_delivery_is_recovered_by_an_operator_never_by_a_blind_resend(world):
    """World: the REAL UC05 worker, its simulated notifier losing the provider's answer. The stage needs a
    review (not a retry); retry-apply does not resend; the operator's resolution completes the case."""
    state = world.to_welcome_pending()
    world.hook("set", target="notifier", attrs={"idempotent_delivery": False, "lose_next_responses": 1})
    state = world.decide(state, S.JOINING_DAY_WELCOME)
    assert status(state, S.JOINING_DAY_WELCOME) == ST.UNCERTAIN.value
    assert error_category(state, S.JOINING_DAY_WELCOME) == ErrorCategory.UNCERTAIN
    state = world.agent.retry_approval(state["case_id"], state["welcome_message_result"]["approval_id"], actor=HR)
    assert world.hook("counts")["messages_by_kind"]["welcome"] == 1  # never resent blindly
    with pytest.raises(PrerequisiteNotMetError):  # a technical retry is refused for an unknown outcome
        world.agent.retry_stage(state["case_id"], "joining_day_welcome", actor=HR)
    state, _ = world.agent.request_uc05_operation(state["case_id"], operation="resolve_welcome_delivery",
                                                  params={"resolution": "delivered",
                                                          "reason": "employee confirmed receipt"}, actor=HR)
    assert status(state, S.JOINING_DAY_WELCOME) == ST.COMPLETE.value
    assert state["welcome_message_result"]["delivery"] == "confirmed_by_operator"
    assert world.hook("counts")["messages_by_kind"]["welcome"] == 1


# ===================================================================== 15. cross-tenant access


def test_15_cross_tenant_access_is_indistinguishable_from_a_missing_case_and_changes_nothing():
    """Flow. Another tenant's HR admin can neither read, change, decide, resume nor send events to a
    case -- each answered like a missing case -- and the case is unchanged."""
    flow = Flow()
    state = flow.to_branches()
    case_id, version = state["case_id"], flow.stored(state["case_id"])["state_version"]
    (item,) = flow.ctx.hitl.list(status=Decision.PENDING, gate="day_one_signoff", client_id=CLIENT)
    attempts = [
        lambda: flow.agent.get_case(case_id, client_id="CLIENT-TEST-B"),
        lambda: flow.agent.stage_executions(case_id, client_id="CLIENT-TEST-B"),
        lambda: flow.agent.resume_case(case_id, actor=OTHER_TENANT_HR),
        lambda: flow.agent.decide_approval(case_id, item.id, decision="approved", actor=OTHER_TENANT_HR),
        lambda: flow.agent.submit_documents(case_id, documents=[synthetic_document("passport")], actor=OTHER_TENANT_HR),
        lambda: flow.agent.handle_event(case_id, event_id="X-1", event_type="resume", payload={}, actor=OTHER_TENANT_HR),
        lambda: flow.agent.retry_stage(case_id, "plan_tasks", actor=OTHER_TENANT_HR),
    ]
    for attempt in attempts:
        with pytest.raises(KeyError):
            attempt()
    assert flow.stored(case_id)["state_version"] == version
    assert flow.ctx.hitl.get(item.id, client_id=CLIENT).status == Decision.PENDING


# ===================================================================== the integrated API surface


def _h(actor) -> dict:
    return {"X-User-Id": actor["on_behalf_of"], "X-Client-Id": actor["client_id"],
            "X-User-Roles": ",".join(actor["roles"])}


def test_the_api_exposes_stage_executions_errors_approvals_events_and_dependencies():
    """Flow behind the real HTTP API (development identity headers)."""
    flow = Flow()
    flow.uc03.fail_next = 1
    client = TestClient(create_orchestrator_app(agent=flow.agent,
                                                identity_provider=InsecureHeaderIdentityProvider(enabled=True)))
    state = flow.start()
    base = f"/v1/orchestration/cases/{state['case_id']}"
    documents = [synthetic_document(t) for t in flow.outstanding(state)]
    body = {"event_id": "DOCS-EVT-1", "type": "documents_submitted", "payload": {"documents": documents}}
    first = client.post(f"{base}/events", json=body, headers=_h(HR))
    assert first.status_code == 200 and first.json()["event"]["duplicate"] is False
    again = client.post(f"{base}/events", json=body, headers=_h(HR)).json()
    assert again["event"]["duplicate"] is True and flow.uc03.calls == 1

    stages = client.get(f"{base}/stages", headers=_h(HR_OPS)).json()["stages"]
    validation = stages["validate_documents"]
    assert (validation["use_case_id"], validation["status"], validation["stage_status"]) == \
        ("UC03", "FAILED", "retry_pending")
    assert validation["error"]["category"] == "infrastructure" and validation["error"]["retryable"] is True
    assert validation["last_execution"]["execution_id"].startswith("EXE-")
    assert {c["key"] for c in validation["dependencies"]} == {"uc01.employee_record", "uc03.document_collection",
                                                               "uc03.documents_supplied"}
    one = client.get(f"{base}/stages/equipment_access", headers=_h(HR_OPS)).json()["execution"]
    assert one["status"] == "NOT_STARTED" and not all(c["satisfied"] for c in one["dependencies"])
    assert client.get(f"{base}/stages/no_such_stage", headers=_h(HR)).status_code == 422

    errors = client.get(f"{base}/errors", headers=_h(HR)).json()["errors"]
    assert [(e["stage"], e["code"], e["category"]) for e in errors] == \
        [("validate_documents", "UC03_TECHNICAL_ERROR", "infrastructure")]

    resumed = client.post(f"{base}/events", json={"event_id": "RESUME-1", "type": "resume"}, headers=_h(HR))
    assert resumed.status_code == 200
    approvals = client.get(f"{base}/approvals", headers=_h(HR)).json()
    assert [p["gate"] for p in approvals["pending_approvals"]] == ["day_one_signoff"]

    registry = client.get("/v1/orchestration/dependencies", headers=_h(HR)).json()
    assert registry["client_id"] == CLIENT and "uc03.documents_verified" in {
        d["key"] for d in registry["dependencies"]["equipment_access"]}

    # Authorization and tenancy on the new routes.
    assert client.post(f"{base}/events", json={"event_id": "RESUME-2", "type": "resume"},
                       headers=_h(HR_OPS)).status_code == 403  # resume needs case_operator_roles
    for path in ("/stages", "/errors", "/approvals"):
        assert client.get(f"{base}{path}", headers=_h(OTHER_TENANT_HR)).status_code == 404
    assert client.post(f"{base}/events", json={"event_id": "X", "type": "resume"},
                       headers=_h(OTHER_TENANT_HR)).status_code == 404
    view = client.get(base, headers=_h(HR)).json()
    assert view["state_version"] >= 1 and view["field_provenance"]["employee_id"]["use_case_id"] == "UC01"
    assert "alex.morgan@example.com" not in str(view) and "Alex Morgan" not in str(stages)
