from __future__ import annotations

import pytest

from knowledge.checklists.loader import load_checklist, resolve_applicable_documents
from src.sub_agent.checklist import ChecklistService


def test_default_checklist_loads():
    checklist = load_checklist("default")
    assert checklist.name == "default"
    assert len(checklist.documents) > 0


def test_unknown_checklist_raises():
    with pytest.raises(FileNotFoundError):
        load_checklist("nonexistent_client_xyz")


def test_all_documents_have_ids():
    checklist = load_checklist("default")
    for doc in checklist.documents:
        assert doc.id, "Every document must have a non-empty ID"


def test_passport_in_default():
    checklist = load_checklist("default")
    ids = [d.id for d in checklist.documents]
    assert "passport" in ids


def test_applies_to_all_includes_passport():
    checklist = load_checklist("default")
    applicable = resolve_applicable_documents(checklist, context={})
    ids = [d.id for d in applicable]
    assert "passport" in ids
    assert "degree_certificate" in ids


def test_emirates_id_only_for_uae():
    checklist = load_checklist("default")

    uae_docs = resolve_applicable_documents(checklist, context={"location": "UAE"})
    uae_ids = [d.id for d in uae_docs]
    assert "emirates_id" in uae_ids

    non_uae_docs = resolve_applicable_documents(checklist, context={"location": "UK"})
    non_uae_ids = [d.id for d in non_uae_docs]
    assert "emirates_id" not in non_uae_ids


def test_family_docs_only_with_sponsorship():
    # Scenario 13: no sponsorship → family_documents not applicable
    checklist = load_checklist("default")

    with_sponsorship = resolve_applicable_documents(
        checklist, context={"sponsorship": True}
    )
    assert any(d.id == "family_documents" for d in with_sponsorship)

    without_sponsorship = resolve_applicable_documents(
        checklist, context={"sponsorship": False}
    )
    assert not any(d.id == "family_documents" for d in without_sponsorship)


def test_visa_docs_only_when_required():
    checklist = load_checklist("default")

    with_visa = resolve_applicable_documents(
        checklist, context={"visa_required": True}
    )
    assert any(d.id == "visa_documents" for d in with_visa)

    without_visa = resolve_applicable_documents(
        checklist, context={"visa_required": False}
    )
    assert not any(d.id == "visa_documents" for d in without_visa)


def test_checklist_service_get_requirement():
    svc = ChecklistService("default")
    req = svc.get_requirement("passport", context={"location": "UAE"})
    assert req is not None
    assert req.id == "passport"


def test_checklist_service_get_requirement_not_in_checklist():
    # Scenario 24
    svc = ChecklistService("default")
    req = svc.get_requirement("unknown_document_type", context={})
    assert req is None


def test_checklist_service_get_outstanding():
    svc = ChecklistService("default")
    outstanding = svc.get_outstanding(context={}, already_complete=set())
    assert len(outstanding) > 0
    assert "passport" in outstanding


def test_checklist_service_outstanding_excludes_complete():
    svc = ChecklistService("default")
    outstanding = svc.get_outstanding(
        context={}, already_complete={"passport", "degree_certificate"}
    )
    assert "passport" not in outstanding
    assert "degree_certificate" not in outstanding
