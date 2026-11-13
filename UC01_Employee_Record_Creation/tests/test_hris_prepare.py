import pytest

from sub_agent.compare_sources import compare_sources
from sub_agent.form_changes import approve_form_change, propose_form_change
from sub_agent.hris_prepare import PrepareOutcome, prepare_hris_records
from sub_agent.hris_write import approve_hris_write, execute_hris_write, reject_hris_write
from sub_agent.models import (
    Approval,
    ApprovalStatus,
    CaseStatus,
    Checkpoint,
    FormChangeRequest,
    HrisRecordStatus,
    HrisSystem,
    SourceSnapshot,
)

from factories import make_change_deps, make_compare_deps, seed_linked_case_and_form

CANDIDATE_NAME = "Hris Prepare Candidate"
SALARY = "Base: 140000 SYNTHETIC/yr; benefits: standard"


def test_happy_path_creates_two_records_and_one_approval():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps, candidate_name=CANDIDATE_NAME, salary_and_terms=SALARY)

    result = prepare_hris_records(case.case_id, deps)

    assert result.outcome == PrepareOutcome.PREPARED
    assert len(result.record_ids) == 2
    assert result.approval_id is not None

    records = deps.hris_record_repo.list_for_case(case.case_id)
    assert len(records) == 2
    assert {r.system for r in records} == {HrisSystem.ZOHO_PEOPLE, HrisSystem.ZOHO_PAYROLL}
    for r in records:
        assert r.status == HrisRecordStatus.PREPARED

    approval = deps.approval_repo.get(result.approval_id)
    assert approval.checkpoint == Checkpoint.HC_05_HRIS_WRITE
    assert approval.status == ApprovalStatus.PENDING
    assert approval.subject_ref == f"hris:{case.case_id}:v1"


def test_salary_text_appears_nowhere():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps, candidate_name=CANDIDATE_NAME, salary_and_terms=SALARY)

    result = prepare_hris_records(case.case_id, deps)

    for record_id in result.record_ids:
        record = deps.hris_record_repo.get(record_id)
        assert SALARY not in repr(record)
        assert SALARY not in str(record)
        assert SALARY not in str(record.model_dump())

    approval = deps.approval_repo.get(result.approval_id)
    assert SALARY not in str(approval.model_dump())
    assert SALARY not in repr(approval)

    for entry in deps.audit_log.entries():
        assert SALARY not in str(entry.model_dump())

    assert SALARY not in str(result.model_dump())
    assert SALARY not in repr(result)


def test_mapping_puts_salary_field_name_only_on_payroll_record():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps, salary_and_terms=SALARY)

    result = prepare_hris_records(case.case_id, deps)
    records = {r.system: r for r in (deps.hris_record_repo.get(rid) for rid in result.record_ids)}

    people_record = records[HrisSystem.ZOHO_PEOPLE]
    payroll_record = records[HrisSystem.ZOHO_PAYROLL]

    # The record never stores the salary itself, only the field NAME that
    # is sensitive; the value is rebuilt from the form at write time.
    assert people_record.sensitive_field_names == []
    assert payroll_record.sensitive_field_names == ["compensation_terms"]

    assert all("salary" not in k.lower() for k in people_record.prepared_payload)
    assert "compensation_terms" not in payroll_record.prepared_payload
    assert SALARY not in str(payroll_record.model_dump())
    assert SALARY not in repr(payroll_record)


def test_rerunning_returns_already_prepared_and_creates_nothing():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)

    first = prepare_hris_records(case.case_id, deps)
    second = prepare_hris_records(case.case_id, deps)

    assert second.outcome == PrepareOutcome.ALREADY_PREPARED
    assert sorted(second.record_ids) == sorted(first.record_ids)
    assert len(deps.hris_record_repo.list_for_case(case.case_id)) == 2
    assert len(deps.approval_repo.list_for_case(case.case_id)) == 1


def test_blocked_by_open_discrepancy():
    deps = make_change_deps()
    case, form = seed_linked_case_and_form(deps)
    compare_deps = make_compare_deps()
    compare_deps.discrepancy_repo = deps.discrepancy_repo
    compare_deps.approval_repo = deps.approval_repo

    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Different Title"})
    compare_sources(case, form, [snapshot], compare_deps)

    result = prepare_hris_records(case.case_id, deps)

    assert result.outcome == PrepareOutcome.BLOCKED_OPEN_DISCREPANCIES
    assert result.blockers
    assert deps.hris_record_repo.list_for_case(case.case_id) == []
    actions = [e.action for e in deps.audit_log.entries() if e.action == "HRIS_PREPARE_BLOCKED"]
    assert len(actions) == 1


