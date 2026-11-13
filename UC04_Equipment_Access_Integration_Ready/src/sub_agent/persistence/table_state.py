"""`TableStateBackend`: UC-04's own case state in UC-04-owned tables, through an injected `TableClient`.

Two tables (the proposed migration is `docs/proposed_migrations/uc04_state_tables.sql`; it is written for review and has NOT been applied):
* `uc04_case_state`: one row per case, with the version and the aggregate as JSON (items with their exact states, tickets, submission attempts,
  adapter errors, the archive and the fingerprint).
* `uc04_replay_results`: one row per (case, idempotency key), with the payload hash, the version and parent status it depended on, and the result.

Concurrency: the state row changes only through a compare-and-swap on the version read (an insert for a new case, a conditional update for an
existing one). If another writer got there first nothing is written and `VersionConflict` is raised. The two tables are written one after the
other (a REST client cannot span tables in one transaction): the state row first, then the replay results. A failure between the two loses only
a replay-cache entry, so a retry re-evaluates; it can never serve a result for state that was not saved. A database function can make the pair
atomic later. Every read is filtered by case id, and a stored case can never change client.
"""

from typing import Any

from sub_agent.persistence.errors import PersistenceError, VersionConflict
from sub_agent.persistence.serialization import record_from_json, record_to_json, result_from_row, result_to_row
from sub_agent.persistence.state_backend import StoredCase
from sub_agent.persistence.table_client import TableClient

STATE_TABLE = "uc04_case_state"
RESULTS_TABLE = "uc04_replay_results"
_STATE_COLUMNS = ["case_id", "client_id", "employee_ref", "version", "record"]
_RESULT_COLUMNS = ["idempotency_key", "payload_hash", "store_version", "parent_status", "result"]


class TableStateBackend:
    def __init__(self, client: TableClient):
        self._client = client

    def _state_row(self, case_id: str) -> dict[str, Any] | None:
        rows = self._client.select(STATE_TABLE, filters={"case_id": case_id}, columns=_STATE_COLUMNS, limit=2)
        if len(rows) > 1:
            raise PersistenceError("more than one state row for a case")
        return rows[0] if rows else None

    def load(self, case_id: str) -> StoredCase | None:
        row = self._state_row(case_id)
        if row is None:
            return None
        record = record_from_json(case_id, row["employee_ref"], row["client_id"], row["record"])
        for result_row in self._client.select(RESULTS_TABLE, filters={"case_id": case_id}, columns=_RESULT_COLUMNS):
            key, entry = result_from_row(result_row)
            record.results[key] = entry
        return StoredCase(record, int(row["version"]))

    def save(self, record, *, expected_version: int, new_version: int) -> None:
        stored = self._state_row(record.case_id)
        stored_version = int(stored["version"]) if stored else 0
        if stored_version != expected_version:
            raise VersionConflict(f"case moved since the unit of work began (expected version {expected_version})")
        if stored and stored["client_id"] != record.client_id:
            raise PersistenceError("a stored case cannot change client")
        values = {"version": new_version, "record": record_to_json(record), "employee_ref": record.employee_ref}
        if stored is None:
            try:
                self._client.insert(STATE_TABLE, {"case_id": record.case_id, "client_id": record.client_id, **values})
            except Exception:  # noqa: BLE001 - a concurrent first write hits the primary key
                if self._state_row(record.case_id) is not None:
                    raise VersionConflict("another writer created this case first") from None
                raise
        else:
            changed = self._client.update(STATE_TABLE, filters={"case_id": record.case_id, "version": expected_version}, values=values)
            if changed != 1:
                raise VersionConflict("another writer changed this case first")
        for key, entry in record.results.items():
            self._client.upsert(RESULTS_TABLE, result_to_row(record.case_id, key, entry), on_conflict=("case_id", "idempotency_key"))

    def case_ids(self) -> list[str]:
        raise PersistenceError("listing every case is not available on a table backend: reads are scoped to one case")
