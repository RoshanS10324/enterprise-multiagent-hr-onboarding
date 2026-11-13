"""The state-driven workflow (docs/STATE_DRIVEN_ONBOARDING_WORKFLOW.md) on the REAL orchestrator
graph: document collection, the wait for documents, UC03-driven routing, correction and resume, UC02
task planning as the sequential prerequisite, the parallel fan-out to UC04 and UC05 and the fan-in,
status aggregation, retries, idempotency, tenancy, authorization and privacy. Execution timing and
concurrency are proven in test_execution_order.py.

UC doubles and what is real: see _workflow.py. Synthetic data, FakeLLM and mocks only; no test
calls a real service. The real-UC03 tests at the end run UC03's own code in-process."""

from __future__ import annotations

import json
import logging
import threading

import pytest
from pydantic import ValidationError

from _helpers import require_uc
from _workflow import (BRANCH_STAGES, CANDIDATE, CLIENT, HR, HR_OPS, JOINING, OFFICER, OTHER_TENANT_HR,
                       POST_VERIFICATION_STAGES, SCHEDULER, Flow, code, status, synthetic_document)
from hr_onboarding_common.orchestration.adapters._shared import OrchestratorAuthorizationError
from hr_onboarding_common.orchestration.adapters.uc05_orientation import PrerequisiteNotMetError
from hr_onboarding_common.orchestration.case_store import SqliteCaseStateStore
from hr_onboarding_common.orchestration.notifications import MockNotificationOutbox
from hr_onboarding_common.orchestration.orchestrator_agent import WorkflowRequestError
from hr_onboarding_common.orchestration.state import (Branch, Stage, StageStatus, WorkflowStatus,
                                                      derive_workflow_status, merge_branch_outcomes, new_state)
from hr_onboarding_common.orchestration.workflow import fold_branch_outcome
from hr_onboarding_common.orchestration.workflow_models import validate_workflow_state
from hr_onboarding_common.platform.hitl import Decision, HITLError

S, ST, W = Stage, StageStatus, WorkflowStatus


def _order(state) -> list[str]:
    """Stages in the order they first started running."""
    seen: list[str] = []
    for event in state["stage_history"]:
        if event["stage"] not in seen:
            seen.append(event["stage"])
    return seen


# ============================================================ 1. happy path


def test_happy_path_collects_waits_validates_fans_out_merges_and_completes():
    flow = Flow()
    state = flow.start()
    # UC01 created the record, UC02 operation A requested the documents (mock), the case now WAITS.
    assert status(state, S.INTAKE) == status(state, S.DOCUMENT_COLLECTION) == ST.COMPLETE.value
    assert status(state, S.DOCUMENT_SUBMISSION) == ST.WAITING_FOR_INPUT.value
    assert state["workflow_status"] == W.WAITING_FOR_INPUT.value
    assert S.DOCUMENT_SUBMISSION.value not in state["completed_stages"]  # a wait is not a completion
    assert all(status(state, s) == ST.PENDING.value for s in (S.VALIDATE_DOCUMENTS, *POST_VERIFICATION_STAGES))
    request = state["document_collection"]["notification"]
    assert request["status"] == "sent" and request["synthetic"] is True and request["transport"] == "mock-notification"
    outstanding = flow.outstanding(state)
    required = [d["document_type"] for d in state["document_collection"]["required_documents"] if d.get("required", True)]
    assert outstanding and outstanding == required  # only mandatory documents are awaited
    assert [a["action"] for a in state["pending_actions"]] == ["supply_documents"]
    assert state["pending_actions"][0]["documents"] == outstanding

    state = flow.supply(state)
    for stage in (S.DOCUMENT_SUBMISSION, S.VALIDATE_DOCUMENTS, S.PLAN_TASKS, S.EQUIPMENT_ACCESS,
                  S.ORIENTATION_SCHEDULING, S.BUDDY_ASSIGNMENT, S.TEAM_INTRODUCTION):
        assert status(state, stage) == ST.COMPLETE.value
    assert {b: s["status"] for b, s in state["branch_statuses"].items()} == {b.value: W.COMPLETED.value for b in Branch}
    # The merged outcomes: every branch's result is in the shared state.
    assert state["task_plan"]["onboarding_id"] == "ONB-SYN-001"
    assert state["equipment_access_result"]["overall_status"] == "READY"
    assert state["orientation_schedule_result"]["session_id"] == "SES-SYN-1"
    # Readiness is a proposal for HR, never auto-completed: the workflow waits for approval.
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value
    assert state["workflow_status"] == W.WAITING_FOR_APPROVAL.value and state["overall_status"] != "complete"

    state = flow.approve_signoff(state)
    assert status(state, S.JOINING_DAY_WELCOME) == ST.WAITING.value
    assert state["workflow_status"] == W.WAITING_FOR_INPUT.value
    state = flow.agent.trigger_joining_day(state["case_id"], as_of=JOINING, actor=SCHEDULER)
    assert state["workflow_status"] == W.COMPLETED.value and state["overall_status"] == "complete"
    assert state["pending_actions"] == [] and state["current_stage"] == S.COMPLETE.value

    order = _order(state)
    assert order.index("intake") < order.index("plan_tasks") < order.index("document_collection") \
        < order.index("document_submission") < order.index("validate_documents") \
        < min(order.index(s.value) for s in BRANCH_STAGES)
    assert status(state, S.REPLAN_TASKS) == ST.NOT_REQUESTED.value  # nothing needed replanning
    # Each UC was called once; the one document request was sent once.
    assert (flow.uc01.calls, flow.plan.calls, flow.uc04.calls, flow.uc03.calls) == (1, 1, 1, 1)
    assert flow.notifier.sent_count() == 1
    validate_workflow_state(flow.stored(state["case_id"]))  # the persisted state meets its typed contract


