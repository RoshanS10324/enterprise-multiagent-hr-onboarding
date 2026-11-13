from datetime import datetime

import pytest
from pydantic import ValidationError

from sub_agent.models import (
    AuditEntry,
    ActorType,
    CaseStatus,
    FormStatus,
    WorkLocationType,
)

from factories import make_case, make_form, utc_timestamp


def test_valid_form_builds():
    form = make_form()
    assert form.status == FormStatus.DRAFT
    assert form.version == 1


def test_valid_case_builds():
    case = make_case()
    assert case.status == CaseStatus.PLAN_PENDING


def test_approved_form_without_approver_is_rejected():
    with pytest.raises(ValidationError):
        make_form(status=FormStatus.APPROVED)


def test_client_site_form_without_site_id_is_rejected():
    with pytest.raises(ValidationError):
        make_form(work_location_type=WorkLocationType.CLIENT_SITE)


def test_client_site_form_with_site_id_is_accepted():
    form = make_form(
        work_location_type=WorkLocationType.CLIENT_SITE,
        client_site_id="site-001",
    )
    assert form.client_site_id == "site-001"


def test_version_zero_is_rejected():
    with pytest.raises(ValidationError):
        make_form(version=0)


def test_approved_form_cannot_be_mutated():
    form = make_form(
        status=FormStatus.APPROVED,
        approved_by="hr-officer-001",
        approved_at=utc_timestamp(),
    )
    with pytest.raises(ValidationError):
        form.designation = "Different Title"


def test_approve_returns_new_form_and_leaves_original_unchanged():
    draft = make_form()
    approved = draft.approve(approved_by="hr-officer-001", approved_at=utc_timestamp())

    assert approved is not draft
    assert approved.status == FormStatus.APPROVED
    assert approved.approved_by == "hr-officer-001"
    assert draft.status == FormStatus.DRAFT
    assert draft.approved_by is None


def test_approve_on_already_approved_form_raises_value_error():
    form = make_form(
        status=FormStatus.APPROVED,
        approved_by="hr-officer-001",
        approved_at=utc_timestamp(),
    )
    with pytest.raises(ValueError):
        form.approve(approved_by="someone-else", approved_at=utc_timestamp())


def test_submit_for_approval_from_draft():
    draft = make_form()
    submitted = draft.submit_for_approval()
    assert submitted.status == FormStatus.AWAITING_APPROVAL
    assert draft.status == FormStatus.DRAFT


def test_supersede_only_from_approved():
    approved = make_form(
        status=FormStatus.APPROVED,
        approved_by="hr-officer-001",
        approved_at=utc_timestamp(),
    )
    superseded = approved.supersede()
    assert superseded.status == FormStatus.SUPERSEDED

    with pytest.raises(ValueError):
        make_form().supersede()


def test_naive_approved_at_is_rejected():
    with pytest.raises(ValidationError):
        make_form(
            status=FormStatus.APPROVED,
            approved_by="hr-officer-001",
            approved_at=datetime(2026, 1, 5, 9, 0, 0),
        )


def test_form_dump_does_not_contain_salary():
    secret_text = "SECRET_SALARY_VALUE_12345"
    form = make_form(salary_and_terms=secret_text)
    assert secret_text not in str(form.model_dump())
    assert secret_text not in form.model_dump_json()
    assert secret_text not in repr(form)


def test_salary_get_secret_value_returns_exact_original():
    secret_text = "Base: 100000 SYNTHETIC/yr; benefits: standard"
    form = make_form(salary_and_terms=secret_text)
    assert form.salary_and_terms.get_secret_value() == secret_text


def test_audit_entry_cannot_be_mutated():
    entry = AuditEntry(
        case_id="case-001",
        actor_type=ActorType.AGENT,
        actor_id="agent-uc01",
        action="CASE_OPENED",
        reference="ref-001",
    )
    with pytest.raises(ValidationError):
        entry.action = "SOMETHING_ELSE"


def test_audit_entry_accepts_request_id_without_case_id():
    entry = AuditEntry(
        request_id="req-001",
        actor_type=ActorType.AGENT,
        actor_id="agent-uc01",
        action="START_GATE_EVALUATED",
        reference="req-001",
    )
    assert entry.case_id is None
    assert entry.request_id == "req-001"


def test_audit_entry_requires_case_id_or_request_id():
    with pytest.raises(ValidationError):
        AuditEntry(
            actor_type=ActorType.AGENT,
            actor_id="agent-uc01",
            action="CASE_OPENED",
            reference="ref-001",
        )


def test_case_invalid_status_assignment_raises():
    case = make_case()
    with pytest.raises(ValidationError):
        case.status = "NOT_A_REAL_STATUS"


def test_default_case_status_is_plan_pending():
    case = make_case()
    assert case.status == CaseStatus.PLAN_PENDING
