"""context_layer: skill discovery/loading, the knowledge package's location, and the rule that
neither context_layer nor connectors ever imports the runtime agent."""

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from context_layer.knowledge import PolicyKnowledgeBase
from context_layer.skills import SkillNotFoundError, list_skills, load_skill
from sub_agent.services import policy_qa, welcome

ROOT = Path(__file__).resolve().parents[1]


def test_every_skill_is_discoverable_with_a_versioned_ref():
    assert [s.ref for s in list_skills()] == ["hr-policy-answerer@v2", "onboarding-welcome-writer@v2"]


def test_services_take_their_prompts_from_the_skills():
    assert welcome.NOTE_SKILL.instructions.startswith("Write one short, warm sentence welcoming a new hire")
    assert policy_qa.PREAMBLE == load_skill("hr-policy-answerer").instructions
    assert "verbatim quote" in policy_qa.PREAMBLE


def test_unknown_skill_fails_loudly():
    with pytest.raises(SkillNotFoundError):
        load_skill("does-not-exist")


def test_the_only_policy_source_is_the_packaged_hr_policy_document():
    assert not (ROOT / "context_layer" / "knowledge" / "policies").exists()  # the synthetic policies are gone
    kb = PolicyKnowledgeBase()
    assert kb.document.source == "package:HR_Policy_Consultants.md"
    (hit, *_) = kb.search("annual leave", client_id="CLIENT-ACME", as_of=date(2026, 11, 1))
    assert hit.citation.doc_id == "hr-policy-consultants" and hit.citation.section == "Annual leave"


@pytest.mark.parametrize("module", ["context_layer.knowledge", "context_layer.skills", "connectors.calendar.base",
                                    "connectors.email.base", "connectors.employee_directory.base",
                                    "connectors.notifications.base", "connectors.teams"])
def test_context_layer_and_connectors_never_import_the_agent(module):
    forbidden = ("sub_agent.agent", "sub_agent.api", "sub_agent.tool_registry", "sub_agent.services")
    code = f"import sys, {module}; print(','.join(sorted(m for m in sys.modules if m.startswith({forbidden!r}))))"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src")])}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


@pytest.mark.parametrize("module", ["connectors.calendar.mock", "connectors.email.mock",
                                    "connectors.employee_directory.mock", "connectors.notifications.mock",
                                    "connectors.teams.mock", "connectors.teams.base"])
def test_connector_mocks_import_nothing_from_the_agent_package(module):
    """Stricter than above: a simulated provider must not depend on sub_agent at all (models,
    repository, services), so it cannot carry workflow or business logic."""
    code = f"import sys, {module}; print(','.join(sorted(m for m in sys.modules if m.startswith('sub_agent'))))"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src")])}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""
