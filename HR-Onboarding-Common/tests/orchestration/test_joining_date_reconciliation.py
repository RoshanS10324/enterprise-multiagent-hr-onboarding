"""Joining-date revisions and the reconciliation of results produced for an earlier date
(orchestration/joining_date.py), on the REAL orchestrator graph.

The date changes through UC02's real DATE_CHANGE gate (its OnboardingService, see _uc02_real.py).
UC04 is FakeUc04Worker -- UC04's idempotency (BR-21) and EC-11 reassessment contract, behind the
real UC04 adapter -- and, in one test, the REAL UC04 worker. UC05 is the Uc05Double; its start date
models UC05's: fixed at its intake, with no operation to change it. The real UC05 adapter's own
check is tested against the recording UC05 fake. Task planning is a double (UC02's package cannot
be imported: advisor.py:1).
"""

from __future__ import annotations

from datetime import date

import pytest

from _helpers import UC_ROOTS
from _lifecycle import BUDDY_CANDIDATE, CLIENT_CONFIG, World, start_uc05_worker
from _uc02_real import RealPlanAdapter, SubAgentStub, real_uc02_flow, real_uc02_service
from _uc05_fakes import FakeUc05Worker, Pool, approval, session, snapshot
from _workflow import CLIENT, HR, HR_2, SCHEDULER, Flow, SimulatedCrash, code, status
from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.orchestration import joining_date
from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import call_uc04_equipment_access
from hr_onboarding_common.orchestration.adapters.uc05_orientation import (PrerequisiteNotMetError,
                                                                          call_uc05_orientation)
from hr_onboarding_common.orchestration.api import case_view
from hr_onboarding_common.orchestration.case_store import SqliteCaseStateStore
from hr_onboarding_common.orchestration.contracts import WorkerResponse
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.orchestrator_agent import (PrematureLifecycleEventError,
                                                                   node_day_one_readiness)
from hr_onboarding_common.orchestration.state import (Stage, StageStatus, WorkflowStatus, ensure_lifecycle_fields,
                                                      new_state)
from hr_onboarding_common.platform.hitl import ApprovalItem, Decision, HITLError, HITLInbox

S, ST, W = Stage, StageStatus, WorkflowStatus
ORIGINAL, NEW, LATER = date(2026, 12, 1), date(2026, 12, 15), date(2026, 12, 22)
BRANCHES = (S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING)


def _change(flow: Flow, state, new: date):
    """HR requests a joining-date change through UC02's gate; a second HR admin approves it."""
    state, outcome = flow.agent.request_uc02_date_change(state["case_id"], joining_date=new, actor=HR)
    return flow.agent.decide_approval(state["case_id"], outcome["request_id"], decision="approved", actor=HR_2)


def _signoffs(flow: Flow, state, decision: Decision = Decision.PENDING) -> list[ApprovalItem]:
    return flow.ctx.hitl.list(status=decision, gate="day_one_signoff", workflow_instance=state["case_id"],
                              client_id=CLIENT)


def _used(state, stage: Stage) -> tuple[str, int]:
    inputs = state["stage_outcomes"][stage.value]["inputs"]
    return inputs["joining_date"], inputs["joining_date_revision"]


def _reopened(state) -> list[str]:
    return sorted(e["stage"] for e in state["stage_history"] if e.get("code") == joining_date.CHANGED_CODE)


# ============================================================ revision, history, provenance