def test_blocked_by_pending_rehire_variant_approval():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    deps.approval_repo.add(
        Approval(
            case_id=case.case_id,
            checkpoint=Checkpoint.HC_12_VARIANT,
            subject_ref="is_rehire",
            requested_to=case.hr_officer_id,
        )
    )

    result = prepare_hris_records(case.case_id, deps)

    assert result.outcome == PrepareOutcome.BLOCKED_PENDING_VARIANT_DECISION
    assert deps.hris_record_repo.list_for_case(case.case_id) == []


def test_blocked_by_pending_form_change():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    propose_form_change(
        FormChangeRequest(
            case_id=case.case_id,
            requested_by="hr-officer-001",
            change_reason="Designation correction",
            designation="Senior Engineer",
        ),
        deps,
        approver_id="hr-manager-001",
    )

    result = prepare_hris_records(case.case_id, deps)

    assert result.outcome == PrepareOutcome.BLOCKED_FORM_CHANGE_PENDING
    assert deps.hris_record_repo.list_for_case(case.case_id) == []


def test_possible_duplicate_notice_does_not_block():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    deps.approval_repo.add(
        Approval(
            case_id=case.case_id,
            checkpoint=Checkpoint.HC_12_VARIANT,
            subject_ref="possible_duplicate",
            requested_to=case.hr_officer_id,
        )
    )

    result = prepare_hris_records(case.case_id, deps)

    assert result.outcome == PrepareOutcome.PREPARED


def test_unknown_case_raises():
    deps = make_change_deps()
    with pytest.raises(ValueError):
        prepare_hris_records("not-a-real-case", deps)


def test_prepare_succeeds_after_discrepancy_resolved():
    from sub_agent.compare_sources import DiscrepancyDecision, resolve_discrepancy

    deps = make_change_deps()
    case, form = seed_linked_case_and_form(deps)
    compare_deps = make_compare_deps()
    compare_deps.discrepancy_repo = deps.discrepancy_repo
    compare_deps.approval_repo = deps.approval_repo

    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Different Title"})
    compare_result = compare_sources(case, form, [snapshot], compare_deps)
    discrepancy_id = compare_result.new_discrepancies[0]

    blocked = prepare_hris_records(case.case_id, deps)
    assert blocked.outcome == PrepareOutcome.BLOCKED_OPEN_DISCREPANCIES

    resolve_discrepancy(discrepancy_id, DiscrepancyDecision.KEEP_FORM_VALUE, "hr-officer-001", compare_deps)

    result = prepare_hris_records(case.case_id, deps)
    assert result.outcome == PrepareOutcome.PREPARED


def test_rejection_is_terminal_for_the_form_version():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    prepare_result = prepare_hris_records(case.case_id, deps)
    reject_hris_write(prepare_result.approval_id, "hr-manager-001", "Compensation mismatch", deps)

    second = prepare_hris_records(case.case_id, deps)

    assert second.outcome == PrepareOutcome.BLOCKED_HRIS_REJECTED
    assert sorted(second.blockers) == sorted(prepare_result.record_ids)
    # No new records were created.
    assert len(deps.hris_record_repo.list_for_case(case.case_id)) == 2
    actions = [e.action for e in deps.audit_log.entries() if e.action == "HRIS_PREPARE_BLOCKED"]
    assert len(actions) == 1