def test_each_stage_records_a_structured_outcome_and_the_data_it_was_handed():
    flow = Flow()
    state = flow.to_branches()
    outcomes = state["stage_outcomes"]
    assert outcomes["intake"]["uc"] == "UC01"
    assert (outcomes["plan_tasks"]["uc"], outcomes["plan_tasks"]["operation"]) == ("UC02", "initial_task_planning")
    assert (outcomes["document_collection"]["uc"], outcomes["document_collection"]["operation"]) == \
        ("UC03", "initiate_document_collection")  # UC03 owns the document lifecycle
    assert {o["data_origin"] for o in outcomes.values()} == {"synthetic"}
    # UC01's record flows into planning; UC02's plan (id and version) into UC03's collection.
    assert outcomes["plan_tasks"]["inputs"]["employee_id"] == state["employee_id"]
    assert outcomes["document_collection"]["inputs"]["plan_version"] == 1
    assert outcomes["document_collection"]["inputs"]["required_documents"] == \
        [d["document_type"] for d in state["onboarding_plan"]["required_documents"]]
    # UC03's verification flows into every branch, with the plan version it verified against.
    for stage in POST_VERIFICATION_STAGES:
        inputs = outcomes[stage.value]["inputs"]
        assert inputs["employee_id"] == state["employee_id"] and inputs["documents_verified"] is True
        assert sorted(inputs["verified_document_types"]) == sorted(state["submitted_documents"])
        assert inputs["plan_version"] == inputs["verified_plan_version"] == 1
    assert outcomes["validate_documents"]["inputs"]["documents"]["passport"]["document_ref"] == "SYN-DOC-passport-1"
    assert state["verified_context"]["documents_verified"] is True
    assert flow.plan.received[0]["documents_seen"] == []  # UC02 planned before any document existed
    # UC04's contract has no task-plan field: recorded explicitly, not silently assumed.
    assert "task_plan_available" in outcomes["equipment_access"]["inputs"]


# ============================================================ 2. parallel fan-out


def test_stage_history_records_plan_tasks_before_either_downstream_branch():
    # The order of stage_history only (merge_outcomes appends the branches' events after plan_tasks's,
    # whatever the timing): actual entry/exit timing is asserted in test_execution_order.py.
    flow = Flow()
    state = flow.to_branches()
    order = _order(state)
    assert order.index("plan_tasks") < order.index("equipment_access")
    assert order.index("plan_tasks") < order.index("orientation_scheduling")
    assert all(status(state, s) == ST.COMPLETE.value for s in POST_VERIFICATION_STAGES)


def test_equipment_access_and_orientation_scheduling_run_at_the_same_time():
    flow = Flow()
    flow.gate.barrier = threading.Barrier(2, timeout=10)  # passes only if both are running together
    state = flow.to_branches()
    assert status(state, S.EQUIPMENT_ACCESS) == status(state, S.ORIENTATION_SCHEDULING) == ST.COMPLETE.value
    assert not flow.gate.barrier.broken
    assert len(set(flow.gate.threads.values())) == 2  # each branch on its own thread


def test_the_graph_runs_uc01_uc02_uc03_in_sequence_and_only_verification_fans_out():
    graph = Flow().agent.graph.get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    # intake -> plan_tasks (UC02) -> document_collection (UC03) -> ... -> evaluate_document_result
    assert {("intake_mismatch_proposal", "plan_tasks"), ("plan_tasks", "document_collection"),
            ("validate_documents", "evaluate_document_result"),
            ("evaluate_document_result", "record_verification"), ("record_verification", "dispatch_branches")} <= edges
    # the UC03 -> UC02 -> UC03 replanning loop
    assert {("evaluate_document_result", "replan_tasks"), ("replan_tasks", "apply_revised_plan"),
            ("apply_revised_plan", "document_collection")} <= edges
    assert {("dispatch_branches", n) for n in ("equipment_access", "orientation_scheduling")} <= edges
    # Nothing but record_verification reaches the fan-out: no route from UC01/UC02/UC03 bypasses verification.
    assert {s for s, t in edges if t == "dispatch_branches"} <= {"record_verification", "evaluate_overall_status"}
    for source in ("intake", "intake_mismatch_proposal", "plan_tasks", "replan_tasks", "document_collection",
                   "document_submission", "validate_documents"):
        assert not {(source, n) for n in ("equipment_access", "orientation_scheduling", "dispatch_branches")} & edges
    assert {("equipment_access", "merge_outcomes"), ("uc05_branch_end", "merge_outcomes")} <= edges
    assert ("plan_tasks", "merge_outcomes") not in edges and ("dispatch_branches", "merge_outcomes") not in edges


