"""Supabase-backed implementations of UC-03's store protocols (`src/state/store.py`).

Tables (see consolidated_database_schema.sql and
supabase/migrations/20261104120000_uc03_persistence.sql):
  uc03_case_state       UC-03 case state                                   (read/write; UC-03-owned,
  proposed migration)
  approval_requests     UC-03 approvals, workflow_module = 'uc03'          (read/write; shared)
  audit_events          UC-03 audit events, reference = 'uc03:<case>'      (append only; shared,
  hash-chained by the DB)
  candidate_documents   registered documents (by the ingestion path)       (read/write; shared)
  document_validations  validation results + non-authoritative advisory   (append, idempotent by
  request_key; shared)
  onboarding_cases      READ ONLY: links a UC-03 case to a real onboarding case (owned by UC-01)

Every write either succeeds or raises `StorageError`. There are no multi-row transactions over
PostgREST, so each write
is single-row and idempotent: retries never duplicate a record.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from src.state.enums import ActorType, ApprovalStatus
from src.state.models import ApprovalRecord, AuditEvent, OnboardingCase

from .mapping import (
    ACTOR_TYPE,
    APPROVAL_OUTCOME,
    CANDIDATE_DOCUMENT_STATUS,
    COMPLIANCE,
    DOCUMENT_TYPES,
    HR_REVIEW,
    VISA_PACK,
    UnmappedValue,
    as_uuid,
    mapped,
    stable_uuid,
)
from .tables import DuplicateKey, StorageError, TableClient

CASE_TABLE = "uc03_case_state"
MODULE = "uc03"
# approval_requests.requested_by; never equal to a human approver (chk_no_self_approval)
REQUESTER = "uc03-agent"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _CaseLinks:
    """Shared, per-process cache of what a UC-03 case maps to in the shared tables (client id,
    onboarding case uuid).
    """

    def __init__(self, tables: TableClient) -> None:
        self._tables = tables
        self._cache: dict[str, tuple[str, str | None]] = {}

    def remember(self, case_ref: str, client_id: str, onboarding_case_id: str | None) -> None:
        self._cache[case_ref] = (client_id, onboarding_case_id)

    def onboarding_case_for(self, case_ref: str) -> str | None:
        """The real onboarding case behind `case_ref`, if there is one (read-only lookup in UC-01's
        table).
        """
        candidate = as_uuid(case_ref)
        if candidate is None:
            return None
        rows = self._tables.select(
            "onboarding_cases", columns="case_id", filters={"case_id": candidate}, limit=1
        )
        return candidate if rows else None

    def resolve(self, case_ref: str) -> tuple[str, str | None]:
        if case_ref not in self._cache:
            rows = self._tables.select(
                CASE_TABLE,
                columns="client_id,onboarding_case_id",
                filters={"case_ref": case_ref},
                limit=1,
            )
            if not rows:
                raise StorageError("CASE_STATE_NOT_FOUND", "the UC-03 case was never saved")
            self._cache[case_ref] = (rows[0]["client_id"], rows[0].get("onboarding_case_id"))
        return self._cache[case_ref]


class SupabaseCaseStore:
    """`CaseStore`: UC-03 case state in uc03_case_state, approvals in approval_requests."""

    def __init__(self, tables: TableClient, links: _CaseLinks) -> None:
        self._tables, self._links = tables, links

    async def get_case(self, case_id: str) -> OnboardingCase | None:
        rows = self._tables.select(CASE_TABLE, filters={"case_ref": case_id}, limit=1)
        if not rows:
            return None
        self._links.remember(case_id, rows[0]["client_id"], rows[0].get("onboarding_case_id"))
        return OnboardingCase.model_validate(rows[0]["state"])

    async def save_case(self, case: OnboardingCase) -> None:
        state = case.model_dump(mode="json")
        rows = self._tables.select(
            CASE_TABLE,
            columns="version,onboarding_case_id",
            filters={"case_ref": case.case_id},
            limit=1,
        )
        if not rows:
            link = self._links.onboarding_case_for(case.case_id)
            row = {
                "case_ref": case.case_id,
                "onboarding_case_id": link,
                "client_id": case.client_id,
                "employee_id": case.employee_id,
                "status": case.status.value,
                "state": state,
                "version": 1,
            }
            try:
                self._tables.insert(CASE_TABLE, row)
            except (
                DuplicateKey
            ):  # created concurrently: fall through to an update of the latest version
                await self.save_case(case)
                return
            self._links.remember(case.case_id, case.client_id, link)
            return
        version = rows[0]["version"]
        updated = self._tables.update(
            CASE_TABLE,
            {
                "state": state,
                "status": case.status.value,
                "version": version + 1,
                "updated_at": _now(),
            },
            {"case_ref": case.case_id, "version": version},
        )
        if not updated:
            raise StorageError(
                "CONCURRENT_MODIFICATION",
                "the case changed since it was read; nothing was overwritten",
            )
        self._links.remember(case.case_id, case.client_id, rows[0].get("onboarding_case_id"))

    async def get_approval(self, case_id: str, action_type: str) -> ApprovalRecord | None:
        rows = self._tables.select(
            "approval_requests",
            filters={"workflow_module": MODULE, "subject_ref": _subject(case_id, action_type)},
            order="created_at",
            desc=True,
            limit=1,
        )
        if not rows:
            return None
        p = rows[0]["payload"]
        return ApprovalRecord(
            approval_id=rows[0]["request_id"],
            case_id=case_id,
            action_type=action_type,
            actor_id=p["actor_id"],
            actor_type=ActorType(p["actor_type"]),
            status=ApprovalStatus(p["uc03_status"]),
            created_at=rows[0]["created_at"],
            approved_at=rows[0].get("decided_at"),
            notes=p.get("notes"),
        )

    async def save_approval(self, record: ApprovalRecord) -> None:
        client_id, onboarding_case = self._links.resolve(record.case_id)
        outcome = mapped(APPROVAL_OUTCOME, record.status, "approval status")
        decided = outcome != "PENDING"
        row = {
            "request_id": record.approval_id,
            "case_id": onboarding_case,
            "client_id": client_id,
            "gate": record.action_type,
            "approver_role": "hr",
            "requested_by": REQUESTER,
            "workflow_module": MODULE,
            "subject_ref": _subject(record.case_id, record.action_type),
            "payload": {
                "uc03_case_ref": record.case_id,
                "actor_id": record.actor_id,
                "actor_type": record.actor_type.value,
                "uc03_status": record.status.value,
                "notes": record.notes,
            },
            "outcome": outcome,
            "decided_by": record.actor_id if decided else None,
            "decided_at": (record.approved_at or record.created_at).isoformat()
            if decided
            else None,
            "created_at": record.created_at.isoformat(),
        }
        try:
            self._tables.insert("approval_requests", row)
        except DuplicateKey:  # the same approval saved again: bring the stored row up to date
            self._tables.update(
                "approval_requests",
                {k: row[k] for k in ("outcome", "decided_by", "decided_at", "payload")},
                {"request_id": record.approval_id},
            )


class SupabaseAuditStore:
    """`AuditStore`: append-only rows in the shared audit_events table (never updated or deleted).
    """

    def __init__(self, tables: TableClient, links: _CaseLinks) -> None:
        self._tables, self._links = tables, links

    async def log(self, event: AuditEvent) -> None:
        try:
            client_id, onboarding_case = self._links.resolve(event.case_id)
        except StorageError as exc:
            if exc.code != "CASE_STATE_NOT_FOUND":
                raise
            client_id, onboarding_case = (
                "unresolved",
                None,
            )  # e.g. an access check before the case exists; still audited
        row = {
            "event_id": stable_uuid(event.event_id),
            "case_id": onboarding_case,
            "client_id": client_id,
            "actor_type": mapped(ACTOR_TYPE, event.actor_type, "actor type"),
            "actor_id": event.actor_id,
            "actor_role": event.actor_type.value.lower(),
            "action": event.action,
            "outcome": "SUCCESS",
            "detail": {
                **event.detail,
                "uc03_event_id": event.event_id,
                "uc03_case_ref": event.case_id,
            },
            "correlation_id": event.correlation_id,
            "reference": _reference(event.case_id),
            "occurred_at": event.timestamp.isoformat(),
        }
        try:
            self._tables.insert("audit_events", row)
        except DuplicateKey:
            pass  # a retry of an already stored event; the audit trail is append-only

    async def get_events(self, case_id: str) -> list[AuditEvent]:
        rows = self._tables.select(
            "audit_events", filters={"reference": _reference(case_id)}, order="occurred_at"
        )
        out = []
        for r in rows:
            detail = dict(r["detail"])
            event_id = detail.pop("uc03_event_id", r["event_id"])
            detail.pop("uc03_case_ref", None)
            out.append(
                AuditEvent(
                    event_id=event_id,
                    case_id=case_id,
                    actor_id=r["actor_id"],
                    actor_type=ActorType(r["actor_role"].upper()),
                    action=r["action"],
                    detail=detail,
                    timestamp=r["occurred_at"],
                    correlation_id=r.get("correlation_id"),
                )
            )
        return out


class SupabaseDocumentRegistry:
    """Registers a received document file in candidate_documents (the ingestion path knows its size,
    type and checksum).
    """

    def __init__(self, tables: TableClient, links: _CaseLinks) -> None:
        self._tables, self._links = tables, links

    def register(
        self,
        *,
        case_ref: str,
        client_id: str,
        document_type: str,
        document_ref: str,
        file_name: str,
        file_size_bytes: int,
        mime_type: str,
        checksum_sha256: str,
    ) -> str:
        """The document_id for this file, creating the row once (unique: case, type, checksum)."""
        case_uuid = self._links.onboarding_case_for(case_ref)
        if case_uuid is None:
            raise StorageError(
                "CASE_NOT_AN_ONBOARDING_CASE",
                "documents can only be registered for a real onboarding case",
            )
        doc_type = mapped(DOCUMENT_TYPES, document_type, "document type")
        row = {
            "case_id": case_uuid,
            "client_id": client_id,
            "document_type": doc_type,
            "document_ref": document_ref,
            "file_name": file_name,
            "file_size_bytes": file_size_bytes,
            "mime_type": mime_type,
            "checksum_sha256": checksum_sha256,
        }
        try:
            return str(self._tables.insert("candidate_documents", row)["document_id"])
        except DuplicateKey:
            rows = self._tables.select(
                "candidate_documents",
                columns="document_id",
                filters={
                    "case_id": case_uuid,
                    "document_type": doc_type,
                    "checksum_sha256": checksum_sha256,
                },
                limit=1,
            )
            if not rows:
                raise
            return str(rows[0]["document_id"])


class SupabaseValidationResultStore:
    """`ValidationResultStore`: one document_validations row per distinct request (idempotent by
    request_key).

    The decision columns hold UC-03's deterministic result. Model output appears only in
    `draft_communication` (the
    HR-approval draft UC-03 already returns) and the separate `advisory` column, never in a decision
    column.
    """

    def __init__(self, tables: TableClient, links: _CaseLinks) -> None:
        self._tables, self._links = tables, links

    async def save_result(self, request: Any, result: Any, advisory: dict[str, Any] | None) -> str:
        key = request_key(request)
        if self._tables.select(
            "document_validations", columns="validation_id", filters={"request_key": key}, limit=1
        ):
            return "PERSISTED"  # the same request was stored before: nothing is duplicated
        case_uuid = self._links.onboarding_case_for(request.case_id)
        if case_uuid is None:
            return "NOT_PERSISTED:CASE_NOT_AN_ONBOARDING_CASE"
        try:
            doc_type = mapped(DOCUMENT_TYPES, request.document_type, "document type")
        except UnmappedValue:
            return "NOT_PERSISTED:DOCUMENT_TYPE_NOT_IN_SCHEMA"
        filters = {"case_id": case_uuid, "document_type": doc_type}
        if request.document_ref:
            filters["document_ref"] = request.document_ref
        docs = self._tables.select(
            "candidate_documents",
            columns="document_id",
            filters=filters,
            order="uploaded_at",
            desc=True,
            limit=1,
        )
        if not docs:
            return "NOT_PERSISTED:DOCUMENT_NOT_REGISTERED"
        row = {
            "document_id": docs[0]["document_id"],
            "case_id": case_uuid,
            "uc03_case_ref": request.case_id,
            "request_key": key,
            "extracted_name": request.extracted_name,
            "extracted_expiry_date": request.expiry_date.isoformat()
            if request.expiry_date
            else None,
            "is_readable": bool(request.readable) if request.readable is not None else True,
            "required_fields_present": bool(request.required_fields_present)
            if request.required_fields_present is not None
            else True,
            "extraction_confidence": round(float(request.extraction_confidence), 4),
            "extracted_fields": dict(result.extracted_fields or {}),
            "validation_findings": [f.value for f in result.validation_findings],
            "outstanding_requirements": list(result.outstanding_requirements),
            "compliance_item_status": mapped(
                COMPLIANCE, result.compliance_item_status, "compliance status"
            ),
            "visa_pack_status": mapped(VISA_PACK, result.visa_pack_status, "visa pack status"),
            "hr_review_status": mapped(HR_REVIEW, result.hr_decision, "HR review status"),
            "injection_detected": bool(result.injection_detected),
            "draft_communication": result.draft_communication,
            "document_status": result.document_status.value,
            "required_next_action": result.required_next_action.value,
            "approval_status": result.approval_status.value,
            "evidence_status": result.evidence_status.value,
            "uc03_compliance_status": result.compliance_item_status.value,
            "uc03_visa_pack_status": result.visa_pack_status.value,
            "decision_detail": result.detail,
            "audit_reference": result.audit_reference,
            "advisory": advisory,
            "validated_at": _now(),
        }
        try:
            self._tables.insert("document_validations", row)
        except DuplicateKey:
            return "PERSISTED"  # stored by a concurrent identical request
        status = CANDIDATE_DOCUMENT_STATUS.get(result.document_status.value, "VALIDATED")
        self._tables.update(
            "candidate_documents", {"status": status}, {"document_id": docs[0]["document_id"]}
        )
        return "PERSISTED"


def request_key(request: Any) -> str:
    """Idempotency key: SHA-256 of the canonical request. The same request always maps to the same
    stored result.
    """
    payload = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _subject(case_ref: str, action_type: str) -> str:
    return f"uc03:{case_ref}:{action_type}"


def _reference(case_ref: str) -> str:
    return f"uc03:{case_ref}"


def build_supabase_stores(
    tables: TableClient,
) -> tuple[
    SupabaseCaseStore, SupabaseAuditStore, SupabaseValidationResultStore, SupabaseDocumentRegistry
]:
    """All four stores over ONE table client and one shared case-link cache."""
    links = _CaseLinks(tables)
    return (
        SupabaseCaseStore(tables, links),
        SupabaseAuditStore(tables, links),
        SupabaseValidationResultStore(tables, links),
        SupabaseDocumentRegistry(tables, links),
    )
