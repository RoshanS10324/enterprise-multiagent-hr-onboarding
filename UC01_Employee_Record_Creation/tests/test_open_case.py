import pytest

from sub_agent.adapters.memory import (
    InMemoryApprovalRepository,
    InMemoryAuditLog,
    InMemoryCaseRepository,
    InMemoryDocumentStore,
    InMemoryEmployeeNumberGenerator,
    InMemoryFormRepository,
    InMemoryPersonDirectory,
)
from sub_agent.duplicate_check import DuplicateCheckResult, DuplicateOutcome
from sub_agent.models import CaseStatus, PersonRecordKind, VariantFlags
from sub_agent.open_case import create_case

from factories import make_approved_form, make_form, make_placement

CANDIDATE_NAME = "Open Case Candidate"
CANDIDATE_EMAIL = "open.case.candidate@example.test"
SECRET = "SECRET_SALARY_VALUE_55555"


def _new_outcome() -> DuplicateCheckResult:
    return DuplicateCheckResult(
        outcome=DuplicateOutcome.NEW,
        existing_case_id=None,
        matched_record_ids=[],
        variants=VariantFlags(),
        needs_hr_decision=False,
        reasons=["no matching record found"],
    )


def _repos():
    return dict(
        case_repo=InMemoryCaseRepository(),
        form_repo=InMemoryFormRepository(),
        approval_repo=InMemoryApprovalRepository(),
        person_directory=InMemoryPersonDirectory(),
        folder_store=InMemoryDocumentStore(),
        number_generator=InMemoryEmployeeNumberGenerator(),
        audit_log=InMemoryAuditLog(),
    )


def test_happy_path_creates_case():
    repos = _repos()
    form = make_approved_form(
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
        salary_and_terms=SECRET,
    )

    case, approvals_created = create_case(placement=make_placement(), 
        form=form,
        hr_officer_id="hr-officer-001",
        id_number=None,
        duplicate_result=_new_outcome(),
        declared_variants=None,
        **repos,
    )

    assert case.status == CaseStatus.PLAN_PENDING
    assert case.employee_no == "EMP-000001"
    assert case.folder_ref is not None
    assert approvals_created == []

    linked_form = repos["form_repo"].latest_approved(case.case_id)
    assert linked_form is not None
    assert linked_form.case_id == case.case_id
    assert linked_form.salary_and_terms.get_secret_value() == SECRET

    directory_matches = repos["person_directory"].find_by_email(form.client_id, CANDIDATE_EMAIL)
    assert len(directory_matches) == 1
    assert directory_matches[0].kind == PersonRecordKind.OPEN_CASE
    assert directory_matches[0].case_id == case.case_id


def test_folder_ref_uses_same_case_id_as_case():
    repos = _repos()
    form = make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)

    case, _ = create_case(placement=make_placement(), 
        form=form,
        hr_officer_id="hr-officer-001",
        id_number=None,
        duplicate_result=_new_outcome(),
        declared_variants=None,
        **repos,
    )

    assert case.folder_ref.endswith(case.case_id)


def test_non_approved_form_raises():
    repos = _repos()
    form = make_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)
    with pytest.raises(ValueError):
        create_case(placement=make_placement(), 
            form=form,
            hr_officer_id="hr-officer-001",
            id_number=None,
            duplicate_result=_new_outcome(),
            declared_variants=None,
            **repos,
        )


def test_form_version_two_raises():
    repos = _repos()
    form = make_approved_form(
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
        version=2,
    )
    with pytest.raises(ValueError):
        create_case(placement=make_placement(), 
            form=form,
            hr_officer_id="hr-officer-001",
            id_number=None,
            duplicate_result=_new_outcome(),
            declared_variants=None,
            **repos,
        )


def test_rehire_creates_pending_hc12_approval():
    repos = _repos()
    form = make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)
    dup_result = DuplicateCheckResult(
        outcome=DuplicateOutcome.REHIRE,
        existing_case_id=None,
        matched_record_ids=["rec-001"],
        variants=VariantFlags(is_rehire=True),
        needs_hr_decision=True,
        reasons=["matched a past employee by email or id number"],
    )

    case, approvals_created = create_case(placement=make_placement(), 
        form=form,
        hr_officer_id="hr-officer-001",
        id_number=None,
        duplicate_result=dup_result,
        declared_variants=None,
        **repos,
    )

    assert case.variants.is_rehire is True
    assert len(approvals_created) == 1
    approvals = repos["approval_repo"].list_for_case(case.case_id)
    assert len(approvals) == 1
    assert approvals[0].subject_ref == "is_rehire"
    assert approvals[0].checkpoint.value == "HC_12_VARIANT"
    assert approvals[0].status.value == "PENDING"


def test_internal_transfer_creates_pending_hc12_approval():
    repos = _repos()
    form = make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)
    dup_result = DuplicateCheckResult(
        outcome=DuplicateOutcome.INTERNAL_TRANSFER,
        existing_case_id=None,
        matched_record_ids=["rec-002"],
        variants=VariantFlags(is_internal_transfer=True),
        needs_hr_decision=True,
        reasons=["matched a current employee by email or id number"],
    )

    case, approvals_created = create_case(placement=make_placement(), 
        form=form,
        hr_officer_id="hr-officer-001",
        id_number=None,
        duplicate_result=dup_result,
        declared_variants=None,
        **repos,
    )

    assert case.variants.is_internal_transfer is True
    approvals = repos["approval_repo"].list_for_case(case.case_id)
    assert len(approvals) == 1
    assert approvals[0].subject_ref == "is_internal_transfer"


def test_declared_minor_creates_hc12_approval():
    repos = _repos()
    form = make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)

    case, approvals_created = create_case(placement=make_placement(), 
        form=form,
        hr_officer_id="hr-officer-001",
        id_number=None,
        duplicate_result=_new_outcome(),
        declared_variants=VariantFlags(is_minor=True),
        **repos,
    )

    assert case.variants.is_minor is True
    approvals = repos["approval_repo"].list_for_case(case.case_id)
    assert len(approvals) == 1
    assert approvals[0].subject_ref == "is_minor"


def test_possible_duplicate_creates_case_and_notice_approval():
    repos = _repos()
    form = make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)
    dup_result = DuplicateCheckResult(
        outcome=DuplicateOutcome.POSSIBLE_DUPLICATE_NEEDS_HR,
        existing_case_id=None,
        matched_record_ids=["rec-003"],
        variants=VariantFlags(),
        needs_hr_decision=True,
        reasons=["name matches an existing record but email/id number do not; needs HR review"],
    )

    case, approvals_created = create_case(placement=make_placement(), 
        form=form,
        hr_officer_id="hr-officer-001",
        id_number=None,
        duplicate_result=dup_result,
        declared_variants=None,
        **repos,
    )

    assert case is not None
    approvals = repos["approval_repo"].list_for_case(case.case_id)
    assert len(approvals) == 1
    assert approvals[0].subject_ref == "possible_duplicate"
    assert "rec-003" in approvals[0].reason


def test_with_case_returns_new_instance_and_leaves_original_unchanged():
    form = make_approved_form(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL)
    linked = form.with_case("case-xyz")

    assert linked is not form
    assert linked.case_id == "case-xyz"
    assert form.case_id is None
    assert linked.salary_and_terms.get_secret_value() == form.salary_and_terms.get_secret_value()
