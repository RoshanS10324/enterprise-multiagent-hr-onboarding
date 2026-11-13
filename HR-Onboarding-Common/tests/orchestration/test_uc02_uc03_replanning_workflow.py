"""The revised UC01 -> UC02 -> UC03 workflow and the UC03 -> UC02 replanning loop, on the REAL orchestrator
graph (docs/UPDATED_ONBOARDING_WORKFLOW.md, docs/UC02_UC03_REPLANNING_CONTRACT.md).

What is real in each test (see _workflow.py / _uc02_real.py):
  * always: the orchestrator graph, routing, contract layer, state synchronization, case store; UC03's REAL
    document collection (DocumentCollectionService: collection per plan version, stored revisions,
    idempotent submissions) and UC03's REAL requirement-gap detection on its own catalog;
  * `real_uc02_flow()`: UC02's REAL OnboardingService for initial planning and for replanning
    (`define_document_requirements` / `replan_documents`) through the real replan adapter;
  * `Flow(real_uc03=True)`: UC03's REAL validator in-process;
  * otherwise: the contract-compatible doubles named in _workflow.py (never presented as a UC result).

Execution order is proven from what ran -- the shared gate's enter/exit events (_workflow._Gate) and
LangGraph's own checkpoint history -- not from completed-stage lists. All data is synthetic.
"""

from __future__ import annotations

import threading
from datetime import date

import pytest

from _uc02_real import real_uc02_flow
from _workflow import (CLIENT, HR, HR_2, HR_OPS, JOINING, SCHEDULER, Flow, code, status, synthetic_document)
from hr_onboarding_common.orchestration.adapters.uc03_documents import collection_service
from hr_onboarding_common.orchestration.adapters.uc05_orientation import PrerequisiteNotMetError
from hr_onboarding_common.orchestration.orchestrator_agent import WorkflowRequestError
from hr_onboarding_common.orchestration.state import Stage, StageStatus, WorkflowStatus
from hr_onboarding_common.orchestration.workflow_models import validate_workflow_state
from hr_onboarding_common.platform.policy import PolicyViolation

S, ST, W = Stage, StageStatus, WorkflowStatus
BRANCHES = ("equipment_access", "orientation_scheduling")


def _supersteps(flow: Flow, thread_id: str) -> list[list[str]]:
    """The nodes LangGraph executed in each superstep of the thread's last invocation, in order."""
    run = []
    for snapshot in flow.agent.graph.get_state_history({"configurable": {"thread_id": thread_id}}):
        run.append(sorted(t.name for t in snapshot.tasks if t.result is not None or t.error is not None))
        if snapshot.metadata.get("source") == "input":
            break
    return [step for step in reversed(run) if step and step != ["__start__"]]


def _nodes(flow: Flow, thread_id: str) -> list[str]:
    return [n for step in _supersteps(flow, thread_id) for n in step]


def _plan_docs(state) -> list[str]:
    return [d["document_type"] for d in state["onboarding_plan"]["required_documents"]]


def _mandatory_plan_docs(state) -> list[str]:
    """The plan's mandatory documents: what UC03 requests and waits for (optional ones -- e.g. the NDA and policy
    acknowledgements in UC03's checklist -- are validated when supplied but never requested or awaited)."""
    return [d["document_type"] for d in state["onboarding_plan"]["required_documents"] if d["required"]]


def _visa_passport(**meta):
    """A valid passport whose holder needs a visa: UC03's catalog then requires visa documents."""
    return synthetic_document("passport", visa_required=True, **meta)


def _supply_with_visa_passport(flow, state):
    documents = [synthetic_document(t) if t != "passport" else _visa_passport() for t in flow.outstanding(state)]
    return flow.agent.submit_documents(state["case_id"], documents=documents, actor=HR)


def _uc02_record(service, state):
    from onboarding_task_management.contracts import Actor, Role

    return service.get(Actor(actor_id=HR["on_behalf_of"], role=Role.HR_OPS, client_id=CLIENT),
                       state["onboarding_plan"]["plan_id"])


