from datetime import datetime, timezone

import pytest
from factories import make_form, make_intake_deps

from sub_agent.intake.email_intake import (
    IntakeOutcome,
    IntakeRejectionCode,
    IntakeReviewCode,
    ParsedAcceptance,
    handle_acceptance_email,
    handle_form_approved,
    parse_acceptance_email,
    try_start,
)
from sub_agent.models import FormStatus, OfferRecord, OfferStatus, PendingStartStatus

OFFER_REF = "OFFER-4001"
CLIENT_ID = "client-intake-001"
HR_OFFICER_ID = "hr-officer-intake-1"
CANDIDATE_EMAIL = "riley.intake.candidate@example.test"
SECRET_BODY_TEXT = "super secret body text that must never be logged"


def _raw_email(from_addr: str, subject: str, body: str = SECRET_BODY_TEXT) -> str:
    return f"From: {from_addr}\nSubject: {subject}\n\n{body}\n"


def _seed_offer(deps, status: OfferStatus = OfferStatus.SENT, **overrides) -> OfferRecord:
    defaults = dict(
        client_id=CLIENT_ID,
        offer_ref=OFFER_REF,
        candidate_email=CANDIDATE_EMAIL,
        hr_officer_id=HR_OFFICER_ID,
        status=status,
    )
    defaults.update(overrides)
    offer = OfferRecord(**defaults)
    deps.offer_repo.save(offer)
    return offer


# --- parse_acceptance_email -------------------------------------------------


def test_parse_rejects_not_an_email():
    result = parse_acceptance_email("")
    assert result.code == IntakeRejectionCode.NOT_AN_EMAIL

    result = parse_acceptance_email("just some text with no headers at all")
    assert result.code == IntakeRejectionCode.NOT_AN_EMAIL


def test_parse_rejects_no_offer_ref():
    raw = _raw_email(CANDIDATE_EMAIL, "Re: Your offer - I accept")
    result = parse_acceptance_email(raw)
    assert result.code == IntakeRejectionCode.NO_OFFER_REF


def test_parse_rejects_multiple_offer_refs():
    raw = _raw_email(CANDIDATE_EMAIL, "Re: [OFFER-1001] vs [OFFER-1002] - I accept")
    result = parse_acceptance_email(raw)
    assert result.code == IntakeRejectionCode.MULTIPLE_OFFER_REFS


def test_parse_accepts_well_formed_email():
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: Your offer [{OFFER_REF}] - I accept")
    result = parse_acceptance_email(raw)
    assert isinstance(result, ParsedAcceptance)
    assert result.offer_ref == OFFER_REF
    assert result.sender_email == CANDIDATE_EMAIL.lower()


# --- handle_acceptance_email -------------------------------------------------


def test_unknown_offer_is_rejected():
    deps = make_intake_deps()
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - I accept")

    result = handle_acceptance_email(raw, deps)

    assert result.outcome == IntakeOutcome.REJECTED
    assert result.rejection_code == IntakeRejectionCode.UNKNOWN_OFFER.value


def test_sender_mismatch_is_rejected():
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email("someone.else@example.test", f"Re: [{OFFER_REF}] - I accept")

    result = handle_acceptance_email(raw, deps)

    assert result.outcome == IntakeOutcome.REJECTED
    assert result.rejection_code == IntakeRejectionCode.SENDER_MISMATCH.value


def test_sender_match_is_case_insensitive_and_trimmed():
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email(f"  {CANDIDATE_EMAIL.upper()}  ", f"Re: [{OFFER_REF}] - I accept")

    result = handle_acceptance_email(raw, deps)

    # No form seeded, so the case is held waiting - but critically, it is
    # NOT rejected for a sender mismatch.
    assert result.outcome != IntakeOutcome.REJECTED


def test_offer_withdrawn_is_rejected():
    deps = make_intake_deps()
    _seed_offer(deps, status=OfferStatus.WITHDRAWN)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - I accept")

    result = handle_acceptance_email(raw, deps)

    assert result.outcome == IntakeOutcome.REJECTED
    assert result.rejection_code == IntakeRejectionCode.OFFER_WITHDRAWN.value


def test_duplicate_acceptance_does_nothing_else():
    deps = make_intake_deps()
    _seed_offer(deps, status=OfferStatus.ACCEPTED, accepted_at=datetime.now(timezone.utc))
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - I accept")

    result = handle_acceptance_email(raw, deps)

    assert result.outcome == IntakeOutcome.DUPLICATE_ACCEPTANCE
    assert result.case_id is None


