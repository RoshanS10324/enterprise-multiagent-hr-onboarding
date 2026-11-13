"""The mock Outlook notification must respect the same controls as everything else in UC-04 (L1: nothing is sent without a person's
approval, GR-01; the policy kill switch, BR-18; every action audited, GR-09).

Regression for a defect found in review: `notify_readiness` sent a message to any recipient with no approval step and kept working with
the kill switch off, because it never went through the policy."""

import pytest

from helpers import make, ready_payload
from sub_agent.agent import Uc04Agent
from sub_agent.policy import PolicyViolation

RECIPIENT = "hiring-manager@example.com"


@pytest.fixture
def agent_and_result():
    agent = Uc04Agent()
    return agent, agent.evaluate(ready_payload(n=3), idempotency_key="notify-1")


def events(agent, event_type):
    return [e for e in agent.service.audit.events if e["event_type"] == event_type]


def test_nothing_is_sent_without_an_explicit_human_approval(agent_and_result):
    agent, result = agent_and_result
    with pytest.raises(PolicyViolation, match="approval"):
        agent.notify_readiness(result, recipient_email=RECIPIENT)
    assert agent.outlook.email_count == 0


@pytest.mark.parametrize("approver", ["", "   ", "alice", "SYN-USER-1", "approved"])
def test_a_missing_or_malformed_approver_reference_is_refused(agent_and_result, approver):
    agent, result = agent_and_result
    with pytest.raises(PolicyViolation):
        agent.notify_readiness(result, recipient_email=RECIPIENT, approved_by=approver)
    assert agent.outlook.email_count == 0


def test_an_approved_notification_is_sent_audited_and_changes_nothing(agent_and_result):
    agent, result = agent_and_result
    before = (result.model_dump_json(), agent.service.store.version, len(agent.it_desk.calls))
    sent = agent.notify_readiness(result, recipient_email=RECIPIENT, approved_by="SYN-USER-100")
    assert sent.message_id.startswith("SYN-MSG-") and "[SIMULATED NOTIFICATION]" in sent.subject and "READY" in sent.subject
    assert agent.outlook.email_count == 1
    assert before == (result.model_dump_json(), agent.service.store.version, len(agent.it_desk.calls))
    sent_events = events(agent, "notification.sent")
    assert len(sent_events) == 1 and sent_events[0]["rule_id"] == "GR-01" and sent_events[0]["case_id"] == result.case_id


def test_the_kill_switch_stops_notifications_too(agent_and_result):
    agent, result = agent_and_result
    agent.service.policy.enabled = False
    with pytest.raises(PolicyViolation, match="kill switch"):
        agent.notify_readiness(result, recipient_email=RECIPIENT, approved_by="SYN-USER-100")
    assert agent.outlook.email_count == 0


def test_refusals_are_audited_without_the_recipient_address(agent_and_result):
    agent, result = agent_and_result
    with pytest.raises(PolicyViolation):
        agent.notify_readiness(result, recipient_email=RECIPIENT)
    refused = events(agent, "notification.refused")
    assert len(refused) == 1 and refused[0]["rule_id"] == "GR-01"
    assert RECIPIENT not in str(refused[0])                       # personal data stays out of the audit trail


def test_a_draft_can_always_be_prepared_and_never_sends(agent_and_result):
    agent, result = agent_and_result
    draft = agent.draft_readiness_notification(result, recipient_email=RECIPIENT)
    assert draft.to == [RECIPIENT] and "[SIMULATED NOTIFICATION]" in draft.subject and "READY" in draft.subject
    assert "does not constitute an approval" in draft.body
    assert agent.outlook.email_count == 0


def test_the_sent_message_equals_the_draft(agent_and_result):
    agent, result = agent_and_result
    draft = agent.draft_readiness_notification(result, recipient_email=RECIPIENT)
    sent = agent.notify_readiness(result, recipient_email=RECIPIENT, approved_by="SYN-USER-100")
    assert sent.subject == draft.subject


def test_a_notification_never_changes_the_case_status():
    agent = Uc04Agent()
    result = agent.evaluate(make(), idempotency_key="status")
    agent.notify_readiness(result, recipient_email=RECIPIENT, approved_by="SYN-USER-100")
    assert agent.evaluate(make(), idempotency_key="status").overall_status == result.overall_status
