"""SQLite :class:`~onboarding_task_management.persistence.TableClient` for LOCAL development.

What this is
    A real, file-backed database behind the existing ``TableClient`` port, so the
    UC-02 business logic (``OnboardingService`` / ``OnboardingStore``) is unchanged
    and the same logic can later run on PostgreSQL/Supabase by swapping the client.

What this is not
    Not the production database (Azure Cosmos DB) and not the shared Supabase
    development database. It is single-host, single-process-at-a-time storage for
    developers. It invents no fields: each row is stored exactly as the store
    produced it.

Layout
    One table per entry in ``persistence.PRIMARY_KEYS``. The primary-key columns
    and the few columns the store filters on (``client_id``, ``onboarding_id``,
    ``employee_id``, ``status``) are real, indexed columns; the whole row is kept
    as JSON in ``doc``. Constraints mirror the Supabase migration where SQLite can:
    primary keys, task -> onboarding and dependency -> task foreign keys with
    cascade, one live onboarding per employee (partial unique index), and an
    append-only audit table (triggers).

Safety
    * Every write runs in a transaction; ``transaction()`` makes a group of writes
      atomic and nests.
    * Any SQLite error becomes :class:`PersistenceError` with no file path or SQL in
      the message. Nothing falls back to memory.
    * Audit rows reject personal-data keys, like the SQL trigger and the in-memory
      client.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from onboarding_task_management.errors import DuplicateOnboardingError
from onboarding_task_management.persistence import (
    FORBIDDEN_DETAIL_KEYS,
    PRIMARY_KEYS,
    TABLE_APPROVAL_REQUEST,
    TABLE_AUDIT_EVENT,
    TABLE_AGENT_RUN,
    TABLE_CLIENT_CONFIG_REF,
    TABLE_ONBOARDING,
    TABLE_PLAN_REVISION,
    TABLE_TASK,
    TABLE_TASK_DEPENDENCY,
    PersistenceError,
    _json_scalar,
)

SCHEMA_VERSION = 1

#: Real (indexed) columns per table, besides the primary key and ``doc``.
_INDEX_COLUMNS: dict[str, tuple[str, ...]] = {
    TABLE_ONBOARDING: ("client_id", "employee_id", "status"),
    TABLE_TASK: ("onboarding_id", "client_id"),
    TABLE_TASK_DEPENDENCY: (),
    TABLE_PLAN_REVISION: ("client_id",),
    TABLE_APPROVAL_REQUEST: ("client_id", "onboarding_id"),
    TABLE_AGENT_RUN: ("client_id", "onboarding_id"),
    TABLE_AUDIT_EVENT: ("client_id", "onboarding_id"),
    TABLE_CLIENT_CONFIG_REF: (),
}
_INTEGER_COLUMNS = {"revision"}

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_CLIENT_CONFIG_REF} (
    client_id TEXT NOT NULL, config_hash TEXT NOT NULL, doc TEXT NOT NULL,
    PRIMARY KEY (client_id, config_hash)
);
CREATE TABLE IF NOT EXISTS {TABLE_ONBOARDING} (
    onboarding_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, employee_id TEXT NOT NULL,
    status TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS onboarding_one_live_per_employee
    ON {TABLE_ONBOARDING} (client_id, employee_id) WHERE status <> 'cancelled';
CREATE INDEX IF NOT EXISTS onboarding_client_idx ON {TABLE_ONBOARDING} (client_id);
CREATE TABLE IF NOT EXISTS {TABLE_TASK} (
    task_id TEXT PRIMARY KEY,
    onboarding_id TEXT NOT NULL REFERENCES {TABLE_ONBOARDING} (onboarding_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_onboarding_idx ON {TABLE_TASK} (onboarding_id);
CREATE TABLE IF NOT EXISTS {TABLE_TASK_DEPENDENCY} (
    task_id TEXT NOT NULL REFERENCES {TABLE_TASK} (task_id) ON DELETE CASCADE,
    depends_on_id TEXT NOT NULL, doc TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_id)
);
CREATE TABLE IF NOT EXISTS {TABLE_PLAN_REVISION} (
    onboarding_id TEXT NOT NULL REFERENCES {TABLE_ONBOARDING} (onboarding_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL, client_id TEXT NOT NULL, doc TEXT NOT NULL,
    PRIMARY KEY (onboarding_id, revision)
);
CREATE TABLE IF NOT EXISTS {TABLE_APPROVAL_REQUEST} (
    request_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, onboarding_id TEXT, doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {TABLE_AGENT_RUN} (
    run_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, onboarding_id TEXT, doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {TABLE_AUDIT_EVENT} (
    event_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, onboarding_id TEXT, doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_client_idx ON {TABLE_AUDIT_EVENT} (client_id);
CREATE TRIGGER IF NOT EXISTS audit_event_no_update BEFORE UPDATE ON {TABLE_AUDIT_EVENT}
BEGIN SELECT RAISE(ABORT, 'audit_event is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_event_no_delete BEFORE DELETE ON {TABLE_AUDIT_EVENT}
BEGIN SELECT RAISE(ABORT, 'audit_event is append-only'); END;
"""


def _fail(message: str, exc: BaseException | None = None, **context: object) -> PersistenceError:
    """A PersistenceError that never carries SQL text or file paths."""
    error = PersistenceError(message, **context)
    if exc is not None:
        error.__cause__ = exc
    return error


