"""`DatabaseCaseRepository`: the `CaseRepository` interface over a durable `StateBackend`.

It gives the service exactly the semantics `CaseStore` gives in memory:

* `begin` loads the case once; the service then reads and mutates that one aggregate; `commit` writes it back atomically with a
  compare-and-swap on the version, `rollback` (or `restore`) discards the changes. Nothing is written before `commit`, so a failed
  evaluation leaves stored state exactly as it was (BR-18).
* The version is **per case** and moves only when the case's own state changes (items, ticket ids, attempts, adapter errors, archive,
  fingerprint) or the service calls `touch`. Adding or changing a stored replay result does not move it, so a stored result stays valid
  until the state it depended on changes, exactly like the in-memory store (BR-21, BR-22).
* A read outside a unit of work is a fresh read of the stored state; it is never cached.
* Another client's case is never handed out: `ensure` refuses a client mismatch.

Every failure of the backend becomes a `PersistenceError` (names only). The caller must fail closed.
"""

import copy
from dataclasses import dataclass
from typing import Any

from sub_agent.persistence.errors import PersistenceError, TenantScopeError
from sub_agent.persistence.state_backend import StateBackend
from sub_agent.store import CaseRecord


@dataclass
class _Unit:
    record: CaseRecord | None
    baseline: CaseRecord | None  # a copy taken at `begin`, to tell what changed
    version: int
    touched: bool = False


def _state(record: CaseRecord | None) -> Any:
    """Everything that makes up the case's state except the replay results."""
    if record is None:
        return None
    return (record.employee_ref, record.client_id, record.fingerprint, record.items, record.adapter_errors, record.archived)


class DatabaseCaseRepository:
    def __init__(self, backend: StateBackend):
        self._backend = backend
        self._units: dict[str, _Unit] = {}
        self.version = 0  # part of the interface; the service reads `version_for`, which is per case here

    # ------------------------------------------------------------------ backend access: one place that turns any failure into PersistenceError
    def _load(self, case_id: str):
        try:
            return self._backend.load(case_id)
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - whatever the client raised, the caller must fail closed
            raise PersistenceError(f"storage read failed ({type(exc).__name__})") from None

    # ------------------------------------------------------------------ reads
    def get(self, case_id: str) -> CaseRecord | None:
        unit = self._units.get(case_id)
        if unit is not None:
            return unit.record  # inside a unit of work: the aggregate that will be written back
        stored = self._load(case_id)
        return stored.record if stored else None

    def version_for(self, case_id: str) -> int:
        unit = self._units.get(case_id)
        if unit is not None:
            return unit.version + (1 if self._state_changed(unit) else 0)  # the version the case will have once committed
        stored = self._load(case_id)
        return stored.version if stored else 0

    # ------------------------------------------------------------------ unit of work
    def begin(self, case_id: str) -> None:
        stored = self._load(case_id)
        self._units[case_id] = _Unit(record=stored.record if stored else None, baseline=copy.deepcopy(stored.record) if stored else None,
                                     version=stored.version if stored else 0)

    def ensure(self, case_id: str, employee_ref: str, client_id: str) -> CaseRecord:
        unit = self._units.get(case_id)
        if unit is None:
            raise PersistenceError("ensure outside a unit of work")
        if unit.record is None:
            unit.record = CaseRecord(case_id, employee_ref, client_id)
        elif unit.record.client_id != client_id:
            raise TenantScopeError("stored case belongs to another client")
        return unit.record

    def touch(self) -> None:
        for unit in self._units.values():
            unit.touched = True

    def snapshot(self, case_id: str) -> tuple[CaseRecord | None, int]:
        unit = self._units.get(case_id)
        if unit is not None:
            return (copy.deepcopy(unit.record) if unit.record else None), self.version_for(case_id)
        stored = self._load(case_id)
        return (stored.record if stored else None), (stored.version if stored else 0)

    def restore(self, case_id: str, snapshot: tuple[CaseRecord | None, int]) -> None:
        unit = self._units.get(case_id)
        if unit is not None:
            record, _ = snapshot
            unit.record = copy.deepcopy(record) if record else None
            unit.touched = False

    def rollback(self, case_id: str) -> None:
        self._units.pop(case_id, None)

    def commit(self, case_id: str) -> None:
        unit = self._units.pop(case_id, None)
        if unit is None or unit.record is None:
            return
        state_changed = self._state_changed(unit)
        results_changed = unit.record.results != (unit.baseline.results if unit.baseline else {})
        if not (state_changed or results_changed):
            return
        try:
            self._backend.save(unit.record, expected_version=unit.version, new_version=unit.version + (1 if state_changed else 0))
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(f"storage write failed ({type(exc).__name__})") from None

    @staticmethod
    def _state_changed(unit: _Unit) -> bool:
        return unit.touched or _state(unit.record) != _state(unit.baseline)

    # ------------------------------------------------------------------ diagnostics (not tenant-scoped; never expose)
    @property
    def case_ids(self) -> list[str]:
        return self._backend.case_ids()

    def request_ids(self, case_id: str) -> list[str]:
        record = self.get(case_id)
        return sorted(i.request_id for i in record.items.values()) if record else []

    def all_request_ids(self) -> list[str]:
        return sorted(rid for cid in self.case_ids for rid in self.request_ids(cid))
