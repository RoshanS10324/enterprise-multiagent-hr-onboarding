"""UC-03 document collection, storage and version tracking.

UC-03 owns the document lifecycle of an onboarding case: it receives the document requirements of a
UC-02 plan version (`DocumentCollectionRequest`), keeps the list of required documents for that
version, stores every submitted document's reference and metadata as a numbered revision, recognises
a repeated submission of the same revision (idempotent), and tells the caller which mandatory
documents its own catalog (knowledge/checklists) requires that the plan does not contain yet
(`requirement_gaps`) -- the signal UC-02 replans on.

What is stored: references (an opaque `document_ref` to the secure document store) and the
extraction metadata validation needs. Never document contents.

Plan versions are monotonic per case. A collection for an older plan version than the one already
open is refused (`STALE_PLAN_VERSION`); re-opening the same version with the same requirements is a
no-op; a plan version is immutable (`PLAN_VERSION_CONFLICT` when the same version arrives with other
requirements).

Two registries ship: `InMemoryDocumentRegistry` (tests, the default) and `SqliteDocumentRegistry`
(survives a restart). Both are synchronous and thread-safe.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from src.sub_agent.checklist import ChecklistService

# Catalog rules that switch a document on from what the employee's own documents show (a passport that
# needs a visa, a sponsored family). A document such a rule makes applicable is mandatory for that
# employee, whatever its default `required` flag.
DOCUMENT_DERIVED_FLAGS: dict[str, str] = {"visa_required": "visa_required",
                                          "sponsorship_applicable": "sponsorship"}


class CollectionError(ValueError):
    """A collection operation refused with nothing changed. `code` is stable."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


class RequiredDocumentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: str
    label: str = ""
    mandatory: bool = True
    due_date: date | None = None
    validation_rules: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class DocumentCollectionRequest(BaseModel):
    """UC-02 -> UC-03: the documents one plan version requires."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    employee_id: str = Field(min_length=1)
    correlation_id: str = ""
    plan_id: str = Field(min_length=1)
    plan_version: int = Field(ge=1)
    joining_date: date | None = None
    required_documents: list[RequiredDocumentSpec]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def mandatory_documents(self) -> list[str]:
        return [d.document_type for d in self.required_documents if d.mandatory]

    @property
    def due_dates(self) -> dict[str, str | None]:
        return {d.document_type: d.due_date.isoformat() if d.due_date else None for d in self.required_documents}

    @property
    def validation_rules(self) -> dict[str, dict[str, Any]]:
        return {d.document_type: d.validation_rules for d in self.required_documents}


class CollectionRecord(BaseModel):
    """The collection UC-03 keeps open for a case: the current plan version's requirements."""

    case_id: str
    tenant_id: str
    employee_id: str
    plan_id: str
    plan_version: int
    required_documents: list[RequiredDocumentSpec]
    opened_at: datetime
    superseded_versions: list[int] = Field(default_factory=list)

    def required_types(self) -> set[str]:
        return {d.document_type for d in self.required_documents}


class StoredDocument(BaseModel):
    """One revision of one document type: a reference and its metadata, never the contents."""

    storage_id: str
    case_id: str
    tenant_id: str
    document_type: str
    revision: int = Field(ge=1)
    document_ref: str
    source: Literal["uploaded", "on_file"] = "uploaded"
    metadata: dict[str, Any] = Field(default_factory=dict)
    metadata_digest: str
    plan_version: int
    submitted_at: datetime
    submitted_by: str


class SubmissionReceipt(BaseModel):
    document_type: str
    revision: int
    storage_id: str
    plan_version: int
    duplicate: bool  # the same revision was already stored: nothing new was recorded


class RequirementGap(BaseModel):
    """A document UC-03's catalog requires for this employee that the plan does not contain."""

    document_type: str
    label: str
    mandatory: bool
    reason: str
    validation_rules: dict[str, Any] = Field(default_factory=dict)


