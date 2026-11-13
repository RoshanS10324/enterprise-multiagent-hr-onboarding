"""State synchronization (orchestration/state_sync.py): field ownership, validated updates, identity and
tenant conflicts, provenance, the completed-stage guard on write-through, and durability of the new
bookkeeping across a restart. Unit tests on synchronize(); graph tests use the test world of
_workflow.py (real graph, fakes/doubles in front of the UCs -- see that module)."""

from __future__ import annotations

import copy

import pytest
from _workflow import CLIENT, HR, JOINING, Flow, code, status

from hr_onboarding_common.orchestration.adapters._shared import with_stage_status
from hr_onboarding_common.orchestration.agent_contract import StatusTransitionError
from hr_onboarding_common.orchestration.case_store import SqliteCaseStateStore
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state
from hr_onboarding_common.orchestration.state_sync import FIELD_OWNERS, synchronize

S, ST = Stage, StageStatus


def _state(**fields):
    state = new_state(case_id="C1", client_id=CLIENT, correlation_id="corr", intake_trigger={},
                      actor={"on_behalf_of": "u", "roles": ["hr_admin"], "client_id": CLIENT})
    state.update(fields)
    return state


def _sync(before, after, stage):
    return synchronize(before, after, stage, execution_id="EXE-T", at="2026-11-01T00:00:00+00:00")


# --------------------------------------------------------------------------- synchronize()


def test_an_owned_update_is_accepted_and_its_provenance_recorded():
    before = _state()
    after = with_stage_status(before, S.EQUIPMENT_ACCESS, ST.COMPLETE)
    after["equipment_access_result"] = {"overall_status": "READY", "items": []}
    result = _sync(before, after, S.EQUIPMENT_ACCESS)
    assert result.accepted and result.updated_fields == ["equipment_access_result"]
    assert result.state["field_provenance"]["equipment_access_result"] == {
        "stage": "equipment_access", "use_case_id": "UC04", "execution_id": "EXE-T", "at": "2026-11-01T00:00:00+00:00"}


@pytest.mark.parametrize("field, value", [("task_plan", {"tasks": []}), ("submitted_documents", {"x": {}}),
                                          ("day_one_signoff_approved", True), ("actor", {"on_behalf_of": "x"}),
                                          ("verified_context", {"documents_verified": True})])
def test_a_field_written_by_a_stage_that_does_not_own_it_rejects_the_whole_update(field, value):
    before = _state()
    after = with_stage_status(before, S.EQUIPMENT_ACCESS, ST.COMPLETE)
    after["equipment_access_result"] = {"overall_status": "READY"}
    after[field] = value
    result = _sync(before, after, S.EQUIPMENT_ACCESS)
    assert not result.accepted and result.violations[0].code == "STATE_OWNERSHIP_VIOLATION"
    assert result.violations[0].field == field
    assert result.state == before  # nothing of the rejected update is kept


def test_another_stages_status_errors_or_blockers_cannot_be_changed():
    before = _state()
    for mutate in (lambda s: s["stage_statuses"].update(plan_tasks="complete"),
                   lambda s: s["stage_errors"].update(plan_tasks={"code": "X", "detail": "", "retryable": True}),
                   lambda s: s["blockers"].append({"stage": "plan_tasks", "code": "X", "reason": "", "owner": "",
                                                   "next_action": ""})):
        after = copy.deepcopy(before)
        mutate(after)
        result = _sync(before, after, S.EQUIPMENT_ACCESS)
        assert not result.accepted and result.violations[0].code == "STATE_OWNERSHIP_VIOLATION"


def test_the_team_introduction_may_refresh_the_optional_buddy_introduction_it_reads_from_uc05():
    before = _state()
    after = with_stage_status(before, S.TEAM_INTRODUCTION, ST.COMPLETE)
    after["stage_statuses"][S.BUDDY_INTRODUCTION.value] = ST.AWAITING_APPROVAL.value
    after["buddy_introduction_result"] = {"session_id": "s2"}
    assert _sync(before, after, S.TEAM_INTRODUCTION).accepted
    assert not _sync(before, after, S.BUDDY_ASSIGNMENT).accepted


def test_a_known_value_is_never_overwritten_with_nothing():
    before = _state(task_plan={"onboarding_id": "ONB-1"})
    after = with_stage_status(before, S.PLAN_TASKS, ST.BLOCKED)
    after["task_plan"] = None
    result = _sync(before, after, S.PLAN_TASKS)
    assert result.accepted and result.state["task_plan"] == {"onboarding_id": "ONB-1"}
    assert [w.code for w in result.warnings] == ["NULL_OVERWRITE_IGNORED"]


def test_tenant_and_employee_identity_conflicts_are_rejected():
    before = _state(employee_id="EMP-1", orientation_schedule_result={"uc05_case": True},
                    employee_ref={"client_id": CLIENT, "employee_id": "EMP-1", "employee_no": "EMP-1"})
    cases = [
        (S.INTAKE, {"client_id": "CLIENT-TEST-B"}, "TENANT_CONTEXT_CONFLICT"),
        (S.INTAKE, {"employee_id": "EMP-2"}, "IDENTITY_CONFLICT"),
        (S.ORIENTATION_SCHEDULING, {"employee_ref": {"client_id": "CLIENT-TEST-B", "employee_id": "EMP-1"}},
         "TENANT_CONTEXT_CONFLICT"),
        (S.ORIENTATION_SCHEDULING, {"employee_ref": {"client_id": CLIENT, "employee_id": "EMP-9"}},
         "IDENTITY_CONFLICT"),
    ]
    for stage, change, expected in cases:
        result = _sync(before, {**copy.deepcopy(before), **change}, stage)
        assert [v.code for v in result.violations] == [expected], (stage, change)


