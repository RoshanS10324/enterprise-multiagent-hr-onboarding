"""Integration tests against the fully-wired orchestrator (real UC01/UC04/UC05 workers,
in-process UC02 and UC03). No live LLM, no live Teams/Outlook/calendar/employee-directory --
FakeLLM/mocks throughout.

Covers the required scenarios as far as each UC's real, currently-working state allows, in the revised
order UC01 -> UC02 (initial planning) -> UC03 (document collection, storage, validation) -> UC04 || UC05.
The real UC04 still refuses UC01's non-SYN case ids (its documented GR-13 gap) and the real UC05 has no
offer-acceptance event for these cases; both are reported as their own outcomes. The day-1 signoff
scenario short-circuits earlier stages through the case store, explicitly (never silently).
"""

from __future__ import annotations

import pytest

from _helpers import UC_ROOTS
from hr_onboarding_common.orchestration.contracts import OnboardingCaseInput
from hr_onboarding_common.orchestration.state import Stage, StageStatus


def _input(**overrides):
    base = dict(
        client_id="CLIENT-ALPHA", hr_officer_id="user:hr-admin-01", offer_accepted=True,
        offer_reference="OFR-EMP-900", candidate_name="Arun Kumar", candidate_email="arun.kumar@example.com",
        designation="Backend Engineer", joining_date="2026-11-16", work_location_type="OFFICE",
        id_number="ID-900", role_id="backend_engineer", department="Technology", manager_id="MGR-001",
        location="Dubai", project="apollo",
        # CLIENT-ALPHA identifies employees by their HRMS number (config/tenants.yaml, scheme none): HR enters it.
        employee_id="HRMS-000900",
        # What UC01 verified when it opened the case: UC02's own start condition needs them to plan.
        offer_accepted_at="2026-10-20T10:00:00Z", form_approved_at="2026-10-21T10:00:00Z",
        form_approved_by_role="hr_ops",
    )
    base.update(overrides)
    return OnboardingCaseInput(**base)


def _actor():
    return {"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": "CLIENT-ALPHA"}


# --------------------------------------------------------------------- 1. real agents, Stage 0-1


def test_intake_and_planning_complete_and_the_case_waits_for_documents_before_any_branch(orchestrator):
    """1. "Happy path" as far as the case can go without documents: intake succeeds against the real UC01,
    the real UC02 plans (with the plan's document requirements), UC03 opens the collection for that plan
    version and the (mock) document request goes out; the case waits -- nothing after the wait has run,
    and the wait is not reported as a completion."""
    state = orchestrator.advance_case(_input(), case_id=None, actor=_actor())
    for stage in (Stage.INTAKE, Stage.PLAN_TASKS, Stage.DOCUMENT_COLLECTION):
        assert state["stage_statuses"][stage.value] == StageStatus.COMPLETE.value, state.get("error_detail")
    assert state["stage_statuses"][Stage.DOCUMENT_SUBMISSION.value] == StageStatus.WAITING_FOR_INPUT.value
    assert state["document_collection"]["notification"]["synthetic"] is True
    assert state["document_collection"]["plan_version"] == state["onboarding_plan"]["plan_version"] == 1
    for stage in (Stage.VALIDATE_DOCUMENTS, Stage.EQUIPMENT_ACCESS, Stage.ORIENTATION_SCHEDULING):
        assert state["stage_statuses"][stage.value] == StageStatus.PENDING.value
    assert state["workflow_status"] == "waiting_for_input" and state["overall_status"] != "complete"


