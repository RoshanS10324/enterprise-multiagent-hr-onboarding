"""UC02 in its Supabase mode inside the real orchestrator: UC02 -> Supabase -> case_orchestration_state -> UC03.

MODE: LOCAL DETERMINISTIC INTEGRATION. What is real and what is not:
  real   the orchestrator graph, contract layer and routing; the REAL UC02 adapters (plan_tasks, replan_tasks) and
         UC02 package (OnboardingService, SupabaseTableClient, SupabaseConfigSource) in UC02_STORE_MODE=supabase;
         the SupabaseCaseStateStore; UC03's real document collection and requirement-gap detection.
  double both databases are in-process PostgREST stand-ins sharing onboarding_cases (_fake_postgrest.py for
         case_orchestration_state, _fake_uc02_supabase.py for UC02's tables, seeded with UC02's synthetic
         configuration fixture); UC01 is a contract double that stores its case like UC01's Supabase mode would;
         UC03 validation, UC04 and UC05 are the contract doubles of _workflow.py. No live database, no network.
The live-database counterpart is HR-Onboarding-Common/scripts/verify_uc02_supabase_workflow.py (opt-in).

Scenarios: A normal flow from persisted state, B restart after UC02, C duplicate execution, D replanning,
E document correction only, F wrong tenant, G missing / expired / invalid configuration; plus database outage,
fail-closed misconfiguration, and the scoping of UC02's credentials.
"""

from __future__ import annotations

import uuid

import pytest

from _fake_postgrest import KEY as PG_KEY, URL as PG_URL, FakePostgrest
from _fake_uc02_supabase import UC02_ENV, Uc02Supabase
from _lifecycle import FakeUc01Worker
from _workflow import CLIENT, HR, OTHER_TENANT_HR, Flow, SimulatedCrash, code, status, synthetic_document
from hr_onboarding_common.orchestration.adapters.uc02_tasks import call_uc02_plan_tasks
from hr_onboarding_common.orchestration.case_store import InMemoryCaseStateStore
from hr_onboarding_common.orchestration.state import Stage, StageStatus, WorkflowStatus
from hr_onboarding_common.orchestration.supabase_state_store import SupabaseCaseStateStore
from onboarding_task_management.supabase_synthetic import FixtureSpec, build_rows

S, ST, W = Stage, StageStatus, WorkflowStatus
# The synthetic client's templates depend on attributes HR states (e.g. location_status); without them UC02 holds
# the plan for review (attribute_unstated, CHECKLIST_REVIEW_REQUIRED) instead of guessing -- see test_unstated_*.
STATED_ATTRIBUTES = build_rows(FixtureSpec(client_id=CLIENT))["onboarding_cases"][0]["onboarding_attributes"]
APPROVAL_FACTS = dict(offer_accepted_at="2026-10-20T10:00:00Z", form_approved_at="2026-10-21T10:00:00Z",
                      form_approved_by_role="hr_ops", attributes=list(STATED_ATTRIBUTES))
OTHER_CLIENT = OTHER_TENANT_HR["client_id"]