def test_plan_tasks_blocked_prevents_document_collection_and_both_downstream_branches():
    flow = Flow()
    flow.plan.status = ST.BLOCKED
    state = flow.start()
    assert status(state, S.PLAN_TASKS) == ST.BLOCKED.value
    assert status(state, S.DOCUMENT_COLLECTION) == ST.PENDING.value and flow.notifier.sent_count() == 0
    assert status(state, S.EQUIPMENT_ACCESS) == status(state, S.ORIENTATION_SCHEDULING) == ST.PENDING.value
    assert flow.uc04.calls == 0 and flow.uc05.calls["orientation"] == 0  # neither branch was ever attempted
    assert "equipment_access" not in state["stage_outcomes"] and "orientation_scheduling" not in state["stage_outcomes"]
    assert state["workflow_status"] == W.BLOCKED.value and state["current_stage"] == S.PLAN_TASKS.value
    assert status(state, S.DAY_ONE_READINESS) == ST.PENDING.value

    flow.plan.status = ST.COMPLETE
    state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.PLAN_TASKS) == ST.COMPLETE.value
    assert status(state, S.DOCUMENT_SUBMISSION) == ST.WAITING_FOR_INPUT.value  # UC03 collects now
    state = flow.supply(state)
    assert status(state, S.EQUIPMENT_ACCESS) == status(state, S.TEAM_INTRODUCTION) == ST.COMPLETE.value
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value
    assert (flow.uc01.calls, flow.uc04.calls, flow.uc05.calls["orientation"], flow.plan.calls) == (1, 1, 1, 2)


# ============================================================ 3-5. validation routing and correction


def test_invalid_documents_wait_for_correction_and_start_no_branch():
    flow = Flow()
    flow.uc03.verdicts = {"passport": "correction_required", "payslips": "missing"}
    state = flow.to_branches()
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.WAITING_FOR_INPUT.value
    assert state["workflow_status"] == W.WAITING_FOR_INPUT.value
    assert state["current_stage"] == S.VALIDATE_DOCUMENTS.value
    assert all(status(state, s) == ST.PENDING.value for s in POST_VERIFICATION_STAGES)
    # UC02 planned once, before the documents; neither branch was attempted.
    assert (flow.plan.calls, flow.uc04.calls, sum(flow.uc05.calls.values())) == (1, 0, 0)
    # Exactly what is wrong, per document.
    requirements = {r["document_type"]: r for r in state["correction_requirements"]}
    assert set(requirements) == {"passport", "payslips"}
    assert requirements["passport"]["action"] == "resubmit"
    assert requirements["passport"]["reason_codes"] == ["EXPIRED_DOCUMENT"]
    assert requirements["payslips"]["action"] == "supply"
    assert {i["document_type"] for i in state["validation_issues"]} == {"passport", "payslips"}
    actions = {a["action"]: a["documents"] for a in state["pending_actions"]}
    assert actions == {"supply_documents": ["payslips"], "correct_documents": ["passport"]}


def test_corrected_documents_resume_at_validation_without_recreating_the_record_or_resending_the_request():
    flow = Flow()
    flow.uc03.verdicts = {"passport": "correction_required"}
    state = flow.to_branches()
    attempts_before = dict(state["stage_attempts"])

    state = flow.agent.resume_case(state["case_id"], actor=HR)  # nothing new: still waiting
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.WAITING_FOR_INPUT.value

    state = flow.supply(state, ["degree_certificate"], tag="2")  # not the document that needs correcting
    assert status(state, S.DOCUMENT_SUBMISSION) == ST.WAITING_FOR_INPUT.value
    assert flow.outstanding(state) == ["passport"] and flow.uc03.calls == 2  # UC03 was not asked again

    flow.uc03.verdicts = {}
    state = flow.supply(state, ["passport"], tag="2")
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.COMPLETE.value
    assert all(status(state, s) == ST.COMPLETE.value for s in POST_VERIFICATION_STAGES)
    assert state["submitted_documents"]["passport"]["revision"] == 2
    assert flow.uc03.seen[-1]["passport"] == 2  # the corrected revision was the one validated
    assert flow.uc01.calls == 1 and flow.notifier.sent_count() == 1
    assert flow.notifier.attempts_for(state["document_collection"]["notification"]["idempotency_key"]) == 1
    for stage in (S.INTAKE, S.DOCUMENT_COLLECTION):
        assert state["stage_attempts"][stage.value] == attempts_before[stage.value] == 1
    assert not [b for b in state["blockers"] if b["stage"] == S.VALIDATE_DOCUMENTS.value]
    assert state["correction_requirements"] == []


def test_documents_needing_hr_review_wait_for_an_authorized_reviewer():
    flow = Flow()
    flow.uc03.verdicts = {"passport": "hr_review_required"}
    state = flow.to_branches()
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.AWAITING_APPROVAL.value
    assert state["workflow_status"] == W.WAITING_FOR_APPROVAL.value
    assert [a for a in state["pending_actions"] if a["action"] == "review_documents"][0]["documents"] == ["passport"]
    assert flow.plan.calls == 1 and flow.uc04.calls == 0  # planned before the documents; no branch yet


