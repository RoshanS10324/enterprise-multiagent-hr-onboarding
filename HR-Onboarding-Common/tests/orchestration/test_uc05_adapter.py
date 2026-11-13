"""UC05 adapter tests, including the required negative tenant-isolation test: UC05 fails closed
on a missing/wrong client_id in its OWN ToolGateway/tool_registry code.

The first group runs against the real UC05 worker. The second group replaces only UC05 with a
recording double (`_uc05_fakes.FakeUc05Worker`, whose replies have the real worker's shape) to pin
down, per capability, what the adapter sends and how it reads UC05's return contract."""

from __future__ import annotations

import pytest

from _uc05_fakes import (EMPLOYEE, FakeUc05Worker, Pool, approval, decision_outcome, event_run, failure, preflight,
                         session, snapshot)
from hr_onboarding_common.orchestration.adapters.uc05_orientation import (
    PrerequisiteNotMetError, Uc05RelayError, call_uc05_buddy_assignment, call_uc05_joining_day_welcome,
    call_uc05_orientation, call_uc05_team_introduction, relay_uc05_decision, relay_uc05_retry, request_uc05_operation,
)
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state
from hr_onboarding_common.orchestration.workers.uc_worker_client import WorkerCrashedError, WorkerTimeoutError


class _SingleWorkerPool:
    def __init__(self, worker):
        self._worker = worker

    def get(self, uc_id):
        assert uc_id == "uc05"
        return self._worker


def _state(client_id="CLIENT-TEST-A", joining_date="2026-12-01", event_id="EVT-001"):
    state = new_state(case_id="CASE-1", client_id=client_id, correlation_id="corr1",
                       actor={"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": client_id},
                       intake_trigger={"event_id": event_id})
    state["employee_id"] = "EMP-TEST-001"
    state["joining_date"] = joining_date
    return state


def test_unconfirmed_joining_date_blocks_scheduling_without_calling_uc05():
    deps = OrchestratorDeps(ctx=None, worker_pool=None, uc03_repo_root=None)
    state = _state(joining_date=None)
    result = call_uc05_orientation(state, deps)
    assert result["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] == StageStatus.BLOCKED.value
    assert result["error_code"] == "MISSING_DATA"


def test_successful_offer_accepted_event_completes_or_awaits_approval(uc05_worker):
    deps = OrchestratorDeps(ctx=None, worker_pool=_SingleWorkerPool(uc05_worker), uc03_repo_root=None)
    result = call_uc05_orientation(_state(), deps)
    assert result["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] in (
        StageStatus.COMPLETE.value, StageStatus.AWAITING_APPROVAL.value,
    )
    assert result["orientation_schedule_result"]["stage"]


def test_cross_tenant_actor_is_rejected_by_uc05_itself(uc05_worker):
    """The actor's client_id does not match EMP-TEST-001's real tenant (CLIENT-TEST-A) -- UC05's
    own tenant isolation must reject this, never silently schedule across tenants."""
    deps = OrchestratorDeps(ctx=None, worker_pool=_SingleWorkerPool(uc05_worker), uc03_repo_root=None)
    state = _state(client_id="SOME-OTHER-TENANT")
    result = call_uc05_orientation(state, deps)
    assert result["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] in (
        StageStatus.FAILED.value, StageStatus.BLOCKED.value,
    )
    assert result.get("error_code") is not None or result.get("blockers")


def test_uc05_itself_refuses_another_tenants_offer_event_even_without_the_preflight(uc05_worker):
    """The preflight is the orchestrator's early check; UC05's own intake (tenant-checked against the
    directory record) refuses the same request when it is sent directly."""
    from hr_onboarding_common.orchestration.contracts import Actor

    other = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], client_id="CLIENT-TEST-B")
    response = uc05_worker.call("run_event", {"event": {"event": "offer_accepted", "event_id": "EVT-001"},
                                              "instance": "EVT-001"}, actor=other)
    assert not response.ok and response.error_code == "POLICY_DENIED"
    assert response.error_type == "TenantMismatchError" and "Traceback" not in (response.error_detail or "")


# ============================================================ unit tests: recording UC05 double

