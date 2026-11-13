"""UC05's supabase-mode persistence (sub_agent/db) against an in-process fake PostgREST server
(tests/fake_postgrest.py). No credentials, no network: every test here runs in CI.

  A. repository operations      C. failure and recovery
  B. tenant isolation           D. the complete UC05 workflow, end to end, asserting the rows written

These tests verify the code against the fake's model of PostgREST. They do NOT verify the live
database's CHECK constraints, foreign keys or RLS; tests/test_supabase_live.py does that, opt-in.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.fake_postgrest import FakePostgrest
from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.config import get_settings
from hr_onboarding_common.contracts import Actor, Purpose
from hr_onboarding_common.platform.hitl import ApprovalItem, Decision, HITLApplyError, HITLError, ReasonCode
from hr_onboarding_common.platform.policy import PolicyStore
from pydantic import SecretStr
from sub_agent.agent import AGENT_HOME, OnboardingAgent, OnboardingWorkflowInput, Stage, WorkflowEvent, workflow_id_for
from sub_agent.db import mapping as m
from sub_agent.db.config import (DatabaseMode, SupabaseConfigurationError, SupabaseSettings, database_mode,
                                 load_supabase_settings)
from sub_agent.db.factory import build_supabase_stores
from sub_agent.db.postgrest import (PersistenceAuthError, PersistenceError, PersistenceUnavailableError,
                                    SchemaMismatchError)
from sub_agent.db.repository import DuplicateCommunicationError, SessionConflictError
from sub_agent.fakes import build_fake_dependencies, offline_llm
from sub_agent.models import (BuddyCapacityExceededError, CaseOwnershipError, CaseStatus,
                              ClientCalendarUnavailableError, CommunicationDraft, CommunicationStatus,
                              DuplicateOnboardingCaseError, DuplicateSessionError, DuplicateTaskError,
                              InvitationStatus, MissingClientConfigError, OnboardingCaseNotFoundError, OnboardingRecord,
                              OnboardingSession, OnboardingTask, OperationRecord, OperationStatus, ResponseStatus,
                              SessionStatus, SessionType, TenantMismatchError, TenantScopeRequiredError)
from sub_agent.runtime import RuntimeConfigurationError, build_context
from sub_agent.tool_registry import OnboardingToolRegistry

A, B = "CLIENT-SUPA-A", "CLIENT-SUPA-B"
CASE_A, CASE_B = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
EMP, TL, BUDDY, BUDDY2 = "EMP-T-001", "TL-T-001", "EMP-T-050", "EMP-T-051"
NOW = datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)
DUBAI = ZoneInfo("Asia/Dubai")
WELCOME = {"enabled_languages": ["en"], "default_language": "en",
           "branding": {"sender_name": "Supa People Team"},
           "templates": {"welcome": {"template_id": "welcome", "language": "en",
                                     "subject": "Welcome to {sender_name}, {full_name}!",
                                     "body": "Dear {full_name},\n\nWelcome aboard as {role_title}, starting {start_date}."}}}
SETTINGS = SupabaseSettings(url="https://example.supabase.co", service_role_key=SecretStr("service-role-test-key"))


def seed(fake: FakePostgrest, *, holidays=("2026-12-02",), joining="2026-12-01", buddy_cap=2) -> None:
    for client, case_id, name in ((A, CASE_A, "Alex Supa"), (B, CASE_B, "Bea Other")):
        fake.seed("client_configs", {"client_id": client, "company_name": f"{client} Ltd", "working_days": [1, 2, 3, 4, 5],
                                     "timezone": "Asia/Dubai", "settings_payload": {"uc05_welcome": WELCOME},
                                     "is_active": True})
        fake.seed("client_config_versions", {"client_id": client, "version": 1, "effective_from": "2026-01-01",
                                             "effective_to": None, "status": "approved", "config_hash": "sha256:x"})
        for h in holidays:
            fake.seed("client_holidays", {"client_id": client, "holiday_date": h, "name": "Holiday", "is_observed": True})
        # the SAME employee number in both tenants: isolation must never depend on it being unique
        fake.seed("onboarding_cases", {"case_id": case_id, "client_id": client, "employee_no": EMP,
                                       "candidate_name": name, "candidate_email": f"{name.split()[0].lower()}@example.com",
                                       "joining_date": joining, "work_location_type": "OFFICE", "role_id": "software_engineer",
                                       "department": "Platform", "manager_id": TL, "status": "DOCUMENTS_VERIFIED",
                                       "current_stage": "orientation", "is_deleted": False,
                                       "opened_at": "2026-10-01T08:00:00+00:00"})
        fake.seed("buddy_profiles", {"buddy_id": f"b-{client}-1", "client_id": client, "employee_id": BUDDY,
                                     "full_name": "Robin Park", "department": "Platform", "location": "Dubai",
                                     "max_active_buddies": buddy_cap, "is_active": True, "profile_payload": {}},
                  {"buddy_id": f"b-{client}-2", "client_id": client, "employee_id": BUDDY2, "full_name": "Sam Lee",
                   "department": "Finance", "location": "Dubai", "max_active_buddies": 1, "is_active": True,
                   "profile_payload": {}},
                  {"buddy_id": f"b-{client}-3", "client_id": client, "employee_id": "EMP-T-099",
                   "full_name": "Inactive", "department": "Platform", "location": "Dubai", "max_active_buddies": 5,
                   "is_active": False, "profile_payload": {}})


@pytest.fixture
def fake() -> FakePostgrest:
    f = FakePostgrest()
    seed(f)
    return f


@pytest.fixture
def stores(fake):
    return build_supabase_stores(SETTINGS, transport=fake.transport, clock=lambda: NOW)


@pytest.fixture
def repo(stores):
    return stores.repository.for_tenant(A)


def link(repo, employee=EMP) -> OnboardingRecord:
    return repo.create_onboarding_case(repo.get_onboarding_record(employee))


def session_for(repo, session_type=SessionType.INDUCTION, hour=9, day=1) -> OnboardingSession:
    start = datetime(2026, 12, day, hour, 0, tzinfo=DUBAI)
    return repo.create_session(OnboardingSession(employee_id=EMP, client_id=repo.tenant, session_type=session_type,
                                                 scheduled_start=start, scheduled_end=start + timedelta(minutes=90),
                                                 attendees=[EMP, TL], external_event_ref="evt-1",
                                                 booking_operation_key="calendar:k"))


# =========================================================================== A. repository operations

def test_fetches_the_onboarding_case_from_onboarding_cases(repo):
    record = repo.get_onboarding_record(EMP)
    assert (record.client_id, record.start_date, record.team_lead_id, record.team) == (A, date(2026, 12, 1), TL, "Platform")
    assert record.case_status == CaseStatus.ACTIVE and record.full_name == "Alex Supa"


def test_missing_onboarding_case_is_not_found_and_never_created(repo, fake):
    with pytest.raises(OnboardingCaseNotFoundError):
        repo.get_onboarding_record("EMP-NOBODY")
    with pytest.raises(KeyError):
        repo.create_onboarding_case(OnboardingRecord(employee_id="EMP-NOBODY", full_name="x", email="x", role_title="x",
                                                     department="x", team="x", team_lead_id="x", client_id=A,
                                                     start_date=date(2026, 12, 1)))
    assert fake.calls("POST", "onboarding_cases") == [] and fake.calls("PATCH", "onboarding_cases") == []


def test_linking_a_case_is_idempotent_and_never_writes_onboarding_cases(repo, fake):
    link(repo)
    with pytest.raises(DuplicateOnboardingCaseError):
        link(repo)
    assert [r.employee_id for r in repo.list_onboarding_records()] == [EMP]
    with pytest.raises(CaseOwnershipError):
        repo.update_case_status(EMP, CaseStatus.CANCELLED)
    assert fake.calls("PATCH", "onboarding_cases") == []


def test_fetches_client_configuration_and_holidays(repo):
    assert repo.get_client_config(A).branding.sender_name == "Supa People Team"
    calendar = repo.get_client_calendar(A, on=date(2026, 11, 1))
    assert calendar.timezone == "Asia/Dubai" and calendar.weekend_weekdays == [5, 6]
    assert calendar.holidays == [date(2026, 12, 2)] and calendar.config_version == 1


def test_client_without_an_effective_approved_version_has_no_calendar(repo, fake):
    for v in fake.rows("client_config_versions", client_id=A):
        v["status"] = "draft"
    with pytest.raises(ClientCalendarUnavailableError):
        repo.get_client_calendar(A)


def test_missing_or_inactive_client_configuration_is_refused(repo, fake):
    fake.rows("client_configs", client_id=A)[0]["is_active"] = False
    with pytest.raises(MissingClientConfigError):
        repo.get_client_config(A)
    with pytest.raises(ClientCalendarUnavailableError):
        repo.get_client_calendar(A)
    fake.tables["client_configs"] = [r for r in fake.tables["client_configs"] if r["client_id"] != A]
    with pytest.raises(MissingClientConfigError):
        repo.get_client_config(A)


def test_eligible_buddies_are_active_with_capacity(repo, fake):
    assert [c.employee_id for c in repo.list_buddy_candidates(A)] == [BUDDY, BUDDY2]
    fake.seed("buddy_assignments", {"assignment_id": "x", "case_id": "33333333-3333-4333-8333-333333333333",
                                    "client_id": A, "buddy_employee_id": BUDDY2, "status": "CONFIRMED"})
    assert [c.employee_id for c in repo.list_buddy_candidates(A)] == [BUDDY]  # BUDDY2 is at max_active_buddies=1


def test_buddy_proposal_assignment_and_duplicate_prevention(repo, fake):
    link(repo)
    repo.record_buddy_proposal(EMP, BUDDY, approval_id="apr_1", match_score=0.9, same_team=True)
    (row,) = fake.rows("buddy_assignments", case_id=CASE_A)
    assert row["status"] == "PROPOSED" and row["match_score"] == 0.9 and row["matching_criteria"]["same_team"]
    record = repo.assign_buddy(EMP, BUDDY)
    assert record.buddy_id == BUDDY
    again = repo.assign_buddy(EMP, BUDDY)  # retried: still one row
    assert again.buddy_id == BUDDY and len(fake.rows("buddy_assignments", case_id=CASE_A)) == 1
    (row,) = fake.rows("buddy_assignments", case_id=CASE_A)
    assert row["status"] == "CONFIRMED" and row["confirmed_at"] and row["assigned_at"]
    repo.record_buddy_proposal(EMP, BUDDY2, approval_id="apr_2", match_score=0.6, same_team=False)
    assert fake.rows("buddy_assignments", case_id=CASE_A)[0]["status"] == "CONFIRMED"  # never downgraded


def test_a_placeholder_buddy_profile_is_never_offered_or_assigned(repo, fake):
    # The shape of a live row seen in the shared project: template text where the colleague's number and name belong.
    fake.seed("buddy_profiles", {"buddy_id": "b-placeholder", "client_id": A, "employee_id": "<colleague employee number>",
                                 "full_name": "<colleague full name>", "department": "Platform", "location": "Dubai",
                                 "max_active_buddies": 5, "is_active": True, "profile_payload": {}},
              {"buddy_id": "b-sample", "client_id": A, "employee_id": "EMP-T-777", "full_name": "Sample Buddy",
               "department": "Platform", "location": "Dubai", "max_active_buddies": 5, "is_active": True,
               "profile_payload": {}})
    offered = {c.employee_id for c in repo.list_buddy_candidates(A)}
    assert "<colleague employee number>" not in offered and "EMP-T-777" not in offered
    assert BUDDY in offered  # a real profile is still offered
    link(repo)
    with pytest.raises(Exception):
        repo.assign_buddy(EMP, "EMP-T-777")
    assert not fake.rows("buddy_assignments", case_id=CASE_A)


def test_buddy_capacity_is_enforced_and_rolled_back(repo, fake):
    link(repo)
    fake.seed("buddy_assignments", {"assignment_id": "other", "case_id": "33333333-3333-4333-8333-333333333333",
                                    "client_id": A, "buddy_employee_id": BUDDY2, "status": "CONFIRMED"})
    with pytest.raises(BuddyCapacityExceededError):
        repo.assign_buddy(EMP, BUDDY2)
    assert fake.rows("buddy_assignments", case_id=CASE_A) == []  # rolled back, nothing kept


def test_concurrent_assignments_never_leave_a_buddy_over_capacity(repo, fake):
    """Two cases race for the last slot of a buddy (max 1): the loser is rolled back."""
    link(repo)
    other = "44444444-4444-4444-8444-444444444444"
    fake.seed("onboarding_cases", {"case_id": other, "client_id": A, "employee_no": "EMP-T-002",
                                   "candidate_name": "C", "candidate_email": "c@example.com", "joining_date": "2026-12-01",
                                   "department": "Finance", "manager_id": TL, "status": "OPEN", "is_deleted": False})
    repo.assign_buddy("EMP-T-002", BUDDY2)  # takes BUDDY2's only slot
    with pytest.raises(BuddyCapacityExceededError):
        repo.assign_buddy(EMP, BUDDY2)
    assert len(fake.rows("buddy_assignments", buddy_employee_id=BUDDY2, status="CONFIRMED")) == 1


def test_session_create_read_update_cancel_and_range(repo, fake):
    link(repo)
    session = session_for(repo)
    (row,) = fake.rows("orientation_sessions", session_id=session.id)
    assert row["start_utc"] == "2026-12-01T05:00:00+00:00" and row["status"] == "SCHEDULED"  # stored in UTC
    assert row["session_type"] == "HR_INDUCTION" and row["session_metadata"]["uc05_session_type"] == "induction"
    assert row["case_id"] == CASE_A and row["session_metadata"]["joining_date_basis"] == "2026-12-01"
    assert repo.get_session(session.id).scheduled_start == datetime(2026, 12, 1, 9, 0, tzinfo=DUBAI)
    assert repo.update_session_status(session.id, SessionStatus.CONFIRMED).status == SessionStatus.CONFIRMED
    moved = repo.update_session_time(session.id, datetime(2026, 12, 3, 9, 0, tzinfo=DUBAI),
                                     datetime(2026, 12, 3, 10, 30, tzinfo=DUBAI))
    assert moved.scheduled_start.astimezone(DUBAI).day == 3
    in_range = repo.list_sessions_in_range(datetime(2026, 12, 3, tzinfo=DUBAI), datetime(2026, 12, 4, tzinfo=DUBAI))
    assert [s.id for s in in_range] == [session.id]
    assert repo.cancel_session(session.id).status == SessionStatus.CANCELLED
    assert repo.list_sessions_in_range(datetime(2026, 12, 3, tzinfo=DUBAI), datetime(2026, 12, 4, tzinfo=DUBAI)) == []


def test_duplicate_and_conflicting_sessions_are_refused(repo):
    link(repo)
    session_for(repo)
    with pytest.raises(DuplicateSessionError):
        session_for(repo)
    with pytest.raises(SessionConflictError):
        session_for(repo, SessionType.TEAM_INTRO, hour=10)  # overlaps 09:00-10:30
    session_for(repo, SessionType.TEAM_INTRO, hour=11)  # after it: fine


def test_participants_invitations_and_newest_wins_rsvp(repo, fake):
    link(repo)
    session = session_for(repo)
    inv = repo.record_invitation(session.id, TL, InvitationStatus.PENDING)
    repo.record_invitation(session.id, EMP, InvitationStatus.PENDING)
    assert repo.update_invitation_status(inv.id, InvitationStatus.SENT).status == InvitationStatus.SENT
    assert {i.participant_id: i.status for i in repo.list_invitations(session.id)} == {
        TL: InvitationStatus.SENT, EMP: InvitationStatus.PENDING}
    # session_participants.email is NOT NULL: only the new hire, whose address the case holds, gets a row. The team
    # lead (named by id only) is recorded on the session with the reason -- never with an invented address.
    roles = {r["actor_id"]: (r["role"], r["email"]) for r in fake.rows("session_participants", session_id=session.id)}
    assert roles == {EMP: ("NEW_HIRE", "alex@example.com")}
    (meta,) = [r["session_metadata"] for r in fake.rows("orientation_sessions", session_id=session.id)]
    assert meta["participants_without_email"][TL]["role"] == "MANAGER"
    assert meta["participants_without_email"][TL]["reason"] == "EMAIL_UNKNOWN"
    listed = {r["actor_id"]: r for r in repo.list_session_participants(session.id)}
    assert set(listed) == {EMP, TL} and listed[TL]["email_unknown"] is True and listed[TL]["email"] is None
    repo.record_invitation(session.id, TL, InvitationStatus.PENDING)  # no duplicate participant row
    assert len(fake.rows("session_participants", session_id=session.id)) == 1

    late = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
    repo.record_participant_response(session.id, TL, ResponseStatus.DECLINED, responded_at=late,
                                     proposed_new_time=late + timedelta(days=1), note="clash")
    stale = repo.record_participant_response(session.id, TL, ResponseStatus.ACCEPTED, responded_at=late - timedelta(hours=1))
    assert stale.response == ResponseStatus.DECLINED  # an older RSVP never overwrites
    (resp,) = repo.list_participant_responses(session.id)
    assert resp.response == ResponseStatus.DECLINED and resp.participant_id == TL
    (meta,) = [r["session_metadata"] for r in fake.rows("orientation_sessions", session_id=session.id)]
    reply = meta["responses_without_participant_row"][TL]  # kept on the session, newest wins
    assert reply["response"] == "declined" and reply["proposed_new_time"] and reply["response_note"] == "clash"
    assert {r["actor_id"]: r["rsvp_status"] for r in repo.list_session_participants(session.id)}[TL] == "DECLINED"


def test_welcome_communication_is_stored_once_and_never_claims_dispatch(repo, fake):
    link(repo)
    draft = repo.create_communication_draft(CommunicationDraft(employee_id=EMP, client_id=A, template_id="welcome",
                                                               language="en", subject="Hi", body="Welcome"))
    (row,) = fake.rows("communications", comm_id=draft.id)
    assert (row["comm_type"], row["status"], row["recipient_email"]) == ("WELCOME_EMAIL", "DRAFT", "alex@example.com")
    with pytest.raises(DuplicateCommunicationError):
        repo.create_communication_draft(CommunicationDraft(employee_id=EMP, client_id=A, template_id="welcome",
                                                           language="en", subject="Hi", body="Welcome"))
    repo.link_review_gate(draft.id, "apr_w", client_id=A)
    assert fake.rows("communications", comm_id=draft.id)[0]["status"] == "AWAITING_REVIEW"
    repo.update_communication_status(draft.id, CommunicationStatus.APPROVED)
    sent = repo.update_communication_status(draft.id, CommunicationStatus.SENT)  # no real delivery on the ledger
    assert sent.status == CommunicationStatus.SENT  # UC05 sees it as sent: never resent ...
    row = fake.rows("communications", comm_id=draft.id)[0]
    assert row["status"] == "APPROVED" and row["dispatched_at"] is None  # ... but the table never claims dispatch
    assert row["review_gate_id"] == "apr_w"


def test_real_delivery_marks_sent_with_dispatched_at(repo, fake):
    link(repo)
    draft = repo.create_communication_draft(CommunicationDraft(employee_id=EMP, client_id=A, template_id="welcome",
                                                               language="en", subject="Hi", body="Welcome"))
    repo.save_operation(OperationRecord(key=f"welcome:{A}:{draft.id}", kind="notification", client_id=A,
                                        status=OperationStatus.SUCCEEDED,
                                        detail={"simulated": False, "accepted_at": "2026-12-01T05:00:00+00:00"}))
    repo.update_communication_status(draft.id, CommunicationStatus.SENT)
    row = fake.rows("communications", comm_id=draft.id)[0]
    assert row["status"] == "DISPATCHED" and row["dispatched_at"] == "2026-12-01T05:00:00+00:00"


def test_tasks_operations_and_notifications_are_versioned_audit_state(repo):
    link(repo)
    task = repo.create_task(OnboardingTask(employee_id=EMP, client_id=A, task_type="it_setup"))
    with pytest.raises(DuplicateTaskError):
        repo.create_task(OnboardingTask(employee_id=EMP, client_id=A, task_type="it_setup"))
    assert repo.update_task_status(task.id, "completed").completed_at is not None
    assert [t.status for t in repo.list_tasks(EMP)] == ["completed"]
    repo.save_operation(OperationRecord(key="calendar:x", kind="calendar_booking", client_id=A))
    repo.save_operation(OperationRecord(key="calendar:x", kind="calendar_booking", client_id=A,
                                        status=OperationStatus.SUCCEEDED))
    assert repo.get_operation("calendar:x").status == OperationStatus.SUCCEEDED
    assert [o.key for o in repo.list_operations(statuses=(OperationStatus.PENDING,))] == []
    repo.record_notification(EMP, BUDDY, "buddy_assignment", approval_id="apr_1", full_name="should be dropped")
    (note,) = repo.list_notifications(EMP)
    assert note.recipient_id == BUDDY and "full_name" not in note.detail


def test_approval_requests_round_trip_and_decide_once(stores, fake):
    hitl = stores.hitl
    link(stores.repository.for_tenant(A))
    item = hitl.submit(ApprovalItem(gate="buddy_assignment", approver_role="hr_admin", workflow="employee-onboarding",
                                    workflow_instance=EMP, agent="employee-onboarding", proposal_type="buddy_assignment",
                                    client_id=A, subject_id=EMP, payload={"employee_id": EMP, "buddy_id": BUDDY},
                                    rationale="r", confidence=0.9, run_id="run_1"))
    (row,) = fake.rows("approval_requests", request_id=item.id)
    assert (row["outcome"], row["workflow_module"], row["case_id"]) == ("PENDING", m.WORKFLOW_MODULE, CASE_A)
    assert hitl.get(item.id, client_id=A).payload == {"employee_id": EMP, "buddy_id": BUDDY}
    edited = hitl.decide(item.id, Decision.EDITED, approver="hr-1", approver_roles=["hr_admin"], client_id=A,
                         reason_code=ReasonCode.ACCURACY, edited_payload={"employee_id": EMP, "buddy_id": BUDDY2})
    assert edited.status == Decision.EDITED
    row = fake.rows("approval_requests", request_id=item.id)[0]
    assert row["outcome"] == "EDITED_AND_APPROVED" and row["final_payload"]["buddy_id"] == BUDDY2 and row["decided_by"] == "hr-1"
    assert hitl.get(item.id, client_id=A).status == Decision.EDITED
    with pytest.raises(HITLError):
        hitl.decide(item.id, Decision.APPROVED, approver="hr-2", approver_roles=["hr_admin"], client_id=A)


def test_a_decision_raced_by_another_process_is_not_applied_twice(stores, fake):
    hitl = stores.hitl
    item = hitl.submit(ApprovalItem(gate="comms_approval", approver_role="hr_admin", workflow="w", workflow_instance=EMP,
                                    agent="employee-onboarding", proposal_type="welcome_message", client_id=A,
                                    subject_id=EMP, payload={}, rationale="r", confidence=0.9, run_id="run_1"))
    original = hitl.get

    def stale_get(approval_id, *, client_id=None):  # this process read PENDING; another one decided meanwhile
        found = original(approval_id, client_id=client_id)
        fake.rows("approval_requests", request_id=item.id)[0]["outcome"] = "REJECTED"
        return found

    hitl.get = stale_get
    with pytest.raises(HITLError):
        hitl.decide(item.id, Decision.APPROVED, approver="hr-1", approver_roles=["hr_admin"], client_id=A)
    assert fake.rows("approval_requests", request_id=item.id)[0]["decided_by"] is None


def test_audit_events_and_agent_runs_are_persisted_without_personal_data(stores, fake):
    audit = stores.audit.for_tenant(A)
    link(stores.repository.for_tenant(A))
    audit.record("scheduling.buddy_assignment_proposed", agent="employee-onboarding", subject_id=EMP, buddy_id=BUDDY,
                 approval_id="apr_9", body="secret text", email="alex@example.com")
    (row,) = [r for r in fake.tables["audit_events"] if r["action"] == "scheduling.buddy_assignment_proposed"]
    assert (row["client_id"], row["case_id"], row["actor_type"], row["correlation_id"]) == (A, CASE_A, "AGENT", "apr_9")
    assert "body" not in row["detail"] and "email" not in row["detail"] and row["detail"]["buddy_id"] == BUDDY
    stores.audit.record("tool.called", agent="employee-onboarding")  # no tenant: kept locally only
    assert not [r for r in fake.tables["audit_events"] if r["action"] == "tool.called"]


# =========================================================================== B. tenant isolation

def test_unbound_repository_refuses_tenant_scoped_lookups(stores):
    with pytest.raises(TenantScopeRequiredError):
        stores.repository.get_onboarding_record(EMP)
    with pytest.raises(TenantMismatchError):
        stores.repository.for_tenant(A).get_client_config(B)


def test_client_a_cannot_read_client_b_case_or_buddies(repo, stores):
    assert repo.get_onboarding_record(EMP).full_name == "Alex Supa"  # same employee_no as B's case
    assert stores.repository.for_tenant(B).get_onboarding_record(EMP).full_name == "Bea Other"
    assert all(c.client_id == A for c in repo.list_buddy_candidates(A))
    with pytest.raises(TenantMismatchError):
        repo.list_buddy_candidates(B)


def test_client_a_cannot_modify_b_assignment_sessions_participants_or_communications(stores, fake):
    a, b = stores.repository.for_tenant(A), stores.repository.for_tenant(B)
    link(a), link(b)
    b.assign_buddy(EMP, BUDDY)
    a.assign_buddy(EMP, BUDDY2)
    assert fake.rows("buddy_assignments", case_id=CASE_B)[0]["buddy_employee_id"] == BUDDY  # untouched by A
    session_b = session_for(b)
    with pytest.raises(KeyError):
        a.get_session(session_b.id)
    with pytest.raises(KeyError):
        a.update_session_status(session_b.id, SessionStatus.CANCELLED)
    with pytest.raises(KeyError):
        a.record_participant_response(session_b.id, EMP, ResponseStatus.DECLINED)
    with pytest.raises(KeyError):
        a.list_participant_responses(session_b.id)
    assert fake.rows("session_participants", session_id=session_b.id) == []
    draft_b = b.create_communication_draft(CommunicationDraft(employee_id=EMP, client_id=B, template_id="welcome",
                                                              language="en", subject="s", body="b"))
    with pytest.raises(KeyError):
        a.get_communication_draft(draft_b.id)
    with pytest.raises(KeyError):
        a.update_communication_status(draft_b.id, CommunicationStatus.APPROVED)
    assert fake.rows("communications", comm_id=draft_b.id)[0]["status"] == "DRAFT"
    assert a.list_sessions(EMP) == [] and a.list_communication_drafts(EMP) == []


def test_every_tenant_table_query_carries_the_client_filter(stores, fake):
    a = stores.repository.for_tenant(A)
    link(a)
    session_for(a)
    a.list_buddy_candidates(A)
    a.get_client_calendar(A)
    scoped = {"onboarding_cases", "buddy_profiles", "buddy_assignments", "orientation_sessions", "communications",
              "client_configs", "client_holidays", "client_config_versions", "audit_events", "approval_requests"}
    for method, table, params in fake.requests:
        if ("limit", "0") in params:
            continue  # the startup schema check: selects column names, reads no row
        if table in scoped and method in ("GET", "PATCH", "DELETE"):
            assert ("client_id", f"eq.{A}") in params, (method, table, params)


def test_approvals_are_tenant_scoped(stores):
    item = stores.hitl.submit(ApprovalItem(gate="g", approver_role="hr_admin", workflow="w", workflow_instance=EMP,
                                           agent="employee-onboarding", proposal_type="x", client_id=B, subject_id=EMP,
                                           payload={}, rationale="r", confidence=0.5, run_id="r"))
    with pytest.raises(KeyError):
        stores.hitl.get(item.id, client_id=A)
    assert stores.hitl.list(status=None, client_id=A) == []
    with pytest.raises(KeyError):
        stores.hitl.decide(item.id, Decision.APPROVED, approver="hr", approver_roles=["hr_admin"], client_id=A)


# =========================================================================== C. failure and recovery

@pytest.mark.parametrize("env, message", [
    ({"UC05_DATABASE_MODE": "supabase"}, "SUPABASE_URL is not set"),
    ({"UC05_DATABASE_MODE": "supabase", "SUPABASE_URL": "https://x.supabase.co"}, "SUPABASE_SERVICE_ROLE_KEY is not set"),
    ({"UC05_DATABASE_MODE": "supabase", "SUPABASE_URL": "http://x.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "k1"},
     "https://"),
    ({"UC05_DATABASE_MODE": "supabase", "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "same",
      "SUPABASE_ANON_KEY": "same"}, "equals SUPABASE_ANON_KEY"),
    ({"UC05_DATABASE_MODE": "postgres"}, "UC05_DATABASE_MODE must be one of"),
    ({"UC05_DATABASE_MODE": "supabase", "SUPABASE_URL": "https://x.supabase.co",
      "SUPABASE_SERVICE_ROLE_KEY": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"}, "looks truncated"),
    ({"UC05_DATABASE_MODE": "supabase", "SUPABASE_URL": "https://x.supabase.co",
      "SUPABASE_SERVICE_ROLE_KEY": "sb_publishable_abc123"}, "publishable"),
])
def test_missing_or_invalid_configuration_is_reported_without_values(env, message):
    with pytest.raises(SupabaseConfigurationError) as exc:
        database_mode(env) == DatabaseMode.SUPABASE and load_supabase_settings(env)
    assert message in str(exc.value)
    assert "same" not in str(exc.value).replace("SUPABASE", "") and "k1" not in str(exc.value)


def test_runtime_refuses_to_start_with_missing_configuration(monkeypatch):
    with pytest.raises(RuntimeConfigurationError, match="SUPABASE_URL is not set"):
        build_context({"HR_AGENT_ENV": "test", "UC05_DATABASE_MODE": "supabase", "LLM_MODE": "offline"})


def test_default_mode_is_mock():
    assert database_mode({}) == DatabaseMode.MOCK


def test_invalid_credentials_fail_at_startup_without_leaking_the_key(fake):
    fake.auth_refused = True
    with pytest.raises(PersistenceAuthError) as exc:
        build_supabase_stores(SETTINGS, transport=fake.transport)
    assert "service-role-test-key" not in str(exc.value) and "service-role-test-key" not in repr(SETTINGS)


def test_unreachable_database_fails_at_startup(fake):
    fake.down = True
    with pytest.raises(PersistenceUnavailableError):
        build_supabase_stores(SETTINGS, transport=fake.transport)


def test_missing_table_fails_schema_verification(fake):
    del fake.tables["buddy_assignments"]
    with pytest.raises(SchemaMismatchError, match="buddy_assignments"):
        build_supabase_stores(SETTINGS, transport=fake.transport)


def test_reads_are_retried_but_writes_never_are(repo, fake):
    fake.fail_next("GET", "onboarding_cases", status=503)
    assert repo.get_onboarding_record(EMP).employee_id == EMP  # one transient read failure absorbed
    fake.fail_next("POST", "audit_events", status=503)
    with pytest.raises(PersistenceUnavailableError):
        link(repo)
    assert len(fake.calls("POST", "audit_events")) == 1  # not retried blindly
    link(repo)  # the deliberate retry is idempotent (deterministic key)
    with pytest.raises(DuplicateOnboardingCaseError):
        link(repo)


def test_database_write_failure_surfaces_as_a_persistence_error(repo, fake):
    link(repo)
    fake.fail_next("POST", "orientation_sessions", status=400, code="23514")
    with pytest.raises(PersistenceError) as exc:
        session_for(repo)
    assert exc.value.code == "CHECK_VIOLATION"


# =========================================================================== D. the complete UC05 workflow

def _actor(client_id, user="user:hr-admin-01", roles=("hr_admin",)):
    return Actor(on_behalf_of=user, roles=list(roles), purpose_of_use=Purpose.ONBOARDING, client_id=client_id)


def build_world(fake):
    stores = build_supabase_stores(SETTINGS, transport=fake.transport, clock=lambda: NOW)
    deps = build_fake_dependencies(clock=lambda: NOW, repository=stores.repository, directory=stores.directory,
                                   email=stores.events, run_recorder=stores.runs)
    settings = get_settings(AGENT_HOME)
    ctx = AgentContext(settings=settings, llm=offline_llm(), policies=PolicyStore(settings.policy_dir),
                       audit=stores.audit, hitl=stores.hitl, connector=deps)
    agent = OnboardingAgent(ctx)
    ctx.hitl.subscribe(agent.handle_approval_decision)
    return SimpleNamespace(fake=fake, stores=stores, deps=deps, ctx=ctx, agent=agent, hitl=ctx.hitl,
                           repo=stores.repository.for_tenant(A))


def offer(world, case_id=CASE_A, client=A):
    return world.agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.OFFER_ACCEPTED, event_id=case_id),
                                 actor=_actor(client), instance=case_id)


def decide(world, approval_id, decision, *, approver="user:hr-admin-01", roles=("hr_admin",), client=A, **kw):
    if decision in (Decision.REJECTED, Decision.EDITED):
        kw.setdefault("reason_code", ReasonCode.TIMING)
    world.hitl.decide(approval_id, decision, approver=approver, approver_roles=list(roles), client_id=client, **kw)
    return world.agent.workflow_state(workflow_id_for(WorkflowEvent.APPROVAL_DECIDED, client, approval_id))


def pending(world, proposal_type, client=A):
    return [i for i in world.hitl.list(status=Decision.PENDING, client_id=client) if i.proposal_type == proposal_type]


def test_complete_uc05_workflow_persists_every_record_in_supabase(fake):
    w = build_world(fake)

    _, wf = offer(w)  # 1-4: authoritative case -> checklist -> induction hold -> HR approval
    assert wf["stage"] == Stage.INDUCTION_AWAITING_APPROVAL
    assert fake.rows("audit_events", action=m.ACTION_CASE_LINKED, case_id=CASE_A)
    (induction_row,) = fake.rows("orientation_sessions", case_id=CASE_A)
    # holidays and working days come from the client's tables: 1 Dec 09:00 Dubai is the first free slot
    assert induction_row["start_utc"] == "2026-12-01T05:00:00+00:00" and induction_row["status"] == "SCHEDULED"
    # the new hire has a session_participants row; the team lead (no e-mail address held) is recorded on the session
    assert {r["actor_id"] for r in fake.rows("session_participants", session_id=induction_row["session_id"])} == {EMP}
    assert set(induction_row["session_metadata"]["participants_without_email"]) == {TL}
    (induction_item,) = pending(w, "session_booking")
    assert fake.rows("approval_requests", request_id=induction_item.id)[0]["outcome"] == "PENDING"

    wf = decide(w, induction_item.id, Decision.APPROVED)  # 5 + 8: confirmed, buddy proposed
    assert wf["stage"] == Stage.BUDDY_AWAITING_APPROVAL
    confirmed = fake.rows("orientation_sessions", case_id=CASE_A)[0]
    assert confirmed["status"] == "SCHEDULED" and confirmed["session_metadata"]["uc05_status"] == "confirmed"
    assert fake.rows("buddy_assignments", case_id=CASE_A)[0]["status"] == "PROPOSED"

    (buddy_item,) = pending(w, "buddy_assignment")
    wf = decide(w, buddy_item.id, Decision.APPROVED)  # buddy confirmed, both notified, team intro drafted
    assert wf["stage"] == Stage.TEAM_INTRO_AWAITING_APPROVAL
    (assignment,) = fake.rows("buddy_assignments", case_id=CASE_A)
    assert assignment["status"] == "CONFIRMED" and assignment["buddy_employee_id"] == BUDDY
    assert len(fake.rows("audit_events", action=m.ACTION_NOTIFICATION, case_id=CASE_A)) == 2

    (intro_item,) = pending(w, "session_booking")
    wf = decide(w, intro_item.id, Decision.APPROVED, approver=TL, roles=("team_lead",))
    assert wf["stage"] == Stage.TEAM_INTRO_CONFIRMED

    _, wf = w.agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.PARTICIPANT_RESPONDED, employee_id=EMP,
                                                      session_id=induction_row["session_id"], participant_id=EMP,
                                                      response=ResponseStatus.ACCEPTED), actor=_actor(A), instance=EMP)
    assert wf["stage"] == Stage.ATTENDANCE_CONFIRMED
    assert fake.rows("session_participants", actor_id=EMP, session_id=induction_row["session_id"])[0]["rsvp_status"] == "ACCEPTED"

    _, wf = w.agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.JOINING_DAY, as_of=date(2026, 12, 1)),
                              actor=_actor(A), instance="joining-day:2026-12-01")
    assert wf["stage"] == Stage.WELCOME_AWAITING_APPROVAL
    (comm,) = fake.rows("communications", case_id=CASE_A)
    assert comm["status"] == "AWAITING_REVIEW" and comm["review_gate_id"]
    (welcome_item,) = pending(w, "welcome_message")
    wf = decide(w, welcome_item.id, Decision.APPROVED)
    assert wf["stage"] == Stage.WELCOME_SENT
    comm = fake.rows("communications", case_id=CASE_A)[0]
    assert comm["status"] == "APPROVED" and comm["dispatched_at"] is None  # MockNotifier: nothing left the system

    _, again = w.agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.JOINING_DAY, as_of=date(2026, 12, 1)),
                                 actor=_actor(A), instance="joining-day:2026-12-01b")
    assert again["stage"] == Stage.NOTHING_TO_DO and len(fake.rows("communications", case_id=CASE_A)) == 1

    runs = fake.tables["agent_runs"]
    assert runs and all(r["client_id"] == A and r["agent_id"] == "employee-onboarding" for r in runs)
    actions = {r["action"] for r in fake.tables["audit_events"] if r["client_id"] == A}
    assert {"scheduling.session_booked", "scheduling.buddy_assignment_proposed", "scheduling.buddy_assigned",
            "hitl.submitted", "hitl.decided", "welcome.draft_submitted", "workflow.completed"} <= actions
    assert all(r["client_id"] == A for r in fake.tables["approval_requests"])
    assert fake.rows("orientation_sessions", client_id=B) == [] and fake.rows("communications", client_id=B) == []
    assert fake.calls("POST", "onboarding_cases") == [] and fake.calls("PATCH", "onboarding_cases") == []


def test_workflow_uses_the_clients_holidays(fake):
    fake.tables["client_holidays"] = []
    fake.seed("client_holidays", {"client_id": A, "holiday_date": "2026-12-01", "name": "H1", "is_observed": True},
              {"client_id": A, "holiday_date": "2026-12-02", "name": "H2", "is_observed": True})
    w = build_world(fake)
    offer(w)
    (row,) = fake.rows("orientation_sessions", case_id=CASE_A)
    assert row["start_utc"] == "2026-12-03T05:00:00+00:00"  # both holidays skipped


def test_retried_offer_after_a_partial_failure_creates_nothing_twice(fake):
    w = build_world(fake)
    fake.fail_next("POST", "orientation_sessions", status=503)
    with pytest.raises(Exception):  # the persistence outage stops the run part-way
        w.agent.graph.invoke(w.agent.initial_state(OnboardingWorkflowInput(event=WorkflowEvent.OFFER_ACCEPTED,
                                                                           event_id=CASE_A)),
                             config={"configurable": {"rc": _rc(w)}})
    _, wf = offer(w)  # redelivered: resumes, books once
    assert wf["stage"] == Stage.INDUCTION_AWAITING_APPROVAL
    assert len(fake.rows("orientation_sessions", case_id=CASE_A)) == 1
    assert len(fake.rows("audit_events", action=m.ACTION_CASE_LINKED)) == 1
    assert len(pending(w, "session_booking")) == 1


def _rc(w):
    from hr_onboarding_common.agent import RunContext
    from hr_onboarding_common.contracts import AgentRequest, Provenance, Scope, WorkflowRef
    request = AgentRequest[OnboardingWorkflowInput](
        workflow=WorkflowRef(id="employee-onboarding", instance=CASE_A, step="offer_accepted"), actor=_actor(A),
        scope=Scope(subject_ids=[]), input=OnboardingWorkflowInput(event=WorkflowEvent.OFFER_ACCEPTED, event_id=CASE_A))
    policy = w.agent.policy
    return RunContext(w.agent, request, policy, Provenance(agent="employee-onboarding", agent_version="t"))


def test_missing_client_calendar_routes_scheduling_to_hr(fake):
    for v in fake.rows("client_config_versions", client_id=A):
        v["status"] = "draft"
    w = build_world(fake)
    _, wf = offer(w)
    assert wf["stage"] == Stage.SCHEDULING_FAILED
    assert wf["errors"][-1]["error"] == "ClientCalendarUnavailableError"
    assert fake.rows("orientation_sessions", case_id=CASE_A) == []


def test_offer_for_another_tenants_case_is_refused(fake):
    w = build_world(fake)
    _, wf = offer(w, case_id=CASE_B, client=A)
    assert wf["stage"] == Stage.INVALID_EMPLOYEE_DATA
    assert fake.tables["orientation_sessions"] == [] and not fake.rows("audit_events", action=m.ACTION_CASE_LINKED)


def test_joining_date_revision_is_read_live_and_flags_stale_sessions(fake):
    w = build_world(fake)
    offer(w)
    fake.rows("onboarding_cases", case_id=CASE_A)[0]["joining_date"] = "2026-12-08"  # revised upstream (UC01/UC02)
    assert w.repo.get_onboarding_record(EMP).start_date == date(2026, 12, 8)  # never a cached date
    status = w.agent.case_status(_actor(A), EMP)
    assert status["stale_sessions"] == [fake.rows("orientation_sessions", case_id=CASE_A)[0]["session_id"]]


def test_rejected_buddy_releases_the_proposal(fake):
    w = build_world(fake)
    offer(w)
    decide(w, pending(w, "session_booking")[0].id, Decision.APPROVED)
    wf = decide(w, pending(w, "buddy_assignment")[0].id, Decision.REJECTED)
    assert wf["stage"] == Stage.BUDDY_ASSIGNMENT_FAILED
    assert fake.rows("buddy_assignments", case_id=CASE_A)[0]["status"] == "DECLINED"
    assert w.repo.get_onboarding_record(EMP).buddy_id is None


def test_rejected_induction_cancels_the_session_row(fake):
    w = build_world(fake)
    offer(w)
    wf = decide(w, pending(w, "session_booking")[0].id, Decision.REJECTED)
    assert wf["stage"] == Stage.MEETING_REJECTED
    assert fake.rows("orientation_sessions", case_id=CASE_A)[0]["status"] == "CANCELLED"
    assert fake.rows("approval_requests", outcome="REJECTED")


def test_registry_binds_every_store_to_the_actor_tenant(fake):
    w = build_world(fake)
    reg = OnboardingToolRegistry.from_dependencies(w.deps, llm=w.ctx.llm, policy=w.agent.policy, hitl=w.hitl,
                                                   audit=w.ctx.audit, actor=_actor(B))
    assert reg.port.tenant == B and reg.directory.tenant == B and reg.email_port.tenant == B
