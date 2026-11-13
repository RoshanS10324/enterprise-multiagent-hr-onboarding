"""Phase 6: welcome communication drafting against the real HITLInbox approval mechanism.

Covers missing information, incorrect/unconfirmed dates, disabled languages, tenant isolation
and approval rejection.
"""

from datetime import datetime, timezone

import pytest

from hr_onboarding_common.llm import FakeLLM
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import Decision, HITLInbox, ReasonCode
from sub_agent.fakes import build_mock_onboarding, offline_llm, PersonalNoteOutput
from sub_agent.models import (CommunicationStatus, OnboardingSession, SessionStatus,
                              SessionType, UnsupportedLanguageError)
from sub_agent.services.welcome import (UnknownTemplateError, apply_decision,
                                        draft_welcome_communication)

ACME = "CLIENT-ACME"
GLOBEX = "CLIENT-GLOBEX"


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


def _draft(port, llm, hitl, audit, employee_id, **kwargs):
    kwargs.setdefault("template_id", "welcome")
    kwargs.setdefault("language", "en")
    kwargs.setdefault("workflow_instance", employee_id)
    return draft_welcome_communication(port, llm, hitl, audit, employee_id=employee_id, **kwargs)


# --------------------------------------------------------------------------- happy path

def test_draft_uses_authorized_facts_and_submits_for_approval(port, llm, hitl, audit):
    draft, item = _draft(port, llm, hitl, audit, "EMP-001")

    assert "Sara Al Marzooqi" in draft.body
    assert "Backend Engineer" in draft.body
    assert "2026-11-02" in draft.body
    assert "EMP-010" in draft.body  # buddy
    assert item.gate == "comms_approval" and item.status == Decision.PENDING
    assert draft.status == CommunicationStatus.DRAFT


def test_personal_note_is_appended_when_it_passes_content_checks(port, llm, hitl, audit):
    draft, _ = _draft(port, llm, hitl, audit, "EMP-001")
    assert "accomplish" in draft.body


# --------------------------------------------------------------------------- missing information

def test_unknown_employee_raises_key_error(port, llm, hitl, audit):
    with pytest.raises(KeyError):
        _draft(port, llm, hitl, audit, "EMP-DOES-NOT-EXIST")


def test_employee_without_buddy_has_no_buddy_line(port, llm, hitl, audit):
    draft, _ = _draft(port, llm, hitl, audit, "EMP-002")
    assert "buddy" not in draft.body.lower()


# --------------------------------------------------------------------------- incorrect / unconfirmed dates

def test_unconfirmed_session_is_never_stated_as_a_fact(port, llm, hitl, audit):
    port.create_session(OnboardingSession(employee_id="EMP-001", client_id=ACME,
                                          session_type=SessionType.ORIENTATION,
                                          scheduled_start=datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc),
                                          scheduled_end=datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)))
    # still just SCHEDULED, never confirmed
    draft, _ = _draft(port, llm, hitl, audit, "EMP-001")
    assert "orientation is confirmed" not in draft.body.lower()


def test_only_the_confirmed_session_date_is_stated(port, llm, hitl, audit):
    scheduled = port.create_session(OnboardingSession(
        employee_id="EMP-001", client_id=ACME, session_type=SessionType.ORIENTATION,
        scheduled_start=datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc),
        scheduled_end=datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)))
    port.update_session_status(scheduled.id, SessionStatus.CONFIRMED)

    draft, _ = _draft(port, llm, hitl, audit, "EMP-001")
    assert "orientation is confirmed for monday, 02 november 2026 at 09:00" in draft.body.lower()


# --------------------------------------------------------------------------- disabled languages

def test_disabled_language_is_rejected_before_any_draft_or_approval_is_created(port, llm, hitl, audit):
    with pytest.raises(UnsupportedLanguageError):
        _draft(port, llm, hitl, audit, "EMP-001", language="fr")

    assert port.list_communication_drafts("EMP-001") == []
    assert hitl.list(status=None) == []
    assert llm.calls == []  # rejected before the model was ever consulted


