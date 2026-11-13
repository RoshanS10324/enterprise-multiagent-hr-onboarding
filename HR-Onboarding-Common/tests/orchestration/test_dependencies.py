"""Stage dependencies (orchestration/dependencies.py): one registry, checked before any UC is called.

Graph-level tests run the REAL orchestrator graph with the test world of _workflow.py (UC01/UC04
fakes behind the real adapters, the real document-collection adapter, UC02-planning/UC03/UC05
doubles). Registry tests are pure.
"""

from __future__ import annotations

import pytest
from _workflow import HR, Flow, code, status
from pydantic import ValidationError

from hr_onboarding_common.orchestration import dependencies
from hr_onboarding_common.orchestration.dependencies import STAGE_DEPENDENCIES, DependencyKind
from hr_onboarding_common.orchestration.state import STAGE_ORDER, Branch, Stage, StageStatus, new_state
from hr_onboarding_common.orchestration.tenant_config import TenantConfig

S, ST = Stage, StageStatus
REQUIRE_UC04_BEFORE_UC05 = {"tenants": {"CLIENT-TEST-A": {"dependencies": {
    "orientation_scheduling": {"uc04.equipment_access_ready": "required"}}}}}


def _state(*complete: Stage, **fields):
    state = new_state(case_id="C1", client_id="CLIENT-TEST-A", correlation_id="c",
                      actor={"on_behalf_of": "u", "roles": ["hr_admin"], "client_id": "CLIENT-TEST-A"}, intake_trigger={})
    for stage in complete:
        state["stage_statuses"][stage.value] = ST.COMPLETE.value
    state.update(fields)
    return state


# ------------------------------------------------------------------------------- the registry


def test_every_stage_has_an_entry_and_every_dependency_points_upstream():
    assert set(STAGE_DEPENDENCIES) == {s for s in Stage if s != Stage.COMPLETE}
    for stage, deps in STAGE_DEPENDENCIES.items():
        for dep in deps:
            assert STAGE_ORDER.index(dep.upstream) < STAGE_ORDER.index(stage), (stage, dep.key)  # acyclic


def test_the_uc01_to_uc05_chain_is_declared_as_the_business_requires():
    def keys(stage, kind=DependencyKind.REQUIRED):
        return {d.key for d in STAGE_DEPENDENCIES[stage] if d.kind == kind}

    assert {"uc01.employee_record", "uc01.joining_date"} <= keys(S.PLAN_TASKS)  # UC01 -> UC02
    assert "uc03.documents_verified" not in keys(S.PLAN_TASKS)  # UC02 plans before the documents
    assert "uc02.document_requirements" in keys(S.DOCUMENT_COLLECTION)  # UC02 -> UC03 (the plan's documents)
    assert "uc03.document_collection" in keys(S.VALIDATE_DOCUMENTS)  # UC03 validates what it collected
    assert "uc02.onboarding_plan" in keys(S.REPLAN_TASKS)  # UC02 replans the plan it made
    assert "uc03.documents_verified" in keys(S.EQUIPMENT_ACCESS)  # UC03 -> UC04
    assert "uc03.documents_verified" in keys(S.ORIENTATION_SCHEDULING)  # UC03 -> UC05
    assert "uc02.task_plan" in keys(S.EQUIPMENT_ACCESS)  # UC02 -> UC04: plan_tasks must complete first
    assert "uc02.task_plan" in keys(S.ORIENTATION_SCHEDULING)  # UC02 -> UC05: plan_tasks must complete first
    assert "uc04.equipment_access_ready" in keys(S.ORIENTATION_SCHEDULING, DependencyKind.OPTIONAL)  # UC04 -> UC05
    assert "uc04.equipment_access_ready" in keys(S.DAY_ONE_READINESS)  # ... and required for day 1
    assert keys(S.JOINING_DAY_WELCOME) == {"orchestrator.day_one_signoff"}  # (UC04 through readiness)


