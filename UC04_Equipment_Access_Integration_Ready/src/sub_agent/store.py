"""In-memory case store: dedupes requests by ID, remembers states for transition checks, and caches results by
idempotency key (BR-14, BR-21). Persistence (Cosmos DB in the Enterprise HR Agent proposal) is a future integration point."""

import copy
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sub_agent.contracts import Uc04Result
from sub_agent.states import ItemKind, ItemState


class IdempotencyConflict(RuntimeError):
    """The same idempotency key was reused with a different payload."""


@dataclass
class ItemRecord:
    request_id: str
    request_key: str
    item_kind: ItemKind
    item_id: str
    state: ItemState = ItemState.DRAFT_PREPARED
    ticket_id: str | None = None
    history: list[tuple[str | None, str]] = field(default_factory=list)
    client_site: bool = False  # EC-35: a client change cancels the old client-site requests
    submission_attempts: int = 0


@dataclass
class CaseRecord:
    case_id: str
    employee_ref: str
    client_id: str
    fingerprint: dict[str, Any] = field(default_factory=dict)
    items: dict[str, ItemRecord] = field(default_factory=dict)  # by request_key
    adapter_errors: dict[str, str] = field(default_factory=dict)  # request_id -> error class name only
    archived: dict[str, ItemRecord] = field(default_factory=dict)  # EC-35: client-site requests cancelled by a client change
    # idempotency_key -> (payload hash, store version, parent case status, result). A replay is served only while all three still match.
    results: dict[str, tuple[str, int, str, Uc04Result]] = field(default_factory=dict)


@runtime_checkable
class CaseRepository(Protocol):
    """What `Uc04Service` needs from case storage. `CaseStore` (in memory) is the reference implementation.

    The service reads and mutates the returned `CaseRecord` / `ItemRecord` objects in place, so a durable implementation must hand out
    aggregates it can write back. Contract:

    * `snapshot` / `restore` undo a failed evaluation (BR-18); a failure must leave stored state exactly as before the call.
    * `version` and `version_for(case_id)` change whenever the case's state changes; a cached result is replayed only while the version
      it was stored under is still current (BR-21, BR-22). In memory both are the same store-wide counter.
    * `begin(case_id)` opens a unit of work for one evaluation, `commit(case_id)` makes it durable, `rollback(case_id)` discards it.
      They are no-ops in memory. A durable implementation must make `commit` atomic and refuse a commit if the case moved meanwhile.
    * `submit_eligible` has no commit points yet: where a durable implementation must persist the intent to call the IT desk, and its
      outcome, is part of the database phase, not of this interface.
    * Tenant scoping is the caller's responsibility today: the service verifies the parent case's client before it touches a record.
    """

    version: int

    def get(self, case_id: str) -> CaseRecord | None: ...

    def ensure(self, case_id: str, employee_ref: str, client_id: str) -> CaseRecord: ...

    def touch(self) -> None: ...

    def snapshot(self, case_id: str) -> tuple[CaseRecord | None, int]: ...

    def restore(self, case_id: str, snapshot: tuple[CaseRecord | None, int]) -> None: ...

    def version_for(self, case_id: str) -> int: ...

    def begin(self, case_id: str) -> None: ...

    def commit(self, case_id: str) -> None: ...

    def rollback(self, case_id: str) -> None: ...

    @property
    def case_ids(self) -> list[str]: ...

    def request_ids(self, case_id: str) -> list[str]: ...

    def all_request_ids(self) -> list[str]: ...


class CaseStore:
    def __init__(self):
        self._cases: dict[str, CaseRecord] = {}
        self.version = 0  # bumped on any state change, so a cached result is never replayed over newer state

    def get(self, case_id: str) -> CaseRecord | None:
        return self._cases.get(case_id)

    def ensure(self, case_id: str, employee_ref: str, client_id: str) -> CaseRecord:
        if case_id not in self._cases:
            self._cases[case_id] = CaseRecord(case_id, employee_ref, client_id)
            self.version += 1
        return self._cases[case_id]

    def touch(self) -> None:
        self.version += 1

    def snapshot(self, case_id: str) -> tuple[CaseRecord | None, int]:
        """A copy of one case's state and the store version, taken before an evaluation, so a failure can be undone (BR-18).
        The idempotency cache is shared, not copied: a failed evaluation never adds to it."""
        record = self._cases.get(case_id)
        if record is None:
            return None, self.version
        return CaseRecord(record.case_id, record.employee_ref, record.client_id, dict(record.fingerprint), copy.deepcopy(record.items),
                          dict(record.adapter_errors), copy.deepcopy(record.archived), record.results), self.version

    def restore(self, case_id: str, snapshot: tuple[CaseRecord | None, int]) -> None:
        record, version = snapshot
        if record is None:
            self._cases.pop(case_id, None)
        else:
            self._cases[case_id] = record
        self.version = version

    def version_for(self, case_id: str) -> int:
        """In memory the version is one store-wide counter, so this is the same value as `version` (behaviour unchanged)."""
        return self.version

    def begin(self, case_id: str) -> None:
        """No-op in memory: `snapshot` / `restore` already give the unit-of-work semantics."""

    def commit(self, case_id: str) -> None:
        """No-op in memory: state is already in the store."""

    def rollback(self, case_id: str) -> None:
        """No-op in memory: `restore` has already undone the change."""

    @property
    def case_ids(self) -> list[str]:
        return sorted(self._cases)

    def request_ids(self, case_id: str) -> list[str]:
        record = self._cases.get(case_id)
        return sorted(i.request_id for i in record.items.values()) if record else []

    def all_request_ids(self) -> list[str]:
        return sorted(i.request_id for c in self._cases.values() for i in c.items.values())
