"""Automated tests for the /v1/onboarding/* HTTP routes (intake, checklist, scheduling,
attendance, buddy assignment, policy Q&A) and their interaction with the shared
/v1/approvals/{id}/decision endpoint.

sub_agent.api builds its AgentContext (and its OnboardingDependencies bundle) as module-level singletons (the
same objects a real running service would use), so this file gives mutation-heavy tests their own
fresh employee record (via the `fresh_employee` fixture, writing directly to the same shared
MockOnboarding the API uses) rather than colliding with other tests over EMP-001/EMP-002. Tests
that only read, or that rely on EMP-001/EMP-002's specific pre-seeded state, use those directly.
"""

import itertools
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from sub_agent import api
from sub_agent.models import OnboardingRecord

ACME = {"X-User-Id": "user:hr-admin-01", "X-User-Roles": "hr_admin", "X-Client-Id": "CLIENT-ACME"}
GLOBEX = {"X-User-Id": "user:hr-admin-02", "X-User-Roles": "hr_admin", "X-Client-Id": "CLIENT-GLOBEX"}
START = datetime(2026, 12, 1, 5, 0, tzinfo=timezone.utc)  # 09:00 Dubai on the fresh employees' joining date (a Tuesday),
# inside the joining-date window (joining date .. +2 days); see services/scheduling.scheduling_window. Formerly 2026-12-07,
# which the window now refuses. Same weekday as every
# other fixture's Nov 2, 2026. Kept at the start of the Dubai office day (09:00-18:00) so the
# +0..+8 hour offsets used below all land within office hours regardless of session duration.

_counter = itertools.count(1)


@pytest.fixture(scope="module")
def client():
    with TestClient(api.app) as c:
        yield c


@pytest.fixture
def fresh_employee():
    """A brand-new CLIENT-ACME employee, isolated from every other test, written directly into
    the same MockOnboarding singleton the running API uses."""
    n = next(_counter)
    employee_id = f"EMP-API-TEST-{n:03d}"
    api.ctx.connector.repository.create_onboarding_case(OnboardingRecord(
        employee_id=employee_id, full_name=f"Alex Tester {n}", email=f"alex.tester{n}@example.com",
        role_title="Software Engineer", department="Engineering", team="Platform", team_lead_id="TL-001",
        client_id="CLIENT-ACME", start_date=date(2026, 12, 1)))
    return employee_id


# --------------------------------------------------------------------------- functional / success

def test_healthz_unaffected_by_the_new_routes(client):
    assert client.get("/healthz").status_code == 200


def test_get_record_success(client):
    r = client.get("/v1/onboarding/EMP-001", headers=ACME)
    assert r.status_code == 200
    assert r.json()["full_name"] == "Sara Al Marzooqi"


def test_ensure_tasks_and_list_them(client, fresh_employee):
    r = client.post(f"/v1/onboarding/{fresh_employee}/tasks", headers=ACME, json={})
    assert r.status_code == 200
    created = r.json()
    assert len(created) >= 1

    listed = client.get(f"/v1/onboarding/{fresh_employee}/tasks", headers=ACME)
    assert listed.status_code == 200 and len(listed.json()) == len(created)

    pending = client.get(f"/v1/onboarding/{fresh_employee}/tasks/pending", headers=ACME)
    assert pending.status_code == 200 and len(pending.json()) == len(created)

    overdue = client.get(f"/v1/onboarding/{fresh_employee}/tasks/overdue", headers=ACME,
                         params={"as_of": "2099-01-01"})
    assert overdue.status_code == 200 and overdue.json() == []  # no due_date set -> never overdue


