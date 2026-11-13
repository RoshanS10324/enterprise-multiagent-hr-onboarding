"""One shared implementation for the direct path and the parent-orchestrated path (BR-23, BR-24; TC-33..38)."""

import inspect

import pytest

import sub_agent.entrypoints.direct as direct_module
import sub_agent.entrypoints.orchestrated as orchestrated_module
from helpers import ALL, Env, approvals, item, make, ready_payload, rid, ticket
from orchestrator_harness import LocalOrchestratorHarness
from sub_agent.policy import PolicyViolation
from sub_agent.service import Uc04Service, Uc04ValidationError, UnknownCaseError
from sub_agent.states import CaseStatus, InvocationMode, ItemState, ParentCaseStatus

SCENARIOS = {
    "awaiting_approval": lambda: make(),
    "approved": lambda: make(approval_records=approvals(ALL)),
    "ready": lambda: ready_payload(),
    "blocked_config": lambda: make(config={"approver_config": {"state": "MISSING"}}),
    "out_of_matrix": lambda: make(requested_access=[{"request_key": "SYN-REQ-103", "item_id": "admin-console-access",
                                                     "item_kind": "ACCESS", "requested_by": "SYN-USER-201"}]),
    "late": lambda: make(approval_records=approvals(ALL), as_of_date="2026-11-05"),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_tc33_equivalent_inputs_give_equivalent_business_results(name):
    direct_env, parent_env = Env(), Env()
    direct = direct_env.run(SCENARIOS[name]())
    parent = parent_env.run_orch(SCENARIOS[name]()).result
    assert direct.business_decision() == parent.business_decision()
    assert direct.caller["invocation_mode"] == "DIRECT" and parent.caller["invocation_mode"] == "PARENT_ORCHESTRATED"


def test_tc33_submission_decisions_are_equivalent_too():
    d, p = Env(), Env()
    direct = d.submit(make(approval_records=approvals(ALL))).result
    parent = p.orch.submit_eligible(p.orch_request(make(approval_records=approvals(ALL)))).result
    assert direct.business_decision() == parent.business_decision()
    assert sorted(c["request_id"] for c in d.desk.calls) == sorted(c["request_id"] for c in p.desk.calls)


def test_tc34_parent_invocation_uses_the_existing_case_and_creates_no_second_case():
    env = Env()
    before = env.cases.case_ids
    response = env.run_orch(make())
    assert response.result.case_id == "SYN-CASE-001"
    assert env.cases.case_ids == before and env.cases.created == []
    assert env.service.store.case_ids == ["SYN-CASE-001"]


def test_tc34_workflow_instance_must_be_the_case_being_assessed():
    env = Env()
    with pytest.raises(Uc04ValidationError):
        env.run_orch(make(), instance="SYN-CASE-002")
    assert env.service.store.case_ids == []


def test_tc34_parent_cannot_make_uc04_open_an_unknown_case():
    env = Env()
    with pytest.raises(UnknownCaseError):
        env.run_orch(make(n=9))
    assert env.service.store.case_ids == [] and env.cases.created == []


def test_tc34_invalid_envelope_or_input_is_rejected_without_side_effects():
    env = Env()
    with pytest.raises(Uc04ValidationError):
        env.run_orch(make(joining_date="not-a-date"))
    assert env.service.store.case_ids == [] and env.desk.calls == []


def test_tc35_direct_invocation_cannot_bypass_approval_or_authorisation():
    env = Env()
    assert env.submit(make()).submitted == [] and env.desk.calls == []  # no approvals, nothing submitted
    with pytest.raises(PolicyViolation):
        env.direct.evaluate(make(), actor_id="SYN-USER-100", roles=["intern"])
    with pytest.raises(PolicyViolation):
        env.run_orch(make(), roles=["intern"])


def test_tc35_the_parent_flag_does_not_unlock_unapproved_submission():
    env = Env()
    report = env.orch.submit_eligible(env.orch_request(make()))
    assert report.result.overall_status == CaseStatus.AWAITING_APPROVAL and env.desk.calls == []


def test_tc36_both_entry_points_call_the_same_service_method(monkeypatch):
    seen = []
    original = Uc04Service.evaluate

    def spy(self, request):
        seen.append(request.caller.invocation_mode)
        return original(self, request)

    monkeypatch.setattr(Uc04Service, "evaluate", spy)
    env = Env()
    env.run(make())
    env.run_orch(make(n=3), run_id="r3", key="k3")
    assert seen == [InvocationMode.DIRECT, InvocationMode.PARENT_ORCHESTRATED]
    assert env.direct.service is env.orch.service


def test_tc36_entry_points_contain_no_business_rules():
    for module in (direct_module, orchestrated_module):
        source = inspect.getsource(module)
        for forbidden in ("sub_agent.rules", "sub_agent.items", "sub_agent.approvals", "sub_agent.risk", "sub_agent.readiness", "sub_agent.lifecycle",
                          "sub_agent.evidence", "sub_agent.calendar"):
            assert forbidden not in source, (module.__name__, forbidden)


def test_tc37_parent_cancellation_blocks_further_progress_through_the_orchestrated_path():
    env = Env()
    payload = make(approval_records=approvals(ALL))
    env.orch.submit_eligible(env.orch_request(payload, key="a"))
    calls = len(env.desk.calls)
    env.cases.set_status("SYN-CASE-001", ParentCaseStatus.WITHDRAWN)
    response = env.orch.submit_eligible(env.orch_request(payload, key="b"))
    assert response.result.overall_status == CaseStatus.CANCELLED and len(env.desk.calls) == calls
    assert {i.state for i in response.result.items} == {ItemState.CANCELLED}
    assert response.proposals and all(p.type == "NOTIFY_IT_WITHDRAWAL" for p in response.proposals)


def test_tc38_parent_supplied_role_change_triggers_reassessment_on_both_paths():
    results = []
    for runner in ("direct", "parent"):
        env = Env()
        first = make(approval_records=approvals(ALL))
        changed = make(role_id="data_engineer", form_version="v2", approval_records=approvals(ALL),
                       config={"role_id": "data_engineer"})
        if runner == "direct":
            env.run(first)
            results.append(env.run(changed, idempotency_key="k2"))
        else:
            env.run_orch(first, key="k1")
            results.append(env.run_orch(changed, key="k2", run_id="run-2").result)
    for r in results:
        assert r.reassessment.required and "role_id" in r.reassessment.changed_fields
        assert {i.state for i in r.items} == {ItemState.APPROVAL_PENDING}
    assert results[0].business_decision()["reassessment"] == results[1].business_decision()["reassessment"]


def test_local_harness_runs_uc04_as_one_stage_and_can_query_status_later():
    harness = LocalOrchestratorHarness(Env())
    harness.open_case_stage_result = harness.run_stage(make(approval_records=approvals(ALL)))
    assert harness.run_stage_status("SYN-CASE-001") == harness.open_case_stage_result.result.overall_status
    assert harness.env.cases.created == [] and harness.stage_log == ["sub_agent"]
