"""Rehire and internal transfer reactivate the existing employee.

Closes known gap #28: UC-01 used to open a rehire case that, against a
real HRIS, would have created a second employee for the same person.
The framework (EC-43) expects UC-01 to *propose* reactivating the
existing record instead - propose, because the decision is HR's at
HC_12_VARIANT and nothing is written until HC_05 is approved too.
"""

import pytest
from factories import TEST_TODAY, make_approved_form, make_change_deps, make_deps, make_placement

from sub_agent.duplicate_check import DuplicateCheckRequest, DuplicateOutcome, check_duplicates
from sub_agent.hris_prepare import PrepareOutcome, prepare_hris_records
from sub_agent.hris_write import approve_hris_write, execute_hris_write
from sub_agent.models import (
    ApprovalStatus,
    Checkpoint,
    HrisRecordStatus,
    PersonRecord,
    PersonRecordKind,
)
from sub_agent.open_case import create_case

CLIENT = "client-reactivate"
EMAIL = "jordan.returning@example.test"
NAME = "Jordan Returning"
PRIOR_REF = "ext-prior-employee-0001"


def _seed_prior(deps, kind: PersonRecordKind, external_ref=PRIOR_REF) -> PersonRecord:
    record = PersonRecord(
        client_id=CLIENT,
        email=EMAIL,
        full_name=NAME,
        kind=kind,
        hris_external_ref=external_ref,
    )
    deps.person_directory.add(record)
    return record


def _open(deps, form=None):
    form = form or make_approved_form(client_id=CLIENT, candidate_name=NAME, candidate_email=EMAIL)
    result = check_duplicates(
        DuplicateCheckRequest(
            request_id="rq-1", client_id=CLIENT, candidate_name=NAME, candidate_email=EMAIL
        ),
        deps.person_directory,
        deps.audit_log,
    )
    case, approvals = create_case(placement=make_placement(), 
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
    return result, case, approvals


# ---------------------------------------------------------------------
# The duplicate check names who to reactivate
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,outcome",
    [
        (PersonRecordKind.PAST_EMPLOYEE, DuplicateOutcome.REHIRE),
        (PersonRecordKind.CURRENT_EMPLOYEE, DuplicateOutcome.INTERNAL_TRANSFER),
    ],
)
def test_match_identifies_the_record_to_reactivate(kind, outcome):
    deps = make_deps()
    prior = _seed_prior(deps, kind)
    result, _, _ = _open(deps)

    assert result.outcome is outcome
    assert result.reactivation_record_id == prior.record_id
    assert result.reactivation_external_ref == PRIOR_REF


def test_a_brand_new_joiner_reactivates_nothing():
    deps = make_deps()
    _, case, _ = _open(deps)

    assert case.reactivates_record_id is None
    assert case.reactivates_external_ref is None


def test_case_carries_the_reactivation_proposal():
    deps = make_deps()
    prior = _seed_prior(deps, PersonRecordKind.PAST_EMPLOYEE)
    _, case, _ = _open(deps)

    assert case.variants.is_rehire
    assert case.reactivates_record_id == prior.record_id
    assert case.reactivates_external_ref == PRIOR_REF


def test_hr_is_asked_to_approve_the_reactivation_explicitly():
    """UC-01 proposes; HR decides. The HC_12 approval has to say what it
    is proposing, or the human cannot make the decision."""
    deps = make_deps()
    prior = _seed_prior(deps, PersonRecordKind.PAST_EMPLOYEE)
    _, case, approval_ids = _open(deps)

    approvals = deps.approval_repo.list_for_case(case.case_id)
    rehire = [a for a in approvals if a.subject_ref == "is_rehire"]
    assert len(rehire) == 1
    assert rehire[0].checkpoint is Checkpoint.HC_12_VARIANT
    assert rehire[0].status is ApprovalStatus.PENDING
    assert prior.record_id in rehire[0].reason
    assert "reactivat" in rehire[0].reason


def test_a_prior_employee_with_no_hris_reference_is_not_a_reactivation():
    """Someone recorded as a past employee but never written to the HRIS
    has nothing to reactivate, so the case creates normally."""
    deps = make_deps()
    _seed_prior(deps, PersonRecordKind.PAST_EMPLOYEE, external_ref=None)
    _, case, _ = _open(deps)

    assert case.variants.is_rehire  # still flagged for HR
    assert case.reactivates_external_ref is None


# ---------------------------------------------------------------------
# The write reactivates instead of creating a second employee
# ---------------------------------------------------------------------


def _write_deps_for(case, form):
    deps = make_change_deps()
    deps.case_repo.save(case)
    deps.form_repo.save(form)
    return deps


def test_rehire_updates_the_existing_employee_rather_than_creating_one():
    onboarding = make_deps()
    _seed_prior(onboarding, PersonRecordKind.PAST_EMPLOYEE)
    form = make_approved_form(
        client_id=CLIENT, candidate_name=NAME, candidate_email=EMAIL, joining_date=TEST_TODAY
    )
    _, case, _ = _open(onboarding, form)

    deps = _write_deps_for(case, onboarding.form_repo.latest_approved(case.case_id))
    # HR approves the rehire variant; prepare blocks until they do.
    for approval in onboarding.approval_repo.list_for_case(case.case_id):
        approval.status = ApprovalStatus.APPROVED
        approval.decided_by = "hr-manager-1"
        deps.approval_repo.add(approval)

    prepared = prepare_hris_records(case.case_id, deps)
    assert prepared.outcome is PrepareOutcome.PREPARED_UPDATE

    records = [deps.hris_record_repo.get(r) for r in prepared.record_ids]
    assert all(r.updates_external_ref == PRIOR_REF for r in records)

    approve_hris_write(prepared.approval_id, "hr-manager-2", deps)
    outcome = execute_hris_write(case.case_id, deps)

    assert outcome.completed
    # The existing employee was reactivated, so no new one was minted.
    assert deps.hris_gateway.updated_count() == 2
    written = [r for r in deps.hris_record_repo.list_for_case(case.case_id)
               if r.status is HrisRecordStatus.WRITTEN]
    assert {r.external_ref for r in written} == {PRIOR_REF}


def test_a_new_joiner_still_creates_a_new_employee():
    """The reactivation path must not leak into the ordinary case."""
    onboarding = make_deps()
    form = make_approved_form(
        client_id=CLIENT, candidate_name=NAME, candidate_email=EMAIL, joining_date=TEST_TODAY
    )
    _, case, _ = _open(onboarding, form)

    deps = _write_deps_for(case, onboarding.form_repo.latest_approved(case.case_id))
    prepared = prepare_hris_records(case.case_id, deps)

    assert prepared.outcome is PrepareOutcome.PREPARED
    records = [deps.hris_record_repo.get(r) for r in prepared.record_ids]
    assert all(r.updates_external_ref is None for r in records)

    approve_hris_write(prepared.approval_id, "hr-manager-2", deps)
    execute_hris_write(case.case_id, deps)

    assert deps.hris_gateway.updated_count() == 0
    assert deps.hris_gateway.written_count() == 2
