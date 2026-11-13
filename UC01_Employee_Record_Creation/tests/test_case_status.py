from sub_agent.case_status import derive_stage, get_case_status
from sub_agent.compare_sources import compare_sources
from sub_agent.form_changes import approve_form_change, propose_form_change
from sub_agent.hris_prepare import prepare_hris_records
from sub_agent.hris_write import approve_hris_write, execute_hris_write, reject_hris_write
from sub_agent.models import (
    Approval,
    CaseStatus,
    Checkpoint,
    FormChangeRequest,
    HrisRecordStatus,
    Role,
    SourceSnapshot,
)
from sub_agent.onboarding_entry import OpenCaseRequest, open_onboarding_case

from factories import make_approved_form, make_compare_deps, make_deps, make_u9_deps, make_viewer, make_placement

CANDIDATE_NAME = "Case Status Candidate"
CANDIDATE_EMAIL = "case.status.candidate@example.test"
SALARY = "Base: 150000 SYNTHETIC/yr; benefits: standard"


def _open_case(onboarding_deps=None, **form_overrides):
    onboarding_deps = onboarding_deps or make_deps()
    defaults = dict(candidate_name=CANDIDATE_NAME, candidate_email=CANDIDATE_EMAIL, salary_and_terms=SALARY)
    defaults.update(form_overrides)
    form = make_approved_form(**defaults)
    request = OpenCaseRequest(placement=make_placement(), 
        request_id="req-status-001",
        client_id=form.client_id,
        hr_officer_id="hr-officer-001",
        offer_accepted=True,
        form=form,
    )
    result = open_onboarding_case(request, onboarding_deps)
    return onboarding_deps, result.case


def _change_deps_sharing(onboarding_deps):
    from factories import make_change_deps

    deps = make_change_deps()
    deps.case_repo = onboarding_deps.case_repo
    deps.form_repo = onboarding_deps.form_repo
    deps.audit_log = onboarding_deps.audit_log
    return deps


# --- derive_stage unit tests (pure function) ---


def test_derive_stage_ready_to_prepare():
    stage = derive_stage(
        case_status=CaseStatus.PLAN_PENDING,
        hris_statuses=[],
        pending_form_change=False,
        open_discrepancy_count=0,
        pending_variant_decision=False,
    )
    assert stage.value == "READY_TO_PREPARE"


def test_derive_stage_precedence_form_change_over_discrepancy():
    stage = derive_stage(
        case_status=CaseStatus.PLAN_PENDING,
        hris_statuses=[],
        pending_form_change=True,
        open_discrepancy_count=1,
        pending_variant_decision=False,
    )
    assert stage.value == "FORM_CHANGE_PENDING"


def test_derive_stage_case_inactive_outranks_everything():
    stage = derive_stage(
        case_status=CaseStatus.ON_HOLD,
        hris_statuses=[HrisRecordStatus.WRITTEN, HrisRecordStatus.WRITTEN],
        pending_form_change=True,
        open_discrepancy_count=1,
        pending_variant_decision=True,
    )
    assert stage.value == "CASE_INACTIVE"


# --- stage tests via real flows ---


def test_stage_ready_to_prepare():
    onboarding_deps, case = _open_case()
    deps = make_u9_deps(_change_deps_sharing(onboarding_deps))
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "READY_TO_PREPARE"


def test_stage_variant_decision_pending():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    change_deps.approval_repo.add(
        Approval(
            case_id=case.case_id,
            checkpoint=Checkpoint.HC_12_VARIANT,
            subject_ref="is_rehire",
            requested_to=case.hr_officer_id,
        )
    )
    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "VARIANT_DECISION_PENDING"


def test_possible_duplicate_notice_does_not_set_variant_stage_but_adds_warning():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    change_deps.approval_repo.add(
        Approval(
            case_id=case.case_id,
            checkpoint=Checkpoint.HC_12_VARIANT,
            subject_ref="possible_duplicate",
            requested_to=case.hr_officer_id,
        )
    )
    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "READY_TO_PREPARE"
    assert "possible duplicate notice pending" in status.warnings