def test_a_uc03_technical_failure_is_retry_pending_never_a_document_rejection():
    flow = Flow()
    flow.uc03.fail_next = 1
    state = flow.to_branches()
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.RETRY_PENDING.value
    assert code(state, S.VALIDATE_DOCUMENTS) == "UC03_TECHNICAL_ERROR"
    assert state["workflow_status"] == W.RETRY_PENDING.value and state["overall_status"] == "at_risk"
    assert state["correction_requirements"] == [] and state["validation_issues"] == []  # no document was judged
    assert [a["action"] for a in state["pending_actions"]] == ["retry_stage"]
    assert state["retry_metadata"]["validate_documents"]["failures"] == 1
    assert flow.plan.calls == 1 and flow.uc04.calls == 0  # no branch starts on a technical failure

    state = flow.agent.retry_stage(state["case_id"], "validate_documents", actor=HR)
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.COMPLETE.value
    assert all(status(state, s) == ST.COMPLETE.value for s in POST_VERIFICATION_STAGES)
    assert flow.uc01.calls == 1 and flow.notifier.sent_count() == 1
    audit = flow.ctx.audit.find("orchestrator.stage_retry_requested")
    assert audit and audit[-1]["stage"] == "validate_documents" and audit[-1]["code"] == "UC03_TECHNICAL_ERROR"


def test_repeated_technical_failures_exhaust_the_retry_budget_and_a_retry_resets_it():
    flow = Flow()
    flow.uc03.fail_next = 5
    state = flow.to_branches()
    for _ in range(2):
        state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.FAILED.value  # stage_max_attempts = 3
    assert code(state, S.VALIDATE_DOCUMENTS) == "RETRY_LIMIT_REACHED"
    assert state["retry_metadata"]["validate_documents"]["exhausted"] is True
    assert state["workflow_status"] == W.FAILED.value

    flow.uc03.fail_next = 0
    state = flow.agent.retry_stage(state["case_id"], "validate_documents", actor=HR)
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.COMPLETE.value
    assert state["retry_metadata"]["validate_documents"]["retries_authorized"] == 1


def test_only_a_technical_failure_can_be_retried():
    flow = Flow()
    flow.uc03.verdicts = {"passport": "correction_required"}
    state = flow.to_branches()
    with pytest.raises(PrerequisiteNotMetError):  # invalid documents need documents, not a retry
        flow.agent.retry_stage(state["case_id"], "validate_documents", actor=HR)
    with pytest.raises(PrerequisiteNotMetError):  # a completed stage is never re-run
        flow.agent.retry_stage(state["case_id"], "intake", actor=HR)
    with pytest.raises(WorkflowRequestError):
        flow.agent.retry_stage(state["case_id"], "no_such_stage", actor=HR)


# ============================================================ 6-7. branch failure and waits


def test_one_branch_blocked_preserves_the_other_branches_results():
    """One branch that cannot proceed does not discard the other branch's results: UC05's orientation
    is BLOCKED (the double) while UC04 completes; both outcomes are merged and persisted."""
    flow = Flow()
    flow.uc05.status["orientation"] = ST.BLOCKED
    state = flow.to_branches()
    assert status(state, S.ORIENTATION_SCHEDULING) == ST.BLOCKED.value
    assert status(state, S.EQUIPMENT_ACCESS) == ST.COMPLETE.value
    assert state["equipment_access_result"]["overall_status"] == "READY"
    assert state["workflow_status"] == W.BLOCKED.value and state["overall_status"] == "blocked"
    assert state["branch_statuses"][Branch.ORIENTATION.value]["status"] == W.BLOCKED.value
    assert state["branch_statuses"][Branch.EQUIPMENT_ACCESS.value]["status"] == W.COMPLETED.value
    assert state["current_stage"] == S.ORIENTATION_SCHEDULING.value
    stored = flow.stored(state["case_id"])
    assert stored["equipment_access_result"] == state["equipment_access_result"]  # persisted, not only returned


def test_the_real_uc02_refusing_to_plan_stops_the_case_before_any_document_is_requested():
    """The real UC02 adapter with an intake that carries no offer-acceptance / form-approval timestamps:
    UC02's own start-condition check refuses (BLOCKED, MISSING_DATA) -- nothing is created or assumed --
    and, UC02 being first after intake, UC03 never collects and no branch starts."""
    flow = Flow(real_uc02=True)  # the real UC02 adapter and package
    state = flow.start()
    assert status(state, S.PLAN_TASKS) == ST.BLOCKED.value and code(state, S.PLAN_TASKS) == "MISSING_DATA"
    assert "offer acceptance has no timestamp" in state["stage_errors"][S.PLAN_TASKS.value]["detail"]
    assert state["task_plan"] is None and state["onboarding_plan"] is None
    assert status(state, S.DOCUMENT_COLLECTION) == ST.PENDING.value and flow.notifier.sent_count() == 0
    assert flow.uc04.calls == 0 and sum(flow.uc05.calls.values()) == 0
    assert state["workflow_status"] == W.BLOCKED.value and state["current_stage"] == S.PLAN_TASKS.value


def test_a_branch_waiting_for_approval_keeps_the_workflow_waiting():
    flow = Flow(uc04_status="AWAITING_APPROVAL")
    state = flow.to_branches()
    assert status(state, S.EQUIPMENT_ACCESS) == ST.AWAITING_APPROVAL.value
    assert status(state, S.PLAN_TASKS) == status(state, S.TEAM_INTRODUCTION) == ST.COMPLETE.value
    assert state["workflow_status"] == W.WAITING_FOR_APPROVAL.value and state["overall_status"] != "complete"
    assert status(state, S.DAY_ONE_READINESS) == ST.PENDING.value
    decisions = [a["agent"] for a in state["pending_actions"] if a["action"] == "decide_approval"]
    assert decisions == ["dact-uc04-equipment-access"]


