"""Smaller defects found in review, each with a regression test:
- the skill `retrieve_access_equipment_requirements` was never selected for any advisory;
- `generate_advisory` ignored its `input_data` argument;
- `Uc04Agent(clock=...)` was ignored and every audit timestamp was the fixed test date 2026-10-15;
- `Uc04Agent.submit_eligible` was annotated as returning `Uc04Result` but returns `SubmissionReport`."""

import typing
from datetime import datetime, timedelta, timezone

import pytest

from helpers import ALL, approvals, make
from sub_agent.agent import Uc04Agent
from sub_agent.contracts import SubmissionReport, Uc04Input
from sub_agent.skills import SkillRegistry

pytest.importorskip("fastapi")
from sub_agent.api.scenarios import BY_KEY  # noqa: E402

RETRIEVE = "retrieve_access_equipment_requirements"


def advise(payload, key):
    agent = Uc04Agent()
    result, advisory = agent.evaluate_with_advisory(payload, idempotency_key=key)
    return result, advisory


# ---------------------------------------------------------------------------------------------------- skill selection
def test_the_requirements_skill_is_selected_for_an_out_of_matrix_item():
    result, advisory = advise(BY_KEY["03_out_of_matrix_access"].body["input"], "oom")
    assert any(i.exception_required for i in result.items)
    assert RETRIEVE in advisory.applicable_skills and f"{RETRIEVE}" in advisory.formatted_explanation


def test_the_requirements_skill_is_selected_when_configuration_is_missing():
    result, advisory = advise(BY_KEY["03_missing_access_matrix"].body["input"], "gap")
    assert result.config_gaps and RETRIEVE in advisory.applicable_skills


def test_the_requirements_skill_is_not_added_to_an_ordinary_case():
    _, advisory = advise(make(), "plain")
    assert RETRIEVE not in advisory.applicable_skills
    assert advisory.applicable_skills == ["explain_approvals_and_exceptions"]


def test_the_skill_order_is_stable_and_has_no_duplicates():
    payload = BY_KEY["03_out_of_matrix_access"].body["input"]
    _, first = advise(payload, "a")
    _, second = advise(payload, "b")
    assert first.applicable_skills == second.applicable_skills and len(set(first.applicable_skills)) == len(first.applicable_skills)


def test_every_skill_can_now_be_selected_by_some_case():
    cases = {"awaiting": make(), "blocked": make(offer_accepted=False), "progress": make(approval_records=approvals(ALL)),
             "oom": BY_KEY["03_out_of_matrix_access"].body["input"],
             "nothing_requested": make(requested_access=[], requested_equipment=[])}  # recommend_role_standard_items
    used = set()
    for name, payload in cases.items():
        used |= set(advise(payload, name)[1].applicable_skills)
    used |= {s.name for status in ("AT_RISK", "READY", "NEEDS_HUMAN_REVIEW") for s in SkillRegistry().match_skills_for_status(status)}
    assert used == set(SkillRegistry().load_all())


# ---------------------------------------------------------------------------------------------------- input_data is used
def test_the_advisory_states_the_case_context_from_the_input():
    for payload in (make(), Uc04Input.model_validate(make())):          # a dict and a validated model
        _, advisory = advise(payload, f"ctx-{type(payload).__name__}")
        context = next(l for l in advisory.formatted_explanation.splitlines() if l.startswith("**Context**"))
        assert "client_alpha" in context and "software_engineer" in context and "2026-11-16" in context and "OFFICE" in context


def test_without_an_input_there_is_simply_no_context_line():
    agent = Uc04Agent()
    result = agent.evaluate(make(), idempotency_key="no-ctx")
    assert "**Context**" not in agent.explain(result).formatted_explanation


def test_a_partial_dict_does_not_break_the_advisory():
    agent = Uc04Agent()
    result = agent.evaluate(make(), idempotency_key="partial")
    text = agent.explain(result, {"client_id": "client_alpha"}).formatted_explanation
    assert "**Context**: client client_alpha" in text and "joining" not in text.split("**Context**")[1].split("\n")[0]


# ---------------------------------------------------------------------------------------------------- the agent's clock
def test_by_default_audit_timestamps_are_the_real_current_time():
    result = Uc04Agent().evaluate(make(), idempotency_key="clock")
    stamps = [datetime.fromisoformat(e["timestamp"]) for e in result.audit_events]
    now = datetime.now(timezone.utc)
    assert stamps and all(abs(now - s) < timedelta(minutes=2) for s in stamps)
    assert {s.date().isoformat() for s in stamps} != {"2026-10-15"} or now.date().isoformat() == "2026-10-15"


def test_an_injected_clock_is_used_for_every_audit_event():
    fixed = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    agent = Uc04Agent(clock=lambda: fixed)
    result = agent.evaluate(make(), idempotency_key="injected")
    assert {e["timestamp"] for e in result.audit_events} == {fixed.isoformat()}
    agent.notify_readiness(result, approved_by="SYN-USER-100")
    assert agent.service.audit.events[-1]["timestamp"] == fixed.isoformat()


# ---------------------------------------------------------------------------------------------------- annotation matches reality
def test_submit_eligible_is_annotated_with_what_it_really_returns():
    assert typing.get_type_hints(Uc04Agent.submit_eligible)["return"] is SubmissionReport
    report = Uc04Agent().submit_eligible(make(approval_records=approvals(ALL)))
    assert isinstance(report, SubmissionReport) and len(report.submitted) == 4