def test_duplicate_acceptance_writes_an_audit_entry():
    deps = make_intake_deps()
    _seed_offer(deps, status=OfferStatus.ACCEPTED, accepted_at=datetime.now(timezone.utc))
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - I accept")

    handle_acceptance_email(raw, deps)

    entries = [e for e in deps.onboarding_deps.audit_log.entries() if e.action == "DUPLICATE_ACCEPTANCE_IGNORED"]
    assert len(entries) == 1
    assert entries[0].after == {"offer_ref": OFFER_REF}


def test_ambiguous_offer_ref_is_rejected():
    deps = make_intake_deps()
    offer_a = OfferRecord(
        client_id=CLIENT_ID, offer_ref=OFFER_REF, candidate_email=CANDIDATE_EMAIL, hr_officer_id=HR_OFFICER_ID
    )
    offer_b = OfferRecord(
        client_id="client-intake-other",
        offer_ref=OFFER_REF,
        candidate_email=CANDIDATE_EMAIL,
        hr_officer_id=HR_OFFICER_ID,
    )
    # Bypass save()'s cross-client uniqueness check directly, to simulate
    # bad legacy data and exercise the defensive AMBIGUOUS_OFFER_REF branch.
    deps.offer_repo._offers[(offer_a.client_id, offer_a.offer_ref)] = offer_a
    deps.offer_repo._offers[(offer_b.client_id, offer_b.offer_ref)] = offer_b

    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - I accept")
    result = handle_acceptance_email(raw, deps)

    assert result.outcome == IntakeOutcome.REJECTED
    assert result.rejection_code == IntakeRejectionCode.AMBIGUOUS_OFFER_REF.value


def test_save_rejects_duplicate_offer_ref_across_clients():
    deps = make_intake_deps()
    _seed_offer(deps)

    other_client_offer = OfferRecord(
        client_id="client-intake-other",
        offer_ref=OFFER_REF,
        candidate_email="other.candidate@example.test",
        hr_officer_id="hr-officer-other",
    )
    with pytest.raises(ValueError):
        deps.offer_repo.save(other_client_offer)


def test_save_allows_resaving_the_same_clients_offer():
    deps = make_intake_deps()
    offer = _seed_offer(deps)
    deps.offer_repo.save(offer.model_copy(update={"status": OfferStatus.WITHDRAWN}))

    assert deps.offer_repo.get(CLIENT_ID, OFFER_REF).status == OfferStatus.WITHDRAWN


# --- acceptance intent: decline / negotiation / no signal -------------------


def test_decline_from_correct_sender_needs_review():
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: Your offer [{OFFER_REF}] - I must decline")

    result = handle_acceptance_email(raw, deps)

    assert result.outcome == IntakeOutcome.NEEDS_HR_REVIEW
    assert result.review_code == IntakeReviewCode.POSSIBLE_DECLINE_OR_NEGOTIATION.value
    assert result.case_id is None
    offer = deps.offer_repo.get(CLIENT_ID, OFFER_REF)
    assert offer.status == OfferStatus.SENT
    assert deps.pending_repo.list_waiting(CLIENT_ID) == []

    entries = [e for e in deps.onboarding_deps.audit_log.entries() if e.action == "INTAKE_NEEDS_REVIEW"]
    assert len(entries) == 1
    assert entries[0].after == {
        "offer_ref": OFFER_REF,
        "code": IntakeReviewCode.POSSIBLE_DECLINE_OR_NEGOTIATION.value,
    }


@pytest.mark.parametrize(
    "subject_fragment",
    ["reject", "withdraw", "counter", "negotiating", "not accept", "cannot accept", "can't accept"],
)
def test_various_decline_keywords_need_review(subject_fragment):
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - {subject_fragment}")

    result = handle_acceptance_email(raw, deps)

    assert result.outcome == IntakeOutcome.NEEDS_HR_REVIEW
    assert result.review_code == IntakeReviewCode.POSSIBLE_DECLINE_OR_NEGOTIATION.value


@pytest.mark.parametrize(
    "subject_fragment",
    ["these terms are unacceptable", "the offer is not acceptable", "Unacceptable package"],
)
def test_unacceptable_is_never_read_as_an_acceptance(subject_fragment):
    """"unacceptable" contains "accept"; it must go to HR review, never open a case."""
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - {subject_fragment}")

    result = handle_acceptance_email(raw, deps)

    assert result.outcome == IntakeOutcome.NEEDS_HR_REVIEW
    assert result.review_code == IntakeReviewCode.POSSIBLE_DECLINE_OR_NEGOTIATION.value
    assert result.case_id is None


def test_acceptable_alone_is_not_an_acceptance_signal():
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - terms look acceptable, questions to follow")

    result = handle_acceptance_email(raw, deps)

    assert result.outcome == IntakeOutcome.NEEDS_HR_REVIEW
    assert result.review_code == IntakeReviewCode.NO_ACCEPTANCE_SIGNAL.value