def test_schedule_session_and_approve_it(client, fresh_employee):
    booked = client.post(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME, json={
        "session_type": "orientation", "start": START.isoformat(), "attendees": [f"HM-{fresh_employee}"]})
    assert booked.status_code == 200
    body = booked.json()
    assert body["session"]["status"] == "scheduled"
    approval_id = body["approval"]["id"]

    decided = client.post(f"/v1/approvals/{approval_id}/decision", headers=ACME, json={"decision": "approved"})
    assert decided.status_code == 200

    sessions = client.get(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME).json()
    assert sessions[0]["status"] == "confirmed"


def test_buddy_intro_session_for_an_employee_with_an_assigned_buddy(client):
    emp001_joining_day = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)  # 09:00 Dubai; EMP-001 joins 2026-11-02
    booked = client.post("/v1/onboarding/EMP-001/sessions/buddy-intro", headers=ACME,
                         json={"start": (emp001_joining_day + timedelta(hours=5)).isoformat()})
    assert booked.status_code == 200
    assert "EMP-010" in booked.json()["session"]["attendees"]


def test_propose_and_approve_buddy_assignment(client, fresh_employee):
    proposed = client.post(f"/v1/onboarding/{fresh_employee}/buddy", headers=ACME)
    assert proposed.status_code == 200
    body = proposed.json()
    assert body["buddy_id"] and body["approval"] is not None

    decided = client.post(f"/v1/approvals/{body['approval']['id']}/decision", headers=ACME,
                          json={"decision": "approved"})
    assert decided.status_code == 200

    record = client.get(f"/v1/onboarding/{fresh_employee}", headers=ACME).json()
    assert record["buddy_id"] == body["buddy_id"]


def test_record_participant_response_and_attendance_status(client, fresh_employee):
    booked = client.post(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME, json={
        "session_type": "orientation", "start": (START + timedelta(hours=1)).isoformat(),
        "attendees": [f"HM2-{fresh_employee}"]}).json()
    session_id = booked["session"]["id"]

    before = client.get(f"/v1/onboarding/sessions/{session_id}/attendance", headers=ACME)
    assert before.json()["satisfied"] is False

    responded = client.post(f"/v1/onboarding/sessions/{session_id}/responses", headers=ACME,
                            json={"participant_id": fresh_employee, "response": "accepted"})
    assert responded.status_code == 200

    after = client.get(f"/v1/onboarding/sessions/{session_id}/attendance", headers=ACME)
    assert after.json()["satisfied"] is True

    invitations = client.get(f"/v1/onboarding/sessions/{session_id}/invitations", headers=ACME)
    assert invitations.status_code == 200 and len(invitations.json()) == 2


