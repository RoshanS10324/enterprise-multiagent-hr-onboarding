from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from sub_agent.compare_sources import DiscrepancyDecision, compare_sources, resolve_discrepancy
from sub_agent.form_changes import approve_form_change, propose_form_change, reject_form_change
from sub_agent.models import (
    ActorType,
    ApprovalStatus,
    DiscrepancyStatus,
    FormChangeRequest,
    FormStatus,
    SourceSnapshot,
)

from factories import TEST_TODAY, make_change_deps, make_compare_deps, seed_linked_case_and_form, make_placement

CANDIDATE_NAME = "Form Change Candidate"
SALARY = "Base: 130000 SYNTHETIC/yr; benefits: standard"


def _next_saturday() -> date:
    days_ahead = (5 - TEST_TODAY.weekday()) % 7
    days_ahead = days_ahead or 7
    return TEST_TODAY + timedelta(days=days_ahead)


def test_propose_creates_v2_awaiting_approval_with_one_approval():
    deps = make_change_deps()
    case, form = seed_linked_case_and_form(deps, candidate_name=CANDIDATE_NAME)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Designation correction",
        designation="Senior Engineer",
    )
    result = propose_form_change(request, deps, approver_id="hr-manager-001")

    assert result.new_version == 2
    assert result.changed_fields == ["designation"]
    assert result.warnings == []

    v2 = deps.form_repo.get(case.case_id, 2)
    assert v2.status == FormStatus.AWAITING_APPROVAL

    v1 = deps.form_repo.get(case.case_id, 1)
    assert v1.status == FormStatus.APPROVED  # still in force

    approvals = deps.approval_repo.list_for_case(case.case_id)
    assert len(approvals) == 1
    assert approvals[0].subject_ref == f"form:{result.new_form_id}"
    assert approvals[0].checkpoint.value == "HC_01_FORM"
    assert approvals[0].status == ApprovalStatus.PENDING

    reloaded_case = deps.case_repo.get(case.case_id)
    assert reloaded_case.current_form_version == 1  # Case untouched

    assert deps.publisher.events == []


def test_approve_supersedes_v1_and_updates_case_and_publishes_event():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(
        deps, candidate_name=CANDIDATE_NAME, salary_and_terms=SALARY
    )

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Role title update",
        designation="Senior Engineer",
    )
    propose_result = propose_form_change(request, deps, approver_id="hr-manager-001")

    updated_case = approve_form_change(propose_result.approval_id, "hr-manager-001", deps)

    v2 = deps.form_repo.get(case.case_id, 2)
    v1 = deps.form_repo.get(case.case_id, 1)
    assert v2.status == FormStatus.APPROVED
    assert v1.status == FormStatus.SUPERSEDED
    assert updated_case.current_form_version == 2

    events = deps.publisher.events
    assert len(events) == 1
    event = events[0]
    assert event.old_version == 1
    assert event.new_version == 2
    assert event.changed_fields == ["designation"]
    assert event.joining_date_changed is False

    event_dump = str(event.model_dump())
    assert CANDIDATE_NAME not in event_dump
    assert SALARY not in event_dump

    actions = [e.action for e in deps.audit_log.entries()]
    assert "FORM_CHANGE_PROPOSED" in actions
    assert "FORM_CHANGE_APPROVED" in actions
    assert "FORM_SUPERSEDED" in actions

    for entry in deps.audit_log.entries():
        dumped = str(entry.model_dump())
        assert CANDIDATE_NAME not in dumped
        assert SALARY not in dumped

    approval = deps.approval_repo.get(propose_result.approval_id)
    approval_dump = str(approval.model_dump())
    assert CANDIDATE_NAME not in approval_dump
    assert SALARY not in approval_dump


def test_salary_change_works_and_is_redacted_everywhere():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps, salary_and_terms=SALARY)
    new_salary = "Base: 150000 SYNTHETIC/yr; benefits: platinum"

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Salary revision approved by finance",
        salary_and_terms=new_salary,
    )
    propose_result = propose_form_change(request, deps, approver_id="hr-manager-001")
    assert "salary_and_terms" in propose_result.changed_fields

    approve_form_change(propose_result.approval_id, "hr-manager-001", deps)

    v2 = deps.form_repo.get(case.case_id, 2)
    assert v2.salary_and_terms.get_secret_value() == new_salary

    for entry in deps.audit_log.entries():
        dumped = str(entry.model_dump())
        assert SALARY not in dumped
        assert new_salary not in dumped

    approval = deps.approval_repo.get(propose_result.approval_id)
    approval_dump = str(approval.model_dump())
    assert SALARY not in approval_dump
    assert new_salary not in approval_dump

    event_dump = str(deps.publisher.events[0].model_dump())
    assert SALARY not in event_dump
    assert new_salary not in event_dump


