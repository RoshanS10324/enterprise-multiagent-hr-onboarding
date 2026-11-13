from sub_agent.adapters.memory import InMemoryPersonDirectory
from sub_agent.models import PersonRecordKind, VariantFlags
from sub_agent.onboarding_entry import (
    PLACEMENT_REQUIRED_REASON,
    OpenCaseOutcome,
    OpenCaseRequest,
    open_onboarding_case,
)

from factories import make_approved_form, make_deps, make_form, make_person, make_placement

SECRET = "SECRET_SALARY_VALUE_77777"
CANDIDATE_NAME = "Onboarding Entry Candidate"
CANDIDATE_EMAIL = "onboarding.entry.candidate@example.test"


def _request(form=None, **overrides) -> OpenCaseRequest:
    defaults = dict(
        request_id="req-oe-001",
        client_id="client-synthetic-001",
        hr_officer_id="hr-officer-001",
        offer_accepted=True,
        form=form,
    )
    defaults.update(overrides)
    return OpenCaseRequest(placement=make_placement(), **defaults)


def test_happy_path_opens_one_case_and_publishes_one_event():
    deps = make_deps()
    form = make_approved_form(
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
        salary_and_terms=SECRET,
    )
    request = _request(form=form)

    result = open_onboarding_case(request, deps)

    assert result.outcome == OpenCaseOutcome.CASE_OPENED
    assert result.case is not None
    assert result.case.status.value == "PLAN_PENDING"
    assert result.case.employee_no is not None
    assert result.case.folder_ref is not None
    assert result.event_id is not None

    events = deps.event_publisher.events
    assert len(events) == 1
    event_dump = events[0].model_dump_json()
    assert SECRET not in event_dump
    assert CANDIDATE_EMAIL not in event_dump
    assert CANDIDATE_NAME not in event_dump

    linked_form = deps.form_repo.latest_approved(result.case.case_id)
    assert linked_form.salary_and_terms.get_secret_value() == SECRET


def test_repeating_same_request_links_existing_case():
    deps = make_deps()
    form = make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)
    request = _request(form=form)

    first = open_onboarding_case(request, deps)
    assert first.outcome == OpenCaseOutcome.CASE_OPENED

    # Re-submit the same (unlinked) approved form again, as a client resubmit might.
    second_form = make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)
    second_request = _request(form=second_form, request_id="req-oe-002")
    second = open_onboarding_case(second_request, deps)

    assert second.outcome == OpenCaseOutcome.LINKED_EXISTING
    assert second.existing_case_id == first.case.case_id
    linked_entries = [
        e for e in deps.audit_log.entries() if e.action == "REQUEST_LINKED_TO_EXISTING_CASE"
    ]
    assert len(linked_entries) == 1
    assert linked_entries[0].case_id == first.case.case_id
    assert linked_entries[0].request_id == "req-oe-002"
    dumped = str(linked_entries[0].model_dump())
    assert CANDIDATE_EMAIL not in dumped
    assert CANDIDATE_NAME not in dumped
    assert len(deps.case_repo.list_for_client("client-synthetic-001")) == 1


def test_offer_not_accepted_creates_nothing():
    deps = make_deps()
    form = make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)
    request = _request(form=form, offer_accepted=False)

    result = open_onboarding_case(request, deps)

    assert result.outcome == OpenCaseOutcome.HELD
    assert result.case is None
    assert deps.event_publisher.events == []
    assert deps.case_repo.list_for_client("client-synthetic-001") == []


def test_draft_form_creates_nothing():
    deps = make_deps()
    form = make_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)
    request = _request(form=form)

    result = open_onboarding_case(request, deps)

    assert result.outcome == OpenCaseOutcome.HELD
    assert result.case is None
    assert deps.event_publisher.events == []


def test_hiring_freeze_creates_nothing():
    deps = make_deps()
    form = make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)
    request = _request(form=form, hiring_freeze=True)

    result = open_onboarding_case(request, deps)

    assert result.outcome == OpenCaseOutcome.PAUSED
    assert result.case is None
    assert deps.event_publisher.events == []
    assert deps.case_repo.list_for_client("client-synthetic-001") == []


def test_rehire_creates_case_with_pending_approval():
    directory = InMemoryPersonDirectory()
    directory.add(
        make_person(
            email="former.employee@example.test",
            full_name="Someone Else Entirely",
            kind=PersonRecordKind.PAST_EMPLOYEE,
        )
    )
    deps = make_deps(person_directory=directory)
    form = make_approved_form(candidate_name="Former Employee", candidate_email="former.employee@example.test")
    request = _request(form=form)

    result = open_onboarding_case(request, deps)

    assert result.outcome == OpenCaseOutcome.CASE_OPENED
    assert result.case.variants.is_rehire is True
    assert len(result.approvals_created) == 1
    approvals = deps.approval_repo.list_for_case(result.case.case_id)
    assert approvals[0].subject_ref == "is_rehire"


def test_internal_transfer_creates_case_with_pending_approval():
    directory = InMemoryPersonDirectory()
    directory.add(
        make_person(
            email="current.employee@example.test",
            full_name="Someone Else Entirely",
            kind=PersonRecordKind.CURRENT_EMPLOYEE,
        )
    )
    deps = make_deps(person_directory=directory)
    form = make_approved_form(candidate_name="Current Employee", candidate_email="current.employee@example.test")
    request = _request(form=form)

    result = open_onboarding_case(request, deps)

    assert result.outcome == OpenCaseOutcome.CASE_OPENED
    assert result.case.variants.is_internal_transfer is True
    approvals = deps.approval_repo.list_for_case(result.case.case_id)
    assert approvals[0].subject_ref == "is_internal_transfer"


