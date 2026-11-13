from sub_agent.adapters.memory import (
    InMemoryDocumentStore,
    InMemoryEmployeeNumberGenerator,
    InMemoryPersonDirectory,
)
from sub_agent.hris_prepare import prepare_hris_records
from sub_agent.hris_write import approve_hris_write, execute_hris_write
from sub_agent.models import CaseOpenedEvent, EmployeeRecordCreatedEvent, HrisRecordStatus, HrisSystem
from sub_agent.onboarding_entry import OnboardingDeps, OpenCaseRequest, open_onboarding_case

from factories import make_approved_form, make_change_deps, make_policy, make_placement


class Toggle:
    def __init__(self, value: bool = True) -> None:
        self.value = value

    def __call__(self) -> bool:
        return self.value


def _open_case(deps):
    onboarding_deps = OnboardingDeps(
        case_repo=deps.case_repo,
        form_repo=deps.form_repo,
        approval_repo=deps.approval_repo,
        person_directory=InMemoryPersonDirectory(),
        document_store=InMemoryDocumentStore(),
        number_generator=InMemoryEmployeeNumberGenerator(),
        event_publisher=deps.publisher,
        audit_log=deps.audit_log,
    )
    form = make_approved_form()
    request = OpenCaseRequest(placement=make_placement(), 
        request_id="req-hooks-001",
        client_id=form.client_id,
        hr_officer_id="hr-officer-001",
        offer_accepted=True,
        form=form,
    )
    result = open_onboarding_case(request, onboarding_deps)
    return result.case


def test_kill_switch_off_blocks_write_and_on_retry_succeeds():
    toggle = Toggle(value=False)
    deps = make_change_deps()
    deps.hris_writes_enabled = toggle
    case = _open_case(deps)

    prepare_result = prepare_hris_records(case.case_id, deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", deps)

    outcome = execute_hris_write(case.case_id, deps)

    assert outcome.blocked_code == "WRITES_DISABLED"
    assert outcome.completed is False
    assert deps.hris_gateway.written_count() == 0
    # Only the CaseOpenedEvent from opening the case; no EmployeeRecordCreatedEvent yet.
    assert all(isinstance(e, CaseOpenedEvent) for e in deps.publisher.events)

    blocked_entries = [e for e in deps.audit_log.entries() if e.action == "HRIS_WRITE_BLOCKED_BY_HOOK"]
    assert len(blocked_entries) == 1
    assert blocked_entries[0].after["code"] == "WRITES_DISABLED"

    toggle.value = True
    retry = execute_hris_write(case.case_id, deps)

    assert retry.completed is True
    assert retry.blocked_code is None
    assert deps.hris_gateway.written_count() == 2
    created_events = [e for e in deps.publisher.events if isinstance(e, EmployeeRecordCreatedEvent)]
    assert len(created_events) == 1


def test_tampered_prepared_payload_blocks_with_payload_mismatch():
    deps = make_change_deps()
    case = _open_case(deps)

    prepare_result = prepare_hris_records(case.case_id, deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", deps)

    people_record = next(
        r
        for r in deps.hris_record_repo.list_for_case(case.case_id)
        if r.system == HrisSystem.ZOHO_PEOPLE
    )
    tampered_payload = dict(people_record.prepared_payload)
    tampered_payload["job_title"] = "Forged Title"
    people_record.prepared_payload = tampered_payload
    deps.hris_record_repo.update(people_record)

    outcome = execute_hris_write(case.case_id, deps)

    assert outcome.blocked_code == "PAYLOAD_MISMATCH"
    assert deps.hris_gateway.written_count() == 0

    reloaded = deps.hris_record_repo.get(people_record.record_id)
    assert reloaded.status == HrisRecordStatus.APPROVED  # unchanged


def test_clean_write_produces_no_invariant_violations():
    deps = make_change_deps()
    deps.policy = make_policy()
    case = _open_case(deps)

    prepare_result = prepare_hris_records(case.case_id, deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", deps)
    execute_hris_write(case.case_id, deps)

    violations = [e for e in deps.audit_log.entries() if e.action == "INVARIANT_VIOLATION"]
    assert violations == []


def test_corrupted_state_produces_one_invariant_violation():
    deps = make_change_deps()
    deps.policy = make_policy()
    case = _open_case(deps)

    prepare_result = prepare_hris_records(case.case_id, deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", deps)
    execute_hris_write(case.case_id, deps)

    # Corrupt: the case's own candidate_name drifts from the in-force
    # form's, so CASE_FORM_MISMATCH fires on the next invariant check.
    corrupted_case = deps.case_repo.get(case.case_id)
    corrupted_case.candidate_name = "Drifted Name"
    deps.case_repo.save(corrupted_case)

    from sub_agent.hooks import after_hris_write

    after_hris_write(case.case_id, deps, make_policy())

    violations = [e for e in deps.audit_log.entries() if e.action == "INVARIANT_VIOLATION"]
    assert len(violations) == 1
    assert violations[0].after["code"] == "CASE_FORM_MISMATCH"
    assert "Drifted Name" not in str(violations[0].model_dump())
