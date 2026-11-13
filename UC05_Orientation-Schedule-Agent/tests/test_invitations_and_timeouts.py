"""UC step 4 remainder: per-participant invitation status, and approval-timeout handling (a
PENDING booking that sits past its sla_hours is cancelled rather than blocking the slot forever).

Phase 4 (finding D): attendees are invited only when the approver confirms the hold
(settings.invitations: on_approval, the default); the hold itself records PENDING invitations.
The two invitation tests below moved their SENT/FAILED assertions from booking time to
confirmation time accordingly; on_hold keeps the old send-with-the-hold behaviour as an opt-in.
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
from sub_agent.models import (InvitationStatus, OperationStatus, SessionStatus, SessionType,
                              TenantMismatchError)
from sub_agent.services.scheduling import apply_decision, check_approval_timeouts, schedule_session
from sub_agent.tool_registry import OnboardingToolRegistry

ACME = "CLIENT-ACME"
GLOBEX = "CLIENT-GLOBEX"
START = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def port():
    return build_mock_onboarding()


@pytest.fixture
def hitl():
    return HITLInbox()


@pytest.fixture
def audit():
    return AuditLog()


# --------------------------------------------------------------------------- invitation status

def _approve(hitl, item):
    return hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"],
                       client_id=ACME)


def test_a_tentative_hold_invites_nobody_until_the_booking_is_approved(port, hitl, audit):
    calendar = FakeCalendar()
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance="EMP-001")

    invitations = port.list_invitations(session.id)
    assert {i.participant_id for i in invitations} == {"EMP-001", "HM-01"}
    assert all(i.status == InvitationStatus.PENDING for i in invitations)
    assert calendar.invited == [] and not audit.find("scheduling.invitations_sent")

    apply_decision(port, calendar, audit, _approve(hitl, item))

    assert port.get_session(session.id).status == SessionStatus.CONFIRMED
    invitations = port.list_invitations(session.id)
    assert {i.participant_id for i in invitations} == {"EMP-001", "HM-01"}
    assert all(i.status == InvitationStatus.SENT for i in invitations)
    assert sorted(a for _, a in calendar.invited) == ["EMP-001", "HM-01"]


def test_one_failed_invite_does_not_block_the_confirmation(port, hitl, audit):
    calendar = FakeCalendar(invite_failures={"HM-01"})
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance="EMP-001")

    apply_decision(port, calendar, audit, _approve(hitl, item))

    assert port.get_session(session.id).status == SessionStatus.CONFIRMED  # the booking itself still succeeded
    invitations = {i.participant_id: i.status for i in port.list_invitations(session.id)}
    assert invitations["HM-01"] == InvitationStatus.FAILED
    assert invitations["EMP-001"] == InvitationStatus.SENT
    assert audit.find("scheduling.invitation_failed")
    sent_summary = audit.find("scheduling.invitations_sent")[0]
    assert sent_summary["sent"] == 1 and sent_summary["failed"] == 1


def test_a_rejected_hold_never_invites_anyone_and_releases_its_booking_operation(port, hitl, audit):
    calendar = FakeCalendar()
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance="EMP-001")
    rejected = hitl.decide(item.id, Decision.REJECTED, approver="hr-admin-01", approver_roles=["hr_admin"],
                           client_id=ACME, reason_code=ReasonCode.TIMING)

    apply_decision(port, calendar, audit, rejected)

    assert port.get_session(session.id).status == SessionStatus.CANCELLED
    assert calendar.invited == []
    assert all(i.status == InvitationStatus.PENDING for i in port.list_invitations(session.id))
    assert port.get_operation(session.booking_operation_key).status == OperationStatus.RELEASED


def test_on_hold_invitation_policy_invites_with_the_hold_and_not_again_on_approval(port, hitl, audit):
    calendar = FakeCalendar()
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance="EMP-001", settings={"invitations": "on_hold"})
    assert all(i.status == InvitationStatus.SENT for i in port.list_invitations(session.id))
    assert len(calendar.invited) == 2

    apply_decision(port, calendar, audit, _approve(hitl, item))

    assert len(calendar.invited) == 2  # confirming does not invite a second time


def test_an_unknown_invitation_policy_is_refused_before_anything_is_booked(port, hitl, audit):
    calendar = FakeCalendar()
    with pytest.raises(ValueError, match="settings.invitations"):
        schedule_session(port, calendar, hitl, audit, employee_id="EMP-001", session_type=SessionType.ORIENTATION,
                         start=START, attendees=["HM-01"], workflow_instance="EMP-001",
                         settings={"invitations": "whenever"})
    assert calendar._bookings == {} and port.list_sessions("EMP-001") == []


def test_registry_list_invitations_is_tenant_checked():
    port = build_mock_onboarding()
    acme_actor = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
                       client_id=ACME)
    acme_reg = OnboardingToolRegistry(port=port, calendar=FakeCalendar(), kb=PolicyKnowledgeBase(),
                                      llm=offline_llm(), policy=PolicyStore(AGENT_HOME / "guardrails").for_agent(
                                          "employee-onboarding"), hitl=HITLInbox(), audit=AuditLog(),
                                      actor=acme_actor)
    session, _ = acme_reg.schedule_session("EMP-001", session_type=SessionType.ORIENTATION, start=START,
                                           attendees=["HM-01"], workflow_instance="EMP-001")
    assert {i.participant_id for i in acme_reg.list_invitations(session.id)} == {"EMP-001", "HM-01"}

    globex_actor = Actor(on_behalf_of="user:hr-admin-02", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
                        client_id=GLOBEX)
    globex_reg = OnboardingToolRegistry(port=port, calendar=acme_reg.calendar, kb=acme_reg.kb, llm=acme_reg.llm,
                                        policy=acme_reg.policy, hitl=acme_reg.hitl, audit=acme_reg.audit,
                                        actor=globex_actor)
    with pytest.raises(TenantMismatchError):
        globex_reg.list_invitations(session.id)


# --------------------------------------------------------------------------- approval timeouts

def test_pending_approval_within_sla_is_left_untouched(port, hitl, audit):
    calendar = FakeCalendar()
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance="EMP-001")

    results = check_approval_timeouts(port, calendar, hitl, audit, as_of=item.created_at + timedelta(hours=1))

    assert results == []
    assert port.get_session(session.id).status == SessionStatus.SCHEDULED


def test_pending_approval_past_sla_is_cancelled_and_calendar_hold_released(port, hitl, audit):
    calendar = FakeCalendar()
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance="EMP-001")

    results = check_approval_timeouts(port, calendar, hitl, audit,
                                      as_of=item.created_at + timedelta(hours=item.sla_hours + 1))

    assert len(results) == 1 and results[0]["session_id"] == session.id
    assert port.get_session(session.id).status == SessionStatus.CANCELLED
    assert session.external_event_ref not in calendar._bookings
    assert audit.find("scheduling.approval_timed_out")
    # the HITL item itself is left pending -- HITLInbox has no "expired" decision of its own
    assert hitl.get(item.id).status == Decision.PENDING


def test_running_the_sweep_twice_does_not_double_process(port, hitl, audit):
    calendar = FakeCalendar()
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance="EMP-001")
    late = item.created_at + timedelta(hours=item.sla_hours + 1)

    first = check_approval_timeouts(port, calendar, hitl, audit, as_of=late)
    second = check_approval_timeouts(port, calendar, hitl, audit, as_of=late)

    assert len(first) == 1 and second == []


def test_a_late_decision_after_timeout_is_safely_ignored(port, hitl, audit):
    calendar = FakeCalendar()
    session, item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                     session_type=SessionType.ORIENTATION, start=START, attendees=["HM-01"],
                                     workflow_instance="EMP-001")
    check_approval_timeouts(port, calendar, hitl, audit, as_of=item.created_at + timedelta(hours=item.sla_hours + 1))

    # HR decides on it anyway, not knowing it already timed out
    decided = hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"],
                          client_id=ACME)
    apply_decision(port, calendar, audit, decided)  # must not raise or re-confirm a released calendar hold

    assert port.get_session(session.id).status == SessionStatus.CANCELLED  # the timeout's outcome stands
    assert audit.find("scheduling.late_decision_ignored")


def test_timeout_sweep_is_scoped_to_one_tenant():
    port = build_mock_onboarding()
    calendar = FakeCalendar()
    hitl, audit = HITLInbox(), AuditLog()
    acme_session, acme_item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                               session_type=SessionType.ORIENTATION, start=START,
                                               attendees=["HM-01"], workflow_instance="EMP-001")
    globex_session, globex_item = schedule_session(port, calendar, hitl, audit, employee_id="EMP-101",
                                                   session_type=SessionType.ORIENTATION,
                                                   start=START + timedelta(days=14, hours=3),  # joins 11-16
                                                   attendees=["HM-101"],
                                                   workflow_instance="EMP-101")
    late = max(acme_item.created_at, globex_item.created_at) + timedelta(hours=25)

    results = check_approval_timeouts(port, calendar, hitl, audit, as_of=late, client_id=ACME)

    assert {r["session_id"] for r in results} == {acme_session.id}
    assert port.get_session(globex_session.id).status == SessionStatus.SCHEDULED  # untouched
