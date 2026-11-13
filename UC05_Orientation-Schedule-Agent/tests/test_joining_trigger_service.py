"""UC step 9 trigger: draft the welcome communication on an employee's joining date, exactly
once, skipping cancelled/withdrawn cases.
"""

from datetime import date

import pytest

from context_layer.knowledge.policy_index import PolicyKnowledgeBase
from hr_onboarding_common.contracts import Actor, Purpose
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import HITLInbox
from hr_onboarding_common.platform.policy import PolicyStore
from sub_agent.agent import AGENT_HOME
from sub_agent.fakes import build_mock_onboarding, FakeCalendar, offline_llm
from sub_agent.models import CaseStatus, OnboardingRecord
from sub_agent.services.joining_trigger import run_joining_day_trigger
from sub_agent.tool_registry import OnboardingToolRegistry

ACME = "CLIENT-ACME"
GLOBEX = "CLIENT-GLOBEX"
JOINING_DATE = date(2026, 11, 2)  # matches EMP-001's seeded start_date
ACTOR = Actor(on_behalf_of="user:hr-admin-01", roles=["hr_admin"], purpose_of_use=Purpose.ONBOARDING,
             client_id=ACME)


@pytest.fixture
def port():
    return build_mock_onboarding()


@pytest.fixture
def llm():
    return offline_llm()


@pytest.fixture
def hitl():
    return HITLInbox()


@pytest.fixture
def audit():
    return AuditLog()


def test_drafts_welcome_for_an_employee_joining_today(port, llm, hitl, audit):
    results = run_joining_day_trigger(port, llm, hitl, audit, client_id=ACME, as_of=JOINING_DATE)

    drafted = [r for r in results if r["employee_id"] == "EMP-001"]
    assert drafted and drafted[0]["status"] == "drafted"
    assert port.list_communication_drafts("EMP-001")


def test_employee_with_a_different_joining_date_is_not_processed(port, llm, hitl, audit):
    results = run_joining_day_trigger(port, llm, hitl, audit, client_id=ACME, as_of=date(2099, 1, 1))
    assert results == []


def test_running_twice_for_the_same_date_does_not_create_a_second_draft(port, llm, hitl, audit):
    run_joining_day_trigger(port, llm, hitl, audit, client_id=ACME, as_of=JOINING_DATE)
    second = run_joining_day_trigger(port, llm, hitl, audit, client_id=ACME, as_of=JOINING_DATE)

    skipped = [r for r in second if r["employee_id"] == "EMP-001"]
    assert skipped and skipped[0]["status"] == "skipped_already_drafted"
    assert len(port.list_communication_drafts("EMP-001")) == 1


def test_cancelled_case_is_skipped_not_welcomed(port, llm, hitl, audit):
    port.update_case_status("EMP-001", CaseStatus.CANCELLED)
    results = run_joining_day_trigger(port, llm, hitl, audit, client_id=ACME, as_of=JOINING_DATE)

    skipped = [r for r in results if r["employee_id"] == "EMP-001"]
    assert skipped and skipped[0]["status"] == "skipped_inactive"
    assert port.list_communication_drafts("EMP-001") == []
    assert audit.find("joining_trigger.skipped_inactive")


def test_withdrawn_case_is_skipped_not_welcomed(port, llm, hitl, audit):
    port.update_case_status("EMP-001", CaseStatus.WITHDRAWN)
    results = run_joining_day_trigger(port, llm, hitl, audit, client_id=ACME, as_of=JOINING_DATE)
    assert [r for r in results if r["employee_id"] == "EMP-001"][0]["status"] == "skipped_inactive"


def test_multiple_employees_joining_the_same_day_are_all_processed(port, llm, hitl, audit):
    port.create_onboarding_case(OnboardingRecord(
        employee_id="EMP-TEST-JOIN", full_name="Jordan Smith", email="jordan.smith@example.com",
        role_title="Analyst", department="Finance", team="FP&A", team_lead_id="TL-FIN-001",
        client_id=ACME, start_date=JOINING_DATE))

    results = run_joining_day_trigger(port, llm, hitl, audit, client_id=ACME, as_of=JOINING_DATE)

    drafted_ids = {r["employee_id"] for r in results if r["status"] == "drafted"}
    assert {"EMP-001", "EMP-TEST-JOIN"} <= drafted_ids


# --------------------------------------------------------------------------- through the real registry

def test_registry_trigger_is_scoped_to_the_actors_own_client():
    port = build_mock_onboarding()
    reg = OnboardingToolRegistry(port=port, calendar=FakeCalendar(), kb=PolicyKnowledgeBase(), llm=offline_llm(),
                                 policy=PolicyStore(AGENT_HOME / "guardrails").for_agent("employee-onboarding"),
                                 hitl=HITLInbox(), audit=AuditLog(), actor=ACTOR)

    results = reg.run_joining_day_trigger(as_of=JOINING_DATE)

    assert {r["employee_id"] for r in results} == {"EMP-001"}  # EMP-101 (GLOBEX) is never considered
    assert {e["tool"] for e in reg.audit.find("tool.call")} >= {"joining_trigger.run"}