class CaseWritingUc01(FakeUc01Worker):
    """UC01 as it behaves once it stores its cases in Supabase: the case it opens is an onboarding_cases row (uuid,
    tenant, employee number, joining date) that the orchestrator's store and UC02's store both see."""

    def __init__(self, pg: FakePostgrest, *, tenant: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.pg, self.tenant = pg, tenant

    def call(self, op, payload, **kwargs):
        response = super().call(op, payload, **kwargs)
        case = (response.result or {}).get("case")
        if case:
            case["case_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, case["case_id"]))
            self.pg.add_case(case["case_id"], self.tenant or payload["client_id"])
            self.pg.tables["onboarding_cases"][case["case_id"]].update(
                employee_no=case["employee_no"], joining_date=case["joining_date"], location_name="Dubai",
                department="Engineering")
        return response


@pytest.fixture(autouse=True)
def uc02_supabase_mode(monkeypatch):
    for name, value in UC02_ENV.items():
        monkeypatch.setenv(name, value)


def world(pg: FakePostgrest | None = None, db: Uc02Supabase | None = None, **flow_kwargs):
    """A new orchestrator PROCESS over the shared databases: a fresh case-state store (no version expectations, an
    empty provisional store), a fresh UC02 sub-agent (built from Supabase on first use) and fresh doubles."""
    pg = pg or FakePostgrest()
    db = db or Uc02Supabase(pg.tables["onboarding_cases"], client_ids=(CLIENT,))
    states = SupabaseCaseStateStore(url=PG_URL, service_role_key=PG_KEY, transport=pg.transport(),
                                    provisional=InMemoryCaseStateStore())
    flow = Flow(case_states=states, **{"real_uc02": True, **flow_kwargs})
    flow.uc01 = CaseWritingUc01(pg)
    flow.deps.worker_pool.workers["uc01"] = flow.uc01
    flow.deps.uc02_rest_factory = db.rest
    return flow, pg, db


def start(flow: Flow, **over):
    return flow.start(**{**APPROVAL_FACTS, **over})


def uc02_rows(db: Uc02Supabase, table: str, case_id: str) -> list[dict]:
    return db.rows(table, case_id=case_id) if table != "task_dependencies" else \
        [e for e in db.rows(table) if e["task_id"].startswith(case_id)]


def revisions(db: Uc02Supabase, case_id: str) -> list[dict]:
    return sorted(db.rows("plan_revisions", case_id=case_id), key=lambda r: r["revision"])


def counts(db: Uc02Supabase, case_id: str) -> tuple[int, int, int, int]:
    snapshots = [r for r in db.rows("audit_events", case_id=case_id) if r["action"] == "UC02_CASE_STATE"]
    return (len(uc02_rows(db, "onboarding_tasks", case_id)), len(uc02_rows(db, "task_dependencies", case_id)),
            len(revisions(db, case_id)), len(snapshots))


def visa_passport():
    return synthetic_document("passport", visa_required=True)


# =========================================================================== A. normal flow from persisted state


def test_A_uc02_plans_in_supabase_and_every_next_stage_reads_the_persisted_plan():
    flow, pg, db = world()
    state = start(flow)
    case_id = state["case_id"]
    row = pg.row(case_id)
    assert row["stage_statuses"][S.PLAN_TASKS.value] == ST.COMPLETE.value, row["blockers"]

    # UC02's business data is in UC02's tables, scoped to the tenant, and the plan is revision 1.
    tasks, edges = uc02_rows(db, "onboarding_tasks", case_id), uc02_rows(db, "task_dependencies", case_id)
    assert tasks and all(t["client_id"] == CLIENT and "uc02" in t["status_summary"] for t in tasks)
    assert edges and all(e["client_id"] == CLIENT for e in edges)
    assert [r["revision"] for r in revisions(db, case_id)] == [1]
    assert revisions(db, case_id)[0]["change_summary"]["trigger_source"] == "initial_planning"
    assert revisions(db, case_id)[0]["correlation_id"] == row["correlation_id"]

    # The orchestrator persisted UC02's result under the canonical stage key, in the row's own columns.
    summary = row["stage_outcomes"][S.PLAN_TASKS.value]["result_summary"]
    assert (summary["status"], summary["plan_revision"], summary["store"]) == ("complete", 1, "supabase")
    assert (summary["task_count"], summary["dependency_count"]) == (len(tasks), len(edges))
    assert summary["required_documents"] and summary["created"] is True
    task_plan = row["orchestrator_context"]["state"]["task_plan"]
    assert {(d["task_id"], d["depends_on_task_id"]) for d in task_plan["dependencies"]} == \
        {(e["task_id"], e["depends_on_task_id"]) for e in edges}
    assert task_plan["config_version"] == "1" and task_plan["trace_recorded"] is True

    # UC02 traced the execution in its own audit_events and agent_runs (tenant-scoped, ids and codes only).
    events = [(e["action"], e["outcome"]) for e in db.rows("audit_events", client_id=CLIENT)
              if e["action"] == "orchestration.plan_tasks"]
    assert events == [("orchestration.plan_tasks", "started"), ("orchestration.plan_tasks", "completed")] or \
        sorted(events) == sorted([("orchestration.plan_tasks", "started"), ("orchestration.plan_tasks", "completed")])
    (run,) = [r for r in db.rows("agent_runs", client_id=CLIENT) if r["run_id"].startswith("UC02-EXE-")]
    assert (run["status"], run["case_id"], run["tool_calls"]) == ("acted", case_id, ["initial_task_planning"])

    # UC03 collects for the persisted plan version -- in a NEW process that only has the database.
    second, _, _ = world(pg, db)
    loaded = second.agent.get_case(case_id, client_id=CLIENT)
    collection = pg.row(case_id)["stage_outcomes"][S.DOCUMENT_COLLECTION.value]["inputs"]
    assert collection["plan_version"] == 1
    assert collection["required_documents"] == [d["document_type"] for d in loaded["onboarding_plan"]["required_documents"]]
    state = second.supply(loaded)
    row = pg.row(case_id)
    for stage in (S.DOCUMENT_COLLECTION, S.DOCUMENT_SUBMISSION, S.VALIDATE_DOCUMENTS, S.EQUIPMENT_ACCESS,
                  S.ORIENTATION_SCHEDULING):
        assert row["stage_statuses"][stage.value] == ST.COMPLETE.value, stage
    assert second.uc03.plan_versions == [1]
    for branch in (S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING):  # UC04 || UC05 got the verified plan from the row
        inputs = row["stage_outcomes"][branch.value]["inputs"]
        assert (inputs["verified_plan_version"], inputs["task_plan_available"]) == (1, True)
    assert second.deps._uc02_sub_agent is None  # nothing after planning needed UC02 in process memory
    assert counts(db, case_id)[:3] == (len(tasks), len(edges), 1)  # and nothing re-planned


# =========================================================================== B. restart after UC02


def test_B_a_crash_after_uc02_committed_resumes_from_supabase_without_a_second_plan():
    def crash_after_uc02(state, deps):
        call_uc02_plan_tasks(state, deps)  # the REAL adapter: UC02 commits its plan to Supabase ...
        raise SimulatedCrash("backend stopped before the orchestrator recorded plan_tasks")  # ... then the process dies

    flow, pg, db = world(real_uc02=False, real_replan=True, plan_adapter=crash_after_uc02)
    with pytest.raises(SimulatedCrash):
        start(flow)
    (case_id,) = list(pg.tables["case_orchestration_state"])
    row = pg.row(case_id)
    assert row["stage_statuses"][S.INTAKE.value] == ST.COMPLETE.value
    assert row["stage_statuses"][S.PLAN_TASKS.value] != ST.COMPLETE.value  # the orchestrator never saw the result
    before = counts(db, case_id)
    assert before[0] > 0 and before[2] == 1

    restarted, _, _ = world(pg, db)  # new process: no UC02 sub-agent, no memory of the run
    state = restarted.agent.resume_case(case_id, actor=HR)
    assert status(state, S.PLAN_TASKS) == ST.COMPLETE.value
    assert state["task_plan"]["created"] is False  # UC02 recognised its stored plan
    assert counts(db, case_id)[:3] == before[:3]  # no duplicate task, dependency or revision
    assert pg.row(case_id)["stage_outcomes"][S.PLAN_TASKS.value]["result_summary"]["plan_revision"] == 1


def test_B_replanning_after_a_restart_rebuilds_uc02_from_supabase():
    flow, pg, db = world()
    case_id = start(flow)["case_id"]

    restarted, _, _ = world(pg, db)
    loaded = restarted.agent.get_case(case_id, client_id=CLIENT)
    documents = [visa_passport() if t == "passport" else synthetic_document(t) for t in restarted.outstanding(loaded)]
    state = restarted.agent.submit_documents(case_id, documents=documents, actor=HR)
    assert status(state, S.REPLAN_TASKS) == ST.COMPLETE.value, state["blockers"]
    assert restarted.deps._uc02_sub_agent is not None  # rebuilt on demand over the same database
    assert [r["revision"] for r in revisions(db, case_id)] == [1, 2]


# =========================================================================== C. duplicate execution


def test_C_running_plan_tasks_twice_for_the_same_state_creates_nothing_twice():
    flow, pg, db = world()
    state = start(flow)
    case_id = state["case_id"]
    before = counts(db, case_id)
    for _ in range(2):  # the same stage replayed (an at-least-once delivery, an operator retry)
        again = call_uc02_plan_tasks(state, flow.deps)
        assert status(again, S.PLAN_TASKS) == ST.COMPLETE.value
        assert again["task_plan"]["created"] is False
    assert counts(db, case_id) == before  # tasks, dependencies, revisions and case-state snapshots unchanged
    other_process, _, _ = world(pg, db)
    assert call_uc02_plan_tasks(state, other_process.deps)["task_plan"]["created"] is False
    assert counts(db, case_id) == before


# =========================================================================== D. replanning


def test_D_a_planning_impacting_change_creates_revision_2_and_uc03_revalidates_against_it():
    flow, pg, db = world()
    state = start(flow)
    case_id = state["case_id"]
    first = revisions(db, case_id)[0]
    documents = [visa_passport() if t == "passport" else synthetic_document(t) for t in flow.outstanding(state)]
    state = flow.agent.submit_documents(case_id, documents=documents, actor=HR)
    row = pg.row(case_id)
    assert row["stage_statuses"][S.REPLAN_TASKS.value] == ST.COMPLETE.value, row["blockers"]

    # revision 1 remains auditable, revision 2 is authoritative
    revs = revisions(db, case_id)
    assert [r["revision"] for r in revs] == [1, 2] and revs[0] == first
    summary = revs[1]["change_summary"]
    assert (summary["trigger_source"], summary["previous_revision"]) == ("document_validation_feedback", 1)
    feedback_id = row["orchestrator_context"]["state"]["document_validation_feedback"]["feedback_id"]
    assert summary["feedback_id"] == feedback_id and "visa_documents" in summary["added_documents"]
    plan = row["orchestrator_context"]["state"]["onboarding_plan"]
    assert (plan["plan_version"], plan["previous_plan_version"]) == (2, 1)
    assert row["orchestrator_context"]["state"]["document_collection"]["plan_version"] == 2
    outcome = row["stage_outcomes"][S.REPLAN_TASKS.value]["result_summary"]
    assert (outcome["plan_revision"], outcome["previous_plan_revision"], outcome["feedback_id"]) == (2, 1, feedback_id)
    task_plan = row["orchestrator_context"]["state"]["task_plan"]
    assert task_plan["plan_version"] == 2 and task_plan["task_count"] == len(uc02_rows(db, "onboarding_tasks", case_id))
    assert [e["outcome"] for e in db.rows("audit_events", client_id=CLIENT) if e["action"] == "orchestration.replan_tasks"] \
        == ["started", "replanned"]
    # nothing downstream started on the stale plan; UC03 then validates against version 2
    assert all(row["stage_statuses"][s.value] == ST.PENDING.value for s in (S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING))
    state = flow.supply(state, ["visa_documents"])
    assert flow.uc03.plan_versions == [1, 2]
    assert pg.row(case_id)["verified_context"]["plan_version"] == 2
    assert [r["revision"] for r in revisions(db, case_id)] == [1, 2]  # the re-validation created no revision


# =========================================================================== E. document correction only


def test_E_a_document_correction_stays_in_uc03_and_creates_no_plan_revision():
    flow, pg, db = world()
    flow.uc03.verdicts = {"passport": "correction_required"}
    state = flow.supply(start(flow))
    case_id = state["case_id"]
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.WAITING_FOR_INPUT.value
    assert status(state, S.REPLAN_TASKS) == ST.NOT_REQUESTED.value
    flow.uc03.verdicts = {}
    state = flow.supply(state, ["passport"], tag="2")
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.COMPLETE.value
    assert status(state, S.REPLAN_TASKS) == ST.NOT_REQUESTED.value
    assert [r["revision"] for r in revisions(db, case_id)] == [1]
    assert not [e for e in db.rows("audit_events", client_id=CLIENT) if e["action"] == "orchestration.replan_tasks"]


# =========================================================================== F. wrong tenant


def test_F_a_case_of_another_tenant_is_never_planned_and_nothing_is_written():
    flow, pg, db = world()
    state = start(flow)  # a genuine case of CLIENT, planned
    foreign_case = str(uuid.uuid4())
    pg.add_case(foreign_case, OTHER_CLIENT)  # a CLIENT-TEST-B case ...
    forged = {**state, "case_id": foreign_case}  # ... presented in CLIENT's workflow
    result = call_uc02_plan_tasks(forged, flow.deps)
    assert status(result, S.PLAN_TASKS) == ST.FAILED.value
    assert code(result, S.PLAN_TASKS) == "TENANT_CONTEXT_CONFLICT"
    assert counts(db, foreign_case) == (0, 0, 0, 0)

    # CLIENT-TEST-B can read neither the workflow state nor UC02's plan of CLIENT's case.
    with pytest.raises(KeyError):
        flow.agent.get_case(state["case_id"], client_id=OTHER_CLIENT)
    from onboarding_task_management.contracts import Actor, Role
    from onboarding_task_management.errors import DataIsolationError

    other = Actor(actor_id="user:hr-admin-b", role=Role.HR_OPS, client_id=OTHER_CLIENT)
    with pytest.raises(DataIsolationError):
        flow.deps._uc02_sub_agent.service.get(other, state["case_id"])


# =========================================================================== G. configuration


def _blocked_on_configuration(flow, pg, db, expected_detail: str):
    state = start(flow)
    case_id = state["case_id"]
    assert status(state, S.PLAN_TASKS) == ST.BLOCKED.value
    assert code(state, S.PLAN_TASKS) == "MISSING_DATA" and expected_detail in state["stage_errors"][S.PLAN_TASKS.value]["detail"]
    (blocker,) = [b for b in state["blockers"] if b["stage"] == S.PLAN_TASKS.value]
    assert blocker["code"] == "UC02_CONFIG_MISSING" and blocker["required_from"].startswith("Client configuration")
    assert counts(db, case_id)[:3] == (0, 0, 0)  # no fabricated task, dependency or revision
    assert status(state, S.DOCUMENT_COLLECTION) == ST.PENDING.value  # nothing after planning started
    assert pg.row(case_id)["stage_outcomes"][S.PLAN_TASKS.value]["result_summary"]["blockers"] == ["UC02_CONFIG_MISSING"]
    (run,) = [r for r in db.rows("agent_runs", client_id=CLIENT) if r["run_id"].startswith("UC02-EXE-")]
    assert (run["status"], run["error_summary"]) == ("exception", "plan_tasks:UC02_CONFIG_MISSING")
    return state


def test_G_missing_configuration_blocks_cleanly_and_the_case_resumes_once_it_is_approved():
    pg = FakePostgrest()
    db = Uc02Supabase(pg.tables["onboarding_cases"])  # no configuration for CLIENT
    flow, _, _ = world(pg, db)
    state = _blocked_on_configuration(flow, pg, db, "client is not in client_configs")
    db.seed_client(CLIENT)
    state = flow.agent.resume_case(state["case_id"], actor=HR)
    assert status(state, S.PLAN_TASKS) == ST.COMPLETE.value
    assert [r["revision"] for r in revisions(db, state["case_id"])] == [1]


def test_G_an_expired_configuration_version_blocks():
    flow, pg, db = world()
    for version in db.tables["client_config_versions"]:
        version["effective_to"] = "2026-01-31"
    _blocked_on_configuration(flow, pg, db, "no approved configuration version is in force")


def test_G_an_invalid_configuration_blocks():
    flow, pg, db = world()
    db.tables["task_templates"][0]["category"] = "not-a-category"
    _blocked_on_configuration(flow, pg, db, "the approved configuration is invalid")


# =========================================================================== database and runtime failures


def test_an_unreachable_database_is_retry_pending_and_never_a_fabricated_plan():
    flow, pg, db = world()
    db.down = True
    state = start(flow)
    assert status(state, S.PLAN_TASKS) == ST.RETRY_PENDING.value
    assert code(state, S.PLAN_TASKS) == "UC02_STORE_UNAVAILABLE"
    assert state["task_plan"] is None and state["onboarding_plan"] is None
    db.down = False
    state = flow.agent.retry_stage(state["case_id"], S.PLAN_TASKS.value, actor=HR)
    assert status(state, S.PLAN_TASKS) == ST.COMPLETE.value


def test_a_case_uc01_did_not_store_blocks_planning():
    pg = FakePostgrest()
    db = Uc02Supabase(pg.tables["onboarding_cases"], client_ids=(CLIENT,))
    flow = Flow(real_uc02=True)  # UC01 in memory: its case id is not an onboarding_cases row
    flow.deps.uc02_rest_factory = db.rest
    state = start(flow)
    assert status(state, S.PLAN_TASKS) == ST.BLOCKED.value
    assert [b["code"] for b in state["blockers"] if b["stage"] == S.PLAN_TASKS.value] == ["UC02_CASE_NOT_FOUND"]
    assert db.rows("onboarding_tasks") == [] and db.rows("plan_revisions") == []


@pytest.mark.parametrize("unset, expected", [("UC02_WORKER_ENV__SUPABASE_URL", "UC02_STORE_ERROR"),
                                             ("UC02_ENV", "UC02_RUNTIME_MISCONFIGURED")])
def test_supabase_mode_fails_closed_and_never_falls_back_to_memory(monkeypatch, unset, expected):
    monkeypatch.delenv(unset)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    flow, pg, db = world()
    flow.deps.uc02_rest_factory = None  # the credentials, not a test transport, decide
    state = start(flow)
    assert status(state, S.PLAN_TASKS) == ST.BLOCKED.value
    assert [b["code"] for b in state["blockers"] if b["stage"] == S.PLAN_TASKS.value] == [expected]
    assert flow.deps._uc02_sub_agent is None  # no in-memory UC02 was put in its place
    assert state["task_plan"] is None


def test_unstated_attributes_keep_the_plan_for_hr_review_instead_of_guessing():
    flow, pg, db = world()
    state = flow.start(**{**APPROVAL_FACTS, "attributes": None})
    assert status(state, S.PLAN_TASKS) == ST.BLOCKED.value
    assert code(state, S.PLAN_TASKS) == "CHECKLIST_REVIEW_REQUIRED"
    assert "attribute_unstated" in state["stage_errors"][S.PLAN_TASKS.value]["detail"]
    assert state["task_plan"]["review_required"] is True  # the plan exists in UC02 and is shown, not discarded
    assert status(state, S.DOCUMENT_COLLECTION) == ST.PENDING.value


def test_without_supabase_mode_uc02_stays_in_memory_for_unit_tests(monkeypatch):
    from hr_onboarding_common.orchestration.adapters import uc02_runtime
    from hr_onboarding_common.orchestration.adapters.uc02_tasks import uc02_sub_agent

    monkeypatch.delenv("UC02_STORE_MODE")
    flow = Flow(real_uc02=True)
    agent = uc02_sub_agent(flow.deps)
    assert agent.service.store is None
    assert uc02_runtime.store_of(agent) == "memory"  # labelled in task_plan.store, never mistaken for a durable plan


def test_the_server_refuses_a_supabase_workflow_whose_uc02_plans_live_in_memory(monkeypatch):
    """serve_orchestrator.py, real mode: case_orchestration_state in Supabase with UC02 in memory would record plans
    a restart loses. The server refuses before any worker starts."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "serve_orchestrator.py"
    monkeypatch.syspath_prepend(str(script.parent))
    spec = importlib.util.spec_from_file_location("serve_orchestrator_under_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("ORCHESTRATOR_STATE_STORE", "supabase")
    monkeypatch.setenv("UC02_STORE_MODE", "memory")
    started = []
    monkeypatch.setattr(module.demo, "build_real_agent", lambda *a, **k: started.append(1))
    with pytest.raises(SystemExit, match="UC02_STORE_MODE=memory"):
        module.build_app(mode="real", persist_dir=None, dev_identity=True)
    assert started == []  # refused before building the agent and its workers


def test_uc02_credentials_never_reach_a_worker_process(monkeypatch):
    from hr_onboarding_common.orchestration.workers.config import WorkerConfig

    monkeypatch.setenv("ORCHESTRATOR_SUPABASE_SERVICE_ROLE_KEY", "orchestrator-key")
    for uc in ("uc01", "uc04", "uc05"):
        env = WorkerConfig(uc_id=uc, python_executable="python", src_dir=".").child_environment()
        assert not any(k.startswith("UC02_WORKER_ENV__") for k in env)
        assert "ORCHESTRATOR_SUPABASE_SERVICE_ROLE_KEY" not in env
        assert env.get("SUPABASE_URL") != UC02_ENV["UC02_WORKER_ENV__SUPABASE_URL"]  # no bare URL that flips UC01


def test_hr_can_state_checklist_attributes_through_the_api_as_codes_only():
    from pydantic import ValidationError

    from hr_onboarding_common.orchestration.api import StartCaseRequest

    body = StartCaseRequest(offer_accepted=True, attributes=["bv_required", "location_status:in_country", "bv_required"])
    assert body.attributes == ["bv_required", "location_status:in_country"]  # forwarded as stated, once each
    assert StartCaseRequest(offer_accepted=True).attributes is None  # optional: nothing is inferred
    for bad in (["has a space"], ["x" * 65], [""], ["<script>"]):
        with pytest.raises(ValidationError):
            StartCaseRequest(offer_accepted=True, attributes=bad)


def test_scoped_settings_alone_put_uc02_in_supabase_mode(monkeypatch):
    """The shared .env carries UC02's settings scoped (UC02_WORKER_ENV__NAME): that file is also loaded by UC02's own
    processes, where a plain UC02_STORE_MODE would switch UC02's standalone runtime. The orchestrator reads them scoped."""
    import os

    for name in ("UC02_STORE_MODE", "UC02_ENV", "UC02_ALLOW_SYNTHETIC_CONFIG"):
        monkeypatch.setenv(f"UC02_WORKER_ENV__{name}", UC02_ENV[name])
        monkeypatch.delenv(name)
    flow, pg, db = world()
    state = start(flow)
    assert status(state, S.PLAN_TASKS) == ST.COMPLETE.value and state["task_plan"]["store"] == "supabase"
    assert not os.environ.get("UC02_STORE_MODE")  # never placed in the process environment unscoped
