"""Rehire detection and handling (approved decision D3b).

Rehire support already existed end to end: check_duplicates classifies
the match, open_case records the reactivation proposal on the Case and
gates it behind an HC_12_VARIANT approval, and hris_prepare turns it
into an UPDATE of the existing employee via updates_external_ref. These
tests pin the behaviour the decision asks for, and cover the ambiguous
case that was previously resolved by picking matches[0].

Persistence of a rehire is a separate, still-open gap: the live schema
has no hris_external_ref / reactivates_* / updates_external_ref
columns, so the Supabase adapters refuse rather than drop the link.
See test_supabase_adapters.py and docs/schema/live-schema-comparison.md.
"""

from __future__ import annotations

from sub_agent.adapters.memory import InMemoryAuditLog, InMemoryPersonDirectory
from sub_agent.duplicate_check import (
    DuplicateCheckRequest,
    DuplicateOutcome,
    check_duplicates,
)
from sub_agent.models import PersonRecordKind

from factories import make_person, make_placement

CLIENT_A = "client-synthetic-001"
CLIENT_B = "client-synthetic-002"
EMAIL = "returning.person@example.test"


def _check(directory: InMemoryPersonDirectory, **overrides):
    defaults = dict(
        request_id="req-rehire-001",
        client_id=CLIENT_A,
        candidate_name="Returning Person",
        candidate_email=EMAIL,
        id_number=None,
    )
    defaults.update(overrides)
    return check_duplicates(
        DuplicateCheckRequest(**defaults), directory, InMemoryAuditLog()
    )


# ------------------------------------------------------------ no match


def test_no_match_is_a_new_joiner():
    result = _check(InMemoryPersonDirectory())
    assert result.outcome is DuplicateOutcome.NEW
    assert result.reactivation_record_id is None
    assert result.reactivation_ambiguous is False
    assert result.needs_hr_decision is False


# ------------------------------------------------- previous employee


def test_single_past_employee_is_a_rehire_with_a_reactivation_proposal():
    past = make_person(
        client_id=CLIENT_A,
        email=EMAIL,
        kind=PersonRecordKind.PAST_EMPLOYEE,
        hris_external_ref="ZP-1001",
    )
    result = _check(InMemoryPersonDirectory([past]))

    assert result.outcome is DuplicateOutcome.REHIRE
    assert result.variants.is_rehire is True
    assert result.reactivation_record_id == past.record_id
    assert result.reactivation_external_ref == "ZP-1001"
    # A rehire is a PROPOSAL, never automatic.
    assert result.needs_hr_decision is True


def test_rehire_match_by_id_number_when_the_email_changed():
    past = make_person(
        client_id=CLIENT_A,
        email="old.address@example.test",
        id_number="ID-9001",
        kind=PersonRecordKind.PAST_EMPLOYEE,
        hris_external_ref="ZP-1002",
    )
    result = _check(InMemoryPersonDirectory([past]), id_number="ID-9001")
    assert result.outcome is DuplicateOutcome.REHIRE
    assert result.reactivation_record_id == past.record_id


def test_past_employee_never_in_the_hris_has_nothing_to_reactivate():
    """Still a rehire for HR purposes, but there is no external record to
    update, so the HRIS write must create one rather than update."""
    past = make_person(
        client_id=CLIENT_A, email=EMAIL, kind=PersonRecordKind.PAST_EMPLOYEE
    )
    result = _check(InMemoryPersonDirectory([past]))
    assert result.outcome is DuplicateOutcome.REHIRE
    assert result.reactivation_record_id == past.record_id
    assert result.reactivation_external_ref is None


# -------------------------------------------------- existing active


def test_current_employee_is_an_internal_transfer_not_a_rehire():
    current = make_person(
        client_id=CLIENT_A,
        email=EMAIL,
        kind=PersonRecordKind.CURRENT_EMPLOYEE,
        hris_external_ref="ZP-2001",
    )
    result = _check(InMemoryPersonDirectory([current]))
    assert result.outcome is DuplicateOutcome.INTERNAL_TRANSFER
    assert result.variants.is_internal_transfer is True
    assert result.variants.is_rehire is False
    assert result.reactivation_record_id == current.record_id


