import pytest

from sub_agent.compare_sources import DiscrepancyDecision, compare_sources, resolve_discrepancy
from sub_agent.errors import HrisUnavailableError
from sub_agent.form_changes import approve_form_change, propose_form_change
from sub_agent.hris_prepare import PrepareOutcome, prepare_hris_records
from sub_agent.hris_write import (
    approve_hris_write,
    execute_hris_write,
    reject_hris_write,
)
from sub_agent.models import (
    ApprovalStatus,
    CaseStatus,
    FormChangeRequest,
    HrisRecordStatus,
    HrisSystem,
    SourceSnapshot,
)

from factories import make_change_deps, make_compare_deps, seed_linked_case_and_form, make_placement

CANDIDATE_NAME = "Hris Write Candidate"
CANDIDATE_EMAIL = "hris.write.candidate@example.test"
SALARY = "Base: 145000 SYNTHETIC/yr; benefits: standard"


def _prepared_and_approved(deps=None, hris_gateway=None, **form_overrides):
    deps = deps or make_change_deps(hris_gateway=hris_gateway)
    case, _ = seed_linked_case_and_form(
        deps, candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL, **form_overrides
    )
    prepare_result = prepare_hris_records(case.case_id, deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", deps)
    return deps, case, prepare_result


def test_approve_then_execute_both_written_one_event():
    deps, case, prepare_result = _prepared_and_approved(salary_and_terms=SALARY)

    outcome = execute_hris_write(case.case_id, deps)

    assert outcome.completed is True
    assert sorted(outcome.written) == sorted(prepare_result.record_ids)
    assert outcome.failed == []
    assert deps.hris_gateway.written_count() == 2

    records = deps.hris_record_repo.list_for_case(case.case_id)
    assert all(r.status == HrisRecordStatus.WRITTEN for r in records)
    assert all(r.external_ref is not None for r in records)

    assert len(deps.publisher.events) == 1


def test_execute_before_approval_raises():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    prepare_hris_records(case.case_id, deps)  # not approved

    with pytest.raises(ValueError):
        execute_hris_write(case.case_id, deps)


def test_execute_twice_second_call_writes_nothing():
    deps, case, _ = _prepared_and_approved()
    execute_hris_write(case.case_id, deps)

    second = execute_hris_write(case.case_id, deps)

    assert second.written == []
    assert second.completed is True
    assert len(second.skipped_already_written) == 2
    assert deps.hris_gateway.written_count() == 2
    assert len(deps.publisher.events) == 1

    actions = [e.action for e in deps.audit_log.entries() if e.action == "HRIS_WRITE_SKIPPED_ALREADY_WRITTEN"]
    assert len(actions) == 1


def test_gateway_failure_on_people_record_blocks_payroll():
    deps, case, _ = _prepared_and_approved()
    deps.hris_gateway.fail_next(1)

    outcome = execute_hris_write(case.case_id, deps)

    records = {r.system: r for r in deps.hris_record_repo.list_for_case(case.case_id)}
    assert records[HrisSystem.ZOHO_PEOPLE].status == HrisRecordStatus.FAILED
    assert records[HrisSystem.ZOHO_PAYROLL].status == HrisRecordStatus.APPROVED
    assert outcome.completed is False
    assert deps.publisher.events == []

    retry = execute_hris_write(case.case_id, deps)
    assert retry.completed is True
    assert len(deps.publisher.events) == 1
    assert deps.hris_gateway.written_count() == 2


def test_gateway_failure_on_payroll_only():
    deps2, case2, _ = _prepared_and_approved()

    # The stub only supports fail_next(n) counting from the *next* call,
    # and people is always attempted first, so to fail only payroll we
    # wrap the gateway to fail specifically on the second write() call
    # within a single execute_hris_write invocation.
    class FailSecondCallGateway:
        def __init__(self, inner):
            self._inner = inner
            self._calls = 0

        def write(self, **kwargs):
            self._calls += 1
            if self._calls == 2:
                raise HrisUnavailableError("forced payroll failure")
            return self._inner.write(**kwargs)

        def written_count(self):
            return self._inner.written_count()

    deps2.hris_gateway = FailSecondCallGateway(deps2.hris_gateway)

    outcome = execute_hris_write(case2.case_id, deps2)

    records = {r.system: r for r in deps2.hris_record_repo.list_for_case(case2.case_id)}
    assert records[HrisSystem.ZOHO_PEOPLE].status == HrisRecordStatus.WRITTEN
    assert records[HrisSystem.ZOHO_PAYROLL].status == HrisRecordStatus.FAILED
    assert outcome.completed is False
    assert deps2.publisher.events == []

    retry = execute_hris_write(case2.case_id, deps2)
    assert retry.completed is True
    assert retry.written == [records[HrisSystem.ZOHO_PAYROLL].record_id]
    assert len(deps2.publisher.events) == 1


def test_gateway_receives_exact_salary_at_write_time():
    class SpyGateway:
        def __init__(self):
            self.calls = []

        def write(self, client_id, system, idempotency_key, payload, sensitive_payload):
            self.calls.append((system, dict(payload), dict(sensitive_payload)))
            return type("R", (), {"external_ref": f"ext-{len(self.calls)}", "already_existed": False})()

    spy = SpyGateway()
    deps, case, _ = _prepared_and_approved(hris_gateway=spy, salary_and_terms=SALARY)

    execute_hris_write(case.case_id, deps)

    payroll_calls = [c for c in spy.calls if c[0] == HrisSystem.ZOHO_PAYROLL]
    assert len(payroll_calls) == 1
    _, payload, sensitive_payload = payroll_calls[0]
    assert "compensation_terms" not in payload
    assert sensitive_payload["compensation_terms"].get_secret_value() == SALARY


def test_stub_gateway_never_duplicates_same_idempotency_key():
    deps = make_change_deps()
    gateway = deps.hris_gateway

    first = gateway.write(
        client_id="client-x",
        system=HrisSystem.ZOHO_PEOPLE,
        idempotency_key="case-x:ZOHO_PEOPLE:1",
        payload={"employee_number": "EMP-000001"},
        sensitive_payload={},
    )
    second = gateway.write(
        client_id="client-x",
        system=HrisSystem.ZOHO_PEOPLE,
        idempotency_key="case-x:ZOHO_PEOPLE:1",
        payload={"employee_number": "EMP-000001"},
        sensitive_payload={},
    )

    assert first.external_ref == second.external_ref
    assert second.already_existed is True
    assert gateway.written_count() == 1


def test_reject_hris_write():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    prepare_result = prepare_hris_records(case.case_id, deps)

    records = reject_hris_write(prepare_result.approval_id, "hr-manager-001", "Compensation mismatch", deps)

    assert all(r.status == HrisRecordStatus.REJECTED for r in records)
    approval = deps.approval_repo.get(prepare_result.approval_id)
    assert approval.status == ApprovalStatus.REJECTED
    assert approval.decision_note == "Compensation mismatch"

    with pytest.raises(ValueError):
        execute_hris_write(case.case_id, deps)


def test_execute_raises_when_all_records_stale():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    prepare_result = prepare_hris_records(case.case_id, deps)
    for record_id in prepare_result.record_ids:
        record = deps.hris_record_repo.get(record_id)
        record.status = HrisRecordStatus.STALE
        deps.hris_record_repo.update(record)

    with pytest.raises(ValueError):
        execute_hris_write(case.case_id, deps)


def test_execute_raises_when_all_records_rejected():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    prepare_result = prepare_hris_records(case.case_id, deps)
    reject_hris_write(prepare_result.approval_id, "hr-manager-001", "No longer needed", deps)

    with pytest.raises(ValueError):
        execute_hris_write(case.case_id, deps)


def test_approving_stale_approval_raises():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    prepare_result = prepare_hris_records(case.case_id, deps)

    change_request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Designation correction",
        designation="Senior Engineer",
    )
    propose_result = propose_form_change(change_request, deps, approver_id="hr-manager-001")
    approve_form_change(propose_result.approval_id, "hr-manager-001", deps)

    with pytest.raises(ValueError):
        approve_hris_write(prepare_result.approval_id, "hr-manager-001", deps)


