"""Tenant welcome configuration (ClientConfig): the seeded CLIENT-TEST-A configuration, typed and
sanitized errors for a missing or unusable one, and tenant isolation of configuration."""

import re
from datetime import date

import pytest
from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.contracts import Actor
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import HITLInbox

from sub_agent.agent import (
    AGENT_HOME,
    OnboardingAgent,
    OnboardingWorkflowInput,
    Stage,
    WorkflowEvent,
)
from sub_agent.fakes import (
    build_fake_dependencies,
    offline_llm,
    synthetic_onboarding_data,
)
from sub_agent.models import (
    BrandingConfig,
    ClientConfig,
    InvalidClientConfigError,
    MessageTemplate,
    MissingClientConfigError,
    OnboardingRecord,
)
from sub_agent.services.welcome import (
    TEMPLATE_FIELDS,
    draft_welcome_communication,
    validate_template,
)
from sub_agent.tool_registry import OnboardingToolRegistry, TenantAuthorizationError

TEST_A, TEST_B = "CLIENT-TEST-A", "CLIENT-TEST-B"


def _actor(client_id):
    return Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], client_id=client_id)


def _record(employee_id, client_id):
    return OnboardingRecord(employee_id=employee_id, full_name="Alex Morgan", email="alex.morgan@example.com",
                            role_title="Software Engineer", department="Engineering", team="Platform",
                            team_lead_id="TL-TEST-001", client_id=client_id, start_date=date(2026, 12, 1))


def _draft(deps, employee_id):
    return draft_welcome_communication(deps.repository, offline_llm(), HITLInbox(), AuditLog(), employee_id=employee_id,
                                       template_id="welcome", language="en", workflow_instance=employee_id)


def test_the_test_tenant_is_seeded_with_a_complete_valid_configuration():
    (config,) = [c for c in synthetic_onboarding_data()[1] if c.client_id == TEST_A]
    assert config.default_language in config.enabled_languages and config.branding.sender_name
    validate_template(config.templates["welcome"], template_id="welcome")  # no exception


def test_a_valid_configuration_produces_the_expected_welcome_draft():
    deps = build_fake_dependencies()
    deps.repository.create_onboarding_case(_record("EMP-TEST-001", TEST_A))
    draft, item = _draft(deps, "EMP-TEST-001")
    assert draft.subject == "Welcome to Test Client A People Team, Alex Morgan!"
    assert draft.body.startswith("Dear Alex Morgan,\n\nWelcome aboard as Software Engineer, starting 2026-12-01.")
    assert item.client_id == TEST_A and item.proposal_type == "welcome_message"


def test_a_missing_configuration_is_a_typed_sanitized_error_and_drafts_nothing():
    deps = build_fake_dependencies()
    deps.repository.create_onboarding_case(_record("EMP-TEST-010", TEST_B))  # CLIENT-TEST-B is not configured
    with pytest.raises(MissingClientConfigError) as err:
        _draft(deps, "EMP-TEST-010")
    assert isinstance(err.value, KeyError)  # existing "not found" handling still applies
    assert str(err.value) == "no welcome configuration (languages, branding, templates) is set up for this client"
    assert deps.repository.list_communication_drafts("EMP-TEST-010") == []


@pytest.mark.parametrize("subject, body, message", [
    ("Hi {full_name}", "Your salary is {salary}", "placeholder(s) the drafting code does not fill: salary"),
    ("Hi {}", "Body", "placeholder(s) the drafting code does not fill: {}"),
    ("Hi {full_name", "Body", "malformed placeholder braces"),
    ("   ", "Body", "empty subject or body"),
])
def test_an_invalid_template_is_refused_before_anything_is_drafted(subject, body, message):
    deps = build_fake_dependencies()
    deps.repository.client_configs[TEST_B] = ClientConfig(
        client_id=TEST_B, name="B", branding=BrandingConfig(sender_name="B"),
        templates={"welcome": MessageTemplate(template_id="welcome", language="en", subject=subject, body=body)})
    deps.repository.create_onboarding_case(_record("EMP-TEST-010", TEST_B))
    with pytest.raises(InvalidClientConfigError, match=re.escape(message)):
        _draft(deps, "EMP-TEST-010")
    assert deps.repository.list_communication_drafts("EMP-TEST-010") == []
    assert set(TEMPLATE_FIELDS) == {"full_name", "role_title", "start_date", "sender_name"}


def test_another_tenants_configuration_is_never_used_or_readable():
    deps = build_fake_dependencies()
    deps.repository.create_onboarding_case(_record("EMP-TEST-010", TEST_B))
    with pytest.raises(MissingClientConfigError):  # TEST-A's template is not borrowed for a TEST-B employee
        _draft(deps, "EMP-TEST-010")
    ctx = AgentContext.build(agent_home=AGENT_HOME, llm=offline_llm(), connector=deps)
    reg = OnboardingToolRegistry.from_dependencies(deps, llm=ctx.llm, policy=OnboardingAgent(ctx).policy,
                                                   hitl=ctx.hitl, audit=ctx.audit, actor=_actor(TEST_B))
    with pytest.raises(TenantAuthorizationError):
        reg.get_client_config(TEST_A)


def test_the_workflow_reports_an_unconfigured_tenant_and_leaves_the_run_retryable():
    deps = build_fake_dependencies()
    ctx = AgentContext.build(agent_home=AGENT_HOME, llm=offline_llm(), connector=deps)
    agent = OnboardingAgent(ctx)
    agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.OFFER_ACCEPTED, event_id="EVT-010"),
                    actor=_actor(TEST_B), instance="EVT-010")
    response, summary = agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.DRAFT_WELCOME,
                                                                employee_id="EMP-TEST-010"),
                                        actor=_actor(TEST_B), instance="EMP-TEST-010")
    assert summary["stage"] == Stage.WELCOME_NOT_CONFIGURED
    assert response.exceptions[0].detail.startswith("welcome not drafted: no welcome configuration")
    assert "'" not in response.exceptions[0].detail  # no KeyError repr

    deps.repository.client_configs[TEST_B] = ClientConfig(  # HR configures the tenant; the joining-day run now drafts
        client_id=TEST_B, name="B", branding=BrandingConfig(sender_name="B HR"),
        templates={"welcome": MessageTemplate(template_id="welcome", language="en", subject="Welcome {full_name}",
                                              body="Hello {full_name}, you start {start_date}.")})
    _, summary = agent.run_event(OnboardingWorkflowInput(event=WorkflowEvent.JOINING_DAY, as_of=date(2026, 12, 1)),
                                 actor=_actor(TEST_B), instance="joining-day:2026-12-01")
    assert summary["stage"] == Stage.WELCOME_AWAITING_APPROVAL


def test_existing_tenants_are_unchanged():
    deps = build_fake_dependencies()
    draft, _ = _draft(deps, "EMP-001")  # CLIENT-ACME, seeded as before
    assert draft.subject == "Welcome to Acme People Team, Sara Al Marzooqi!"
