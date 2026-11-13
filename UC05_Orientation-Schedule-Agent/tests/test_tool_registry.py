"""Phase 8: onboarding operations registered through the real ToolGateway/Policy/HITLInbox/
AuditLog -- enforcement is verified against those actual classes, not inferred from a prompt.
"""

from datetime import date, datetime, timezone

import pytest

from context_layer.knowledge.policy_index import PolicyKnowledgeBase
from hr_onboarding_common.contracts import Actor, Autonomy, Purpose
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import Decision, HITLInbox, ReasonCode
from hr_onboarding_common.platform.policy import Policy, PolicyStore, PolicyViolation
from sub_agent.agent import AGENT_HOME
from sub_agent.fakes import build_mock_onboarding, FakeCalendar, FakeNotifier, offline_llm
from sub_agent.models import ResponseStatus, SessionType
from sub_agent.tool_registry import DuplicateSendError, OnboardingToolRegistry, TenantAuthorizationError

ACME = "CLIENT-ACME"
GLOBEX = "CLIENT-GLOBEX"
AS_OF = date(2026, 11, 1)
ACTOR = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
             client_id=ACME)
GLOBEX_ACTOR = Actor(on_behalf_of="user:hr-admin-02", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
                     client_id=GLOBEX)


def _real_policy() -> Policy:
    return PolicyStore(AGENT_HOME / "guardrails").for_agent("employee-onboarding")


@pytest.fixture
def registry():
    return OnboardingToolRegistry(port=build_mock_onboarding(), calendar=FakeCalendar(), kb=PolicyKnowledgeBase(),
                                  llm=offline_llm(), policy=_real_policy(), hitl=HITLInbox(), audit=AuditLog(),
                                  actor=ACTOR, notifier=FakeNotifier())


# --------------------------------------------------------------------------- the shipped policy.yaml itself

def test_real_policy_yaml_covers_every_registry_operation(registry):
    record = registry.get_record("EMP-001")
    assert record.employee_id == "EMP-001"

    registry.ensure_tasks("EMP-001")
    assert registry.list_tasks("EMP-001")
    assert registry.list_sessions("EMP-001") == []
    assert registry.get_client_config(ACME).client_id == ACME
    assert registry.search_policy("What is the dress code?", as_of=AS_OF)
    answer = registry.answer_policy_question("What is the dress code?", as_of=AS_OF)
    assert answer.answer is not None

    draft, item = registry.draft_welcome("EMP-001", template_id="welcome", language="en", workflow_instance="EMP-001")
    assert item.gate == "comms_approval"

    session, s_item = registry.schedule_session("EMP-002", session_type=SessionType.ORIENTATION,
                                                start=datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc),  # joins 11-09
                                                attendees=["HM-01"], workflow_instance="EMP-002")
    assert s_item.gate == "scheduling_approval"


def test_tool_calls_are_audited_with_the_real_audit_log(registry):
    registry.get_record("EMP-001")
    registry.ensure_tasks("EMP-001")

    tool_calls = {e["tool"] for e in registry.audit.find("tool.call")}
    assert {"onboarding.get_record", "checklist.ensure_tasks"} <= tool_calls


# --------------------------------------------------------------------------- read/write permission enforcement

def test_tool_not_on_the_allowlist_is_a_policy_violation(registry):
    restricted = registry.policy.model_copy(update={"tools": registry.policy.tools.model_copy(
        update={"read": [], "write": []})})
    registry.policy = restricted
    registry.gateway.policy = restricted

    with pytest.raises(PolicyViolation, match="not on the allowlist"):
        registry.get_record("EMP-001")
    with pytest.raises(PolicyViolation, match="not on the allowlist"):
        registry.ensure_tasks("EMP-001")


def test_l0_autonomy_blocks_every_write_but_not_reads(registry):
    l0_policy = registry.policy.model_copy(update={"autonomy": Autonomy.L0})
    registry.policy = l0_policy
    registry.gateway.policy = l0_policy

    assert registry.get_record("EMP-001").employee_id == "EMP-001"  # reads still work
    with pytest.raises(PolicyViolation, match="L0"):
        registry.ensure_tasks("EMP-001")
    assert registry.port.list_tasks("EMP-001") == []  # the blocked write left no partial state


