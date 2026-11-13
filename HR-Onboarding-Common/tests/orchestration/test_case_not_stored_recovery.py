"""A form approved while UC01's case cannot be stored: the case is blocked with the reason, never stranded.

Reproduces the Control Center report "apr-... already decided (approved)": the orchestration state is in Supabase
(case_orchestration_state, foreign key to onboarding_cases), UC01 runs in memory and opens a case (uuid) that has no
onboarding_cases row, and the HR lead approves the internal onboarding form. Before the fix the decision was recorded,
the intake hand-off failed when the state was saved, and the case kept offering an approval that could no longer be
decided. Now: the decision stands, the approval leaves the pending list, intake is BLOCKED with CASE_NOT_IN_DATABASE
(stored on the provisional case), the same decision repeated re-runs the follow-up, and the case resumes once UC01
stores the case.

Doubles: the database (_fake_postgrest.py), UC01 (_lifecycle.FakeUc01Worker, with uuid case ids), UC02-UC05
(_workflow.py). Real: the orchestrator, its HITL gate, the SupabaseCaseStateStore and its provisional store.
"""

from __future__ import annotations

import uuid

import pytest

from _fake_postgrest import KEY, URL, FakePostgrest
from _lifecycle import FakeUc01Worker
from _workflow import CLIENT, HR, Flow, code, status
from hr_onboarding_common.orchestration.case_store import InMemoryCaseStateStore
from hr_onboarding_common.orchestration.state import Stage, StageStatus
from hr_onboarding_common.orchestration.supabase_state_store import SupabaseCaseStateStore
from hr_onboarding_common.platform.hitl import Decision, HITLError

HR_LEAD = {"on_behalf_of": "user:hr-lead-01", "roles": ["hr_lead"], "client_id": CLIENT}


class InMemoryUc01(FakeUc01Worker):
    """UC01 in its in-memory mode: uuid case ids, nothing stored in onboarding_cases (until `store_cases`)."""

    def __init__(self, pg: FakePostgrest):
        super().__init__()
        self.pg, self.store_cases, self.opened = pg, False, []

    def call(self, op, payload, **kwargs):
        response = super().call(op, payload, **kwargs)
        case = (response.result or {}).get("case")
        if case:
            case["case_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, case["case_id"]))  # same request -> same case
            self.opened.append(case["case_id"])
            if self.store_cases:
                self.pg.add_case(case["case_id"], payload["client_id"])
        return response


def world():
    pg = FakePostgrest()
    states = SupabaseCaseStateStore(url=URL, service_role_key=KEY, transport=pg.transport(),
                                    provisional=InMemoryCaseStateStore())
    flow = Flow(case_states=states)
    flow.uc01 = InMemoryUc01(pg)
    flow.deps.worker_pool.workers["uc01"] = flow.uc01
    return flow, pg


def submit_form(flow: Flow):
    state = flow.agent.advance_case(flow.case_input(form_approval_required=True), case_id=None, actor=HR)
    assert status(state, Stage.INTAKE) == StageStatus.AWAITING_APPROVAL.value
    return state["case_id"], state["form_approval"]["approval_id"]


def test_an_approved_form_whose_case_cannot_be_stored_is_blocked_with_the_reason_not_stranded():
    flow, pg = world()
    case_id, approval_id = submit_form(flow)

    state = flow.agent.decide_approval(case_id, approval_id, decision="approved", actor=HR_LEAD)
    assert status(state, Stage.INTAKE) == StageStatus.BLOCKED.value
    assert code(state, Stage.INTAKE) == "CASE_NOT_IN_DATABASE"
    (blocker,) = [b for b in state["blockers"] if b["stage"] == Stage.INTAKE.value]
    assert blocker["required_from"].startswith("UC01") and blocker["retryable"] is True
    assert state["form_approval"]["status"] == "approved"  # the decision stands
    assert not [p for p in state["pending_approvals"] if p["reference"] == approval_id]  # no longer offered
    assert pg.tables["case_orchestration_state"] == {}  # nothing written to Supabase for an unstored case
    stored = flow.agent.get_case(case_id, client_id=CLIENT)  # the provisional case holds the outcome
    assert code(stored, Stage.INTAKE) == "CASE_NOT_IN_DATABASE"


def test_the_same_decision_again_re_runs_the_follow_up_and_the_requester_still_cannot_decide():
    flow, pg = world()
    case_id, approval_id = submit_form(flow)
    # A case stranded by the earlier behaviour: decided in the inbox, still listed as pending on the case.
    flow.ctx.hitl.decide(approval_id, Decision.APPROVED, approver=HR_LEAD["on_behalf_of"],
                         approver_roles=HR_LEAD["roles"], client_id=CLIENT)
    state = flow.agent.get_case(case_id, client_id=CLIENT)
    assert [p for p in state["pending_approvals"] if p["reference"] == approval_id]

    with pytest.raises(HITLError):  # the requester (separation of duties) gets no second path to the gate
        flow.agent.decide_approval(case_id, approval_id, decision="approved",
                                   actor={**HR, "roles": ["hr_admin", "hr_lead"]})
    with pytest.raises(HITLError):  # nor does a different decision
        flow.agent.decide_approval(case_id, approval_id, decision="rejected", actor=HR_LEAD)

    state = flow.agent.decide_approval(case_id, approval_id, decision="approved", actor=HR_LEAD)
    assert code(state, Stage.INTAKE) == "CASE_NOT_IN_DATABASE"  # re-applied: intake ran again, the case is blocked
    assert flow.uc01.calls == 1
    assert not [p for p in state["pending_approvals"] if p["reference"] == approval_id]


def test_once_uc01_stores_the_case_a_resume_continues_into_supabase():
    flow, pg = world()
    case_id, approval_id = submit_form(flow)
    flow.agent.decide_approval(case_id, approval_id, decision="approved", actor=HR_LEAD)

    flow.uc01.store_cases = True  # UC01's Supabase mode: its case is now an onboarding_cases row
    state = flow.agent.resume_case(case_id, actor=HR)
    assert status(state, Stage.INTAKE) == StageStatus.COMPLETE.value
    assert status(state, Stage.PLAN_TASKS) == StageStatus.COMPLETE.value
    assert state["case_id"] == flow.uc01.opened[-1] == flow.uc01.opened[0]  # the same UC01 case, not a second one
    assert pg.row(state["case_id"])["stage_statuses"][Stage.INTAKE.value] == StageStatus.COMPLETE.value
    assert not [b for b in state["blockers"] if b["code"] == "CASE_NOT_IN_DATABASE"]
