from sub_agent.compare_sources import compare_sources
from sub_agent.hris_prepare import prepare_hris_records
from sub_agent.hris_write import approve_hris_write, execute_hris_write
from sub_agent.invariants import InvariantCode, check_case_invariants
from sub_agent.models import (
    Approval,
    ApprovalStatus,
    AuditEntry,
    Checkpoint,
    FormStatus,
    HrisRecordStatus,
    HrisSystem,
    SourceSnapshot,
)
from sub_agent.onboarding_entry import OnboardingDeps, OpenCaseRequest, open_onboarding_case

from factories import (
    make_placement,
    make_approved_form,
    make_change_deps,
    make_compare_deps,
    make_policy,
    seed_linked_case_and_form,
)
from sub_agent.adapters.memory import (
    InMemoryDocumentStore,
    InMemoryEmployeeNumberGenerator,
    InMemoryPersonDirectory,
)

CANDIDATE_NAME = "Invariant Test Candidate"
CANDIDATE_EMAIL = "invariant.test.candidate@example.test"
SALARY = "Base: 155000 SYNTHETIC/yr; benefits: standard"


def _full_flow_case():
    change_deps = make_change_deps()
    onboarding_deps = OnboardingDeps(
        case_repo=change_deps.case_repo,
        form_repo=change_deps.form_repo,
        approval_repo=change_deps.approval_repo,
        person_directory=InMemoryPersonDirectory(),
        document_store=InMemoryDocumentStore(),
        number_generator=InMemoryEmployeeNumberGenerator(),
        event_publisher=change_deps.publisher,
        audit_log=change_deps.audit_log,
    )
    form = make_approved_form(
        candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL, salary_and_terms=SALARY
    )
    open_request = OpenCaseRequest(placement=make_placement(), 
        request_id="req-inv-001",
        client_id=form.client_id,
        hr_officer_id="hr-officer-001",
        offer_accepted=True,
        form=form,
    )
    result = open_onboarding_case(open_request, onboarding_deps)
    case = result.case

    prepare_result = prepare_hris_records(case.case_id, change_deps, requested_by="hr-officer-001")
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", change_deps)
    execute_hris_write(case.case_id, change_deps)

    return change_deps, case


def test_clean_full_flow_has_no_violations():
    deps, case = _full_flow_case()
    violations = check_case_invariants(case.case_id, deps, policy=make_policy())
    assert violations == []


def test_multiple_approved_forms():
    deps, case = _full_flow_case()
    v1 = deps.form_repo.get(case.case_id, 1)
    # Force a second APPROVED form by resurrecting v1's status directly.
    duplicate = v1.model_copy(update={"status": FormStatus.APPROVED, "form_id": "forged-form-id"})
    deps.form_repo.save(duplicate)

    violations = check_case_invariants(case.case_id, deps)
    assert any(v.code == InvariantCode.MULTIPLE_APPROVED_FORMS for v in violations)


def test_multiple_pending_forms():
    deps, case = _full_flow_case()
    v1 = deps.form_repo.get(case.case_id, 1)
    pending_a = v1.model_copy(update={"status": FormStatus.AWAITING_APPROVAL, "form_id": "pending-a", "version": 2})
    pending_b = v1.model_copy(update={"status": FormStatus.AWAITING_APPROVAL, "form_id": "pending-b", "version": 3})
    deps.form_repo.save(pending_a)
    deps.form_repo.save(pending_b)

    violations = check_case_invariants(case.case_id, deps)
    assert any(v.code == InvariantCode.MULTIPLE_PENDING_FORMS for v in violations)


def test_case_form_mismatch():
    deps, case = _full_flow_case()
    case2 = deps.case_repo.get(case.case_id)
    case2.candidate_name = "Someone Totally Different"
    deps.case_repo.save(case2)

    violations = check_case_invariants(case.case_id, deps)
    mismatch = next(v for v in violations if v.code == InvariantCode.CASE_FORM_MISMATCH)
    assert "candidate_name" in mismatch.detail
    assert "Someone Totally Different" not in mismatch.detail


def test_written_without_approval():
    deps, case = _full_flow_case()
    hc05 = next(
        a for a in deps.approval_repo.list_for_case(case.case_id) if a.checkpoint == Checkpoint.HC_05_HRIS_WRITE
    )
    hc05.status = ApprovalStatus.CANCELLED
    deps.approval_repo.update(hc05)

    violations = check_case_invariants(case.case_id, deps)
    assert any(v.code == InvariantCode.WRITTEN_WITHOUT_APPROVAL for v in violations)