def test_real_agents_plan_then_verify_documents_then_dispatch_both_branches_and_a_failed_branch_is_never_complete(
        orchestrator):
    """The real UC01 worker, UC02 and UC03 in-process, then the real UC04 and UC05 workers.

    Workflow order (docs/STATE_DRIVEN_ONBOARDING_WORKFLOW.md): UC02 plans right after intake, UC03
    collects and HR verifies the plan's documents, and only then are UC04 and UC05 dispatched together.
    (The missing-approval-timestamps scenario this test used to cover, from the old order in which
    UC02 ran after UC03, is test_uc02_real_orchestration::test_missing_offer_and_form_approval_facts_*.)

    Real-agent finding kept visible here: UC04's contract accepts synthetic ids only
    (`^SYN-CASE-\d{3,}$`) while UC01 now issues UUID case ids, so the real UC04 rejects the input. The
    orchestrator must record that as a FAILED branch -- never complete, never ready -- and must not
    evaluate readiness on it. UC05 is given no offer-acceptance event here, so it blocks for HR."""
    inp = _input(offer_reference="OFR-EMP-906", candidate_email="fan.out@example.com",
                 uc04_client_config_ref=str(UC_ROOTS["uc04"] / "data" / "client_alpha_config.json"))
    state = orchestrator.advance_case(inp, case_id=None, actor=_actor())
    case_id = state["case_id"]
    assert state["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value
    documents = [{"document_type": o["document_type"], "document_ref": f"SYN-DOC-{o['document_type']}",
                  "metadata": {"readable": True, "required_fields_present": True, "expiry_date": "2031-01-01",
                               "evidence_stored": True, "extracted_name": "Arun Kumar", "attestation_confirmed": True}}
                 for o in state["document_collection"]["outstanding"]]
    state = orchestrator.submit_documents(case_id, documents=documents, actor=_actor())
    # Real UC03: valid documents still need an HR reviewer's decision before UC03 verifies them.
    assert state["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] == StageStatus.AWAITING_APPROVAL.value
    assert Stage.EQUIPMENT_ACCESS.value not in state["stage_outcomes"]  # no branch before verification
    state = orchestrator.review_documents(case_id, reviews=[{"document_type": d["document_type"], "decision": "approved"}
                                                            for d in documents], actor=_actor())
    assert state["stage_statuses"][Stage.VALIDATE_DOCUMENTS.value] == StageStatus.COMPLETE.value
    assert state["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.COMPLETE.value
    assert state["stage_statuses"][Stage.REPLAN_TASKS.value] == StageStatus.NOT_REQUESTED.value
    # Both branches were dispatched after the document gate (neither is still PENDING).
    # UC04's SYN-id contract gap (its PLATFORM identity profile not enabled) is a named, resumable block.
    assert state["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.BLOCKED.value
    assert state["stage_errors"][Stage.EQUIPMENT_ACCESS.value]["code"] == "UC04_IDENTITY_PROFILE_NOT_ENABLED"
    assert state["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] == StageStatus.BLOCKED.value
    # A blocked branch is never folded into a completion: no readiness evaluation, no signoff.
    assert state["branch_statuses"]["uc04_equipment_access"]["status"] == "blocked"
    assert state["branch_statuses"]["uc05_orientation"]["status"] == "blocked"
    assert state["workflow_status"] == "blocked"
    assert state["stage_statuses"][Stage.DAY_ONE_READINESS.value] == StageStatus.PENDING.value
    assert state["stage_statuses"][Stage.JOINING_DAY_WELCOME.value] == StageStatus.PENDING.value


def test_duplicate_offer_acceptance_links_existing_case_not_a_new_one(orchestrator):
    """2. Calling intake twice with the same offer/candidate links to the SAME case (UC01's own
    duplicate-check), never creating a second one."""
    inp = _input(offer_reference="OFR-EMP-901", candidate_email="dup.candidate@example.com")
    first = orchestrator.advance_case(inp, case_id=None, actor=_actor())
    second = orchestrator.advance_case(inp, case_id=None, actor=_actor())
    assert first["case_id"] == second["case_id"]


def test_unverified_acceptance_pauses_the_workflow(orchestrator):
    """3. offer_accepted=False must hold the case at intake, never proceed."""
    inp = _input(offer_accepted=False, offer_reference="OFR-EMP-902")
    state = orchestrator.advance_case(inp, case_id=None, actor=_actor())
    assert state["stage_statuses"][Stage.INTAKE.value] == StageStatus.BLOCKED.value
    assert state["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.PENDING.value


def test_unapproved_form_cannot_trigger_downstream_processing(orchestrator):
    """4. No designation/joining_date -> no form is built -> UC01 holds the case; plan_tasks
    never runs."""
    inp = _input(designation=None, joining_date=None, offer_reference="OFR-EMP-903")
    state = orchestrator.advance_case(inp, case_id=None, actor=_actor())
    assert state["stage_statuses"][Stage.INTAKE.value] == StageStatus.BLOCKED.value
    assert state["stage_statuses"][Stage.PLAN_TASKS.value] == StageStatus.PENDING.value


# --------------------------------------------------------------------- identity / tenant


def test_unauthorized_actor_blocks_intake_before_uc01_is_ever_called(orchestrator):
    """13 (cross-tenant/unauthorized family). An actor without an allowed intake role is
    rejected by the orchestrator's OWN boundary check -- UC01 is never touched."""
    inp = _input(offer_reference="OFR-EMP-904")
    bad_actor = {"on_behalf_of": "user:random-01", "roles": ["random_unaffiliated_role"], "client_id": "CLIENT-ALPHA"}
    state = orchestrator.advance_case(inp, case_id=None, actor=bad_actor)
    assert state["stage_statuses"][Stage.INTAKE.value] == StageStatus.BLOCKED.value
    assert state["error_code"] == "POLICY_BREACH"


# --------------------------------------------------------------------- 14/15. Q&A preserved, NDA not reintroduced


def test_uc05_policy_qa_still_works_independently_of_orchestration(uc05_worker):
    """14. UC05's existing HR Policies Q&A keeps working -- exercised directly through UC05's
    own `run_event(POLICY_QUESTION)`, which this orchestrator's adapter does not need to touch
    or modify to preserve."""
    from hr_onboarding_common.orchestration.contracts import Actor

    actor = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], client_id="CLIENT-TEST-A")
    response = uc05_worker.call(
        "run_event",
        {"event": {"event": "policy_question", "question": "What is the probation period?",
                   "as_of": "2026-10-28"}, "instance": "EMP-TEST-001"},
        actor=actor,
    )
    assert response.ok
    assert response.result["workflow"]["stage"] in ("policy_answered", "policy_no_answer")


def test_nda_or_signature_tracking_not_reintroduced():
    """15. No NDA/acknowledgement-signature field or route exists anywhere in the new state
    model or orchestrator module -- this scope decision is preserved, not silently restored."""
    import inspect

    from hr_onboarding_common.orchestration import state as state_module
    source = inspect.getsource(state_module)
    for forbidden in ("acknowledgement", "nda_signed", "signature_status"):
        assert forbidden not in source.lower()


# --------------------------------------------------------------------- 16. existing suites untouched


# --------------------------------------------------------------------- day-1 signoff: approval + resume


def test_day_one_signoff_requires_hr_approval_and_resumes_correct_case(orchestrator):
    """5/8/9/10/11. Verifies the orchestrator's OWN day-1 gate: readiness is never auto-completed
    just because every stage reports COMPLETE -- it raises `day_one_signoff` and waits; approving
    it resumes the SAME case/thread_id from its checkpoint to COMPLETE, never a new case."""
    from hr_onboarding_common.orchestration.state import Stage, StageStatus

    inp = _input(offer_reference="OFR-EMP-905", candidate_email="dayone@example.com")
    state = orchestrator.advance_case(inp, case_id=None, actor=_actor())
    case_id = state["case_id"]

    # Short-circuit stages 1-4 to COMPLETE via the SAME last-known-state handoff a real resume
    # uses -- isolates the day-1 readiness/signoff logic, which is the orchestrator's OWN
    # responsibility, from needing to satisfy UC03's full multi-document checklist end-to-end
    # (already covered by the UC03 adapter's own focused unit tests).
    # Readiness now also requires UC05's buddy assignment and team introduction (the UC05
    # lifecycle integration); they are short-circuited the same way as the other stages here.
    cached = orchestrator.deps.case_states[case_id]
    cached["stage_statuses"].update({
        Stage.DOCUMENT_SUBMISSION.value: StageStatus.COMPLETE.value,
        Stage.PLAN_TASKS.value: StageStatus.COMPLETE.value,
        Stage.VALIDATE_DOCUMENTS.value: StageStatus.COMPLETE.value,
        Stage.EQUIPMENT_ACCESS.value: StageStatus.COMPLETE.value,
        Stage.ORIENTATION_SCHEDULING.value: StageStatus.COMPLETE.value,
        Stage.BUDDY_ASSIGNMENT.value: StageStatus.COMPLETE.value,
        Stage.TEAM_INTRODUCTION.value: StageStatus.COMPLETE.value,
    })
    resumed = orchestrator.resume_case(case_id)
    assert resumed["case_id"] == case_id
    assert resumed["stage_statuses"][Stage.DAY_ONE_READINESS.value] == StageStatus.AWAITING_APPROVAL.value
    assert resumed["overall_status"] != "complete"

    pending = orchestrator.ctx.hitl.list(gate="day_one_signoff", workflow_instance=case_id, client_id="CLIENT-ALPHA")
    assert len(pending) == 1

    final = orchestrator.decide_gate(case_id, "day_one_signoff", decision="approved",
                                      approver="user:hr-admin-02", approver_roles=["hr_admin"],
                                      client_id="CLIENT-ALPHA")
    assert final["case_id"] == case_id  # resumed the SAME case, not a new one
    assert final["stage_statuses"][Stage.DAY_ONE_READINESS.value] == StageStatus.COMPLETE.value
    # Signoff no longer closes the case: the joining-day welcome (UC05) follows it, and nothing
    # happens until the joining-day event arrives -- a normal wait, not a block.
    assert final["stage_statuses"][Stage.JOINING_DAY_WELCOME.value] == StageStatus.WAITING.value
    assert final["overall_status"] == "in_progress"
    assert final["stage_statuses"].get(Stage.COMPLETE.value) != StageStatus.COMPLETE.value


def test_uc_standalone_entry_points_remain_importable_and_callable(uc01_worker, uc04_worker, uc05_worker):
    """16 (partial, structural check -- full suite runs are reported separately per UC's own
    test command). Each UC's real public entry point is still reachable exactly as documented,
    proving this orchestrator did not have to route around a broken standalone API."""
    for worker in (uc01_worker, uc04_worker, uc05_worker):
        result = worker._raw_call({"contract_version": "1.0", "op": "__identity_check__", "run_id": "r",
                                    "idempotency_key": "r", "actor": {"on_behalf_of": "s"}, "payload": {}})
        assert result["ok"] and result["result"]["under_configured_src_dir"]
