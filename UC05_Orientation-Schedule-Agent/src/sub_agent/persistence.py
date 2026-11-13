"""Persistent storage: the onboarding repository (OnboardingPort) and the joining-day job store,
both in one SQLite database file (<HR_AGENT_DATA_DIR>/onboarding.db).

Why SQLite. It is the storage this project already configures for durable state: the HITL inbox
(hitl.db) and the LangGraph checkpoints (checkpoints.db) are SQLite files in the same HR_AGENT_DATA_DIR,
switched on by the same HR_AGENT_PERSIST=1. It needs no new service or driver (stdlib `sqlite3`), and it
gives real transactions and unique constraints. No Enterprise HR Agent-approved server database has been
identified, and none is available here to test against.

Suitability -- what this is and is not fit for:
  - One host, local disk. WAL mode, `BEGIN IMMEDIATE` write transactions and a busy timeout make one
    API process plus the scheduler process on the same machine safe.
  - NOT for several hosts or replicas, network file systems (NFS/SMB), or high availability. Those
    need a server database (e.g. PostgreSQL) implementing the same OnboardingPort contract; the
    contract tests in tests/test_onboarding_repository.py are the acceptance suite for it.
  - Backups must copy the database together with its -wal file (or use the sqlite3 backup API).
  - Personal data (names, email addresses) is stored unencrypted; encrypt the volume.

Uniqueness rules from the repository contract are enforced by the schema itself (partial unique
indexes), not only by application checks, so concurrent writers cannot break them.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from hr_onboarding_common.contracts import now_utc
from sub_agent.models import (BuddyCandidate, CaseStatus, ClientConfig,
                              CommunicationDraft, CommunicationStatus,
                              DuplicateOnboardingCaseError, DuplicateSessionError, DuplicateTaskError, Invitation,
                              InvitationStatus, JobStatus, JoiningDayJob, Notification, OnboardingRecord,
                              OnboardingSession, OnboardingTask, OperationRecord, ParticipantResponse,
                              ResponseStatus, SessionStatus, TaskStatus, TenantMismatchError,
                              UnsupportedLanguageError)

SCHEMA_VERSION = 1
M = TypeVar("M", bound=BaseModel)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (employee_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_records_client ON records(client_id);
CREATE TABLE IF NOT EXISTS client_configs (client_id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS buddy_candidates (client_id TEXT NOT NULL, employee_id TEXT NOT NULL,
    active INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY (client_id, employee_id));
CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, employee_id TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_notifications_employee ON notifications(employee_id);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, employee_id TEXT NOT NULL, client_id TEXT NOT NULL,
    task_type TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_live ON tasks(employee_id, task_type) WHERE status != 'cancelled';
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, employee_id TEXT NOT NULL, client_id TEXT NOT NULL,
    session_type TEXT NOT NULL, start_utc TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_live ON sessions(employee_id, session_type, start_utc)
    WHERE status != 'cancelled';
CREATE INDEX IF NOT EXISTS ix_sessions_employee ON sessions(employee_id);
CREATE TABLE IF NOT EXISTS participant_responses (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    participant_id TEXT NOT NULL, body TEXT NOT NULL, UNIQUE (session_id, participant_id));
CREATE TABLE IF NOT EXISTS invitations (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_invitations_session ON invitations(session_id);
CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, employee_id TEXT NOT NULL, client_id TEXT NOT NULL,
    body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_drafts_employee ON drafts(employee_id);
CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, client_id TEXT NOT NULL, status TEXT NOT NULL,
    body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS joining_day_jobs (client_id TEXT NOT NULL, run_date TEXT NOT NULL,
    status TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (client_id, run_date));
"""