def test_joining_date_warnings():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)

    saturday = _next_saturday()
    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Joining date pushed",
        joining_date=saturday,
    )
    result = propose_form_change(request, deps, approver_id="hr-manager-001")
    assert "joining date falls on a weekend" in result.warnings


def test_joining_date_holiday_warning():
    from sub_agent.adapters.memory import InMemoryHolidayCalendar

    calendar = InMemoryHolidayCalendar()
    deps = make_change_deps(holiday_calendar=calendar)
    case, _ = seed_linked_case_and_form(deps)

    holiday = date(2026, 6, 10)
    calendar.add(case.client_id, holiday)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Joining date moved to a holiday",
        joining_date=holiday,
    )
    result = propose_form_change(request, deps, approver_id="hr-manager-001")
    assert "joining date falls on a holiday" in result.warnings


def test_joining_date_in_past_warning():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    past_date = date(2020, 1, 1)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Backdated joining date",
        joining_date=past_date,
    )
    result = propose_form_change(request, deps, approver_id="hr-manager-001")
    assert "joining date is in the past" in result.warnings
    # Still proposed, not blocked.
    assert result.new_version == 2


def test_joining_date_equal_to_today_is_not_in_the_past():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Joining date moved to today",
        joining_date=TEST_TODAY,
    )
    result = propose_form_change(request, deps, approver_id="hr-manager-001")
    assert "joining date is in the past" not in result.warnings


def test_form_change_proposed_audit_records_requester():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-007",
        change_reason="Designation correction",
        designation="Senior Engineer",
    )
    propose_result = propose_form_change(request, deps, approver_id="hr-manager-001")

    entries = [e for e in deps.audit_log.entries() if e.action == "FORM_CHANGE_PROPOSED"]
    assert len(entries) == 1
    assert entries[0].actor_type == ActorType.HUMAN
    assert entries[0].actor_id == "hr-officer-007"

    approval = deps.approval_repo.get(propose_result.approval_id)
    assert approval.requested_by == "hr-officer-007"


def test_no_op_change_raises():
    deps = make_change_deps()
    case, form = seed_linked_case_and_form(deps, designation="Software Engineer")

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="No real change",
        designation=form.designation,
    )
    with pytest.raises(ValueError):
        propose_form_change(request, deps, approver_id="hr-manager-001")


def test_empty_change_reason_raises():
    with pytest.raises(ValueError):
        FormChangeRequest(
            case_id="case-001",
            requested_by="hr-officer-001",
            change_reason="   ",
            designation="Senior Engineer",
        )


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        FormChangeRequest(
            case_id="case-001",
            requested_by="hr-officer-001",
            change_reason="Trying to change email",
            candidate_email="new.email@example.test",
        )


def test_second_proposal_while_pending_raises():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="First change",
        designation="Senior Engineer",
    )
    propose_form_change(request, deps, approver_id="hr-manager-001")

    second_request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Second change",
        designation="Staff Engineer",
    )
    with pytest.raises(ValueError):
        propose_form_change(second_request, deps, approver_id="hr-manager-001")


def test_reject_leaves_case_and_v1_unchanged_and_publishes_no_event():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Proposed then rejected",
        designation="Senior Engineer",
    )
    propose_result = propose_form_change(request, deps, approver_id="hr-manager-001")

    rejected_form = reject_form_change(
        propose_result.approval_id, "hr-manager-001", "Not approved by finance", deps
    )

    assert rejected_form.status == FormStatus.REJECTED
    v1 = deps.form_repo.get(case.case_id, 1)
    assert v1.status == FormStatus.APPROVED
    reloaded_case = deps.case_repo.get(case.case_id)
    assert reloaded_case.current_form_version == 1
    assert deps.publisher.events == []

    approval = deps.approval_repo.get(propose_result.approval_id)
    assert approval.status == ApprovalStatus.REJECTED
    assert approval.decision_note == "Not approved by finance"
    assert approval.reason != "Not approved by finance"  # reason describes the original proposal

    entries = deps.audit_log.entries()
    rejected_entry = next(e for e in entries if e.action == "FORM_CHANGE_REJECTED")
    assert "Not approved by finance" not in str(rejected_entry.model_dump())


