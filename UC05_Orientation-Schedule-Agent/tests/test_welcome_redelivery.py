"""Recovery from a welcome delivery that did not complete: the operator's resolution record (who,
when, why) and an authorized new delivery attempt (registry.redeliver_welcome) that is only ever
made when the welcome is known NOT to have been delivered -- never a blind resend while the
outcome is unknown, and never a second delivery."""

from contextlib import suppress
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.contracts import Actor
from hr_onboarding_common.platform.hitl import Decision, HITLApplyError

from sub_agent import api
from sub_agent.agent import (
    AGENT_HOME,
    OnboardingAgent,
    OnboardingWorkflowInput,
    WorkflowEvent,
)
from sub_agent.fakes import FakeNotifier, build_fake_dependencies, offline_llm
from sub_agent.models import CommunicationStatus, OperationStatus, TenantMismatchError
from sub_agent.services.delivery import DeliveryOutcomeUncertainError
from sub_agent.tool_registry import (
    DuplicateSendError,
    OnboardingToolRegistry,
    TenantAuthorizationError,
)

ACME, GLOBEX = "CLIENT-ACME", "CLIENT-GLOBEX"
NOW = datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)


def _actor(client_id=ACME, roles=("hr_admin",)):
    return Actor(on_behalf_of="user:hr-admin-01", roles=list(roles), client_id=client_id)


@pytest.fixture
def world():
    deps = build_fake_dependencies(clock=lambda: NOW)
    deps.notifier = FakeNotifier(idempotent_delivery=False)  # the strict case: a resend could deliver twice
    ctx = AgentContext.build(agent_home=AGENT_HOME, llm=offline_llm(), connector=deps)
    agent = OnboardingAgent(ctx)
    ctx.hitl.subscribe(agent.handle_approval_decision)
    return SimpleNamespace(deps=deps, ctx=ctx, agent=agent, repo=deps.repository, notifier=deps.notifier,
                           hitl=ctx.hitl, audit=ctx.audit)


def _registry(world, client_id=ACME, roles=("hr_admin",)):
    return OnboardingToolRegistry.from_dependencies(world.deps, llm=world.ctx.llm, policy=world.agent.policy,
                                                    hitl=world.hitl, audit=world.audit,
                                                    actor=_actor(client_id, roles))


def _welcome(world, *, approve=True, fault=None):
    """Joining day for CLIENT-ACME drafts EMP-001's welcome; `fault` then sets up the notifier's
    failure and HR approves (the send happens in the approval listener). Returns (draft_id, key)."""
    world.agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.JOINING_DAY, as_of=date(2026, 11, 2)),
                          actor=_actor(), instance="joining-day:2026-11-02")
    (item,) = [i for i in world.hitl.list(status=Decision.PENDING, client_id=ACME)
               if i.subject_id == "EMP-001" and i.proposal_type == "welcome_message"]
    if fault:
        fault(world.notifier)
    if approve:
        with suppress(HITLApplyError):  # a failed send surfaces as an apply error; the ledger tells the outcome
            world.hitl.decide(item.id, Decision.APPROVED, approver="user:hr-admin-01", approver_roles=["hr_admin"],
                              client_id=ACME)
    return item.payload["id"], f"welcome:{ACME}:{item.payload['id']}"


def _sent(world):
    return world.notifier.sent_to("EMP-001", kind="welcome")


