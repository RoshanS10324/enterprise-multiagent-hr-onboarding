"""UC steps 5 and 7: participant RSVP tracking + HR decision (Don't Cancel/Cancel/Reschedule),
and approver routing to the team lead/buddy instead of a generic hr_admin pool.

Attendance requirements and approver-role-by-session-type both come from guardrails/policy.yaml
settings (confirmed with the user), never from a code heuristic or the model.
"""

from datetime import datetime, timedelta, timezone

import pytest

from context_layer.knowledge.policy_index import PolicyKnowledgeBase
from hr_onboarding_common.contracts import Actor, Purpose
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import Decision, HITLInbox, ReasonCode
from hr_onboarding_common.platform.policy import PolicyStore
from sub_agent.agent import AGENT_HOME
from sub_agent.fakes import build_mock_onboarding, FakeCalendar, offline_llm
from sub_agent.models import ResponseStatus, SessionStatus, SessionType, \
    TenantMismatchError
from sub_agent.services.scheduling import (UnauthorizedApproverError, apply_attendance_decision,
                                           apply_decision, is_attendance_satisfied, record_response,
                                           schedule_session)
from sub_agent.tool_registry import OnboardingToolRegistry

ACME = "CLIENT-ACME"
GLOBEX = "CLIENT-GLOBEX"
START = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

ATTENDANCE_SETTINGS = {
    "attendance_requirements": {"orientation": ["employee"], "team_intro": ["employee", "team_lead"],
                                "buddy_intro": ["employee", "buddy"]},
    "approver_role_by_session_type": {"orientation": "hr_admin", "team_intro": "team_lead",
                                      "buddy_intro": "buddy"},
}


@pytest.fixture
def port():
    return build_mock_onboarding()


@pytest.fixture
def calendar():
    return FakeCalendar()


@pytest.fixture
def hitl():
    return HITLInbox()


@pytest.fixture
def audit():
    return AuditLog()


# --------------------------------------------------------------------------- participant responses

def test_record_response_is_stored_and_queryable(port, calendar, hitl, audit):
    session, _ = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                  session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                  workflow_instance="EMP-001")
    record_response(port, hitl, audit, session_id=session.id, participant_id="HM-01",
                    response=ResponseStatus.ACCEPTED, workflow_instance="EMP-001")

    responses = port.list_participant_responses(session.id)
    assert len(responses) == 1 and responses[0].response == ResponseStatus.ACCEPTED


def test_response_update_is_idempotent_not_duplicated(port, calendar, hitl, audit):
    session, _ = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                  session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                  workflow_instance="EMP-001")
    record_response(port, hitl, audit, session_id=session.id, participant_id="HM-01",
                    response=ResponseStatus.TENTATIVE, workflow_instance="EMP-001")
    record_response(port, hitl, audit, session_id=session.id, participant_id="HM-01",
                    response=ResponseStatus.ACCEPTED, workflow_instance="EMP-001")

    responses = port.list_participant_responses(session.id)
    assert len(responses) == 1 and responses[0].response == ResponseStatus.ACCEPTED


@pytest.mark.parametrize("response", [ResponseStatus.ACCEPTED, ResponseStatus.TENTATIVE, ResponseStatus.NO_RESPONSE])
def test_non_decline_responses_do_not_notify_hr(port, calendar, hitl, audit, response):
    session, _ = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                  session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                  workflow_instance="EMP-001")
    record_response(port, hitl, audit, session_id=session.id, participant_id="HM-01", response=response,
                    workflow_instance="EMP-001")
    assert not [i for i in hitl.list(status=None) if i.proposal_type == "attendee_declined"]


def test_decline_notifies_hr_with_a_real_approval_item(port, calendar, hitl, audit):
    session, _ = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                  session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                  workflow_instance="EMP-001")
    record_response(port, hitl, audit, session_id=session.id, participant_id="HM-01",
                    response=ResponseStatus.DECLINED, workflow_instance="EMP-001")

    pending = [i for i in hitl.list(status=None) if i.proposal_type == "attendee_declined"]
    assert len(pending) == 1
    assert pending[0].gate == "attendance_decision" and pending[0].payload["participant_id"] == "HM-01"
    assert audit.find("scheduling.attendance_decision_requested")


# --------------------------------------------------------------------------- HR decision outcomes

