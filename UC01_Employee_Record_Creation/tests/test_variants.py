import pytest

from sub_agent.models import Approval, ApprovalStatus, CaseStatus, Checkpoint
from sub_agent.variants import decide_variant_approval

from factories import make_change_deps, seed_linked_case_and_form


def _pending_variant_approval(deps, case, subject_ref="is_rehire"):
    approval = Approval(
        case_id=case.case_id,
        checkpoint=Checkpoint.HC_12_VARIANT,
        subject_ref=subject_ref,
        requested_to=case.hr_officer_id,
    )
    deps.approval_repo.add(approval)
    return approval


def test_approve_variant():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    approval = _pending_variant_approval(deps, case)

    result = decide_variant_approval(approval.approval_id, "hr-manager-001", True, "confirmed rehire", deps)

    assert result.status == ApprovalStatus.APPROVED
    assert result.decided_by == "hr-manager-001"
    assert result.decision_note == "confirmed rehire"

    reloaded_case = deps.case_repo.get(case.case_id)
    assert reloaded_case.status != CaseStatus.ON_HOLD

    actions = [e.action for e in deps.audit_log.entries()]
    assert "VARIANT_APPROVED" in actions
    assert "VARIANT_REJECTED" not in actions


def test_reject_variant_puts_case_on_hold():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    approval = _pending_variant_approval(deps, case)

    result = decide_variant_approval(approval.approval_id, "hr-manager-001", False, "actually a duplicate", deps)

    assert result.status == ApprovalStatus.REJECTED
    assert result.decision_note == "actually a duplicate"

    reloaded_case = deps.case_repo.get(case.case_id)
    assert reloaded_case.status == CaseStatus.ON_HOLD

    actions = [e.action for e in deps.audit_log.entries()]
    assert "VARIANT_REJECTED" in actions
    assert "CASE_PUT_ON_HOLD" in actions


def test_possible_duplicate_approve_means_not_a_duplicate():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    approval = _pending_variant_approval(deps, case, subject_ref="possible_duplicate")

    result = decide_variant_approval(approval.approval_id, "hr-manager-001", True, "confirmed different person", deps)

    assert result.status == ApprovalStatus.APPROVED
    reloaded_case = deps.case_repo.get(case.case_id)
    assert reloaded_case.status != CaseStatus.ON_HOLD


def test_possible_duplicate_reject_means_it_is_a_duplicate():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    approval = _pending_variant_approval(deps, case, subject_ref="possible_duplicate")

    result = decide_variant_approval(approval.approval_id, "hr-manager-001", False, "confirmed duplicate", deps)

    assert result.status == ApprovalStatus.REJECTED
    reloaded_case = deps.case_repo.get(case.case_id)
    assert reloaded_case.status == CaseStatus.ON_HOLD


def test_decide_non_pending_approval_raises():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    approval = _pending_variant_approval(deps, case)
    decide_variant_approval(approval.approval_id, "hr-manager-001", True, "", deps)

    with pytest.raises(ValueError):
        decide_variant_approval(approval.approval_id, "hr-manager-002", True, "", deps)


def test_decide_wrong_checkpoint_raises():
    deps = make_change_deps()
    case, _ = seed_linked_case_and_form(deps)
    approval = Approval(
        case_id=case.case_id,
        checkpoint=Checkpoint.HC_04_DATA,
        subject_ref="discrepancy:x",
        requested_to=case.hr_officer_id,
    )
    deps.approval_repo.add(approval)

    with pytest.raises(ValueError):
        decide_variant_approval(approval.approval_id, "hr-manager-001", True, "", deps)


def test_unknown_approval_raises():
    deps = make_change_deps()
    with pytest.raises(ValueError):
        decide_variant_approval("not-a-real-id", "hr-manager-001", True, "", deps)
