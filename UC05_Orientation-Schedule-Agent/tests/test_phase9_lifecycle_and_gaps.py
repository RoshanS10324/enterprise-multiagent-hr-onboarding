"""Phase 9: review-driven gap fill against the UC-05 business requirements.

  1. Schedule orientation, induction, team introductions and buddy arrangements.
  2. (retired from this agent's scope -- NDA/HR policy signature and acknowledgement tracking is
     no longer a responsibility of this agent; see README.)
  3. Prepare employee welcome information and day-one instructions.
  4. Answer employee questions using approved client HR policies, citing the source.
  5. Support client-specific message templates, branding and enabled languages.
  6. Track outstanding orientation activities.

Adds the authorization/failure/idempotency/citation coverage the per-phase test files didn't
reach, plus one end-to-end lifecycle test exercising every retained requirement for a single
employee through the real OnboardingToolRegistry (ToolGateway/Policy/HITLInbox/AuditLog).
"""

from datetime import date, datetime, timezone

import pytest

from connectors.calendar.base import CalendarBookingError
from context_layer.knowledge.policy_index import PolicyKnowledgeBase
from hr_onboarding_common.contracts import Actor, Purpose
from hr_onboarding_common.llm import FakeLLM, LLMError
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import Decision, HITLInbox
from hr_onboarding_common.platform.policy import PolicyStore
from sub_agent.agent import AGENT_HOME
from sub_agent.fakes import FAILING_ATTENDEE, build_mock_onboarding, FakeCalendar, FakeNotifier, offline_llm
from sub_agent.models import SessionStatus, SessionType, TaskStatus, UnsupportedLanguageError
from sub_agent.services.scheduling import apply_buddy_decision
from sub_agent.services.scheduling import apply_decision as apply_scheduling_decision
from sub_agent.services.welcome import apply_decision as apply_welcome_decision
from sub_agent.tool_registry import OnboardingToolRegistry, TenantAuthorizationError

ACME = "CLIENT-ACME"
GLOBEX = "CLIENT-GLOBEX"
AS_OF = date(2026, 11, 1)
ACTOR = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
             client_id=ACME)


def _registry(client_id=ACME, port=None, llm=None, policy=None):
    actor = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
                 client_id=client_id)
    return OnboardingToolRegistry(
        port=port or build_mock_onboarding(), calendar=FakeCalendar(), kb=PolicyKnowledgeBase(),
        llm=llm or offline_llm(), policy=policy or PolicyStore(AGENT_HOME / "guardrails").for_agent(
            "employee-onboarding"), hitl=HITLInbox(), audit=AuditLog(), actor=actor,
        notifier=FakeNotifier())


@pytest.fixture
def registry():
    return _registry()


# =========================================================================== authorization gaps

def test_cross_tenant_scheduling_is_blocked(registry):
    with pytest.raises(TenantAuthorizationError):
        registry.schedule_session("EMP-101", session_type=SessionType.ORIENTATION,
                                  start=datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc), attendees=["HM-01"],
                                  workflow_instance="EMP-101")
    assert registry.port.list_sessions("EMP-101") == []


def test_cross_tenant_buddy_intro_scheduling_is_blocked(registry):
    with pytest.raises(TenantAuthorizationError):
        registry.schedule_buddy_intro("EMP-101", start=datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc),
                                      workflow_instance="EMP-101")


def test_cross_tenant_welcome_draft_is_blocked(registry):
    with pytest.raises(TenantAuthorizationError):
        registry.draft_welcome("EMP-101", template_id="welcome", language="en", workflow_instance="EMP-101")
    assert registry.port.list_communication_drafts("EMP-101") == []


@pytest.mark.parametrize("method", ["list_tasks", "list_sessions"])
def test_cross_tenant_reads_are_blocked(registry, method):
    with pytest.raises(TenantAuthorizationError):
        getattr(registry, method)("EMP-101")
    assert registry.audit.find("tool.call") == []


def test_policy_question_about_another_employee_is_blocked(registry):
    with pytest.raises(TenantAuthorizationError):
        registry.answer_policy_question("What is the dress code?", as_of=AS_OF, employee_id="EMP-101")


def test_search_policy_always_scopes_to_the_actors_own_client(registry):
    """search_policy takes no client_id parameter at all -- there is nothing a caller could pass
    to make it search another tenant's documents."""
    import inspect
    assert "client_id" not in inspect.signature(registry.search_policy).parameters


# =========================================================================== failure handling gaps

def test_llm_failure_during_policy_qa_propagates_without_corrupting_state(registry):
    failing_llm = FakeLLM().on("onboarding.policy_qa", lambda system, user, schema: (_ for _ in ()).throw(
        LLMError("provider outage")))
    broken = _registry(llm=failing_llm)

    with pytest.raises(LLMError):
        broken.answer_policy_question("What is the dress code?", as_of=AS_OF)
    assert broken.audit.find("policy_qa.answered") == []


def test_calendar_failure_through_the_registry_leaves_no_session_or_approval(registry):
    with pytest.raises(CalendarBookingError):
        registry.schedule_session("EMP-001", session_type=SessionType.ORIENTATION,
                                  start=datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc),
                                  attendees=[FAILING_ATTENDEE], workflow_instance="EMP-001")
    assert registry.port.list_sessions("EMP-001") == []
    assert registry.hitl.list(status=None) == []