def test_uc01_sets_revision_1_and_an_approved_uc02_change_is_the_next_revision():
    flow, _ = real_uc02_flow()
    state = flow.to_branches()
    assert (state["joining_date"], state["joining_date_revision"]) == (ORIGINAL.isoformat(), 1)
    (first,) = state["joining_date_history"]
    assert (first["stage"], first["use_case_id"], first["previous_joining_date"]) == ("intake", "UC01", None)

    state, outcome = flow.agent.request_uc02_date_change(state["case_id"], joining_date=NEW, actor=HR)
    assert state["joining_date_revision"] == 1  # a request changes nothing
    state = flow.agent.decide_approval(state["case_id"], outcome["request_id"], decision="approved", actor=HR_2)
    assert (state["joining_date"], state["joining_date_revision"]) == (NEW.isoformat(), 2)
    second = state["joining_date_history"][-1]
    assert second == {**second, "revision": 2, "joining_date": NEW.isoformat(),
                      "previous_joining_date": ORIGINAL.isoformat(), "use_case_id": "UC02",
                      "source": "date_change_approval", "approval_id": outcome["request_id"]}
    assert state["field_provenance"]["joining_date"]["revision"] == 2
    assert state["verified_context"]["joining_date"] == NEW.isoformat()  # no second, stale copy
    view = case_view(state)
    assert view.joining_date_revision == 2 and view.joining_date_consistency["status"] == "consistent"


def test_a_state_saved_before_revisions_existed_counts_its_date_as_revision_1():
    legacy = new_state(case_id="C1", client_id=CLIENT, correlation_id="c", actor={"on_behalf_of": "u", "roles": []},
                       intake_trigger={})
    legacy["joining_date"] = ORIGINAL.isoformat()
    for key in ("joining_date_revision", "joining_date_history", "joining_date_consistency"):
        del legacy[key]
    state = ensure_lifecycle_fields(legacy)
    assert state["joining_date_revision"] == 1 and state["joining_date_history"] == []
    assert joining_date.record_change(state, ORIGINAL.isoformat(), stage=S.INTAKE, use_case_id="UC01",
                                      source="x", at="t") is None  # the same date is no change


# ============================================================ 6. the updated date reaches both branches


def test_an_approved_change_reaches_both_branches_when_they_resume():
    flow, _ = real_uc02_flow()
    flow.uc04.overall_status = "AWAITING_APPROVAL"  # UC04 waits for a hiring-manager approval
    flow.uc05.status["orientation"] = ST.BLOCKED  # UC05 is paused
    state = flow.to_branches()
    assert status(state, S.EQUIPMENT_ACCESS) == ST.AWAITING_APPROVAL.value
    assert status(state, S.ORIENTATION_SCHEDULING) == ST.BLOCKED.value

    flow.uc04.overall_status, flow.uc05.status["orientation"] = "READY", ST.COMPLETE  # both causes resolved
    state = _change(flow, state, NEW)

    # What each UC was actually handed -- not only what the orchestrator recorded.
    assert flow.uc04.payloads[-1]["input"]["joining_date"] == NEW.isoformat()
    assert flow.uc05.received[-1] == NEW.isoformat()
    assert flow.uc04.keys[-1].endswith(".jd2") and flow.uc04.keys[-1] != flow.uc04.keys[0]
    for stage in BRANCHES:
        assert status(state, stage) == ST.COMPLETE.value and _used(state, stage) == (NEW.isoformat(), 2)
    assert state["joining_date_consistency"]["status"] == "consistent"
    (signoff,) = _signoffs(flow, state)
    assert (signoff.payload["joining_date"], signoff.payload["joining_date_revision"]) == (NEW.isoformat(), 2)


# ============================================================ 7. a change after both branches completed