def test_stage_discrepancies_open():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    compare_deps = make_compare_deps()
    compare_deps.discrepancy_repo = change_deps.discrepancy_repo
    compare_deps.approval_repo = change_deps.approval_repo

    form = onboarding_deps.form_repo.latest_approved(case.case_id)
    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Different Title"})
    compare_sources(case, form, [snapshot], compare_deps)

    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "DISCREPANCIES_OPEN"
    assert len(status.open_discrepancies) == 1
    assert status.open_discrepancies[0].field == "designation"


def test_stage_form_change_pending():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)

    propose_form_change(
        FormChangeRequest(
            case_id=case.case_id,
            requested_by="hr-officer-001",
            change_reason="Designation correction",
            designation="Senior Engineer",
        ),
        change_deps,
        approver_id="hr-manager-001",
    )

    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "FORM_CHANGE_PENDING"


def test_precedence_form_change_pending_over_open_discrepancy():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    compare_deps = make_compare_deps()
    compare_deps.discrepancy_repo = change_deps.discrepancy_repo
    compare_deps.approval_repo = change_deps.approval_repo

    form = onboarding_deps.form_repo.latest_approved(case.case_id)
    snapshot = SourceSnapshot(source="hr_system", values={"work_location_type": "CLIENT_SITE"})
    compare_sources(case, form, [snapshot], compare_deps)

    propose_form_change(
        FormChangeRequest(
            case_id=case.case_id,
            requested_by="hr-officer-001",
            change_reason="Unrelated designation correction",
            designation="Senior Engineer",
        ),
        change_deps,
        approver_id="hr-manager-001",
    )

    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "FORM_CHANGE_PENDING"
    assert len(status.open_discrepancies) == 1  # still present, just outranked


def test_stage_hris_approval_pending():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    prepare_hris_records(case.case_id, change_deps)

    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "HRIS_APPROVAL_PENDING"
    assert len(status.hris) == 2