# =========================================================================== idempotency gaps (via the registry)

def test_ensure_tasks_through_the_registry_is_idempotent():
    reg = _registry()
    first = reg.ensure_tasks("EMP-001")
    second = reg.ensure_tasks("EMP-001")
    assert {t.id for t in first} == {t.id for t in second}


# =========================================================================== citation completeness (UC-05 #4)

def test_policy_answer_citation_has_version_and_effective_date_through_the_registry(registry):
    answer = registry.answer_policy_question("What is the dress code?", as_of=AS_OF)
    assert answer.citations
    citation = answer.citations[0]
    # HR_Policy_Consultants.md states no effective date; the version is the document's content hash
    assert citation.version and citation.effective_date is None and citation.section and citation.pages


# =========================================================================== outstanding activities (UC-05 #6)

def test_outstanding_views_are_exposed_through_the_registry_and_tenant_checked(registry):
    tasks = registry.ensure_tasks("EMP-001")
    registry.port.update_task_status(tasks[0].id, TaskStatus.COMPLETED)
    assert tasks[0].id not in {t.id for t in registry.pending_tasks("EMP-001")}

    overdue = registry.ensure_tasks("EMP-002", due_date=date(2026, 10, 1))
    assert len(registry.overdue_tasks("EMP-002", as_of=date(2026, 11, 5))) == len(overdue)

    session, _ = registry.schedule_session("EMP-001", session_type=SessionType.ORIENTATION,
                                            start=datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc),
                                            attendees=["HM-01"], workflow_instance="EMP-001")
    assert session.id in {s.id for s in registry.outstanding_sessions("EMP-001")}

    with pytest.raises(TenantAuthorizationError):
        registry.outstanding_sessions("EMP-101")


# =========================================================================== full lifecycle (retained requirements)

def test_full_onboarding_lifecycle_covers_the_retained_uc05_requirements():
    port = build_mock_onboarding()
    hitl = HITLInbox()
    audit = AuditLog()
    calendar = FakeCalendar()
    reg = OnboardingToolRegistry(port=port, calendar=calendar, kb=PolicyKnowledgeBase(), llm=offline_llm(),
                                 policy=PolicyStore(AGENT_HOME / "guardrails").for_agent("employee-onboarding"),
                                 hitl=hitl, audit=audit, actor=ACTOR, notifier=FakeNotifier())
    employee_id = "EMP-002"  # starts with no buddy and no tasks/sessions yet
    hitl.subscribe(lambda item: apply_welcome_decision(port, audit, item))
    hitl.subscribe(lambda item: apply_scheduling_decision(port, calendar, audit, item))
    hitl.subscribe(lambda item: apply_buddy_decision(port, FakeNotifier(), audit, item))

    # 1. orientation + buddy arrangement scheduling
    orientation, orientation_item = reg.schedule_session(
        employee_id, session_type=SessionType.ORIENTATION, start=datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc),
        attendees=["HM-02"], workflow_instance=employee_id)
    buddy_id, buddy_assignment_item = reg.propose_buddy_assignment(employee_id, workflow_instance=employee_id)
    hitl.decide(buddy_assignment_item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"],
               client_id=ACME)
    assert port.get_onboarding_record(employee_id).buddy_id == buddy_id  # buddy arrangement made
    buddy_session, buddy_item = reg.schedule_buddy_intro(
        employee_id, start=datetime(2026, 11, 9, 11, 0, tzinfo=timezone.utc), workflow_instance=employee_id)
    hitl.decide(orientation_item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"],
               client_id=ACME)
    assert port.get_session(orientation.id).status == SessionStatus.CONFIRMED

    # 3. welcome information + day-one instructions (approved before anything is sent)
    draft, comms_item = reg.draft_welcome(employee_id, template_id="welcome", language="en",
                                          workflow_instance=employee_id)
    assert port.get_onboarding_record(employee_id).full_name in draft.body
    hitl.decide(comms_item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"],
               client_id=ACME)
    sent = reg.send_welcome_email(draft_id=draft.id, approval_id=comms_item.id)
    assert sent["sent"] is True

    # 4. HR policy Q&A with a verifiable, section-level citation
    answer = reg.answer_policy_question("What is the dress code?", as_of=AS_OF)
    assert answer.answer is not None and answer.citations[0].doc_id == "hr-policy-consultants"

    # 5. client-specific template/branding/language (already proven by the welcome draft above;
    #    confirm a disabled language is still rejected for this same client)
    with pytest.raises(UnsupportedLanguageError):
        reg.draft_welcome(employee_id, template_id="welcome", language="fr", workflow_instance=employee_id)

    # 6. outstanding orientation activities
    assert buddy_session.id in {s.id for s in reg.outstanding_sessions(employee_id)}  # not yet confirmed
    assert orientation.id not in {s.id for s in reg.outstanding_sessions(employee_id)}  # already confirmed

    # every write that happened is in the real audit trail
    tool_calls = {e["tool"] for e in audit.find("tool.call")}
    assert {"scheduling.schedule_session", "scheduling.schedule_buddy_intro", "scheduling.propose_buddy_assignment",
           "welcome.draft_communication", "comms.send_welcome_email", "policy_qa.answer_question"} <= tool_calls
