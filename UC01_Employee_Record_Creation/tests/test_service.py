import pytest

from sub_agent.adapters.memory import (
    InMemoryDocumentStore,
    InMemoryEmployeeNumberGenerator,
    InMemoryPersonDirectory,
)
from sub_agent.compare_sources import DiscrepancyDecision
from sub_agent.errors import CaseNotFoundError, PolicyViolationCode, PolicyViolationError
from sub_agent.models import EmployeeRecordCreatedEvent, FormChangeRequest, Role, SourceSnapshot
from sub_agent.onboarding_entry import OnboardingDeps, OpenCaseRequest, open_onboarding_case

from factories import make_approved_form, make_case_service, make_change_deps, make_viewer, make_placement

CANDIDATE_NAME = "Service Test Candidate"
CANDIDATE_EMAIL = "service.test.candidate@example.test"
SALARY = "Base: 170000 SYNTHETIC/yr; benefits: standard"


def _open_case_with_service(designation="Engineer A"):
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
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
        salary_and_terms=SALARY,
        designation=designation,
    )
    open_request = OpenCaseRequest(placement=make_placement(), 
        request_id="req-service-001",
        client_id=form.client_id,
        hr_officer_id="hr-officer-001",
        offer_accepted=True,
        form=form,
    )
    result = open_onboarding_case(open_request, onboarding_deps)
    service = make_case_service(change_deps)
    return change_deps, service, result.case


def _viewers(case):
    hr_officer = make_viewer(role=Role.HR_OFFICER, user_id="hr-officer-001", client_ids=frozenset({case.client_id}))
    hr_manager = make_viewer(role=Role.HR_MANAGER, user_id="hr-manager-001", client_ids=frozenset({case.client_id}))
    client_user = make_viewer(role=Role.CLIENT_USER, user_id="client-user-001", client_ids=frozenset({case.client_id}))
    return hr_officer, hr_manager, client_user


def test_full_happy_path_through_case_service_only():
    change_deps, service, case = _open_case_with_service()
    hr_officer, hr_manager, _ = _viewers(case)

    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Engineer B"})
    compare_result = service.compare_sources(hr_officer, case.case_id, [snapshot])
    discrepancy_id = compare_result.new_discrepancies[0]

    service.resolve_discrepancy(hr_officer, discrepancy_id, DiscrepancyDecision.KEEP_FORM_VALUE)

    prepare_result = service.prepare_hris(hr_officer, case.case_id)

    service.decide_hris_write(hr_manager, prepare_result.approval_id, approve=True)

    outcome = service.execute_hris_write(hr_manager, case.case_id)
    assert outcome.completed is True

    status = service.get_status(hr_manager, case.case_id)
    assert status.stage.value == "RECORD_CREATED"

    created_events = [e for e in change_deps.publisher.events if isinstance(e, EmployeeRecordCreatedEvent)]
    assert len(created_events) == 1


def test_hr_officer_cannot_execute_hris_write():
    change_deps, service, case = _open_case_with_service()
    hr_officer, hr_manager, _ = _viewers(case)

    prepare_result = service.prepare_hris(hr_officer, case.case_id)
    service.decide_hris_write(hr_manager, prepare_result.approval_id, approve=True)

    with pytest.raises(PolicyViolationError) as exc_info:
        service.execute_hris_write(hr_officer, case.case_id)
    assert exc_info.value.code == PolicyViolationCode.ROLE_NOT_ALLOWED


def test_client_user_cannot_decide_hc05_or_resolve_discrepancy():
    change_deps, service, case = _open_case_with_service()
    hr_officer, hr_manager, client_user = _viewers(case)

    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Engineer B"})
    compare_result = service.compare_sources(hr_officer, case.case_id, [snapshot])
    discrepancy_id = compare_result.new_discrepancies[0]

    with pytest.raises(PolicyViolationError):
        service.resolve_discrepancy(client_user, discrepancy_id, DiscrepancyDecision.KEEP_FORM_VALUE)

    service.resolve_discrepancy(hr_officer, discrepancy_id, DiscrepancyDecision.KEEP_FORM_VALUE)
    prepare_result = service.prepare_hris(hr_officer, case.case_id)

    with pytest.raises(PolicyViolationError):
        service.decide_hris_write(client_user, prepare_result.approval_id, approve=True)


