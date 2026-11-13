"""Storage of UC-04's own case state: the aggregate `CaseRecord` (items with their exact lifecycle state, ticket ids, submission attempts,
adapter errors, the EC-35 archive, the last-evaluation fingerprint) and the idempotency / replay results.

This is the part of persistence whose table design is **not yet decided**: the shared tables cannot hold it (see UC04_MISSING_DATABASE_FIELDS),
and whether it goes into extensions of shared tables or into UC-04-owned tables needs the integration team's approval. So the repository is
written against this small interface, and only the backend behind it depends on that decision. `InMemoryStateBackend` is the reference
implementation and the test double; a table-backed one is added once the structure is approved.
"""

import copy
from dataclasses import dataclass
from typing import Protocol

from sub_agent.persistence.errors import PersistenceError, VersionConflict
from sub_agent.store import CaseRecord


@dataclass
class StoredCase:
    record: CaseRecord
    version: int


class StateBackend(Protocol):
    def load(self, case_id: str) -> StoredCase | None:
        """The stored aggregate and its version, or None. The caller owns the returned objects."""
        ...

    def save(self, record: CaseRecord, *, expected_version: int, new_version: int) -> None:
        """Write the whole aggregate atomically if, and only if, the stored version is `expected_version` (0 when none is stored yet).
        Raises `VersionConflict` otherwise; nothing is written then."""
        ...

    def case_ids(self) -> list[str]:
        """Diagnostic listing, not tenant-scoped: never expose it to a caller."""
        ...


class InMemoryStateBackend:
    def __init__(self) -> None:
        self._rows: dict[str, tuple[CaseRecord, int]] = {}
        self.fail_with: Exception | None = None  # fault injection for tests
        self.saves = 0

    def load(self, case_id):
        if self.fail_with is not None:
            raise self.fail_with
        row = self._rows.get(case_id)
        return StoredCase(copy.deepcopy(row[0]), row[1]) if row else None

    def save(self, record, *, expected_version, new_version):
        if self.fail_with is not None:
            raise self.fail_with
        stored = self._rows.get(record.case_id)
        if (stored[1] if stored else 0) != expected_version:
            raise VersionConflict(f"case moved since the unit of work began (expected version {expected_version})")
        if stored and stored[0].client_id != record.client_id:
            raise PersistenceError("a stored case cannot change client")
        self._rows[record.case_id] = (copy.deepcopy(record), new_version)
        self.saves += 1

    def case_ids(self):
        return sorted(self._rows)