# ============================================================ UC01 -> UC02


def test_01_a_successful_intake_triggers_uc02_initial_planning_next():
    flow = Flow()
    state = flow.start()
    nodes = _nodes(flow, state["intake_trigger"]["origin_case_id"])
    assert nodes[:4] == ["form_approval", "intake", "intake_mismatch_proposal", "plan_tasks"]  # attested start: no gate
    assert flow.gate.entered("plan_tasks") == 1 and status(state, S.PLAN_TASKS) == ST.COMPLETE.value
    assert state["onboarding_plan"]["plan_version"] == 1


def test_02_an_invalid_intake_never_reaches_uc02():
    flow = Flow()
    state = flow.start(offer_accepted=False)  # UC01 holds an unverified acceptance
    assert status(state, S.INTAKE) == ST.BLOCKED.value
    assert flow.plan.calls == 0 and status(state, S.PLAN_TASKS) == ST.PENDING.value
    assert state["onboarding_plan"] is None and flow.notifier.sent_count() == 0


# ============================================================ UC02 -> UC03


def test_03_initial_planning_defines_versioned_document_requirements():
    flow, _service = real_uc02_flow()
    state = flow.start()
    plan = state["onboarding_plan"]
    assert (plan["plan_version"], plan["previous_plan_version"], plan["source"]) == (1, None, "uc02")
    catalog = flow.deps._uc03_collection_service.catalog({"role": "software_engineer", "location": "Dubai",
                                                          "grade": "", "nationality": ""})
    assert _plan_docs(state) == [g.document_type for g in catalog]  # UC03's validatable documents for the role
    # Each document carries UC03's own mandatory flag (NDA and policy acknowledgements are optional).
    assert {d["document_type"]: d["required"] for d in plan["required_documents"]} ==         {g.document_type: g.mandatory for g in catalog}
    assert [h["action"] for h in state["plan_history"]] == ["initial"]
    assert [d.key for d in _uc02_record(_service, state).documents] == _plan_docs(state)  # held by UC02 itself


def test_04_uc03_receives_the_plan_and_its_version():
    flow = Flow()
    state = flow.start()
    collection = state["document_collection"]
    assert (collection["plan_id"], collection["plan_version"], collection["source"]) == ("ONB-SYN-001", 1, "uc02_plan")
    request = collection["request"]
    assert (request["plan_id"], request["plan_version"], request["tenant_id"], request["employee_id"]) == \
        ("ONB-SYN-001", 1, CLIENT, state["employee_id"])
    assert request["required_documents"] == _plan_docs(state)
    assert request["mandatory_documents"] == _mandatory_plan_docs(state)
    assert set(request["validation_rules"]) == set(_plan_docs(state)) and all(request["due_dates"].values())
    uc03 = flow.deps._uc03_collection_service.collection(state["case_id"])  # opened in UC03 itself
    assert (uc03.plan_id, uc03.plan_version) == ("ONB-SYN-001", 1)


def test_05_uc02_neither_collects_nor_validates_documents():
    flow, service = real_uc02_flow()
    state = _supply_all = flow.supply(flow.start())
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.COMPLETE.value
    assert flow.plan.calls == 1  # planned once, before any document
    from onboarding_task_management.contracts import DocumentState

    assert {d.state for d in _uc02_record(service, state).documents} == {DocumentState.PENDING}  # UC02 judged none
    assert set(flow.deps._uc03_collection_service.latest(state["case_id"])) == set(state["submitted_documents"])
    assert _supply_all["stage_outcomes"]["validate_documents"]["uc"] == "UC03"


# ============================================================ UC03 document lifecycle


