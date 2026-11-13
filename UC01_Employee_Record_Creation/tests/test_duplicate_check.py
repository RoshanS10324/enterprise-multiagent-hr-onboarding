from sub_agent.adapters.memory import InMemoryAuditLog, InMemoryPersonDirectory
from sub_agent.duplicate_check import (
    DuplicateCheckRequest,
    DuplicateOutcome,
    check_duplicates,
)
from sub_agent.models import PersonRecordKind

from factories import make_person

CLIENT_A = "client-synthetic-001"
CLIENT_B = "client-synthetic-002"


def _request(**overrides) -> DuplicateCheckRequest:
    defaults = dict(
        request_id="req-dup-001",
        client_id=CLIENT_A,
        candidate_name="Synthetic Candidate",
        candidate_email="synthetic.candidate@example.test",
        id_number=None,
    )
    defaults.update(overrides)
    return DuplicateCheckRequest(**defaults)


def _assert_single_clean_audit_entry(audit_log, request_id, candidate_name, candidate_email):
    entries = audit_log.entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.request_id == request_id
    dumped = str(entry.model_dump())
    assert candidate_name not in dumped
    assert candidate_email not in dumped
    return entry


def test_empty_directory_is_new():
    directory = InMemoryPersonDirectory()
    audit_log = InMemoryAuditLog()
    request = _request()

    result = check_duplicates(request, directory, audit_log)

    assert result.outcome == DuplicateOutcome.NEW
    assert result.matched_record_ids == []
    assert result.needs_hr_decision is False
    _assert_single_clean_audit_entry(
        audit_log, request.request_id, request.candidate_name, request.candidate_email
    )


def test_open_case_match_by_email():
    directory = InMemoryPersonDirectory()
    existing = make_person(
        client_id=CLIENT_A,
        email="synthetic.candidate@example.test",
        full_name="Someone Else Entirely",
        kind=PersonRecordKind.OPEN_CASE,
        case_id="case-existing-001",
    )
    directory.add(existing)
    audit_log = InMemoryAuditLog()
    request = _request(candidate_email="synthetic.candidate@example.test")

    result = check_duplicates(request, directory, audit_log)

    assert result.outcome == DuplicateOutcome.DUPLICATE_OPEN_CASE
    assert result.existing_case_id == "case-existing-001"
    assert result.matched_record_ids == [existing.record_id]
    assert result.needs_hr_decision is False


def test_open_case_match_by_id_number_only():
    directory = InMemoryPersonDirectory()
    existing = make_person(
        client_id=CLIENT_A,
        email="unrelated.email@example.test",
        id_number="ID-9001",
        full_name="Someone Else Entirely",
        kind=PersonRecordKind.OPEN_CASE,
        case_id="case-existing-002",
    )
    directory.add(existing)
    audit_log = InMemoryAuditLog()
    request = _request(
        candidate_email="synthetic.candidate@example.test",
        id_number="ID-9001",
    )

    result = check_duplicates(request, directory, audit_log)

    assert result.outcome == DuplicateOutcome.DUPLICATE_OPEN_CASE
    assert result.existing_case_id == "case-existing-002"
    assert result.matched_record_ids == [existing.record_id]


def test_past_employee_is_rehire():
    directory = InMemoryPersonDirectory()
    existing = make_person(
        client_id=CLIENT_A,
        email="synthetic.candidate@example.test",
        full_name="Someone Else Entirely",
        kind=PersonRecordKind.PAST_EMPLOYEE,
    )
    directory.add(existing)
    audit_log = InMemoryAuditLog()
    request = _request(candidate_email="synthetic.candidate@example.test")

    result = check_duplicates(request, directory, audit_log)

    assert result.outcome == DuplicateOutcome.REHIRE
    assert result.variants.is_rehire is True
    assert result.needs_hr_decision is True
    assert result.matched_record_ids == [existing.record_id]