def test_form_change_after_prepare_stales_records_and_cancels_approval():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps, designation="Engineer A")
    prepare_result = prepare_hris_records(case.case_id, deps)

    change_request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Designation correction",
        designation="Engineer B",
    )
    propose_result = propose_form_change(change_request, deps, approver_id="hr-manager-001")
    approve_form_change(propose_result.approval_id, "hr-manager-001", deps)

    for record_id in prepare_result.record_ids:
        record = deps.hris_record_repo.get(record_id)
        assert record.status == HrisRecordStatus.STALE

    hc05_approval = deps.approval_repo.get(prepare_result.approval_id)
    assert hc05_approval.status == ApprovalStatus.CANCELLED

    staled_entries = [e for e in deps.audit_log.entries() if e.action == "HRIS_RECORDS_STALED"]
    assert len(staled_entries) == 1

    second_prepare = prepare_hris_records(case.case_id, deps)
    assert second_prepare.outcome == PrepareOutcome.PREPARED
    assert set(second_prepare.record_ids).isdisjoint(prepare_result.record_ids)

    new_record = deps.hris_record_repo.get(second_prepare.record_ids[0])
    old_record = deps.hris_record_repo.get(prepare_result.record_ids[0])
    assert new_record.idempotency_key != old_record.idempotency_key


