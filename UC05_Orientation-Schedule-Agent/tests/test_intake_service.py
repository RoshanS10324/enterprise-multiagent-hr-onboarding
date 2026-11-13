"""UC steps 1-2: offer-acceptance email trigger + employee-detail retrieval.

Covers functional creation, event-level and employee-level duplicate prevention, malformed/
incomplete-data flagging (never inventing a missing field), email-service failure, and tenant
checks -- both at the service layer and through the real OnboardingToolRegistry.
"""

from datetime import datetime, timezone

import pytest

from connectors.email.base import EmailServiceError, OfferAcceptanceEvent
from context_layer.knowledge.policy_index import PolicyKnowledgeBase
from hr_onboarding_common.contracts import Actor, Purpose
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import HITLInbox
from hr_onboarding_common.platform.policy import PolicyStore
from sub_agent.agent import AGENT_HOME
from sub_agent.fakes import (FAILING_EVENT_ID, build_mock_email_adapter, build_mock_employee_directory,
                             build_mock_onboarding, FakeCalendar, offline_llm)
from sub_agent.models import TenantMismatchError
from sub_agent.services.intake import (IncompleteEmployeeDataError, MalformedOfferAcceptanceEmailError,
                                       process_offer_acceptance_event)
from sub_agent.tool_registry import ConnectorNotConfiguredError, OnboardingToolRegistry

CLIENT_A = "CLIENT-TEST-A"
CLIENT_B = "CLIENT-TEST-B"


@pytest.fixture
def port():
    return build_mock_onboarding()


@pytest.fixture
def email_port():
    return build_mock_email_adapter()


@pytest.fixture
def directory():
    return build_mock_employee_directory()


@pytest.fixture
def audit():
    return AuditLog()


# --------------------------------------------------------------------------- functional

def test_valid_offer_acceptance_creates_the_onboarding_case(port, email_port, directory, audit):
    case = process_offer_acceptance_event(port, email_port, directory, audit, event_id="EVT-001")

    assert case.employee_id == "EMP-TEST-001"
    assert case.full_name == "Alex Morgan"
    assert case.email == "alex.morgan@example.com"
    assert case.role_title == "Software Engineer"
    assert case.department == "Engineering"
    assert case.team == "Platform"
    assert case.team_lead_id == "TL-TEST-001"
    assert case.client_id == CLIENT_A
    assert audit.find("intake.case_created")


def test_created_case_is_queryable_afterward(port, email_port, directory, audit):
    process_offer_acceptance_event(port, email_port, directory, audit, event_id="EVT-001")
    assert port.get_onboarding_record("EMP-TEST-001").full_name == "Alex Morgan"


# --------------------------------------------------------------------------- idempotency / duplicate prevention

def test_reprocessing_the_same_event_does_not_duplicate(port, email_port, directory, audit):
    first = process_offer_acceptance_event(port, email_port, directory, audit, event_id="EVT-001")
    second = process_offer_acceptance_event(port, email_port, directory, audit, event_id="EVT-001")

    assert first == second
    assert len(audit.find("intake.case_created")) == 1
    assert len(audit.find("intake.duplicate_event_skipped")) == 1
    assert len(port.list_onboarding_records(client_id=CLIENT_A)) == 1


def test_a_different_event_for_an_already_onboarded_employee_is_not_duplicated(port, email_port, directory, audit):
    process_offer_acceptance_event(port, email_port, directory, audit, event_id="EVT-001")

    # a second, differently-id'd email somehow referencing the same employee (e.g. a forward)
    email_port.events["EVT-001-FORWARD"] = OfferAcceptanceEvent(
        event_id="EVT-001-FORWARD", employee_id="EMP-TEST-001", received_at=datetime.now(timezone.utc),
        raw_subject="FWD: Offer accepted - Alex Morgan", raw_body="forwarded")

    result = process_offer_acceptance_event(port, email_port, directory, audit, event_id="EVT-001-FORWARD")

    assert result.employee_id == "EMP-TEST-001"
    assert audit.find("intake.duplicate_case_skipped")
    assert len(port.list_onboarding_records(client_id=CLIENT_A)) == 1
    assert email_port.is_processed("EVT-001-FORWARD")  # marked so it isn't retried forever


# --------------------------------------------------------------------------- validation / edge cases