def test_a_tenant_may_only_make_a_configurable_optional_dependency_required():
    tenants = TenantConfig.model_validate(REQUIRE_UC04_BEFORE_UC05)
    (uc04,) = [d for d in dependencies.dependencies_for(S.ORIENTATION_SCHEDULING, "CLIENT-TEST-A", tenants)
               if d.key == "uc04.equipment_access_ready"]
    assert uc04.kind == DependencyKind.REQUIRED
    (other,) = [d for d in dependencies.dependencies_for(S.ORIENTATION_SCHEDULING, "CLIENT-TEST-B", tenants)
                if d.key == "uc04.equipment_access_ready"]
    assert other.kind == DependencyKind.OPTIONAL  # another tenant keeps the default
    for invalid in ({"equipment_access": {"uc03.documents_verified": "optional"}},  # relaxing a required one
                    {"plan_tasks": {"uc02.task_plan": "required"}},  # not one of its dependencies
                    {"orientation_scheduling": {"uc02.task_plan": "optional"}},  # required and not configurable
                    {"no_such_stage": {}}):
        with pytest.raises(ValidationError):
            TenantConfig.model_validate({"tenants": {"CLIENT-TEST-A": {"dependencies": invalid}}})


def test_an_unmet_upstream_stage_is_a_wait_and_a_missing_upstream_output_is_missing_data():
    check = dependencies.evaluate(S.PLAN_TASKS, _state())[0]
    assert (check.satisfied, check.code, check.upstream_stage, check.use_case_id) == \
        (False, "PREREQUISITE_NOT_MET", "intake", "UC01")
    check = dependencies.evaluate(S.PLAN_TASKS, _state(S.INTAKE, employee_id=None))[0]
    assert check.code == "MISSING_DATA" and "employee_id" in check.detail
    assert dependencies.evaluate(S.PLAN_TASKS, _state(S.INTAKE, employee_id="E1"))[0].satisfied


def test_the_registry_is_served_per_tenant():
    graph = dependencies.dependency_graph("CLIENT-TEST-A", TenantConfig.model_validate(REQUIRE_UC04_BEFORE_UC05))
    (uc04,) = [d for d in graph["orientation_scheduling"] if d["key"] == "uc04.equipment_access_ready"]
    assert uc04["kind"] == "required" and uc04["configurable"] is True


# ------------------------------------------------------------------- dependencies in the graph


def test_uc02_cannot_plan_without_uc01s_employee_record_and_no_document_is_requested():
    flow = Flow()
    flow.uc01.employee_id = None  # UC01 opened the case without an employee number
    state = flow.start()
    assert status(state, S.INTAKE) == ST.COMPLETE.value
    assert status(state, S.PLAN_TASKS) == ST.BLOCKED.value and flow.plan.calls == 0
    assert code(state, S.PLAN_TASKS) == "MISSING_DATA" and flow.notifier.sent_count() == 0
    assert status(state, S.DOCUMENT_COLLECTION) == ST.PENDING.value
    (check,) = [c for c in state["stage_outcomes"]["plan_tasks"]["dependencies"] if not c["satisfied"]]
    assert (check["key"], check["code"]) == ("uc01.employee_record", "MISSING_DATA")


def test_uc03_cannot_validate_before_uc02s_requirements_are_met_by_submitted_documents():
    flow = Flow()
    state = flow.start()
    assert status(state, S.DOCUMENT_SUBMISSION) == ST.WAITING_FOR_INPUT.value
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.PENDING.value and flow.uc03.calls == 0
    # Driving the stage directly (as a retry or a resume path could) still never reaches UC03.
    result = flow.agent._execute_stage(state, S.VALIDATE_DOCUMENTS, lambda s: flow.uc03(s, flow.deps), "t" * 32)
    assert status(result, S.VALIDATE_DOCUMENTS) == ST.BLOCKED.value
    assert code(result, S.VALIDATE_DOCUMENTS) == "PREREQUISITE_NOT_MET" and flow.uc03.calls == 0
    assert result["stage_outcomes"]["validate_documents"]["canonical_status"] == "PENDING"