def test_approve_twice_raises():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Change",
        designation="Senior Engineer",
    )
    propose_result = propose_form_change(request, deps, approver_id="hr-manager-001")
    approve_form_change(propose_result.approval_id, "hr-manager-001", deps)

    with pytest.raises(ValueError):
        approve_form_change(propose_result.approval_id, "hr-manager-001", deps)


def test_approve_unknown_id_raises():
    deps = make_change_deps()
    with pytest.raises(ValueError):
        approve_form_change("not-a-real-id", "hr-manager-001", deps)


def test_client_scoping_form_lookup():
    deps = make_change_deps()
    case_a, _ = seed_linked_case_and_form(deps, client_id="client-aaa")
    case_b, _ = seed_linked_case_and_form(deps, client_id="client-bbb")

    form_for_a = deps.form_repo.get(case_a.case_id, 1)
    form_for_b = deps.form_repo.get(case_b.case_id, 1)

    assert form_for_a.client_id == "client-aaa"
    assert form_for_b.client_id == "client-bbb"
    # Looking up case B's id never returns case A's form, and vice versa.
    assert deps.form_repo.get(case_b.case_id, 1).form_id != form_for_a.form_id


def test_stale_discrepancy_on_changed_field_and_untouched_on_other_field():
    deps = make_change_deps()
    case, form = seed_linked_case_and_form(deps, designation="Engineer A")
    compare_deps = make_compare_deps()
    compare_deps.discrepancy_repo = deps.discrepancy_repo
    compare_deps.approval_repo = deps.approval_repo

    designation_snapshot = SourceSnapshot(source="hr_system", values={"designation": "Engineer B"})
    name_snapshot = SourceSnapshot(
        source="hr_system", values={"candidate_name": "A Totally Different Name"}
    )
    compare_result = compare_sources(case, form, [designation_snapshot, name_snapshot], compare_deps)
    assert len(compare_result.new_discrepancies) == 2

    designation_discrepancy_id = next(
        d.discrepancy_id
        for d in deps.discrepancy_repo.list_for_case(case.case_id)
        if d.field == "designation"
    )
    name_discrepancy_id = next(
        d.discrepancy_id
        for d in deps.discrepancy_repo.list_for_case(case.case_id)
        if d.field == "candidate_name"
    )

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Fixing designation",
        designation="Engineer C",
    )
    propose_result = propose_form_change(request, deps, approver_id="hr-manager-001")
    approve_form_change(propose_result.approval_id, "hr-manager-001", deps)

    designation_discrepancy = deps.discrepancy_repo.get(designation_discrepancy_id)
    name_discrepancy = deps.discrepancy_repo.get(name_discrepancy_id)

    assert designation_discrepancy.status == DiscrepancyStatus.STALE
    assert name_discrepancy.status == DiscrepancyStatus.OPEN  # untouched field

    designation_approval = next(
        a
        for a in deps.approval_repo.list_for_case(case.case_id)
        if a.subject_ref == f"discrepancy:{designation_discrepancy_id}"
    )
    name_approval = next(
        a
        for a in deps.approval_repo.list_for_case(case.case_id)
        if a.subject_ref == f"discrepancy:{name_discrepancy_id}"
    )
    assert designation_approval.status == ApprovalStatus.CANCELLED
    assert name_approval.status == ApprovalStatus.PENDING


def test_source_discrepancy_id_accepted_when_valid():
    deps = make_change_deps()
    case, form = seed_linked_case_and_form(deps, designation="Engineer A")
    compare_deps = make_compare_deps()
    compare_deps.discrepancy_repo = deps.discrepancy_repo
    compare_deps.approval_repo = deps.approval_repo

    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Engineer B"})
    compare_result = compare_sources(case, form, [snapshot], compare_deps)
    discrepancy_id = compare_result.new_discrepancies[0]

    resolve_discrepancy(discrepancy_id, DiscrepancyDecision.ACCEPT_AS_CHANGE, "hr-officer-001", compare_deps)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Accepted HR data as the real designation",
        designation="Engineer B",
        source_discrepancy_id=discrepancy_id,
    )
    result = propose_form_change(request, deps, approver_id="hr-manager-001")
    assert result.changed_fields == ["designation"]