@pytest.mark.parametrize("subject_fragment", ["I accept", "Accepted, thank you", "accepting the offer", "Acceptance"])
def test_inflections_of_accept_still_count_as_acceptance(subject_fragment):
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - {subject_fragment}")

    result = handle_acceptance_email(raw, deps)

    assert result.outcome != IntakeOutcome.NEEDS_HR_REVIEW


def test_subject_with_both_accept_and_not_accept_goes_to_review():
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - I accept, but I must not accept")

    result = handle_acceptance_email(raw, deps)

    assert result.outcome == IntakeOutcome.NEEDS_HR_REVIEW
    assert result.review_code == IntakeReviewCode.POSSIBLE_DECLINE_OR_NEGOTIATION.value


def test_subject_with_no_acceptance_signal_needs_review():
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - please advise")

    result = handle_acceptance_email(raw, deps)

    assert result.outcome == IntakeOutcome.NEEDS_HR_REVIEW
    assert result.review_code == IntakeReviewCode.NO_ACCEPTANCE_SIGNAL.value
    assert deps.offer_repo.get(CLIENT_ID, OFFER_REF).status == OfferStatus.SENT


# --- try_start / WAITING_FOR_FORM / handle_form_approved --------------------


def test_email_before_form_approval_waits_with_one_pending_start():
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - I accept")

    result = handle_acceptance_email(raw, deps)

    assert result.outcome == IntakeOutcome.WAITING_FOR_FORM
    assert result.reasons == ["no onboarding form"]
    waiting = deps.pending_repo.list_waiting(CLIENT_ID)
    assert len(waiting) == 1
    assert waiting[0].status == PendingStartStatus.WAITING
    assert waiting[0].offer_ref == OFFER_REF

    # Calling try_start again (e.g. a retry) must not create a second
    # PendingStart for the same offer.
    try_start(CLIENT_ID, OFFER_REF, deps)
    assert len(deps.pending_repo.list_waiting(CLIENT_ID)) == 1


def test_form_approved_later_opens_case_and_marks_pending_started():
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - I accept")
    waiting_result = handle_acceptance_email(raw, deps)
    assert waiting_result.outcome == IntakeOutcome.WAITING_FOR_FORM
    pending_id = waiting_result.pending_id

    form = make_form(
        client_id=CLIENT_ID, candidate_email=CANDIDATE_EMAIL
    ).approve(approved_by="hr-officer-approver", approved_at=datetime.now(timezone.utc))
    deps.form_source.save_for_offer(CLIENT_ID, OFFER_REF, form)

    result = handle_form_approved(CLIENT_ID, OFFER_REF, deps)

    assert result.outcome == IntakeOutcome.CASE_OPENED
    assert result.case_id is not None

    all_pending = [p for p in deps.pending_repo.list_waiting(CLIENT_ID)]
    assert all_pending == []  # no longer WAITING
    started = deps.pending_repo.get_waiting(CLIENT_ID, OFFER_REF)
    assert started is None  # get_waiting only returns WAITING ones


def test_second_form_approved_call_does_not_open_a_second_case():
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - I accept")
    handle_acceptance_email(raw, deps)

    form = make_form(
        client_id=CLIENT_ID, candidate_email=CANDIDATE_EMAIL
    ).approve(approved_by="hr-officer-approver", approved_at=datetime.now(timezone.utc))
    deps.form_source.save_for_offer(CLIENT_ID, OFFER_REF, form)

    first = handle_form_approved(CLIENT_ID, OFFER_REF, deps)
    assert first.outcome == IntakeOutcome.CASE_OPENED
    first_case_id = first.case_id

    second = handle_form_approved(CLIENT_ID, OFFER_REF, deps)
    assert second.outcome in (IntakeOutcome.LINKED_EXISTING, IntakeOutcome.NOTHING_PENDING)
    if second.outcome == IntakeOutcome.LINKED_EXISTING:
        assert second.case_id == first_case_id
    else:
        assert second.case_id is None

    cases = deps.onboarding_deps.case_repo.list_for_client(CLIENT_ID)
    assert len(cases) == 1


def test_nothing_pending_when_no_waiting_pending_start():
    deps = make_intake_deps()
    _seed_offer(deps)

    result = handle_form_approved(CLIENT_ID, OFFER_REF, deps)

    assert result.outcome == IntakeOutcome.NOTHING_PENDING