def test_parallel_branch_results_merge_without_overwriting_each_other():
    flow = Flow(uc04_status="AWAITING_APPROVAL")
    flow.uc05.status["orientation"] = ST.AWAITING_APPROVAL
    state = flow.to_branches()
    assert status(state, S.PLAN_TASKS) == ST.COMPLETE.value  # ran to completion before the fan-out
    approvals = {p["stage"]: p["agent"] for p in state["pending_approvals"]}
    assert approvals == {"equipment_access": "dact-uc04-equipment-access",
                         "orientation_scheduling": "employee-onboarding"}
    assert {b["stage"] for b in state["blockers"]} >= {"equipment_access"}
    history = {(e["stage"], e["status"]) for e in state["stage_history"]}
    assert {("equipment_access", "awaiting_approval"), ("orientation_scheduling", "awaiting_approval")} <= history
    assert state["workflow_status"] == W.WAITING_FOR_APPROVAL.value  # neither branch is hidden
    assert state["branch_outcomes"] == {}  # the transient channel is folded and cleared
    stored = flow.stored(state["case_id"])  # the store holds the same merged result
    assert stored["stage_statuses"] == state["stage_statuses"]
    assert stored["pending_approvals"] == state["pending_approvals"]


def test_the_branch_reducer_never_lets_one_branch_overwrite_another():
    left = merge_branch_outcomes({}, {"uc04_equipment_access": {"n": 1}})
    both = merge_branch_outcomes(left, {"uc05_orientation": {"n": 2}})
    assert both == {"uc04_equipment_access": {"n": 1}, "uc05_orientation": {"n": 2}}
    assert merge_branch_outcomes(both, {"__reset__": True}) == {}
    base = new_state(case_id="C1", client_id="CL1", correlation_id="c", actor={"on_behalf_of": "u", "roles": []},
                     intake_trigger={})
    delta = {"branch": "uc04_equipment_access", "stage_statuses": {"equipment_access": "complete"},
             "stage_errors": {}, "stage_attempts": {"equipment_access": 1}, "stage_outcomes": {}, "retry_metadata": {},
             "idempotency_keys": {}, "stage_history": [{"stage": "equipment_access", "status": "complete", "at": "t"}],
             "blockers": [], "pending_approvals": [],
             "results": {"equipment_access_result": {"overall_status": "READY"}}}
    folded = fold_branch_outcome(base, delta)
    assert folded["stage_statuses"]["equipment_access"] == "complete"
    assert folded["stage_statuses"]["plan_tasks"] == "pending"  # another branch's stage is untouched
    assert base["stage_statuses"]["equipment_access"] == "pending"  # the input is not changed


@pytest.mark.parametrize("statuses, expected", [
    ({"plan_tasks": "failed", "equipment_access": "complete"}, W.FAILED),
    ({"plan_tasks": "complete", "equipment_access": "awaiting_approval"}, W.WAITING_FOR_APPROVAL),
    ({"equipment_access": "awaiting_approval", "orientation_scheduling": "blocked"}, W.BLOCKED),
    ({"orientation_scheduling": "blocked"}, W.BLOCKED),
    ({"validate_documents": "retry_pending", "plan_tasks": "awaiting_approval"}, W.RETRY_PENDING),
    ({"document_submission": "waiting_for_input"}, W.WAITING_FOR_INPUT),
    ({"joining_day_welcome": "waiting"}, W.WAITING_FOR_INPUT),
    ({"orientation_scheduling": "uncertain"}, W.BLOCKED),
    ({"buddy_introduction": "failed"}, W.PENDING),  # an optional stage never decides the workflow status
    ({"intake": "in_progress"}, W.RUNNING),
])
def test_workflow_status_aggregation_policy(statuses, expected):
    state = new_state(case_id="C1", client_id="CL1", correlation_id="c", actor={"on_behalf_of": "u", "roles": []},
                      intake_trigger={})
    state["stage_statuses"].update(statuses)
    assert derive_workflow_status(state) == expected


def test_a_blocked_stage_with_a_technical_code_is_retry_pending_not_a_business_block():
    state = new_state(case_id="C1", client_id="CL1", correlation_id="c", actor={"on_behalf_of": "u", "roles": []},
                      intake_trigger={})
    state["stage_statuses"]["orientation_scheduling"] = "blocked"
    state["stage_errors"]["orientation_scheduling"] = {"code": "UC05_UNAVAILABLE", "detail": "", "retryable": True}
    assert derive_workflow_status(state) == W.RETRY_PENDING
    state["stage_errors"]["orientation_scheduling"]["code"] = "NO_AVAILABLE_SLOT"
    assert derive_workflow_status(state) == W.BLOCKED


# ============================================================ 8. idempotency, resume, notifications


