"""The table access UC-03's repositories need, and its implementation over the SHARED Supabase
client.

UC-03 never builds its own HTTP client or reads a key: `SharedSupabaseTables` wraps
`hr_onboarding_common.platform.supabase_client.SupabaseClient` (PostgREST). Errors are reduced to a
status code and a
PostgreSQL error code, so no row data, URL or key can reach a log, a result or an exception message.
"""

from __future__ import annotations

import re
from typing import Any, Protocol


class StorageError(RuntimeError):
    """A database operation failed. `code` is a short reason code; the message never has row data
    or secrets.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}{': ' + detail if detail else ''}")
        self.code = code


class DuplicateKey(StorageError):
    """A primary-key or unique constraint rejected the insert (PostgreSQL 23505 / HTTP 409)."""


class TableClient(Protocol):
    def select(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: dict[str, Any] | None = None,
        order: str | None = None,
        desc: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def insert(self, table: str, row: dict[str, Any]) -> dict[str, Any]: ...

    def update(
        self, table: str, values: dict[str, Any], filters: dict[str, Any]
    ) -> list[dict[str, Any]]: ...


_HTTP = re.compile(r"HTTP (\d{3})")
_PG = re.compile(r'"code"\s*:\s*"([0-9A-Z]{5})"')


def _reduce(exc: Exception) -> StorageError:
    text = str(exc)
    http = _HTTP.search(text)
    pg = _PG.search(text)
    status, pgcode = (http.group(1) if http else "?"), (pg.group(1) if pg else "")
    if status == "409" or pgcode == "23505":
        return DuplicateKey("DUPLICATE_KEY", f"http {status} {pgcode}".strip())
    return StorageError("DATABASE_ERROR", f"http {status} {pgcode}".strip())


class SharedSupabaseTables:
    """`TableClient` over the shared Common Supabase client (one client for the whole process)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def select(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: dict[str, Any] | None = None,
        order: str | None = None,
        desc: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        try:
            q = self._client.table(table).select(columns)
            for col, val in (filters or {}).items():
                q = q.eq(col, val)
            if order:
                q = q.order(order, desc=desc)
            if limit:
                q = q.limit(limit)
            return list(q.execute())
        except Exception as exc:  # noqa: BLE001 - reduced to a code; never re-raised with the provider's text
            raise _reduce(exc) from None

    def insert(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        try:
            out = self._client.table(table).insert(row)
        except Exception as exc:  # noqa: BLE001
            raise _reduce(exc) from None
        if not out:
            raise StorageError("DATABASE_ERROR", "insert returned no row")
        return dict(out[0])

    def update(
        self, table: str, values: dict[str, Any], filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        try:
            return list(self._client.table(table).update(values, match=filters))
        except Exception as exc:  # noqa: BLE001
            raise _reduce(exc) from None