def test_offer_linked_audit_entry_present_with_case_id_after_case_opened():
    deps = make_intake_deps()
    _seed_offer(deps)
    form = make_form(client_id=CLIENT_ID, candidate_email=CANDIDATE_EMAIL).approve(
        approved_by="hr-officer-approver", approved_at=datetime.now(timezone.utc)
    )
    deps.form_source.save_for_offer(CLIENT_ID, OFFER_REF, form)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - I accept")

    result = handle_acceptance_email(raw, deps)

    assert result.outcome == IntakeOutcome.CASE_OPENED
    linked_entries = [e for e in deps.onboarding_deps.audit_log.entries() if e.action == "OFFER_LINKED"]
    assert len(linked_entries) == 1
    assert linked_entries[0].case_id == result.case_id
    assert linked_entries[0].after["offer_ref"] == OFFER_REF
    assert linked_entries[0].after["source"] == "email"


# --- privacy: body and sender address never land in the audit log ----------


def test_sender_address_and_body_never_appear_in_audit_entries():
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email("mismatched.sender@example.test", f"Re: [{OFFER_REF}] - I accept")

    handle_acceptance_email(raw, deps)

    for entry in deps.onboarding_deps.audit_log.entries():
        dumped = str(entry.model_dump())
        assert "mismatched.sender@example.test" not in dumped
        assert SECRET_BODY_TEXT not in dumped


def test_body_text_never_appears_in_audit_for_a_successful_acceptance():
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] - I accept")

    handle_acceptance_email(raw, deps)

    for entry in deps.onboarding_deps.audit_log.entries():
        dumped = str(entry.model_dump())
        assert SECRET_BODY_TEXT not in dumped


def test_body_text_never_appears_in_audit_for_a_decline():
    deps = make_intake_deps()
    _seed_offer(deps)
    raw = _raw_email(CANDIDATE_EMAIL, f"Re: Your offer [{OFFER_REF}] - I must decline")

    handle_acceptance_email(raw, deps)

    for entry in deps.onboarding_deps.audit_log.entries():
        dumped = str(entry.model_dump())
        assert SECRET_BODY_TEXT not in dumped
        assert CANDIDATE_EMAIL not in dumped


# --- hiring freeze lifted (known gap #32) -----------------------------


def _frozen_deps(frozen: dict):
    """Intake deps whose freeze state the test can flip."""
    deps = make_intake_deps()
    deps.hiring_freeze = lambda client_id: frozen["on"]
    return deps


def test_freeze_lifted_with_nothing_paused_does_nothing():
    from sub_agent.intake.email_intake import handle_freeze_lifted

    deps = make_intake_deps()
    assert handle_freeze_lifted(CLIENT_ID, deps) == []


def test_paused_start_resumes_once_the_freeze_is_lifted():
    from sub_agent.intake.email_intake import handle_freeze_lifted

    frozen = {"on": True}
    deps = _frozen_deps(frozen)
    _seed_offer(deps)
    deps.form_source.save_for_offer(
        CLIENT_ID, OFFER_REF, make_form(client_id=CLIENT_ID, status=FormStatus.APPROVED,
                                       approved_by="hr-manager-1",
                                       approved_at=datetime(2026, 1, 10, tzinfo=timezone.utc))
    )

    paused = handle_acceptance_email(_raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] I accept"), deps)
    assert paused.outcome == IntakeOutcome.PAUSED
    assert paused.reasons == ["hiring_freeze"]

    # Still frozen: retrying changes nothing, the joiner stays paused.
    assert [r.outcome for r in handle_freeze_lifted(CLIENT_ID, deps)] == [IntakeOutcome.PAUSED]

    frozen["on"] = False
    resumed = handle_freeze_lifted(CLIENT_ID, deps)

    assert [r.outcome for r in resumed] == [IntakeOutcome.CASE_OPENED]
    assert resumed[0].case_id
    # Idempotent: the start is no longer waiting, so a second lift is a no-op.
    assert handle_freeze_lifted(CLIENT_ID, deps) == []


def test_freeze_lift_is_audited():
    from sub_agent.intake.email_intake import handle_freeze_lifted

    frozen = {"on": True}
    deps = _frozen_deps(frozen)
    _seed_offer(deps)
    deps.form_source.save_for_offer(
        CLIENT_ID, OFFER_REF, make_form(client_id=CLIENT_ID, status=FormStatus.APPROVED,
                                       approved_by="hr-manager-1",
                                       approved_at=datetime(2026, 1, 10, tzinfo=timezone.utc))
    )
    handle_acceptance_email(_raw_email(CANDIDATE_EMAIL, f"Re: [{OFFER_REF}] I accept"), deps)

    frozen["on"] = False
    handle_freeze_lifted(CLIENT_ID, deps)

    actions = [e.action for e in deps.onboarding_deps.audit_log.entries()]
    assert "HIRING_FREEZE_LIFTED" in actions