def test_prepares_an_update_after_write_at_older_version():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps, designation="Engineer A")
    prepare_result = prepare_hris_records(case.case_id, deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", deps)
    execute_hris_write(case.case_id, deps)
    assert deps.hris_gateway.written_count() == 2

    propose_result = propose_form_change(
        FormChangeRequest(
            case_id=case.case_id,
            requested_by="hr-officer-001",
            change_reason="Designation correction",
            designation="Engineer B",
        ),
        deps,
        approver_id="hr-manager-001",
    )
    approve_form_change(propose_result.approval_id, "hr-manager-001", deps)

    written_refs = {
        r.system: r.external_ref for r in deps.hris_record_repo.list_for_case(case.case_id)
    }

    result = prepare_hris_records(case.case_id, deps)

    # The v2 batch updates the employees already written rather than
    # creating a second pair.
    assert result.outcome == PrepareOutcome.PREPARED_UPDATE
    v2_records = [r for r in deps.hris_record_repo.list_for_case(case.case_id) if r.form_version == 2]
    assert len(v2_records) == 2
    for record in v2_records:
        assert record.updates_external_ref == written_refs[record.system]

    # Nothing is sent until a human approves the update batch.
    assert deps.hris_gateway.written_count() == 2


def test_update_batch_reaches_the_target_system_without_a_second_employee():
    """Known gap #11/#13: a form change approved after the record was
    written used to be a dead end. It now flows through to the target
    system in place."""
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps, designation="Engineer A")
    first = prepare_hris_records(case.case_id, deps)
    approve_hris_write(first.approval_id, "hr-manager-001", deps)
    execute_hris_write(case.case_id, deps)
    original_refs = sorted(
        r.external_ref for r in deps.hris_record_repo.list_for_case(case.case_id)
    )

    propose_result = propose_form_change(
        FormChangeRequest(
            case_id=case.case_id,
            requested_by="hr-officer-001",
            change_reason="Designation correction",
            designation="Engineer B",
        ),
        deps,
        approver_id="hr-manager-001",
    )
    approve_form_change(propose_result.approval_id, "hr-manager-001", deps)

    second = prepare_hris_records(case.case_id, deps)
    approve_hris_write(second.approval_id, "hr-manager-001", deps)
    outcome = execute_hris_write(case.case_id, deps)

    assert outcome.completed
    # Still two employees in the target system, not four.
    assert deps.hris_gateway.written_count() == 2
    assert deps.hris_gateway.updated_count() == 2

    records = deps.hris_record_repo.list_for_case(case.case_id)
    live = [r for r in records if r.status == HrisRecordStatus.WRITTEN]
    retired = [r for r in records if r.status == HrisRecordStatus.CORRECTED]

    # Exactly one live record per system, carrying the new designation
    # and the ORIGINAL external reference.
    assert len(live) == 2 and len(retired) == 2
    assert all(r.form_version == 2 for r in live)
    assert sorted(r.external_ref for r in live) == original_refs
    assert all(r.prepared_payload["job_title"] == "Engineer B" for r in live)


@pytest.mark.parametrize("status", [CaseStatus.ON_HOLD, CaseStatus.WITHDRAWN, CaseStatus.CLOSED])
def test_blocked_case_not_active(status):
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    case.status = status
    deps.case_repo.save(case)

    result = prepare_hris_records(case.case_id, deps)

    assert result.outcome == PrepareOutcome.BLOCKED_CASE_NOT_ACTIVE
    assert deps.hris_record_repo.list_for_case(case.case_id) == []


def test_case_not_active_checked_before_update_needed():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    prepare_result = prepare_hris_records(case.case_id, deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", deps)
    execute_hris_write(case.case_id, deps)

    case.status = CaseStatus.ON_HOLD
    deps.case_repo.save(case)

    propose_result = propose_form_change(
        FormChangeRequest(
            case_id=case.case_id,
            requested_by="hr-officer-001",
            change_reason="Designation correction",
            designation="Senior Engineer",
        ),
        deps,
        approver_id="hr-manager-001",
    )
    approve_form_change(propose_result.approval_id, "hr-manager-001", deps)

    # Both BLOCKED_CASE_NOT_ACTIVE and BLOCKED_UPDATE_NEEDED conditions
    # hold here (v1 written, case on hold); CASE_NOT_ACTIVE must win.
    result = prepare_hris_records(case.case_id, deps)
    assert result.outcome == PrepareOutcome.BLOCKED_CASE_NOT_ACTIVE


def test_requested_by_sets_approval_requester_and_human_audit_actor():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)

    result = prepare_hris_records(case.case_id, deps, requested_by="hr-officer-001")

    approval = deps.approval_repo.get(result.approval_id)
    assert approval.requested_by == "hr-officer-001"

    prepared_entry = next(e for e in deps.audit_log.entries() if e.action == "HRIS_PREPARED")
    assert prepared_entry.actor_type.value == "HUMAN"
    assert prepared_entry.actor_id == "hr-officer-001"