def test_disabled_agent_kill_switch_blocks_everything(registry):
    """Finding S6: ToolGateway.call() itself must refuse to run ANY tool -- read or write -- once
    the policy is disabled, not only the LangGraph run path. This replaces a prior version of this
    test that asserted only `disabled.enabled is False` (a tautology about the Policy dataclass,
    not a proof that any call was actually blocked)."""
    disabled = registry.policy.model_copy(update={"enabled": False})
    registry.policy = disabled
    registry.gateway.policy = disabled

    with pytest.raises(PolicyViolation, match="disabled"):
        registry.get_record("EMP-001")  # a read tool -- the kill switch blocks reads too
    with pytest.raises(PolicyViolation, match="disabled"):
        registry.ensure_tasks("EMP-001")
    assert registry.port.list_tasks("EMP-001") == []  # the blocked write left no partial state
    assert registry.audit.find("tool.call") == []  # no tool actually ran
    assert len(registry.audit.find("policy.denied")) == 2


# --------------------------------------------------------------------------- tenant checks

def test_cross_tenant_read_is_blocked_before_any_tool_call(registry):
    with pytest.raises(TenantAuthorizationError):
        registry.get_record("EMP-101")  # EMP-101 belongs to CLIENT-GLOBEX
    assert registry.audit.find("tool.call") == []


def test_cross_tenant_write_is_blocked_before_any_tool_call(registry):
    with pytest.raises(TenantAuthorizationError):
        registry.ensure_tasks("EMP-101")
    assert registry.port.list_tasks("EMP-101") == []


def test_cross_tenant_client_config_is_blocked(registry):
    with pytest.raises(TenantAuthorizationError):
        registry.get_client_config(GLOBEX)



# --------------------------------------------------------------------------- approval gate: write_requires_approval

def test_send_welcome_email_requires_an_approval_id(registry):
    draft, _ = registry.draft_welcome("EMP-001", template_id="welcome", language="en", workflow_instance="EMP-001")
    with pytest.raises(PolicyViolation, match="no approval_id"):
        registry.send_welcome_email(draft_id=draft.id, approval_id=None)


def test_send_welcome_email_requires_an_approved_not_pending_item(registry):
    draft, item = registry.draft_welcome("EMP-001", template_id="welcome", language="en", workflow_instance="EMP-001")
    with pytest.raises(PolicyViolation, match="pending"):
        registry.send_welcome_email(draft_id=draft.id, approval_id=item.id)


def test_rejected_approval_cannot_be_used_to_send(registry):
    draft, item = registry.draft_welcome("EMP-001", template_id="welcome", language="en", workflow_instance="EMP-001")
    registry.hitl.decide(item.id, Decision.REJECTED, approver="hr-admin-01", approver_roles=["hr_admin"],
                         client_id=ACME, reason_code=ReasonCode.TONE)

    with pytest.raises(PolicyViolation, match="not approved"):
        registry.send_welcome_email(draft_id=draft.id, approval_id=item.id)


def test_approved_item_allows_exactly_one_send_then_blocks_a_replay(registry):
    draft, item = registry.draft_welcome("EMP-001", template_id="welcome", language="en", workflow_instance="EMP-001")
    registry.hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"],
                         client_id=ACME)

    result = registry.send_welcome_email(draft_id=draft.id, approval_id=item.id)
    assert result == {"sent": True, "draft_id": draft.id, "simulated": True}

    with pytest.raises(DuplicateSendError):
        registry.send_welcome_email(draft_id=draft.id, approval_id=item.id)


def test_send_welcome_email_approval_for_a_different_draft_is_rejected(registry):
    draft1, _ = registry.draft_welcome("EMP-001", template_id="welcome", language="en", workflow_instance="EMP-001")
    draft2, item2 = registry.draft_welcome("EMP-002", template_id="welcome", language="en",
                                           workflow_instance="EMP-002")
    registry.hitl.decide(item2.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"],
                         client_id=ACME)

    with pytest.raises(PolicyViolation, match="not"):
        registry.send_welcome_email(draft_id=draft1.id, approval_id=item2.id)


def test_send_welcome_email_rejects_an_approval_for_another_draft_of_the_same_employee(registry):
    """An approval binds to one draft: approving draft B must not authorize sending draft A, even
    though both belong to the same employee (the gateway's subject check alone would allow it)."""
    draft_a, _ = registry.draft_welcome("EMP-001", template_id="welcome", language="en", workflow_instance="EMP-001")
    draft_b, item_b = registry.draft_welcome("EMP-001", template_id="welcome", language="en",
                                             workflow_instance="EMP-001")
    registry.hitl.decide(item_b.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"],
                         client_id=ACME)

    with pytest.raises(PolicyViolation, match="not the welcome approval for draft"):
        registry.send_welcome_email(draft_id=draft_a.id, approval_id=item_b.id)
    assert registry.notifier.sent == []