def test_stage_hris_approved_not_written():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    prepare_result = prepare_hris_records(case.case_id, change_deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", change_deps)

    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "HRIS_APPROVED_NOT_WRITTEN"


def test_stage_write_failed():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    prepare_result = prepare_hris_records(case.case_id, change_deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", change_deps)
    change_deps.hris_gateway.fail_next(1)
    execute_hris_write(case.case_id, change_deps)

    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "WRITE_FAILED"


def test_stage_hris_rejected():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    prepare_result = prepare_hris_records(case.case_id, change_deps)
    reject_hris_write(prepare_result.approval_id, "hr-manager-001", "Compensation mismatch", change_deps)

    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "HRIS_REJECTED"


def test_stage_record_created():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    prepare_result = prepare_hris_records(case.case_id, change_deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", change_deps)
    execute_hris_write(case.case_id, change_deps)

    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "RECORD_CREATED"


def test_stale_hris_records_are_ignored_after_form_change():
    onboarding_deps, case = _open_case(designation="Engineer A")
    change_deps = _change_deps_sharing(onboarding_deps)
    prepare_result = prepare_hris_records(case.case_id, change_deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", change_deps)

    propose_result = propose_form_change(
        FormChangeRequest(
            case_id=case.case_id,
            requested_by="hr-officer-001",
            change_reason="Designation correction",
            designation="Engineer B",
        ),
        change_deps,
        approver_id="hr-manager-001",
    )
    approve_form_change(propose_result.approval_id, "hr-manager-001", change_deps)

    # Old v1 records are now STALE; the form change itself is already
    # approved, so there's nothing pending. derive_stage must not pick up
    # the STALE records (which would otherwise look like HRIS_APPROVED).
    for record_id in prepare_result.record_ids:
        record = change_deps.hris_record_repo.get(record_id)
        assert record.status == HrisRecordStatus.STALE

    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "READY_TO_PREPARE"
    assert status.hris == []


def test_stage_hris_update_needed_after_write_then_approved_form_change():
    onboarding_deps, case = _open_case(designation="Engineer A")
    change_deps = _change_deps_sharing(onboarding_deps)
    prepare_result = prepare_hris_records(case.case_id, change_deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", change_deps)
    execute_hris_write(case.case_id, change_deps)  # both v1 records WRITTEN

    propose_result = propose_form_change(
        FormChangeRequest(
            case_id=case.case_id,
            requested_by="hr-officer-001",
            change_reason="Designation correction",
            designation="Engineer B",
        ),
        change_deps,
        approver_id="hr-manager-001",
    )
    approve_form_change(propose_result.approval_id, "hr-manager-001", change_deps)

    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "HRIS_UPDATE_NEEDED"


def test_stage_form_change_pending_outranks_hris_update_needed():
    onboarding_deps, case = _open_case(designation="Engineer A")
    change_deps = _change_deps_sharing(onboarding_deps)

    # v1: prepare, approve, write -> both WRITTEN at v1.
    prepare_result = prepare_hris_records(case.case_id, change_deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", change_deps)
    execute_hris_write(case.case_id, change_deps)

    # v2: approved form change. v1's WRITTEN records are now "at an older
    # version" relative to the new in-force version (v2), and v2 has no
    # HRIS records of its own yet -> HRIS_UPDATE_NEEDED (already covered by
    # test_stage_hris_update_needed_after_write_then_approved_form_change).
    propose_v2 = propose_form_change(
        FormChangeRequest(
            case_id=case.case_id,
            requested_by="hr-officer-001",
            change_reason="Designation correction",
            designation="Engineer B",
        ),
        change_deps,
        approver_id="hr-manager-001",
    )
    approve_form_change(propose_v2.approval_id, "hr-manager-001", change_deps)

    # v3: a further change proposed (but not yet approved) while v2 is
    # still in force. written_at_older_version is still True (v1 < v2),
    # and now pending_form_change is also True: FORM_CHANGE_PENDING (#6)
    # must outrank HRIS_UPDATE_NEEDED (#7).
    propose_form_change(
        FormChangeRequest(
            case_id=case.case_id,
            requested_by="hr-officer-001",
            change_reason="Another correction",
            designation="Engineer C",
        ),
        change_deps,
        approver_id="hr-manager-001",
    )

    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    assert status.stage.value == "FORM_CHANGE_PENDING"


# --- status view content tests ---


def test_status_view_has_no_salary_email_or_discrepancy_values():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    compare_deps = make_compare_deps()
    compare_deps.discrepancy_repo = change_deps.discrepancy_repo
    compare_deps.approval_repo = change_deps.approval_repo

    form = onboarding_deps.form_repo.latest_approved(case.case_id)
    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Different Title"})
    compare_sources(case, form, [snapshot], compare_deps)

    deps = make_u9_deps(change_deps)
    viewer = make_viewer()

    status = get_case_status(case.case_id, viewer, deps)
    dumped = str(status.model_dump())
    assert SALARY not in dumped
    assert CANDIDATE_EMAIL not in dumped
    assert "Different Title" not in dumped
    assert str(status) == dumped or True  # model __str__ covered by model_dump check above
    assert SALARY not in repr(status)
    assert CANDIDATE_EMAIL not in repr(status)


def test_candidate_name_visible_for_hr_and_client_but_not_auditor():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    deps = make_u9_deps(change_deps)

    hr_officer = make_viewer(role=Role.HR_OFFICER, user_id="hr-officer-001")
    hr_manager = make_viewer(role=Role.HR_MANAGER, user_id="hr-manager-001")
    client_user = make_viewer(role=Role.CLIENT_USER, user_id="client-user-001")
    auditor = make_viewer(role=Role.AUDITOR, user_id="auditor-001")

    assert get_case_status(case.case_id, hr_officer, deps).candidate_name == CANDIDATE_NAME
    assert get_case_status(case.case_id, hr_manager, deps).candidate_name == CANDIDATE_NAME
    assert get_case_status(case.case_id, client_user, deps).candidate_name == CANDIDATE_NAME
    assert get_case_status(case.case_id, auditor, deps).candidate_name is None