def test_source_discrepancy_id_open_raises():
    deps = make_change_deps()
    case, form = seed_linked_case_and_form(deps, designation="Engineer A")
    compare_deps = make_compare_deps()
    compare_deps.discrepancy_repo = deps.discrepancy_repo
    compare_deps.approval_repo = deps.approval_repo

    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Engineer B"})
    compare_result = compare_sources(case, form, [snapshot], compare_deps)
    discrepancy_id = compare_result.new_discrepancies[0]  # still OPEN

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Trying to use an open discrepancy",
        designation="Engineer B",
        source_discrepancy_id=discrepancy_id,
    )
    with pytest.raises(ValueError):
        propose_form_change(request, deps, approver_id="hr-manager-001")


def test_source_discrepancy_id_other_case_raises():
    deps = make_change_deps()
    case_a, form_a = seed_linked_case_and_form(deps, designation="Engineer A")
    case_b, _ = seed_linked_case_and_form(deps, designation="Engineer Z")
    compare_deps = make_compare_deps()
    compare_deps.discrepancy_repo = deps.discrepancy_repo
    compare_deps.approval_repo = deps.approval_repo

    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Engineer B"})
    compare_result = compare_sources(case_a, form_a, [snapshot], compare_deps)
    discrepancy_id = compare_result.new_discrepancies[0]
    resolve_discrepancy(discrepancy_id, DiscrepancyDecision.ACCEPT_AS_CHANGE, "hr-officer-001", compare_deps)

    request = FormChangeRequest(
        case_id=case_b.case_id,
        requested_by="hr-officer-001",
        change_reason="Using another case's discrepancy",
        designation="Engineer B",
        source_discrepancy_id=discrepancy_id,
    )
    with pytest.raises(ValueError):
        propose_form_change(request, deps, approver_id="hr-manager-001")


def test_source_discrepancy_id_different_field_raises():
    deps = make_change_deps()
    case, form = seed_linked_case_and_form(deps, designation="Engineer A")
    compare_deps = make_compare_deps()
    compare_deps.discrepancy_repo = deps.discrepancy_repo
    compare_deps.approval_repo = deps.approval_repo

    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Engineer B"})
    compare_result = compare_sources(case, form, [snapshot], compare_deps)
    discrepancy_id = compare_result.new_discrepancies[0]
    resolve_discrepancy(discrepancy_id, DiscrepancyDecision.ACCEPT_AS_CHANGE, "hr-officer-001", compare_deps)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Changing a different field than the discrepancy's",
        client_site_id="site-999",
        work_location_type="CLIENT_SITE",
        source_discrepancy_id=discrepancy_id,
    )
    with pytest.raises(ValueError):
        propose_form_change(request, deps, approver_id="hr-manager-001")


def test_full_flow_open_case_compare_accept_propose_approve_recompare():
    from sub_agent.onboarding_entry import OpenCaseRequest, open_onboarding_case
    from factories import make_approved_form, make_deps

    onboarding_deps = make_deps()
    form = make_approved_form(designation="Engineer A")
    open_request = OpenCaseRequest(placement=make_placement(), 
        request_id="req-full-001",
        client_id=form.client_id,
        hr_officer_id="hr-officer-001",
        offer_accepted=True,
        form=form,
    )
    open_result = open_onboarding_case(open_request, onboarding_deps)
    case = open_result.case
    assert case is not None

    change_deps = make_change_deps()
    change_deps.case_repo = onboarding_deps.case_repo
    change_deps.form_repo = onboarding_deps.form_repo

    compare_deps = make_compare_deps()
    compare_deps.discrepancy_repo = change_deps.discrepancy_repo
    compare_deps.approval_repo = change_deps.approval_repo

    in_force_form = onboarding_deps.form_repo.latest_approved(case.case_id)
    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Engineer B"})
    compare_result = compare_sources(case, in_force_form, [snapshot], compare_deps)
    discrepancy_id = compare_result.new_discrepancies[0]

    resolve_discrepancy(discrepancy_id, DiscrepancyDecision.ACCEPT_AS_CHANGE, "hr-officer-001", compare_deps)

    request = FormChangeRequest(
        case_id=case.case_id,
        requested_by="hr-officer-001",
        change_reason="Matching HR system designation",
        designation="Engineer B",
        source_discrepancy_id=discrepancy_id,
    )
    propose_result = propose_form_change(request, change_deps, approver_id="hr-manager-001")
    approve_form_change(propose_result.approval_id, "hr-manager-001", change_deps)

    updated_form = onboarding_deps.form_repo.latest_approved(case.case_id)
    assert updated_form.designation == "Engineer B"

    recompare = compare_sources(case, updated_form, [snapshot], compare_deps)
    assert recompare.new_discrepancies == []