def test_06_document_collection_starts_with_one_request_for_the_plan_version():
    flow = Flow()
    state = flow.start()
    notification = state["document_collection"]["notification"]
    assert notification["status"] == "sent" and notification["idempotency_key"].endswith(":r1")
    assert notification["document_types"] == _mandatory_plan_docs(state) and flow.notifier.sent_count() == 1
    assert status(state, S.DOCUMENT_SUBMISSION) == ST.WAITING_FOR_INPUT.value
    assert [a["action"] for a in state["pending_actions"]] == ["supply_documents"]


def test_07_documents_are_stored_in_uc03_with_their_metadata_and_revision():
    flow = Flow()
    state = flow.supply(flow.start())
    stored = flow.deps._uc03_collection_service.latest(state["case_id"])
    passport = stored["passport"]
    assert (passport.revision, passport.plan_version, passport.document_ref) == (1, 1, "SYN-DOC-passport-1")
    assert passport.metadata["expiry_date"] == "2031-01-01"  # metadata kept with the reference, never contents
    projection = state["submitted_documents"]["passport"]
    assert (projection["storage_id"], projection["revision"], projection["plan_version"]) == \
        (passport.storage_id, 1, 1)


def test_08_a_duplicate_submission_is_idempotent():
    flow = Flow()
    state = flow.start()
    state = flow.supply(state, ["passport"])
    calls, version = flow.uc03.calls, flow.stored(state["case_id"])["state_version"]
    again = flow.supply(state, ["passport"])  # the same document again (a redelivered upload)
    assert again["submitted_documents"]["passport"]["revision"] == 1
    assert flow.uc03.calls == calls and flow.stored(state["case_id"])["state_version"] == version
    assert len(flow.deps._uc03_collection_service.history(state["case_id"], "passport")) == 1
    audit = flow.ctx.audit.find("orchestrator.documents_submitted")
    assert audit[-1]["documents"][0]["duplicate"] is True


def test_09_an_expired_document_asks_for_a_correction_without_replanning():
    flow = Flow(real_uc03=True)
    state = flow.start()
    documents = [synthetic_document(t) if t != "passport" else synthetic_document(t, expiry_date="2020-01-01")
                 for t in flow.outstanding(state)]
    state = flow.agent.submit_documents(state["case_id"], documents=documents, actor=HR)
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.WAITING_FOR_INPUT.value
    assert state["workflow_status"] == W.WAITING_FOR_INPUT.value
    feedback = state["document_validation_feedback"]
    assert feedback["expired_documents"] == ["passport"] and "passport" in feedback["invalid_documents"]
    assert feedback["correction_required"] is True and feedback["replanning_required"] is False
    assert status(state, S.REPLAN_TASKS) == ST.NOT_REQUESTED.value and flow.replan.calls == 0
    assert flow.uc04.calls == 0 and sum(flow.uc05.calls.values()) == 0


def test_10_missing_documents_wait_for_input():
    flow = Flow()
    state = flow.start()
    state = flow.supply(state, ["passport", "degree_certificate"])
    assert status(state, S.DOCUMENT_SUBMISSION) == ST.WAITING_FOR_INPUT.value
    assert "passport" not in flow.outstanding(state) and "payslips" in flow.outstanding(state)
    (action,) = [a for a in state["pending_actions"] if a["action"] == "supply_documents"]
    assert set(action["documents"]) == set(flow.outstanding(state))
    assert flow.uc03.calls == 0 and flow.uc04.calls == 0  # nothing validated or started yet


def test_11_a_corrected_document_is_revalidated_as_a_new_revision():
    flow = Flow(real_uc03=True)
    state = flow.start()
    documents = [synthetic_document(t) if t != "passport" else synthetic_document(t, expiry_date="2020-01-01")
                 for t in flow.outstanding(state)]
    state = flow.agent.submit_documents(state["case_id"], documents=documents, actor=HR)
    state = flow.supply(state, ["passport"], tag="2")
    result = state["document_validation_result"]["passport"]
    assert result["revision"] == 2 and "EXPIRED_DOCUMENT" not in result["findings"]
    assert "DUPLICATE_REQUEST" not in result["findings"]  # UC03 tracks revisions: validated again
    assert [d.revision for d in flow.deps._uc03_collection_service.history(state["case_id"], "passport")] == [1, 2]