def test_template_not_available_in_requested_language_is_rejected(port, llm, hitl, audit):
    # Acme's "welcome" template only exists in English, even though Arabic is an enabled language
    with pytest.raises(UnknownTemplateError):
        _draft(port, llm, hitl, audit, "EMP-001", language="ar")


# --------------------------------------------------------------------------- tenant isolation

def test_each_client_gets_its_own_template_content(port, llm, hitl, audit):
    acme_draft, _ = _draft(port, llm, hitl, audit, "EMP-001")
    globex_draft, _ = _draft(port, llm, hitl, audit, "EMP-101")

    assert "Acme People Team" in acme_draft.subject
    assert "Globex HR" in globex_draft.subject
    assert acme_draft.subject != globex_draft.subject


def test_sessions_do_not_leak_across_employees(port, llm, hitl, audit):
    session = port.create_session(OnboardingSession(
        employee_id="EMP-001", client_id=ACME, session_type=SessionType.ORIENTATION,
        scheduled_start=datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc),
        scheduled_end=datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)))
    port.update_session_status(session.id, SessionStatus.CONFIRMED)

    other_draft, _ = _draft(port, llm, hitl, audit, "EMP-002")
    assert "confirmed for" not in other_draft.body.lower()


# --------------------------------------------------------------------------- content checks

def test_note_with_pay_figures_is_dropped_not_sent(port, hitl, audit):
    tainted = FakeLLM().on(
        "onboarding.welcome_note",
        lambda system, user, schema: PersonalNoteOutput(personal_note="Your starting salary is AED 20000/month."))

    draft, _ = _draft(port, tainted, hitl, audit, "EMP-001")

    assert "20000" not in draft.body
    assert audit.find("welcome.note_dropped")


def test_note_with_protected_attribute_terms_is_dropped(port, hitl, audit):
    tainted = FakeLLM().on(
        "onboarding.welcome_note",
        lambda system, user, schema: PersonalNoteOutput(personal_note="As a married Emirati, you'll fit right in."))

    draft, _ = _draft(port, tainted, hitl, audit, "EMP-001")

    assert "married" not in draft.body.lower() and "emirati" not in draft.body.lower()
    assert audit.find("welcome.note_dropped")


# --------------------------------------------------------------------------- approval rejection

def test_approval_rejection_updates_the_draft_status_via_the_real_hitl_mechanism(port, llm, hitl, audit):
    hitl.subscribe(lambda item: apply_decision(port, audit, item))
    draft, item = _draft(port, llm, hitl, audit, "EMP-001")

    decided = hitl.decide(item.id, Decision.REJECTED, approver="hr-admin-01", approver_roles=["hr_admin"],
                          client_id=ACME, reason_code=ReasonCode.TONE, note="Too informal for this client.")

    assert decided.status == Decision.REJECTED
    assert port.get_communication_draft(draft.id).status == CommunicationStatus.REJECTED
    assert audit.find("welcome.decision_applied")[0]["reason_code"] == ReasonCode.TONE


def test_approval_acceptance_marks_the_draft_approved(port, llm, hitl, audit):
    hitl.subscribe(lambda item: apply_decision(port, audit, item))
    draft, item = _draft(port, llm, hitl, audit, "EMP-001")

    hitl.decide(item.id, Decision.APPROVED, approver="hr-admin-01", approver_roles=["hr_admin"], client_id=ACME)

    assert port.get_communication_draft(draft.id).status == CommunicationStatus.APPROVED


def test_rejection_requires_a_reason_code(port, llm, hitl, audit):
    _, item = _draft(port, llm, hitl, audit, "EMP-001")
    with pytest.raises(Exception):
        hitl.decide(item.id, Decision.REJECTED, approver="hr-admin-01", approver_roles=["hr_admin"],
                   client_id=ACME)