def test_a_change_after_both_branches_completed_is_reconciled_before_signoff():
    flow, _ = real_uc02_flow()
    state = flow.to_branches()
    case_id = state["case_id"]
    (old_signoff,) = _signoffs(flow, state)
    assert old_signoff.payload["joining_date_revision"] == 1
    flow.uc05.start_date = ORIGINAL.isoformat()  # UC05 fixed the case's start date at its intake

    state = _change(flow, state, NEW)

    # Both stale results were detected and run again for the new date, and readiness (evaluated for
    # the original date, awaiting signoff) was re-opened; nothing else was.
    assert _reopened(state) == ["day_one_readiness", "equipment_access", "orientation_scheduling"]
    assert status(state, S.DAY_ONE_READINESS) == ST.IN_PROGRESS.value
    assert flow.uc04.evaluations == 2 and flow.uc04.payloads[-1]["input"]["joining_date"] == NEW.isoformat()
    equipment = state["equipment_access_result"]
    assert (equipment["joining_date"], equipment["joining_date_revision"]) == (NEW.isoformat(), 2)
    assert equipment["reassessment"]["previous_joining_date"] == ORIGINAL.isoformat()  # UC04's own re-plan
    assert flow.uc05.received == [ORIGINAL.isoformat(), NEW.isoformat()]
    assert (flow.uc05.calls["buddy"], flow.uc05.calls["team_intro"], flow.plan.calls) == (1, 1, 1)

    # UC05 still holds the original start date: a person must align it; nothing proceeds meanwhile.
    assert status(state, S.EQUIPMENT_ACCESS) == ST.COMPLETE.value
    assert (status(state, S.ORIENTATION_SCHEDULING), code(state, S.ORIENTATION_SCHEDULING)) == \
        (ST.BLOCKED.value, "JOINING_DATE_MISMATCH")
    assert state["joining_date_consistency"]["status"] == "conflict"
    assert status(state, S.DAY_ONE_READINESS) != ST.AWAITING_APPROVAL.value
    assert state["workflow_status"] == W.BLOCKED.value and not state["day_one_signoff_approved"]

    # The signoff requested for the original date was withdrawn and can no longer be given.
    assert flow.ctx.hitl.get(old_signoff.id, client_id=CLIENT).status == Decision.WITHDRAWN
    assert _signoffs(flow, state) == []
    with pytest.raises(HITLError):
        flow.agent.decide_approval(case_id, old_signoff.id, decision="approved", actor=HR_2)
    (withdrawal,) = [d for d in state["approval_decisions"] if d.get("kind") == "withdrawal"]
    assert (withdrawal["reference"], withdrawal["joining_date_revision"]) == (old_signoff.id, 1)
    with pytest.raises(PrematureLifecycleEventError):
        flow.agent.trigger_joining_day(case_id, as_of=NEW, actor=SCHEDULER)

    # UC05's owner aligns UC05's record; HR resumes. Only UC05 runs again -- UC04 is already current.
    flow.uc05.start_date = NEW.isoformat()
    state = flow.agent.resume_case(case_id, actor=HR)
    assert status(state, S.ORIENTATION_SCHEDULING) == ST.COMPLETE.value and flow.uc04.evaluations == 2
    assert state["joining_date_consistency"]["status"] == "consistent"
    (signoff,) = _signoffs(flow, state)
    assert signoff.payload["joining_date_revision"] == 2

    state = flow.approve_signoff(state)
    assert status(state, S.JOINING_DAY_WELCOME) == ST.WAITING.value
    with pytest.raises(PrematureLifecycleEventError):  # the original date is no longer the joining day
        flow.agent.trigger_joining_day(case_id, as_of=ORIGINAL, actor=SCHEDULER)
    state = flow.agent.trigger_joining_day(case_id, as_of=NEW, actor=SCHEDULER)
    assert state["workflow_status"] == W.COMPLETED.value
    with pytest.raises(PrerequisiteNotMetError):  # after the joining day the date is final
        flow.agent.request_uc02_date_change(case_id, joining_date=LATER, actor=HR)


# ============================================================ 8. one branch paused when the date changes


def test_one_branch_paused_when_the_date_changes_never_leaves_the_branches_on_different_dates():
    flow, _ = real_uc02_flow()
    flow.uc05.status["orientation"] = ST.BLOCKED  # UC05 paused; UC04 completes on the original date
    state = flow.to_branches()
    assert status(state, S.EQUIPMENT_ACCESS) == ST.COMPLETE.value
    assert _used(state, S.EQUIPMENT_ACCESS) == (ORIGINAL.isoformat(), 1)

    flow.uc05.status["orientation"] = ST.COMPLETE
    state = _change(flow, state, NEW)

    assert _reopened(state) == ["equipment_access"]  # UC04's result was stale; UC05's re-ran anyway
    assert flow.uc04.evaluations == 2 and flow.uc05.calls["orientation"] == 2
    assert flow.uc05.received == [ORIGINAL.isoformat(), NEW.isoformat()]
    assert _used(state, S.EQUIPMENT_ACCESS) == _used(state, S.ORIENTATION_SCHEDULING) == (NEW.isoformat(), 2)
    assert state["joining_date_consistency"]["status"] == "consistent"
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value


