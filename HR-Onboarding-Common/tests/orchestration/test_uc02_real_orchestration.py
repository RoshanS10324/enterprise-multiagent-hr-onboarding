"""UC-02 through the REAL main orchestrator: real UC-01/UC-04/UC-05 workers, real UC-03 in-process,
real UC-02 adapter and package. UC-02 plans right after UC-01 intake -- before any document -- and its
plan's versioned document requirements are what UC-03 collects and validates.

What is real here: the orchestrator graph, the three subprocess workers, UC-03's checklist and
validator, the UC-02 adapter and service. What is not: no live LLM, mail, calendar or directory; the
document-request transport is the orchestrator's mock outbox; documents are synthetic.

Not changed by this file: other use cases' behaviour. UC-04 still refuses UC-01's case id (its
documented contract gap), so the equipment branch fails for that reason; these tests only assert
that this does not affect UC-02.
"""

from __future__ import annotations

from _helpers import UC_ROOTS  # noqa: F401  (also puts the shared helpers on sys.path)
from hr_onboarding_common.orchestration.contracts import OnboardingCaseInput
from hr_onboarding_common.orchestration.state import Stage, StageStatus

APPROVAL_FACTS = dict(
    offer_accepted_at="2026-10-20T10:00:00Z",
    form_approved_at="2026-10-21T10:00:00Z",
    form_approved_by_role="hr_ops",
)


def _input(**overrides):
    base = dict(
        client_id="CLIENT-ALPHA", hr_officer_id="user:hr-admin-01", offer_accepted=True,
        offer_reference="OFR-EMP-910", candidate_name="Arun Kumar", candidate_email="uc02.real@example.com",
        designation="Backend Engineer", joining_date="2026-11-16", work_location_type="OFFICE",
        id_number="ID-910", role_id="backend_engineer", department="Technology", manager_id="MGR-001",
        location="Dubai", project="apollo",
        # CLIENT-ALPHA identifies employees by their HRMS number (config/tenants.yaml, scheme none): HR enters it.
        employee_id="HRMS-000910",
    )
    base.update(overrides)
    return OnboardingCaseInput(**base)


def _actor():
    return {"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": "CLIENT-ALPHA"}


def _documents(state):
    return [{"document_type": o["document_type"], "document_ref": f"SYN-DOC-{o['document_type']}",
             "metadata": {"readable": True, "required_fields_present": True, "expiry_date": "2031-01-01",
                          "evidence_stored": True, "extracted_name": "Arun Kumar", "attestation_confirmed": True}}
            for o in state["document_collection"]["outstanding"]]