def test_send_welcome_email_sends_the_content_hr_approved_including_edits(registry):
    draft, item = registry.draft_welcome("EMP-001", template_id="welcome", language="en", workflow_instance="EMP-001")
    registry.hitl.decide(item.id, Decision.EDITED, approver="hr-admin-01", approver_roles=["hr_admin"], client_id=ACME,
                         reason_code=ReasonCode.TONE, edited_payload={**item.payload, "subject": "Edited subject"})

    registry.send_welcome_email(draft_id=draft.id, approval_id=item.id)

    (sent,) = registry.notifier.sent
    assert sent.subject == "Edited subject" and sent.recipient_address == "sara.almarzooqi@example.com"


def test_duplicate_send_is_refused_durably_not_only_within_one_registry(registry):
    draft, item = registry.draft_welcome("EMP-001", template_id="welcome", language="en", workflow_instance="EMP-001")
    registry.hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"],
                         client_id=ACME)
    registry.send_welcome_email(draft_id=draft.id, approval_id=item.id)
    # a brand-new registry (e.g. the next HTTP request) over the same repository and inbox
    next_request = OnboardingToolRegistry(port=registry.port, calendar=registry.calendar, kb=registry.kb,
                                          llm=registry.llm, policy=_real_policy(), hitl=registry.hitl,
                                          audit=registry.audit, actor=ACTOR, notifier=registry.notifier)

    with pytest.raises(DuplicateSendError):
        next_request.send_welcome_email(draft_id=draft.id, approval_id=item.id)
    assert len(registry.notifier.sent) == 1


def test_notify_approver_routes_a_team_lead_gate_to_the_team_lead_on_record(registry):
    _, item = registry.schedule_session("EMP-001", session_type=SessionType.TEAM_INTRO,
                                        start=datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc), attendees=["TL-001"],
                                        workflow_instance="EMP-001")
    receipt = registry.notify_approver(item)
    assert receipt.recipient_id == "TL-001" and receipt.simulated is True


def test_applying_another_tenants_decision_is_refused(registry):
    _, item = registry.draft_welcome("EMP-001", template_id="welcome", language="en", workflow_instance="EMP-001")
    globex = OnboardingToolRegistry(port=registry.port, calendar=registry.calendar, kb=registry.kb, llm=registry.llm,
                                    policy=_real_policy(), hitl=registry.hitl, audit=registry.audit,
                                    actor=GLOBEX_ACTOR, notifier=registry.notifier)
    with pytest.raises(TenantAuthorizationError):
        globex.apply_welcome_decision(item)


class _RepositoryIgnoringTenantFilter:
    """An OnboardingPort that ignores the `client_id=` filter on single-record reads -- what a
    careless real adapter might do. The registry must not depend on the repository for tenancy."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_session(self, session_id, *, client_id=None):
        return self._inner.get_session(session_id)

    def get_communication_draft(self, draft_id, *, client_id=None):
        return self._inner.get_communication_draft(draft_id)


def test_registry_enforces_tenancy_itself_even_if_the_repository_ignores_its_client_filter():
    inner, hitl, calendar, audit = build_mock_onboarding(), HITLInbox(), FakeCalendar(), AuditLog()
    common = dict(calendar=calendar, kb=PolicyKnowledgeBase(), llm=offline_llm(), policy=_real_policy(), hitl=hitl,
                  audit=audit, notifier=FakeNotifier())
    globex = OnboardingToolRegistry(port=inner, actor=GLOBEX_ACTOR, **common)
    session, _ = globex.schedule_session("EMP-101", session_type=SessionType.ORIENTATION,
                                         start=datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc), attendees=[],
                                         workflow_instance="EMP-101")
    draft, _ = globex.draft_welcome("EMP-101", template_id="welcome", language="en", workflow_instance="EMP-101")

    acme = OnboardingToolRegistry(port=_RepositoryIgnoringTenantFilter(inner), actor=ACTOR, **common)
    attempts = [lambda: acme.get_session(session.id), lambda: acme.list_invitations(session.id),
                lambda: acme.is_attendance_satisfied(session.id),
                lambda: acme.record_participant_response(session.id, "EMP-101", ResponseStatus.DECLINED),
                lambda: acme.send_welcome_email(draft_id=draft.id, approval_id="apr-anything")]
    for attempt in attempts:
        with pytest.raises(TenantAuthorizationError):
            attempt()

    assert inner.list_participant_responses(session.id) == []
    assert len([e for e in audit.find("policy.denied") if e.get("record_kind")]) == len(attempts)
