"""The OnboardingPort contract suite: every test runs against BOTH implementations -- the in-memory
MockOnboarding and the persistent SqliteOnboardingRepository -- seeded with the same synthetic data.

Covers CRUD, Pydantic validation, tenant isolation, duplicate prevention, idempotency records and
returned-copy semantics. A new repository implementation (e.g. PostgreSQL) must pass this suite.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sub_agent.fakes import build_mock_onboarding, seed_synthetic_data
from sub_agent.persistence import SqliteOnboardingRepository
from sub_agent.models import (BrandingConfig, ClientConfig,
                              CommunicationDraft, CommunicationStatus,
                              DuplicateSessionError,
                              DuplicateTaskError, InvitationStatus, OnboardingSession, OnboardingTask,
                              OperationRecord, OperationStatus, ResponseStatus, SessionStatus, SessionType, TaskStatus,
                              TenantMismatchError, UnsupportedLanguageError)

ACME = "CLIENT-ACME"
GLOBEX = "CLIENT-GLOBEX"


@pytest.fixture(params=["memory", "sqlite"])
def onboarding(request, tmp_path):
    if request.param == "memory":
        return build_mock_onboarding()
    repository = SqliteOnboardingRepository(tmp_path / "onboarding.db")
    seed_synthetic_data(repository)
    return repository


# --------------------------------------------------------------------------- employee records (CRUD: read)

def test_get_and_list_onboarding_records(onboarding):
    record = onboarding.get_onboarding_record("EMP-001")
    assert record.full_name == "Sara Al Marzooqi"

    acme_only = onboarding.list_onboarding_records(client_id=ACME)
    assert {r.employee_id for r in acme_only} == {"EMP-001", "EMP-002"}
    assert all(r.client_id == ACME for r in acme_only)


def test_get_onboarding_record_missing_raises_key_error(onboarding):
    with pytest.raises(KeyError):
        onboarding.get_onboarding_record("EMP-DOES-NOT-EXIST")


# --------------------------------------------------------------------------- tasks (CRUD + duplicate + tenant)

def test_task_crud_lifecycle(onboarding):
    created = onboarding.create_task(OnboardingTask(employee_id="EMP-001", client_id=ACME,
                                                     task_type="nda_signature", due_date=date(2026, 11, 3)))
    assert created.status == TaskStatus.PENDING

    fetched = onboarding.get_task(created.id)
    assert fetched == created

    completed = onboarding.update_task_status(created.id, TaskStatus.COMPLETED)
    assert completed.status == TaskStatus.COMPLETED
    assert completed.completed_at is not None

    assert [t.id for t in onboarding.list_tasks("EMP-001")] == [created.id]


def test_duplicate_task_is_rejected(onboarding):
    onboarding.create_task(OnboardingTask(employee_id="EMP-001", client_id=ACME, task_type="it_setup"))
    with pytest.raises(DuplicateTaskError):
        onboarding.create_task(OnboardingTask(employee_id="EMP-001", client_id=ACME, task_type="it_setup"))


def test_cancelled_task_does_not_block_recreation(onboarding):
    first = onboarding.create_task(OnboardingTask(employee_id="EMP-001", client_id=ACME, task_type="it_setup"))
    onboarding.update_task_status(first.id, TaskStatus.CANCELLED)
    second = onboarding.create_task(OnboardingTask(employee_id="EMP-001", client_id=ACME, task_type="it_setup"))
    assert second.id != first.id


def test_task_tenant_isolation(onboarding):
    task = onboarding.create_task(OnboardingTask(employee_id="EMP-001", client_id=ACME, task_type="it_setup"))

    assert onboarding.get_task(task.id, client_id=ACME).id == task.id
    with pytest.raises(TenantMismatchError):
        onboarding.get_task(task.id, client_id=GLOBEX)


def test_task_type_must_be_a_string(onboarding):
    with pytest.raises(ValidationError):
        OnboardingTask(employee_id="EMP-001", client_id=ACME, task_type=None)


# --------------------------------------------------------------------------- sessions (CRUD + duplicate + tenant)

def _session(**overrides) -> OnboardingSession:
    base = dict(employee_id="EMP-001", client_id=ACME, session_type=SessionType.ORIENTATION,
               scheduled_start=datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc),
               scheduled_end=datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc))
    base.update(overrides)
    return OnboardingSession(**base)


def test_session_crud_lifecycle(onboarding):
    created = onboarding.create_session(_session())
    assert created.status == SessionStatus.SCHEDULED

    confirmed = onboarding.update_session_status(created.id, SessionStatus.CONFIRMED)
    assert confirmed.status == SessionStatus.CONFIRMED
    assert [s.id for s in onboarding.list_sessions("EMP-001")] == [created.id]


def test_duplicate_booking_is_rejected(onboarding):
    onboarding.create_session(_session())
    with pytest.raises(DuplicateSessionError):
        onboarding.create_session(_session())


def test_session_tenant_isolation(onboarding):
    session = onboarding.create_session(_session())
    with pytest.raises(TenantMismatchError):
        onboarding.get_session(session.id, client_id=GLOBEX)


def test_session_end_must_be_after_start():
    with pytest.raises(ValidationError):
        _session(scheduled_end=datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc))  # before scheduled_start


def test_session_end_equal_to_start_is_rejected():
    with pytest.raises(ValidationError):
        _session(scheduled_end=datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc))  # same instant as start


# --------------------------------------------------------------------------- client configuration

def test_get_client_config(onboarding):
    config = onboarding.get_client_config(ACME)
    assert config.enabled_languages == ["en", "ar"]
    assert "welcome" in config.templates


def test_get_client_config_missing_raises_key_error(onboarding):
    with pytest.raises(KeyError):
        onboarding.get_client_config("CLIENT-DOES-NOT-EXIST")


def test_default_language_must_be_one_of_enabled_languages():
    with pytest.raises(ValidationError):
        ClientConfig(client_id=GLOBEX, name="Globex Industries", enabled_languages=["en"],
                    default_language="fr", branding=BrandingConfig(sender_name="Globex HR"))


# --------------------------------------------------------------------------- communication drafts

def test_communication_draft_crud_lifecycle(onboarding):
    created = onboarding.create_communication_draft(CommunicationDraft(
        employee_id="EMP-001", client_id=ACME, template_id="welcome", language="en",
        subject="Welcome!", body="Welcome to Acme."))
    assert created.status == CommunicationStatus.DRAFT

    approved = onboarding.update_communication_status(created.id, CommunicationStatus.APPROVED)
    assert approved.status == CommunicationStatus.APPROVED
    assert [d.id for d in onboarding.list_communication_drafts("EMP-001")] == [created.id]


def test_communication_draft_rejects_language_not_enabled_for_client(onboarding):
    with pytest.raises(UnsupportedLanguageError):
        onboarding.create_communication_draft(CommunicationDraft(
            employee_id="EMP-101", client_id=GLOBEX, template_id="welcome", language="ar",
            subject="مرحبا", body="مرحبا بك"))


def test_communication_draft_tenant_isolation(onboarding):
    created = onboarding.create_communication_draft(CommunicationDraft(
        employee_id="EMP-001", client_id=ACME, template_id="welcome", language="en",
        subject="Welcome!", body="Welcome to Acme."))
    with pytest.raises(TenantMismatchError):
        onboarding.get_communication_draft(created.id, client_id=GLOBEX)


# --------------------------------------------------------------------------- Phase 4 additions

def _stored_session(onboarding) -> OnboardingSession:
    start = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
    return onboarding.create_session(OnboardingSession(employee_id="EMP-001", client_id=ACME,
                                                       session_type=SessionType.ORIENTATION, scheduled_start=start,
                                                       scheduled_end=start + timedelta(hours=1)))


def test_participant_responses_are_newest_wins_by_responded_at(onboarding):
    session = _stored_session(onboarding)
    t1 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    onboarding.record_participant_response(session.id, "TL-001", ResponseStatus.ACCEPTED,
                                           responded_at=t1 + timedelta(hours=1))

    returned = onboarding.record_participant_response(session.id, "TL-001", ResponseStatus.DECLINED, responded_at=t1)

    assert returned.response == ResponseStatus.ACCEPTED and returned.responded_at == t1 + timedelta(hours=1)
    (stored,) = onboarding.list_participant_responses(session.id)
    assert stored.response == ResponseStatus.ACCEPTED
    newer = onboarding.record_participant_response(session.id, "TL-001", ResponseStatus.TENTATIVE,
                                                   responded_at=t1 + timedelta(hours=2))
    assert newer.response == ResponseStatus.TENTATIVE and len(onboarding.list_participant_responses(session.id)) == 1


def test_invitation_status_can_move_from_pending_to_sent(onboarding):
    session = _stored_session(onboarding)
    invitation = onboarding.record_invitation(session.id, "EMP-001", InvitationStatus.PENDING)

    updated = onboarding.update_invitation_status(invitation.id, InvitationStatus.SENT)

    assert updated.status == InvitationStatus.SENT
    assert [i.status for i in onboarding.list_invitations(session.id)] == [InvitationStatus.SENT]
    with pytest.raises(KeyError):
        onboarding.update_invitation_status("invite-missing", InvitationStatus.SENT)


def test_operation_records_upsert_by_key_and_filter_by_tenant_and_status(onboarding):
    assert onboarding.get_operation("welcome:CLIENT-ACME:comm-1") is None
    op = onboarding.save_operation(OperationRecord(key="welcome:CLIENT-ACME:comm-1", kind="notification",
                                                   client_id=ACME, detail={"recipient_id": "EMP-001"}))
    onboarding.save_operation(OperationRecord(key="welcome:CLIENT-GLOBEX:comm-2", kind="notification",
                                              client_id=GLOBEX, status=OperationStatus.UNCERTAIN))

    onboarding.save_operation(op.model_copy(update={"status": OperationStatus.SUCCEEDED, "provider_ref": "dlv-1"}))

    stored = onboarding.get_operation("welcome:CLIENT-ACME:comm-1")
    assert (stored.status, stored.provider_ref, stored.operation_id) == (OperationStatus.SUCCEEDED, "dlv-1",
                                                                        op.operation_id)
    assert stored.detail == {"recipient_id": "EMP-001"}
    assert [o.key for o in onboarding.list_operations(client_id=ACME)] == ["welcome:CLIENT-ACME:comm-1"]
    assert [o.key for o in onboarding.list_operations(statuses=(OperationStatus.UNCERTAIN,))] == [
        "welcome:CLIENT-GLOBEX:comm-2"]
    stored.detail["recipient_id"] = "changed"  # a returned copy, not the stored record
    assert onboarding.get_operation("welcome:CLIENT-ACME:comm-1").detail == {"recipient_id": "EMP-001"}


def test_session_booking_operation_key_round_trips(onboarding):
    start = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)
    session = onboarding.create_session(OnboardingSession(
        employee_id="EMP-001", client_id=ACME, session_type=SessionType.INDUCTION, scheduled_start=start,
        scheduled_end=start + timedelta(minutes=90), booking_operation_key="calendar:CLIENT-ACME:EMP-001:x"))
    assert onboarding.get_session(session.id).booking_operation_key == "calendar:CLIENT-ACME:EMP-001:x"