HR = {"on_behalf_of": "user:hr-admin-01", "roles": ["hr_admin"], "client_id": "CLIENT-TEST-A"}


def _deps(worker):
    return OrchestratorDeps(ctx=None, worker_pool=Pool(uc05=worker), uc03_repo_root=None)


def _after(*complete, **fields):
    state = _state()
    for stage in complete:
        state["stage_statuses"][stage.value] = StageStatus.COMPLETE.value
    state["orientation_schedule_result"] = {"uc05_case": True, "uc05_start_date": "2026-12-01"}
    state.update(fields)
    return state


INDUCTION_PENDING = snapshot(stage="induction_awaiting_approval",
                             sessions=[session("induction", "scheduled")],
                             approvals=[approval("apr-ind", "session_booking", record_id="session-1",
                                                 session_type="induction")])


# ------------------------------------------------------------------ A. orientation scheduling


def test_orientation_preflights_then_sends_the_offer_event_then_reads_back():
    worker = FakeUc05Worker(preflight_offer_event=preflight(), run_event=event_run("induction_awaiting_approval"),
                            case_snapshot=INDUCTION_PENDING)
    result = call_uc05_orientation(_state(), _deps(worker))

    assert worker.ops() == ["preflight_offer_event", "run_event", "case_snapshot"]
    _, event, actor = worker.calls[1]
    assert event == {"event": {"event": "offer_accepted", "event_id": "EVT-001"}, "instance": "EVT-001"}
    assert actor.client_id == "CLIENT-TEST-A" and actor.on_behalf_of == "user:hr-admin-01"  # the real actor
    assert worker.calls[2][1] == {"employee_id": EMPLOYEE}
    assert result["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] == StageStatus.AWAITING_APPROVAL.value
    assert result["orientation_schedule_result"]["approval_id"] == "apr-ind"
    (pending,) = result["pending_approvals"]
    assert pending["reference"] == "apr-ind" and pending["agent"] == "employee-onboarding"
    assert pending["stage"] == Stage.ORIENTATION_SCHEDULING.value


def test_confirmed_induction_completes_and_a_resume_reads_without_resending():
    confirmed = snapshot(sessions=[session("induction", "confirmed")],
                         approvals=[approval("apr-ind", "session_booking", status="approved", record_id="session-1")])
    worker = FakeUc05Worker(case_snapshot=confirmed)
    result = call_uc05_orientation(_after(), _deps(worker))
    assert worker.ops() == ["case_snapshot"]  # UC05 already has the case: read only, no event
    assert result["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] == StageStatus.COMPLETE.value


@pytest.mark.parametrize("pre, code", [
    (preflight(status="event_not_found", employee_id=None, joining_date=None), "UC05_OFFER_EVENT_NOT_FOUND"),
    (preflight(status="malformed_event", employee_id=None, joining_date=None), "UC05_OFFER_EVENT_MALFORMED"),
    (preflight(status="employee_not_in_directory", employee_id=None, joining_date=None),
     "EMPLOYEE_NOT_IN_UC05_DIRECTORY"),
    (preflight(status="incomplete_employee_data", missing_fields=["team", "team_lead_id"]), "INCOMPLETE_EMPLOYEE_DATA"),
    (preflight(employee_id="EMP-TEST-002"), "IDENTITY_MISMATCH"),
    (preflight(joining_date="2026-12-08"), "JOINING_DATE_MISMATCH"),
])
def test_invalid_employee_context_blocks_before_any_uc05_write(pre, code):
    worker = FakeUc05Worker(preflight_offer_event=pre)
    result = call_uc05_orientation(_state(), _deps(worker))
    assert worker.ops() == ["preflight_offer_event"]  # nothing was written in UC05
    assert result["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] == StageStatus.BLOCKED.value
    assert result["stage_errors"][Stage.ORIENTATION_SCHEDULING.value]["code"] == code
    assert any(b["code"] == code for b in result["blockers"])


def test_missing_offer_event_reference_blocks_without_calling_uc05():
    worker = FakeUc05Worker()
    result = call_uc05_orientation(_state(event_id=None), _deps(worker))
    assert worker.ops() == []
    assert result["stage_errors"][Stage.ORIENTATION_SCHEDULING.value]["code"] == "UC05_OFFER_EVENT_MISSING"


def test_actor_of_another_tenant_is_refused_before_uc05_is_called():
    state = _state()
    state["actor"]["client_id"] = "CLIENT-TEST-B"
    worker = FakeUc05Worker()
    result = call_uc05_orientation(state, _deps(worker))
    assert worker.ops() == [] and result["error_code"] == "POLICY_BREACH"


@pytest.mark.parametrize("error, code", [("NoAvailableSlotError", "NO_AVAILABLE_SLOT"),
                                         ("CalendarConflictError", "SCHEDULING_CONFLICT")])
def test_scheduling_failure_is_blocked_with_its_cause_and_redelivered_on_resume(error, code):
    no_session = snapshot(stage="checklist_ready")
    worker = FakeUc05Worker(preflight_offer_event=preflight(),
                            run_event=[event_run("scheduling_failed", errors=[{"stage": "scheduling_failed",
                                                                               "error": error, "detail": "x"}]),
                                       event_run("induction_awaiting_approval")],
                            case_snapshot=[no_session, no_session, INDUCTION_PENDING])
    blocked = call_uc05_orientation(_state(), _deps(worker))
    assert worker.ops() == ["preflight_offer_event", "run_event", "case_snapshot"]  # not resent in the same tick
    assert blocked["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] == StageStatus.BLOCKED.value
    assert blocked["stage_errors"][Stage.ORIENTATION_SCHEDULING.value] == {
        "code": code, "detail": f"UC05 could not draft the induction: {error}", "retryable": True}

    resumed = call_uc05_orientation(blocked, _deps(worker))  # calendar freed: UC05 accepts the event again
    assert worker.ops()[3:] == ["case_snapshot", "run_event", "case_snapshot"]
    assert resumed["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] == StageStatus.AWAITING_APPROVAL.value


def test_rejected_induction_is_blocked_and_never_redelivered():
    rejected = snapshot(sessions=[session("induction", "cancelled")],
                        approvals=[approval("apr-ind", "session_booking", status="rejected", record_id="session-1")])
    worker = FakeUc05Worker(case_snapshot=rejected)
    result = call_uc05_orientation(_after(), _deps(worker))
    assert worker.ops() == ["case_snapshot"]
    assert result["stage_errors"][Stage.ORIENTATION_SCHEDULING.value]["code"] == "INDUCTION_REJECTED"


@pytest.mark.parametrize("exc", [WorkerCrashedError("pipe closed"), WorkerTimeoutError("no response")])
def test_worker_lost_during_the_offer_event_is_uncertain_and_never_resent(exc):
    worker = FakeUc05Worker(preflight_offer_event=preflight(), run_event=exc,
                            case_snapshot=snapshot(found=False, client_id=None, case_status=None, start_date=None))
    uncertain = call_uc05_orientation(_state(), _deps(worker))
    assert uncertain["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] == StageStatus.UNCERTAIN.value
    assert uncertain["next_action"] == "ops_review:orientation_scheduling:uncertain_outcome"

    again = call_uc05_orientation(uncertain, _deps(worker))  # resume: re-query; UC05 has nothing
    assert worker.ops().count("run_event") == 1  # the event was NOT sent a second time
    assert again["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] == StageStatus.UNCERTAIN.value


def test_unsettled_booking_keeps_an_uncertain_stage_uncertain_without_writing():
    unsettled = snapshot(operations=[{"key": f"calendar:CLIENT-TEST-A:{EMPLOYEE}:induction:2026-12-01T05:00:00+00:00",
                                      "kind": "calendar_booking", "status": "uncertain", "last_error": "Timeout",
                                      "attempts": 1}])
    state = _after()
    state["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] = StageStatus.UNCERTAIN.value
    worker = FakeUc05Worker(case_snapshot=unsettled)
    result = call_uc05_orientation(state, _deps(worker))
    assert worker.ops() == ["case_snapshot"]
    assert result["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] == StageStatus.UNCERTAIN.value
    assert result["stage_errors"][Stage.ORIENTATION_SCHEDULING.value]["code"] == "INDUCTION_BOOKING_UNCERTAIN"


def test_a_status_outside_uc05s_contract_fails_the_stage_instead_of_guessing():
    bad = snapshot(sessions=[{**session("induction", "confirmed"), "status": "half_confirmed"}])
    result = call_uc05_orientation(_after(), _deps(FakeUc05Worker(case_snapshot=bad)))
    assert result["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] == StageStatus.FAILED.value
    assert result["stage_errors"][Stage.ORIENTATION_SCHEDULING.value]["code"] == "UC05_CONTRACT_VIOLATION"


def test_retryable_provider_failure_is_resumable_not_terminal():
    worker = FakeUc05Worker(preflight_offer_event=preflight(),
                            run_event=failure("RETRYABLE_PROVIDER_ERROR", "calendar down", "CalendarBookingError"))
    result = call_uc05_orientation(_state(), _deps(worker))
    assert result["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] == StageStatus.BLOCKED.value
    assert result["stage_errors"][Stage.ORIENTATION_SCHEDULING.value]["retryable"] is True
    assert result["orientation_schedule_result"]["offer_event"]["outcome"] == "provider_error"


# ------------------------------------------------------------------ B. buddy assignment


def test_pending_buddy_proposal_awaits_approval_without_proposing_again():
    snap = snapshot(sessions=[session("induction", "confirmed")],
                    approvals=[approval("apr-bud", "buddy_assignment", gate="buddy_assignment", buddy_id="EMP-TEST-050")])
    worker = FakeUc05Worker(case_snapshot=snap)
    result = call_uc05_buddy_assignment(_after(Stage.ORIENTATION_SCHEDULING), _deps(worker))
    assert worker.ops() == ["case_snapshot"]
    assert result["stage_statuses"][Stage.BUDDY_ASSIGNMENT.value] == StageStatus.AWAITING_APPROVAL.value
    assert result["buddy_assignment_result"]["proposed_buddy_id"] == "EMP-TEST-050"


def test_no_buddy_decision_asks_uc05_to_propose_and_reports_no_candidates():
    worker = FakeUc05Worker(case_snapshot=snapshot(sessions=[session("induction", "confirmed")]),
                            propose_buddy={"outcome": "no_candidates", "detail": "no buddy candidates"})
    result = call_uc05_buddy_assignment(_after(Stage.ORIENTATION_SCHEDULING), _deps(worker))
    assert worker.ops() == ["case_snapshot", "propose_buddy"]
    assert worker.calls[1][1] == {"employee_id": EMPLOYEE}
    assert result["stage_errors"][Stage.BUDDY_ASSIGNMENT.value]["code"] == "NO_BUDDY_CANDIDATES"
    assert result["stage_errors"][Stage.BUDDY_ASSIGNMENT.value]["retryable"] is True


@pytest.mark.parametrize("item, code", [
    (approval("apr-bud", "buddy_assignment", status="edited", gate="buddy_assignment", buddy_id="EMP-110",
              apply_error="IneligibleBuddyError"), "BUDDY_INELIGIBLE"),
    (approval("apr-bud", "buddy_assignment", status="rejected", gate="buddy_assignment"), "BUDDY_REJECTED"),
])
def test_ineligible_or_rejected_buddy_blocks_and_is_not_re_proposed_automatically(item, code):
    worker = FakeUc05Worker(case_snapshot=snapshot(sessions=[session("induction", "confirmed")], approvals=[item]))
    result = call_uc05_buddy_assignment(_after(Stage.ORIENTATION_SCHEDULING), _deps(worker))
    assert "propose_buddy" not in worker.ops()
    assert result["stage_errors"][Stage.BUDDY_ASSIGNMENT.value]["code"] == code


def test_assigned_buddy_completes():
    worker = FakeUc05Worker(case_snapshot=snapshot(buddy_id="EMP-TEST-050"))
    result = call_uc05_buddy_assignment(_after(Stage.ORIENTATION_SCHEDULING), _deps(worker))
    assert result["stage_statuses"][Stage.BUDDY_ASSIGNMENT.value] == StageStatus.COMPLETE.value
    assert result["buddy_assignment_result"]["buddy_id"] == "EMP-TEST-050"


def test_buddy_stage_never_runs_before_the_induction_is_confirmed():
    worker = FakeUc05Worker()
    result = call_uc05_buddy_assignment(_after(), _deps(worker))
    assert worker.ops() == []
    assert result["stage_errors"][Stage.BUDDY_ASSIGNMENT.value]["code"] == "PREREQUISITE_NOT_MET"


# ------------------------------------------------------------------ C. team and buddy introductions


def test_team_intro_awaits_the_team_lead_and_completes_once_confirmed():
    intro = approval("apr-ti", "session_booking", record_id="session-2", approver_role="team_lead",
                     session_type="team_intro")
    pending = snapshot(buddy_id="EMP-TEST-050", sessions=[session("induction", "confirmed"),
                                                          session("team_intro", "scheduled", "session-2")],
                       approvals=[intro])
    done = snapshot(buddy_id="EMP-TEST-050", sessions=[session("team_intro", "confirmed", "session-2")],
                    approvals=[{**intro, "status": "approved"}])
    state = _after(Stage.ORIENTATION_SCHEDULING, Stage.BUDDY_ASSIGNMENT)
    worker = FakeUc05Worker(case_snapshot=[pending, done])

    awaiting = call_uc05_team_introduction(state, _deps(worker))
    assert awaiting["stage_statuses"][Stage.TEAM_INTRODUCTION.value] == StageStatus.AWAITING_APPROVAL.value
    assert awaiting["pending_approvals"][0]["role"] == "team_lead"
    assert awaiting["next_action"] == "approval:team_introduction:team_lead"
    assert awaiting["stage_statuses"][Stage.BUDDY_INTRODUCTION.value] == StageStatus.NOT_REQUESTED.value

    awaiting["pending_approvals"] = [p for p in awaiting["pending_approvals"]
                                     if p.get("stage") != Stage.TEAM_INTRODUCTION.value]  # the graph wrapper's reset
    complete = call_uc05_team_introduction(awaiting, _deps(worker))
    assert complete["stage_statuses"][Stage.TEAM_INTRODUCTION.value] == StageStatus.COMPLETE.value
    assert complete["pending_approvals"] == []


def test_team_intro_decided_by_another_team_lead_is_blocked_not_retryable():
    intro = approval("apr-ti", "session_booking", status="approved", record_id="session-2", approver_role="team_lead",
                     session_type="team_intro", apply_error="UnauthorizedApproverError")
    snap = snapshot(buddy_id="EMP-TEST-050", sessions=[session("team_intro", "scheduled", "session-2")],
                    approvals=[intro])
    result = call_uc05_team_introduction(_after(Stage.ORIENTATION_SCHEDULING, Stage.BUDDY_ASSIGNMENT),
                                         _deps(FakeUc05Worker(case_snapshot=snap)))
    assert result["stage_errors"][Stage.TEAM_INTRODUCTION.value] == {
        "code": "UNAUTHORIZED_APPROVER", "detail": "approval apr-ti: UnauthorizedApproverError: detail",
        "retryable": False}


def test_team_intro_drafting_failure_from_the_decision_run_is_reported():
    state = _after(Stage.ORIENTATION_SCHEDULING, Stage.BUDDY_ASSIGNMENT,
                   approval_decisions=[{"agent": "employee-onboarding", "reference": "apr-bud",
                                        "stage": "buddy_assignment", "run_stage": "scheduling_failed",
                                        "run_meeting_type": "team_intro",
                                        "run_errors": [{"error": "NoAvailableSlotError", "detail": "x"}]}])
    result = call_uc05_team_introduction(state, _deps(FakeUc05Worker(case_snapshot=snapshot(buddy_id="EMP-TEST-050"))))
    assert result["stage_errors"][Stage.TEAM_INTRODUCTION.value]["code"] == "NO_AVAILABLE_SLOT"


def test_buddy_intro_is_optional_and_follows_its_own_session():
    buddy_intro = approval("apr-bi", "session_booking", record_id="session-3", approver_role="buddy",
                           session_type="buddy_intro")
    snap = snapshot(buddy_id="EMP-TEST-050", sessions=[session("team_intro", "confirmed", "session-2"),
                                                       session("buddy_intro", "scheduled", "session-3")],
                    approvals=[buddy_intro])
    result = call_uc05_team_introduction(_after(Stage.ORIENTATION_SCHEDULING, Stage.BUDDY_ASSIGNMENT),
                                         _deps(FakeUc05Worker(case_snapshot=snap)))
    assert result["stage_statuses"][Stage.TEAM_INTRODUCTION.value] == StageStatus.COMPLETE.value
    assert result["stage_statuses"][Stage.BUDDY_INTRODUCTION.value] == StageStatus.AWAITING_APPROVAL.value
    assert any(p["reference"] == "apr-bi" and p["role"] == "buddy" for p in result["pending_approvals"])


# ------------------------------------------------------------------ D. joining-day welcome


def _ready(**fields):
    return _after(Stage.ORIENTATION_SCHEDULING, Stage.BUDDY_ASSIGNMENT, Stage.TEAM_INTRODUCTION,
                  Stage.DAY_ONE_READINESS, **fields)


def test_welcome_waits_for_the_joining_day_event_without_calling_uc05():
    worker = FakeUc05Worker()
    result = call_uc05_joining_day_welcome(_ready(), _deps(worker))
    assert worker.ops() == []
    assert result["stage_statuses"][Stage.JOINING_DAY_WELCOME.value] == StageStatus.WAITING.value
    assert result["next_action"] == "await_joining_day:2026-12-01"


def test_welcome_is_never_drafted_before_day_one_readiness_is_signed_off():
    state = _after(Stage.ORIENTATION_SCHEDULING, Stage.BUDDY_ASSIGNMENT, Stage.TEAM_INTRODUCTION,
                   joining_day_event={"as_of": "2026-12-01"})
    worker = FakeUc05Worker()
    result = call_uc05_joining_day_welcome(state, _deps(worker))
    assert worker.ops() == []
    assert result["stage_errors"][Stage.JOINING_DAY_WELCOME.value]["code"] == "PREREQUISITE_NOT_MET"


def test_joining_day_runs_uc05s_trigger_for_the_start_date_and_awaits_hr():
    run = event_run("welcome_awaiting_approval", actions=[{"tool": "joining_trigger.run", "detail": {
        "results": [{"employee_id": EMPLOYEE, "status": "drafted", "draft_id": "comm-1", "approval_id": "apr-w"}]}}])
    drafted = snapshot(buddy_id="EMP-TEST-050",
                       welcome_drafts=[{"draft_id": "comm-1", "status": "draft", "language": "en",
                                        "created_at": "2026-12-01T05:00:00+00:00"}],
                       approvals=[approval("apr-w", "welcome_message", gate="comms_approval", record_id="comm-1")])
    worker = FakeUc05Worker(case_snapshot=[snapshot(), drafted], run_event=run)
    result = call_uc05_joining_day_welcome(_ready(joining_day_event={"as_of": "2026-12-01"}), _deps(worker))
    assert worker.ops() == ["case_snapshot", "run_event", "case_snapshot"]
    assert worker.calls[1][1] == {"event": {"event": "joining_day", "as_of": "2026-12-01"},
                                  "instance": "joining-day:2026-12-01"}
    assert result["stage_statuses"][Stage.JOINING_DAY_WELCOME.value] == StageStatus.AWAITING_APPROVAL.value
    assert result["welcome_message_result"]["approval_id"] == "apr-w"


def test_joining_day_run_that_does_not_include_the_employee_is_a_start_date_mismatch():
    run = event_run("nothing_to_do", actions=[{"tool": "joining_trigger.run", "detail": {"results": []}}])
    worker = FakeUc05Worker(case_snapshot=snapshot(), run_event=run)
    result = call_uc05_joining_day_welcome(_ready(joining_day_event={"as_of": "2026-12-01"}), _deps(worker))
    assert result["stage_errors"][Stage.JOINING_DAY_WELCOME.value]["code"] == "UC05_START_DATE_MISMATCH"


@pytest.mark.parametrize("draft_status, op_status, expected, code", [
    ("sent", "succeeded", StageStatus.COMPLETE, None),
    ("approved", "succeeded", StageStatus.COMPLETE, None),  # an operator confirmed the delivery
    ("approved", "uncertain", StageStatus.UNCERTAIN, "WELCOME_DELIVERY_UNCERTAIN"),
    ("approved", "rejected", StageStatus.BLOCKED, "WELCOME_DELIVERY_REJECTED"),
    ("approved", "released", StageStatus.BLOCKED, "WELCOME_NOT_DELIVERED"),
    ("rejected", None, StageStatus.BLOCKED, "WELCOME_REJECTED"),
])
def test_welcome_outcome_follows_the_draft_and_the_delivery_ledger(draft_status, op_status, expected, code):
    operations = [] if op_status is None else [{"key": "welcome:CLIENT-TEST-A:comm-1", "kind": "notification",
                                                "status": op_status, "last_error": None, "attempts": 1}]
    snap = snapshot(welcome_drafts=[{"draft_id": "comm-1", "status": draft_status, "language": "en",
                                     "created_at": "2026-12-01T05:00:00+00:00"}], operations=operations)
    worker = FakeUc05Worker(case_snapshot=snap)
    result = call_uc05_joining_day_welcome(_ready(joining_day_event={"as_of": "2026-12-01"}), _deps(worker))
    assert "run_event" not in worker.ops()  # a draft exists: never drafted (or sent) again
    assert result["stage_statuses"][Stage.JOINING_DAY_WELCOME.value] == expected.value
    if code:
        assert result["stage_errors"][Stage.JOINING_DAY_WELCOME.value]["code"] == code


# ------------------------------------------------------------------ relays


def _awaiting_induction():
    state = _after()
    state["pending_approvals"] = [{"agent": "employee-onboarding", "reference": "apr-ind", "role": "hr_admin",
                                   "description": "x", "stage": Stage.ORIENTATION_SCHEDULING.value}]
    return state


def test_decision_relay_passes_the_approvers_own_identity_and_the_case_employee():
    decided = approval("apr-ind", "session_booking", status="approved", record_id="session-1")
    worker = FakeUc05Worker(decide_approval=decision_outcome(decided, run_stage="buddy_awaiting_approval",
                                                             meeting_type="induction"))
    approver = {"on_behalf_of": "user:hr-admin-02", "roles": ["hr_admin"], "client_id": "CLIENT-TEST-A"}
    result = relay_uc05_decision(_awaiting_induction(), _deps(worker), approval_id="apr-ind", decision="approved",
                                 approver=approver)
    op, payload, actor = worker.calls[0]
    assert op == "decide_approval" and payload["expected_subject_id"] == EMPLOYEE
    assert payload["decision"] == "approved" and actor.on_behalf_of == "user:hr-admin-02"
    (record,) = result["approval_decisions"]
    assert record["reference"] == "apr-ind" and record["applied"] is True
    assert record["run_stage"] == "buddy_awaiting_approval"


def test_decision_relay_refuses_an_approval_that_is_not_this_cases():
    worker = FakeUc05Worker()
    with pytest.raises(KeyError):
        relay_uc05_decision(_awaiting_induction(), _deps(worker), approval_id="apr-someone-else", decision="approved",
                            approver=HR)
    assert worker.ops() == []


def test_decision_refused_by_uc05_is_raised_not_recorded():
    worker = FakeUc05Worker(decide_approval=failure("POLICY_DENIED", "user lacks role 'hr_admin'"))
    with pytest.raises(Uc05RelayError) as err:
        relay_uc05_decision(_awaiting_induction(), _deps(worker), approval_id="apr-ind", decision="approved",
                            approver={**HR, "roles": ["employee"]})
    assert err.value.code == "POLICY_DENIED"


def test_decision_relay_lost_mid_call_marks_the_stage_uncertain():
    worker = FakeUc05Worker(decide_approval=WorkerTimeoutError("no response"))
    result = relay_uc05_decision(_awaiting_induction(), _deps(worker), approval_id="apr-ind", decision="approved",
                                 approver=HR)
    assert result["stage_statuses"][Stage.ORIENTATION_SCHEDULING.value] == StageStatus.UNCERTAIN.value


def test_retry_relay_uses_uc05s_retry_apply():
    state = _after(approval_decisions=[{"agent": "employee-onboarding", "reference": "apr-w",
                                        "stage": Stage.JOINING_DAY_WELCOME.value}])
    decided = approval("apr-w", "welcome_message", status="approved", gate="comms_approval", record_id="comm-1")
    worker = FakeUc05Worker(retry_apply=decision_outcome(decided, run_stage="welcome_sent"))
    result = relay_uc05_retry(state, _deps(worker), approval_id="apr-w", actor=HR)
    assert worker.ops() == ["retry_apply"] and result["approval_decisions"][-1]["kind"] == "retry_apply"


@pytest.mark.parametrize("operation, params, complete, message", [
    ("schedule_buddy_intro", {"start": "2026-12-02T10:00:00+04:00"}, (Stage.ORIENTATION_SCHEDULING,), "assigned buddy"),
    ("reschedule_session", {"session_type": "team_intro", "start": "2026-12-02T10:00:00+04:00"},
     (Stage.ORIENTATION_SCHEDULING,), "follows buddy assignment"),
    ("reschedule_session", {"session_type": "induction", "start": "2026-12-02T10:00:00"}, (), "timezone"),
    ("redraft_welcome", {}, (), "joining-day event"),
    ("resolve_operation", {"key": "calendar:CLIENT-TEST-A:EMP-TEST-999:induction:x", "outcome": "released"}, (),
     "does not belong"),
])
def test_hr_operations_check_their_uc05_prerequisites_before_calling_uc05(operation, params, complete, message):
    worker = FakeUc05Worker()
    with pytest.raises(PrerequisiteNotMetError, match=message):
        request_uc05_operation(_after(*complete), _deps(worker), operation=operation, params=params, actor=HR)
    assert worker.ops() == []


def test_buddy_intro_request_sends_the_hr_chosen_start_to_uc05():
    worker = FakeUc05Worker(schedule_session={"outcome": "scheduled", "session_id": "session-3", "status": "scheduled",
                                              "approval_id": "apr-bi", "approver_role": "buddy"})
    state, outcome = request_uc05_operation(
        _after(Stage.ORIENTATION_SCHEDULING, Stage.BUDDY_ASSIGNMENT), _deps(worker), operation="schedule_buddy_intro",
        params={"start": "2026-12-02T10:00:00+04:00"}, actor=HR)
    assert worker.calls[0][1] == {"employee_id": EMPLOYEE, "session_type": "buddy_intro",
                                  "start": "2026-12-02T10:00:00+04:00"}
    assert outcome["outcome"] == "scheduled"
    assert state["stage_statuses"][Stage.BUDDY_INTRODUCTION.value] == StageStatus.AWAITING_APPROVAL.value


def test_decision_relay_names_the_activity_the_case_expects():
    state = _awaiting_induction()
    state["pending_approvals"][0].update(proposal_type="session_booking", record_id="session-1")
    decided = approval("apr-ind", "session_booking", status="approved", record_id="session-1")
    worker = FakeUc05Worker(decide_approval=decision_outcome(decided))
    relay_uc05_decision(state, _deps(worker), approval_id="apr-ind", decision="approved", approver=HR)
    payload = worker.calls[0][1]
    assert payload["expected_proposal_type"] == "session_booking" and payload["expected_record_id"] == "session-1"


@pytest.mark.parametrize("error, code", [("MissingClientConfigError", "UC05_CLIENT_CONFIG_MISSING"),
                                         ("InvalidClientConfigError", "UC05_CLIENT_CONFIG_INVALID")])
def test_welcome_not_configured_is_mapped_by_uc05s_error_type(error, code):
    run = event_run("welcome_not_configured", errors=[{"stage": "welcome_not_configured", "error": error,
                                                       "detail": "no welcome configuration ..."}])
    worker = FakeUc05Worker(case_snapshot=snapshot(), run_event=run)
    result = call_uc05_joining_day_welcome(_ready(joining_day_event={"as_of": "2026-12-01"}), _deps(worker))
    assert result["stage_errors"][Stage.JOINING_DAY_WELCOME.value]["code"] == code
    assert result["stage_errors"][Stage.JOINING_DAY_WELCOME.value]["retryable"] is True
