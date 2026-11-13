import pytest

from sub_agent.case_status import get_audit_trail, get_case_status, list_pending_approvals
from sub_agent.errors import AccessDeniedError, CaseNotFoundError
from sub_agent.form_changes import approve_form_change, propose_form_change
from sub_agent.hris_prepare import prepare_hris_records
from sub_agent.hris_write import approve_hris_write, execute_hris_write
from sub_agent.models import FormChangeRequest, Role
from sub_agent.onboarding_entry import OpenCaseRequest, open_onboarding_case

from factories import make_approved_form, make_change_deps, make_deps, make_u9_deps, make_viewer, make_placement

CANDIDATE_NAME = "Access Test Candidate"
CANDIDATE_EMAIL = "access.test.candidate@example.test"
SALARY = "Base: 160000 SYNTHETIC/yr; benefits: standard"
OTHER_CLIENT = "client-other-999"


def _open_case(client_id="client-synthetic-001", hr_officer_id="hr-officer-001", **form_overrides):
    onboarding_deps = make_deps()
    defaults = dict(
        client_id=client_id,
        candidate_name=CANDIDATE_NAME,
        candidate_email=CANDIDATE_EMAIL,
        salary_and_terms=SALARY,
    )
    defaults.update(form_overrides)
    form = make_approved_form(**defaults)
    request = OpenCaseRequest(placement=make_placement(), 
        request_id="req-access-001",
        client_id=form.client_id,
        hr_officer_id=hr_officer_id,
        offer_accepted=True,
        form=form,
    )
    result = open_onboarding_case(request, onboarding_deps)
    return onboarding_deps, result.case


def _change_deps_sharing(onboarding_deps):
    deps = make_change_deps()
    deps.case_repo = onboarding_deps.case_repo
    deps.form_repo = onboarding_deps.form_repo
    deps.audit_log = onboarding_deps.audit_log
    return deps


def _access_denied_entries(deps):
    return [e for e in deps.audit_log.entries() if e.action == "ACCESS_DENIED"]


def test_other_client_client_user_gets_case_not_found():
    onboarding_deps, case = _open_case()
    deps = make_u9_deps(_change_deps_sharing(onboarding_deps))
    viewer = make_viewer(role=Role.CLIENT_USER, user_id="client-user-001", client_ids=frozenset({OTHER_CLIENT}))

    with pytest.raises(CaseNotFoundError):
        get_case_status(case.case_id, viewer, deps)


def test_unassigned_hr_officer_gets_case_not_found():
    onboarding_deps, case = _open_case(hr_officer_id="hr-officer-001")
    deps = make_u9_deps(_change_deps_sharing(onboarding_deps))
    viewer = make_viewer(role=Role.HR_OFFICER, user_id="hr-officer-999")

    with pytest.raises(CaseNotFoundError):
        get_case_status(case.case_id, viewer, deps)


def test_unknown_case_id_gives_identical_exception():
    onboarding_deps, case = _open_case()
    deps = make_u9_deps(_change_deps_sharing(onboarding_deps))
    viewer = make_viewer()

    with pytest.raises(CaseNotFoundError) as unknown_exc_info:
        get_case_status("not-a-real-case-id", viewer, deps)

    other_viewer = make_viewer(role=Role.CLIENT_USER, user_id="client-user-001", client_ids=frozenset({OTHER_CLIENT}))
    with pytest.raises(CaseNotFoundError) as denied_exc_info:
        get_case_status(case.case_id, other_viewer, deps)

    assert type(unknown_exc_info.value) is type(denied_exc_info.value)
    assert str(unknown_exc_info.value) == str(denied_exc_info.value)


def test_every_denial_writes_exactly_one_access_denied_entry_with_no_case_id():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    deps = make_u9_deps(change_deps)
    viewer = make_viewer(role=Role.CLIENT_USER, user_id="client-user-001", client_ids=frozenset({OTHER_CLIENT}))

    with pytest.raises(CaseNotFoundError):
        get_case_status(case.case_id, viewer, deps)

    denied_entries = _access_denied_entries(change_deps)
    assert len(denied_entries) == 1
    entry = denied_entries[0]
    assert entry.case_id is None
    assert case.case_id not in str(entry.model_dump())


def test_hr_manager_of_same_client_sees_case():
    onboarding_deps, case = _open_case()
    deps = make_u9_deps(_change_deps_sharing(onboarding_deps))
    viewer = make_viewer(role=Role.HR_MANAGER, user_id="hr-manager-001")

    status = get_case_status(case.case_id, viewer, deps)
    assert status.case_id == case.case_id


def test_client_user_cannot_view_audit_trail():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    deps = make_u9_deps(change_deps)
    viewer = make_viewer(role=Role.CLIENT_USER, user_id="client-user-001")

    with pytest.raises(AccessDeniedError):
        get_audit_trail(case.case_id, viewer, deps)

    assert len(_access_denied_entries(change_deps)) == 1


def test_auditor_can_read_audit_trail():
    onboarding_deps, case = _open_case()
    deps = make_u9_deps(_change_deps_sharing(onboarding_deps))
    viewer = make_viewer(role=Role.AUDITOR, user_id="auditor-001")

    entries = get_audit_trail(case.case_id, viewer, deps)
    assert len(entries) > 0


def test_wrong_role_calls_to_audit_trail_raise_access_denied():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    deps = make_u9_deps(change_deps)

    for role in (Role.CLIENT_USER,):
        viewer = make_viewer(role=role, user_id=f"user-{role.value.lower()}")
        with pytest.raises(AccessDeniedError):
            get_audit_trail(case.case_id, viewer, deps)


