"""Persistent storage for UC-02 runtime records.

Design
------
UC-02 holds its records in memory (``OnboardingService._records``), which does
not survive process exit. This module adds a durable store **behind a port**,
so the business layer is unchanged and the prototype keeps working with no
database at all.

Three layers, deliberately separated:

``TableClient``
    The narrow protocol this module needs from a backend: upsert rows, select
    rows matching equality filters, delete rows. The shape mirrors the
    PostgREST/``supabase-py`` client (``table(name).upsert(...)``) so the
    production adapter is a thin wrapper rather than a rewrite.

``InMemoryTableClient``
    A faithful local implementation: primary keys, upsert-by-key, equality
    filtering and ordering. Used by the whole test suite and by the demos, so
    persistence logic is exercised without a database.

``OnboardingStore``
    Maps :mod:`contracts` models to and from table rows and enforces the rules
    the schema enforces — one live onboarding per employee, append-only audit,
    tenant scoping on every read and write.

What is **not** here
--------------------
* No database driver. ``supabase``, ``psycopg`` and ``sqlalchemy`` are not
  imported and not installed; see ``ProviderDataError`` in :mod:`providers`
  for the same rule applied to the other seams.
* No credentials, endpoints or connection strings. Those belong in the
  environment; see ``.env.example``.
* No milestone or overdue storage. Both are derived (see
  ``supabase/migrations/20261027090000_uc02_init.sql`` for why).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from onboarding_task_management.contracts import (
    ApprovalRequest,
    AuditEvent,
    OnboardingRecord,
    OnboardingStatus,
    ReplanOutcome,
    TaskRecord,
)
from onboarding_task_management.errors import (
    DataIsolationError,
    DuplicateOnboardingError,
    OnboardingError,
)
from onboarding_task_management.sub_agent.checklist import ClientOnboardingConfig

#: Table names. Must match the migration; ``test_persistence`` asserts parity.
TABLE_ONBOARDING = "onboarding"
TABLE_TASK = "task"
TABLE_TASK_DEPENDENCY = "task_dependency"
TABLE_PLAN_REVISION = "plan_revision"
TABLE_APPROVAL_REQUEST = "approval_request"
TABLE_AGENT_RUN = "agent_run"
TABLE_AUDIT_EVENT = "audit_event"
TABLE_CLIENT_CONFIG_REF = "client_config_ref"

#: Primary key columns per table, mirroring the migration.
PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    TABLE_ONBOARDING: ("onboarding_id",),
    TABLE_TASK: ("task_id",),
    TABLE_TASK_DEPENDENCY: ("task_id", "depends_on_id"),
    TABLE_PLAN_REVISION: ("onboarding_id", "revision"),
    TABLE_APPROVAL_REQUEST: ("request_id",),
    TABLE_AGENT_RUN: ("run_id",),
    TABLE_AUDIT_EVENT: ("event_id",),
    TABLE_CLIENT_CONFIG_REF: ("client_id", "config_hash"),
}

#: Mirrors ``audit.FORBIDDEN_DETAIL_KEYS`` and the SQL trigger of the same name.
FORBIDDEN_DETAIL_KEYS = frozenset(
    {"name", "full_name", "email", "phone", "nationality", "passport", "address"}
)


class PersistenceError(OnboardingError):
    """A storage operation failed, or the backend returned something unusable.

    Raised rather than swallowed: a write that did not happen must never be
    reported as success (CLAUDE.md "verify before you claim").
    """

    code = "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Backend port
# ---------------------------------------------------------------------------


@runtime_checkable
class TableClient(Protocol):
    """Minimal row store. A production adapter wraps PostgREST or psycopg.

    Every method is tenant-agnostic; :class:`OnboardingStore` supplies the
    ``client_id`` filter on every call, and the database enforces it again
    through Row Level Security.
    """

    def upsert(self, table: str, rows: Sequence[dict[str, Any]]) -> int:
        """Insert or replace ``rows`` by primary key. Returns rows written."""
        ...

    def select(
        self, table: str, filters: dict[str, Any], order_by: str | None = None
    ) -> list[dict[str, Any]]:
        """Rows matching every equality filter."""
        ...

    def delete(self, table: str, filters: dict[str, Any]) -> int:
        """Delete rows matching every equality filter. Returns rows removed."""
        ...


class InMemoryTableClient:
    """Local :class:`TableClient`. Behaviour mirrors the migration's constraints.

    Not a toy: it enforces primary keys, rejects personal-data keys in audit
    detail and refuses to mutate an existing audit row, so the tests that
    cover those rules are meaningful without a database.
    """

    def __init__(self) -> None:
        self._tables: dict[str, dict[tuple, dict[str, Any]]] = {}

    @staticmethod
    def _key(table: str, row: dict[str, Any]) -> tuple:
        try:
            return tuple(row[column] for column in PRIMARY_KEYS[table])
        except KeyError as exc:
            raise PersistenceError(
                "row is missing a primary-key column", table=table, column=str(exc)
            ) from exc

    def upsert(self, table: str, rows: Sequence[dict[str, Any]]) -> int:
        if table not in PRIMARY_KEYS:
            raise PersistenceError("unknown table", table=table)
        store = self._tables.setdefault(table, {})
        for row in rows:
            if table == TABLE_AUDIT_EVENT:
                self._check_audit_row(store, row)
            store[self._key(table, row)] = dict(row)
        return len(rows)

    @staticmethod
    def _check_audit_row(
        store: dict[tuple, dict[str, Any]], row: dict[str, Any]
    ) -> None:
        """Append-only, and no personal-data keys — the SQL trigger's rules."""
        leaked = FORBIDDEN_DETAIL_KEYS & set(row.get("detail") or {})
        if leaked:
            raise PersistenceError(
                "audit detail must not contain personal data keys",
                keys=",".join(sorted(leaked)),
            )
        if (row["event_id"],) in store:
            raise PersistenceError(
                "audit_event is append-only", event_id=row["event_id"]
            )

    def select(
        self, table: str, filters: dict[str, Any], order_by: str | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in self._tables.get(table, {}).values()
            if all(row.get(column) == value for column, value in filters.items())
        ]
        if order_by:
            rows.sort(key=lambda r: (r.get(order_by) is None, r.get(order_by)))
        return rows

    def delete(self, table: str, filters: dict[str, Any]) -> int:
        store = self._tables.get(table, {})
        doomed = [
            key
            for key, row in store.items()
            if all(row.get(column) == value for column, value in filters.items())
        ]
        if table == TABLE_AUDIT_EVENT and doomed:
            raise PersistenceError("audit_event is append-only; delete refused")
        for key in doomed:
            del store[key]
        return len(doomed)

    # -- test/demo helpers -------------------------------------------------

    def row_count(self, table: str) -> int:
        return len(self._tables.get(table, {}))

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Deep copy of everything stored. Used to simulate a process restart."""
        return {
            table: [dict(row) for row in rows.values()]
            for table, rows in self._tables.items()
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, list[dict[str, Any]]]) -> InMemoryTableClient:
        """Rebuild from :meth:`snapshot`, as a new process would from the database."""
        client = cls()
        for table, rows in data.items():
            store = client._tables.setdefault(table, {})
            for row in rows:
                store[cls._key(table, row)] = dict(row)
        return client


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------


def _json_scalar(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def config_hash(config: ClientOnboardingConfig) -> str:
    """Stable content hash of a client configuration.

    Identifies *which* checklist definition produced a plan without copying
    client content into the database. Deterministic across processes: the
    model is dumped in JSON mode with sorted keys.
    """
    exclude = {"eligibility"} if getattr(config, "eligibility", None) is None else None  # unchanged hash for configs without one
    payload = json.dumps(config.model_dump(mode="json", exclude=exclude), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def onboarding_to_row(record: OnboardingRecord, config_ref: str | None = None) -> dict[str, Any]:
    data = record.model_dump(mode="json", exclude={"tasks"})
    data["config_hash"] = config_ref
    return data


def row_to_onboarding(
    row: dict[str, Any], tasks: Sequence[TaskRecord] = ()
) -> OnboardingRecord:
    payload = {k: v for k, v in row.items() if k != "config_hash"}
    payload["tasks"] = list(tasks)
    return OnboardingRecord.model_validate(payload)


def task_to_row(task: TaskRecord, plan_order: int = 0) -> dict[str, Any]:
    """Row for one task.

    ``plan_order`` carries the task's position in the topologically ordered
    plan. Without it a read would have to sort by ``task_id``, which is
    alphabetical and would present prerequisites after their dependants.
    """
    row = task.model_dump(mode="json", exclude={"depends_on"})
    row["plan_order"] = plan_order
    return row


def row_to_task(row: dict[str, Any], depends_on: Sequence[str] = ()) -> TaskRecord:
    payload = {k: v for k, v in row.items() if k != "plan_order"}
    payload["depends_on"] = list(depends_on)
    return TaskRecord.model_validate(payload)


def approval_to_row(request: ApprovalRequest) -> dict[str, Any]:
    return request.model_dump(mode="json")


def row_to_approval(row: dict[str, Any]) -> ApprovalRequest:
    return ApprovalRequest.model_validate(row)


def audit_to_row(event: AuditEvent) -> dict[str, Any]:
    return event.model_dump(mode="json")


def row_to_audit(row: dict[str, Any]) -> AuditEvent:
    return AuditEvent.model_validate(row)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class OnboardingStore:
    """Durable store for UC-02 records, scoped by ``client_id`` on every call.

    Writes are **idempotent**: saving the same record twice upserts by primary
    key and never duplicates a case, a task or a dependency edge. That is what
    makes a replayed trigger or a retried agent step safe.
    """

    def __init__(self, client: TableClient) -> None:
        self.client = client

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Make a group of writes atomic when the backend supports it.

        A client exposing ``transaction()`` (the SQLite client does) rolls the
        whole group back on error. A client without it (the in-memory one) runs
        the group as plain sequential writes, exactly as before.
        """
        opener = getattr(self.client, "transaction", None)
        with (opener() if callable(opener) else nullcontext()):
            yield

    def client_of_onboarding(self, onboarding_id: str) -> str | None:
        """Which client owns ``onboarding_id`` in storage, or ``None`` if unknown.

        Lets the service report a cross-client access as an isolation breach even
        when the owning client has not been loaded into memory in this process.
        """
        rows = self.client.select(TABLE_ONBOARDING, {"onboarding_id": onboarding_id})
        return rows[0]["client_id"] if rows else None

    # -- onboarding cases --------------------------------------------------

    def save_onboarding(
        self,
        record: OnboardingRecord,
        *,
        config: ClientOnboardingConfig | None = None,
    ) -> None:
        """Persist a case and its full task set. Idempotent.

        The task rows are replaced wholesale rather than diffed: a re-plan can
        add and remove tasks, and a stale row left behind would resurrect a
        task the planner removed.
        """
        with self.transaction():
            self._save_onboarding(record, config)

    def _save_onboarding(
        self, record: OnboardingRecord, config: ClientOnboardingConfig | None
    ) -> None:
        config_ref: str | None = None
        if config is not None:
            config_ref = config_hash(config)
            self.client.upsert(
                TABLE_CLIENT_CONFIG_REF,
                [
                    {
                        "client_id": config.client_id,
                        "config_hash": config_ref,
                        "template_count": len(config.templates),
                        "milestone_count": len(config.milestones),
                    }
                ],
            )

        self.client.upsert(
            TABLE_ONBOARDING, [onboarding_to_row(record, config_ref)]
        )

        existing = {
            row["task_id"]
            for row in self.client.select(
                TABLE_TASK, {"onboarding_id": record.onboarding_id}
            )
        }
        current = {task.task_id for task in record.tasks}
        for stale in sorted(existing - current):
            self.client.delete(TABLE_TASK_DEPENDENCY, {"task_id": stale})
            self.client.delete(TABLE_TASK, {"task_id": stale})

        if record.tasks:
            self.client.upsert(
                TABLE_TASK,
                [task_to_row(task, position)
                 for position, task in enumerate(record.tasks)],
            )
        for task in record.tasks:
            self.client.delete(TABLE_TASK_DEPENDENCY, {"task_id": task.task_id})
            if task.depends_on:
                self.client.upsert(
                    TABLE_TASK_DEPENDENCY,
                    [
                        {"task_id": task.task_id, "depends_on_id": dependency}
                        for dependency in task.depends_on
                    ],
                )

    def load_onboarding(
        self, client_id: str, onboarding_id: str
    ) -> OnboardingRecord | None:
        """Read one case. Returns ``None`` when it does not exist for ``client_id``.

        A case belonging to a *different* client raises rather than returning
        ``None``, so an isolation breach is reported as one instead of being
        indistinguishable from a typo.
        """
        rows = self.client.select(TABLE_ONBOARDING, {"onboarding_id": onboarding_id})
        if not rows:
            return None
        row = rows[0]
        if row["client_id"] != client_id:
            raise DataIsolationError(
                "stored onboarding belongs to another client",
                actor_client_id=client_id,
                onboarding_id=onboarding_id,
            )
        return row_to_onboarding(row, self._load_tasks(onboarding_id))

    def _load_tasks(self, onboarding_id: str) -> list[TaskRecord]:
        task_rows = self.client.select(
            TABLE_TASK, {"onboarding_id": onboarding_id}, order_by="plan_order"
        )
        tasks = []
        for row in task_rows:
            edges = self.client.select(
                TABLE_TASK_DEPENDENCY,
                {"task_id": row["task_id"]},
                order_by="depends_on_id",
            )
            tasks.append(row_to_task(row, [e["depends_on_id"] for e in edges]))
        return tasks

    def list_onboardings(self, client_id: str) -> list[OnboardingRecord]:
        return [
            row_to_onboarding(row, self._load_tasks(row["onboarding_id"]))
            for row in self.client.select(
                TABLE_ONBOARDING, {"client_id": client_id}, order_by="onboarding_id"
            )
        ]

    def assert_no_live_onboarding(self, client_id: str, employee_id: str) -> None:
        """Mirror of the partial unique index, checked before an insert."""
        for row in self.client.select(
            TABLE_ONBOARDING, {"client_id": client_id, "employee_id": employee_id}
        ):
            if row["status"] != OnboardingStatus.CANCELLED.value:
                raise DuplicateOnboardingError(
                    "a stored onboarding already exists for this employee",
                    onboarding_id=row["onboarding_id"],
                )

    # -- plan revisions ----------------------------------------------------

    def save_plan_revision(
        self, outcome: ReplanOutcome, *, client_id: str, applied_by: str,
        approval_request_id: str | None = None,
    ) -> None:
        self.client.upsert(
            TABLE_PLAN_REVISION,
            [
                {
                    "onboarding_id": outcome.onboarding_id,
                    "revision": outcome.revision,
                    "client_id": client_id,
                    "drivers": [d.value for d in outcome.drivers],
                    "added_task_ids": list(outcome.added_task_ids),
                    "removed_task_ids": list(outcome.removed_task_ids),
                    "rescheduled_task_ids": list(outcome.rescheduled_task_ids),
                    "retained_task_ids": list(outcome.retained_task_ids),
                    "approval_request_id": approval_request_id,
                    "applied_by": applied_by,
                }
            ],
        )

    def plan_revisions(self, client_id: str, onboarding_id: str) -> list[dict[str, Any]]:
        return self.client.select(
            TABLE_PLAN_REVISION,
            {"client_id": client_id, "onboarding_id": onboarding_id},
            order_by="revision",
        )

    # -- approvals ---------------------------------------------------------

    def save_approval(self, request: ApprovalRequest) -> None:
        self.client.upsert(TABLE_APPROVAL_REQUEST, [approval_to_row(request)])

    def load_approvals(self, client_id: str) -> list[ApprovalRequest]:
        return [
            row_to_approval(row)
            for row in self.client.select(
                TABLE_APPROVAL_REQUEST, {"client_id": client_id}, order_by="request_id"
            )
        ]

    # -- audit -------------------------------------------------------------

    def append_audit(self, events: Iterable[AuditEvent]) -> int:
        rows = [audit_to_row(event) for event in events]
        if not rows:
            return 0
        return self.client.upsert(TABLE_AUDIT_EVENT, rows)

    def load_audit(self, client_id: str) -> list[AuditEvent]:
        return [
            row_to_audit(row)
            for row in self.client.select(
                TABLE_AUDIT_EVENT, {"client_id": client_id}, order_by="event_id"
            )
        ]

    # -- agent runs --------------------------------------------------------

    def save_agent_run(self, row: dict[str, Any]) -> None:
        self.client.upsert(TABLE_AGENT_RUN, [row])

    def agent_runs(self, client_id: str) -> list[dict[str, Any]]:
        return self.client.select(
            TABLE_AGENT_RUN, {"client_id": client_id}, order_by="run_id"
        )


