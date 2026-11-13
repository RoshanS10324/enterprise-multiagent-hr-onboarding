"""The contract the central onboarding orchestrator (HR-Onboarding-Common, orchestration/adapters/
uc05_orientation.py and orchestration/workers/uc_worker_runtime.py) relies on from this agent.

The orchestrator runs this agent in its own worker process and calls only the public entry points
below; it validates every result against these status vocabularies and treats any other value as a
contract violation. A change here that breaks the orchestrator fails this file first."""

import inspect
from datetime import date

from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.contracts import Actor
from hr_onboarding_common.platform.hitl import Decision
from sub_agent.agent import (AGENT_HOME, AGENT_ID, REDELIVERABLE_STAGES, OnboardingAgent, OnboardingWorkflowInput,
                             Stage, WorkflowEvent, workflow_id_for)
from sub_agent.fakes import build_fake_dependencies, offline_llm
from sub_agent.models import (BuddyCandidate, CaseStatus, CommunicationStatus, OperationStatus, SessionStatus,
                              SessionType)
from sub_agent.runtime import build_context
from sub_agent.services.intake import REQUIRED_DIRECTORY_FIELDS
from sub_agent.tool_registry import OnboardingToolRegistry

TEST_A = "CLIENT-TEST-A"


def test_status_vocabularies_match_what_the_orchestrator_validates():
    assert AGENT_ID == "employee-onboarding"
    assert {s.value for s in SessionStatus} == {"scheduled", "confirmed", "cancelled"}
    assert {s.value for s in SessionType} == {"orientation", "induction", "team_intro", "buddy_intro"}
    assert {s.value for s in CommunicationStatus} == {"draft", "approved", "rejected", "sent"}
    assert {s.value for s in OperationStatus} == {"pending", "succeeded", "uncertain", "rejected", "released"}
    assert {s.value for s in CaseStatus} == {"active", "cancelled", "withdrawn"}
    # "withdrawn": retracted by the requesting agent before anyone decided (HITLInbox.withdraw) -- the
    # orchestrator withdraws a superseded day-one signoff on a joining-date change. Not a decision.
    assert {d.value for d in Decision} == {"pending", "approved", "edited", "rejected", "withdrawn"}


def test_workflow_stages_and_events_the_orchestrator_interprets_exist():
    for name in ("received", "case_inactive", "event_rejected", "invalid_employee_data", "scheduling_failed",
                 "induction_awaiting_approval", "buddy_awaiting_approval", "team_intro_awaiting_approval",
                 "welcome_awaiting_approval", "welcome_sent", "welcome_delivery_uncertain", "welcome_not_configured",
                 "nothing_to_do",
                 "policy_answered", "policy_no_answer"):
        Stage(name)
    assert {"invalid_employee_data", "scheduling_failed"} <= {s.value for s in REDELIVERABLE_STAGES}
    for event in ("offer_accepted", "approval_decided", "joining_day", "draft_welcome", "policy_question"):
        WorkflowEvent(event)
    assert set(REQUIRED_DIRECTORY_FIELDS) >= {"team", "team_lead_id"}


def test_public_entry_points_keep_their_signatures():
    def params(fn):
        return set(inspect.signature(fn).parameters)

    assert {"event", "actor", "instance"} <= params(OnboardingAgent.run_event)
    assert {"actor", "employee_id"} <= params(OnboardingAgent.case_status)
    assert {"workflow_id"} <= params(OnboardingAgent.workflow_state)
    assert {"item"} <= params(OnboardingAgent.handle_approval_decision)
    assert {"employee_id", "workflow_instance"} <= params(OnboardingToolRegistry.propose_buddy_assignment)
    assert {"employee_id", "session_type", "start", "attendees", "workflow_instance"} <= params(
        OnboardingToolRegistry.schedule_session)
    assert {"employee_id", "start", "workflow_instance"} <= params(OnboardingToolRegistry.schedule_buddy_intro)
    assert {"key", "outcome", "provider_ref"} <= params(OnboardingToolRegistry.resolve_operation)
    for name in ("get_record", "list_sessions", "list_communication_drafts", "pending_approvals", "participants_for",
                 "notify_approver"):
        assert callable(getattr(OnboardingToolRegistry, name))
    assert callable(OnboardingToolRegistry.from_dependencies)


