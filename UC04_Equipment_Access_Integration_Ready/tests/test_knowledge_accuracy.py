"""The brain knowledge and the skills are what the Fake LLM quotes, so they must say only what the code does.

Regression for defects found in review: the brain file named states and codes that do not exist (`DRAFT`, `MISSING_CONFIGURATION`,
`CLEARANCE_LATE`, proposals the service never produces), listed a work mode (`HYBRID`) and client ids that do not exist, invented exception
approvers, and described prompt injection as "blocked" (the service flags it and treats it as data). Only 1 of the 7 real risk codes had
specific guidance, and the existing guidance test passed on the generic fallback text because the code name itself contained "clearance".

These tests compare the documents with the code's own vocabulary, so a future change to either side fails here instead of silently
misleading the advisor."""

import glob
import re
from pathlib import Path

import pytest

from sub_agent.knowledge import BrainKnowledgeLoader
from sub_agent.states import CaseStatus, ItemState, WorkMode

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "sub_agent"
BRAIN = ROOT / "brain" / "onboarding" / "uc04_equipment_access.md"
FALLBACK = "Review with HR officer."
LITERAL = re.compile(r'"([A-Z][A-Z0-9_]{2,}(?::[A-Za-z_.]+)?)"')


@pytest.fixture(scope="module")
def loader() -> BrainKnowledgeLoader:
    return BrainKnowledgeLoader()


@pytest.fixture(scope="module")
def vocabulary() -> set[str]:
    """Every upper-case code the service itself uses: enum values, risk, blocker and proposal codes, reason codes."""
    words: set[str] = set()
    for path in [*SRC.glob("*.py"), *(SRC / "entrypoints").glob("*.py"), *(SRC / "adapters").glob("*.py")]:
        words |= set(LITERAL.findall(path.read_text(encoding="utf-8")))
    return words | {"L0", "L1", "L2"}


def backticked_codes(text: str) -> set[str]:
    return set(re.findall(r"`([A-Z][A-Z0-9_]{3,}(?::[A-Za-z_.]+)?)`", text))


def section_bullets(loader, title: str, pattern: str) -> set[str]:
    return set(re.findall(pattern, loader.get_section(title), re.M))


# ---------------------------------------------------------------------------------------------------- no invented codes
def test_every_code_named_in_the_brain_and_skills_is_a_real_code(vocabulary):
    unknown = {}
    for path in [BRAIN, *map(Path, sorted(glob.glob(str(ROOT / "context_layer" / "skills" / "*.md"))))]:
        bad = sorted(c for c in backticked_codes(path.read_text(encoding="utf-8")) if c.split(":")[0] not in {v.split(":")[0] for v in vocabulary})
        if bad:
            unknown[path.name] = bad
    assert unknown == {}, unknown


def test_the_item_and_case_states_listed_are_exactly_the_real_ones(loader):
    items = section_bullets(loader, "item lifecycle", r"^- `([A-Z_]+)`:")
    cases = section_bullets(loader, "case lifecycle", r"^\d+\. `([A-Z_]+)`:")
    assert items == {s.value for s in ItemState} and cases == {s.value for s in CaseStatus}


def test_the_work_modes_and_clients_named_are_real(loader):
    text = BRAIN.read_text(encoding="utf-8")
    for mode in re.findall(r"`(OFFICE|REMOTE|OVERSEAS|HYBRID)`", text):
        assert mode in {m.value for m in WorkMode}, mode
    assert "SYN-CLIENT" not in text and "client_alpha" in text and "client_bravo" in text


# ---------------------------------------------------------------------------------------------------- risk guidance is real
def real_risk_codes() -> list[str]:
    return sorted(set(re.findall(r'_risk\("([A-Z_]+)"', (SRC / "risk.py").read_text(encoding="utf-8"))))


def test_the_service_has_the_seven_risk_codes_the_knowledge_must_cover():
    assert len(real_risk_codes()) == 7


@pytest.mark.parametrize("code", real_risk_codes())
def test_every_real_risk_code_has_specific_guidance_not_the_generic_fallback(loader, code):
    guidance = loader.get_risk_guidance(code)
    assert FALLBACK not in guidance and "next action" in guidance.lower(), guidance
    assert "\n" not in guidance                                        # one clean line, so it reads well inside the advisory


def test_unknown_codes_still_get_the_safe_fallback(loader):
    assert FALLBACK in loader.get_risk_guidance("NOT_A_REAL_RISK")


def test_proposals_named_in_the_guidance_are_proposals_the_service_really_makes(loader, vocabulary):
    proposals = set(re.findall(r"Proposal: `([A-Z_]+)`", loader.get_section("risk and blocker detection")))
    assert proposals and proposals <= vocabulary and "FLAG_TO_CLIENT_ACCOUNT_MANAGER" not in proposals


def test_guidance_names_the_real_owner_and_proposal_for_the_two_best_known_risks(loader):
    delayed = loader.get_risk_guidance("EQUIPMENT_DELAYED_BEYOND_JOINING")
    assert "LOAN_DEVICE" in delayed and "HR_OFFICER" in delayed and "OFFICE_START_OR_LATER_DEPLOYMENT" not in delayed
    clearance = loader.get_risk_guidance("CLIENT_SITE_CLEARANCE_LATE")
    assert "OFFICE_START_OR_LATER_DEPLOYMENT" in clearance and "account_manager" in clearance.lower()


# ---------------------------------------------------------------------------------------------------- what the brain says about behaviour
def test_prompt_injection_is_described_as_flagged_and_treated_as_data_not_blocked(loader):
    text = loader.get_section("privacy").lower()
    assert "treated as data" in text and "stripped" not in text


def test_exception_approvers_are_not_invented(loader):
    text = BRAIN.read_text(encoding="utf-8")
    assert "department_head" not in text and "client_sponsor" not in text


def test_status_guidance_for_every_case_status_is_specific(loader):
    for status in CaseStatus:
        guidance = loader.get_status_guidance(status.value)
        assert len(guidance) > 60 and "No guidance" not in guidance, status