def test_12_hr_review_interrupts_only_an_authorized_reviewer_resumes_and_nothing_is_consumed():
    flow = Flow()
    flow.uc03.verdicts = {"passport": "hr_review_required"}
    state = flow.supply(flow.start())
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.AWAITING_APPROVAL.value
    assert state["document_validation_feedback"]["pending_hr_reviews"] == ["passport"]
    assert flow.uc04.calls == 0 and sum(flow.uc05.calls.values()) == 0
    with pytest.raises(PolicyViolation):  # hr_ops may read the case, not decide a document review
        flow.agent.review_documents(state["case_id"], reviews=[{"document_type": "passport", "decision": "approved"}],
                                    actor=HR_OPS)
    unchanged = flow.agent.get_case(state["case_id"], client_id=CLIENT)
    assert status(unchanged, S.VALIDATE_DOCUMENTS) == ST.AWAITING_APPROVAL.value  # still pending, not consumed
    assert unchanged["submitted_documents"]["passport"].get("hr_review") is None
    flow.uc03.verdicts = {}
    state = flow.agent.review_documents(state["case_id"], reviews=[{"document_type": "passport",
                                                                    "decision": "approved"}], actor=HR)
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.COMPLETE.value
    assert status(state, S.EQUIPMENT_ACCESS) == status(state, S.ORIENTATION_SCHEDULING) == ST.COMPLETE.value


# ============================================================ UC03 -> UC02 replanning (real UC02)


def test_13_to_17_uc03_reports_a_plan_change_uc02_replans_and_uc03_revalidates_against_the_new_version():
    flow, service = real_uc02_flow()
    state = flow.start()
    assert "visa_documents" not in _plan_docs(state)
    origin = state["case_id"]

    # 13. UC03 finds the plan must change: the passport shows a visa is needed (UC03's own catalog rule).
    state = _supply_with_visa_passport(flow, state)
    feedback = state["document_validation_feedback"]
    assert feedback["replanning_required"] is True and feedback["plan_version"] == 1
    assert {(c["action"], c["target"]) for c in feedback["recommended_task_changes"]} == \
        {("add_document", "visa_documents"), ("add_attribute", "visa_required")}
    # 14-15. UC02 received it and produced plan version 2 (the real OnboardingService).
    record = _uc02_record(service, state)
    assert record.plan_version == 2 and "visa_documents" in [d.key for d in record.documents]
    assert "visa_required" in record.attributes and record.task_by_key("visa_processing") is not None
    assert status(state, S.REPLAN_TASKS) == ST.COMPLETE.value
    plan = state["onboarding_plan"]
    assert (plan["plan_version"], plan["previous_plan_version"]) == (2, 1) and "visa_documents" in _plan_docs(state)
    revised = state["revised_plan"]
    assert (revised["plan_version"], revised["previous_plan_version"], revised["feedback_id"]) == \
        (2, 1, feedback["feedback_id"])
    assert any(t.endswith("#visa_processing") for t in revised["added_tasks"])
    assert [h["action"] for h in state["plan_history"]] == ["initial", "replanned"]
    # 16. UC03 received the revised plan: its collection is version 2, and only the new document is asked for.
    assert state["document_collection"]["plan_version"] == 2
    assert flow.deps._uc03_collection_service.collection(state["case_id"]).plan_version == 2
    assert state["document_collection"]["notification"]["document_types"] == ["visa_documents"]
    assert flow.notifier.sent_count() == 2 and flow.outstanding(state) == ["visa_documents"]
    assert status(state, S.DOCUMENT_SUBMISSION) == ST.WAITING_FOR_INPUT.value
    assert flow.uc04.calls == 0 and sum(flow.uc05.calls.values()) == 0  # nothing downstream yet
    nodes = _nodes(flow, origin)
    i = nodes.index("validate_documents")
    assert nodes[i:i + 6] == ["validate_documents", "evaluate_document_result", "replan_tasks", "apply_revised_plan",
                              "document_collection", "document_submission"]

    # 17. UC03 revalidates against version 2; verified; then the branches run.
    state = flow.supply(state, ["visa_documents"])
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.COMPLETE.value
    assert flow.uc03.plan_versions == [1, 2]  # validated against version 1, then version 2
    assert state["document_validation_feedback"]["plan_version"] == 2
    assert state["verified_context"]["plan_version"] == 2
    assert status(state, S.EQUIPMENT_ACCESS) == status(state, S.ORIENTATION_SCHEDULING) == ST.COMPLETE.value
    assert flow.gate.violations == []
    validate_workflow_state(flow.stored(state["case_id"]))