# ------------------------------------------------- duplicate request


def test_an_open_case_takes_priority_and_proposes_no_reactivation():
    """A second request for someone already being onboarded is a
    duplicate, not a rehire: nothing is reactivated."""
    directory = InMemoryPersonDirectory(
        [
            make_person(
                client_id=CLIENT_A,
                email=EMAIL,
                kind=PersonRecordKind.OPEN_CASE,
                case_id="case-existing-1",
            ),
            make_person(
                client_id=CLIENT_A,
                email=EMAIL,
                kind=PersonRecordKind.PAST_EMPLOYEE,
                hris_external_ref="ZP-3001",
            ),
        ]
    )
    result = _check(directory)
    assert result.outcome is DuplicateOutcome.DUPLICATE_OPEN_CASE
    assert result.existing_case_id == "case-existing-1"
    assert result.reactivation_record_id is None


# ------------------------------------------------- ambiguous matches


def test_two_past_employees_matching_produce_no_automatic_reactivation():
    """Regression: this used to reactivate matches[0].

    Two distinct people sharing an email (or an id number) is a data
    problem, and picking one arbitrarily could attach a new joiner to
    the WRONG person's HRIS record. The rehire outcome and the HR gate
    are kept; the reactivation proposal is withheld.
    """
    a = make_person(
        client_id=CLIENT_A,
        email=EMAIL,
        full_name="Returning Person",
        kind=PersonRecordKind.PAST_EMPLOYEE,
        hris_external_ref="ZP-4001",
    )
    b = make_person(
        client_id=CLIENT_A,
        email=EMAIL,
        full_name="Returning Person Also",
        kind=PersonRecordKind.PAST_EMPLOYEE,
        hris_external_ref="ZP-4002",
    )
    result = _check(InMemoryPersonDirectory([a, b]))

    assert result.outcome is DuplicateOutcome.REHIRE
    assert result.needs_hr_decision is True
    assert result.reactivation_ambiguous is True
    assert result.reactivation_record_id is None
    assert result.reactivation_external_ref is None
    # Both candidates are still reported so HR can choose.
    assert set(result.matched_record_ids) == {a.record_id, b.record_id}
    assert any("HR must choose" in r for r in result.reasons)


def test_two_current_employees_matching_are_also_ambiguous():
    directory = InMemoryPersonDirectory(
        [
            make_person(
                client_id=CLIENT_A, email=EMAIL, kind=PersonRecordKind.CURRENT_EMPLOYEE
            ),
            make_person(
                client_id=CLIENT_A, email=EMAIL, kind=PersonRecordKind.CURRENT_EMPLOYEE
            ),
        ]
    )
    result = _check(directory)
    assert result.outcome is DuplicateOutcome.INTERNAL_TRANSFER
    assert result.reactivation_ambiguous is True
    assert result.reactivation_record_id is None


def test_email_and_id_number_hitting_the_same_record_is_not_ambiguous():
    """Deduplication by record_id must not make one record look like two."""
    past = make_person(
        client_id=CLIENT_A,
        email=EMAIL,
        id_number="ID-9100",
        kind=PersonRecordKind.PAST_EMPLOYEE,
        hris_external_ref="ZP-5001",
    )
    result = _check(InMemoryPersonDirectory([past]), id_number="ID-9100")
    assert result.reactivation_ambiguous is False
    assert result.reactivation_record_id == past.record_id


# --------------------------------------------- name-only and tenancy


def test_a_name_match_alone_never_reactivates_anyone():
    past = make_person(
        client_id=CLIENT_A,
        email="someone.else@example.test",
        full_name="Returning Person",
        kind=PersonRecordKind.PAST_EMPLOYEE,
        hris_external_ref="ZP-6001",
    )
    result = _check(InMemoryPersonDirectory([past]))
    assert result.outcome is DuplicateOutcome.POSSIBLE_DUPLICATE_NEEDS_HR
    assert result.reactivation_record_id is None
    assert result.variants.is_rehire is False
    assert result.needs_hr_decision is True