def test_audit_trail_entries_in_order_and_viewed_entry_excluded_and_recorded():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    deps = make_u9_deps(change_deps)
    viewer = make_viewer(role=Role.HR_MANAGER, user_id="hr-manager-001")

    raw_before = change_deps.audit_log.entries_for_case(case.case_id)

    entries = get_audit_trail(case.case_id, viewer, deps)

    assert [e.log_id for e in entries] == [e.log_id for e in raw_before]
    assert all(e.action != "AUDIT_TRAIL_VIEWED" for e in entries)

    raw_after = change_deps.audit_log.entries_for_case(case.case_id)
    viewed_entries = [e for e in raw_after if e.action == "AUDIT_TRAIL_VIEWED"]
    assert len(viewed_entries) == 1
    assert viewed_entries[0].actor_id == "hr-manager-001"


def test_full_flow_audit_trail_has_no_pii_or_salary():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)

    from sub_agent.compare_sources import compare_sources
    from sub_agent.models import SourceSnapshot
    from factories import make_compare_deps

    compare_deps = make_compare_deps()
    compare_deps.discrepancy_repo = change_deps.discrepancy_repo
    compare_deps.approval_repo = change_deps.approval_repo
    form = onboarding_deps.form_repo.latest_approved(case.case_id)
    snapshot = SourceSnapshot(source="hr_system", values={"designation": "Different Title"})
    compare_sources(case, form, [snapshot], compare_deps)

    propose_result = propose_form_change(
        FormChangeRequest(
            case_id=case.case_id,
            requested_by="hr-officer-001",
            change_reason="Designation correction",
            designation="Senior Engineer",
        ),
        change_deps,
        approver_id="hr-manager-001",
    )
    approve_form_change(propose_result.approval_id, "hr-manager-001", change_deps)

    prepare_result = prepare_hris_records(case.case_id, change_deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", change_deps)
    execute_hris_write(case.case_id, change_deps)

    deps = make_u9_deps(change_deps)
    viewer = make_viewer(role=Role.AUDITOR, user_id="auditor-001")
    entries = get_audit_trail(case.case_id, viewer, deps)

    for entry in entries:
        dumped = str(entry.model_dump())
        assert SALARY not in dumped
        assert CANDIDATE_NAME not in dumped
        assert CANDIDATE_EMAIL not in dumped


def test_worklist_only_viewers_own_pending_approvals():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    prepare_result = prepare_hris_records(case.case_id, change_deps)  # requested_to = case.hr_officer_id

    deps = make_u9_deps(change_deps)
    owner_viewer = make_viewer(role=Role.HR_OFFICER, user_id=case.hr_officer_id)
    other_viewer = make_viewer(role=Role.HR_OFFICER, user_id="someone-else")

    owner_worklist = list_pending_approvals(owner_viewer, deps)
    other_worklist = list_pending_approvals(other_viewer, deps)

    assert any(a.approval_id == prepare_result.approval_id for a in owner_worklist)
    assert other_worklist == []


def test_worklist_excludes_decided_approvals():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    prepare_result = prepare_hris_records(case.case_id, change_deps)
    approve_hris_write(prepare_result.approval_id, "hr-manager-001", change_deps)

    deps = make_u9_deps(change_deps)
    viewer = make_viewer(role=Role.HR_OFFICER, user_id=case.hr_officer_id)

    worklist = list_pending_approvals(viewer, deps)
    assert all(a.approval_id != prepare_result.approval_id for a in worklist)


def test_worklist_excludes_other_clients_cases():
    onboarding_deps, case = _open_case(client_id=OTHER_CLIENT, hr_officer_id="shared-hr-officer")
    change_deps = _change_deps_sharing(onboarding_deps)
    prepare_result = prepare_hris_records(case.case_id, change_deps)

    deps = make_u9_deps(change_deps)
    viewer = make_viewer(
        role=Role.HR_OFFICER, user_id="shared-hr-officer", client_ids=frozenset({"client-synthetic-001"})
    )

    worklist = list_pending_approvals(viewer, deps)
    assert all(a.approval_id != prepare_result.approval_id for a in worklist)


def test_worklist_oldest_first():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)

    first = prepare_hris_records(case.case_id, change_deps)
    approve_hris_write(first.approval_id, "hr-manager-001", change_deps)

    propose_result = propose_form_change(
        FormChangeRequest(
            case_id=case.case_id,
            requested_by="hr-officer-001",
            change_reason="Designation correction",
            designation="Senior Engineer",
        ),
        change_deps,
        approver_id=case.hr_officer_id,
    )

    deps = make_u9_deps(change_deps)
    viewer = make_viewer(role=Role.HR_OFFICER, user_id=case.hr_officer_id)

    worklist = list_pending_approvals(viewer, deps)
    assert len(worklist) == 1
    assert worklist[0].approval_id == propose_result.approval_id

    created_ats = [a.created_at for a in worklist]
    assert created_ats == sorted(created_ats)


def test_worklist_denies_client_user():
    onboarding_deps, case = _open_case()
    change_deps = _change_deps_sharing(onboarding_deps)
    deps = make_u9_deps(change_deps)
    viewer = make_viewer(role=Role.CLIENT_USER, user_id="client-user-001")

    with pytest.raises(AccessDeniedError):
        list_pending_approvals(viewer, deps)

    assert len(_access_denied_entries(change_deps)) == 1