def test_resume_and_retry_never_duplicate_a_side_effect():
    flow = Flow()
    state = flow.to_branches()
    case_id = state["case_id"]
    for _ in range(3):
        state = flow.agent.resume_case(case_id, actor=HR)
    state = flow.start()  # a duplicate offer acceptance resumes the same case
    assert state["case_id"] == case_id
    assert (flow.uc01.calls, flow.plan.calls, flow.uc04.calls, flow.uc03.calls) == (1, 1, 1, 1)
    assert dict(flow.uc05.calls) == {"orientation": 1, "buddy": 1, "team_intro": 1}
    assert flow.notifier.sent_count() == 1
    assert len(flow.ctx.hitl.list(status=Decision.PENDING, gate="day_one_signoff", workflow_instance=case_id,
                                  client_id=CLIENT)) == 1  # one signoff request, not four
    state = flow.approve_signoff(state)
    for _ in range(2):
        state = flow.agent.trigger_joining_day(case_id, as_of=JOINING, actor=SCHEDULER)
    assert state["workflow_status"] == W.COMPLETED.value and flow.uc05.calls["welcome"] == 2  # waiting, then once


def test_an_uncertain_document_request_is_not_resent_until_an_authorized_retry():
    notifier = MockNotificationOutbox(raise_next=1)  # the transport is lost mid-send: the outcome is unknown
    flow = Flow(notifier=notifier)
    state = flow.start()
    case_id, key = state["case_id"], state["document_collection"]["notification"]["idempotency_key"]
    assert status(state, S.DOCUMENT_COLLECTION) == ST.RETRY_PENDING.value
    assert code(state, S.DOCUMENT_COLLECTION) == "DOCUMENT_REQUEST_DELIVERY_UNCERTAIN"
    assert state["document_collection"]["notification"]["status"] == "uncertain"
    assert status(state, S.DOCUMENT_SUBMISSION) == ST.PENDING.value

    state = flow.agent.resume_case(case_id, actor=HR)  # a plain resume never resends an unknown outcome
    assert notifier.attempts_for(key) == 1 and status(state, S.DOCUMENT_COLLECTION) == ST.RETRY_PENDING.value

    state = flow.agent.retry_stage(case_id, "document_collection", actor=HR)  # authorized: same key
    assert status(state, S.DOCUMENT_COLLECTION) == ST.COMPLETE.value
    assert notifier.attempts_for(key) == 2 and notifier.sent_count() == 1
    assert state["document_collection"]["notification"]["idempotency_key"] == key
    assert status(state, S.DOCUMENT_SUBMISSION) == ST.WAITING_FOR_INPUT.value
    assert flow.uc01.calls == 1


def test_a_refused_document_request_is_resent_under_the_same_key_on_resume():
    notifier = MockNotificationOutbox(fail_next=1)  # definitively not delivered
    flow = Flow(notifier=notifier)
    state = flow.start()
    key = state["document_collection"]["notification"]["idempotency_key"]
    assert code(state, S.DOCUMENT_COLLECTION) == "DOCUMENT_REQUEST_DELIVERY_FAILED"
    state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.DOCUMENT_COLLECTION) == ST.COMPLETE.value
    assert notifier.sent_count() == 1 and notifier.attempts_for(key) == 2


def test_a_process_stopping_mid_send_leaves_the_request_uncertain_not_resent(tmp_path):
    """Write-ahead: the request is recorded as `sending` before the send. A new orchestrator over the
    same SQLite store finds `sending` and does not send again."""
    store = SqliteCaseStateStore(tmp_path / "case_states.db")

    class _CrashingOutbox(MockNotificationOutbox):
        def send(self, message):
            raise SystemExit("process killed mid-send (simulated)")

    first = Flow(case_states=store, notifier=_CrashingOutbox())
    with pytest.raises(SystemExit):
        first.start()
    case_id = first.agent.get_case(first.agent.case_id_for(first.case_input()), client_id=CLIENT)["case_id"]
    assert store[case_id]["document_collection"]["notification"]["status"] == "sending"

    restarted = Flow(case_states=SqliteCaseStateStore(tmp_path / "case_states.db"))
    state = restarted.agent.resume_case(case_id, actor=HR)
    assert code(state, S.DOCUMENT_COLLECTION) == "DOCUMENT_REQUEST_DELIVERY_UNCERTAIN"
    assert restarted.notifier.sent_count() == 0


def test_a_restarted_orchestrator_resumes_the_wait_at_validation(tmp_path):
    flow = Flow(case_states=SqliteCaseStateStore(tmp_path / "case_states.db"))
    state = flow.start()
    case_id, outstanding = state["case_id"], flow.outstanding(state)

    restarted = Flow(case_states=SqliteCaseStateStore(tmp_path / "case_states.db"))  # a new process, same store
    loaded = restarted.agent.get_case(case_id, client_id=CLIENT)
    assert restarted.outstanding(loaded) == outstanding
    state = restarted.supply(loaded)
    assert all(status(state, s) == ST.COMPLETE.value for s in POST_VERIFICATION_STAGES)
    assert restarted.uc01.calls == 0 and restarted.notifier.sent_count() == 0  # nothing before the wait re-ran
    validate_workflow_state(restarted.stored(case_id))


# ============================================================ 9. tenancy, authorization, privacy


def test_another_tenant_can_neither_read_nor_change_a_case():
    flow = Flow()
    state = flow.start()
    case_id, before = state["case_id"], flow.stored(state["case_id"])
    with pytest.raises(KeyError):
        flow.agent.get_case(case_id, client_id=OTHER_TENANT_HR["client_id"])
    with pytest.raises(KeyError):
        flow.agent.submit_documents(case_id, documents=[synthetic_document("passport")], actor=OTHER_TENANT_HR)
    with pytest.raises(KeyError):
        flow.agent.retry_stage(case_id, "document_collection", actor=OTHER_TENANT_HR)
    with pytest.raises(KeyError):
        flow.agent.review_documents(case_id, reviews=[{"document_type": "passport", "decision": "approved"}],
                                    actor=OTHER_TENANT_HR)
    with pytest.raises(KeyError):
        flow.agent.resume_case(case_id, actor=OTHER_TENANT_HR)
    assert flow.stored(case_id) == before


