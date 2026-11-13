"""Tests for UC-04 Skills Registry and Skill Definitions.

Verifies discovering, validating, and matching skills from context_layer/skills/.
"""
from __future__ import annotations

import pytest

from sub_agent.skills import SkillRegistry


@pytest.fixture
def registry() -> SkillRegistry:
    return SkillRegistry()


def test_all_five_skills_loaded(registry: SkillRegistry) -> None:
    skills = registry.list_skills()
    assert len(skills) >= 5

    names = {s.name for s in skills}
    expected = {
        "assess_case_and_requests",
        "retrieve_access_equipment_requirements",
        "explain_approvals_and_exceptions",
        "summarize_evidence_and_it_status",
        "assess_readiness_risks_and_actions",
    }
    assert expected.issubset(names)


def test_skills_have_required_metadata(registry: SkillRegistry) -> None:
    for skill in registry.list_skills():
        assert skill.use_case == "UC-04"
        assert skill.version == "1.0.0"
        assert skill.type == "advisory_skill"
        assert len(skill.description) > 0
        assert skill.constraints.get("write_access") is False
        assert skill.constraints.get("execution") == "read_only"
        assert len(skill.knowledge_dependencies) > 0
        assert len(skill.failure_behavior) > 0


def test_match_skills_for_status(registry: SkillRegistry) -> None:
    # AWAITING_APPROVAL should match explain_approvals_and_exceptions
    matched_app = registry.match_skills_for_status("AWAITING_APPROVAL")
    names_app = [s.name for s in matched_app]
    assert "explain_approvals_and_exceptions" in names_app

    # AT_RISK or READY should match assess_readiness_risks_and_actions
    matched_risk = registry.match_skills_for_status("AT_RISK")
    names_risk = [s.name for s in matched_risk]
    assert "assess_readiness_risks_and_actions" in names_risk

    # BLOCKED should match assess_readiness_risks_and_actions or assess_case_and_requests
    matched_blocked = registry.match_skills_for_status("BLOCKED")
    names_blocked = [s.name for s in matched_blocked]
    has_expected = (
        "assess_readiness_risks_and_actions" in names_blocked
        or "assess_case_and_requests" in names_blocked
    )
    assert has_expected


def test_skills_read_only_constraint(registry: SkillRegistry) -> None:
    for skill in registry.list_skills():
        assert skill.constraints["write_access"] is False
        assert skill.constraints["execution"] == "read_only"