def _verified_from(orchestrator, state):
    """Synthetic documents for what UC03 asks -> UC-03 validation + HR review."""
    case_id = state["case_id"]
    documents = _documents(state)
    state = orchestrator.submit_documents(case_id, documents=documents, actor=_actor())
    state = orchestrator.review_documents(
        case_id, reviews=[{"document_type": d["document_type"], "decision": "approved"} for d in documents],
        actor=_actor())
    assert state["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] == StageStatus.COMPLETE.value
    return state


def _verified(orchestrator, **overrides):
    """UC-01 intake -> document request -> synthetic documents -> UC-03 validation + HR review."""
    state = orchestrator.advance_case(_input(**overrides), case_id=None, actor=_actor())
    case_id = state["case_id"]
    documents = _documents(state)
    state = orchestrator.submit_documents(case_id, documents=documents, actor=_actor())
    state = orchestrator.review_documents(
        case_id, reviews=[{"document_type": d["document_type"], "decision": "approved"} for d in documents],
        actor=_actor())
    assert state["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] == StageStatus.COMPLETE.value
    return state


def test_uc02_plans_tasks_right_after_intake_and_the_plan_drives_uc03s_collection(orchestrator):
    state = orchestrator.advance_case(_input(**APPROVAL_FACTS), case_id=None, actor=_actor())
    # UC01 -> UC02: planned before any document exists, with the shared identifiers.
    assert state["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value, state.get("error_detail")
    plan = state["task_plan"]
    assert plan["created"] is True and len(plan["tasks"]) > 0 and len(plan["ready"]) > 0
    assert plan["case_id"] == state["case_id"] and plan["correlation_id"] == state["correlation_id"]
    assert plan["onboarding_id"].startswith("ONB-CLIENT-ALPHA-")
    # UC02 -> UC03: the plan's versioned document requirements are exactly what UC03 collects.
    onboarding_plan = state["onboarding_plan"]
    assert (onboarding_plan["plan_id"], onboarding_plan["plan_version"], onboarding_plan["source"]) ==         (plan["onboarding_id"], 1, "uc02")
    collection = state["document_collection"]
    assert (collection["plan_id"], collection["plan_version"]) == (plan["onboarding_id"], 1)
    assert [d["document_type"] for d in collection["required_documents"]] ==         [d["document_type"] for d in onboarding_plan["required_documents"]]
    # UC02 itself holds the same requirements on its plan (the real OnboardingService).
    from onboarding_task_management.contracts import Actor, Role

    record = orchestrator.deps._uc02_sub_agent.service.get(
        Actor(actor_id="user:hr-admin-01", role=Role.HR_OPS, client_id="CLIENT-ALPHA"), plan["onboarding_id"])
    assert [d.key for d in record.documents] == [d["document_type"] for d in onboarding_plan["required_documents"]]
    assert record.plan_version == 1

    state = _verified_from(orchestrator, state)
    assert state["stage_outcomes"][Stage.PLAN_TASKS.value]["attempt"] == 1  # never re-planned after verification
    assert state["stage_outcomes"][Stage.EQUIPMENT_ACCESS.value]["attempt"] == 1
    assert state["stage_outcomes"][Stage.ORIENTATION_SCHEDULING.value]["attempt"] == 1
    assert state["verified_context"]["plan_version"] == 1


def test_uc02_never_collects_or_validates_documents(orchestrator):
    state = orchestrator.advance_case(_input(**APPROVAL_FACTS), case_id=None, actor=_actor())
    state = orchestrator.submit_documents(state["case_id"], documents=_documents(state), actor=_actor())
    # UC03 validated (and holds the revisions); UC02's record of the documents is untouched.
    assert state["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] == StageStatus.AWAITING_APPROVAL.value
    from onboarding_task_management.contracts import Actor, DocumentState, Role

    record = orchestrator.deps._uc02_sub_agent.service.get(
        Actor(actor_id="user:hr-admin-01", role=Role.HR_OPS, client_id="CLIENT-ALPHA"),
        state["task_plan"]["onboarding_id"])
    assert {d.state for d in record.documents} == {DocumentState.PENDING}
    stored = orchestrator.deps._uc03_collection_service.latest(state["case_id"])
    assert set(stored) == set(state["submitted_documents"])  # UC03 stored every revision
    assert state["stage_outcomes"][Stage.PLAN_TASKS.value]["attempt"] == 1


def test_missing_offer_and_form_approval_facts_block_planning_and_everything_after_it(orchestrator):
    """The adapter never assumes approvals: with no approval timestamps in the intake, UC-02's own
    start-condition check refuses, the stage is BLOCKED with MISSING_DATA (not a fabricated success),
    nothing was created -- and, UC02 being first after intake, UC03 collects nothing and no branch runs."""
    state = orchestrator.advance_case(_input(), case_id=None, actor=_actor())  # no approval facts supplied
    assert state["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.BLOCKED.value
    error = state["stage_errors"][Stage.PLAN_TASKS.value]
    assert error["code"] == "MISSING_DATA" and "offer acceptance has no timestamp" in error["detail"]
    assert state["task_plan"] is None and state["onboarding_plan"] is None
    for stage in (Stage.DOCUMENT_COLLECTION, Stage.EQUIPMENT_ACCESS, Stage.ORIENTATION_SCHEDULING):
        assert state["stage_statuses"][stage.value] == StageStatus.PENDING.value
        assert stage.value not in state["stage_outcomes"]


def test_an_unaccepted_offer_never_reaches_uc02(orchestrator):
    state = orchestrator.advance_case(_input(offer_accepted=False, **APPROVAL_FACTS), case_id=None, actor=_actor())
    assert state["stage_statuses"][Stage.INTAKE.value] != StageStatus.COMPLETE.value
    assert state["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.PENDING.value
    assert state.get("task_plan") is None


def test_a_form_approved_through_the_gate_is_accepted_by_the_real_uc02(orchestrator):
    """A person's internal onboarding form (no approval facts) waits for the onboarding_form_approval gate. Once
    an authorized approver who is not the requester decides it, the real UC01 opens the case from the approved
    form and the real UC02 plans from the decision's facts. Regression: with an approver role UC02 does not accept
    (hr_manager), UC02 refused to plan with MISSING_DATA 'not approved by an HR role'."""
    from hr_onboarding_common.platform.hitl import Decision

    state = orchestrator.advance_case(_input(offer_accepted_at="2026-10-20T10:00:00Z", form_approval_required=True),
                                      case_id=None, actor=_actor())
    assert state["stage_statuses"][Stage.INTAKE.value] == StageStatus.AWAITING_APPROVAL.value
    (item,) = orchestrator.ctx.hitl.list(status=Decision.PENDING, gate="onboarding_form_approval",
                                         client_id="CLIENT-ALPHA")
    lead = {"on_behalf_of": "user:hr-lead-01", "roles": [item.approver_role], "client_id": "CLIENT-ALPHA"}
    state = orchestrator.decide_approval(state["case_id"], item.id, decision="approved", actor=lead)
    assert state["stage_statuses"][Stage.INTAKE.value] == StageStatus.COMPLETE.value
    assert state["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value, state["stage_errors"]
    assert state["uc02_role_context"]["approved_by_role"] == item.approver_role
