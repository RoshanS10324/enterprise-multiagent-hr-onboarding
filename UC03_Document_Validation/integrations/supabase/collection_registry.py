"""`DocumentRegistry` (src/sub_agent/collection.py) in Supabase: UC-03's open document collection and every stored
document revision, in UC-03's own `uc03_case_state` table.

One row per case, keyed `case_ref = "collection:<case id>"`, next to the case-state row `SupabaseCaseStore` keeps
under the plain case id. Its `state` holds the open `CollectionRecord` (the plan version's requirements) and, per
document type, the ordered list of `StoredDocument` revisions: references and extraction metadata, never document
contents (what the SQLite and in-memory registries hold, unchanged).

Concurrency: every change is a compare-and-swap on `version` (insert for the first write; an update filtered on the
version read otherwise). Losing the race raises `StorageError("CONCURRENT_MODIFICATION")`; nothing is overwritten.

Tenancy: the row carries `client_id` (the collection's tenant). A revision for another tenant is refused, and the
collection's tenant can never change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.sub_agent.collection import CollectionRecord, StoredDocument

from .mapping import as_uuid
from .tables import DuplicateKey, StorageError, TableClient

CASE_TABLE = "uc03_case_state"
PREFIX = "collection:"
STATUS = "DOCUMENT_COLLECTION"


def collection_ref(case_id: str) -> str:
    return f"{PREFIX}{case_id}"


class SupabaseCollectionRegistry:
    def __init__(self, tables: TableClient) -> None:
        self._tables = tables

    # -- reads ------------------------------------------------------------------------------------------------------
    def _row(self, case_id: str) -> dict[str, Any] | None:
        rows = self._tables.select(CASE_TABLE, filters={"case_ref": collection_ref(case_id)}, limit=1)
        return rows[0] if rows else None

    def get_collection(self, case_id: str) -> CollectionRecord | None:
        row = self._row(case_id)
        body = ((row or {}).get("state") or {}).get("collection")
        return CollectionRecord.model_validate(body) if body else None

    def revisions(self, case_id: str, document_type: str) -> list[StoredDocument]:
        row = self._row(case_id)
        docs = (((row or {}).get("state") or {}).get("documents") or {}).get(document_type) or []
        return sorted((StoredDocument.model_validate(d) for d in docs), key=lambda d: d.revision)

    def document_types(self, case_id: str) -> list[str]:
        row = self._row(case_id)
        return sorted(t for t, docs in (((row or {}).get("state") or {}).get("documents") or {}).items() if docs)

    # -- writes -----------------------------------------------------------------------------------------------------
    def save_collection(self, record: CollectionRecord) -> None:
        row = self._row(record.case_id)
        if row is None:
            self._insert(record.case_id, client_id=record.tenant_id, employee_id=record.employee_id,
                         state={"collection": record.model_dump(mode="json"), "documents": {}})
            return
        if row["client_id"] != record.tenant_id:
            raise StorageError("TENANT_MISMATCH", "a collection cannot move to another tenant")
        state = dict(row.get("state") or {})
        state["collection"] = record.model_dump(mode="json")
        self._update(row, state)

    def add_revision(self, document: StoredDocument) -> None:
        row = self._row(document.case_id)
        if row is None:
            raise StorageError("COLLECTION_NOT_OPEN", "no document collection is stored for this case")
        if row["client_id"] != document.tenant_id:
            raise StorageError("TENANT_MISMATCH", "the document belongs to another tenant than the collection")
        state = dict(row.get("state") or {})
        documents = {t: list(v) for t, v in (state.get("documents") or {}).items()}
        history = documents.setdefault(document.document_type, [])
        if any(int(d.get("revision", 0)) == document.revision for d in history):
            raise DuplicateKey("DUPLICATE_KEY", "this revision is already stored")
        history.append(document.model_dump(mode="json"))
        state["documents"] = documents
        self._update(row, state)

    def _insert(self, case_id: str, *, client_id: str, employee_id: str, state: dict[str, Any]) -> None:
        link = self._onboarding_case(case_id)
        try:
            self._tables.insert(CASE_TABLE, {"case_ref": collection_ref(case_id), "onboarding_case_id": link,
                                             "client_id": client_id, "employee_id": employee_id, "status": STATUS,
                                             "state": state, "version": 1})
        except DuplicateKey:
            raise StorageError("CONCURRENT_MODIFICATION", "the collection was created concurrently") from None

    def _update(self, row: dict[str, Any], state: dict[str, Any]) -> None:
        version = int(row["version"])
        changed = self._tables.update(CASE_TABLE, {"state": state, "version": version + 1,
                                                   "updated_at": datetime.now(UTC).isoformat()},
                                      {"case_ref": row["case_ref"], "version": version})
        if not changed:
            raise StorageError("CONCURRENT_MODIFICATION", "the collection changed since it was read; nothing was "
                                                          "overwritten")

    def _onboarding_case(self, case_id: str) -> str | None:
        candidate = as_uuid(case_id)
        if candidate is None:
            return None
        rows = self._tables.select("onboarding_cases", columns="case_id", filters={"case_id": candidate}, limit=1)
        return candidate if rows else None
