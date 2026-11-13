"""Persistent storage and recovery across a restart (Phase 4, finding B).

A "restart" here builds a completely new set of in-process objects -- repository, HITL inbox,
checkpointer, agent -- over the same SQLite files, exactly what a new process would do. The
external systems (calendar, mailbox, directory, notifier) are deliberately shared across the
restart: they are outside the process and keep their state when it dies.
"""

import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from connectors.calendar.base import CalendarBookingError
from connectors.notifications.base import NotificationDeliveryError
from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.config import get_settings
from hr_onboarding_common.contracts import Actor, Purpose
from hr_onboarding_common.platform.hitl import Decision, HITLApplyError
from sub_agent import reconciliation
from sub_agent.agent import AGENT_HOME, OnboardingAgent, OnboardingWorkflowInput, Stage, WorkflowEvent, \
    build_checkpointer, workflow_id_for
from sub_agent.fakes import (FakeCalendar, FakeNotifier, build_fake_dependencies, build_mock_email_adapter,
                             build_mock_employee_directory, offline_llm, seed_synthetic_data)
from sub_agent.models import (BuddyCandidate, CaseStatus, CommunicationStatus, DuplicateSessionError, JobStatus,
                              OnboardingSession, OperationStatus, SessionStatus, SessionType)
from sub_agent.persistence import SCHEMA_VERSION, Database, SqliteJobStore, SqliteOnboardingRepository
from sub_agent.runtime import build_context

TEST_A, ACME = "CLIENT-TEST-A", "CLIENT-ACME"
NEW_HIRE, BUDDY = "EMP-TEST-001", "EMP-TEST-050"
NOW = datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)
HR = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING, client_id=TEST_A)


@pytest.fixture
def external():
    """The systems outside the process: they survive a restart of the agent."""
    return dict(calendar=FakeCalendar(), notifier=FakeNotifier(), email=build_mock_email_adapter(),
                directory=build_mock_employee_directory())


def boot(data_dir, external, monkeypatch):
    """One 'process': every durable store opened from data_dir, as runtime.build_context does."""
    monkeypatch.setenv("HR_AGENT_DATA_DIR", str(data_dir))
    repository = SqliteOnboardingRepository(Database(data_dir / "onboarding.db"))
    if repository.is_empty():
        seed_synthetic_data(repository)
        repository.upsert_buddy_candidate(BuddyCandidate(employee_id=BUDDY, full_name="Robin Park", client_id=TEST_A,
                                                         team="Platform"))
    deps = build_fake_dependencies(repository=repository, checkpointer=build_checkpointer(data_dir),
                                   clock=lambda: NOW, **external)
    ctx = AgentContext.build(agent_home=AGENT_HOME, llm=offline_llm(), connector=deps,
                             settings=get_settings(AGENT_HOME), persist=True)
    agent = OnboardingAgent(ctx)
    ctx.hitl.subscribe(agent.handle_approval_decision)
    return SimpleNamespace(ctx=ctx, agent=agent, repo=repository, hitl=ctx.hitl)


def offer(app, event_id="EVT-001"):
    return app.agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.OFFER_ACCEPTED, event_id=event_id),
                               actor=HR, instance=event_id)


def pending(app, proposal_type, client_id=TEST_A):
    return [i for i in app.hitl.list(status=Decision.PENDING, client_id=client_id) if i.proposal_type == proposal_type]


# =========================================================================== restart

def test_a_case_its_approvals_and_checkpoints_survive_a_restart_and_the_workflow_continues(tmp_path, external,
                                                                                            monkeypatch):
    first = boot(tmp_path, external, monkeypatch)
    offer(first)
    (item,) = pending(first, "session_booking")
    del first  # the process is gone

    second = boot(tmp_path, external, monkeypatch)
    assert second.repo.get_onboarding_record(NEW_HIRE).client_id == TEST_A
    assert len(second.repo.list_tasks(NEW_HIRE)) == 5
    status = second.agent.case_status(HR, NEW_HIRE)
    assert status["stage"] == Stage.INDUCTION_AWAITING_APPROVAL and status["pending_approvals"][0]["id"] == item.id
    response, _ = offer(second)  # a redelivered event after the restart is still a duplicate
    assert [a.tool for a in response.actions_taken] == ["workflow.duplicate_event"]

    second.hitl.decide(item.id, Decision.APPROVED, approver="user:hr-admin-01", approver_roles=["hr_admin"],
                       client_id=TEST_A)

    assert second.repo.get_session(item.payload["id"]).status == SessionStatus.CONFIRMED
    state = second.agent.workflow_state(workflow_id_for(WorkflowEvent.APPROVAL_DECIDED, TEST_A, item.id))
    assert state["stage"] == Stage.BUDDY_AWAITING_APPROVAL
    assert len(external["calendar"]._bookings) == 1 and len(second.repo.list_sessions(NEW_HIRE)) == 1


def test_a_run_interrupted_by_an_outage_resumes_after_a_restart_without_repeating_steps(tmp_path, external,
                                                                                         monkeypatch):
    first = boot(tmp_path, external, monkeypatch)
    external["calendar"].lose_next_booking_responses = 1  # the event is created, the response is lost
    with pytest.raises(CalendarBookingError):
        offer(first)
    report = reconciliation.report(first.agent, TEST_A)
    assert not report["consistent"]
    assert [r["pending_nodes"] for r in report["interrupted_runs"]] == [["draft_induction_meeting"]]
    assert [o["status"] for o in report["unsettled_operations"]] == [OperationStatus.UNCERTAIN]
    del first

    second = boot(tmp_path, external, monkeypatch)
    _, wf = offer(second)  # redelivered after the restart

    assert wf["stage"] == Stage.INDUCTION_AWAITING_APPROVAL
    assert len(second.repo.list_tasks(NEW_HIRE)) == 5  # the checklist step was not repeated
    assert len(external["calendar"]._bookings) == 1  # the lost booking was reused through its idempotency key
    assert reconciliation.report(second.agent, TEST_A)["consistent"]