def test_check_timeouts_route(client):
    r = client.post("/v1/onboarding/sessions/check-timeouts", headers=ACME,
                    json={"as_of": datetime.now(timezone.utc).isoformat()})
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_policy_question_route(client):
    r = client.post("/v1/onboarding/policy-questions", headers=ACME,
                    json={"question": "What is the dress code?", "as_of": "2026-11-01"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] is not None and body["citations"][0]["doc_id"] == "hr-policy-consultants"


def test_intake_creates_a_case(client):
    r = client.post("/v1/onboarding/intake",
                    headers={"X-User-Id": "user:hr-admin-03", "X-User-Roles": "hr_admin",
                             "X-Client-Id": "CLIENT-TEST-A"},
                    json={"event_id": "EVT-001"})
    assert r.status_code == 200
    assert r.json()["employee_id"] == "EMP-TEST-001"


# --------------------------------------------------------------------------- validation

def test_missing_required_body_field_is_422(client, fresh_employee):
    r = client.post(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME,
                    json={"session_type": "orientation"})  # missing "start"
    assert r.status_code == 422


def test_invalid_session_type_enum_value_is_422(client, fresh_employee):
    r = client.post(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME,
                    json={"session_type": "not_a_real_type", "start": START.isoformat(), "attendees": []})
    assert r.status_code == 422


def test_out_of_office_hours_meeting_time_is_422(client, fresh_employee):
    out_of_hours = datetime(2026, 12, 1, 23, 0, tzinfo=timezone.utc)  # 03:00 Dubai on 2 Dec: inside the window, outside office hours
    r = client.post(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME,
                    json={"session_type": "orientation", "start": out_of_hours.isoformat(), "attendees": []})
    assert r.status_code == 422


def test_invalid_response_enum_value_is_422(client, fresh_employee):
    booked = client.post(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME, json={
        "session_type": "orientation", "start": (START + timedelta(hours=2)).isoformat(), "attendees": []}).json()
    r = client.post(f"/v1/onboarding/sessions/{booked['session']['id']}/responses", headers=ACME,
                    json={"participant_id": fresh_employee, "response": "maybe_later"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- authorization

def test_missing_client_id_header_is_401(client):
    """Finding S2: identity is verified through _identity_provider, not read as a bare, optional
    header -- a caller who can't be verified (here: no client_id at all) is rejected outright
    (401), not merely failed on request-shape validation (422)."""
    r = client.get("/v1/onboarding/EMP-001", headers={"X-User-Id": "user:hr-admin-01", "X-User-Roles": "hr_admin"})
    assert r.status_code == 401


def test_missing_user_id_header_is_401(client):
    r = client.get("/v1/onboarding/EMP-001", headers={"X-User-Roles": "hr_admin", "X-Client-Id": "CLIENT-ACME"})
    assert r.status_code == 401


# Phase 4 (finding F): cross-tenant access was 403, which confirmed that the record exists. It is
# now the same 404 response, byte for byte, as a record that does not exist.

def test_cross_tenant_read_is_indistinguishable_from_not_found(client):
    r = client.get("/v1/onboarding/EMP-101", headers=ACME)  # EMP-101 belongs to CLIENT-GLOBEX
    missing = client.get("/v1/onboarding/EMP-DOES-NOT-EXIST", headers=ACME)
    assert r.status_code == missing.status_code == 404 and r.content == missing.content


def test_cross_tenant_write_is_indistinguishable_from_not_found(client, fresh_employee):
    r = client.post(f"/v1/onboarding/{fresh_employee}/tasks", headers=GLOBEX, json={})
    missing = client.post("/v1/onboarding/EMP-DOES-NOT-EXIST/tasks", headers=GLOBEX, json={})
    assert r.status_code == missing.status_code == 404 and r.content == missing.content
    assert client.get(f"/v1/onboarding/{fresh_employee}/tasks", headers=ACME).json() == []  # nothing written


def test_decision_by_an_unauthorized_role_is_403(client, fresh_employee):
    booked = client.post(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME, json={
        "session_type": "orientation", "start": (START + timedelta(hours=3)).isoformat(), "attendees": []}).json()
    approval_id = booked["approval"]["id"]

    # X-Client-Id must be present and correct (same tenant) so this actually exercises the role
    # check, not identity verification (finding S2 requires a verified client_id before anything
    # else runs, so an actor missing it is rejected with 401 before the role check is reached).
    r = client.post(f"/v1/approvals/{approval_id}/decision",
                    headers={"X-User-Id": "user:someone", "X-User-Roles": "not_hr_admin",
                             "X-Client-Id": "CLIENT-ACME"},
                    json={"decision": "approved"})
    assert r.status_code == 403


# --------------------------------------------------------------------------- approval enforcement

def test_rejected_session_booking_is_cancelled_not_confirmed(client, fresh_employee):
    booked = client.post(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME, json={
        "session_type": "induction", "start": (START + timedelta(hours=4)).isoformat(), "attendees": []}).json()

    client.post(f"/v1/approvals/{booked['approval']['id']}/decision", headers=ACME,
               json={"decision": "rejected", "reason_code": "timing"})

    sessions = client.get(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME).json()
    assert sessions[0]["status"] == "cancelled"


# --------------------------------------------------------------------------- duplicate requests

def test_duplicate_intake_event_is_idempotent(client):
    headers = {"X-User-Id": "user:hr-admin-03", "X-User-Roles": "hr_admin", "X-Client-Id": "CLIENT-TEST-B"}
    first = client.post("/v1/onboarding/intake", headers=headers, json={"event_id": "EVT-010"})
    second = client.post("/v1/onboarding/intake", headers=headers, json={"event_id": "EVT-010"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["employee_id"] == second.json()["employee_id"] == "EMP-TEST-010"


def test_deciding_the_same_approval_twice_is_409(client, fresh_employee):
    booked = client.post(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME, json={
        "session_type": "team_intro", "start": (START + timedelta(hours=6)).isoformat(),
        "attendees": ["TL-001"]}).json()
    approval_id = booked["approval"]["id"]

    # the approver string must match the employee's team_lead_id on record exactly (TL-001),
    # not an identity-style "user:..." string -- HITLInbox checks role membership, but
    # scheduling._check_instance_approver separately requires the specific person on record.
    decide_headers = {"X-User-Id": "TL-001", "X-User-Roles": "team_lead", "X-Client-Id": "CLIENT-ACME"}
    first = client.post(f"/v1/approvals/{approval_id}/decision", headers=decide_headers,
                        json={"decision": "approved"})
    second = client.post(f"/v1/approvals/{approval_id}/decision", headers=decide_headers,
                         json={"decision": "approved"})
    assert first.status_code == 200 and second.status_code == 409


def test_double_booking_the_same_attendee_and_slot_is_409(client, fresh_employee):
    client.post(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME, json={
        "session_type": "orientation", "start": (START + timedelta(hours=7)).isoformat(),
        "attendees": [f"SHARED-{fresh_employee}"]})

    second_employee = f"{fresh_employee}-B"
    api.ctx.connector.repository.create_onboarding_case(OnboardingRecord(
        employee_id=second_employee, full_name="Second Tester", email="second@example.com",
        role_title="Analyst", department="Data", team="Analytics", team_lead_id="TL-002",
        client_id="CLIENT-ACME", start_date=date(2026, 12, 1)))
    r = client.post(f"/v1/onboarding/{second_employee}/sessions", headers=ACME, json={
        "session_type": "orientation", "start": (START + timedelta(hours=7)).isoformat(),
        "attendees": [f"SHARED-{fresh_employee}"]})
    assert r.status_code == 409


# --------------------------------------------------------------------------- failures

def test_unknown_employee_is_404(client):
    r = client.get("/v1/onboarding/EMP-DOES-NOT-EXIST", headers=ACME)
    assert r.status_code == 404


def test_calendar_provider_failure_is_502(client, fresh_employee):
    r = client.post(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME, json={
        "session_type": "orientation", "start": (START + timedelta(hours=8)).isoformat(),
        "attendees": ["CALENDAR-OUTAGE"]})
    assert r.status_code == 502


def test_email_service_failure_is_502(client):
    r = client.post("/v1/onboarding/intake",
                    headers={"X-User-Id": "user:hr-admin-03", "X-User-Roles": "hr_admin",
                             "X-Client-Id": "CLIENT-TEST-A"},
                    json={"event_id": "EVT-PROVIDER-OUTAGE"})
    assert r.status_code == 502


def test_rsvp_from_a_participant_who_was_not_invited_is_422(client, fresh_employee):
    booked = client.post(f"/v1/onboarding/{fresh_employee}/sessions", headers=ACME, json={
        "session_type": "orientation", "start": (START + timedelta(hours=1, minutes=30)).isoformat(),
        "attendees": []}).json()
    session_id = booked["session"]["id"]
    for route in (f"/v1/onboarding/sessions/{session_id}/responses",
                  f"/v1/onboarding/workflow/sessions/{session_id}/responses"):
        r = client.post(route, headers=ACME, json={"participant_id": "STRANGER-99", "response": "declined"})
        assert r.status_code == 422 and r.json()["error_type"] == "InvalidParticipantResponseError"