def test_declared_minor_creates_pending_approval():
    deps = make_deps()
    form = make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)
    request = _request(form=form, declared_variants=VariantFlags(is_minor=True))

    result = open_onboarding_case(request, deps)

    assert result.outcome == OpenCaseOutcome.CASE_OPENED
    assert result.case.variants.is_minor is True
    assert len(result.approvals_created) == 1
    approvals = deps.approval_repo.list_for_case(result.case.case_id)
    assert approvals[0].subject_ref == "is_minor"


def test_same_name_only_opens_case_with_possible_duplicate_notice():
    deps = make_deps()
    existing_form = make_approved_form(
        candidate_name="Shared Name Person",
        candidate_email="original.email@example.test",
    )
    first = open_onboarding_case(_request(form=existing_form), deps)
    assert first.outcome == OpenCaseOutcome.CASE_OPENED

    new_form = make_approved_form(
        candidate_name="Shared Name Person",
        candidate_email="different.email@example.test",
    )
    request = _request(form=new_form, request_id="req-samename-001")
    result = open_onboarding_case(request, deps)

    assert result.outcome == OpenCaseOutcome.CASE_OPENED
    assert result.case.case_id != first.case.case_id
    approvals = deps.approval_repo.list_for_case(result.case.case_id)
    assert len(approvals) == 1
    assert approvals[0].subject_ref == "possible_duplicate"


def test_ec39_two_people_same_name_different_email_get_separate_cases():
    deps = make_deps()
    form_one = make_approved_form(
        candidate_name="Shared Name Person",
        candidate_email="person.one@example.test",
    )
    result_one = open_onboarding_case(_request(form=form_one), deps)

    form_two = make_approved_form(
        candidate_name="Shared Name Person",
        candidate_email="person.two@example.test",
    )
    result_two = open_onboarding_case(
        _request(form=form_two, request_id="req-ec39-002"), deps
    )

    assert result_one.outcome == OpenCaseOutcome.CASE_OPENED
    assert result_two.outcome == OpenCaseOutcome.CASE_OPENED
    assert result_one.case.case_id != result_two.case.case_id
    assert result_one.case.employee_no != result_two.case.employee_no


def test_other_client_with_same_email_creates_new_case():
    deps = make_deps()
    form_client_a = make_approved_form(
        client_id="client-synthetic-001",
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
    )
    result_a = open_onboarding_case(_request(form=form_client_a), deps)
    assert result_a.outcome == OpenCaseOutcome.CASE_OPENED

    form_client_b = make_approved_form(
        client_id="client-synthetic-002",
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
    )
    request_b = _request(
        form=form_client_b,
        client_id="client-synthetic-002",
        request_id="req-clientb-001",
    )
    result_b = open_onboarding_case(request_b, deps)

    assert result_b.outcome == OpenCaseOutcome.CASE_OPENED
    assert result_b.case.case_id != result_a.case.case_id


def test_happy_path_audit_trail_has_no_pii():
    deps = make_deps()
    form = make_approved_form(
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
        salary_and_terms=SECRET,
    )
    request = _request(form=form)

    open_onboarding_case(request, deps)

    entries = deps.audit_log.entries()
    actions = [e.action for e in entries]
    assert "CASE_OPENED" in actions
    assert "FORM_LINKED" in actions
    assert "CASE_OPENED_EVENT_PUBLISHED" in actions

    for entry in entries:
        dumped = str(entry.model_dump())
        assert CANDIDATE_EMAIL not in dumped
        assert CANDIDATE_NAME not in dumped
        assert SECRET not in dumped


# --- placement missing: HELD, never a crash, never an invented location ------


def _request_without_placement(form=None, **overrides) -> OpenCaseRequest:
    defaults = dict(
        request_id="req-oe-noplace",
        client_id="client-synthetic-001",
        hr_officer_id="hr-officer-001",
        offer_accepted=True,
        form=form,
    )
    defaults.update(overrides)
    return OpenCaseRequest(**defaults)


def test_missing_placement_holds_and_creates_nothing():
    deps = make_deps()
    form = make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)

    result = open_onboarding_case(_request_without_placement(form=form), deps)

    assert result.outcome == OpenCaseOutcome.HELD
    assert result.reasons == [PLACEMENT_REQUIRED_REASON]
    assert result.case is None
    assert deps.case_repo.list_for_client("client-synthetic-001") == []
    assert deps.event_publisher.events == []
    held = [e for e in deps.audit_log.entries() if e.action == "CASE_OPEN_HELD_PLACEMENT_MISSING"]
    assert len(held) == 1 and held[0].after == {"reasons": [PLACEMENT_REQUIRED_REASON]}


def test_missing_placement_is_reported_alongside_other_start_gate_reasons():
    deps = make_deps()
    form = make_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)  # DRAFT

    result = open_onboarding_case(_request_without_placement(form=form, offer_accepted=False), deps)

    assert result.outcome == OpenCaseOutcome.HELD
    assert "offer not accepted" in result.reasons
    assert "form not approved" in result.reasons
    assert PLACEMENT_REQUIRED_REASON in result.reasons


def test_hiring_freeze_still_pauses_without_placement():
    deps = make_deps()
    form = make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)

    result = open_onboarding_case(_request_without_placement(form=form, hiring_freeze=True), deps)

    assert result.outcome == OpenCaseOutcome.PAUSED


def test_repeat_request_without_placement_still_links_the_existing_case():
    deps = make_deps()
    first = open_onboarding_case(
        _request(form=make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)), deps
    )
    assert first.outcome == OpenCaseOutcome.CASE_OPENED

    again = open_onboarding_case(
        _request_without_placement(
            form=make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL),
            request_id="req-oe-repeat",
        ),
        deps,
    )

    assert again.outcome == OpenCaseOutcome.LINKED_EXISTING
    assert again.existing_case_id == first.case.case_id