def test_every_owned_field_has_at_least_one_owner():
    assert all(owners for owners in FIELD_OWNERS.values())


# ----------------------------------------------------------------------------- in the graph


def test_a_stage_that_writes_outside_its_ownership_is_failed_and_its_update_discarded():
    flow = Flow()

    def rogue_uc04(state, deps):  # a defective adapter: completes, and also rewrites UC02's task plan
        new = with_stage_status(state, S.EQUIPMENT_ACCESS, ST.COMPLETE)
        new["equipment_access_result"] = {"overall_status": "READY", "items": []}
        new["task_plan"] = {"onboarding_id": "FORGED"}
        return new

    flow.deps.stage_adapters[S.EQUIPMENT_ACCESS.value] = rogue_uc04
    flow.agent.graph = flow.agent.build_graph()  # stage adapter overrides are bound at build time
    state = flow.to_branches()
    assert status(state, S.EQUIPMENT_ACCESS) == ST.FAILED.value
    assert code(state, S.EQUIPMENT_ACCESS) == "STATE_OWNERSHIP_VIOLATION"
    assert state["task_plan"]["onboarding_id"] == "ONB-SYN-001"  # UC02's own plan, untouched
    assert state["equipment_access_result"] is None
    assert state["stage_outcomes"]["equipment_access"]["canonical_status"] == "FAILED"


def test_employee_tenant_and_correlation_identity_stay_consistent_across_every_uc():
    flow = Flow()
    state = flow.approve_signoff(flow.to_branches())
    state = flow.agent.trigger_joining_day(state["case_id"], as_of=JOINING, actor=HR)
    assert status(state, S.COMPLETE) == ST.COMPLETE.value
    outcomes = state["stage_outcomes"]
    assert {o["correlation_id"] for o in outcomes.values()} == {state["correlation_id"]}
    assert len({o["trace_id"] for o in outcomes.values()}) >= 2  # one trace per invocation
    assert len({o["execution_id"] for o in outcomes.values()}) == len(outcomes)  # one id per execution
    for stage, outcome in outcomes.items():
        if stage != "intake":
            assert outcome["inputs"]["employee_id"] == state["employee_id"] == "EMP-TEST-001", stage
            assert outcome["inputs"]["client_id"] == CLIENT, stage
    assert state["employee_ref"]["employee_no"] == state["employee_id"]
    provenance = state["field_provenance"]
    assert {k: provenance[k]["use_case_id"] for k in ("employee_id", "joining_date", "document_validation_result",
                                                      "task_plan", "equipment_access_result", "employee_ref",
                                                      "welcome_message_result")} == {
        "employee_id": "UC01", "joining_date": "UC01", "document_validation_result": "UC03", "task_plan": "UC02",
        "equipment_access_result": "UC04", "employee_ref": "UC05", "welcome_message_result": "UC05"}
    # The last writer is recorded: UC02 created the collection record, the document wait updated what is outstanding.
    assert provenance["document_collection"]["stage"] == "document_submission"
    assert provenance["equipment_access_result"]["execution_id"] == outcomes["equipment_access"]["execution_id"]


def test_a_write_through_that_would_undo_a_completed_stage_is_refused():
    flow = Flow()
    state = flow.to_branches()
    assert status(state, S.EQUIPMENT_ACCESS) == ST.COMPLETE.value
    stored_version = flow.stored(state["case_id"])["state_version"]
    stale = copy.deepcopy(state)
    stale["stage_statuses"][S.EQUIPMENT_ACCESS.value] = ST.BLOCKED.value
    with pytest.raises(StatusTransitionError):
        flow.agent._persist(stale)
    assert flow.stored(state["case_id"])["stage_statuses"]["equipment_access"] == ST.COMPLETE.value
    assert flow.stored(state["case_id"])["state_version"] == stored_version
    reopened = copy.deepcopy(state)
    reopened["stage_statuses"][S.EQUIPMENT_ACCESS.value] = ST.IN_PROGRESS.value  # an explicit re-open is fine
    flow.agent._persist(reopened)
    assert flow.stored(state["case_id"])["state_version"] == stored_version + 1


def test_the_new_bookkeeping_survives_a_restart(tmp_path):
    first = Flow(case_states=SqliteCaseStateStore(tmp_path / "case_states.db"), persist_dir=tmp_path)
    state = first.to_branches()
    case_id, version = state["case_id"], state["state_version"]
    restarted = Flow(case_states=SqliteCaseStateStore(tmp_path / "case_states.db"), persist_dir=tmp_path)
    stored = restarted.agent.get_case(case_id, client_id=CLIENT)
    assert stored["field_provenance"] == state["field_provenance"] and stored["state_version"] == version
    assert stored["stage_outcomes"]["equipment_access"]["execution_id"] == \
        state["stage_outcomes"]["equipment_access"]["execution_id"]
    resumed = restarted.agent.resume_case(case_id, actor=HR)
    assert resumed["state_version"] > version
    assert restarted.uc01.calls == 0 and restarted.notifier.sent_count() == 0  # nothing re-executed