def test_no_pii_or_salary_in_event_audit_and_last_error():
    deps, case, _ = _prepared_and_approved(salary_and_terms=SALARY)
    deps.hris_gateway.fail_next(1)
    execute_hris_write(case.case_id, deps)  # people fails
    execute_hris_write(case.case_id, deps)  # retry succeeds

    event = deps.publisher.events[0]
    event_dump = str(event.model_dump())
    assert CANDIDATE_NAME not in event_dump
    assert CANDIDATE_EMAIL not in event_dump
    assert SALARY not in event_dump

    for entry in deps.audit_log.entries():
        dumped = str(entry.model_dump())
        assert CANDIDATE_NAME not in dumped
        assert CANDIDATE_EMAIL not in dumped
        assert SALARY not in dumped

    for record in deps.hris_record_repo.list_for_case(case.case_id):
        if record.last_error is not None:
            assert record.last_error == "HrisUnavailableError"
            assert SALARY not in record.last_error
            assert CANDIDATE_NAME not in record.last_error


def test_full_flow_open_prepare_blocked_resolve_prepare_approve_execute():
    from sub_agent.onboarding_entry import OpenCaseRequest, open_onboarding_case
    from factories import make_approved_form, make_deps

    onboarding_deps = make_deps()
    form = make_approved_form(
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
        designation="Engineer A",
    )
    open_request = OpenCaseRequest(placement=make_placement(), 
        request_id="req-full-hris-001",
        client_id=form.client_id,
        hr_officer_id="hr-officer-001",
        offer_accepted=True,
        form=form,
    )
    open_result = open_onboarding_case(open_request, onboarding_deps)
    case = open_result.case
    assert case is not None

    deps = make_change_deps()
    deps.case_repo = onboarding_deps.case_repo
    deps.form_repo = onboarding_deps.form_repo

    compare_deps = make_compare_deps()
    compare_deps.discrepancy_repo = deps.discrepancy_repo
    compare_deps.approval_repo = deps.approval_repo

    in_force_form = onboarding_deps.form_repo.latest_approved(case.case_id)
    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Engineer B"})
    compare_result = compare_sources(case, in_force_form, [snapshot], compare_deps)
    discrepancy_id = compare_result.new_discrepancies[0]

    blocked = prepare_hris_records(case.case_id, deps)
    assert blocked.outcome == PrepareOutcome.BLOCKED_OPEN_DISCREPANCIES

    resolve_discrepancy(discrepancy_id, DiscrepancyDecision.KEEP_FORM_VALUE, "hr-officer-001", compare_deps)

    prepare_result = prepare_hris_records(case.case_id, deps)
    assert prepare_result.outcome == PrepareOutcome.PREPARED

    approve_hris_write(prepare_result.approval_id, "hr-manager-001", deps)
    outcome = execute_hris_write(case.case_id, deps)

    assert outcome.completed is True
    assert len(deps.publisher.events) == 1


@pytest.mark.parametrize("status", [CaseStatus.ON_HOLD, CaseStatus.WITHDRAWN, CaseStatus.CLOSED])
def test_execute_raises_for_inactive_case(status):
    deps, case, prepare_result = _prepared_and_approved()
    case2 = deps.case_repo.get(case.case_id)
    case2.status = status
    deps.case_repo.save(case2)

    with pytest.raises(ValueError):
        execute_hris_write(case.case_id, deps)

    assert deps.hris_gateway.written_count() == 0