class Database:
    """One SQLite connection shared by the repository and the job store, with a process-local
    lock (the connection is used from the API's worker threads) and IMMEDIATE write transactions
    (which also serialize writers in other processes on the same file)."""

    def __init__(self, path: Path | str):
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self.lock = threading.RLock()
        with self.lock:
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.executescript(_SCHEMA)
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"{self.path} has schema version {version}; this code supports {SCHEMA_VERSION}")
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def rows(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def close(self) -> None:
        with self.lock:
            self.conn.close()


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class SqliteOnboardingRepository:
    """OnboardingPort over SQLite. Mirrors MockOnboarding's behaviour exactly; both pass the same
    contract tests."""

    def __init__(self, database: Database | Path | str):
        self.db = database if isinstance(database, Database) else Database(database)

    # ---- generic helpers
    def _one(self, model: type[M], table: str, column: str, value: str) -> M:
        found = self.db.rows(f"SELECT body FROM {table} WHERE {column} = ?", (value,))
        if not found:
            raise KeyError(value)
        return model.model_validate_json(found[0][0])

    def _many(self, model: type[M], sql: str, args: tuple = ()) -> list[M]:
        return [model.model_validate_json(r[0]) for r in self.db.rows(sql, args)]

    @staticmethod
    def _check_tenant(owner_client_id: str, expected_client_id: str | None, kind: str, record_id: str) -> None:
        if expected_client_id is not None and owner_client_id != expected_client_id:
            raise TenantMismatchError(f"{kind} {record_id} is not accessible to this client")

    # ---- reference data (loaded by an operator; seeded with synthetic data in development)
    def upsert_client_config(self, config: ClientConfig) -> None:
        with self.db.transaction() as c:
            c.execute("INSERT INTO client_configs VALUES (?, ?) ON CONFLICT(client_id) DO UPDATE SET body = excluded.body",
                      (config.client_id, config.model_dump_json()))

    def upsert_buddy_candidate(self, candidate: BuddyCandidate) -> None:
        with self.db.transaction() as c:
            c.execute("INSERT INTO buddy_candidates VALUES (?, ?, ?, ?) ON CONFLICT(client_id, employee_id) "
                      "DO UPDATE SET active = excluded.active, body = excluded.body",
                      (candidate.client_id, candidate.employee_id, int(candidate.active), candidate.model_dump_json()))

    def is_empty(self) -> bool:
        return not self.db.rows("SELECT 1 FROM records LIMIT 1") and not self.db.rows("SELECT 1 FROM client_configs LIMIT 1")

    # ---- employee records
    def get_onboarding_record(self, employee_id: str) -> OnboardingRecord:
        return self._one(OnboardingRecord, "records", "employee_id", employee_id)

    def list_onboarding_records(self, *, client_id: str | None = None) -> list[OnboardingRecord]:
        if client_id is None:
            return self._many(OnboardingRecord, "SELECT body FROM records ORDER BY employee_id")
        return self._many(OnboardingRecord, "SELECT body FROM records WHERE client_id = ? ORDER BY employee_id",
                          (client_id,))

    def create_onboarding_case(self, record: OnboardingRecord) -> OnboardingRecord:
        try:
            with self.db.transaction() as c:
                c.execute("INSERT INTO records VALUES (?, ?, ?)",
                          (record.employee_id, record.client_id, record.model_dump_json()))
        except sqlite3.IntegrityError:
            raise DuplicateOnboardingCaseError(f"an onboarding case already exists for {record.employee_id}") from None
        return self.get_onboarding_record(record.employee_id)

    def _update_record(self, employee_id: str, **changes) -> OnboardingRecord:
        with self.db.transaction() as c:
            found = c.execute("SELECT body FROM records WHERE employee_id = ?", (employee_id,)).fetchone()
            if not found:
                raise KeyError(employee_id)
            updated = OnboardingRecord.model_validate_json(found[0]).model_copy(update=changes)
            c.execute("UPDATE records SET body = ? WHERE employee_id = ?", (updated.model_dump_json(), employee_id))
        return updated

    def update_case_status(self, employee_id: str, status: CaseStatus) -> OnboardingRecord:
        return self._update_record(employee_id, case_status=status)

    def list_buddy_candidates(self, client_id: str) -> list[BuddyCandidate]:
        return self._many(BuddyCandidate, "SELECT body FROM buddy_candidates WHERE client_id = ? AND active = 1 "
                                          "ORDER BY employee_id", (client_id,))

    def assign_buddy(self, employee_id: str, buddy_id: str) -> OnboardingRecord:
        return self._update_record(employee_id, buddy_id=buddy_id)

    def record_notification(self, employee_id: str, recipient_id: str, kind: str, **detail: object) -> Notification:
        notification = Notification(employee_id=employee_id, recipient_id=recipient_id, kind=kind, detail=detail)
        with self.db.transaction() as c:
            c.execute("INSERT INTO notifications VALUES (?, ?, ?)",
                      (notification.id, employee_id, notification.model_dump_json()))
        return notification

    def list_notifications(self, employee_id: str) -> list[Notification]:
        return self._many(Notification, "SELECT body FROM notifications WHERE employee_id = ? ORDER BY id",
                          (employee_id,))

    # ---- tasks
    def create_task(self, task: OnboardingTask) -> OnboardingTask:
        try:
            with self.db.transaction() as c:
                c.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", (task.id, task.employee_id, task.client_id,
                                                                         task.task_type, task.status,
                                                                         task.model_dump_json()))
        except sqlite3.IntegrityError:
            raise DuplicateTaskError(f"task '{task.task_type}' already exists for {task.employee_id}") from None
        return task.model_copy(deep=True)

    def get_task(self, task_id: str, *, client_id: str | None = None) -> OnboardingTask:
        task = self._one(OnboardingTask, "tasks", "id", task_id)
        self._check_tenant(task.client_id, client_id, "task", task_id)
        return task

    def list_tasks(self, employee_id: str) -> list[OnboardingTask]:
        return self._many(OnboardingTask, "SELECT body FROM tasks WHERE employee_id = ? ORDER BY id", (employee_id,))

    def update_task_status(self, task_id: str, status: TaskStatus) -> OnboardingTask:
        with self.db.transaction() as c:
            found = c.execute("SELECT body FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not found:
                raise KeyError(task_id)
            task = OnboardingTask.model_validate_json(found[0])
            updated = task.model_copy(update={"status": status, "completed_at": now_utc()
                                              if status == TaskStatus.COMPLETED else task.completed_at})
            c.execute("UPDATE tasks SET status = ?, body = ? WHERE id = ?", (status, updated.model_dump_json(), task_id))
        return updated

    # ---- sessions
    def create_session(self, session: OnboardingSession) -> OnboardingSession:
        try:
            with self.db.transaction() as c:
                c.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (session.id, session.employee_id, session.client_id, session.session_type,
                           _utc(session.scheduled_start), session.status, session.model_dump_json()))
        except sqlite3.IntegrityError:
            raise DuplicateSessionError(f"a {session.session_type} session already exists for "
                                        f"{session.employee_id} at {session.scheduled_start}") from None
        return session.model_copy(deep=True)

    def get_session(self, session_id: str, *, client_id: str | None = None) -> OnboardingSession:
        session = self._one(OnboardingSession, "sessions", "id", session_id)
        self._check_tenant(session.client_id, client_id, "session", session_id)
        return session

    def list_sessions(self, employee_id: str) -> list[OnboardingSession]:
        return self._many(OnboardingSession, "SELECT body FROM sessions WHERE employee_id = ? ORDER BY id",
                          (employee_id,))

    def update_session_status(self, session_id: str, status: SessionStatus) -> OnboardingSession:
        with self.db.transaction() as c:
            found = c.execute("SELECT body FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not found:
                raise KeyError(session_id)
            updated = OnboardingSession.model_validate_json(found[0]).model_copy(update={"status": status})
            c.execute("UPDATE sessions SET status = ?, body = ? WHERE id = ?",
                      (status, updated.model_dump_json(), session_id))
        return updated

    def record_participant_response(self, session_id: str, participant_id: str, response: ResponseStatus, *,
                                    responded_at: datetime | None = None) -> ParticipantResponse:
        when = responded_at or now_utc()
        with self.db.transaction() as c:
            found = c.execute("SELECT body FROM participant_responses WHERE session_id = ? AND participant_id = ?",
                              (session_id, participant_id)).fetchone()
            if found:
                recorded = ParticipantResponse.model_validate_json(found[0])
                if recorded.responded_at > when:
                    return recorded  # newest wins: an older, late-delivered response never overwrites
                recorded = recorded.model_copy(update={"response": response, "responded_at": when})
                c.execute("UPDATE participant_responses SET body = ? WHERE id = ?",
                          (recorded.model_dump_json(), recorded.id))
            else:
                recorded = ParticipantResponse(session_id=session_id, participant_id=participant_id,
                                               response=response, responded_at=when)
                c.execute("INSERT INTO participant_responses VALUES (?, ?, ?, ?)",
                          (recorded.id, session_id, participant_id, recorded.model_dump_json()))
        return recorded

    def list_participant_responses(self, session_id: str) -> list[ParticipantResponse]:
        return self._many(ParticipantResponse, "SELECT body FROM participant_responses WHERE session_id = ? "
                                               "ORDER BY id", (session_id,))

    def record_invitation(self, session_id: str, participant_id: str, status: InvitationStatus) -> Invitation:
        invitation = Invitation(session_id=session_id, participant_id=participant_id, status=status)
        with self.db.transaction() as c:
            c.execute("INSERT INTO invitations VALUES (?, ?, ?)", (invitation.id, session_id,
                                                                  invitation.model_dump_json()))
        return invitation

    def update_invitation_status(self, invitation_id: str, status: InvitationStatus) -> Invitation:
        with self.db.transaction() as c:
            found = c.execute("SELECT body FROM invitations WHERE id = ?", (invitation_id,)).fetchone()
            if not found:
                raise KeyError(invitation_id)
            updated = Invitation.model_validate_json(found[0]).model_copy(update={"status": status,
                                                                                  "sent_at": now_utc()})
            c.execute("UPDATE invitations SET body = ? WHERE id = ?", (updated.model_dump_json(), invitation_id))
        return updated

    def list_invitations(self, session_id: str) -> list[Invitation]:
        return self._many(Invitation, "SELECT body FROM invitations WHERE session_id = ? ORDER BY id", (session_id,))

    # ---- client configuration
    def get_client_config(self, client_id: str) -> ClientConfig:
        return self._one(ClientConfig, "client_configs", "client_id", client_id)

    # ---- communication drafts
    def create_communication_draft(self, draft: CommunicationDraft) -> CommunicationDraft:
        config = self.get_client_config(draft.client_id)
        if draft.language not in config.enabled_languages:
            raise UnsupportedLanguageError(f"language '{draft.language}' is not enabled for {draft.client_id} "
                                           f"(enabled: {config.enabled_languages})")
        with self.db.transaction() as c:
            c.execute("INSERT INTO drafts VALUES (?, ?, ?, ?)", (draft.id, draft.employee_id, draft.client_id,
                                                                draft.model_dump_json()))
        return draft.model_copy(deep=True)

    def get_communication_draft(self, draft_id: str, *, client_id: str | None = None) -> CommunicationDraft:
        draft = self._one(CommunicationDraft, "drafts", "id", draft_id)
        self._check_tenant(draft.client_id, client_id, "communication draft", draft_id)
        return draft

    def list_communication_drafts(self, employee_id: str) -> list[CommunicationDraft]:
        return self._many(CommunicationDraft, "SELECT body FROM drafts WHERE employee_id = ? ORDER BY id",
                          (employee_id,))

    def update_communication_status(self, draft_id: str, status: CommunicationStatus) -> CommunicationDraft:
        with self.db.transaction() as c:
            found = c.execute("SELECT body FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            if not found:
                raise KeyError(draft_id)
            updated = CommunicationDraft.model_validate_json(found[0]).model_copy(update={"status": status})
            c.execute("UPDATE drafts SET body = ? WHERE id = ?", (updated.model_dump_json(), draft_id))
        return updated

    # ---- idempotency records
    def get_operation(self, key: str) -> OperationRecord | None:
        found = self.db.rows("SELECT body FROM operations WHERE key = ?", (key,))
        return OperationRecord.model_validate_json(found[0][0]) if found else None

    def save_operation(self, record: OperationRecord) -> OperationRecord:
        stored = record.model_copy(update={"updated_at": now_utc()})
        with self.db.transaction() as c:
            c.execute("INSERT INTO operations VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                      "status = excluded.status, body = excluded.body",
                      (stored.key, stored.client_id, stored.status, stored.model_dump_json()))
        return stored

    def list_operations(self, *, client_id: str | None = None, statuses: tuple[str, ...] = ()) -> list[OperationRecord]:
        records = self._many(OperationRecord, "SELECT body FROM operations ORDER BY key")
        return [r for r in records if (client_id is None or r.client_id == client_id)
                and (not statuses or r.status in statuses)]


class SqliteJobStore:
    """Durable joining-day job claims (see sub_agent.scheduler.JobStore). The claim is a single
    IMMEDIATE transaction, so two schedulers -- threads or processes -- can never both claim the
    same tenant-date while a lease is live."""

    def __init__(self, database: Database | Path | str):
        self.db = database if isinstance(database, Database) else Database(database)

    def _save(self, c: sqlite3.Connection, job: JoiningDayJob) -> None:
        c.execute("INSERT INTO joining_day_jobs VALUES (?, ?, ?, ?) ON CONFLICT(client_id, run_date) DO UPDATE SET "
                  "status = excluded.status, body = excluded.body",
                  (job.client_id, job.run_date.isoformat(), job.status, job.model_dump_json()))

    def _load(self, c: sqlite3.Connection, client_id: str, run_date: date) -> JoiningDayJob | None:
        found = c.execute("SELECT body FROM joining_day_jobs WHERE client_id = ? AND run_date = ?",
                          (client_id, run_date.isoformat())).fetchone()
        return JoiningDayJob.model_validate_json(found[0]) if found else None

    def get(self, client_id: str, run_date: date) -> JoiningDayJob | None:
        with self.db.lock:
            return self._load(self.db.conn, client_id, run_date)

    def claim(self, client_id: str, run_date: date, *, now: datetime, lease: timedelta,
              max_attempts: int) -> JoiningDayJob | None:
        with self.db.transaction() as c:
            job = self._load(c, client_id, run_date)
            if job is not None:
                if job.status == JobStatus.DONE or job.attempts >= max_attempts:
                    return None
                if job.status == JobStatus.RUNNING and job.lease_until is not None and job.lease_until > now:
                    return None  # another worker holds a live lease
            claimed = JoiningDayJob(client_id=client_id, run_date=run_date, status=JobStatus.RUNNING,
                                    attempts=(job.attempts if job else 0) + 1, lease_until=now + lease,
                                    last_error=job.last_error if job else None, updated_at=now)
            self._save(c, claimed)
        return claimed

    def complete(self, client_id: str, run_date: date, *, now: datetime) -> None:
        self._finish(client_id, run_date, now=now, status=JobStatus.DONE, error=None)

    def fail(self, client_id: str, run_date: date, *, now: datetime, error: str) -> None:
        self._finish(client_id, run_date, now=now, status=JobStatus.FAILED, error=error[:500])

    def _finish(self, client_id: str, run_date: date, *, now: datetime, status: JobStatus, error: str | None) -> None:
        with self.db.transaction() as c:
            job = self._load(c, client_id, run_date)
            if job is None:
                raise KeyError(f"{client_id}:{run_date}")
            self._save(c, job.model_copy(update={"status": status, "lease_until": None, "last_error": error,
                                                 "updated_at": now}))