def test_unauthorized_callers_change_nothing_and_consume_no_approval():
    flow = Flow()
    flow.uc03.verdicts = {"passport": "hr_review_required"}
    state = flow.to_branches()
    case_id, before = state["case_id"], flow.stored(state["case_id"])
    with pytest.raises(OrchestratorAuthorizationError):  # hr_ops may read, not supply documents
        flow.agent.submit_documents(case_id, documents=[synthetic_document("passport", tag="2")], actor=HR_OPS)
    with pytest.raises(OrchestratorAuthorizationError):  # an officer may supply documents, not review them
        flow.agent.review_documents(case_id, reviews=[{"document_type": "passport", "decision": "approved"}],
                                    actor=OFFICER)
    with pytest.raises(OrchestratorAuthorizationError):
        flow.agent.retry_stage(case_id, "validate_documents", actor=HR_OPS)
    with pytest.raises(OrchestratorAuthorizationError):
        flow.agent.resume_case(case_id, actor=HR_OPS)
    assert flow.stored(case_id) == before and flow.uc03.calls == 1

    flow.uc03.verdicts = {}
    state = flow.agent.review_documents(case_id, reviews=[{"document_type": "passport", "decision": "approved"}],
                                        actor=HR)
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value
    (item,) = flow.ctx.hitl.list(status=Decision.PENDING, gate="day_one_signoff", workflow_instance=case_id,
                                 client_id=CLIENT)
    before = flow.stored(case_id)
    with pytest.raises(HITLError):  # the signoff needs hr_admin
        flow.agent.decide_approval(case_id, item.id, decision="approved", actor=OFFICER)
    assert flow.ctx.hitl.get(item.id, client_id=CLIENT).status == Decision.PENDING  # not consumed
    after = flow.stored(case_id)
    unchanged = {k: v for k, v in before.items() if k != "updated_at"}
    assert {k: v for k, v in after.items() if k != "updated_at"} == unchanged


def test_documents_this_case_does_not_require_or_contents_are_refused():
    flow = Flow()
    state = flow.start()
    with pytest.raises(WorkflowRequestError):
        flow.agent.submit_documents(state["case_id"], documents=[synthetic_document("tax_return")], actor=HR)
    with pytest.raises(ValidationError):  # no field for document contents
        flow.agent.submit_documents(state["case_id"], documents=[{**synthetic_document("passport"),
                                                                  "content": "JVBERi0xLjQK"}], actor=HR)
    assert flow.stored(state["case_id"])["submitted_documents"] == {}


def test_documents_cannot_be_submitted_before_collection_or_after_verification():
    flow = Flow()
    held = flow.start(offer_accepted=False)  # UC01 holds the case: no collection was initiated
    with pytest.raises(PrerequisiteNotMetError):
        flow.agent.submit_documents(held["case_id"], documents=[synthetic_document("passport")], actor=HR)
    flow = Flow()
    state = flow.to_branches()
    with pytest.raises(PrerequisiteNotMetError):  # verified documents are not silently re-opened
        flow.agent.submit_documents(state["case_id"], documents=[synthetic_document("passport", tag="2")], actor=HR)
    assert flow.stored(state["case_id"])["submitted_documents"]["passport"]["revision"] == 1


def test_document_metadata_never_reaches_logs_audit_or_responses():
    from fastapi.testclient import TestClient

    from hr_onboarding_common.observability.logging import JsonFormatter
    from hr_onboarding_common.orchestration.api import create_orchestrator_app
    from hr_onboarding_common.platform.identity import InsecureHeaderIdentityProvider

    sentinel = "Zyx Sentinel-Person"
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = _Capture(level=logging.DEBUG)
    handler.setFormatter(JsonFormatter("test"))
    dact = logging.getLogger("dact")
    previous_level = dact.level
    dact.addHandler(handler)
    dact.setLevel(logging.DEBUG)
    try:
        flow = Flow()
        flow.uc03.verdicts = {"passport": "correction_required"}
        client = TestClient(create_orchestrator_app(agent=flow.agent,
                                                    identity_provider=InsecureHeaderIdentityProvider(enabled=True)))
        headers = {"X-User-Id": HR["on_behalf_of"], "X-Client-Id": CLIENT, "X-User-Roles": "hr_admin"}
        state = flow.start()
        body = {"documents": [synthetic_document(t, extracted_name=sentinel) for t in flow.outstanding(state)]}
        response = client.post(f"/v1/orchestration/cases/{state['case_id']}/documents", json=body, headers=headers)
        assert response.status_code == 200, response.text
        read = client.get(f"/v1/orchestration/cases/{state['case_id']}", headers=headers)
        actions = client.get(f"/v1/orchestration/cases/{state['case_id']}/pending-actions", headers=headers)
    finally:
        dact.removeHandler(handler)
        dact.setLevel(previous_level)
    assert any('"stage": "validate_documents"' in r for r in records)  # stage logs were captured
    for text in (*records, response.text, read.text, actions.text, json.dumps(flow.ctx.audit.events)):
        assert sentinel not in text and "2031-01-01" not in text
        assert CANDIDATE not in text and "alex.morgan@example.com" not in text
    assert sentinel in json.dumps(flow.stored(state["case_id"])["submitted_documents"])  # kept for UC03 only


