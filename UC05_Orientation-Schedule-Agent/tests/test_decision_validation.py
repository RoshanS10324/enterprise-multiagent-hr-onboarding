"""A decision is validated BEFORE HITLInbox records it (services/decisions.py), so an unauthorized
person or a malformed decision can no longer consume an approval meant for someone else -- at the
service level, through the API's POST /v1/approvals/{id}/decision, and for every UC05 approval
type. Plus the cross-midnight office-hours fix found while testing replans."""

import itertools
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import Decision, HITLInbox, ReasonCode

from sub_agent import api
from sub_agent.fakes import build_fake_dependencies
from sub_agent.models import OnboardingRecord, ResponseStatus, SessionType
from sub_agent.services.decisions import (
    DecisionNotApplicableError,
    InvalidDecisionError,
    validate_decision,
)
from sub_agent.services.scheduling import (
    InvalidMeetingTimeError,
    UnauthorizedApproverError,
    apply_decision,
    propose_buddy_assignment,
    record_response,
    schedule_session,
    validate_meeting_time,
)

START = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)  # 09:00 Dubai, a Monday
SETTINGS = {"timezone": "Asia/Dubai", "office_hours": {"start": "09:00", "end": "18:00"}, "weekend_weekdays": [4, 5]}


@pytest.fixture
def world():
    deps = build_fake_dependencies()
    return deps.repository, deps.calendar, HITLInbox(), AuditLog()


def _team_intro(world):
    port, calendar, hitl, audit = world
    return schedule_session(port, calendar, hitl, audit, employee_id="EMP-001", session_type=SessionType.TEAM_INTRO,
                            start=START, attendees=["TL-001"], workflow_instance="EMP-001")


def test_only_the_team_lead_on_record_passes_and_the_message_names_no_person(world):
    port, *_ = world
    _, item = _team_intro(world)
    with pytest.raises(UnauthorizedApproverError) as err:
        validate_decision(port, SETTINGS, item, decision=Decision.APPROVED, approver="TL-IMPOSTER", edited_payload=None)
    assert "TL-001" not in str(err.value) and "team_lead on record" in str(err.value)
    validate_decision(port, SETTINGS, item, decision=Decision.APPROVED, approver="TL-001", edited_payload=None)


def test_a_booking_that_is_no_longer_pending_is_not_applicable(world):
    port, calendar, hitl, audit = world
    _, item = _team_intro(world)
    decided = hitl.decide(item.id, Decision.REJECTED, approver="TL-001", approver_roles=["team_lead"],
                          client_id="CLIENT-ACME", reason_code=ReasonCode.TIMING)
    apply_decision(port, calendar, audit, decided)  # the hold is released
    with pytest.raises(DecisionNotApplicableError):
        validate_decision(port, SETTINGS, item, decision=Decision.APPROVED, approver="TL-001", edited_payload=None)


def test_an_edited_buddy_must_be_an_eligible_candidate_of_the_same_client(world):
    port, _, hitl, audit = world
    _, item = propose_buddy_assignment(port, hitl, audit, employee_id="EMP-002", workflow_instance="EMP-002")
    for edit in ({"employee_id": "EMP-002", "buddy_id": "EMP-110"},  # CLIENT-GLOBEX's candidate
                 {"employee_id": "EMP-001", "buddy_id": "EMP-011"},  # retargets another employee
                 {"employee_id": "EMP-002"}):  # no buddy at all
        with pytest.raises(InvalidDecisionError):
            validate_decision(port, SETTINGS, item, decision=Decision.EDITED, approver="hr", edited_payload=edit)
    validate_decision(port, SETTINGS, item, decision=Decision.EDITED, approver="hr",
                      edited_payload={"employee_id": "EMP-002", "buddy_id": "EMP-011"})


def test_a_replan_needs_a_valid_new_start(world):
    port, calendar, hitl, audit = world
    session, _ = schedule_session(port, calendar, hitl, audit, employee_id="EMP-001",
                                  session_type=SessionType.INDUCTION, start=START, attendees=["TL-001"],
                                  workflow_instance="EMP-001")
    record_response(port, hitl, audit, session_id=session.id, participant_id="TL-001",
                    response=ResponseStatus.DECLINED, workflow_instance="EMP-001")
    (conflict,) = [i for i in hitl.list() if i.proposal_type == "attendee_declined"]
    for edit in ({}, {"new_start": "not a date"}, {"new_start": "2026-11-03T23:00:00+04:00"},
                 {"new_start": "2026-11-06T10:00:00+04:00"}):  # missing, malformed, after hours, a Friday
        with pytest.raises(InvalidDecisionError):
            validate_decision(port, SETTINGS, conflict, decision=Decision.EDITED, approver="hr", edited_payload=edit)
    validate_decision(port, SETTINGS, conflict, decision=Decision.REJECTED, approver="hr", edited_payload=None)