def test_runtime_builder_used_by_the_orchestrators_worker(monkeypatch, tmp_path):
    """The worker builds this agent through build_context with HR_AGENT_ENV=development: the offline LLM
    must answer the welcome note, and HR_AGENT_PERSIST=1 must put every store under HR_AGENT_DATA_DIR."""
    monkeypatch.setenv("HR_AGENT_DATA_DIR", str(tmp_path))
    ctx, _ = build_context({"HR_AGENT_ENV": "development", "HR_AGENT_PERSIST": "1", "LLM_MODE": "offline"})
    assert (tmp_path / "onboarding.db").exists() and (tmp_path / "checkpoints.db").exists()
    assert "onboarding.welcome_note" in ctx.llm.inner.handlers


def test_orchestrated_sequence_through_public_entry_points_only():
    """offer_accepted -> case_status -> an HR decision through the HITL listener -> the decision
    run's checkpointed state -> joining_day's per-employee results: the shapes the adapter reads."""
    deps = build_fake_dependencies()
    deps.repository.buddy_candidates.append(BuddyCandidate(employee_id="EMP-TEST-050", full_name="Robin Park",
                                                           client_id=TEST_A, team="Platform"))
    ctx = AgentContext.build(agent_home=AGENT_HOME, llm=offline_llm(), connector=deps)
    agent = OnboardingAgent(ctx)
    ctx.hitl.subscribe(agent.handle_approval_decision)
    hr = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], client_id=TEST_A)

    _, summary = agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.OFFER_ACCEPTED, event_id="EVT-001"),
                                 actor=hr, instance="EVT-001")
    assert summary["stage"] == Stage.INDUCTION_AWAITING_APPROVAL and summary["pending_nodes"] == []
    status = agent.case_status(hr, "EMP-TEST-001")
    (item,) = [i for i in status["pending_approvals"] if i["proposal_type"] == "session_booking"]

    ctx.hitl.decide(item["id"], Decision.APPROVED, approver=hr.on_behalf_of, approver_roles=hr.roles, client_id=TEST_A)
    run = agent.workflow_state(workflow_id_for(WorkflowEvent.APPROVAL_DECIDED, TEST_A, item["id"]))
    assert run["stage"] == Stage.BUDDY_AWAITING_APPROVAL and run["meeting_type"] == "induction"

    response, summary = agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.JOINING_DAY,
                                                                as_of=date(2026, 12, 1)),
                                        actor=hr, instance="joining-day:2026-12-01")
    (action,) = [a for a in response.actions_taken if a.tool == "joining_trigger.run"]
    assert {"employee_id", "status"} <= set(action.detail["results"][0])
    assert summary["stage"] == Stage.WELCOME_AWAITING_APPROVAL


def test_a_tenant_without_welcome_configuration_ends_in_a_typed_stage_not_a_raw_key_error():
    """The orchestrator maps this stage (and the error type) to UC05_CLIENT_CONFIG_MISSING."""
    deps = build_fake_dependencies()
    ctx = AgentContext.build(agent_home=AGENT_HOME, llm=offline_llm(), connector=deps)
    agent = OnboardingAgent(ctx)
    test_b = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], client_id="CLIENT-TEST-B")
    agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.OFFER_ACCEPTED, event_id="EVT-010"),
                    actor=test_b, instance="EVT-010")
    response, summary = agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.JOINING_DAY,
                                                                as_of=date(2026, 12, 1)),
                                        actor=test_b, instance="joining-day:2026-12-01")
    assert summary["stage"] == Stage.WELCOME_NOT_CONFIGURED and summary["pending_nodes"] == []
    assert summary["errors"][-1]["error"] == "MissingClientConfigError"
    assert response.exceptions[0].code == "MISSING_DATA" and "CLIENT-TEST" not in response.exceptions[0].detail