def _digest(document_ref: str, source: str, metadata: dict[str, Any]) -> str:
    payload = json.dumps({"ref": document_ref, "source": source, "metadata": metadata}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------- registries


class DocumentRegistry(Protocol):
    def get_collection(self, case_id: str) -> CollectionRecord | None: ...
    def save_collection(self, record: CollectionRecord) -> None: ...
    def revisions(self, case_id: str, document_type: str) -> list[StoredDocument]: ...
    def add_revision(self, document: StoredDocument) -> None: ...
    def document_types(self, case_id: str) -> list[str]: ...


class InMemoryDocumentRegistry:
    def __init__(self) -> None:
        self._collections: dict[str, CollectionRecord] = {}
        self._documents: dict[tuple[str, str], list[StoredDocument]] = {}
        self._lock = threading.RLock()

    def get_collection(self, case_id: str) -> CollectionRecord | None:
        with self._lock:
            record = self._collections.get(case_id)
            return record.model_copy(deep=True) if record else None

    def save_collection(self, record: CollectionRecord) -> None:
        with self._lock:
            self._collections[record.case_id] = record.model_copy(deep=True)

    def revisions(self, case_id: str, document_type: str) -> list[StoredDocument]:
        with self._lock:
            return [d.model_copy(deep=True) for d in self._documents.get((case_id, document_type), [])]

    def add_revision(self, document: StoredDocument) -> None:
        with self._lock:
            self._documents.setdefault((document.case_id, document.document_type), []).append(
                document.model_copy(deep=True))

    def document_types(self, case_id: str) -> list[str]:
        with self._lock:
            return sorted(t for (c, t) in self._documents if c == case_id)


class SqliteDocumentRegistry:
    """The same registry in a SQLite file, so revisions and the open collection survive a restart."""

    def __init__(self, path: Path | str):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.execute("CREATE TABLE IF NOT EXISTS uc03_collections (case_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS uc03_documents (case_id TEXT NOT NULL, document_type TEXT NOT NULL, "
                               "revision INTEGER NOT NULL, body TEXT NOT NULL, "
                               "PRIMARY KEY (case_id, document_type, revision))")

    def get_collection(self, case_id: str) -> CollectionRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT body FROM uc03_collections WHERE case_id = ?", (case_id,)).fetchone()
        return CollectionRecord.model_validate_json(row[0]) if row else None

    def save_collection(self, record: CollectionRecord) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO uc03_collections (case_id, body) VALUES (?, ?)",
                               (record.case_id, record.model_dump_json()))

    def revisions(self, case_id: str, document_type: str) -> list[StoredDocument]:
        with self._lock:
            rows = self._conn.execute("SELECT body FROM uc03_documents WHERE case_id = ? AND document_type = ? "
                                      "ORDER BY revision", (case_id, document_type)).fetchall()
        return [StoredDocument.model_validate_json(r[0]) for r in rows]

    def add_revision(self, document: StoredDocument) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO uc03_documents (case_id, document_type, revision, body) VALUES (?, ?, ?, ?)",
                               (document.case_id, document.document_type, document.revision,
                                document.model_dump_json()))

    def document_types(self, case_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT document_type FROM uc03_documents WHERE case_id = ? "
                                      "ORDER BY document_type", (case_id,)).fetchall()
        return [r[0] for r in rows]


# ------------------------------------------------------------------------------- the service