def test_payroll_without_people():
    deps, case = _full_flow_case()
    people = next(
        r for r in deps.hris_record_repo.list_for_case(case.case_id) if r.system == HrisSystem.ZOHO_PEOPLE
    )
    people.status = HrisRecordStatus.APPROVED
    deps.hris_record_repo.update(people)

    violations = check_case_invariants(case.case_id, deps)
    assert any(v.code == InvariantCode.PAYROLL_WITHOUT_PEOPLE for v in violations)


def test_duplicate_written_system():
    deps, case = _full_flow_case()
    existing = next(
        r for r in deps.hris_record_repo.list_for_case(case.case_id) if r.system == HrisSystem.ZOHO_PEOPLE
    )
    duplicate = existing.model_copy(update={"record_id": "forged-record-id"})
    deps.hris_record_repo.add(duplicate)

    violations = check_case_invariants(case.case_id, deps)
    assert any(v.code == InvariantCode.DUPLICATE_WRITTEN_SYSTEM for v in violations)


def test_self_approved():
    deps, case = _full_flow_case()
    hc05 = next(
        a for a in deps.approval_repo.list_for_case(case.case_id) if a.checkpoint == Checkpoint.HC_05_HRIS_WRITE
    )
    hc05.requested_by = "hr-manager-001"
    hc05.decided_by = "hr-manager-001"
    deps.approval_repo.update(hc05)

    violations = check_case_invariants(case.case_id, deps, policy=make_policy())
    assert any(v.code == InvariantCode.SELF_APPROVED for v in violations)

    # Skipped entirely when no policy is given.
    violations_no_policy = check_case_invariants(case.case_id, deps, policy=None)
    assert not any(v.code == InvariantCode.SELF_APPROVED for v in violations_no_policy)


def test_decision_incomplete():
    deps, case = _full_flow_case()
    hc05 = next(
        a for a in deps.approval_repo.list_for_case(case.case_id) if a.checkpoint == Checkpoint.HC_05_HRIS_WRITE
    )
    hc05.decided_by = None
    deps.approval_repo.update(hc05)

    violations = check_case_invariants(case.case_id, deps)
    assert any(v.code == InvariantCode.DECISION_INCOMPLETE for v in violations)


def test_missing_case_opened_audit():
    deps, case = _full_flow_case()
    deps.audit_log._entries = [e for e in deps.audit_log._entries if e.action != "CASE_OPENED"]

    violations = check_case_invariants(case.case_id, deps)
    assert any(v.code == InvariantCode.MISSING_CASE_OPENED_AUDIT for v in violations)


def test_sensitive_in_audit():
    deps, case = _full_flow_case()
    deps.audit_log.append(
        AuditEntry(
            case_id=case.case_id,
            actor_type="AGENT",
            actor_id="forged",
            action="FORGED_LEAK",
            after={"leaked": SALARY},
            reference=case.case_id,
        )
    )

    violations = check_case_invariants(case.case_id, deps)
    leak = next(v for v in violations if v.code == InvariantCode.SENSITIVE_IN_AUDIT)
    assert SALARY not in leak.detail
    assert CANDIDATE_EMAIL not in leak.detail
    assert CANDIDATE_NAME not in leak.detail


def test_no_violation_dump_contains_pii_or_salary():
    deps, case = _full_flow_case()
    case2 = deps.case_repo.get(case.case_id)
    case2.candidate_name = "Mismatch Name"
    deps.case_repo.save(case2)
    deps.audit_log.append(
        AuditEntry(
            case_id=case.case_id,
            actor_type="AGENT",
            actor_id="forged",
            action="FORGED_LEAK",
            after={"leaked": SALARY, "email": CANDIDATE_EMAIL},
            reference=case.case_id,
        )
    )

    violations = check_case_invariants(case.case_id, deps, policy=make_policy())
    for v in violations:
        dumped = str(v.model_dump())
        assert SALARY not in dumped
        assert CANDIDATE_EMAIL not in dumped
        assert CANDIDATE_NAME not in dumped
        assert "Mismatch Name" not in dumped
