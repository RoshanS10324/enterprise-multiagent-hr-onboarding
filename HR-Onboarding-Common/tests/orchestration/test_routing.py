"""Pure conditional-edge tests: constructed state dicts, no live agents. Covers every branch of the
state-driven workflow's routing: verified/unverified intake, document request sent/not sent,
documents available/outstanding, UC03 verified / correction / HR review / technical failure, a
completed/parked UC05 step inside its branch, every branch complete/not, and ready/not-ready day-1."""

from __future__ import annotations

import pytest

from hr_onboarding_common.orchestration.orchestrator_agent import (
    route_after_document_collection, route_after_document_submission, route_after_evaluation, route_after_intake,
    route_after_plan_tasks, route_after_readiness, route_after_scheduling, route_after_validation,
)
from hr_onboarding_common.orchestration.state import READINESS_STAGES, Stage, StageStatus, new_state


def _state(**stage_overrides):
    state = new_state(case_id="C1", client_id="CL1", correlation_id="corr1",
                       actor={"on_behalf_of": "u1", "roles": ["hr_admin"]}, intake_trigger={})
    for stage, status in stage_overrides.items():
        state["stage_statuses"][stage] = status
    return state


def test_route_after_intake_verified_starts_uc02_initial_planning():
    # UC01 -> UC02: a verified intake goes to task planning first (never to documents or a branch).
    assert route_after_intake(_state(intake=StageStatus.COMPLETE.value)) == "plan_tasks"


def test_route_after_intake_unverified_or_mismatch_pauses():
    assert route_after_intake(_state(intake=StageStatus.BLOCKED.value)) == "await_intake_review"


def test_route_after_document_collection_waits_for_documents_only_once_the_request_went_out():
    assert route_after_document_collection(_state(document_collection="complete")) == "document_submission"
    for status in ("retry_pending", "blocked", "failed"):
        assert route_after_document_collection(_state(document_collection=status)) == "await_document_collection"


def test_route_after_document_submission_validates_only_when_every_document_is_available():
    assert route_after_document_submission(_state(document_submission="complete")) == "validate_documents"
    assert route_after_document_submission(_state(document_submission="waiting_for_input")) == "await_documents"


@pytest.mark.parametrize("status, target", [
    ("complete", "record_verification"),  # verified: records verified_context, then the fan-out
    ("waiting_for_input", "await_document_correction"),  # supply / correct documents
    ("blocked", "await_document_correction"),  # a UC03 finding to resolve
    ("awaiting_approval", "await_document_review"),  # an HR review decision
    ("retry_pending", "await_validation_retry"),  # a technical failure is never a document verdict
    ("failed", "await_validation_retry"),
])
def test_route_after_validation_follows_uc03s_structured_result(status, target):
    assert route_after_validation(_state(validate_documents=status)) == target


def test_route_after_plan_tasks_goes_to_uc03_document_collection_only_once_it_is_complete():
    # UC02 -> UC03: the plan (with its document requirements) exists before UC03 collects; never a branch.
    assert route_after_plan_tasks(_state(plan_tasks=StageStatus.COMPLETE.value)) == "document_collection"
    for status in ("blocked", "failed", "retry_pending", "awaiting_approval"):
        assert route_after_plan_tasks(_state(plan_tasks=status)) == "await_plan_tasks"


def test_route_after_scheduling_success_proceeds_inside_the_uc05_branch():
    # A confirmed induction leads to UC05's buddy assignment (UC05's own order), not straight to readiness.
    assert route_after_scheduling(_state(orientation_scheduling=StageStatus.COMPLETE.value)) == "buddy_assignment"


def test_route_after_scheduling_failure_ends_the_branch_for_this_run():
    assert route_after_scheduling(_state(orientation_scheduling=StageStatus.FAILED.value)) == "uc05_branch_end"
    assert route_after_scheduling(_state(orientation_scheduling=StageStatus.UNCERTAIN.value)) == "uc05_branch_end"


def test_a_branch_route_sees_the_branchs_own_outcome_of_this_run():
    state = _state()
    state["branch_outcomes"] = {"uc05_orientation": {
        "branch": "uc05_orientation", "stage_statuses": {"orientation_scheduling": "complete"}, "stage_errors": {},
        "stage_attempts": {}, "stage_outcomes": {}, "retry_metadata": {}, "idempotency_keys": {}, "stage_history": [],
        "blockers": [], "pending_approvals": [], "results": {}}}
    assert route_after_scheduling(state) == "buddy_assignment"


def test_route_after_evaluation_reaches_readiness_only_when_every_branch_is_complete():
    complete = {s.value: "complete" for s in READINESS_STAGES}
    assert route_after_evaluation(_state(**complete)) == "day_one_readiness"
    for stage in (Stage.PLAN_TASKS, Stage.EQUIPMENT_ACCESS, Stage.TEAM_INTRODUCTION):
        assert route_after_evaluation(_state(**{**complete, stage.value: "awaiting_approval"})) == "await_branches"


def test_route_after_readiness_ready_proceeds_to_the_welcome():
    # Signed-off readiness leads to the joining-day welcome; the case completes after it (route_after_welcome).
    assert route_after_readiness(_state(day_one_readiness=StageStatus.COMPLETE.value)) == "joining_day_welcome"


def test_route_after_readiness_not_ready_pauses():
    assert route_after_readiness(_state(day_one_readiness=StageStatus.AWAITING_APPROVAL.value)) == "await_day_one_blockers"
    assert route_after_readiness(_state(day_one_readiness=StageStatus.BLOCKED.value)) == "await_day_one_blockers"


def test_missing_required_field_state_is_rejectable_by_adapters():
    """Shared-state contract: callers relying on required keys fail safely (KeyError), not
    silently with a wrong default -- the state module never injects a fabricated fallback for a
    field a stage genuinely needs."""
    state = _state()
    del state["stage_statuses"]
    with pytest.raises(KeyError):
        route_after_intake(state)