# ============================================================ 9. repeated changes


def test_repeated_changes_never_leave_an_earlier_result_marked_current():
    flow, _ = real_uc02_flow()
    state = flow.to_branches()
    for revision, new in enumerate((NEW, LATER, ORIGINAL), start=2):  # ... and back to the first date
        state = _change(flow, state, new)
        assert (state["joining_date"], state["joining_date_revision"]) == (new.isoformat(), revision)
        assert state["joining_date_consistency"]["status"] == "consistent"
        for stage in BRANCHES:
            assert _used(state, stage) == (new.isoformat(), revision)
        assert state["equipment_access_result"]["joining_date"] == new.isoformat()
        assert state["orientation_schedule_result"]["uc05_start_date"] == new.isoformat()
        assert [s.payload["joining_date_revision"] for s in _signoffs(flow, state)] == [revision]

    assert [h["revision"] for h in state["joining_date_history"]] == [1, 2, 3, 4]
    assert [h["previous_joining_date"] for h in state["joining_date_history"]] == \
        [None, ORIGINAL.isoformat(), NEW.isoformat(), LATER.isoformat()]
    base = flow.uc04.keys[0]
    assert flow.uc04.keys == [base, f"{base}.jd2", f"{base}.jd3", f"{base}.jd4"]  # one evaluation per date
    assert flow.uc04.evaluations == 4 and len(_signoffs(flow, state, Decision.WITHDRAWN)) == 3

    # Nothing is left to reconcile: further resumes run nothing again.
    calls = (flow.plan.calls, flow.uc04.calls, flow.uc05.calls["orientation"])
    for _ in range(2):
        state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert (flow.plan.calls, flow.uc04.calls, flow.uc05.calls["orientation"]) == calls


# ============================================================ 10. failure, restart, idempotency


def test_a_process_stopping_mid_reconciliation_resumes_without_repeating_uc04s_evaluation(tmp_path):
    store_path = tmp_path / "case_states.db"
    flow, _ = real_uc02_flow(case_states=SqliteCaseStateStore(store_path))
    state = flow.to_branches()
    case_id, uc04 = state["case_id"], flow.uc04
    answer = uc04.call

    def stop_after_uc04_answers(op, payload, **kwargs):
        answer(op, payload, **kwargs)  # UC04 evaluated for the new date: its side effect happened
        raise SimulatedCrash("the process stopped before the orchestrator saved UC04's answer")

    uc04.call = stop_after_uc04_answers
    state, outcome = flow.agent.request_uc02_date_change(case_id, joining_date=NEW, actor=HR)
    with pytest.raises(SimulatedCrash):
        flow.agent.decide_approval(case_id, outcome["request_id"], decision="approved", actor=HR_2)
    del uc04.call
    stored = SqliteCaseStateStore(store_path)[case_id]
    assert (stored["joining_date"], stored["joining_date_revision"]) == (NEW.isoformat(), 2)  # saved first
    assert stored["stage_statuses"]["equipment_access"] == ST.IN_PROGRESS.value  # re-opened, answer not saved
    assert uc04.evaluations == 2

    restarted = Flow(case_states=SqliteCaseStateStore(store_path))  # a new process, the same store
    restarted.deps.worker_pool.workers["uc04"] = uc04  # UC04 is its own service: its records survived
    state = restarted.agent.resume_case(case_id, actor=HR)

    assert uc04.evaluations == 2 and uc04.keys[-1] == uc04.keys[-2]  # replayed under the same key, not re-done
    assert restarted.plan.calls == 0  # plan_tasks was complete: never re-run
    assert all(d == NEW.isoformat() for d in restarted.uc05.received)
    for stage in BRANCHES:
        assert status(state, stage) == ST.COMPLETE.value and _used(state, stage) == (NEW.isoformat(), 2)
    assert state["joining_date_consistency"]["status"] == "consistent"
    (signoff,) = _signoffs(restarted, state)
    assert signoff.payload["joining_date_revision"] == 2