def _decline_and_get_item(port, calendar, hitl, audit, *, employee_id="EMP-001") -> tuple:
    session, booking_item = schedule_session(port, calendar, hitl, audit, employee_id=employee_id,
                                              session_type=SessionType.ORIENTATION, start=START,
                                              attendees=["HM-01"], workflow_instance=employee_id)
    decided_booking = hitl.decide(booking_item.id, Decision.APPROVED, approver="hr-admin-01",
                                  approver_roles=["hr_admin"], client_id=ACME)
    apply_decision(port, calendar, audit, decided_booking)  # actually confirm it, as the real flow would
    record_response(port, hitl, audit, session_id=session.id, participant_id="HM-01",
                    response=ResponseStatus.DECLINED, workflow_instance=employee_id)
    decline_item = next(i for i in hitl.list(status=None) if i.proposal_type == "attendee_declined")
    return session, decline_item


def test_dont_cancel_leaves_the_confirmed_session_untouched(port, calendar, hitl, audit):
    session, decline_item = _decline_and_get_item(port, calendar, hitl, audit)
    hitl.decide(decline_item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"],
               client_id=ACME)

    result = apply_attendance_decision(port, calendar, hitl, audit, hitl.get(decline_item.id))
    assert result is None
    assert port.get_session(session.id).status == SessionStatus.CONFIRMED
    assert audit.find("scheduling.attendance_decision_applied")[0]["action"] == "dont_cancel"


def test_cancel_cancels_the_session_and_releases_the_calendar_hold(port, calendar, hitl, audit):
    session, decline_item = _decline_and_get_item(port, calendar, hitl, audit)
    hitl.decide(decline_item.id, Decision.REJECTED, approver="hr-admin-01", approver_roles=["hr_admin"],
               client_id=ACME, reason_code=ReasonCode.TIMING)

    apply_attendance_decision(port, calendar, hitl, audit, hitl.get(decline_item.id))
    assert port.get_session(session.id).status == SessionStatus.CANCELLED
    assert session.external_event_ref not in calendar._bookings


def test_reschedule_cancels_the_old_session_and_books_a_new_one(port, calendar, hitl, audit):
    session, decline_item = _decline_and_get_item(port, calendar, hitl, audit)
    new_start = (START + timedelta(days=1)).isoformat()
    hitl.decide(decline_item.id, Decision.EDITED, approver="hr-admin-01", approver_roles=["hr_admin"],
               client_id=ACME, reason_code=ReasonCode.TIMING, edited_payload={"new_start": new_start})

    rescheduled = apply_attendance_decision(port, calendar, hitl, audit, hitl.get(decline_item.id))

    assert port.get_session(session.id).status == SessionStatus.CANCELLED
    assert rescheduled is not None and rescheduled.id != session.id
    assert rescheduled.scheduled_start.isoformat() == new_start
    assert port.get_session(rescheduled.id).status == SessionStatus.SCHEDULED
    new_approvals = [i for i in hitl.list(status=None) if i.payload.get("id") == rescheduled.id]
    assert len(new_approvals) == 1  # the rebooked session has its own fresh approval cycle


# --------------------------------------------------------------------------- attendance satisfaction (step 5)

def test_attendance_is_unsatisfied_until_the_required_role_responds(port, calendar, hitl, audit):
    session, _ = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                  session_type=SessionType.TEAM_INTRO, start=START, attendees=["TL-001"],
                                  workflow_instance="EMP-001", approver_role="team_lead")
    assert not is_attendance_satisfied(port, ATTENDANCE_SETTINGS, session)

    record_response(port, hitl, audit, session_id=session.id, participant_id="EMP-001",
                    response=ResponseStatus.ACCEPTED, workflow_instance="EMP-001")
    assert not is_attendance_satisfied(port, ATTENDANCE_SETTINGS, session)  # team_lead still missing

    record_response(port, hitl, audit, session_id=session.id, participant_id="TL-001",
                    response=ResponseStatus.TENTATIVE, workflow_instance="EMP-001")
    assert is_attendance_satisfied(port, ATTENDANCE_SETTINGS, session)


def test_attendance_is_never_satisfied_when_the_required_role_has_no_assigned_person(port, calendar, hitl, audit):
    session, _ = schedule_session(port, calendar, hitl, audit, employee_id="EMP-002",  # EMP-002 has no buddy yet
                                  session_type=SessionType.BUDDY_INTRO, start=START + timedelta(days=7),  # joins 11-09
                                  attendees=["HM-02"],
                                  workflow_instance="EMP-002", approver_role="buddy")
    record_response(port, hitl, audit, session_id=session.id, participant_id="EMP-002",
                    response=ResponseStatus.ACCEPTED, workflow_instance="EMP-002")
    assert not is_attendance_satisfied(port, ATTENDANCE_SETTINGS, session)  # no buddy_id to resolve