def test_a_meeting_running_past_midnight_is_outside_office_hours():
    start = datetime(2026, 11, 3, 23, 0, tzinfo=timezone(timedelta(hours=4)))
    with pytest.raises(InvalidMeetingTimeError):
        validate_meeting_time(SETTINGS, start, start + timedelta(minutes=90))  # 23:00-00:30


# --------------------------------------------------------------------------- through the API

_counter = itertools.count(1)
ACME = {"X-User-Id": "user:hr-admin-01", "X-User-Roles": "hr_admin", "X-Client-Id": "CLIENT-ACME"}


@pytest.fixture(scope="module")
def client():
    with TestClient(api.app) as c:
        yield c


@pytest.fixture
def employee():
    employee_id = f"EMP-DECIDE-{next(_counter):03d}"
    api.ctx.connector.repository.create_onboarding_case(OnboardingRecord(
        employee_id=employee_id, full_name="Decision Tester", email="decision.tester@example.com",
        role_title="Engineer", department="Engineering", team="Platform", team_lead_id="TL-001",
        client_id="CLIENT-ACME", start_date=date(2026, 12, 1)))
    return employee_id


def _book_team_intro(client, employee_id, hours):
    booked = client.post(f"/v1/onboarding/{employee_id}/sessions", headers=ACME, json={
        "session_type": "team_intro", "start": (datetime(2026, 12, 2, 5, 0, tzinfo=timezone.utc)
                                                + timedelta(hours=hours)).isoformat(), "attendees": ["TL-001"]})
    assert booked.status_code == 200
    return booked.json()["approval"]["id"], booked.json()["session"]["id"]


def test_api_refuses_another_team_lead_without_consuming_the_approval(client, employee):
    approval_id, session_id = _book_team_intro(client, employee, 1)
    impostor = {"X-User-Id": "TL-IMPOSTER", "X-User-Roles": "team_lead", "X-Client-Id": "CLIENT-ACME"}
    refused = client.post(f"/v1/approvals/{approval_id}/decision", headers=impostor, json={"decision": "approved"})
    assert refused.status_code == 403 and "TL-001" not in refused.text
    assert client.get(f"/v1/approvals/{approval_id}", headers=ACME).json()["status"] == "pending"

    lead = {"X-User-Id": "TL-001", "X-User-Roles": "team_lead", "X-Client-Id": "CLIENT-ACME"}
    approved = client.post(f"/v1/approvals/{approval_id}/decision", headers=lead, json={"decision": "approved"})
    assert approved.status_code == 200
    sessions = client.get(f"/v1/onboarding/{employee}/sessions", headers=ACME).json()
    assert [s["status"] for s in sessions if s["id"] == session_id] == ["confirmed"]


def test_api_refuses_an_ineligible_edited_buddy_and_keeps_the_proposal(client, employee):
    proposed = client.post(f"/v1/onboarding/{employee}/buddy", headers=ACME).json()
    approval_id = proposed["approval"]["id"]
    refused = client.post(f"/v1/approvals/{approval_id}/decision", headers=ACME, json={
        "decision": "edited", "reason_code": "accuracy",
        "edited_payload": {"employee_id": employee, "buddy_id": "EMP-110"}})
    assert refused.status_code == 422
    assert client.get(f"/v1/approvals/{approval_id}", headers=ACME).json()["status"] == "pending"
    assert client.post(f"/v1/approvals/{approval_id}/decision", headers=ACME,
                       json={"decision": "approved"}).status_code == 200


def test_api_reports_a_booking_released_by_the_timeout_sweep_as_a_conflict(client, employee):
    approval_id, _ = _book_team_intro(client, employee, 2)
    swept = client.post("/v1/onboarding/sessions/check-timeouts", headers=ACME,
                        json={"as_of": "2027-01-31T00:00:00+00:00"})
    assert swept.status_code == 200
    lead = {"X-User-Id": "TL-001", "X-User-Roles": "team_lead", "X-Client-Id": "CLIENT-ACME"}
    late = client.post(f"/v1/approvals/{approval_id}/decision", headers=lead, json={"decision": "approved"})
    assert late.status_code == 409 and "no longer pending" in late.json()["detail"]
    assert client.get(f"/v1/approvals/{approval_id}", headers=ACME).json()["status"] == "pending"


def test_api_cross_tenant_decision_is_not_found(client, employee):
    approval_id, _ = _book_team_intro(client, employee, 3)
    globex = {"X-User-Id": "user:hr-admin-02", "X-User-Roles": "hr_admin", "X-Client-Id": "CLIENT-GLOBEX"}
    assert client.post(f"/v1/approvals/{approval_id}/decision", headers=globex,
                       json={"decision": "approved"}).status_code == 404
    assert client.get(f"/v1/approvals/{approval_id}", headers=ACME).json()["status"] == "pending"