def test_a_failed_reevaluation_holds_readiness_until_an_authorized_retry():
    flow, _ = real_uc02_flow()
    state = flow.to_branches()
    flow.uc04.call = lambda op, payload, **kw: WorkerResponse(run_id="r", ok=False, error_code="UC04_UNAVAILABLE",
                                                              error_detail="UC04 is down (double)")
    state = _change(flow, state, NEW)
    assert status(state, S.EQUIPMENT_ACCESS) == ST.FAILED.value
    assert status(state, S.DAY_ONE_READINESS) != ST.AWAITING_APPROVAL.value and _signoffs(flow, state) == []
    assert state["equipment_access_result"]["joining_date"] == ORIGINAL.isoformat()  # kept, never used as ready

    del flow.uc04.call
    state = flow.agent.retry_stage(state["case_id"], "equipment_access", actor=HR)
    assert status(state, S.EQUIPMENT_ACCESS) == ST.COMPLETE.value
    assert state["equipment_access_result"]["joining_date"] == NEW.isoformat()
    (signoff,) = _signoffs(flow, state)
    assert signoff.payload["joining_date_revision"] == 2


# ============================================================ readiness protection (defence in depth)


def test_readiness_refuses_signoff_for_results_produced_for_another_date():
    flow = Flow()
    state = flow.to_branches()
    state["joining_date"] = NEW.isoformat()  # a change no reconciliation has seen
    result = node_day_one_readiness(state)
    assert result["stage_statuses"][S.DAY_ONE_READINESS.value] == ST.BLOCKED.value
    error = result["stage_errors"][S.DAY_ONE_READINESS.value]
    assert error["code"] == "JOINING_DATE_UNRECONCILED"
    assert "equipment_access" in error["detail"] and "orientation_scheduling" in error["detail"]
    assert any(b["code"] == "JOINING_DATE_UNRECONCILED" for b in result["blockers"])


def test_readiness_refuses_signoff_while_uc05_holds_another_start_date():
    flow = Flow()
    state = flow.to_branches()
    state["orientation_schedule_result"]["uc05_start_date"] = "2026-11-24"
    result = node_day_one_readiness(state)
    assert result["stage_statuses"][S.DAY_ONE_READINESS.value] == ST.BLOCKED.value
    assert "UC05 holds start date 2026-11-24" in result["stage_errors"][S.DAY_ONE_READINESS.value]["detail"]


def test_reconcile_leaves_an_uncertain_or_failed_stale_result_to_a_person():
    flow = Flow()
    state = flow.to_branches()
    state["stage_statuses"][S.EQUIPMENT_ACCESS.value] = ST.UNCERTAIN.value
    state["day_one_signoff_approved"] = True
    joining_date.record_change(state, NEW.isoformat(), stage=S.PLAN_TASKS, use_case_id="UC02", source="test",
                               at=now_utc().isoformat())
    actions = {a["stage"]: a["action"] for a in joining_date.reconcile(state, at=now_utc().isoformat())}
    assert actions == {"equipment_access": "manual", "orientation_scheduling": "re_execute",
                       "day_one_readiness": "re_evaluate"}
    assert state["stage_statuses"][S.EQUIPMENT_ACCESS.value] == ST.UNCERTAIN.value  # never written while unknown
    assert state["stage_statuses"][S.ORIENTATION_SCHEDULING.value] == ST.IN_PROGRESS.value
    assert state["day_one_signoff_approved"] is False