def test_hr_requirement_change_is_replanned_by_uc02_and_applied_by_uc03():
    flow, service = real_uc02_flow()
    state = flow.start()
    state = flow.agent.request_document_requirement_change(
        state["case_id"], add_documents=["family_documents"], remove_documents=["payslips"],
        reason="sponsored family; payslips waived", actor=HR)
    assert state["onboarding_plan"]["plan_version"] == 2
    assert "family_documents" in _plan_docs(state) and "payslips" not in _plan_docs(state)
    (request,) = state["requirement_change_requests"]
    assert (request["status"], request["applied_in_plan_version"]) == ("applied", 2)
    assert "HR_REQUIREMENT_CHANGE" in state["revised_plan"]["replanning_reason"]
    assert _uc02_record(service, state).plan_version == 2
    assert "family_documents" in flow.outstanding(state) and "payslips" not in flow.outstanding(state)
    state = flow.supply(state)
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.COMPLETE.value
    assert "payslips" not in state["document_validation_result"]
    # HR's waiver stands: UC03's catalog (which lists payslips as mandatory) never re-adds it.
    assert state["onboarding_plan"]["plan_version"] == 2 and flow.uc03.plan_versions[-1] == 2
    # The same change again is recorded once; one that changes nothing or names an unknown type is refused.
    with pytest.raises(PrerequisiteNotMetError):  # verified: the requirements are final
        flow.agent.request_document_requirement_change(state["case_id"], add_documents=["nda"],
                                                       remove_documents=[], reason="x", actor=HR)


def test_a_requirement_change_is_refused_when_unauthorized_unknown_or_empty():
    flow = Flow()
    state = flow.start()
    with pytest.raises(PolicyViolation):
        flow.agent.request_document_requirement_change(state["case_id"], add_documents=["family_documents"],
                                                       remove_documents=[], reason="x", actor=HR_OPS)
    with pytest.raises(WorkflowRequestError):
        flow.agent.request_document_requirement_change(state["case_id"], add_documents=["tax_return"],
                                                       remove_documents=[], reason="x", actor=HR)
    with pytest.raises(WorkflowRequestError):  # already required: changes nothing
        flow.agent.request_document_requirement_change(state["case_id"], add_documents=["passport"],
                                                       remove_documents=[], reason="x", actor=HR)
    assert flow.agent.get_case(state["case_id"], client_id=CLIENT)["requirement_change_requests"] == []


def test_18_a_failed_replan_holds_the_case_and_neither_branch_starts():
    flow = Flow()
    flow.replan.status = ST.BLOCKED  # UC02 refuses the replan
    state = _supply_with_visa_passport(flow, flow.start())
    assert (status(state, S.REPLAN_TASKS), code(state, S.REPLAN_TASKS)) == (ST.BLOCKED.value, "REPLANNING_FAILED")
    assert code(state, S.VALIDATE_DOCUMENTS) == "REPLANNING_REQUIRED"
    assert state["onboarding_plan"]["plan_version"] == 1 and state["revised_plan"] is None  # nothing assumed applied
    assert state["document_collection"]["plan_version"] == 1
    assert state["workflow_status"] == W.BLOCKED.value and state["current_stage"] == S.REPLAN_TASKS.value
    assert flow.gate.entered("equipment_access") == flow.gate.entered("orientation_scheduling") == 0
    assert "dispatch_branches" not in _nodes(flow, state["case_id"])
    assert _nodes(flow, state["case_id"])[-1] == "await_replanning"

    flow.replan.status = ST.COMPLETE  # the cause is resolved in UC02
    state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.REPLAN_TASKS) == ST.COMPLETE.value and state["onboarding_plan"]["plan_version"] == 2
    state = flow.supply(state, ["visa_documents"])
    assert status(state, S.EQUIPMENT_ACCESS) == status(state, S.ORIENTATION_SCHEDULING) == ST.COMPLETE.value