def test_a_past_employee_of_another_tenant_is_not_a_rehire():
    past = make_person(
        client_id=CLIENT_B,
        email=EMAIL,
        kind=PersonRecordKind.PAST_EMPLOYEE,
        hris_external_ref="ZP-7001",
    )
    result = _check(InMemoryPersonDirectory([past]), client_id=CLIENT_A)
    assert result.outcome is DuplicateOutcome.NEW
    assert result.reactivation_record_id is None


# ------------------------------------------------- history preserved


def test_the_previous_record_is_referenced_not_modified():
    past = make_person(
        client_id=CLIENT_A,
        email=EMAIL,
        kind=PersonRecordKind.PAST_EMPLOYEE,
        hris_external_ref="ZP-8001",
    )
    before = past.model_dump()
    directory = InMemoryPersonDirectory([past])

    result = _check(directory)

    assert result.reactivation_record_id == past.record_id
    # The prior record is untouched: still PAST_EMPLOYEE, same ref.
    assert past.model_dump() == before
    assert directory.find_by_email(CLIENT_A, EMAIL)[0].kind is (
        PersonRecordKind.PAST_EMPLOYEE
    )


def test_hr_is_told_why_no_reactivation_was_proposed():
    """The HC_12 gate still fires on an ambiguous rehire, and its reason
    has to explain the ambiguity - otherwise HR sees a bare 'is_rehire'
    approval with no way to act on it."""
    from factories import make_approved_form, make_deps

    from sub_agent.models import ApprovalStatus, Checkpoint
    from sub_agent.open_case import create_case

    deps = make_deps()
    for _ in range(2):
        deps.person_directory.add(
            make_person(
                client_id=CLIENT_A,
                email=EMAIL,
                full_name="Returning Person",
                kind=PersonRecordKind.PAST_EMPLOYEE,
                hris_external_ref="ZP-9001",
            )
        )

    result = _check(deps.person_directory)
    form = make_approved_form(
        client_id=CLIENT_A, candidate_name="Returning Person", candidate_email=EMAIL
    )
    case, _ = create_case(placement=make_placement(), 
        form=form,
        hr_officer_id="hr-officer-1",
        id_number=None,
        duplicate_result=result,
        declared_variants=None,
        case_repo=deps.case_repo,
        form_repo=deps.form_repo,
        approval_repo=deps.approval_repo,
        person_directory=deps.person_directory,
        folder_store=deps.document_store,
        number_generator=deps.number_generator,
        audit_log=deps.audit_log,
    )

    # Nothing is proposed on the case itself...
    assert case.reactivates_record_id is None
    assert case.reactivates_external_ref is None
    # ...but HR is still gated, and told why.
    rehire = [
        a
        for a in deps.approval_repo.list_for_case(case.case_id)
        if a.subject_ref == "is_rehire"
    ]
    assert len(rehire) == 1
    assert rehire[0].checkpoint is Checkpoint.HC_12_VARIANT
    assert rehire[0].status is ApprovalStatus.PENDING
    assert "HR must choose" in rehire[0].reason


def test_duplicate_check_audit_entry_carries_no_pii():
    past = make_person(
        client_id=CLIENT_A, email=EMAIL, kind=PersonRecordKind.PAST_EMPLOYEE
    )
    audit_log = InMemoryAuditLog()
    check_duplicates(
        DuplicateCheckRequest(
            request_id="req-rehire-pii",
            client_id=CLIENT_A,
            candidate_name="Returning Person",
            candidate_email=EMAIL,
        ),
        InMemoryPersonDirectory([past]),
        audit_log,
    )
    dumped = str(audit_log.entries()[0].model_dump())
    assert EMAIL not in dumped
    assert "Returning Person" not in dumped