# --------------------------------------------------------------------------- approver routing (step 7)

def test_team_intro_booking_defaults_to_team_lead_approver_role(port, calendar, hitl, audit):
    _, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                               session_type=SessionType.TEAM_INTRO, start=START, attendees=["TL-001"],
                               workflow_instance="EMP-001")
    assert item.approver_role == "team_lead"


def test_buddy_intro_booking_defaults_to_buddy_approver_role(port, calendar, hitl, audit):
    _, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                               session_type=SessionType.BUDDY_INTRO, start=START, attendees=["EMP-010"],
                               workflow_instance="EMP-001")
    assert item.approver_role == "buddy"


def test_wrong_person_claiming_team_lead_role_cannot_confirm_the_booking(port, calendar, hitl, audit):
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.TEAM_INTRO, start=START, attendees=["TL-001"],
                                     workflow_instance="EMP-001")
    decided = hitl.decide(item.id, Decision.APPROVED, approver="TL-IMPOSTER", approver_roles=["team_lead"],
                          client_id=ACME)
    assert decided.status == Decision.APPROVED  # the generic role check alone has no reason to reject it

    with pytest.raises(UnauthorizedApproverError):
        apply_decision(port, calendar, audit, decided)
    assert port.get_session(session.id).status == SessionStatus.SCHEDULED  # never confirmed


def test_the_actual_team_lead_on_record_can_confirm_the_booking(port, calendar, hitl, audit):
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.TEAM_INTRO, start=START, attendees=["TL-001"],
                                     workflow_instance="EMP-001")
    decided = hitl.decide(item.id, Decision.APPROVED, approver="TL-001", approver_roles=["team_lead"],
                          client_id=ACME)

    apply_decision(port, calendar, audit, decided)
    assert port.get_session(session.id).status == SessionStatus.CONFIRMED


def test_hr_admin_approvals_have_no_specific_person_requirement(port, calendar, hitl, audit):
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance="EMP-001")
    decided = hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-99", approver_roles=["hr_admin"],
                          client_id=ACME)

    apply_decision(port, calendar, audit, decided)  # any hr_admin may confirm -- it's a pool, not one person
    assert port.get_session(session.id).status == SessionStatus.CONFIRMED


# --------------------------------------------------------------------------- through the real registry

def _registry(client_id: str) -> OnboardingToolRegistry:
    actor = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
                 client_id=client_id)
    return OnboardingToolRegistry(port=build_mock_onboarding(), calendar=FakeCalendar(), kb=PolicyKnowledgeBase(),
                                  llm=offline_llm(), policy=PolicyStore(AGENT_HOME / "guardrails").for_agent(
                                      "employee-onboarding"), hitl=HITLInbox(), audit=AuditLog(), actor=actor)


def test_registry_schedule_team_intro_uses_the_configured_approver_role():
    reg = _registry(ACME)
    _, item = reg.schedule_session("EMP-001", session_type=SessionType.TEAM_INTRO, start=START,
                                   attendees=["TL-001"], workflow_instance="EMP-001")
    assert item.approver_role == "team_lead"


def test_registry_record_participant_response_is_tenant_checked():
    acme_reg = _registry(ACME)
    session, _ = acme_reg.schedule_session("EMP-001", session_type=SessionType.ORIENTATION, start=START,
                                            attendees=["HM-01"], workflow_instance="EMP-001")

    globex_actor = Actor(on_behalf_of="user:hr-admin-02", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
                        client_id=GLOBEX)
    globex_reg = OnboardingToolRegistry(port=acme_reg.port, calendar=acme_reg.calendar, kb=acme_reg.kb,
                                        llm=acme_reg.llm, policy=acme_reg.policy, hitl=acme_reg.hitl,
                                        audit=acme_reg.audit, actor=globex_actor)
    with pytest.raises(TenantMismatchError):
        globex_reg.record_participant_response(session.id, "HM-01", ResponseStatus.ACCEPTED,
                                                workflow_instance="EMP-001")


def test_registry_is_attendance_satisfied_reflects_real_responses():
    reg = _registry(ACME)
    session, booking_item = reg.schedule_session("EMP-001", session_type=SessionType.ORIENTATION, start=START,
                                                  attendees=["HM-01"], workflow_instance="EMP-001")
    assert reg.is_attendance_satisfied(session.id) is False

    reg.record_participant_response(session.id, "EMP-001", ResponseStatus.ACCEPTED, workflow_instance="EMP-001")
    assert reg.is_attendance_satisfied(session.id) is True