def test_a_technical_replan_failure_is_retry_pending_and_bounded():
    flow = Flow()
    flow.replan.status = ST.RETRY_PENDING
    state = _supply_with_visa_passport(flow, flow.start())
    assert status(state, S.REPLAN_TASKS) == ST.RETRY_PENDING.value
    assert state["retry_metadata"]["replan_tasks"]["failures"] == 1 and flow.uc04.calls == 0
    for _ in range(2):
        state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.REPLAN_TASKS) == ST.FAILED.value and code(state, S.REPLAN_TASKS) == "RETRY_LIMIT_REACHED"
    flow.replan.status = ST.COMPLETE
    state = flow.agent.retry_stage(state["case_id"], "replan_tasks", actor=HR)
    assert status(state, S.REPLAN_TASKS) == ST.COMPLETE.value and state["onboarding_plan"]["plan_version"] == 2


def test_19_a_plain_resubmission_needs_no_replanning():
    flow = Flow()
    flow.uc03.verdicts = {"passport": "correction_required"}
    state = flow.supply(flow.start())
    flow.uc03.verdicts = {}
    state = flow.supply(state, ["passport"], tag="2")
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.COMPLETE.value
    assert flow.replan.calls == 0 and status(state, S.REPLAN_TASKS) == ST.NOT_REQUESTED.value
    assert state["onboarding_plan"]["plan_version"] == 1 and len(state["plan_history"]) == 1


def test_the_replanning_loop_is_bounded_per_invocation():
    flow = Flow()
    flow.deps.max_replan_rounds = 0  # no loop allowed in this run
    state = _supply_with_visa_passport(flow, flow.start())
    assert (status(state, S.REPLAN_TASKS), code(state, S.REPLAN_TASKS)) == (ST.BLOCKED.value, "REPLAN_LIMIT_REACHED")
    assert flow.replan.calls == 0 and flow.uc04.calls == 0
    flow.deps.max_replan_rounds = 3
    state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.REPLAN_TASKS) == ST.COMPLETE.value and flow.replan.calls == 1


def test_a_stale_validation_result_is_never_accepted_after_the_plan_changed():
    """UC02 versions its plan for an approved joining-date change while the documents are still being
    collected: the result UC03 produced for version 1 is stale, so UC03's stages re-open and revalidate
    under version 2 before anything is dispatched."""
    flow, service = real_uc02_flow()
    state = flow.start()
    state, outcome = flow.agent.request_uc02_date_change(state["case_id"], joining_date=date(2026, 12, 15), actor=HR)
    state = flow.agent.decide_approval(state["case_id"], outcome["request_id"], decision="approved", actor=HR_2)
    assert state["onboarding_plan"]["plan_version"] == 2 and state["document_collection"]["plan_version"] == 1
    assert [h["action"] for h in state["plan_history"]] == ["initial", "date_change"]
    state = flow.supply(state)
    # Validated against the collection UC03 still held (version 1), found stale, re-opened for version 2
    # and validated again -- the version-1 result was never accepted.
    assert flow.uc03.plan_versions == [1, 2]
    assert state["document_collection"]["plan_version"] == 2
    assert state["document_validation_feedback"]["plan_version"] == 2
    assert state["verified_context"]["plan_version"] == 2 and state["joining_date"] == "2026-12-15"
    assert any(e.get("code") == "STALE_PLAN_VERSION" for e in state["stage_history"])
    assert status(state, S.EQUIPMENT_ACCESS) == ST.COMPLETE.value
    assert flow.notifier.sent_count() == 1  # nothing new to collect: no second request


