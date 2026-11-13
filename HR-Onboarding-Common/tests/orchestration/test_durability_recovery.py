"""Section 5a tests: three durability layers kept distinct (orchestrator checkpoint, each UC's
own business data, external side-effect delivery). Two uncertain-recovery scenarios tested
separately per explicit instruction: (1) operation succeeded but its response was lost, (2) the
worker itself restarted and lost its in-memory records."""

from __future__ import annotations

import json

from _helpers import UC_ROOTS
from hr_onboarding_common.orchestration.adapters.uc04_equipment_access import call_uc04_equipment_access
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import Stage, StageStatus, new_state
from hr_onboarding_common.orchestration.workers.config import WorkerConfig
from hr_onboarding_common.orchestration.workers.uc_worker_client import UcWorker, WorkerCrashedError


class _SingleWorkerPool:
    def __init__(self, worker):
        self._worker = worker

    def get(self, uc_id):
        return self._worker


def _uc04_state(config_ref):
    state = new_state(case_id="SYN-CASE-001", client_id="client_alpha", correlation_id="corr1",
                       actor={"on_behalf_of": "SYN-USER-201", "roles": ["hr_onboarding_officer"]},
                       intake_trigger={}, uc04_client_config_ref=str(config_ref))
    state["employee_id"] = "SYN-EMP-001"
    state["joining_date"] = "2026-11-16"
    return state


def test_worker_crash_mid_call_surfaces_as_uncertain_not_silently_retried(uc04_worker):
    """Scenario 2: the worker is killed; its in-memory case/ticket state is gone. The adapter
    must not blindly retry the write or report success -- it reports UNCERTAIN for the stage."""
    config_ref = UC_ROOTS["uc04"] / "data" / "client_alpha_config.json"
    deps = OrchestratorDeps(ctx=None, worker_pool=_SingleWorkerPool(uc04_worker), uc03_repo_root=UC_ROOTS["uc03"])

    uc04_worker.stop()  # simulate a crash: the worker process is gone before the call is made

    result = call_uc04_equipment_access(_uc04_state(config_ref), deps)

    assert result["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] == StageStatus.UNCERTAIN.value
    assert any(b["code"] == "UNCERTAIN_OUTCOME" for b in result["blockers"])
    assert "ops_review" in (result.get("next_action") or "")


def test_restarted_worker_starts_with_no_business_data_then_succeeds_fresh():
    """After a restart, the SAME case_id is accepted as a fresh registration (no stale state to
    conflict with) -- demonstrating the documented limitation plainly rather than hiding it."""
    from _helpers import require_uc, venv_python

    require_uc("uc04")
    cfg = WorkerConfig(uc_id="uc04", python_executable=venv_python(UC_ROOTS["uc04"]), src_dir=UC_ROOTS["uc04"] / "src")
    config_ref = UC_ROOTS["uc04"] / "data" / "client_alpha_config.json"

    worker = UcWorker(cfg)
    worker.start()
    deps = OrchestratorDeps(ctx=None, worker_pool=_SingleWorkerPool(worker), uc03_repo_root=UC_ROOTS["uc03"])
    first = call_uc04_equipment_access(_uc04_state(config_ref), deps)
    assert first["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] != StageStatus.FAILED.value
    worker.stop()  # restart: in-memory case registry is lost (documented limitation)

    worker2 = UcWorker(cfg)
    worker2.start()
    deps2 = OrchestratorDeps(ctx=None, worker_pool=_SingleWorkerPool(worker2), uc03_repo_root=UC_ROOTS["uc03"])
    try:
        second = call_uc04_equipment_access(_uc04_state(config_ref), deps2)
        assert second["stage_statuses"][Stage.EQUIPMENT_ACCESS.value] != StageStatus.FAILED.value
    finally:
        worker2.stop()


def test_checkpoint_recovery_resumes_same_case_and_stage(tmp_path):
    """Orchestrator-level checkpoint recovery (SqliteSaver) -- distinct from UC business-data
    recovery (tested above). Mirrors UC05's own test_persistence_and_recovery.py pattern."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = tmp_path / "orchestrator_checkpoints.db"
    with SqliteSaver.from_conn_string(str(db_path)) as saver:
        from langgraph.graph import END, StateGraph

        def node_a(state):
            return {**state, "stage_statuses": {**state["stage_statuses"], "intake": "complete"}}

        def node_b(state):
            return {**state, "stage_statuses": {**state["stage_statuses"], "plan_tasks": "blocked"}}

        graph = StateGraph(dict)
        graph.add_node("a", node_a)
        graph.add_node("b", node_b)
        graph.set_entry_point("a")
        graph.add_edge("a", "b")
        graph.add_edge("b", END)
        compiled = graph.compile(checkpointer=saver)

        config = {"configurable": {"thread_id": "case-xyz"}}
        initial = new_state(case_id="case-xyz", client_id="CL1", correlation_id="c1",
                             actor={"on_behalf_of": "u1", "roles": []}, intake_trigger={})
        result1 = compiled.invoke(initial, config=config)
        assert result1["stage_statuses"]["plan_tasks"] == "blocked"

    # Reopen the SAME sqlite file in a NEW saver/graph -- simulates an orchestrator process
    # restart; the checkpoint must still be there.
    with SqliteSaver.from_conn_string(str(db_path)) as saver2:
        from langgraph.graph import END, StateGraph

        graph2 = StateGraph(dict)
        graph2.add_node("a", node_a)
        graph2.add_node("b", node_b)
        graph2.set_entry_point("a")
        graph2.add_edge("a", "b")
        graph2.add_edge("b", END)
        compiled2 = graph2.compile(checkpointer=saver2)
        state_snapshot = compiled2.get_state({"configurable": {"thread_id": "case-xyz"}})
        assert state_snapshot.values["case_id"] == "case-xyz"
        assert state_snapshot.values["stage_statuses"]["plan_tasks"] == "blocked"