class DocumentCollectionService:
    def __init__(self, registry: DocumentRegistry | None = None, checklist_service: ChecklistService | None = None):
        self.registry: DocumentRegistry = registry or InMemoryDocumentRegistry()
        self.checklist = checklist_service or ChecklistService()
        self._lock = threading.RLock()

    # -- catalog ------------------------------------------------------------------------------

    def catalog(self, context: dict[str, Any]) -> list[RequirementGap]:
        """Every document UC-03's catalog makes applicable for `context`, with its validation rules."""
        result = []
        for requirement in self.checklist.get_applicable_documents(context):
            flag_rule = next((r for r in requirement.applies_to if r.split(":", 1)[0] in DOCUMENT_DERIVED_FLAGS.values()
                              and context.get(r.split(":", 1)[0])), None)
            mandatory = requirement.required or flag_rule is not None
            reason = (f"required by the document checklist ({flag_rule})" if flag_rule
                      else "required by the document checklist" if requirement.required
                      else "optional in the document checklist")
            result.append(RequirementGap(document_type=requirement.id, label=requirement.label, mandatory=mandatory,
                                         reason=reason, validation_rules=dict(requirement.validations)))
        return result

    def known_document_types(self) -> list[str]:
        """Every document type UC-03's catalog can validate (whatever it applies to)."""
        return [d.id for d in self.checklist._checklist.documents]

    def validation_rules(self, document_type: str, context: dict[str, Any]) -> dict[str, Any]:
        requirement = self.checklist.get_requirement(document_type, context)
        return dict(requirement.validations) if requirement else {}

    # -- collection ---------------------------------------------------------------------------

    def open_collection(self, request: DocumentCollectionRequest, *, at: datetime | None = None
                        ) -> tuple[CollectionRecord, bool]:
        """Open (or move to) the collection of `request.plan_version`. Returns (record, changed)."""
        with self._lock:
            current = self.registry.get_collection(request.case_id)
            if current is not None:
                if current.tenant_id != request.tenant_id:
                    raise CollectionError("TENANT_MISMATCH", "the case belongs to another tenant")
                if request.plan_version < current.plan_version:
                    raise CollectionError("STALE_PLAN_VERSION",
                                          f"plan version {request.plan_version} is older than the open collection's "
                                          f"version {current.plan_version}")
                if request.plan_version == current.plan_version:
                    if current.required_documents == request.required_documents:
                        return current, False
                    raise CollectionError("PLAN_VERSION_CONFLICT",
                                          f"plan version {request.plan_version} was already opened with other "
                                          f"requirements; a changed plan needs a new version")
            record = CollectionRecord(
                case_id=request.case_id, tenant_id=request.tenant_id, employee_id=request.employee_id,
                plan_id=request.plan_id, plan_version=request.plan_version,
                required_documents=list(request.required_documents), opened_at=at or datetime.now(UTC),
                superseded_versions=[*(current.superseded_versions if current else []),
                                     *([current.plan_version] if current else [])])
            self.registry.save_collection(record)
            return record, True

    def collection(self, case_id: str) -> CollectionRecord | None:
        return self.registry.get_collection(case_id)

    # -- storage and versions -----------------------------------------------------------------

    def submit(self, *, case_id: str, tenant_id: str, document_type: str, document_ref: str, submitted_by: str,
               source: Literal["uploaded", "on_file"] = "uploaded", metadata: dict[str, Any] | None = None,
               at: datetime | None = None) -> SubmissionReceipt:
        """Store one submitted document as its next revision. Re-submitting what the latest revision
        already holds (same reference, source and metadata) records nothing and returns that revision
        (`duplicate=True`)."""
        metadata = dict(metadata or {})
        with self._lock:
            collection = self.registry.get_collection(case_id)
            if collection is None:
                raise CollectionError("COLLECTION_NOT_OPEN", "no document collection is open for this case")
            if collection.tenant_id != tenant_id:
                raise CollectionError("TENANT_MISMATCH", "the case belongs to another tenant")
            if document_type not in collection.required_types():
                raise CollectionError("DOCUMENT_NOT_REQUIRED",
                                      f"{document_type} is not required by plan version {collection.plan_version}")
            history = self.registry.revisions(case_id, document_type)
            digest = _digest(document_ref, source, metadata)
            if history and history[-1].metadata_digest == digest:
                latest = history[-1]
                return SubmissionReceipt(document_type=document_type, revision=latest.revision,
                                         storage_id=latest.storage_id, plan_version=latest.plan_version, duplicate=True)
            stored = StoredDocument(
                storage_id=f"UC03-DOC-{uuid.uuid4().hex[:16]}", case_id=case_id, tenant_id=tenant_id,
                document_type=document_type, revision=(history[-1].revision if history else 0) + 1,
                document_ref=document_ref, source=source, metadata=metadata, metadata_digest=digest,
                plan_version=collection.plan_version, submitted_at=at or datetime.now(UTC), submitted_by=submitted_by)
            self.registry.add_revision(stored)
            return SubmissionReceipt(document_type=document_type, revision=stored.revision,
                                     storage_id=stored.storage_id, plan_version=stored.plan_version, duplicate=False)

    def latest(self, case_id: str) -> dict[str, StoredDocument]:
        result = {}
        for document_type in self.registry.document_types(case_id):
            history = self.registry.revisions(case_id, document_type)
            if history:
                result[document_type] = history[-1]
        return result

    def history(self, case_id: str, document_type: str) -> list[StoredDocument]:
        return self.registry.revisions(case_id, document_type)

    # -- requirement changes --------------------------------------------------------------------

    @staticmethod
    def derived_context(documents: Iterable[StoredDocument | dict[str, Any]]) -> dict[str, bool]:
        """Catalog flags the employee's own documents establish (e.g. a passport whose holder needs a
        visa). Read from the extraction metadata of each document's current revision."""
        flags = {flag: False for flag in DOCUMENT_DERIVED_FLAGS.values()}
        for document in documents:
            meta = document.metadata if isinstance(document, StoredDocument) else (document.get("metadata") or {})
            for field, flag in DOCUMENT_DERIVED_FLAGS.items():
                if meta.get(field):
                    flags[flag] = True
        return flags

    def requirement_gaps(self, context: dict[str, Any], planned_types: Iterable[str]) -> list[RequirementGap]:
        """Mandatory documents UC-03's catalog requires for `context` that the plan does not list."""
        planned = set(planned_types)
        return [gap for gap in self.catalog(context) if gap.mandatory and gap.document_type not in planned]


__all__ = ["CollectionError", "CollectionRecord", "DOCUMENT_DERIVED_FLAGS", "DocumentCollectionRequest",
           "DocumentCollectionService", "DocumentRegistry", "InMemoryDocumentRegistry", "RequiredDocumentSpec",
           "RequirementGap", "SqliteDocumentRegistry", "StoredDocument", "SubmissionReceipt"]