# ============================================================ downstream parallel execution


def test_20_to_22_branches_start_only_after_validation_run_concurrently_and_share_the_context():
    flow = Flow()
    flow.gate.barrier = threading.Barrier(2, timeout=10)  # passes only if both branches run at once
    flow.gate.delay["validate_documents"] = 0.2
    state = _supply_with_visa_passport(flow, flow.start())  # one replan round first
    state = flow.supply(state, ["visa_documents"])
    assert not flow.gate.barrier.broken and len(set(flow.gate.threads.values())) == 2
    validate_out = flow.gate.interval("validate_documents")[1]
    assert validate_out <= flow.gate.interval("equipment_access")[0]
    assert validate_out <= flow.gate.interval("orientation_scheduling")[0]
    assert flow.gate.violations == []  # checked at each branch's entry, under the gate's lock
    for stage in (S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING):
        inputs = state["stage_outcomes"][stage.value]["inputs"]
        assert (inputs["employee_id"], inputs["joining_date"]) == (state["employee_id"], JOINING.isoformat())
        assert (inputs["plan_version"], inputs["verified_plan_version"], inputs["documents_verified"]) == (2, 2, True)


def test_23_a_uc03_failure_starts_neither_branch():
    flow = Flow()
    flow.uc03.fail_next = 1
    state = flow.supply(flow.start())
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.RETRY_PENDING.value
    assert flow.gate.entered("equipment_access") == flow.gate.entered("orientation_scheduling") == 0
    assert "dispatch_branches" not in _nodes(flow, state["case_id"])


def test_24_a_pending_hr_review_starts_neither_branch():
    flow = Flow()
    flow.uc03.default = "hr_review_required"
    state = flow.supply(flow.start())
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.AWAITING_APPROVAL.value
    assert flow.gate.entered("equipment_access") == flow.gate.entered("orientation_scheduling") == 0
    assert state["verified_context"] is None


# ============================================================ recovery and regression


def test_25_a_redelivered_document_event_stores_nothing_twice():
    flow = Flow()
    state = flow.start()
    payload = {"documents": [synthetic_document("passport")]}
    _, first = flow.agent.handle_event(state["case_id"], event_id="EVT-DOC-1", event_type="documents_submitted",
                                       payload=payload, actor=HR)
    _, again = flow.agent.handle_event(state["case_id"], event_id="EVT-DOC-1", event_type="documents_submitted",
                                       payload=payload, actor=HR)
    _, other = flow.agent.handle_event(state["case_id"], event_id="EVT-DOC-2", event_type="documents_submitted",
                                       payload=payload, actor=HR)  # another event id, the same document
    assert (first["duplicate"], again["duplicate"], other["duplicate"]) == (False, True, False)
    assert len(flow.deps._uc03_collection_service.history(state["case_id"], "passport")) == 1  # UC03 deduplicated