def test_audit_entries_have_viewer_user_id_as_actor_for_human_decisions():
    change_deps, service, case = _open_case_with_service()
    hr_officer, hr_manager, _ = _viewers(case)

    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Engineer B"})
    compare_result = service.compare_sources(hr_officer, case.case_id, [snapshot])
    discrepancy_id = compare_result.new_discrepancies[0]
    service.resolve_discrepancy(hr_officer, discrepancy_id, DiscrepancyDecision.KEEP_FORM_VALUE)

    prepare_result = service.prepare_hris(hr_officer, case.case_id)
    service.decide_hris_write(hr_manager, prepare_result.approval_id, approve=True)
    service.execute_hris_write(hr_manager, case.case_id)

    human_entries = [e for e in change_deps.audit_log.entries() if e.actor_type.value == "HUMAN"]
    assert any(e.actor_id == "hr-officer-001" for e in human_entries)
    assert any(e.actor_id == "hr-manager-001" for e in human_entries)
    for entry in human_entries:
        assert entry.actor_id in ("hr-officer-001", "hr-manager-001")


def test_denied_call_changes_nothing():
    change_deps, service, case = _open_case_with_service()
    _, _, client_user = _viewers(case)

    records_before = len(change_deps.hris_record_repo.list_for_case(case.case_id))
    written_before = change_deps.hris_gateway.written_count()
    events_before = len(change_deps.publisher.events)

    with pytest.raises(PolicyViolationError):
        service.prepare_hris(client_user, case.case_id)

    records_after = len(change_deps.hris_record_repo.list_for_case(case.case_id))
    written_after = change_deps.hris_gateway.written_count()
    events_after = len(change_deps.publisher.events)

    assert records_before == records_after == 0
    assert written_before == written_after == 0
    assert events_before == events_after


def test_no_pii_in_policy_or_access_denied_entries():
    change_deps, service, case = _open_case_with_service()
    _, _, client_user = _viewers(case)
    hr_officer, hr_manager, _ = _viewers(case)

    with pytest.raises(PolicyViolationError):
        service.prepare_hris(client_user, case.case_id)

    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Engineer B"})
    compare_result = service.compare_sources(hr_officer, case.case_id, [snapshot])
    discrepancy_id = compare_result.new_discrepancies[0]
    service.resolve_discrepancy(hr_officer, discrepancy_id, DiscrepancyDecision.KEEP_FORM_VALUE)
    prepare_result = service.prepare_hris(hr_officer, case.case_id)

    with pytest.raises(PolicyViolationError):
        service.decide_hris_write(client_user, prepare_result.approval_id, approve=True)

    denial_entries = [
        e for e in change_deps.audit_log.entries() if e.action in ("ACCESS_DENIED", "POLICY_DENIED")
    ]
    assert len(denial_entries) >= 2
    for entry in denial_entries:
        dumped = str(entry.model_dump())
        assert SALARY not in dumped
        assert CANDIDATE_NAME not in dumped
        assert CANDIDATE_EMAIL not in dumped


def test_propose_form_change_requester_mismatch():
    change_deps, service, case = _open_case_with_service()
    hr_officer, hr_manager, _ = _viewers(case)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="someone-else",
        change_reason="Designation correction",
        designation="Senior Engineer",
    )

    with pytest.raises(PolicyViolationError) as exc_info:
        service.propose_form_change(hr_officer, request, approver_id="hr-manager-001")
    assert exc_info.value.code == PolicyViolationCode.REQUESTER_MISMATCH


def test_propose_form_change_requester_match_succeeds():
    change_deps, service, case = _open_case_with_service()
    hr_officer, hr_manager, _ = _viewers(case)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by=hr_officer.user_id,
        change_reason="Designation correction",
        designation="Senior Engineer",
    )
    propose_result = service.propose_form_change(hr_officer, request, approver_id="hr-manager-001")
    assert propose_result.new_version == 2