#: Environment variables selecting the storage mode. Neither is read from, or written
#: to, ``.env``; set them in the shell. Default is in-memory (no durable store).
ENV_STORE_MODE = "UC02_STORE_MODE"
ENV_SQLITE_PATH = "UC02_SQLITE_PATH"
STORE_MODES = ("memory", "sqlite", "supabase")
DEFAULT_SQLITE_PATH = Path(__file__).resolve().parents[2] / "local_db" / "uc02_dev.sqlite3"


def store_from_env(environ: Mapping[str, str] | None = None, *, rest=None) -> OnboardingStore | None:
    """The store the environment asks for, or ``None`` for in-memory mode.

    ``UC02_STORE_MODE=sqlite`` is explicit opt-in. If it is set and the database
    cannot be opened this RAISES :class:`PersistenceError`; it never falls back to
    memory, because a caller that asked for durability must not silently lose it.
    An unknown mode is an error rather than being guessed.
    """
    env = os.environ if environ is None else environ
    mode = (env.get(ENV_STORE_MODE) or env.get("PERSISTENCE_BACKEND") or "memory").strip().lower()
    if (env.get("UC02_ENV") or "").strip().lower() == "production" and mode != "supabase":
        raise PersistenceError(
            "production runtime requires UC02_STORE_MODE=supabase", mode=mode
        )
    if mode == "memory":
        return None
    if mode == "supabase":
        return _supabase_store(env, rest)
    if mode == "sqlite":
        from onboarding_task_management.sqlite_store import SqliteTableClient

        path = env.get(ENV_SQLITE_PATH) or str(DEFAULT_SQLITE_PATH)
        return OnboardingStore(SqliteTableClient(path))
    raise PersistenceError(
        f"unknown {ENV_STORE_MODE}; expected one of {STORE_MODES}", mode=mode
    )


def _supabase_store(env: Mapping[str, str], rest=None) -> OnboardingStore:
    """Supabase mode: connect, verify the live schema, and refuse to continue if it falls short.

    Raises instead of falling back to memory or SQLite when the database is unreachable,
    unconfigured or missing a table or column UC-02 needs.
    """
    from onboarding_task_management.supabase_rest import SupabaseRestClient
    from onboarding_task_management.supabase_schema import verify_schema

    from onboarding_task_management.supabase_store import SupabaseTableClient

    rest = rest if rest is not None else SupabaseRestClient.from_env(env)  # an injected client is for tests
    problems = verify_schema(rest.schema())
    if problems:
        raise PersistenceError(
            "the supabase schema does not hold what UC-02 must store",
            problems="; ".join(problems),
        )
    return OnboardingStore(SupabaseTableClient(rest))


__all__ = [
    "DEFAULT_SQLITE_PATH",
    "ENV_SQLITE_PATH",
    "ENV_STORE_MODE",
    "STORE_MODES",
    "store_from_env",
    "FORBIDDEN_DETAIL_KEYS",
    "PRIMARY_KEYS",
    "InMemoryTableClient",
    "OnboardingStore",
    "PersistenceError",
    "TableClient",
    "config_hash",
]
