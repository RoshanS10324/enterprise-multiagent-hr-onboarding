"""TEST / DEMO ONLY. Every in-memory stand-in this project uses, assembled in one place.

Nothing here talks to a real system. Each double is deterministic, holds synthetic data only, and
can simulate the outcomes callers must handle. The connector simulations are defined next to the
interface they implement, in connectors/<name>/mock.py, and re-exported here (with their former
names) so existing imports keep working:

  connectors.calendar.mock            MockCalendar (= FakeCalendar), FAILING_ATTENDEE
  connectors.email.mock               MockEmailAdapter, build_mock_email_adapter, FAILING_EVENT_ID,
                                      MockEmailSender (outbound email only)
  connectors.employee_directory.mock  MockEmployeeDirectory, build_mock_employee_directory
  connectors.notifications.mock       MockNotifier (= FakeNotifier), every channel
  connectors.teams.mock               MockTeamsAdapter (Teams only)

Defined here, because they stand in for this agent's own components rather than external systems:

  FakeLLM handlers (offline_llm)  deterministic model output for policy Q&A and the welcome note
  MockOnboarding                  the OnboardingPort repository in memory (the persistent one is
                                  sub_agent.persistence.SqliteOnboardingRepository)
  InMemoryJobStore, FakeClock     the joining-day scheduler's job store and a settable clock

build_fake_dependencies() assembles all of them into the bundle the agent and API consume.
api.py wires these in only because no real adapter exists (CONNECTOR_MODE=offline is the only
supported mode), and HR_AGENT_ENV=production refuses to start with them (sub_agent.runtime).
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime, timedelta
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel

from connectors.calendar.mock import FAILING_ATTENDEE, MockCalendar
from connectors.email.mock import FAILING_EVENT_ID, MockEmailAdapter, MockEmailSender, build_mock_email_adapter
from connectors.employee_directory.mock import MockEmployeeDirectory, build_mock_employee_directory
from connectors.notifications.mock import MockNotifier
from connectors.teams.mock import MockTeamsAdapter
from context_layer.knowledge.policy_index import PolicyKnowledgeBase
from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.llm import FakeLLM, extract_input_block
from sub_agent.models import (BrandingConfig, BuddyCandidate, CaseStatus,
                              ClientConfig, CommunicationDraft, CommunicationStatus,
                              DuplicateOnboardingCaseError, DuplicateSessionError, DuplicateTaskError, Invitation,
                              InvitationStatus, JobStatus, JoiningDayJob, MessageTemplate, Notification,
                              OnboardingRecord, OnboardingSession, OnboardingTask, OperationRecord, ParticipantResponse,
                              ResponseStatus, SessionStatus, TaskStatus, TenantMismatchError, UnsupportedLanguageError)
from sub_agent.persistence import SqliteOnboardingRepository
from sub_agent.services.policy_qa import CitedClaim, PolicyQAModelOutput
from sub_agent.services.welcome import PersonalNoteOutput
from sub_agent.tool_registry import OnboardingDependencies


# =========================================================================== LLM (offline mode)

_CANDIDATE_BLOCK = re.compile(
    r'<untrusted_document source="policy_document:(?P<ref>C\d+)">\n(?P<text>.*?)\n</untrusted_document>', re.S)


def policy_qa(system: str, user: str, schema: type[BaseModel]) -> PolicyQAModelOutput:
    """Parses the actual synthetic data only. Live-model quality is an evaluation concern."""
    payload = extract_input_block(user)
    candidates = {m["ref"]: m["text"] for m in _CANDIDATE_BLOCK.finditer(user)}
    question_words = {w for w in payload["question"].lower().split() if len(w) > 3}

    best_ref, best_overlap = None, 0
    for ref, text in candidates.items():
        overlap = sum(1 for w in question_words if w in text.lower())
        if overlap > best_overlap:
            best_ref, best_overlap = ref, overlap

    if best_ref is None or best_overlap == 0:
        return PolicyQAModelOutput(answer=None, cited_claims=[], insufficient_evidence=True)

    quote = candidates[best_ref].strip()
    return PolicyQAModelOutput(answer=f"Per {best_ref}: {quote}",
                               cited_claims=[CitedClaim(citation_ref=best_ref, quote=quote)],
                               insufficient_evidence=False)


def welcome_note(system: str, user: str, schema: type[BaseModel]) -> PersonalNoteOutput:
    payload = extract_input_block(user)
    first_name = payload["full_name"].split()[0]
    return PersonalNoteOutput(personal_note=f"We can't wait to see what you'll accomplish, {first_name}!")


def offline_llm() -> FakeLLM:
    return FakeLLM().on("onboarding.policy_qa", policy_qa).on("onboarding.welcome_note", welcome_note)


# =========================================================================== onboarding repository

class MockOnboarding:
    """In-memory OnboardingPort (sub_agent.repository). Not-found lookups raise KeyError (caught by
    BaseAgent.run -> ExceptionCode.MISSING_DATA, same convention as hr_recruitment_common's MockATS).
    Enforces the repository contract documented in sub_agent/repository.py."""

    def __init__(self, records: list[OnboardingRecord], client_configs: list[ClientConfig], *,
                 buddy_candidates: list[BuddyCandidate] | None = None,
                 tasks: list[OnboardingTask] | None = None, sessions: list[OnboardingSession] | None = None,
                 drafts: list[CommunicationDraft] | None = None,
                 participant_responses: list[ParticipantResponse] | None = None,
                 invitations: list[Invitation] | None = None,
                 notifications: list[Notification] | None = None):
        self.records: dict[str, OnboardingRecord] = {r.employee_id: r for r in records}
        self.client_configs: dict[str, ClientConfig] = {c.client_id: c for c in client_configs}
        self.buddy_candidates: list[BuddyCandidate] = list(buddy_candidates or [])
        self.tasks: dict[str, OnboardingTask] = {t.id: t for t in (tasks or [])}
        self.sessions: dict[str, OnboardingSession] = {s.id: s for s in (sessions or [])}
        self.drafts: dict[str, CommunicationDraft] = {d.id: d for d in (drafts or [])}
        self.participant_responses: dict[str, ParticipantResponse] = {r.id: r for r in (participant_responses or [])}
        self.invitations: dict[str, Invitation] = {i.id: i for i in (invitations or [])}
        self.notifications: dict[str, Notification] = {n.id: n for n in (notifications or [])}
        self.operations: dict[str, OperationRecord] = {}

    # ---- employee records
    def get_onboarding_record(self, employee_id: str) -> OnboardingRecord:
        return self.records[employee_id].model_copy(deep=True)

    def list_onboarding_records(self, *, client_id: str | None = None) -> list[OnboardingRecord]:
        recs = [r for r in self.records.values() if client_id is None or r.client_id == client_id]
        return [r.model_copy(deep=True) for r in sorted(recs, key=lambda r: r.employee_id)]

    def create_onboarding_case(self, record: OnboardingRecord) -> OnboardingRecord:
        if record.employee_id in self.records:
            raise DuplicateOnboardingCaseError(f"an onboarding case already exists for {record.employee_id}")
        self.records[record.employee_id] = record.model_copy(deep=True)
        return self.records[record.employee_id].model_copy(deep=True)

    def update_case_status(self, employee_id: str, status: CaseStatus) -> OnboardingRecord:
        updated = self.records[employee_id].model_copy(update={"case_status": status})
        self.records[employee_id] = updated
        return updated.model_copy(deep=True)

    def list_buddy_candidates(self, client_id: str) -> list[BuddyCandidate]:
        return [b.model_copy(deep=True) for b in sorted(self.buddy_candidates, key=lambda b: b.employee_id)
                if b.client_id == client_id and b.active]

    def assign_buddy(self, employee_id: str, buddy_id: str) -> OnboardingRecord:
        updated = self.records[employee_id].model_copy(update={"buddy_id": buddy_id})
        self.records[employee_id] = updated
        return updated.model_copy(deep=True)

    def record_notification(self, employee_id: str, recipient_id: str, kind: str, **detail: object) -> Notification:
        notification = Notification(employee_id=employee_id, recipient_id=recipient_id, kind=kind, detail=detail)
        self.notifications[notification.id] = notification
        return notification.model_copy(deep=True)

    def list_notifications(self, employee_id: str) -> list[Notification]:
        return [n.model_copy(deep=True) for n in sorted(self.notifications.values(), key=lambda n: n.id)
                if n.employee_id == employee_id]

    # ---- tasks
    def create_task(self, task: OnboardingTask) -> OnboardingTask:
        if any(t.employee_id == task.employee_id and t.task_type == task.task_type
               and t.status != TaskStatus.CANCELLED for t in self.tasks.values()):
            raise DuplicateTaskError(f"task '{task.task_type}' already exists for {task.employee_id}")
        self.tasks[task.id] = task.model_copy(deep=True)
        return self.tasks[task.id].model_copy(deep=True)

    def get_task(self, task_id: str, *, client_id: str | None = None) -> OnboardingTask:
        task = self.tasks[task_id]
        self._check_tenant(task.client_id, client_id, "task", task_id)
        return task.model_copy(deep=True)

    def list_tasks(self, employee_id: str) -> list[OnboardingTask]:
        return [t.model_copy(deep=True) for t in sorted(self.tasks.values(), key=lambda t: t.id)
                if t.employee_id == employee_id]

    def update_task_status(self, task_id: str, status: TaskStatus) -> OnboardingTask:
        task = self.tasks[task_id]
        updated = task.model_copy(update={"status": status,
                                          "completed_at": now_utc() if status == TaskStatus.COMPLETED
                                          else task.completed_at})
        self.tasks[task_id] = updated
        return updated.model_copy(deep=True)

    # ---- sessions
    def create_session(self, session: OnboardingSession) -> OnboardingSession:
        if any(s.employee_id == session.employee_id and s.session_type == session.session_type
               and s.scheduled_start == session.scheduled_start and s.status != SessionStatus.CANCELLED
               for s in self.sessions.values()):
            raise DuplicateSessionError(f"a {session.session_type} session already exists for "
                                        f"{session.employee_id} at {session.scheduled_start}")
        self.sessions[session.id] = session.model_copy(deep=True)
        return self.sessions[session.id].model_copy(deep=True)

    def get_session(self, session_id: str, *, client_id: str | None = None) -> OnboardingSession:
        session = self.sessions[session_id]
        self._check_tenant(session.client_id, client_id, "session", session_id)
        return session.model_copy(deep=True)

    def list_sessions(self, employee_id: str) -> list[OnboardingSession]:
        return [s.model_copy(deep=True) for s in sorted(self.sessions.values(), key=lambda s: s.id)
                if s.employee_id == employee_id]

    def update_session_status(self, session_id: str, status: SessionStatus) -> OnboardingSession:
        updated = self.sessions[session_id].model_copy(update={"status": status})
        self.sessions[session_id] = updated
        return updated.model_copy(deep=True)

    def record_participant_response(self, session_id: str, participant_id: str, response: ResponseStatus, *,
                                    responded_at: datetime | None = None) -> ParticipantResponse:
        """Upsert by (session_id, participant_id): a participant changing their RSVP updates the
        existing record rather than accumulating a new one each time."""
        when = responded_at or now_utc()
        existing = next((r for r in self.participant_responses.values()
                         if r.session_id == session_id and r.participant_id == participant_id), None)
        if existing:
            if existing.responded_at > when:
                return existing.model_copy(deep=True)  # newest wins (see the OnboardingPort contract)
            updated = existing.model_copy(update={"response": response, "responded_at": when})
            self.participant_responses[existing.id] = updated
            return updated.model_copy(deep=True)
        new = ParticipantResponse(session_id=session_id, participant_id=participant_id, response=response,
                                  responded_at=when)
        self.participant_responses[new.id] = new
        return new.model_copy(deep=True)

    def list_participant_responses(self, session_id: str) -> list[ParticipantResponse]:
        return [r.model_copy(deep=True) for r in sorted(self.participant_responses.values(), key=lambda r: r.id)
                if r.session_id == session_id]

    def record_invitation(self, session_id: str, participant_id: str, status: InvitationStatus) -> Invitation:
        new = Invitation(session_id=session_id, participant_id=participant_id, status=status)
        self.invitations[new.id] = new
        return new.model_copy(deep=True)

    def update_invitation_status(self, invitation_id: str, status: InvitationStatus) -> Invitation:
        updated = self.invitations[invitation_id].model_copy(update={"status": status, "sent_at": now_utc()})
        self.invitations[invitation_id] = updated
        return updated.model_copy(deep=True)

    def list_invitations(self, session_id: str) -> list[Invitation]:
        return [i.model_copy(deep=True) for i in sorted(self.invitations.values(), key=lambda i: i.id)
                if i.session_id == session_id]

    # ---- client configuration
    def get_client_config(self, client_id: str) -> ClientConfig:
        return self.client_configs[client_id].model_copy(deep=True)

    # ---- communication drafts
    def create_communication_draft(self, draft: CommunicationDraft) -> CommunicationDraft:
        config = self.client_configs[draft.client_id]
        if draft.language not in config.enabled_languages:
            raise UnsupportedLanguageError(f"language '{draft.language}' is not enabled for {draft.client_id} "
                                           f"(enabled: {config.enabled_languages})")
        self.drafts[draft.id] = draft.model_copy(deep=True)
        return self.drafts[draft.id].model_copy(deep=True)

    def get_communication_draft(self, draft_id: str, *, client_id: str | None = None) -> CommunicationDraft:
        draft = self.drafts[draft_id]
        self._check_tenant(draft.client_id, client_id, "communication draft", draft_id)
        return draft.model_copy(deep=True)

    def list_communication_drafts(self, employee_id: str) -> list[CommunicationDraft]:
        return [d.model_copy(deep=True) for d in sorted(self.drafts.values(), key=lambda d: d.id)
                if d.employee_id == employee_id]

    def update_communication_status(self, draft_id: str, status: CommunicationStatus) -> CommunicationDraft:
        updated = self.drafts[draft_id].model_copy(update={"status": status})
        self.drafts[draft_id] = updated
        return updated.model_copy(deep=True)

    # ---- idempotency records
    def get_operation(self, key: str) -> OperationRecord | None:
        record = self.operations.get(key)
        return record.model_copy(deep=True) if record else None

    def save_operation(self, record: OperationRecord) -> OperationRecord:
        stored = record.model_copy(update={"updated_at": now_utc()}, deep=True)
        self.operations[record.key] = stored
        return stored.model_copy(deep=True)

    def list_operations(self, *, client_id: str | None = None,
                        statuses: tuple[str, ...] = ()) -> list[OperationRecord]:
        return [r.model_copy(deep=True) for k, r in sorted(self.operations.items())
                if (client_id is None or r.client_id == client_id) and (not statuses or r.status in statuses)]

    # ---- helpers
    @staticmethod
    def _check_tenant(owner_client_id: str, expected_client_id: str | None, kind: str, record_id: str) -> None:
        if expected_client_id is not None and owner_client_id != expected_client_id:
            raise TenantMismatchError(f"{kind} {record_id} is not accessible to this client")


def build_mock_onboarding() -> MockOnboarding:
    """Synthetic world for offline mode, tests and evaluation. No real employee data."""
    records, client_configs, buddy_candidates = synthetic_onboarding_data()
    return MockOnboarding(records, client_configs, buddy_candidates=buddy_candidates)


def seed_synthetic_data(repository: SqliteOnboardingRepository, *, people: bool = True) -> None:
    """Loads the same synthetic world into a persistent repository (development and tests only). `people=False`
    loads only the client configurations (welcome templates, branding) -- no sample employees and no sample buddies."""
    records, client_configs, buddy_candidates = synthetic_onboarding_data()
    for config in client_configs:
        repository.upsert_client_config(config)
    if not people:
        return
    for candidate in buddy_candidates:
        repository.upsert_buddy_candidate(candidate)
    for record in records:
        repository.create_onboarding_case(record)


def synthetic_onboarding_data() -> tuple[list[OnboardingRecord], list[ClientConfig], list[BuddyCandidate]]:
    acme = ClientConfig(client_id="CLIENT-ACME", name="Acme Technologies", enabled_languages=["en", "ar"],
                        default_language="en",
                        branding=BrandingConfig(sender_name="Acme People Team", primary_color="#1A73E8"),
                        templates={"welcome": MessageTemplate(
                            template_id="welcome", language="en", subject="Welcome to {sender_name}, {full_name}!",
                            body="Dear {full_name},\n\nWelcome to {sender_name}! We're thrilled to have you join "
                                 "us as {role_title}, starting {start_date}.")})
    globex = ClientConfig(client_id="CLIENT-GLOBEX", name="Globex Industries", enabled_languages=["en"],
                          default_language="en",
                          branding=BrandingConfig(sender_name="Globex HR"),
                          templates={"welcome": MessageTemplate(
                              template_id="welcome", language="en", subject="{sender_name}: welcome aboard, {full_name}",
                              body="Hi {full_name},\n\n{sender_name} is glad to have you starting as {role_title} "
                                   "on {start_date}.")})
    # The tenant of the synthetic employee directory's EMP-TEST-* employees (connectors/
    # employee_directory/mock.py). CLIENT-TEST-B is deliberately left without a configuration, so a
    # tenant with none can be exercised.
    test_a = ClientConfig(client_id="CLIENT-TEST-A", name="Test Client A", enabled_languages=["en"],
                          default_language="en", branding=BrandingConfig(sender_name="Test Client A People Team"),
                          templates={"welcome": MessageTemplate(
                              template_id="welcome", language="en", subject="Welcome to {sender_name}, {full_name}!",
                              body="Dear {full_name},\n\nWelcome aboard as {role_title}, starting {start_date}.")})
    records = [
        OnboardingRecord(employee_id="EMP-001", full_name="Sara Al Marzooqi", email="sara.almarzooqi@example.com",
                         role_title="Backend Engineer", department="Engineering", team="Backend",
                         team_lead_id="TL-001", client_id="CLIENT-ACME", start_date=date(2026, 11, 2),
                         buddy_id="EMP-010"),
        OnboardingRecord(employee_id="EMP-002", full_name="Daniel Wren", email="daniel.wren@example.com",
                         role_title="Data Analyst", department="Data", team="Analytics", team_lead_id="TL-002",
                         client_id="CLIENT-ACME", start_date=date(2026, 11, 9), buddy_id=None),
        OnboardingRecord(employee_id="EMP-101", full_name="Mei Lin", email="mei.lin@example.com",
                         role_title="Support Engineer", department="Support", team="Tier 1",
                         team_lead_id="TL-101", client_id="CLIENT-GLOBEX", start_date=date(2026, 11, 16),
                         buddy_id=None),
    ]
    buddy_candidates = [
        BuddyCandidate(employee_id="EMP-010", full_name="Yousef Al Zaabi", client_id="CLIENT-ACME", team="Backend"),
        BuddyCandidate(employee_id="EMP-011", full_name="Nadia Haddad", client_id="CLIENT-ACME", team="Data"),
        BuddyCandidate(employee_id="EMP-110", full_name="Carlos Reyes", client_id="CLIENT-GLOBEX", team="Support"),
    ]
    return records, [acme, globex, test_a], buddy_candidates


# =========================================================================== connector simulations
# Defined in connectors/<name>/mock.py; the former names stay importable from here.

FakeCalendar = MockCalendar
FakeNotifier = MockNotifier


# =========================================================================== joining-day scheduler

class InMemoryJobStore:
    """The joining-day job store (sub_agent.scheduler.JobStore) in memory, with exactly the claim
    semantics of SqliteJobStore. For tests and HR_AGENT_PERSIST=0 development only: nothing survives a
    restart, so it cannot prevent a re-run after one."""

    def __init__(self) -> None:
        self.jobs: dict[tuple[str, date], JoiningDayJob] = {}
        self._lock = threading.Lock()

    def get(self, client_id: str, run_date: date) -> JoiningDayJob | None:
        job = self.jobs.get((client_id, run_date))
        return job.model_copy() if job else None

    def claim(self, client_id: str, run_date: date, *, now: datetime, lease: timedelta,
              max_attempts: int) -> JoiningDayJob | None:
        with self._lock:
            job = self.jobs.get((client_id, run_date))
            if job is not None:
                if job.status == JobStatus.DONE or job.attempts >= max_attempts:
                    return None
                if job.status == JobStatus.RUNNING and job.lease_until is not None and job.lease_until > now:
                    return None
            claimed = JoiningDayJob(client_id=client_id, run_date=run_date, status=JobStatus.RUNNING,
                                    attempts=(job.attempts if job else 0) + 1, lease_until=now + lease,
                                    last_error=job.last_error if job else None, updated_at=now)
            self.jobs[(client_id, run_date)] = claimed
            return claimed.model_copy()

    def complete(self, client_id: str, run_date: date, *, now: datetime) -> None:
        self._finish(client_id, run_date, now=now, status=JobStatus.DONE, error=None)

    def fail(self, client_id: str, run_date: date, *, now: datetime, error: str) -> None:
        self._finish(client_id, run_date, now=now, status=JobStatus.FAILED, error=error[:500])

    def _finish(self, client_id: str, run_date: date, *, now: datetime, status: JobStatus, error: str | None) -> None:
        with self._lock:
            job = self.jobs[(client_id, run_date)]
            self.jobs[(client_id, run_date)] = job.model_copy(update={"status": status, "lease_until": None,
                                                                      "last_error": error, "updated_at": now})


class FakeClock:
    """A settable clock for scheduler and workflow tests: `clock()` returns `now`; `advance()`
    moves it forward."""

    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


# =========================================================================== assembled bundle

def build_fake_dependencies(**overrides) -> OnboardingDependencies:
    """Every connector the agent uses, as fakes, plus an in-memory LangGraph checkpointer. Pass
    any field (repository=, calendar=, notifier=, ...) to substitute one double."""
    deps: dict[str, Any] = dict(
        repository=build_mock_onboarding(), calendar=MockCalendar(), kb=PolicyKnowledgeBase(),
        email=build_mock_email_adapter(), directory=build_mock_employee_directory(), notifier=MockNotifier(),
        checkpointer=MemorySaver())
    deps.update(overrides)
    return OnboardingDependencies(**deps)


__all__ = ["FAILING_ATTENDEE", "FAILING_EVENT_ID", "FakeCalendar", "FakeClock", "FakeNotifier",
           "InMemoryJobStore", "MockCalendar", "MockEmailAdapter", "MockEmailSender", "MockEmployeeDirectory",
           "MockNotifier", "MockOnboarding", "MockTeamsAdapter", "build_fake_dependencies", "build_mock_email_adapter",
           "build_mock_employee_directory", "build_mock_onboarding", "offline_llm", "policy_qa", "seed_synthetic_data",
           "synthetic_onboarding_data", "welcome_note"]
