"""UC-03 document collection, storage and version tracking (src/sub_agent/collection.py), and the
validator's revision-aware duplicate check. Synthetic data only."""

from __future__ import annotations

import pytest

from src.sub_agent.collection import (CollectionError, DocumentCollectionRequest, DocumentCollectionService,
                                      RequiredDocumentSpec, SqliteDocumentRegistry)
from src.sub_agent.validator import DocumentValidationService
from src.state.enums import ValidationFinding
from src.state.store import InMemoryAuditStore, InMemoryCaseStore
from src.sub_agent.checklist import ChecklistService
from src.sub_agent.extraction import MockDocumentExtractor
from tests.conftest import make_request

CASE, TENANT = "case-col-1", "client-a"


def _request(version: int = 1, types=("passport", "degree_certificate")) -> DocumentCollectionRequest:
    return DocumentCollectionRequest(case_id=CASE, tenant_id=TENANT, employee_id="emp-1", plan_id="ONB-1",
                                     plan_version=version,
                                     required_documents=[RequiredDocumentSpec(document_type=t) for t in types])


def _submit(service, doc_type="passport", ref="SYN-REF-1", **meta):
    return service.submit(case_id=CASE, tenant_id=TENANT, document_type=doc_type, document_ref=ref,
                          submitted_by="user:hr", metadata=meta or {"readable": True})


def test_open_collection_is_idempotent_per_plan_version_and_refuses_stale_versions():
    service = DocumentCollectionService()
    record, changed = service.open_collection(_request(1))
    assert changed and record.plan_version == 1
    assert service.open_collection(_request(1)) == (record, False)
    with pytest.raises(CollectionError) as conflict:
        service.open_collection(_request(1, types=("passport",)))
    assert conflict.value.code == "PLAN_VERSION_CONFLICT"
    record2, changed2 = service.open_collection(_request(2, types=("passport", "degree_certificate", "visa_documents")))
    assert changed2 and record2.superseded_versions == [1]
    with pytest.raises(CollectionError) as stale:
        service.open_collection(_request(1))
    assert stale.value.code == "STALE_PLAN_VERSION"


def test_submissions_are_stored_as_revisions_and_a_repeat_is_idempotent():
    service = DocumentCollectionService()
    service.open_collection(_request(1))
    first = _submit(service)
    again = _submit(service)
    corrected = _submit(service, ref="SYN-REF-2")
    assert (first.revision, first.duplicate) == (1, False)
    assert (again.revision, again.duplicate, again.storage_id) == (1, True, first.storage_id)
    assert (corrected.revision, corrected.duplicate) == (2, False)
    assert [d.revision for d in service.history(CASE, "passport")] == [1, 2]
    assert service.latest(CASE)["passport"].document_ref == "SYN-REF-2"


def test_a_document_the_plan_does_not_require_or_another_tenant_is_refused():
    service = DocumentCollectionService()
    with pytest.raises(CollectionError) as not_open:
        _submit(service)
    assert not_open.value.code == "COLLECTION_NOT_OPEN"
    service.open_collection(_request(1))
    with pytest.raises(CollectionError) as unknown:
        _submit(service, doc_type="payslips")
    assert unknown.value.code == "DOCUMENT_NOT_REQUIRED"
    with pytest.raises(CollectionError) as tenant:
        service.submit(case_id=CASE, tenant_id="client-b", document_type="passport", document_ref="X",
                       submitted_by="u")
    assert tenant.value.code == "TENANT_MISMATCH"


def test_requirement_gaps_follow_the_catalog_and_document_derived_flags():
    service = DocumentCollectionService()
    base = {"role": "engineer", "location": "Dubai"}
    planned = [g.document_type for g in service.catalog(base) if g.mandatory]
    assert service.requirement_gaps(base, planned) == []
    flags = service.derived_context([{"metadata": {"visa_required": True}}])
    gaps = service.requirement_gaps({**base, **flags}, planned)
    assert [g.document_type for g in gaps] == ["visa_documents"]
    assert gaps[0].mandatory and "visa_required" in gaps[0].reason


def test_sqlite_registry_survives_a_restart(tmp_path):
    path = tmp_path / "uc03_documents.db"
    service = DocumentCollectionService(SqliteDocumentRegistry(path))
    service.open_collection(_request(1))
    first = _submit(service)
    restarted = DocumentCollectionService(SqliteDocumentRegistry(path))
    assert restarted.collection(CASE).plan_version == 1
    again = _submit(restarted)
    assert (again.revision, again.duplicate, again.storage_id) == (1, True, first.storage_id)


async def test_a_newer_revision_is_validated_again_not_flagged_as_a_duplicate():
    svc = DocumentValidationService(case_store=InMemoryCaseStore(), audit_store=InMemoryAuditStore(),
                                    checklist_service=ChecklistService("default"), extractor=MockDocumentExtractor())
    first = await svc.validate(make_request(document_revision=1))
    same = await svc.validate(make_request(document_revision=1))
    corrected = await svc.validate(make_request(document_revision=2))
    assert ValidationFinding.DUPLICATE_REQUEST not in first.validation_findings
    assert ValidationFinding.DUPLICATE_REQUEST in same.validation_findings
    assert ValidationFinding.DUPLICATE_REQUEST not in corrected.validation_findings
