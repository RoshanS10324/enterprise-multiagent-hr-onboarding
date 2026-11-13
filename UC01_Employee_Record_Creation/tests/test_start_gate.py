from sub_agent.adapters.memory import InMemoryAuditLog
from sub_agent.models import FormStatus
from sub_agent.start_gate import StartDecision, StartRequest, evaluate_start_gate

from factories import make_form, utc_timestamp

SECRET = "SECRET_SALARY_VALUE_99999"
CANDIDATE_NAME = "Synthetic Candidate Name"
CANDIDATE_EMAIL = "synthetic.candidate@example.test"


def _approved_form(**overrides):
    defaults = dict(
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
        salary_and_terms=SECRET,
    )
    defaults.update(overrides)
    form = make_form(**defaults)
    return form.approve(approved_by="hr-officer-001", approved_at=utc_timestamp())


def _assert_single_clean_audit_entry(audit_log, request_id):
    entries = audit_log.entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.request_id == request_id
    dumped = str(entry.model_dump())
    assert CANDIDATE_EMAIL not in dumped
    assert CANDIDATE_NAME not in dumped
    assert SECRET not in dumped
    return entry


def test_approved_form_and_accepted_offer_starts():
    audit_log = InMemoryAuditLog()
    form = _approved_form()
    request = StartRequest(
        request_id="req-001",
        client_id=form.client_id,
        candidate_email=CANDIDATE_EMAIL,
        offer_accepted=True,
        form=form,
    )
    result = evaluate_start_gate(request, audit_log)

    assert result.decision == StartDecision.STARTED
    assert result.reasons == []
    assert result.may_contact_candidate is False
    _assert_single_clean_audit_entry(audit_log, "req-001")


def test_offer_not_accepted_is_held():
    audit_log = InMemoryAuditLog()
    form = _approved_form()
    request = StartRequest(
        request_id="req-002",
        client_id=form.client_id,
        candidate_email=CANDIDATE_EMAIL,
        offer_accepted=False,
        form=form,
    )
    result = evaluate_start_gate(request, audit_log)

    assert result.decision == StartDecision.HELD
    assert "offer not accepted" in result.reasons
    _assert_single_clean_audit_entry(audit_log, "req-002")


def test_missing_form_is_held():
    audit_log = InMemoryAuditLog()
    request = StartRequest(
        request_id="req-003",
        client_id="client-synthetic-001",
        candidate_email=CANDIDATE_EMAIL,
        offer_accepted=True,
        form=None,
    )
    result = evaluate_start_gate(request, audit_log)

    assert result.decision == StartDecision.HELD
    assert "no onboarding form" in result.reasons
    _assert_single_clean_audit_entry(audit_log, "req-003")


def test_draft_form_is_held():
    audit_log = InMemoryAuditLog()
    form = make_form(
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
        salary_and_terms=SECRET,
    )
    request = StartRequest(
        request_id="req-004",
        client_id=form.client_id,
        candidate_email=CANDIDATE_EMAIL,
        offer_accepted=True,
        form=form,
    )
    result = evaluate_start_gate(request, audit_log)

    assert result.decision == StartDecision.HELD
    assert "form not approved" in result.reasons
    _assert_single_clean_audit_entry(audit_log, "req-004")


def test_awaiting_approval_form_is_held():
    audit_log = InMemoryAuditLog()
    form = make_form(
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
        salary_and_terms=SECRET,
    ).submit_for_approval()
    assert form.status == FormStatus.AWAITING_APPROVAL
    request = StartRequest(
        request_id="req-005",
        client_id=form.client_id,
        candidate_email=CANDIDATE_EMAIL,
        offer_accepted=True,
        form=form,
    )
    result = evaluate_start_gate(request, audit_log)

    assert result.decision == StartDecision.HELD
    assert "form not approved" in result.reasons
    _assert_single_clean_audit_entry(audit_log, "req-005")


def test_form_from_another_client_is_held():
    audit_log = InMemoryAuditLog()
    form = _approved_form(client_id="client-other-999")
    request = StartRequest(
        request_id="req-006",
        client_id="client-synthetic-001",
        candidate_email=CANDIDATE_EMAIL,
        offer_accepted=True,
        form=form,
    )
    result = evaluate_start_gate(request, audit_log)

    assert result.decision == StartDecision.HELD
    assert "form client mismatch" in result.reasons
    _assert_single_clean_audit_entry(audit_log, "req-006")


def test_hiring_freeze_pauses():
    audit_log = InMemoryAuditLog()
    form = _approved_form()
    request = StartRequest(
        request_id="req-007",
        client_id=form.client_id,
        candidate_email=CANDIDATE_EMAIL,
        offer_accepted=True,
        form=form,
        hiring_freeze=True,
    )
    result = evaluate_start_gate(request, audit_log)

    assert result.decision == StartDecision.PAUSED
    assert result.reasons
    assert result.may_contact_candidate is False
    _assert_single_clean_audit_entry(audit_log, "req-007")