def test_a_failed_decision_is_retried_after_a_restart_and_the_welcome_is_sent_once(tmp_path, external,
                                                                                    monkeypatch):
    first = boot(tmp_path, external, monkeypatch)
    actor = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
                  client_id=ACME)
    first.agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.JOINING_DAY, as_of=date(2026, 11, 2)),
                          actor=actor, instance="joining-day:2026-11-02")
    (item,) = pending(first, "welcome_message", client_id=ACME)
    external["notifier"].lose_next_responses = 1
    with pytest.raises(HITLApplyError):
        first.hitl.decide(item.id, Decision.APPROVED, approver="user:hr-admin-01", approver_roles=["hr_admin"],
                          client_id=ACME)
    assert [d["approval_id"] for d in reconciliation.report(first.agent, ACME)["failed_decisions"]] == [item.id]
    del first

    second = boot(tmp_path, external, monkeypatch)
    second.hitl.retry_apply(item.id, client_id=ACME)

    assert second.repo.get_communication_draft(item.payload["id"]).status == CommunicationStatus.SENT
    assert len(external["notifier"].sent_to("EMP-001", kind="welcome")) == 1
    assert reconciliation.report(second.agent, ACME)["consistent"]


def test_runtime_build_context_uses_the_persistent_repository_and_job_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HR_AGENT_DATA_DIR", str(tmp_path))
    env = {"HR_AGENT_ENV": "development", "HR_AGENT_PERSIST": "1"}
    ctx, jobs = build_context(env)
    assert isinstance(ctx.connector.repository, SqliteOnboardingRepository) and isinstance(jobs, SqliteJobStore)
    ctx.connector.repository.update_case_status("EMP-001", CaseStatus.WITHDRAWN)
    jobs.claim(ACME, date(2026, 11, 2), now=NOW, lease=timedelta(minutes=5), max_attempts=3)

    again, jobs_again = build_context(env)  # a restart: nothing re-seeded, nothing lost

    assert again.connector.repository.get_onboarding_record("EMP-001").case_status == CaseStatus.WITHDRAWN
    assert jobs_again.get(ACME, date(2026, 11, 2)).status == JobStatus.RUNNING
    assert {p.name for p in tmp_path.iterdir()} >= {"onboarding.db", "hitl.db", "checkpoints.db"}


# =========================================================================== SQLite specifics

def test_uniqueness_holds_across_two_connections_to_the_same_file(tmp_path):
    a = SqliteOnboardingRepository(tmp_path / "onboarding.db")
    seed_synthetic_data(a)
    b = SqliteOnboardingRepository(tmp_path / "onboarding.db")  # e.g. the scheduler process
    start = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
    session = dict(employee_id="EMP-001", client_id=ACME, session_type=SessionType.ORIENTATION, scheduled_start=start,
                   scheduled_end=start + timedelta(hours=1))
    a.create_session(OnboardingSession(**session))
    with pytest.raises(DuplicateSessionError):
        b.create_session(OnboardingSession(**session))
    # the same instant written in another offset is the same slot
    with pytest.raises(DuplicateSessionError):
        b.create_session(OnboardingSession(**{**session, "scheduled_start": start.astimezone(
            timezone(timedelta(hours=4))), "scheduled_end": start + timedelta(hours=2)}))


def test_concurrent_job_claims_from_two_connections_admit_exactly_one_worker(tmp_path):
    SqliteOnboardingRepository(tmp_path / "onboarding.db")  # creates the schema
    stores = [SqliteJobStore(tmp_path / "onboarding.db") for _ in range(8)]
    barrier, claimed = threading.Barrier(len(stores)), []

    def claim(store):
        barrier.wait()
        if store.claim(ACME, date(2026, 11, 2), now=NOW, lease=timedelta(minutes=5), max_attempts=3):
            claimed.append(store)

    threads = [threading.Thread(target=claim, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 1


def test_a_database_from_a_newer_schema_version_is_refused(tmp_path):
    path = tmp_path / "onboarding.db"
    Database(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="schema version"):
        Database(path)


def test_reconciliation_finds_a_hold_whose_approval_was_never_created(tmp_path, external, monkeypatch):
    app = boot(tmp_path, external, monkeypatch)
    offer(app)
    start = datetime(2026, 12, 3, 6, 0, tzinfo=timezone.utc)
    orphan = app.repo.create_session(OnboardingSession(employee_id=NEW_HIRE, client_id=TEST_A,
                                                       session_type=SessionType.ORIENTATION, scheduled_start=start,
                                                       scheduled_end=start + timedelta(hours=1)))

    report = reconciliation.report(app.agent, TEST_A)

    assert [s["session_id"] for s in report["sessions_without_approval"]] == [orphan.id]
    assert reconciliation.report(app.agent, ACME)["consistent"]  # another tenant sees none of it


def test_an_approval_request_lost_mid_send_is_reported_then_settled_on_resume(tmp_path, external, monkeypatch):
    app = boot(tmp_path, external, monkeypatch)
    external["notifier"].lose_next_responses = 1
    with pytest.raises(NotificationDeliveryError):
        offer(app)
    (op,) = [o for o in reconciliation.report(app.agent, TEST_A)["unsettled_operations"] if o["kind"] == "notification"]
    assert op["key"].startswith(f"approval-request:{TEST_A}:")

    offer(app)

    assert reconciliation.report(app.agent, TEST_A)["consistent"]
    assert len(external["notifier"].sent_to("role:hr_admin", kind="approval_request")) == 1