def test_uc04_cannot_start_while_document_validation_is_incomplete():
    flow = Flow()
    flow.uc03.default = "hr_review_required"
    state = flow.to_branches()
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.AWAITING_APPROVAL.value
    assert status(state, S.EQUIPMENT_ACCESS) == ST.PENDING.value and flow.uc04.calls == 0
    result = flow.agent._execute_stage(state, S.EQUIPMENT_ACCESS, lambda s: s, "t" * 32)
    assert code(result, S.EQUIPMENT_ACCESS) == "PREREQUISITE_NOT_MET" and flow.uc04.calls == 0


def test_by_default_uc05_scheduling_runs_in_parallel_with_uc04_and_records_the_optional_dependency():
    flow = Flow(uc04_status="AWAITING_APPROVAL")
    state = flow.to_branches()
    assert status(state, S.EQUIPMENT_ACCESS) == ST.AWAITING_APPROVAL.value
    assert status(state, S.ORIENTATION_SCHEDULING) == ST.COMPLETE.value and flow.uc05.calls["orientation"] == 1
    outcome = state["stage_outcomes"]["orientation_scheduling"]
    (uc04,) = [c for c in outcome["dependencies"] if c["key"] == "uc04.equipment_access_ready"]
    assert uc04["kind"] == "optional" and uc04["satisfied"] is False
    assert any(w["code"] == "DEPENDENCY_OPTIONAL_UNMET" and "uc04" in w["detail"] for w in outcome["warnings"])
    assert status(state, S.DAY_ONE_READINESS) == ST.PENDING.value  # day 1 still requires UC04


def test_a_tenant_requiring_uc04_first_holds_uc05_then_runs_it_in_the_same_invocation_once_uc04_is_ready():
    flow = Flow(uc04_status="AWAITING_APPROVAL")
    flow.deps.tenants = TenantConfig.model_validate(REQUIRE_UC04_BEFORE_UC05)
    state = flow.to_branches()
    assert status(state, S.ORIENTATION_SCHEDULING) == ST.BLOCKED.value
    assert code(state, S.ORIENTATION_SCHEDULING) == "PREREQUISITE_NOT_MET"
    assert flow.uc05.calls["orientation"] == 0  # UC05 never called
    # A dependency wait is not reported as a block: the case waits for UC04's approval.
    assert state["branch_statuses"][Branch.ORIENTATION.value]["status"] == "pending"
    assert state["workflow_status"] == "waiting_for_approval"

    flow.uc04.overall_status = "READY"  # UC04's approval was applied in UC04
    state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.EQUIPMENT_ACCESS) == ST.COMPLETE.value
    # UC04 completed in the first fan-out; only the UC05 branch was dispatched again, in the same invocation.
    assert state["dispatch_round"] == 2 and state["active_branches"] == [Branch.ORIENTATION.value]
    for stage in (S.ORIENTATION_SCHEDULING, S.BUDDY_ASSIGNMENT, S.TEAM_INTRODUCTION):
        assert status(state, stage) == ST.COMPLETE.value
    assert flow.uc05.calls["orientation"] == 1 and flow.plan.calls == 1 and flow.uc04.calls == 2
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value


def test_redispatch_is_bounded_and_never_reruns_a_branch_that_does_not_need_it():
    flow = Flow(uc04_status="AWAITING_APPROVAL")
    flow.deps.tenants = TenantConfig.model_validate(REQUIRE_UC04_BEFORE_UC05)
    state = flow.to_branches()
    for _ in range(3):  # UC04 stays unready: every invocation dispatches once and parks
        state = flow.agent.resume_case(state["case_id"], actor=HR)
        assert state["dispatch_round"] == 1 and state["redispatch_branches"] == []
    assert flow.uc05.calls["orientation"] == 0 and flow.plan.calls == 1


def test_the_joining_day_welcome_cannot_start_before_day_one_signoff():
    flow = Flow()
    state = flow.to_branches()
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value
    assert status(state, S.JOINING_DAY_WELCOME) == ST.PENDING.value and flow.uc05.calls["welcome"] == 0
    result = flow.agent._execute_stage(state, S.JOINING_DAY_WELCOME, lambda s: flow.uc05.welcome(s, flow.deps),
                                       "t" * 32)
    assert code(result, S.JOINING_DAY_WELCOME) == "PREREQUISITE_NOT_MET" and flow.uc05.calls["welcome"] == 0
