"""Skill recommend_role_standard_items (src/sub_agent/role_packages.py, context_layer/skills/recommend_role_standard_items.md).

When nothing has been requested (NO_ITEMS_REQUESTED, DL-17), the skill recommends the role's standard package from the
case's own configuration for HR to review. It is advisory: it changes nothing, and nothing is guessed. A configuration
for another client or role recommends nothing, a MISSING matrix recommends nothing for that kind, and a VERIFIED_EMPTY
matrix recommends an empty list."""

from helpers import Env, codes, make

from sub_agent.contracts import Uc04Input
from sub_agent.role_packages import SKILL_NAME, recommend_standard_items
from sub_agent.skills import SkillRegistry
from sub_agent.states import CaseStatus


def _input(**config):
    return Uc04Input.model_validate(make(requested_access=[], requested_equipment=[], config=config))


def test_the_roles_verified_lists_are_recommended_in_order_with_the_configuration_version():
    rec = recommend_standard_items(_input())
    assert rec.equipment == ["laptop-standard", "headset-basic"]
    assert rec.access == ["email-teams-account", "source-control-access", "ci-pipeline-access"]
    assert (rec.client_id, rec.role_id, rec.config_version) == ("client_alpha", "software_engineer", "alpha-v1")
    assert rec.gaps == [] and rec.advisory_only is True and rec.skill == SKILL_NAME


def test_each_role_gets_its_own_configurations_lists():
    bravo = recommend_standard_items(Uc04Input.model_validate(make("bravo", requested_access=[], requested_equipment=[])))
    assert bravo.equipment == ["laptop-standard", "monitor-27"] and bravo.access == ["email-teams-account", "vpn-access"]


def test_a_configuration_for_another_role_recommends_nothing():
    rec = recommend_standard_items(_input(role_id="data_engineer"))
    assert rec.equipment is None and rec.access is None and rec.gaps == ["CONFIG_SCOPE_MISMATCH"]


def test_a_missing_matrix_is_no_recommendation_never_an_empty_list():
    rec = recommend_standard_items(_input(role_equipment_matrix={"state": "MISSING"}))
    assert rec.equipment is None and rec.access == ["email-teams-account", "source-control-access", "ci-pipeline-access"]
    assert rec.gaps == ["CONFIG_MISSING:role_equipment_matrix"]


def test_a_verified_empty_matrix_recommends_nothing_for_that_kind():
    rec = recommend_standard_items(_input(role_access_matrix={"state": "VERIFIED_EMPTY"}))
    assert rec.access == [] and rec.gaps == []


def test_the_recommendation_changes_nothing_on_the_case():
    env = Env()
    payload = make(requested_access=[], requested_equipment=[])
    before = env.run(payload)
    recommend_standard_items(Uc04Input.model_validate(payload))
    after = env.run(payload)
    assert "NO_ITEMS_REQUESTED" in codes(before.blockers) and "NO_ITEMS_REQUESTED" in codes(after.blockers)
    assert after.items == [] and after.overall_status == before.overall_status


def test_the_skill_is_registered_read_only_and_matched_only_when_nothing_was_requested():
    registry = SkillRegistry()
    skill = registry.get_skill(SKILL_NAME)
    assert skill is not None and skill.constraints["write_access"] is False
    assert skill.constraints["requests_items"] is False and skill.constraints["approves"] is False
    # Its text names no case status, so status matching alone never selects it.
    assert all(SKILL_NAME not in {s.name for s in registry.match_skills_for_status(st.value)} for st in CaseStatus)

    env = Env()
    nothing = env.run(make(requested_access=[], requested_equipment=[]))
    something = env.run(make())
    assert SKILL_NAME in {s.name for s in registry.match_skills(result=nothing)}
    assert SKILL_NAME not in {s.name for s in registry.match_skills(result=something)}