def test_resolve_approval_unknown_approval_other_client_and_unassigned_officer_give_identical_error():
    change_deps, service, case = _open_case_with_service()
    hr_officer, hr_manager, _ = _viewers(case)

    other_client_viewer = make_viewer(role=Role.HR_MANAGER, user_id="hr-manager-999", client_ids=frozenset({"client-other-999"}))
    unassigned_officer = make_viewer(role=Role.HR_OFFICER, user_id="hr-officer-999", client_ids=frozenset({case.client_id}))

    errors = []
    for viewer, approval_id in (
        (hr_manager, "not-a-real-approval-id"),
        (other_client_viewer, "not-a-real-approval-id"),
        (unassigned_officer, "not-a-real-approval-id"),
    ):
        with pytest.raises(CaseNotFoundError) as exc_info:
            service.decide_form_change(viewer, approval_id, approve=True)
        errors.append(exc_info.value)

    assert len({type(e) for e in errors}) == 1
    assert len({str(e) for e in errors}) == 1


def test_self_approval_of_hc05_denied_different_manager_succeeds():
    change_deps, service, case = _open_case_with_service()
    hr_officer, hr_manager, _ = _viewers(case)
    other_manager = make_viewer(role=Role.HR_MANAGER, user_id="hr-manager-002", client_ids=frozenset({case.client_id}))

    prepare_result = service.prepare_hris(hr_manager, case.case_id)  # requested_by = hr-manager-001

    with pytest.raises(PolicyViolationError) as exc_info:
        service.decide_hris_write(hr_manager, prepare_result.approval_id, approve=True)
    assert exc_info.value.code == PolicyViolationCode.SELF_APPROVAL

    records = service.decide_hris_write(other_manager, prepare_result.approval_id, approve=True)
    assert len(records) == 2


def test_checkpoint_mismatch_gives_case_not_found_for_each_decision_method():
    change_deps, service, case = _open_case_with_service()
    hr_officer, hr_manager, _ = _viewers(case)

    # An HC_04_DATA approval (from compare_sources) used where HC_01/HC_05/
    # HC_12 approvals are expected.
    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Engineer B"})
    compare_result = service.compare_sources(hr_officer, case.case_id, [snapshot])
    discrepancy_id = compare_result.new_discrepancies[0]
    wrong_approval_id = next(
        a.approval_id
        for a in change_deps.approval_repo.list_for_case(case.case_id)
        if a.subject_ref == f"discrepancy:{discrepancy_id}"
    )

    with pytest.raises(CaseNotFoundError):
        service.decide_form_change(hr_manager, wrong_approval_id, approve=True)
    with pytest.raises(CaseNotFoundError):
        service.decide_hris_write(hr_manager, wrong_approval_id, approve=True)
    with pytest.raises(CaseNotFoundError):
        service.decide_variant(hr_officer, wrong_approval_id, approve=True)

    denied = [e for e in change_deps.audit_log.entries() if e.action == "ACCESS_DENIED"]
    assert len(denied) == 3


def test_list_pending_approvals_and_decide_hris_write_are_typed_lists():
    change_deps, service, case = _open_case_with_service()
    hr_officer, hr_manager, _ = _viewers(case)

    prepare_result = service.prepare_hris(hr_officer, case.case_id)
    worklist = service.list_pending_approvals(hr_officer)  # requested_to = case.hr_officer_id
    assert any(a.approval_id == prepare_result.approval_id for a in worklist)

    records = service.decide_hris_write(hr_manager, prepare_result.approval_id, approve=True)
    assert all(hasattr(r, "record_id") for r in records)


def test_decide_form_change_returns_form_decision_result():
    change_deps, service, case = _open_case_with_service()
    hr_officer, hr_manager, _ = _viewers(case)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by=hr_officer.user_id,
        change_reason="Designation correction",
        designation="Senior Engineer",
    )
    propose_result = service.propose_form_change(hr_officer, request, approver_id="hr-manager-001")

    result = service.decide_form_change(hr_manager, propose_result.approval_id, approve=True)
    assert result.approved is True
    assert result.form_version == 2
    assert result.case_id == case.case_id
