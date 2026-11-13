from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

from src.state.models import ApprovalRecord, AuditEvent, OnboardingCase


@runtime_checkable
class CaseStore(Protocol):
    async def get_case(self, case_id: str) -> OnboardingCase | None: ...
    async def save_case(self, case: OnboardingCase) -> None: ...
    async def get_approval(self, case_id: str, action_type: str) -> ApprovalRecord | None: ...
    async def save_approval(self, record: ApprovalRecord) -> None: ...


@runtime_checkable
class AuditStore(Protocol):
    async def log(self, event: AuditEvent) -> None: ...
    async def get_events(self, case_id: str) -> list[AuditEvent]: ...


@runtime_checkable
class ValidationResultStore(Protocol):
    """Durable storage for a validation RESULT (the decision), apart from advisory metadata.

    Returns "PERSISTED", or "NOT_PERSISTED:<reason>" when the storage model cannot hold this
    result (a documented precondition, e.g. the document was never registered). Raises on any
    storage error: a failed write must never look like a success. Saving the same request again
    must not create a second record.
    """

    async def save_result(
        self, request: Any, result: Any, advisory: dict[str, Any] | None
    ) -> str: ...


class InMemoryCaseStore:
    def __init__(self) -> None:
        self._cases: dict[str, OnboardingCase] = {}
        self._approvals: dict[str, dict[str, ApprovalRecord]] = {}
        self._lock = asyncio.Lock()

    async def get_case(self, case_id: str) -> OnboardingCase | None:
        async with self._lock:
            return self._cases.get(case_id)

    async def save_case(self, case: OnboardingCase) -> None:
        async with self._lock:
            self._cases[case.case_id] = case

    async def get_approval(self, case_id: str, action_type: str) -> ApprovalRecord | None:
        async with self._lock:
            return self._approvals.get(case_id, {}).get(action_type)

    async def save_approval(self, record: ApprovalRecord) -> None:
        async with self._lock:
            if record.case_id not in self._approvals:
                self._approvals[record.case_id] = {}
            self._approvals[record.case_id][record.action_type] = record


class InMemoryAuditStore:
    def __init__(self) -> None:
        self._events: dict[str, list[AuditEvent]] = {}
        self._lock = asyncio.Lock()

    async def log(self, event: AuditEvent) -> None:
        async with self._lock:
            if event.case_id not in self._events:
                self._events[event.case_id] = []
            self._events[event.case_id].append(event)

    async def get_events(self, case_id: str) -> list[AuditEvent]:
        async with self._lock:
            return list(self._events.get(case_id, []))


# Singletons for FastAPI dependency injection. In-memory by default (tests, offline development).
# An entry point that wants durable storage calls `configure_stores(...)` once at start-up (see
# src/main.py and integrations/supabase/), so the API never builds a database client per request.
_case_store: Any = None
_audit_store: Any = None
_result_store: Any = None


def configure_stores(case_store: Any, audit_store: Any, result_store: Any = None) -> None:
    """Install the process-wide stores (once, by the composition root). Tests use the default."""
    global _case_store, _audit_store, _result_store
    _case_store, _audit_store, _result_store = case_store, audit_store, result_store


def get_case_store() -> Any:
    global _case_store
    if _case_store is None:
        _case_store = InMemoryCaseStore()
    return _case_store


def get_audit_store() -> Any:
    global _audit_store
    if _audit_store is None:
        _audit_store = InMemoryAuditStore()
    return _audit_store


def get_result_store() -> Any:
    """The configured ValidationResultStore, or None (in memory, results are only returned)."""
    return _result_store
