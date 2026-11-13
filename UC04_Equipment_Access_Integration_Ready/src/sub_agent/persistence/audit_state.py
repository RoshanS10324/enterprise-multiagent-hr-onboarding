"""`AuditEventStateBackend`: UC-04's own case state as versioned, append-only snapshots in the shared `audit_events` table.

Why this backend. UC-04's state (items with their exact lifecycle states, tickets, submission attempts, adapter errors, the archive, the
fingerprint, and the replay results) fits none of the shared provisioning tables (their CHECK lists hold neither UC-04's item kinds nor 14
of its 16 item states), and the UC-04-owned tables of `docs/proposed_migrations/uc04_state_tables.sql` are not applied. The shared
`audit_events` table is already where the platform's other agents keep versioned state snapshots (UC-02's case state, UC-05's operation
ledger), so UC-04's state lives there too, without a new table:

* one row per saved version: `action = 'UC04_CASE_STATE'`, `reference = 'uc04-state:<case id>'`, `detail = {version, employee_ref}`,
  `after_state = {record, results}` (the same JSON the table backend stores; codes, ids, dates and counts only);
* the row's `event_id` is derived from the case id, the version and the snapshot's sequence within that version (a save that changes only
  the replay results keeps the version, as the repository's contract says, and appends the next sequence). Two writers saving the same
  (version, sequence) insert the same primary key: the second is refused, so every save is a compare-and-swap (`VersionConflict`), and
  nothing is overwritten -- rows are never updated;
* the current state is the snapshot with the highest (version, sequence); older snapshots remain as the history.

Every read is filtered by case id, a stored case can never change client, and the case id must be a real onboarding case (the table's
`case_id` is a foreign key to `onboarding_cases`). Like every adapter here it holds no client: a `TableClient` is injected.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sub_agent.persistence.errors import PersistenceError, VersionConflict
from sub_agent.persistence.serialization import record_from_json, record_to_json, result_from_row, result_to_row
from sub_agent.persistence.state_backend import StoredCase
from sub_agent.persistence.table_client import TableClient

AUDIT_TABLE = "audit_events"
ACTION = "UC04_CASE_STATE"
ACTOR_ID = "uc04-agent"
_NAMESPACE = uuid.UUID("6f3c2b1e-4d5a-5b6c-8d7e-9f0a1b2c3d4e")


def snapshot_event_id(case_id: str, version: int, seq: int = 0) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"uc04-case-state:{case_id}:{int(version)}:{int(seq)}"))


def _order(row: dict[str, Any]) -> tuple[int, int]:
    detail = row.get("detail") or {}
    return int(detail.get("version", 0)), int(detail.get("seq", 0))


def state_reference(case_id: str) -> str:
    return f"uc04-state:{case_id}"


class AuditEventStateBackend:
    def __init__(self, client: TableClient, *, clock=None):
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    def _filters(self, case_id: str) -> dict[str, Any]:
        return {"case_id": case_id, "action": ACTION, "reference": state_reference(case_id)}

    def _latest(self, case_id: str) -> dict[str, Any] | None:
        try:
            rows = self._client.select(AUDIT_TABLE, filters=self._filters(case_id), columns=["event_id", "client_id", "detail"])
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - a client failure is a persistence failure, without the client's text
            raise PersistenceError(f"state read failed ({type(exc).__name__})") from None
        if not rows:
            return None
        return max(rows, key=_order)

    def load(self, case_id: str) -> StoredCase | None:
        latest = self._latest(case_id)
        if latest is None:
            return None
        rows = self._client.select(AUDIT_TABLE, filters={"event_id": latest["event_id"]}, columns=["after_state"], limit=1)
        if not rows or not rows[0].get("after_state"):
            raise PersistenceError("a state snapshot without its state")
        detail, body = latest.get("detail") or {}, rows[0]["after_state"]
        record = record_from_json(case_id, detail["employee_ref"], latest["client_id"], body.get("record") or {})
        for row in body.get("results") or []:
            key, entry = result_from_row(row)
            record.results[key] = entry
        return StoredCase(record, int(detail["version"]))

    def save(self, record, *, expected_version: int, new_version: int) -> None:
        latest = self._latest(record.case_id)
        stored_version, stored_seq = _order(latest) if latest else (0, -1)
        if stored_version != expected_version:
            raise VersionConflict(f"case moved since the unit of work began (expected version {expected_version})")
        if latest and latest["client_id"] != record.client_id:
            raise PersistenceError("a stored case cannot change client")
        if new_version not in (expected_version, expected_version + 1):
            raise PersistenceError("a save moves the version by at most one")
        seq = stored_seq + 1 if new_version == expected_version else 0
        event_id = snapshot_event_id(record.case_id, new_version, seq)
        row = {
            "event_id": event_id, "case_id": record.case_id, "client_id": record.client_id, "actor_type": "AGENT", "actor_id": ACTOR_ID,
            "action": ACTION, "outcome": "SUCCESS", "reference": state_reference(record.case_id),
            "detail": {"version": new_version, "seq": seq, "employee_ref": record.employee_ref, "schema": 1},
            "after_state": {"record": record_to_json(record),
                            "results": [result_to_row(record.case_id, key, entry) for key, entry in record.results.items()]},
            "occurred_at": self._clock().isoformat(),
        }
        try:
            self._client.insert(AUDIT_TABLE, row)
        except Exception as exc:  # noqa: BLE001
            try:
                taken = bool(self._client.select(AUDIT_TABLE, filters={"event_id": event_id}, columns=["event_id"], limit=1))
            except Exception:  # noqa: BLE001
                taken = False
            if taken:
                raise VersionConflict("another writer saved this version first") from None
            raise PersistenceError(f"state write failed ({type(exc).__name__})") from None

    def case_ids(self) -> list[str]:
        raise PersistenceError("listing every case is not available on a table backend: reads are scoped to one case")
