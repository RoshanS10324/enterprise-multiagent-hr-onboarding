"""The Fake LLM advisory must be COMPLETE: every section present, nothing truncated, nothing flattened, and privacy redaction applied
to the offending line only, never to the whole advisory.

Regression for a defect found in review: the formatted advisory was passed whole through `sanitize_free_text`, which flattens text to one
line and cuts it to 200 characters, so every advisory came out as a single 200-character fragment while the earlier tests (which only
checked that the banner was present) still passed."""

import pytest

from helpers import ALL, approvals, delivery, make, ready_payload, ticket
from sub_agent.advisor import ADVISORY_BANNER
from sub_agent.agent import Uc04Agent
from sub_agent.contracts import Blocker
from sub_agent.states import ParentCaseStatus, Severity

SECTIONS = ["### Case Evaluation Advisory:", "#### 1. Status & Findings", "#### 2. Approvals", "#### 3. Risks & Blockers",
            "#### 4. Readiness", "#### 5. Recommended Actions", "*(Skills applied:"]

PAYLOADS = {
    "AWAITING_APPROVAL": lambda: make(),
    "BLOCKED": lambda: make(offer_accepted=False),
    "IN_PROGRESS": lambda: make(approval_records=approvals(ALL)),
    "AT_RISK": lambda: make(approval_records=approvals(["201"]), mock_it_ticket_updates=[ticket("201")],
                            delivery_status=[delivery("201", "PENDING", expected="2026-11-20")]),
    "NEEDS_HUMAN_REVIEW": lambda: make(approval_records=approvals(["201"]), mock_it_ticket_updates=[
        ticket("201", "CREATED"), ticket("201", "REJECTED", reason="Wrong model requested")]),
    "READY": lambda: ready_payload(n=3),
}


def _advise(status: str):
    agent = Uc04Agent()
    result, advisory = agent.evaluate_with_advisory(PAYLOADS[status](), idempotency_key=f"adv-{status}")
    assert result.overall_status.value == status
    return agent, result, advisory


@pytest.mark.parametrize("status", sorted(PAYLOADS))
def test_the_formatted_advisory_is_complete_multiline_and_not_truncated(status):
    _, _, advisory = _advise(status)
    text = advisory.formatted_explanation
    assert text.splitlines()[0] == ADVISORY_BANNER
    for heading in SECTIONS:
        assert heading in text, heading
    assert text.count("\n") >= 12 and len(text) > 400, (len(text), text.count("\n"))
    assert text.rstrip().endswith(")*")                       # the skills footer is the last line, so nothing was cut off


@pytest.mark.parametrize("status", sorted(PAYLOADS))
def test_every_structured_section_appears_in_the_formatted_text(status):
    _, _, advisory = _advise(status)
    text = advisory.formatted_explanation
    for part in (advisory.status_explanation, advisory.approval_guidance, advisory.risk_analysis, advisory.readiness_summary):
        for line in part.splitlines():
            assert line in text, line
    for action in advisory.action_recommendations:
        assert f"- {action}" in text, action
    assert f"`{status}`" in text and advisory.case_id in text


def test_a_cancelled_case_gets_a_complete_advisory_too():
    agent = Uc04Agent()
    agent.cases.set_status("SYN-CASE-001", ParentCaseStatus.WITHDRAWN)
    result, advisory = agent.evaluate_with_advisory(make(), idempotency_key="cancelled")
    assert result.overall_status.value == "CANCELLED"
    assert all(h in advisory.formatted_explanation for h in SECTIONS) and "CANCELLED" in advisory.formatted_explanation


def test_the_advisory_is_the_same_every_time_and_changes_nothing():
    agent, result, advisory = _advise("AT_RISK")
    before = (result.model_dump_json(), agent.service.store.version, len(agent.it_desk.calls), len(agent.service.audit.events))
    payload = PAYLOADS["AT_RISK"]()                                     # same input as the first advisory, so the Context line matches
    again = [agent.explain(result, payload).formatted_explanation for _ in range(3)]
    assert again == [advisory.formatted_explanation] * 3
    assert before == (result.model_dump_json(), agent.service.store.version, len(agent.it_desk.calls), len(agent.service.audit.events))


def test_the_risk_section_names_the_risk_and_its_owner():
    _, result, advisory = _advise("AT_RISK")
    code = result.risks[0].code
    assert f"`{code}`" in advisory.formatted_explanation and result.risks[0].owner in advisory.formatted_explanation


def test_only_the_sensitive_line_is_redacted_never_the_whole_advisory():
    agent = Uc04Agent()
    result = agent.evaluate(make(offer_accepted=False), idempotency_key="sensitive")
    leaky = result.model_copy(update={"blockers": [Blocker(code="TEST", severity=Severity.BLOCKING, reason="employee salary AED 5000",
                                                          owner="HR_OFFICER", next_action="Ask HR.", rule_id="BR-15")]})
    advisory = agent.explain(leaky)
    text = advisory.formatted_explanation
    assert "[REDACTED: HR item, see HR]" in text
    assert "salary" not in text.lower() and "5000" not in text
    assert all(h in text for h in SECTIONS) and len(text) > 300      # the rest of the advisory is intact
    for field in (advisory.status_explanation, advisory.risk_analysis, advisory.readiness_summary):
        assert "salary" not in field.lower() and "5000" not in field   # the structured fields are cleaned too


def test_a_missing_result_gives_the_fallback_not_an_exception():
    advisory = Uc04Agent().explain(None)
    assert advisory.overall_status == "UNKNOWN" and ADVISORY_BANNER in advisory.formatted_explanation
