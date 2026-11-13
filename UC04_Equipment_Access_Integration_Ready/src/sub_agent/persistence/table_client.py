"""The only thing the database adapters need from a database client, as a Protocol.

UC-04 contains no database client and no HTTP code (a packaging safeguard forbids it). A concrete client is injected by the layer that embeds
UC-04, written against the approved integration approach. `InMemoryTableClient` is a faithful local stand-in: it enforces primary keys and,
when given a table schema, refuses an unknown column or a missing required column, which is how tests prove a payload fits the real tables.
"""

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sub_agent.persistence.errors import PersistenceError, SchemaMismatchError


class TableClient(Protocol):
    def select(self, table: str, *, filters: Mapping[str, Any], columns: Sequence[str] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Rows whose columns equal every filter value. Always filtered; there is no unfiltered read."""
        ...

    def insert(self, table: str, row: Mapping[str, Any]) -> None:
        """Add one row. Fails if the primary key exists."""
        ...

    def upsert(self, table: str, row: Mapping[str, Any], *, on_conflict: Sequence[str]) -> None:
        """Add one row, or replace the row with the same `on_conflict` columns."""
        ...

    def update(self, table: str, *, filters: Mapping[str, Any], values: Mapping[str, Any]) -> int:
        """Set `values` on the rows whose columns equal every filter value, and return how many rows changed. Always filtered. This is what
        makes a compare-and-swap possible: filter on the version you read, and zero rows changed means someone else wrote first."""
        ...


@dataclass(frozen=True)
class TableSchema:
    columns: frozenset[str]
    required: frozenset[str] = frozenset()  # NOT NULL columns without a default: they must be supplied
    primary_key: tuple[str, ...] = ()


@dataclass
class InMemoryTableClient:
    schemas: Mapping[str, TableSchema] = field(default_factory=dict)
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    fail_with: Exception | None = None  # fault injection for tests

    def _check(self, table: str, row: Mapping[str, Any], partial: bool = False) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        schema = self.schemas.get(table)
        if schema is None:
            return
        unknown = sorted(set(row) - schema.columns)
        if unknown:
            raise SchemaMismatchError(f"{table}: unknown columns {unknown}")
        missing = [] if partial else sorted(c for c in schema.required if row.get(c) is None)
        if missing:
            raise SchemaMismatchError(f"{table}: required columns missing {missing}")

    def select(self, table, *, filters, columns=None, limit=None):
        if self.fail_with is not None:
            raise self.fail_with
        if not filters:
            raise PersistenceError("an unfiltered read is not allowed")
        rows = [r for r in self.tables.get(table, []) if all(r.get(k) == v for k, v in filters.items())]
        rows = [{c: r.get(c) for c in columns} if columns else dict(r) for r in rows]
        return copy.deepcopy(rows[:limit] if limit else rows)

    def insert(self, table, row):
        self._check(table, row)
        schema = self.schemas.get(table)
        if schema and schema.primary_key:
            key = tuple(row.get(c) for c in schema.primary_key)
            if None not in key and any(tuple(r.get(c) for c in schema.primary_key) == key for r in self.tables.get(table, [])):
                raise PersistenceError(f"{table}: primary key already exists")
        self.tables.setdefault(table, []).append(copy.deepcopy(dict(row)))

    def update(self, table, *, filters, values):
        self._check(table, values, partial=True)
        if not filters:
            raise PersistenceError("an unfiltered update is not allowed")
        changed = 0
        for row in self.tables.get(table, []):
            if all(row.get(k) == v for k, v in filters.items()):
                row.update(copy.deepcopy(dict(values)))
                changed += 1
        return changed

    def upsert(self, table, row, *, on_conflict):
        self._check(table, row)
        rows = self.tables.setdefault(table, [])
        for i, existing in enumerate(rows):
            if all(existing.get(c) == row.get(c) for c in on_conflict):
                rows[i] = copy.deepcopy(dict(row))
                return
        rows.append(copy.deepcopy(dict(row)))