def test_an_outage_resolved_as_not_delivered_is_redelivered_exactly_once(world):
    # an outage: nothing went out, but the ledger cannot know that
    draft_id, key = _welcome(world, fault=lambda n: setattr(n, "fail_next", 1))
    assert world.repo.get_operation(key).status == OperationStatus.UNCERTAIN and _sent(world) == []
    reg = _registry(world)

    with pytest.raises(DeliveryOutcomeUncertainError):  # unknown outcome: resolve first
        reg.redeliver_welcome(draft_id, reason="the new hire says nothing arrived")
    assert _sent(world) == []

    reg.resolve_operation(key, outcome=OperationStatus.RELEASED, reason="provider log shows no accepted message")
    resolved = world.repo.get_operation(key)
    assert resolved.status == OperationStatus.RELEASED
    assert resolved.detail["resolved_by"] == "user:hr-admin-01" and resolved.detail["resolved_at"]
    assert resolved.detail["resolution_reason"] == "provider log shows no accepted message"

    reg.redeliver_welcome(draft_id, reason="confirmed not delivered")
    assert len(_sent(world)) == 1
    assert world.repo.get_operation(key).status == OperationStatus.SUCCEEDED
    assert world.repo.get_communication_draft(draft_id).status == CommunicationStatus.SENT
    (audited,) = world.audit.find("welcome.redelivery_authorized")
    assert audited["reason"] == "confirmed not delivered" and audited["previous_operation_status"] == "released"

    with pytest.raises(DuplicateSendError):
        reg.redeliver_welcome(draft_id, reason="again")
    assert len(_sent(world)) == 1


def test_a_lost_response_resolved_as_delivered_is_never_sent_again(world):
    # the provider DID deliver; only its response was lost
    draft_id, key = _welcome(world, fault=lambda n: setattr(n, "lose_next_responses", 1))
    assert world.repo.get_operation(key).status == OperationStatus.UNCERTAIN and len(_sent(world)) == 1
    reg = _registry(world)

    reg.resolve_operation(key, outcome=OperationStatus.SUCCEEDED, provider_ref="ticket-4711",
                          reason="the new hire confirmed receipt")
    with pytest.raises(DuplicateSendError):
        reg.redeliver_welcome(draft_id, reason="just to be sure")
    assert len(_sent(world)) == 1
    assert world.audit.find("welcome.redelivery_authorized") == []


def test_a_permanent_refusal_is_redelivered_once_its_cause_is_fixed(world):
    draft_id, key = _welcome(world, fault=lambda n: n.reject_recipients.add("EMP-001"))
    assert world.repo.get_operation(key).status == OperationStatus.REJECTED and _sent(world) == []

    world.notifier.reject_recipients.clear()  # e.g. the address was corrected
    _registry(world).redeliver_welcome(draft_id, reason="address corrected in HRIS")
    assert len(_sent(world)) == 1 and world.repo.get_operation(key).status == OperationStatus.SUCCEEDED


def test_redelivery_is_hr_admin_only_needs_a_reason_an_approved_draft_and_the_same_tenant(world):
    draft_id, key = _welcome(world, fault=lambda n: setattr(n, "fail_next", 1))
    _registry(world).resolve_operation(key, outcome=OperationStatus.RELEASED, reason="checked")

    with pytest.raises(PermissionError):
        _registry(world, roles=("team_lead",)).redeliver_welcome(draft_id, reason="please")
    with pytest.raises(ValueError, match="reason is required"):
        _registry(world).redeliver_welcome(draft_id, reason="   ")
    with pytest.raises((KeyError, TenantAuthorizationError, TenantMismatchError)):  # all reported as 404
        _registry(world, client_id=GLOBEX).redeliver_welcome(draft_id, reason="cross tenant")
    assert _sent(world) == []


def test_an_unapproved_draft_cannot_be_delivered_through_redelivery(world):
    draft_id, _ = _welcome(world, approve=False)
    with pytest.raises(ValueError, match="only an approved welcome"):
        _registry(world).redeliver_welcome(draft_id, reason="skip the approval")
    assert _sent(world) == []


def test_the_redeliver_route_is_hr_admin_only_and_needs_a_reason():
    headers = {"X-User-Id": "user:hr-admin-01", "X-User-Roles": "hr_admin", "X-Client-Id": ACME}
    with TestClient(api.app) as client:
        route = "/v1/onboarding/admin/welcome/comm-unknown/redeliver"
        assert client.post(route, headers={**headers, "X-User-Roles": "employee"},
                           json={"reason": "x"}).status_code == 403
        assert client.post(route, headers=headers, json={"reason": ""}).status_code == 422
        assert client.post(route, headers=headers, json={"reason": "checked"}).status_code == 404