def test_incomplete_directory_record_is_flagged_never_invented(port, email_port, directory, audit):
    with pytest.raises(IncompleteEmployeeDataError, match="team"):
        process_offer_acceptance_event(port, email_port, directory, audit, event_id="EVT-002")

    assert port.list_onboarding_records(client_id=CLIENT_A) == []
    flagged = audit.find("intake.flagged_missing_data")
    assert flagged and set(flagged[0]["missing_fields"]) == {"team", "team_lead_id"}


def test_malformed_email_is_flagged_without_creating_anything(port, email_port, directory, audit):
    before = {r.employee_id for r in port.list_onboarding_records()}
    with pytest.raises(MalformedOfferAcceptanceEmailError):
        process_offer_acceptance_event(port, email_port, directory, audit, event_id="EVT-MALFORMED")

    assert {r.employee_id for r in port.list_onboarding_records()} == before
    assert audit.find("intake.malformed_email")


def test_employee_reference_not_in_the_directory_raises_key_error(port, email_port, directory, audit):
    email_port.events["EVT-UNKNOWN"] = OfferAcceptanceEvent(
        event_id="EVT-UNKNOWN", employee_id="EMP-NOT-IN-DIRECTORY", received_at=datetime.now(timezone.utc),
        raw_subject="Offer accepted", raw_body="...")
    before = {r.employee_id for r in port.list_onboarding_records()}

    with pytest.raises(KeyError):
        process_offer_acceptance_event(port, email_port, directory, audit, event_id="EVT-UNKNOWN")
    assert {r.employee_id for r in port.list_onboarding_records()} == before


# --------------------------------------------------------------------------- failure handling

def test_email_service_failure_propagates_and_creates_nothing(port, email_port, directory, audit):
    before = {r.employee_id for r in port.list_onboarding_records()}
    with pytest.raises(EmailServiceError):
        process_offer_acceptance_event(port, email_port, directory, audit, event_id=FAILING_EVENT_ID)

    assert {r.employee_id for r in port.list_onboarding_records()} == before
    assert audit.find("intake.case_created") == []


# --------------------------------------------------------------------------- tenant / security

def test_cross_tenant_email_is_rejected_when_an_expected_client_is_set(port, email_port, directory, audit):
    with pytest.raises(TenantMismatchError):
        process_offer_acceptance_event(port, email_port, directory, audit, event_id="EVT-010",
                                       expected_client_id=CLIENT_A)
    assert port.list_onboarding_records(client_id=CLIENT_B) == []


def test_unscoped_intake_allows_any_tenant(port, email_port, directory, audit):
    case = process_offer_acceptance_event(port, email_port, directory, audit, event_id="EVT-010",
                                          expected_client_id=None)
    assert case.client_id == CLIENT_B


# --------------------------------------------------------------------------- through the real registry

def _registry(client_id: str) -> OnboardingToolRegistry:
    actor = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
                 client_id=client_id)
    return OnboardingToolRegistry(port=build_mock_onboarding(), calendar=FakeCalendar(), kb=PolicyKnowledgeBase(),
                                  llm=offline_llm(), policy=PolicyStore(AGENT_HOME / "guardrails").for_agent(
                                      "employee-onboarding"), hitl=HITLInbox(), audit=AuditLog(), actor=actor,
                                  email_port=build_mock_email_adapter(), directory=build_mock_employee_directory())


def test_registry_without_an_email_connector_fails_closed_instead_of_using_a_fake():
    actor = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
                  client_id=CLIENT_A)
    reg = OnboardingToolRegistry(port=build_mock_onboarding(), calendar=FakeCalendar(), kb=PolicyKnowledgeBase(),
                                 llm=offline_llm(), policy=PolicyStore(AGENT_HOME / "guardrails").for_agent(
                                     "employee-onboarding"), hitl=HITLInbox(), audit=AuditLog(), actor=actor)
    with pytest.raises(ConnectorNotConfiguredError):
        reg.process_offer_acceptance_event("EVT-001")
    assert reg.audit.find("tool.call") == []  # nothing ran


def test_registry_creates_a_case_for_the_actors_own_client():
    reg = _registry(CLIENT_A)
    case = reg.process_offer_acceptance_event("EVT-001")
    assert case.client_id == CLIENT_A
    assert {e["tool"] for e in reg.audit.find("tool.call")} >= {"intake.process_offer_acceptance_event"}


def test_registry_rejects_another_clients_email():
    reg = _registry(CLIENT_A)
    with pytest.raises(TenantMismatchError):
        reg.process_offer_acceptance_event("EVT-010")  # EMP-TEST-010 is CLIENT-TEST-B
