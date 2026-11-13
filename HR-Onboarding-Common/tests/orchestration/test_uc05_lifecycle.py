"""Full-lifecycle orchestration tests: the real orchestrator graph driving the REAL UC05 worker
(UC05's own code, its simulated calendar/notifier/directory, FakeLLM) through orientation, buddy
assignment, team introduction, day-1 signoff and the joining-day welcome. UC01-UC04 are
contract-compatible doubles (see _lifecycle.py) -- these tests verify the orchestration and the
UC05 integration, not a real UC01-UC04 integration.

Every test asserts what ran, what did not, what the case state holds, and whether a UC05 side
effect (calendar booking, message, approval) happened more than once."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from _lifecycle import (BUDDY, BUDDY_CANDIDATE, CLIENT, CLIENT_CONFIG, EMPLOYEE, HR, HR_2, JOINING, OTHER_TENANT_HR,
                        SCHEDULER, TEAM_LEAD, World, code, start_uc05_worker, status)
from hr_onboarding_common.orchestration.adapters._shared import OrchestratorAuthorizationError
from hr_onboarding_common.orchestration.adapters.uc05_orientation import PrerequisiteNotMetError, Uc05RelayError
from hr_onboarding_common.orchestration.case_store import SqliteCaseStateStore
from hr_onboarding_common.orchestration.contracts import Actor
from hr_onboarding_common.orchestration.orchestrator_agent import PrematureLifecycleEventError
from hr_onboarding_common.orchestration.state import Stage, StageStatus
from hr_onboarding_common.orchestration.workers.uc_worker_client import WorkerTimeoutError

S = Stage


_uc05_worker = start_uc05_worker


@pytest.fixture(scope="module")
def uc05():
    worker = _uc05_worker()
    yield worker
    worker.stop()


@pytest.fixture
def world(uc05):
    w = World(uc05)
    w.hook("reset")  # a fresh UC05 world (its synthetic directory has no buddy pool for CLIENT-TEST-A)
    w.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    w.hook("add_client_config", config=CLIENT_CONFIG)
    return w


@pytest.fixture
def world_without_buddies(uc05):
    w = World(uc05)
    w.hook("reset")
    return w


# ============================================================ 1. the whole lifecycle


def test_successful_onboarding_through_every_stage(world):
    state = world.start()
    case_id = state["case_id"]
    for stage in (S.INTAKE, S.PLAN_TASKS, S.VALIDATE_DOCUMENTS, S.EQUIPMENT_ACCESS):
        assert status(state, stage) == StageStatus.COMPLETE.value
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.AWAITING_APPROVAL.value
    assert state["orientation_schedule_result"]["session_status"] == "scheduled"  # a tentative hold only
    assert world.hook("counts")["calendar_invited"] == 0  # nobody invited before HR approves

    state = world.decide(state, S.ORIENTATION_SCHEDULING)  # HR approves the induction
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.COMPLETE.value
    assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.AWAITING_APPROVAL.value
    assert state["buddy_assignment_result"]["proposed_buddy_id"] == BUDDY["on_behalf_of"]

    state = world.decide(state, S.BUDDY_ASSIGNMENT)  # HR approves the buddy
    assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.COMPLETE.value
    assert status(state, S.TEAM_INTRODUCTION) == StageStatus.AWAITING_APPROVAL.value
    assert [p["role"] for p in state["pending_approvals"] if p.get("stage") == S.TEAM_INTRODUCTION.value] == ["team_lead"]

    state = world.decide(state, S.TEAM_INTRODUCTION, actor=TEAM_LEAD)  # the team lead on record approves
    assert status(state, S.TEAM_INTRODUCTION) == StageStatus.COMPLETE.value
    assert status(state, S.DAY_ONE_READINESS) == StageStatus.AWAITING_APPROVAL.value
    assert status(state, S.JOINING_DAY_WELCOME) == StageStatus.PENDING.value  # nothing drafted before signoff

    state = world.agent.decide_approval(case_id, world.gate_item(case_id, "day_one_signoff"), decision="approved",
                                        actor=HR_2)
    assert status(state, S.DAY_ONE_READINESS) == StageStatus.COMPLETE.value
    assert status(state, S.JOINING_DAY_WELCOME) == StageStatus.WAITING.value
    assert state["overall_status"] == "in_progress" and world.uc05_snapshot()["welcome_drafts"] == []

    state = world.agent.trigger_joining_day(case_id, as_of=JOINING, actor=SCHEDULER)
    assert status(state, S.JOINING_DAY_WELCOME) == StageStatus.AWAITING_APPROVAL.value
    assert world.hook("counts")["messages_by_kind"].get("welcome", 0) == 0  # drafted, not sent

    state = world.decide(state, S.JOINING_DAY_WELCOME)  # HR approves the welcome
    assert status(state, S.JOINING_DAY_WELCOME) == StageStatus.COMPLETE.value
    assert state["overall_status"] == "complete" and status(state, S.COMPLETE) == StageStatus.COMPLETE.value
    assert state["pending_approvals"] == [] and state["blockers"] == [] and state["stage_errors"] == {}

    counts = world.hook("counts")
    assert counts["calendar_live"] == 2  # induction + team introduction, each booked once
    assert counts["messages_by_kind"]["welcome"] == 1 and counts["messages_by_kind"]["buddy_assignment"] == 2
    # Completed stages were never re-executed across seven advances of the same case.
    assert (world.uc01.calls, world.plan_calls, world.docs_calls, world.uc04.calls) == (1, 1, 1, 1)
    assert [d["decision"] for d in state["approval_decisions"]] == ["approved"] * 5
    transitions = [(e["stage"], e["status"]) for e in state["stage_history"]]
    assert (S.ORIENTATION_SCHEDULING.value, "awaiting_approval") in transitions
    assert (S.JOINING_DAY_WELCOME.value, "complete") in transitions
    # No intake-only personal data is kept once intake is done.
    assert "id_number" not in state["intake_trigger"] and "candidate_email" not in state["intake_trigger"]
    assert "alex.morgan@example.com" not in json.dumps(state)


# ============================================================ 2-6. prerequisites block what depends on them


@pytest.mark.parametrize("uc01_kwargs, start_kwargs, expected_code", [
    ({"joining_date": None}, {}, "MISSING_DATA"),
    ({}, {"offer_acceptance_event_id": None}, "UC05_OFFER_EVENT_MISSING"),
    ({"employee_id": "EMP-TEST-002"}, {}, "IDENTITY_MISMATCH"),
    ({"joining_date": "2026-12-08"}, {}, "JOINING_DATE_MISMATCH"),
    ({}, {"offer_acceptance_event_id": "EVT-DOES-NOT-EXIST"}, "UC05_OFFER_EVENT_NOT_FOUND"),
    ({"employee_id": "EMP-TEST-003", "joining_date": "2026-12-15"}, {"offer_acceptance_event_id": "EVT-002"},
     "INCOMPLETE_EMPLOYEE_DATA"),
])
def test_missing_or_invalid_employee_context_blocks_orientation_and_writes_nothing(uc05, uc01_kwargs, start_kwargs,
                                                                                   expected_code):
    from _lifecycle import FakeUc01Worker

    world = World(uc05, uc01=FakeUc01Worker(**uc01_kwargs))
    world.hook("reset")
    state = world.start(**start_kwargs)
    if expected_code == "MISSING_DATA":
        # No joining date at all: plan_tasks (a required, sequential prerequisite of
        # orientation_scheduling) is blocked by the very same missing field first, so
        # orientation_scheduling never even starts -- it stays PENDING, not BLOCKED.
        assert status(state, S.PLAN_TASKS) == StageStatus.BLOCKED.value
        assert code(state, S.PLAN_TASKS) == expected_code
        assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.PENDING.value
    else:
        assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.BLOCKED.value
        assert code(state, S.ORIENTATION_SCHEDULING) == expected_code
    assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.PENDING.value
    assert world.hook("counts")["calendar_live"] == 0 and world.hook("counts")["messages_by_kind"] == {}
    assert state["overall_status"] == "blocked"


def test_the_real_uc02_refusing_to_plan_blocks_documents_uc04_and_uc05(uc05):
    """UC02 plans right after intake and is a sequential prerequisite of everything after it: the real UC02
    refuses an intake without offer/form approval facts (BLOCKED, MISSING_DATA), so UC03 collects nothing
    and neither UC04 nor UC05 is ever called."""
    world = World(uc05, real_uc02=True)  # the real UC02 adapter and package
    world.hook("reset")
    state = world.start()
    assert status(state, S.PLAN_TASKS) == StageStatus.BLOCKED.value and state["error_code"] == "MISSING_DATA"
    for stage in (S.DOCUMENT_COLLECTION, S.VALIDATE_DOCUMENTS, S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING):
        assert status(state, stage) == StageStatus.PENDING.value
    assert world.docs_calls == 0 and world.uc04.calls == 0 and world.uc05_snapshot()["found"] is False
    assert state["workflow_status"] == "blocked"
    assert status(state, S.DAY_ONE_READINESS) == StageStatus.PENDING.value


def test_uc02_precondition_failure_blocks_planning_and_the_stages_behind_it(world):
    world.plan_ok = False
    state = world.start()
    assert status(state, S.PLAN_TASKS) == StageStatus.BLOCKED.value and world.docs_calls == 0  # UC02 comes first
    assert status(state, S.DOCUMENT_COLLECTION) == StageStatus.PENDING.value  # UC03 collects nothing yet
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.PENDING.value  # UC05 never started
    assert world.uc04.calls == 0 and world.uc05_snapshot()["found"] is False
    world.plan_ok = True
    state = world.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.PLAN_TASKS) == StageStatus.COMPLETE.value
    state = world.supply_documents(state)  # UC03 now collects; the documents arrive
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.AWAITING_APPROVAL.value
    assert world.uc01.calls == 1  # intake was not re-executed on resume
    assert len(world.uc05_snapshot()["sessions"]) == 1 and world.uc04.calls == 1


def test_invalid_documents_block_dependent_stages_and_corrected_documents_resume(world):
    world.docs_valid = False
    state = world.start()
    assert status(state, S.VALIDATE_DOCUMENTS) == StageStatus.WAITING_FOR_INPUT.value
    assert state["current_stage"] == S.VALIDATE_DOCUMENTS.value
    assert [r["document_type"] for r in state["correction_requirements"]] == ["passport"]
    assert world.uc04.calls == 0 and world.uc05_snapshot()["found"] is False  # nothing downstream ran

    world.docs_valid = True
    state = world.supply_documents(state, ["passport"], tag="2")  # the candidate re-submits a valid passport
    assert status(state, S.VALIDATE_DOCUMENTS) == StageStatus.COMPLETE.value
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.AWAITING_APPROVAL.value
    assert not [b for b in state["blockers"] if b["stage"] == S.VALIDATE_DOCUMENTS.value]  # stale blocker cleared
    assert (world.uc01.calls, world.plan_calls, world.docs_calls) == (1, 1, 2)


def test_equipment_access_awaiting_approval_does_not_hold_up_uc05(uc05):
    from _lifecycle import FakeUc04Worker

    world = World(uc05, uc04=FakeUc04Worker("AWAITING_APPROVAL"))
    world.hook("reset")
    state = world.start()
    assert status(state, S.EQUIPMENT_ACCESS) == StageStatus.AWAITING_APPROVAL.value
    assert any(p["agent"] == "dact-uc04-equipment-access" for p in state["pending_approvals"])
    assert world.uc05_snapshot()["found"] is True  # UC05 ran in parallel
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.AWAITING_APPROVAL.value
    assert state["workflow_status"] == "waiting_for_approval" and state["overall_status"] != "complete"
    with pytest.raises(PrerequisiteNotMetError):  # a UC04 approval is decided through UC04, not relayed
        world.agent.decide_approval(state["case_id"], "REQ-1", decision="approved", actor=HR)


# ============================================================ 7-9. scheduling conflicts, buddies, introductions


def test_orientation_conflict_blocks_then_resumes_once_the_calendar_is_free(world):
    world.hook("calendar_busy", attendee="TL-TEST-001", start="2026-12-01T00:00:00+04:00",
               end="2026-12-31T00:00:00+04:00")
    state = world.start()
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.BLOCKED.value
    assert code(state, S.ORIENTATION_SCHEDULING) == "NO_AVAILABLE_SLOT"
    assert state["stage_errors"][S.ORIENTATION_SCHEDULING.value]["retryable"] is True
    assert world.hook("counts")["calendar_live"] == 0

    world.hook("calendar_clear_busy", attendee="TL-TEST-001")
    state = world.agent.resume_case(state["case_id"], actor=HR)  # UC05 accepts the same offer event again
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.AWAITING_APPROVAL.value
    assert world.hook("counts")["calendar_live"] == 1
    assert len(world.uc05_snapshot()["sessions"]) == 1  # one case, one induction


def test_no_eligible_buddy_blocks_until_one_exists(world_without_buddies):
    world = world_without_buddies
    state = world.decide(world.start(), S.ORIENTATION_SCHEDULING)
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.COMPLETE.value
    assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.BLOCKED.value
    assert code(state, S.BUDDY_ASSIGNMENT) == "NO_BUDDY_CANDIDATES"
    assert status(state, S.TEAM_INTRODUCTION) == StageStatus.PENDING.value  # never drafted without a buddy

    world.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    state = world.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.AWAITING_APPROVAL.value
    state = world.decide(state, S.BUDDY_ASSIGNMENT)
    assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.COMPLETE.value
    assert status(state, S.TEAM_INTRODUCTION) == StageStatus.AWAITING_APPROVAL.value


def test_an_ineligible_edited_buddy_is_refused_without_consuming_the_proposal(world):
    """Gap 4: UC05 validates the edited buddy BEFORE the decision is recorded (it used to be recorded
    and then fail to apply, consuming the proposal)."""
    state = world.to_buddy_pending()
    proposal = world.pending(state, S.BUDDY_ASSIGNMENT)
    with pytest.raises(Uc05RelayError) as err:
        world.decide(state, S.BUDDY_ASSIGNMENT, decision="edited", reason_code="accuracy",
                     edited_payload={"employee_id": EMPLOYEE, "buddy_id": "EMP-110"})  # another tenant's employee
    assert err.value.code == "INVALID_INPUT" and "not an active buddy candidate" in err.value.detail
    (item,) = [a for a in world.uc05_snapshot()["approvals"] if a["approval_id"] == proposal]
    assert item["status"] == "pending" and world.uc05_snapshot()["buddy_id"] is None  # still decidable

    state = world.decide(world.agent.get_case(state["case_id"], client_id=CLIENT), S.BUDDY_ASSIGNMENT)
    assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.COMPLETE.value


def test_rejected_buddy_is_not_re_proposed_automatically(world):
    state = world.decide(world.to_buddy_pending(), S.BUDDY_ASSIGNMENT, decision="rejected")
    assert code(state, S.BUDDY_ASSIGNMENT) == "BUDDY_REJECTED"
    state = world.agent.resume_case(state["case_id"], actor=HR)
    buddy_items = [a for a in world.uc05_snapshot()["approvals"] if a["proposal_type"] == "buddy_assignment"]
    assert len(buddy_items) == 1 and code(state, S.BUDDY_ASSIGNMENT) == "BUDDY_REJECTED"


def test_team_introduction_scheduling_failure_blocks_and_hr_reschedules(world):
    state = world.to_buddy_pending()
    world.hook("calendar_busy", attendee="TL-TEST-001", start="2026-12-01T10:30:00+04:00",
               end="2026-12-31T00:00:00+04:00")  # the team lead has no time after the induction
    state = world.decide(state, S.BUDDY_ASSIGNMENT)
    assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.COMPLETE.value
    assert status(state, S.TEAM_INTRODUCTION) == StageStatus.BLOCKED.value
    assert code(state, S.TEAM_INTRODUCTION) == "NO_AVAILABLE_SLOT"
    assert status(state, S.DAY_ONE_READINESS) == StageStatus.PENDING.value

    world.hook("calendar_clear_busy", attendee="TL-TEST-001")
    state, outcome = world.agent.request_uc05_operation(
        state["case_id"], operation="reschedule_session",
        params={"session_type": "team_intro", "start": "2026-12-02T11:00:00+04:00"}, actor=HR)
    assert outcome["outcome"] == "scheduled" and outcome["approver_role"] == "team_lead"
    assert status(state, S.TEAM_INTRODUCTION) == StageStatus.AWAITING_APPROVAL.value
    state = world.decide(state, S.TEAM_INTRODUCTION, actor=TEAM_LEAD)
    assert status(state, S.TEAM_INTRODUCTION) == StageStatus.COMPLETE.value


def test_a_team_intro_decision_by_another_team_lead_is_refused_and_leaves_the_approval_usable(world):
    """Gap 4: the wrong person is refused BEFORE the decision is recorded; the team lead on record
    can still approve afterwards (it used to be consumed, so HR had to re-draft the meeting)."""
    state = world.to_team_intro_pending()
    approval = world.pending(state, S.TEAM_INTRODUCTION)
    other_lead = {"on_behalf_of": "TL-SOMEONE-ELSE", "roles": ["team_lead"], "client_id": CLIENT}
    with pytest.raises(Uc05RelayError) as err:
        world.decide(state, S.TEAM_INTRODUCTION, actor=other_lead)
    assert err.value.code == "POLICY_DENIED" and "TL-TEST-001" not in err.value.detail  # names a role, not a person
    (item,) = [a for a in world.uc05_snapshot()["approvals"] if a["approval_id"] == approval]
    assert item["status"] == "pending" and item["decided_at"] is None
    state = world.agent.get_case(state["case_id"], client_id=CLIENT)
    assert status(state, S.TEAM_INTRODUCTION) == StageStatus.AWAITING_APPROVAL.value

    state = world.decide(state, S.TEAM_INTRODUCTION, actor=TEAM_LEAD)  # the legitimate approver
    assert status(state, S.TEAM_INTRODUCTION) == StageStatus.COMPLETE.value


def test_optional_buddy_introduction_needs_an_assigned_buddy_and_is_approved_by_the_buddy(world):
    state = world.to_buddy_pending()
    with pytest.raises(PrerequisiteNotMetError):  # no buddy yet
        world.agent.request_uc05_operation(state["case_id"], operation="schedule_buddy_intro",
                                           params={"start": "2026-12-02T14:00:00+04:00"}, actor=HR)
    state = world.decide(state, S.BUDDY_ASSIGNMENT)
    state, outcome = world.agent.request_uc05_operation(state["case_id"], operation="schedule_buddy_intro",
                                                        params={"start": "2026-12-02T14:00:00+04:00"}, actor=HR)
    assert outcome["approver_role"] == "buddy"
    assert status(state, S.BUDDY_INTRODUCTION) == StageStatus.AWAITING_APPROVAL.value
    state = world.decide(state, S.BUDDY_INTRODUCTION, actor=BUDDY)
    assert status(state, S.BUDDY_INTRODUCTION) == StageStatus.COMPLETE.value


def test_buddy_introduction_never_blocks_readiness(world):
    state = world.to_welcome_waiting()
    assert status(state, S.BUDDY_INTRODUCTION) == StageStatus.NOT_REQUESTED.value
    assert status(state, S.DAY_ONE_READINESS) == StageStatus.COMPLETE.value


# ============================================================ 10-12. joining day and approvals


def test_joining_day_event_is_refused_before_the_joining_date(world):
    state = world.to_welcome_waiting()
    with pytest.raises(PrematureLifecycleEventError):
        world.agent.trigger_joining_day(state["case_id"], as_of=date(2026, 11, 30), actor=SCHEDULER)
    assert world.agent.get_case(state["case_id"], client_id=CLIENT)["joining_day_event"] is None
    assert world.uc05_snapshot()["welcome_drafts"] == []


def test_joining_day_event_before_signoff_is_kept_and_the_welcome_follows_signoff(world):
    state = world.to_signoff_pending()
    state = world.agent.trigger_joining_day(state["case_id"], as_of=JOINING, actor=SCHEDULER)
    assert state["joining_day_event"]["as_of"] == "2026-12-01"
    assert world.uc05_snapshot()["welcome_drafts"] == []  # readiness is not signed off: no welcome yet

    state = world.agent.decide_approval(state["case_id"], world.gate_item(state["case_id"], "day_one_signoff"),
                                        decision="approved", actor=HR_2)
    assert status(state, S.JOINING_DAY_WELCOME) == StageStatus.AWAITING_APPROVAL.value


def test_only_the_scheduler_or_hr_may_send_the_joining_day_event(world):
    state = world.to_welcome_waiting()
    with pytest.raises(OrchestratorAuthorizationError):
        world.agent.trigger_joining_day(state["case_id"], as_of=JOINING, actor=TEAM_LEAD)


def test_a_tenant_without_welcome_configuration_gets_a_structured_reason_not_a_success(uc05):
    """Gap 1: UC05 reports a typed, sanitized reason (its welcome_not_configured stage); the
    orchestrator maps it to UC05_CLIENT_CONFIG_MISSING, and a resume after HR configures it drafts."""
    w = World(uc05)
    w.hook("reset")
    w.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
    w.hook("remove_client_config", client_id=CLIENT)  # this tenant has no welcome configuration
    state = w.to_welcome_pending()
    assert status(state, S.JOINING_DAY_WELCOME) == StageStatus.BLOCKED.value
    assert code(state, S.JOINING_DAY_WELCOME) == "UC05_CLIENT_CONFIG_MISSING"
    detail = state["stage_errors"][S.JOINING_DAY_WELCOME.value]["detail"]
    assert "no welcome configuration" in detail and "KeyError" not in detail and CLIENT not in detail
    assert state["stage_errors"][S.JOINING_DAY_WELCOME.value]["retryable"] is True
    assert state["overall_status"] != "complete" and w.uc05_snapshot()["welcome_drafts"] == []

    w.hook("add_client_config", config=CLIENT_CONFIG)  # HR configures the tenant's welcome template
    state = w.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.JOINING_DAY_WELCOME) == StageStatus.AWAITING_APPROVAL.value


def test_repeated_joining_day_event_drafts_and_sends_one_welcome(world):
    state = world.to_welcome_pending()
    for _ in range(2):
        state = world.agent.trigger_joining_day(state["case_id"], as_of=JOINING, actor=SCHEDULER)
    assert len(world.uc05_snapshot()["welcome_drafts"]) == 1
    state = world.decide(state, S.JOINING_DAY_WELCOME)
    world.agent.trigger_joining_day(state["case_id"], as_of=date(2026, 12, 2), actor=SCHEDULER)  # a late repeat
    assert world.hook("counts")["messages_by_kind"]["welcome"] == 1
    assert world.agent.get_case(state["case_id"], client_id=CLIENT)["overall_status"] == "complete"


def test_pending_approval_parks_the_case_and_a_rejected_induction_is_released(world):
    state = world.start()
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.AWAITING_APPROVAL.value
    assert state["overall_status"] == "blocked" and state["current_stage"] == S.ORIENTATION_SCHEDULING.value

    state = world.decide(state, S.ORIENTATION_SCHEDULING, decision="rejected")
    assert code(state, S.ORIENTATION_SCHEDULING) == "INDUCTION_REJECTED"
    counts = world.hook("counts")
    assert counts["calendar_live"] == 0 and counts["calendar_cancelled"] == 1  # the hold was released
    assert not [a for a in world.uc05_snapshot()["approvals"] if a["proposal_type"] == "buddy_assignment"]

    state, _ = world.agent.request_uc05_operation(state["case_id"], operation="reschedule_session",
                                                  params={"session_type": "induction",
                                                          "start": "2026-12-02T09:00:00+04:00"}, actor=HR)
    assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.AWAITING_APPROVAL.value
    state = world.decide(state, S.ORIENTATION_SCHEDULING)
    assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.AWAITING_APPROVAL.value


def test_rejected_day_one_signoff_holds_the_case_until_hr_resumes(world):
    state = world.to_signoff_pending()
    case_id = state["case_id"]
    state = world.agent.decide_approval(case_id, world.gate_item(case_id, "day_one_signoff"), decision="rejected",
                                        actor=HR_2, reason_code="missing_info")
    assert code(state, S.DAY_ONE_READINESS) == "DAY_ONE_SIGNOFF_REJECTED"
    world.agent.trigger_joining_day(case_id, as_of=JOINING, actor=SCHEDULER)
    assert world.uc05_snapshot()["welcome_drafts"] == []  # no welcome while readiness is rejected

    state = world.agent.resume_case(case_id, actor=HR)  # HR addressed the cause and asks again
    assert status(state, S.DAY_ONE_READINESS) == StageStatus.AWAITING_APPROVAL.value
    assert world.gate_item(case_id, "day_one_signoff")  # exactly one fresh request


def test_an_approver_without_the_role_is_refused_by_uc05(world):
    state = world.start()
    employee = {"on_behalf_of": "user:someone", "roles": ["employee"], "client_id": CLIENT}
    with pytest.raises(Uc05RelayError) as err:
        world.decide(state, S.ORIENTATION_SCHEDULING, actor=employee)
    assert err.value.code == "POLICY_DENIED"
    assert world.uc05_snapshot()["sessions"][0]["status"] == "scheduled"


# ============================================================ 13. repeated resume


def test_repeated_resume_repeats_no_side_effect(world):
    state = world.to_team_intro_pending()
    before = (world.hook("counts"), world.uc05_snapshot()["approvals"], len(world.ctx.hitl.list(status=None)))
    for _ in range(3):
        state = world.agent.resume_case(state["case_id"], actor=HR)
    after = (world.hook("counts"), world.uc05_snapshot()["approvals"], len(world.ctx.hitl.list(status=None)))
    assert after == before
    assert len(state["pending_approvals"]) == 1  # no duplicated entries
    assert (world.uc01.calls, world.plan_calls, world.docs_calls, world.uc04.calls) == (1, 1, 1, 1)


def test_repeated_start_for_the_same_offer_resumes_the_same_case(world):
    first = world.start()
    second = world.start()
    assert second["case_id"] == first["case_id"] and world.uc01.calls == 1
    assert world.hook("counts")["calendar_live"] == 1


# ============================================================ 14-15. worker failure and uncertain outcomes


def test_worker_crash_mid_decision_is_uncertain_and_lost_records_are_never_replayed():
    worker = _uc05_worker()
    try:
        world = World(worker)
        state = world.start()
        approval_id = world.pending(state, S.ORIENTATION_SCHEDULING)
        worker.kill()  # the UC05 process dies

        state = world.agent.decide_approval(state["case_id"], approval_id, decision="approved", actor=HR)
        assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.UNCERTAIN.value
        assert state["overall_status"] == "at_risk"

        worker.start()  # restarted WITHOUT persistence: UC05's in-memory records are gone
        state = world.agent.resume_case(state["case_id"], actor=HR)
        assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.UNCERTAIN.value
        assert "no longer holds" in state["stage_errors"][S.ORIENTATION_SCHEDULING.value]["detail"]
        assert world.hook("counts")["calendar_live"] == 0  # the offer event was NOT re-sent
        assert status(state, S.BUDDY_ASSIGNMENT) == StageStatus.PENDING.value
    finally:
        worker.stop()


def test_worker_timeout_stops_the_worker_and_reports_an_unknown_outcome():
    worker = _uc05_worker()
    try:
        worker.timeout_s = 1.0
        with pytest.raises(WorkerTimeoutError):
            worker.call("__test_hooks__", {"action": "sleep", "seconds": 5},
                        actor=Actor(on_behalf_of="t", roles=["hr_admin"], client_id=CLIENT))
        assert not worker.is_alive  # a late reply can never be read as the answer to the next request
    finally:
        worker.stop()


def test_uncertain_welcome_delivery_is_never_resent_and_hr_resolves_it(world):
    state = world.to_welcome_pending()
    world.hook("set", target="notifier", attrs={"idempotent_delivery": False, "lose_next_responses": 1})
    state = world.decide(state, S.JOINING_DAY_WELCOME)  # delivered, but the provider's answer is lost
    assert status(state, S.JOINING_DAY_WELCOME) == StageStatus.UNCERTAIN.value
    assert code(state, S.JOINING_DAY_WELCOME) == "WELCOME_DELIVERY_UNCERTAIN"
    assert state["approval_decisions"][-1]["applied"] is False

    state = world.agent.retry_approval(state["case_id"], state["welcome_message_result"]["approval_id"], actor=HR)
    assert status(state, S.JOINING_DAY_WELCOME) == StageStatus.UNCERTAIN.value
    assert world.hook("counts")["messages_by_kind"]["welcome"] == 1  # never resent blindly

    key = state["welcome_message_result"]["operation_key"]
    state, outcome = world.agent.request_uc05_operation(state["case_id"], operation="resolve_operation",
                                                        params={"key": key, "outcome": "succeeded"}, actor=HR)
    assert outcome["outcome"] == "succeeded"
    assert status(state, S.JOINING_DAY_WELCOME) == StageStatus.COMPLETE.value
    assert state["welcome_message_result"]["delivery"] == "confirmed_by_operator"  # not a provider receipt
    assert state["overall_status"] == "complete" and world.hook("counts")["messages_by_kind"]["welcome"] == 1


def test_provider_outage_during_a_decision_is_retried_and_delivered_once(world):
    state = world.to_welcome_pending()
    world.hook("set", target="notifier", attrs={"fail_next": 1})  # the provider is down
    state = world.decide(state, S.JOINING_DAY_WELCOME)
    assert state["approval_decisions"][-1]["applied"] is False
    # UC05's ledger cannot know whether a failed provider call delivered anything: UNCERTAIN, with
    # UC05's retry-apply (same idempotency key) offered alongside an operator resolution.
    assert status(state, S.JOINING_DAY_WELCOME) == StageStatus.UNCERTAIN.value
    assert "retry-apply" in next(b for b in state["blockers"]
                                 if b["stage"] == S.JOINING_DAY_WELCOME.value)["next_action"]
    assert world.hook("counts")["messages_by_kind"].get("welcome", 0) == 0

    state = world.agent.retry_approval(state["case_id"], state["welcome_message_result"]["approval_id"], actor=HR)
    assert status(state, S.JOINING_DAY_WELCOME) == StageStatus.COMPLETE.value
    assert world.hook("counts")["messages_by_kind"]["welcome"] == 1


# ============================================================ 16-17. authorization and tenancy


def test_cross_tenant_access_to_a_case_is_indistinguishable_from_not_found(world):
    state = world.start()
    case_id, approval_id = state["case_id"], world.pending(state, S.ORIENTATION_SCHEDULING)
    with pytest.raises(KeyError):
        world.agent.get_case(case_id, client_id=OTHER_TENANT_HR["client_id"])
    with pytest.raises(KeyError):
        world.agent.resume_case(case_id, actor=OTHER_TENANT_HR)
    with pytest.raises(KeyError):
        world.agent.decide_approval(case_id, approval_id, decision="approved", actor=OTHER_TENANT_HR)
    with pytest.raises(KeyError):
        world.agent.trigger_joining_day(case_id, as_of=JOINING, actor={**OTHER_TENANT_HR,
                                                                        "roles": ["onboarding_scheduler"]})
    assert world.uc05_snapshot()["sessions"][0]["status"] == "scheduled"  # nothing was decided


def test_an_approval_of_another_case_cannot_be_decided_through_this_case(world):
    state = world.start()
    with pytest.raises(KeyError):
        world.agent.decide_approval(state["case_id"], "apr-not-this-case", decision="approved", actor=HR)


def test_uc05_operations_need_an_operator_role(world):
    state = world.start()
    with pytest.raises(OrchestratorAuthorizationError):
        world.agent.request_uc05_operation(state["case_id"], operation="propose_buddy", params={}, actor=TEAM_LEAD)


# ============================================================ 18. restart and recovery with persistence


def test_orchestrator_and_uc05_restart_resume_the_same_case_without_duplicates(tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    uc05_dir, orch_dir = tmp_path / "uc05", tmp_path / "orchestrator"
    orch_dir.mkdir()

    def orchestrator(worker):
        saver = SqliteSaver(sqlite3.connect(str(orch_dir / "checkpoints.db"), check_same_thread=False))
        saver.setup()
        return World(worker, case_states=SqliteCaseStateStore(orch_dir / "case_states.db"), checkpointer=saver,
                     persist_dir=orch_dir)

    worker = _uc05_worker(UC05_WORKER_DATA_DIR=str(uc05_dir))  # UC05's own SQLite stores (HR_AGENT_PERSIST=1)
    try:
        world = orchestrator(worker)
        world.hook("add_buddy_candidate", candidate=BUDDY_CANDIDATE)
        state = world.start()
        case_id, approval_id = state["case_id"], world.pending(state, S.ORIENTATION_SCHEDULING)
        uc05_before = world.uc05_snapshot()
        holds_before = world.hook("counts")["calendar_live"]
    finally:
        worker.stop()

    worker = _uc05_worker(UC05_WORKER_DATA_DIR=str(uc05_dir))  # both processes "restart"
    try:
        restarted = orchestrator(worker)
        loaded = restarted.agent.get_case(case_id, client_id=CLIENT)
        assert loaded["pending_approvals"][0]["reference"] == approval_id  # from SQLite, not memory
        state = restarted.agent.resume_case(case_id, actor=HR)
        assert status(state, S.ORIENTATION_SCHEDULING) == StageStatus.AWAITING_APPROVAL.value
        assert restarted.pending(state, S.ORIENTATION_SCHEDULING) == approval_id
        uc05_after = restarted.uc05_snapshot()
        assert uc05_after["sessions"] == uc05_before["sessions"]  # UC05 persisted its case: no second induction
        assert [a["approval_id"] for a in uc05_after["approvals"]] == [approval_id]
        # Nothing was booked again after the restart: the simulated calendar persists with UC05's records, so the
        # same hold is still there and no second one was made.
        assert restarted.hook("counts")["calendar_live"] == holds_before == 1
        assert restarted.uc01.calls == 0  # completed stages were not re-executed
    finally:
        worker.stop()


def test_a_new_orchestrator_instance_continues_a_case_from_its_sqlite_case_store(tmp_path, world):
    """Resume does not depend on the in-memory case_states of the instance that started the case."""
    state = world.to_team_intro_pending()
    SqliteCaseStateStore(tmp_path / "case_states.db").save(world.agent.get_case(state["case_id"], client_id=CLIENT))

    restarted = World(world.uc05, case_states=SqliteCaseStateStore(tmp_path / "case_states.db"))
    loaded = restarted.agent.get_case(state["case_id"], client_id=CLIENT)
    state = restarted.decide(loaded, S.TEAM_INTRODUCTION, actor=TEAM_LEAD)
    assert status(state, S.TEAM_INTRODUCTION) == StageStatus.COMPLETE.value
    assert status(state, S.DAY_ONE_READINESS) == StageStatus.AWAITING_APPROVAL.value
    assert restarted.uc01.calls == 0 and restarted.plan_calls == 0  # nothing before it re-ran


# ============================================================ 19. a later failure never re-runs earlier stages


def test_a_failure_in_a_later_stage_does_not_re_execute_completed_ones(world):
    state = world.to_buddy_pending()
    world.hook("calendar_busy", attendee="TL-TEST-001", start="2026-12-01T10:30:00+04:00",
               end="2026-12-31T00:00:00+04:00")
    state = world.decide(state, S.BUDDY_ASSIGNMENT)
    assert status(state, S.TEAM_INTRODUCTION) == StageStatus.BLOCKED.value
    bookings_before = world.hook("counts")["calendar_live"]
    attempts_before = dict(state["stage_attempts"])
    for _ in range(2):
        state = world.agent.resume_case(state["case_id"], actor=HR)
    assert (world.uc01.calls, world.plan_calls, world.docs_calls, world.uc04.calls) == (1, 1, 1, 1)
    assert world.hook("counts")["calendar_live"] == bookings_before  # the induction was not booked again
    for stage in (S.ORIENTATION_SCHEDULING, S.BUDDY_ASSIGNMENT):  # complete: skipped, not re-run
        assert state["stage_attempts"][stage.value] == attempts_before[stage.value]
    assert state["stage_attempts"][S.TEAM_INTRODUCTION.value] == attempts_before[S.TEAM_INTRODUCTION.value] + 2


# ============================================================ 20. HR policy Q&A intact, NDA tracking absent


def test_hr_policy_qa_still_answers_through_uc05(world):
    response = world.uc05.call("run_event", {"event": {"event": "policy_question", "question":
                                                       "How many days of annual leave do I get?",
                                                       "as_of": "2026-10-29"}, "instance": "policy-qa"},
                               actor=Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], client_id="CLIENT-ACME"))
    assert response.ok and response.result["workflow"]["stage"] in ("policy_answered", "policy_no_answer")


def test_nda_and_signature_tracking_is_not_part_of_the_orchestration():
    from pathlib import Path

    import hr_onboarding_common.orchestration as package
    from hr_onboarding_common.orchestration.api import create_orchestrator_app

    sources = " ".join(p.read_text(encoding="utf-8").lower() for p in Path(package.__file__).parent.rglob("*.py"))
    for term in ("acknowledgement", "nda_signed", "signature_status", "sign_nda", "/acknowledgements"):
        assert term not in sources
    from hr_onboarding_common.platform.identity import InsecureHeaderIdentityProvider

    app = create_orchestrator_app(agent=_DummyAgent(), identity_provider=InsecureHeaderIdentityProvider(enabled=False))
    paths = " ".join(app.openapi()["paths"]).lower()
    assert "/v1/orchestration/cases/{case_id}/joining-day" in paths
    assert "acknowledg" not in paths and "signature" not in paths and "nda" not in paths


class _DummyAgent:
    agent_id, version = "onboarding-orchestrator", "0"