# ============================================================ the adapters


def test_the_uc05_adapter_blocks_when_uc05_holds_an_earlier_start_date():
    confirmed = snapshot(start_date=ORIGINAL.isoformat(), sessions=[session("induction", "confirmed")],
                         approvals=[approval("apr-ind", "session_booking", status="approved", record_id="session-1")])
    worker = FakeUc05Worker(case_snapshot=confirmed)
    state = new_state(case_id="CASE-1", client_id=CLIENT, correlation_id="corr1",
                      actor={"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": CLIENT},
                      intake_trigger={"event_id": "EVT-001"})
    state.update(employee_id="EMP-TEST-001", joining_date=NEW.isoformat(),
                 orientation_schedule_result={"uc05_case": True, "uc05_start_date": ORIGINAL.isoformat()})
    deps = OrchestratorDeps(ctx=None, worker_pool=Pool(uc05=worker), uc03_repo_root=None)

    result = call_uc05_orientation(state, deps)

    assert worker.ops() == ["case_snapshot"]  # read only: nothing was rescheduled in UC05
    assert result["stage_statuses"][S.ORIENTATION_SCHEDULING.value] == ST.BLOCKED.value
    assert result["stage_errors"][S.ORIENTATION_SCHEDULING.value]["code"] == "JOINING_DATE_MISMATCH"
    assert result["orientation_schedule_result"]["uc05_start_date"] == ORIGINAL.isoformat()


class _RealUc02World(World):
    """The REAL UC05 worker (World), with task planning creating a real UC02 onboarding record so
    UC02's real DATE_CHANGE gate can be used."""

    def __init__(self, uc05_worker):
        super().__init__(uc05_worker)
        service = real_uc02_service()
        self._real_plan = RealPlanAdapter(service)
        self.deps._uc02_sub_agent = SubAgentStub(service)

    def _plan_double(self, state, deps):
        self.plan_calls += 1
        return self._real_plan(state, deps)


@pytest.fixture(scope="module")
def uc05():
    worker = start_uc05_worker()
    yield worker
    worker.stop()


def test_with_the_real_uc05_a_change_at_signoff_is_held_because_uc05_keeps_its_start_date(uc05):
    """UC05 fixes a case's start date at its intake and offers no operation to change it. After an
    approved change, the real UC05 adapter re-reads UC05 (read only) and stops for a person; the
    signoff requested for the original date is withdrawn and none is raised."""
    world = _RealUc02World(uc05)
    world.hook("reset")
    world.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    world.hook("add_client_config", config=CLIENT_CONFIG)
    state = world.to_signoff_pending()
    case_id = state["case_id"]
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value
    counts = world.hook("counts")

    state = _change(world, state, NEW)

    assert world.hook("counts") == counts  # nothing booked, cancelled or sent in UC05
    assert (status(state, S.ORIENTATION_SCHEDULING), code(state, S.ORIENTATION_SCHEDULING)) == \
        (ST.BLOCKED.value, "JOINING_DATE_MISMATCH")
    assert state["orientation_schedule_result"]["uc05_start_date"] == ORIGINAL.isoformat()
    assert _used(state, S.EQUIPMENT_ACCESS) == (NEW.isoformat(), 2)  # UC04 (double) re-evaluated
    assert state["joining_date_consistency"]["status"] == "conflict"
    assert status(state, S.DAY_ONE_READINESS) == ST.IN_PROGRESS.value and _signoffs(world, state) == []
    assert len(_signoffs(world, state, Decision.WITHDRAWN)) == 1
    with pytest.raises(PrematureLifecycleEventError):
        world.agent.trigger_joining_day(case_id, as_of=NEW, actor=SCHEDULER)


class _Uc04Pool:
    def __init__(self, worker):
        self.worker = worker

    def get(self, uc_id):
        assert uc_id == "uc04"
        return self.worker


