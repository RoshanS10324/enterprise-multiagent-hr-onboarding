"""Pure tests (no worker): the extended routing table, overall-status derivation, state
back-compatibility, and the SQLite case store."""

from __future__ import annotations

import json

import pytest

from hr_onboarding_common.orchestration.case_store import InMemoryCaseStateStore, SqliteCaseStateStore
from hr_onboarding_common.orchestration.orchestrator_agent import (
    AWAIT_NODES, ROUTING_TABLE, route_after_buddy, route_after_document_collection, route_after_document_submission,
    route_after_readiness, route_after_scheduling, route_after_welcome,
)
from hr_onboarding_common.orchestration.state import (Stage, StageStatus, derive_overall_status,
                                                      ensure_lifecycle_fields, new_state)


def _state(**stage_overrides):
    state = new_state(case_id="C1", client_id="CL1", correlation_id="corr1",
                      actor={"on_behalf_of": "u1", "roles": ["hr_admin"]}, intake_trigger={})
    for stage, status in stage_overrides.items():
        state["stage_statuses"][stage] = status
    return state


PARKED = [s for s in StageStatus if s != StageStatus.COMPLETE]


@pytest.mark.parametrize("route, stage, proceed, park", [
    (route_after_document_collection, Stage.DOCUMENT_COLLECTION, "document_submission", "await_document_collection"),
    (route_after_document_submission, Stage.DOCUMENT_SUBMISSION, "validate_documents", "await_documents"),
    # Inside the UC05 branch a parked step ends the branch for this run (the fan-in still receives it).
    (route_after_scheduling, Stage.ORIENTATION_SCHEDULING, "buddy_assignment", "uc05_branch_end"),
    (route_after_buddy, Stage.BUDDY_ASSIGNMENT, "team_introduction", "uc05_branch_end"),
    (route_after_readiness, Stage.DAY_ONE_READINESS, "joining_day_welcome", "await_day_one_blockers"),
    (route_after_welcome, Stage.JOINING_DAY_WELCOME, "complete", "await_joining_day"),
])
def test_routes_proceed_only_on_complete(route, stage, proceed, park):
    assert route(_state(**{stage.value: StageStatus.COMPLETE.value})) == proceed
    for status in PARKED:  # pending/blocked/awaiting/waiting/uncertain/failed/... never proceed
        assert route(_state(**{stage.value: status.value})) == park


def test_routing_table_explains_every_stage_status():
    assert set(ROUTING_TABLE) == {s.value for s in StageStatus}


def test_every_stage_that_can_park_has_a_holding_node():
    parked = {stage for _, stage, _ in AWAIT_NODES}
    # None: await_branches, which holds any of the parallel branches' stages (equipment_access,
    # orientation_scheduling, buddy_assignment, team_introduction). plan_tasks parks on its own
    # (await_plan_tasks) -- UC02 plans before the documents -- and so does UC02's replanning.
    assert parked == {Stage.INTAKE, Stage.PLAN_TASKS, Stage.DOCUMENT_COLLECTION, Stage.DOCUMENT_SUBMISSION,
                      Stage.VALIDATE_DOCUMENTS, Stage.REPLAN_TASKS, None, Stage.DAY_ONE_READINESS,
                      Stage.JOINING_DAY_WELCOME}
    # Validation parks in three distinct places: correction, HR review, technical retry.
    assert [n for n, s, _ in AWAIT_NODES if s == Stage.VALIDATE_DOCUMENTS] == [
        "await_document_correction", "await_document_review", "await_validation_retry"]


@pytest.mark.parametrize("statuses, current, expected", [
    ({"plan_tasks": "failed"}, "plan_tasks", "failed"),
    ({"orientation_scheduling": "uncertain"}, "orientation_scheduling", "at_risk"),
    ({"joining_day_welcome": "waiting"}, "joining_day_welcome", "in_progress"),
    ({"buddy_assignment": "awaiting_approval"}, "buddy_assignment", "blocked"),
    ({"complete": "complete"}, "complete", "complete"),
])
def test_overall_status_is_derived_from_stage_statuses_only(statuses, current, expected):
    state = _state(**statuses)
    state["current_stage"] = current
    assert derive_overall_status(state) == expected


def test_optional_buddy_introduction_starts_not_requested():
    assert _state()["stage_statuses"][Stage.BUDDY_INTRODUCTION.value] == StageStatus.NOT_REQUESTED.value


def test_a_state_persisted_by_the_previous_version_is_back_filled():
    old = _state()
    for key in ("buddy_assignment_result", "stage_history", "stage_errors", "joining_day_event", "gate_rejections"):
        old.pop(key)
    for stage in (Stage.BUDDY_ASSIGNMENT, Stage.TEAM_INTRODUCTION, Stage.BUDDY_INTRODUCTION, Stage.JOINING_DAY_WELCOME):
        old["stage_statuses"].pop(stage.value)
    filled = ensure_lifecycle_fields(old)
    assert filled["stage_statuses"][Stage.BUDDY_ASSIGNMENT.value] == StageStatus.PENDING.value
    assert filled["stage_history"] == [] and filled["stage_errors"] == {} and filled["joining_day_event"] is None


def test_state_is_json_serializable_for_checkpointing():
    json.dumps(_state())


def test_sqlite_case_store_round_trips_and_survives_reopening(tmp_path):
    path = tmp_path / "cases.db"
    store = SqliteCaseStateStore(path)
    state = _state(intake="complete")
    store.save(state)
    copy = store["C1"]
    copy["stage_statuses"]["plan_tasks"] = "complete"  # a read is a copy: the store is unchanged
    assert store["C1"]["stage_statuses"]["plan_tasks"] == "pending"
    store.close()

    reopened = SqliteCaseStateStore(path)
    assert "C1" in reopened and len(reopened) == 1 and list(reopened) == ["C1"]
    assert reopened["C1"]["stage_statuses"]["intake"] == "complete"
    with pytest.raises(KeyError):
        reopened["missing"]
    del reopened["C1"]
    assert "C1" not in reopened


def test_in_memory_store_is_still_a_plain_dict():
    store = InMemoryCaseStateStore()
    store.save(_state())
    assert isinstance(store, dict) and store["C1"]["case_id"] == "C1"
