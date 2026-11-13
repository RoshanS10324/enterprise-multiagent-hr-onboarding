"""Tests for BrainKnowledgeLoader.

Verifies loading, section extraction, status guidance, risk guidance, and search
queries from brain/onboarding/uc04_equipment_access.md.
"""
from __future__ import annotations

import pytest

from sub_agent.knowledge import BrainKnowledgeLoader


@pytest.fixture
def loader() -> BrainKnowledgeLoader:
    return BrainKnowledgeLoader()


def test_brain_knowledge_file_exists(loader: BrainKnowledgeLoader) -> None:
    doc_path = loader.brain_dir / "uc04_equipment_access.md"
    assert doc_path.exists()
    assert doc_path.name == "uc04_equipment_access.md"


def test_brain_knowledge_loads_sections(loader: BrainKnowledgeLoader) -> None:
    doc = loader.get_document("uc04_equipment_access")
    assert doc is not None
    assert len(doc.sections) >= 8

    # Verify key sections are present
    titles = [s.lower() for s in doc.sections.keys()]
    assert any("purpose" in t for t in titles)
    assert any("lifecycle" in t for t in titles)
    assert any("approval" in t for t in titles)
    assert any("evidence" in t for t in titles)
    assert any("guardrails" in t for t in titles)


def test_get_section_by_anchor(loader: BrainKnowledgeLoader) -> None:
    sec = loader.get_section("evidence requirements for readiness")
    assert sec is not None
    assert "evidence" in sec.lower()


def test_get_status_guidance(loader: BrainKnowledgeLoader) -> None:
    guidance_ready = loader.get_status_guidance("READY")
    assert "authoritative completion evidence" in guidance_ready.lower()

    guidance_blocked = loader.get_status_guidance("BLOCKED")
    assert "start gate" in guidance_blocked.lower()

    guidance_awaiting = loader.get_status_guidance("AWAITING_APPROVAL")
    is_awaiting = (
        "awaiting required" in guidance_awaiting.lower()
        or "approval" in guidance_awaiting.lower()
    )
    assert is_awaiting


def test_get_risk_guidance(loader: BrainKnowledgeLoader) -> None:
    guidance_delayed = loader.get_risk_guidance("EQUIPMENT_DELAYED_BEYOND_JOINING")
    assert "loan_device" in guidance_delayed.lower() or "deployment" in guidance_delayed.lower()

    guidance_clearance = loader.get_risk_guidance("CLIENT_SITE_CLEARANCE_LATE")
    matched = (
        "account_manager" in guidance_clearance.lower()
        or "clearance" in guidance_clearance.lower()
    )
    assert matched


def test_search_knowledge(loader: BrainKnowledgeLoader) -> None:
    results = loader.search("idempotency")
    assert len(results) > 0
    assert any("idempotency" in text.lower() for name, text in results)