class SqliteTableClient:
    """File-backed ``TableClient``. One connection, serialised by a lock."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self._lock = threading.RLock()
        self._depth = 0
        self._closed = False
        target = str(path)
        try:
            if target != ":memory:":
                Path(target).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                target, isolation_level=None, check_same_thread=False, timeout=busy_timeout_ms / 1000
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            if target != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._init_schema()
        except PersistenceError:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise _fail("local database could not be opened", exc) from exc

    # -- lifecycle ------------------------------------------------------------

    def _init_schema(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise _fail("local database was written by a newer schema version", version=version)
        self._conn.executescript(_DDL)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "SqliteTableClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transactions ---------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes atomically. Re-entrant: only the outermost level commits."""
        with self._lock:
            self._check_open()
            outermost = self._depth == 0
            try:
                if outermost:
                    self._conn.execute("BEGIN IMMEDIATE")
                self._depth += 1
            except sqlite3.Error as exc:
                raise _fail("transaction could not be started", exc) from exc
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._rollback()
                raise
            self._depth -= 1
            if outermost:
                try:
                    self._conn.execute("COMMIT")
                except sqlite3.Error as exc:
                    self._rollback()
                    raise _fail("transaction could not be committed", exc) from exc

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - nothing left to roll back
            pass

    def _check_open(self) -> None:
        if self._closed:
            raise _fail("local database is closed")

    # -- TableClient ----------------------------------------------------------

    @staticmethod
    def _table(table: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if table not in PRIMARY_KEYS:
            raise PersistenceError("unknown table", table=table)
        return PRIMARY_KEYS[table], _INDEX_COLUMNS[table]

    def upsert(self, table: str, rows: Sequence[dict[str, Any]]) -> int:
        pk, extra = self._table(table)
        columns = [*pk, *[c for c in extra if c not in pk], "doc"]
        written = 0
        with self._lock, self.transaction():
            for row in rows:
                missing = [c for c in pk if c not in row]
                if missing:
                    raise PersistenceError(
                        "row is missing a primary-key column", table=table, column=missing[0]
                    )
                if table == TABLE_AUDIT_EVENT:
                    leaked = FORBIDDEN_DETAIL_KEYS & set(row.get("detail") or {})
                    if leaked:
                        raise PersistenceError(
                            "audit detail must not contain personal data keys",
                            keys=",".join(sorted(leaked)),
                        )
                values = [row.get(c) for c in columns[:-1]]
                values.append(json.dumps(row, default=_json_scalar, sort_keys=True))
                placeholders = ", ".join("?" for _ in columns)
                if table == TABLE_AUDIT_EVENT:
                    verb = "INSERT"  # plain insert: a repeated event id is refused (append-only)
                    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
                else:
                    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in pk)
                    sql = (
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
                        f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {updates}"
                    )
                try:
                    self._conn.execute(sql, values)
                except sqlite3.IntegrityError as exc:
                    raise self._integrity_error(table, row, exc) from exc
                except sqlite3.Error as exc:
                    raise _fail("write failed", exc, table=table) from exc
                written += 1
        return written

    @staticmethod
    def _integrity_error(table: str, row: dict[str, Any], exc: sqlite3.IntegrityError) -> Exception:
        text = str(exc)
        if table == TABLE_ONBOARDING and "UNIQUE" in text:
            return DuplicateOnboardingError(
                "a stored onboarding already exists for this employee",
                onboarding_id=str(row.get("onboarding_id")),
            )
        if table == TABLE_AUDIT_EVENT:
            return PersistenceError("audit_event is append-only", event_id=str(row.get("event_id")))
        if "FOREIGN KEY" in text:
            return _fail("row refers to a record that does not exist", exc, table=table)
        return _fail("write violated a constraint", exc, table=table)

    def select(
        self, table: str, filters: dict[str, Any], order_by: str | None = None
    ) -> list[dict[str, Any]]:
        pk, extra = self._table(table)
        real = {*pk, *extra}
        sql_filters = {c: v for c, v in filters.items() if c in real}
        residual = {c: v for c, v in filters.items() if c not in real}
        where = " AND ".join(f"{c} = ?" for c in sql_filters)
        sql = f"SELECT doc FROM {table}" + (f" WHERE {where}" if where else "")
        with self._lock:
            self._check_open()
            try:
                cursor = self._conn.execute(sql, list(sql_filters.values()))
                rows = [json.loads(r["doc"]) for r in cursor.fetchall()]
            except (sqlite3.Error, ValueError) as exc:
                raise _fail("read failed", exc, table=table) from exc
        rows = [r for r in rows if all(r.get(c) == v for c, v in residual.items())]
        if order_by:
            rows.sort(key=lambda r: (r.get(order_by) is None, r.get(order_by)))
        return rows

    def delete(self, table: str, filters: dict[str, Any]) -> int:
        self._table(table)
        if table == TABLE_AUDIT_EVENT:
            raise PersistenceError("audit_event is append-only; delete refused")
        victims = self.select(table, filters)
        pk = PRIMARY_KEYS[table]
        removed = 0
        with self._lock, self.transaction():
            for row in victims:
                where = " AND ".join(f"{c} = ?" for c in pk)
                try:
                    removed += self._conn.execute(
                        f"DELETE FROM {table} WHERE {where}", [row[c] for c in pk]
                    ).rowcount
                except sqlite3.Error as exc:
                    raise _fail("delete failed", exc, table=table) from exc
        return removed

    # -- helpers --------------------------------------------------------------

    def row_count(self, table: str) -> int:
        self._table(table)
        with self._lock:
            self._check_open()
            try:
                return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                raise _fail("read failed", exc, table=table) from exc


__all__ = ["SCHEMA_VERSION", "SqliteTableClient"]