def test_state_and_api_carry_no_nda_or_signature_tracking():
    state = Flow().to_branches()
    names = " ".join(state) + " " + " ".join(state["stage_statuses"]) + " " + " ".join(state["stage_outcomes"])
    for term in ("nda_signed", "signature", "sign_nda"):
        assert term not in names


# ============================================================ 10. the real UC03 (in-process)


def test_real_uc03_verifies_only_after_an_hr_review_and_then_fans_out():
    """UC03's own contract: a valid document is UNDER_REVIEW until the caller supplies an HR
    approval; never ACCEPTED. Its HR review is an authenticated reviewer's decision here."""
    require_uc("uc03")
    flow = Flow(real_uc03=True)
    state = flow.to_branches()
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.AWAITING_APPROVAL.value
    result = state["document_validation_result"]["passport"]
    assert (result["document_status"], result["compliance_item_status"], result["required_next_action"]) == \
        ("UNDER_REVIEW", "PENDING_APPROVAL", "HUMAN_CONFIRMATION")
    assert flow.plan.calls == 1 and flow.uc04.calls == 0  # planned before the documents; no branch yet

    reviews = [{"document_type": t, "decision": "approved"} for t in state["submitted_documents"]]
    state = flow.agent.review_documents(state["case_id"], reviews=reviews, actor=HR)
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.COMPLETE.value
    assert {r["compliance_item_status"] for r in state["document_validation_result"].values()} == {"COMPLETE"}
    assert all(status(state, s) == ST.COMPLETE.value for s in POST_VERIFICATION_STAGES)
    assert state["submitted_documents"]["passport"]["hr_review"]["reviewer"] == HR["on_behalf_of"]


def test_real_uc03_expired_document_is_corrected_and_the_new_revision_is_revalidated():
    """UC03 tracks document revisions: a corrected revision is validated again with UC03's content
    rules (no longer a DUPLICATE_REQUEST); being valid, it then needs the HR review UC03 requires."""
    require_uc("uc03")
    flow = Flow(real_uc03=True)
    state = flow.start()
    documents = [synthetic_document(t) for t in flow.outstanding(state)]
    documents = [d if d["document_type"] != "passport" else synthetic_document("passport", expiry_date="2020-01-01")
                 for d in documents]
    state = flow.agent.submit_documents(state["case_id"], documents=documents, actor=HR)
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.WAITING_FOR_INPUT.value
    (passport,) = [r for r in state["correction_requirements"] if r["document_type"] == "passport"]
    assert passport["action"] == "resubmit" and "EXPIRED_DOCUMENT" in passport["reason_codes"]

    state = flow.supply(state, ["passport"], tag="2")
    result = state["document_validation_result"]["passport"]
    assert result["revision"] == 2 and "DUPLICATE_REQUEST" not in result["findings"]
    assert "EXPIRED_DOCUMENT" not in result["findings"]  # the content rules ran on the new revision
    assert result["classification"] == "hr_review_required"
    state = flow.agent.review_documents(state["case_id"], reviews=[
        {"document_type": t, "decision": "approved"} for t in state["submitted_documents"]], actor=HR)
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.COMPLETE.value
    assert flow.uc01.calls == 1 and flow.notifier.sent_count() == 1


def test_an_hr_rejection_requires_a_new_revision():
    require_uc("uc03")
    flow = Flow(real_uc03=True)
    state = flow.to_branches()
    state = flow.agent.review_documents(state["case_id"], reviews=[{"document_type": "passport", "decision": "rejected",
                                                                   "note": "photo page cropped"}], actor=HR)
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.WAITING_FOR_INPUT.value
    (passport,) = [r for r in state["correction_requirements"] if r["document_type"] == "passport"]
    assert passport["action"] == "resubmit" and passport["reason_codes"] == ["HR_REVIEW_REJECTED"]
    with pytest.raises(PrerequisiteNotMetError):  # the rejected revision cannot be approved afterwards
        flow.agent.review_documents(state["case_id"], reviews=[{"document_type": "passport", "decision": "approved"}],
                                    actor=HR)


# ============================================================ 11. back-compatibility


def test_a_case_persisted_by_the_sequential_version_is_not_sent_a_second_request():
    from hr_onboarding_common.orchestration.state import ensure_lifecycle_fields

    old = new_state(case_id="C1", client_id="CL1", correlation_id="c", actor={"on_behalf_of": "u", "roles": []},
                    intake_trigger={})
    for key in ("document_collection", "submitted_documents", "workflow_status", "stage_outcomes", "branch_outcomes"):
        old.pop(key)
    for stage in (Stage.DOCUMENT_COLLECTION, Stage.DOCUMENT_SUBMISSION):
        old["stage_statuses"].pop(stage.value)
    old["stage_statuses"]["validate_documents"] = "complete"
    filled = ensure_lifecycle_fields(old)
    for stage in ("document_collection", "document_submission"):
        assert filled["stage_statuses"][stage] == "complete"
    assert filled["workflow_status"] == "pending" and filled["submitted_documents"] == {}