def test_the_real_uc04_reassesses_a_revised_date_under_a_new_key(uc04_worker):
    """The real UC04 worker: a revised joining date gets UC04's EC-11 reassessment; reusing the
    first revision's key for the new date is exactly what UC04 refuses."""
    deps = OrchestratorDeps(ctx=None, worker_pool=_Uc04Pool(uc04_worker), uc03_repo_root=UC_ROOTS["uc03"])
    state = new_state(case_id="SYN-CASE-901", client_id="client_alpha", correlation_id="corr-jd",
                      actor={"on_behalf_of": "SYN-USER-201", "roles": ["hr_onboarding_officer"]}, intake_trigger={},
                      uc04_client_config_ref=str(UC_ROOTS["uc04"] / "data" / "client_alpha_config.json"))
    state["employee_id"] = "SYN-EMP-901"
    joining_date.record_change(state, ORIGINAL.isoformat(), stage=S.INTAKE, use_case_id="UC01", source="test",
                               at=now_utc().isoformat())
    first = call_uc04_equipment_access(state, deps)
    assert first["stage_statuses"][S.EQUIPMENT_ACCESS.value] != ST.FAILED.value
    assert not (first["equipment_access_result"]["reassessment"] or {}).get("required")

    joining_date.record_change(first, NEW.isoformat(), stage=S.PLAN_TASKS, use_case_id="UC02",
                               source="date_change_approval", at=now_utc().isoformat())
    second = call_uc04_equipment_access(first, deps)
    result = second["equipment_access_result"]
    assert second["stage_statuses"][S.EQUIPMENT_ACCESS.value] != ST.FAILED.value
    assert (result["joining_date"], result["joining_date_revision"]) == (NEW.isoformat(), 2)
    assert result["reassessment"]["required"] is True and "joining_date" in result["reassessment"]["changed_fields"]
    assert result["reassessment"]["previous_joining_date"] == ORIGINAL.isoformat()

    replay = call_uc04_equipment_access(second, deps)  # same revision, same key, same payload: idempotent
    assert replay["equipment_access_result"]["overall_status"] == result["overall_status"]

    unrotated = {**second, "joining_date_revision": 1}  # the first revision's key with the new date
    refused = call_uc04_equipment_access(unrotated, deps)
    assert refused["stage_statuses"][S.EQUIPMENT_ACCESS.value] == ST.FAILED.value
    # UC04's IdempotencyConflict, reported by the worker as INTERNAL_ERROR with UC04's own message.
    assert "idempotency key reused with a different payload" in refused["error_detail"]


# ============================================================ HITL: withdrawing a request


def test_a_withdrawn_request_is_no_decision_and_can_no_longer_be_decided():
    inbox = HITLInbox()

    def submit():
        return inbox.submit(ApprovalItem(gate="day_one_signoff", approver_role="hr_admin", workflow="w",
                                         workflow_instance="C1", agent="onboarding-orchestrator",
                                         proposal_type="day_one_signoff", client_id=CLIENT, payload={},
                                         rationale="r", confidence=1.0, run_id="run"))

    item = submit()
    withdrawn = inbox.withdraw(item.id, client_id=CLIENT, by="agent:test", reason="superseded")
    assert withdrawn.status == Decision.WITHDRAWN and withdrawn.approver is None
    assert inbox.list(status=Decision.PENDING) == []
    with pytest.raises(HITLError):
        inbox.decide(item.id, Decision.APPROVED, approver="u", approver_roles=["hr_admin"], client_id=CLIENT)
    with pytest.raises(HITLError):
        inbox.withdraw(item.id, client_id=CLIENT, by="agent:test", reason="again")
    with pytest.raises(HITLError):  # "withdrawn" is never a decision a person can give
        inbox.decide(submit().id, Decision.WITHDRAWN, approver="u", approver_roles=["hr_admin"], client_id=CLIENT)
    assert inbox.approval_stats("onboarding-orchestrator")["decided"] == 0