def test_current_employee_is_internal_transfer():
    directory = InMemoryPersonDirectory()
    existing = make_person(
        client_id=CLIENT_A,
        email="synthetic.candidate@example.test",
        full_name="Someone Else Entirely",
        kind=PersonRecordKind.CURRENT_EMPLOYEE,
    )
    directory.add(existing)
    audit_log = InMemoryAuditLog()
    request = _request(candidate_email="synthetic.candidate@example.test")

    result = check_duplicates(request, directory, audit_log)

    assert result.outcome == DuplicateOutcome.INTERNAL_TRANSFER
    assert result.variants.is_internal_transfer is True
    assert result.needs_hr_decision is True
    assert result.matched_record_ids == [existing.record_id]


def test_same_name_only_needs_hr_decision():
    directory = InMemoryPersonDirectory()
    existing = make_person(
        client_id=CLIENT_A,
        email="totally.different@example.test",
        full_name="Synthetic Candidate",
        kind=PersonRecordKind.CURRENT_EMPLOYEE,
    )
    directory.add(existing)
    audit_log = InMemoryAuditLog()
    request = _request(
        candidate_name="Synthetic Candidate",
        candidate_email="synthetic.candidate@example.test",
    )

    result = check_duplicates(request, directory, audit_log)

    assert result.outcome == DuplicateOutcome.POSSIBLE_DUPLICATE_NEEDS_HR
    assert result.needs_hr_decision is True
    assert result.existing_case_id is None
    assert result.matched_record_ids == [existing.record_id]
    # Name match alone must not flip any variant flag or claim a real match kind.
    assert result.variants.is_rehire is False
    assert result.variants.is_internal_transfer is False


def test_different_people_same_name_different_email_and_id_is_new():
    # EC-39: two unrelated existing records sharing a name must not confuse
    # the lookup for a third, unrelated candidate who matches neither by
    # email nor id_number.
    directory = InMemoryPersonDirectory()
    directory.add(
        make_person(
            client_id=CLIENT_A,
            email="person.one@example.test",
            id_number="ID-1001",
            full_name="Jordan Lee",
            kind=PersonRecordKind.CURRENT_EMPLOYEE,
        )
    )
    directory.add(
        make_person(
            client_id=CLIENT_A,
            email="person.two@example.test",
            id_number="ID-1002",
            full_name="Jordan Lee",
            kind=PersonRecordKind.PAST_EMPLOYEE,
        )
    )
    audit_log = InMemoryAuditLog()
    request = _request(
        candidate_name="Alex Carter",
        candidate_email="alex.carter@example.test",
        id_number="ID-9999",
    )

    result = check_duplicates(request, directory, audit_log)

    assert result.outcome == DuplicateOutcome.NEW
    assert result.matched_record_ids == []
    assert result.needs_hr_decision is False


def test_other_client_with_same_email_is_new():
    directory = InMemoryPersonDirectory()
    directory.add(
        make_person(
            client_id=CLIENT_B,
            email="synthetic.candidate@example.test",
            full_name="Someone Else Entirely",
            kind=PersonRecordKind.OPEN_CASE,
            case_id="case-other-client-001",
        )
    )
    audit_log = InMemoryAuditLog()
    request = _request(client_id=CLIENT_A, candidate_email="synthetic.candidate@example.test")

    result = check_duplicates(request, directory, audit_log)

    assert result.outcome == DuplicateOutcome.NEW
    assert result.matched_record_ids == []


def test_mixed_kinds_follow_priority_order():
    directory = InMemoryPersonDirectory()
    # Same person matched twice: once by email (as a past employee record)
    # and once by id_number (as an open-case record). OPEN_CASE must win.
    directory.add(
        make_person(
            client_id=CLIENT_A,
            email="synthetic.candidate@example.test",
            id_number="ID-5001",
            full_name="Past Self",
            kind=PersonRecordKind.PAST_EMPLOYEE,
        )
    )
    directory.add(
        make_person(
            client_id=CLIENT_A,
            email="other.email@example.test",
            id_number="ID-5002",
            full_name="Open Case Self",
            kind=PersonRecordKind.OPEN_CASE,
            case_id="case-priority-001",
        )
    )
    audit_log = InMemoryAuditLog()
    request = _request(
        candidate_email="synthetic.candidate@example.test",
        id_number="ID-5002",
    )

    result = check_duplicates(request, directory, audit_log)

    assert result.outcome == DuplicateOutcome.DUPLICATE_OPEN_CASE
    assert result.existing_case_id == "case-priority-001"
    assert len(result.matched_record_ids) == 2