def test_26_to_28_a_restart_restores_the_plan_version_pending_corrections_and_resumes_replanning(tmp_path):
    from hr_onboarding_common.orchestration.case_store import SqliteCaseStateStore

    def flow_on_disk():
        return Flow(case_states=SqliteCaseStateStore(tmp_path / "case_states.db"), persist_dir=tmp_path)

    flow = flow_on_disk()
    flow.uc03.verdicts = {"degree_certificate": "correction_required"}
    state = _supply_with_visa_passport(flow, flow.start())
    case_id = state["case_id"]
    assert state["onboarding_plan"]["plan_version"] == 2
    assert sorted(flow.outstanding(state)) == ["degree_certificate", "visa_documents"]  # a correction, a new document

    restarted = flow_on_disk()  # a new process: new doubles, the same SQLite case store and UC03 registry
    restarted.replan.applied = dict(flow.replan.applied)  # UC02's own ledger survives a restart (UC02's store)
    restarted.uc03.verdicts = {"degree_certificate": "correction_required"}
    state = restarted.agent.get_case(case_id, client_id=CLIENT)
    assert state["onboarding_plan"]["plan_version"] == 2  # 26. the plan version
    assert collection_service(restarted.deps).collection(case_id).plan_version == 2  # UC03's SQLite registry
    assert [r["document_type"] for r in state["correction_requirements"]] == ["degree_certificate"]  # 27.
    again = restarted.agent.submit_documents(case_id, documents=[_visa_passport()], actor=HR)  # redelivered
    assert again["submitted_documents"]["passport"]["revision"] == 1  # UC03's registry still knows it
    restarted.uc03.verdicts = {}
    state = restarted.supply(again, ["visa_documents", "degree_certificate"], tag="2")
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.COMPLETE.value
    assert restarted.replan.calls == 0 and restarted.plan.calls == 0 and restarted.uc01.calls == 0  # 28. not repeated
    assert state["onboarding_plan"]["plan_version"] == 2 and restarted.notifier.sent_count() == 0


def test_28_a_replan_uc02_applied_before_a_crash_is_not_applied_twice():
    """The crash window: UC02 applied the replan, the orchestrator never recorded it. The resumed replan
    sends the same feedback id; UC02 answers with its first outcome -- no third plan version."""
    flow, service = real_uc02_flow()
    flow.deps.max_replan_rounds = 0  # stop before replanning, as if the process died at that point
    state = _supply_with_visa_passport(flow, flow.start())
    feedback = state["document_validation_feedback"]
    from onboarding_task_management.contracts import Actor, DocumentFeedback, DocumentSpec, Role

    actor = Actor(actor_id=HR["on_behalf_of"], role=Role.HR_OPS, client_id=CLIENT)
    service.replan_documents(actor, state["onboarding_plan"]["plan_id"], DocumentFeedback(  # UC02 applied it ...
        feedback_id=feedback["feedback_id"], plan_version=1, add_documents=[DocumentSpec(document_type="visa_documents")],
        add_attributes=["visa_required"]))
    flow.deps.max_replan_rounds = 3
    state = flow.agent.resume_case(state["case_id"], actor=HR)  # ... and the orchestrator resumes
    assert state["revised_plan"]["duplicate_replay"] is True
    assert state["onboarding_plan"]["plan_version"] == 2 and _uc02_record(service, state).plan_version == 2


def test_29_day_one_readiness_signoff_and_welcome_still_follow_a_replanned_case():
    flow = Flow()
    state = _supply_with_visa_passport(flow, flow.start())
    state = flow.supply(state, ["visa_documents"])
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value  # never auto-completed
    state = flow.approve_signoff(state)
    state = flow.agent.trigger_joining_day(state["case_id"], as_of=JOINING, actor=SCHEDULER)
    assert state["workflow_status"] == W.COMPLETED.value and state["current_stage"] == S.COMPLETE.value
    assert (flow.plan.calls, flow.replan.calls, flow.uc04.calls, flow.uc05.calls["orientation"]) == (1, 1, 1, 1)


def test_31_no_nda_or_policy_signature_tracking_was_added_to_the_plan_or_state():
    """UC03's catalog lists an NDA and policy acknowledgements as onboarding documents (it did before this
    change); no signature-tracking field, stage or route was introduced by the UC02/UC03 redesign."""
    flow = Flow()
    state = flow.supply(flow.start())
    new_fields = ("onboarding_plan", "plan_history", "revised_plan", "document_validation_feedback",
                  "requirement_change_requests")
    for field in new_fields:
        assert "signature" not in str(state.get(field)).lower()
    assert not [s for s in Stage if "nda" in s.value or "signature" in s.value or "acknowledg" in s.value]
