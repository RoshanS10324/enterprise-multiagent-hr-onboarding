"""End-to-end orchestration with Supabase `case_orchestration_state` as the authoritative case state.

MODE 2 -- LOCAL DETERMINISTIC INTEGRATION. What is real and what is not:
  real   the orchestrator graph, routing, contract layer, gates, merge and readiness logic; the
         SupabaseCaseStateStore (every read and write goes through it, over HTTP); UC03's real document
         collection and requirement-gap detection; UC02's real OnboardingService where real_uc02_flow is used
  double the database is an in-process PostgREST (_fake_postgrest.py: primary key, foreign key, tenant
         filters, atomic conditional PATCH); UC01/UC04/UC05 and UC03 validation are the contract-shaped
         doubles of _workflow.py; the LLM is a FakeLLM. Nothing here calls OpenRouter or the live database.
The live-database counterpart is scripts/e2e_supabase_live.py (opt-in, reported separately).

Scenarios (ORCHESTRATION_END_TO_END_TEST_REPORT.md): A complete workflow, B document correction, C
replanning, D parallel branches and concurrent writers, E failure and recovery, F tenant isolation,
G joining-date change.
"""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import date

import pytest

from _fake_postgrest import KEY, URL, FakePostgrest
from _lifecycle import FakeUc01Worker
from _uc02_real import real_uc02_flow
from _workflow import (CLIENT, HR, HR_2, JOINING, OTHER_TENANT_HR, SCHEDULER, Flow, SimulatedCrash, code, status,
                       synthetic_document)
from hr_onboarding_common.orchestration.case_store import (CaseOwnershipError, InMemoryCaseStateStore,
                                                           StatePersistenceError, StatePersistenceUnavailable,
                                                           StateVersionConflict)
from hr_onboarding_common.orchestration.contracts import WorkerResponse
from hr_onboarding_common.orchestration.state import Branch, Stage, StageStatus, WorkflowStatus, new_state
from hr_onboarding_common.orchestration.supabase_state_store import (FORMAT, SupabaseCaseStateStore, is_case_uuid,
                                                                     row_to_state, state_to_row)
from hr_onboarding_common.orchestration.workflow_models import validate_workflow_state
from hr_onboarding_common.platform.hitl import Decision, HITLError

S, ST, W = Stage, StageStatus, WorkflowStatus
SEQUENTIAL = (S.INTAKE, S.PLAN_TASKS, S.DOCUMENT_COLLECTION, S.DOCUMENT_SUBMISSION, S.VALIDATE_DOCUMENTS)
BRANCH_RESULTS = (S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING, S.BUDDY_ASSIGNMENT, S.TEAM_INTRODUCTION)


# --------------------------------------------------------------------------- harness


class UuidUc01Worker(FakeUc01Worker):
    """UC01 as it behaves once it persists to Supabase: the case id it returns is the uuid of the
    `onboarding_cases` row it created (registered in the fake database, in the case's tenant)."""

    def __init__(self, pg: FakePostgrest):
        super().__init__()
        self.pg = pg

    def call(self, op, payload, **kwargs):
        response = super().call(op, payload, **kwargs)
        case = (response.result or {}).get("case")
        if case:
            case["case_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, case["case_id"]))
            self.pg.add_case(case["case_id"], payload["client_id"])
        return response


def store(pg: FakePostgrest, *, key: str = KEY, provisional=None) -> SupabaseCaseStateStore:
    return SupabaseCaseStateStore(url=URL, service_role_key=key, transport=pg.transport(),
                                  provisional=provisional if provisional is not None else InMemoryCaseStateStore())


def with_uuid_uc01(flow: Flow, pg: FakePostgrest) -> Flow:
    flow.uc01 = UuidUc01Worker(pg)
    flow.deps.worker_pool.workers["uc01"] = flow.uc01
    return flow


def supabase_flow(pg: FakePostgrest, **kwargs) -> Flow:
    """A new orchestrator 'process' over the shared database: a fresh store instance (its provisional
    store and version expectations start empty) and fresh UC doubles."""
    return with_uuid_uc01(Flow(case_states=store(pg), **kwargs), pg)


def real_uc02_supabase_flow(pg: FakePostgrest, **kwargs):
    flow, service = real_uc02_flow(case_states=store(pg), **kwargs)
    return with_uuid_uc01(flow, pg), service


def committed(pg: FakePostgrest, case_id: str) -> dict:
    """The committed state, read straight from the database row."""
    return row_to_state(pg.row(case_id))


# =========================================================================== store unit behaviour


def test_row_mapping_uses_the_table_columns_and_round_trips():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    state = flow.supply(flow.start())
    row = pg.row(state["case_id"])
    for column in ("case_id", "client_id", "correlation_id", "workflow_status", "overall_status", "stage_statuses",
                   "branch_statuses", "completed_stages", "pending_actions", "blockers", "stage_outcomes",
                   "retry_metadata", "verified_context", "orchestrator_context", "last_error_code",
                   "last_error_detail", "version", "updated_at"):
        assert column in row, column
    assert row["orchestrator_context"]["format"] == FORMAT
    # Columns hold the first-class fields; nothing is duplicated into orchestrator_context.
    assert "stage_outcomes" not in row["orchestrator_context"]["state"]
    assert row["version"] == state["state_version"]
    stored = committed(pg, state["case_id"])
    assert row_to_state(state_to_row(stored)) == stored
    validate_workflow_state(stored)


def test_a_row_the_orchestrator_did_not_write_is_refused_not_guessed():
    pg = FakePostgrest()
    case_id = str(uuid.uuid4())
    pg.add_case(case_id, CLIENT)
    pg.tables["case_orchestration_state"][case_id] = {"case_id": case_id, "client_id": CLIENT, "version": 1,
                                                      "orchestrator_context": {}}
    with pytest.raises(StatePersistenceError, match="not written by the onboarding orchestrator"):
        store(pg)[case_id]


def test_the_first_write_needs_the_onboarding_cases_row_in_the_same_tenant():
    pg = FakePostgrest()
    case_id = str(uuid.uuid4())
    state = new_state(case_id=case_id, client_id=CLIENT, correlation_id="c", actor=HR, intake_trigger={})
    with pytest.raises(CaseOwnershipError):
        store(pg).save(state)  # no onboarding_cases row at all
    pg.add_case(case_id, "CLIENT-TEST-B")
    with pytest.raises(CaseOwnershipError):
        store(pg).save(state)  # the case belongs to another tenant
    assert pg.row(case_id) is None


def test_optimistic_concurrency_a_stale_writer_is_refused_by_the_database():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    case_id = flow.start()["case_id"]
    a, b = store(pg), store(pg)  # two orchestrator processes
    state_a, state_b = a.load(case_id), b.load(case_id)
    version = state_a["state_version"]
    state_b["state_version"] = version + 1
    state_b["next_action"] = "written-by-b"
    b.save(state_b)
    state_a["state_version"] = version + 1
    state_a["next_action"] = "written-by-a"
    with pytest.raises(StateVersionConflict):
        a.save(state_a)
    row = pg.row(case_id)
    assert (row["version"], row["orchestrator_context"]["state"]["next_action"]) == (version + 1, "written-by-b")


def test_concurrent_first_writes_create_one_row():
    pg = FakePostgrest()
    case_id = str(uuid.uuid4())
    pg.add_case(case_id, CLIENT)
    state = new_state(case_id=case_id, client_id=CLIENT, correlation_id="c", actor=HR, intake_trigger={})
    state["state_version"] = 1
    a, b = store(pg), store(pg)
    a.save(copy.deepcopy(state))
    with pytest.raises(StateVersionConflict):
        b.save(copy.deepcopy(state))
    assert len(pg.tables["case_orchestration_state"]) == 1


def test_errors_carry_no_credential_and_a_refused_key_is_not_retried_elsewhere():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    case_id = flow.start()["case_id"]
    wrong = "sk-wrong-" + "9" * 40
    with pytest.raises(StatePersistenceError) as err:
        store(pg, key=wrong)[case_id]
    assert "refused" in str(err.value) and wrong not in str(err.value) and KEY not in str(err.value)
    pg.down = True
    with pytest.raises(StatePersistenceUnavailable) as err:
        store(pg)[case_id]
    assert KEY not in str(err.value) and KEY not in repr(store(pg).__dict__.get("_client"))


# =========================================================================== A. complete workflow


def test_A_complete_workflow_every_stage_output_is_persisted_in_case_orchestration_state():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    state = flow.start()
    case_id = state["case_id"]
    assert is_case_uuid(case_id)  # UC01's onboarding_cases id: from here on the case lives in Supabase
    row = pg.row(case_id)
    assert row["client_id"] == CLIENT and row["correlation_id"] == state["correlation_id"]
    for stage in (S.INTAKE, S.PLAN_TASKS, S.DOCUMENT_COLLECTION):
        assert row["stage_statuses"][stage.value] == ST.COMPLETE.value
        assert row["stage_outcomes"][stage.value]["status"] == ST.COMPLETE.value
        assert stage.value in row["completed_stages"]
    assert row["workflow_status"] == W.WAITING_FOR_INPUT.value
    assert [a["action"] for a in row["pending_actions"]] == ["supply_documents"]
    # UC02's plan, persisted: UC03's collection is the plan version it was handed.
    plan = row["orchestrator_context"]["state"]["onboarding_plan"]
    assert plan["plan_version"] == 1
    assert row["orchestrator_context"]["state"]["document_collection"]["plan_version"] == 1
    # The provisional id resolves through the database.
    origin = row["orchestrator_context"]["origin_case_id"]
    assert origin.startswith("CASE-") and flow.agent.get_case(origin, client_id=CLIENT)["case_id"] == case_id
    first_version = row["version"]

    state = flow.supply(state)
    row = pg.row(case_id)
    for stage in (*SEQUENTIAL, *BRANCH_RESULTS):
        assert row["stage_statuses"][stage.value] == ST.COMPLETE.value, stage
        assert row["stage_outcomes"][stage.value]["status"] == ST.COMPLETE.value, stage
    assert {b: s["status"] for b, s in row["branch_statuses"].items()} == {b.value: "completed" for b in Branch}
    assert row["verified_context"]["plan_version"] == 1
    assert row["stage_statuses"][S.DAY_ONE_READINESS.value] == ST.AWAITING_APPROVAL.value
    assert row["workflow_status"] == W.WAITING_FOR_APPROVAL.value and row["version"] > first_version

    state = flow.approve_signoff(state)
    assert pg.row(case_id)["stage_statuses"][S.JOINING_DAY_WELCOME.value] == ST.WAITING.value
    state = flow.agent.trigger_joining_day(case_id, as_of=JOINING, actor=SCHEDULER)
    row = pg.row(case_id)
    assert (row["workflow_status"], row["overall_status"]) == (W.COMPLETED.value, "complete")
    assert row["pending_actions"] == [] and row["blockers"] == []
    assert row["stage_statuses"][S.JOINING_DAY_WELCOME.value] == ST.COMPLETE.value
    assert row["version"] == state["state_version"]
    assert committed(pg, case_id)["stage_statuses"] == state["stage_statuses"]
    validate_workflow_state(committed(pg, case_id))


def test_A_every_step_runs_in_a_new_process_from_the_committed_row_only(tmp_path):
    """Each step is a new orchestrator process: the next UC's input can come only from Supabase."""
    pg = FakePostgrest()
    first = supabase_flow(pg, persist_dir=tmp_path)
    state = first.start()
    case_id, plan, outstanding = state["case_id"], state["onboarding_plan"], first.outstanding(state)

    second = supabase_flow(pg, persist_dir=tmp_path)
    loaded = second.agent.get_case(case_id, client_id=CLIENT)
    assert loaded["onboarding_plan"] == plan and second.outstanding(loaded) == outstanding
    state = second.supply(loaded)
    assert (second.uc01.calls, second.plan.calls) == (0, 0)  # nothing before the wait ran again
    assert second.uc03.plan_versions == [1]  # UC03 validated against the persisted plan version
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value

    third = supabase_flow(pg, persist_dir=tmp_path)
    state = third.approve_signoff(third.agent.get_case(case_id, client_id=CLIENT))
    # Branch results came from the row: no branch stage ran again (only the welcome stage, which waits).
    assert (third.uc04.calls, third.uc05.calls["orientation"], third.uc05.calls["buddy"]) == (0, 0, 0)
    assert status(state, S.JOINING_DAY_WELCOME) == ST.WAITING.value

    fourth = supabase_flow(pg, persist_dir=tmp_path)
    state = fourth.agent.trigger_joining_day(case_id, as_of=JOINING, actor=SCHEDULER)
    assert state["workflow_status"] == W.COMPLETED.value
    assert pg.row(case_id)["workflow_status"] == W.COMPLETED.value


# =========================================================================== B. document correction


def test_B_a_correctable_document_waits_for_correction_without_replanning():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    flow.uc03.verdicts = {"passport": "correction_required"}
    state = flow.to_branches()
    case_id = state["case_id"]
    row = pg.row(case_id)
    assert row["stage_statuses"][S.VALIDATE_DOCUMENTS.value] == ST.WAITING_FOR_INPUT.value
    assert row["stage_statuses"][S.REPLAN_TASKS.value] == ST.NOT_REQUESTED.value
    assert all(row["stage_statuses"][s.value] == ST.PENDING.value for s in (S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING))
    assert {a["action"]: a["documents"] for a in row["pending_actions"]} == {"correct_documents": ["passport"]}

    flow.uc03.verdicts = {}
    state = flow.supply(state, ["passport"], tag="2")
    row = pg.row(case_id)
    assert row["stage_statuses"][S.VALIDATE_DOCUMENTS.value] == ST.COMPLETE.value
    assert row["stage_statuses"][S.REPLAN_TASKS.value] == ST.NOT_REQUESTED.value  # never replanned
    assert flow.replan.calls == 0 and flow.plan.calls == 1
    assert row["orchestrator_context"]["state"]["submitted_documents"]["passport"]["revision"] == 2
    assert all(row["stage_statuses"][s.value] == ST.COMPLETE.value for s in (S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING))


# =========================================================================== C. replanning


def _visa_passport():
    return synthetic_document("passport", visa_required=True)


def test_C_a_genuine_plan_change_reaches_uc02_and_uc03_revalidates_against_the_new_version():
    pg = FakePostgrest()
    flow, _service = real_uc02_supabase_flow(pg)
    state = flow.start()
    case_id = state["case_id"]
    documents = [synthetic_document(t) if t != "passport" else _visa_passport() for t in flow.outstanding(state)]
    state = flow.agent.submit_documents(case_id, documents=documents, actor=HR)
    row = pg.row(case_id)
    feedback = row["orchestrator_context"]["state"]["document_validation_feedback"]
    assert feedback["replanning_required"] is True and feedback["plan_version"] == 1
    assert row["stage_statuses"][S.REPLAN_TASKS.value] == ST.COMPLETE.value
    plan = row["orchestrator_context"]["state"]["onboarding_plan"]
    assert (plan["plan_version"], plan["previous_plan_version"]) == (2, 1)
    assert row["orchestrator_context"]["state"]["document_collection"]["plan_version"] == 2
    # Nothing downstream ran on the stale plan.
    assert all(row["stage_statuses"][s.value] == ST.PENDING.value for s in (S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING))

    state = flow.supply(state, ["visa_documents"])
    row = pg.row(case_id)
    assert flow.uc03.plan_versions == [1, 2]
    assert row["verified_context"]["plan_version"] == 2
    assert row["orchestrator_context"]["state"]["document_validation_feedback"]["plan_version"] == 2
    assert all(row["stage_statuses"][s.value] == ST.COMPLETE.value for s in (S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING))
    assert flow.gate.violations == []


# =========================================================================== D. parallel branches


def test_D_both_branches_run_in_parallel_and_both_outcomes_are_persisted():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    flow.gate.barrier = threading.Barrier(2, timeout=10)  # each branch waits for the other: they overlap
    state = flow.to_branches()
    row = pg.row(state["case_id"])
    assert flow.gate.threads["equipment_access"] != flow.gate.threads["orientation_scheduling"]
    assert row["orchestrator_context"]["state"]["equipment_access_result"]["overall_status"] == "READY"
    assert row["orchestrator_context"]["state"]["orientation_schedule_result"]["session_id"] == "SES-SYN-1"
    assert {b: s["status"] for b, s in row["branch_statuses"].items()} == {b.value: "completed" for b in Branch}
    assert row["stage_outcomes"][S.EQUIPMENT_ACCESS.value]["status"] == ST.COMPLETE.value
    assert row["stage_outcomes"][S.ORIENTATION_SCHEDULING.value]["status"] == ST.COMPLETE.value


def test_D_a_concurrent_writer_never_loses_a_branch_result_or_its_own_change():
    """Another orchestrator process writes the case while the branches run. The database refuses the
    stale branch write; the branch step is folded onto the newer state and written again; the run's
    final whole-state write is not made over the other writer's change; a resume completes the case."""
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    state = flow.start()
    case_id = state["case_id"]
    other = store(pg)
    injected = threading.Event()

    def another_process_writes(method, written_case, body):
        if (method == "PATCH" and not injected.is_set() and written_case == case_id
                and body.get("stage_statuses", {}).get(S.EQUIPMENT_ACCESS.value) == ST.COMPLETE.value):
            injected.set()
            theirs = other.load(case_id)
            theirs["evidence_references"] = [*theirs.get("evidence_references", []), "EVID-OTHER-PROCESS"]
            theirs["state_version"] += 1
            other.save(theirs)

    pg.before_write = another_process_writes
    with pytest.raises(StateVersionConflict, match="resume the case"):
        flow.supply(state)
    assert injected.is_set()
    pg.before_write = None
    row = pg.row(case_id)
    assert "EVID-OTHER-PROCESS" in row["orchestrator_context"]["state"]["evidence_references"]
    assert row["stage_statuses"][S.EQUIPMENT_ACCESS.value] == ST.COMPLETE.value
    assert row["orchestrator_context"]["state"]["equipment_access_result"]["overall_status"] == "READY"

    state = flow.agent.resume_case(case_id, actor=HR)
    row = pg.row(case_id)
    assert "EVID-OTHER-PROCESS" in row["orchestrator_context"]["state"]["evidence_references"]
    assert all(row["stage_statuses"][s.value] == ST.COMPLETE.value for s in BRANCH_RESULTS)
    assert row["stage_statuses"][S.DAY_ONE_READINESS.value] == ST.AWAITING_APPROVAL.value
    assert flow.uc04.evaluations == 1  # the stored branch result was used, not re-evaluated


def test_D_merge_waits_for_both_branches():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    flow.uc05.status["orientation"] = ST.AWAITING_APPROVAL
    state = flow.to_branches()
    row = pg.row(state["case_id"])
    assert row["stage_statuses"][S.EQUIPMENT_ACCESS.value] == ST.COMPLETE.value
    assert row["stage_statuses"][S.DAY_ONE_READINESS.value] == ST.PENDING.value  # no readiness on one branch
    assert row["workflow_status"] == W.WAITING_FOR_APPROVAL.value


# =========================================================================== E. failure and recovery


def test_E_supabase_unavailable_fails_loudly_writes_nowhere_else_and_resumes_after_recovery():
    pg = FakePostgrest()
    provisional = InMemoryCaseStateStore()
    flow = with_uuid_uc01(Flow(case_states=store(pg, provisional=provisional)), pg)
    state = flow.start()
    case_id = state["case_id"]
    version = pg.row(case_id)["version"]
    pg.down = True
    with pytest.raises(StatePersistenceUnavailable):
        flow.supply(state)
    assert case_id not in provisional  # no silent fallback for a real case
    pg.down = False
    assert pg.row(case_id)["version"] == version  # nothing half-written
    state = flow.supply(flow.agent.get_case(case_id, client_id=CLIENT))  # the redelivered submission
    assert status(state, S.DAY_ONE_READINESS) == ST.AWAITING_APPROVAL.value
    assert pg.row(case_id)["stage_statuses"][S.EQUIPMENT_ACCESS.value] == ST.COMPLETE.value


def test_E_a_duplicate_start_is_one_case_one_row_and_no_repeated_uc_call():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    first = flow.start()
    again = flow.start()
    assert again["case_id"] == first["case_id"]
    assert (flow.uc01.calls, flow.plan.calls, flow.notifier.sent_count()) == (1, 1, 1)
    assert len(pg.tables["case_orchestration_state"]) == 1


def test_E_uc02_failure_holds_the_case_before_any_document_request():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    flow.plan.status = ST.FAILED
    state = flow.start()
    row = pg.row(state["case_id"])
    assert row["stage_statuses"][S.PLAN_TASKS.value] == ST.FAILED.value
    assert row["last_error_code"] == "UC02_PACKAGE_UNIMPORTABLE"
    assert row["stage_statuses"][S.DOCUMENT_COLLECTION.value] == ST.PENDING.value
    assert row["workflow_status"] == W.FAILED.value and flow.notifier.sent_count() == 0


def test_E_uc03_technical_failure_is_retry_pending_and_recovers_on_retry():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    flow.uc03.fail_next = 1
    state = flow.to_branches()
    row = pg.row(state["case_id"])
    assert row["stage_statuses"][S.VALIDATE_DOCUMENTS.value] == ST.RETRY_PENDING.value
    assert row["retry_metadata"] or row["stage_outcomes"][S.VALIDATE_DOCUMENTS.value]["error"]
    state = flow.agent.retry_stage(state["case_id"], S.VALIDATE_DOCUMENTS.value, actor=HR)
    assert pg.row(state["case_id"])["stage_statuses"][S.VALIDATE_DOCUMENTS.value] == ST.COMPLETE.value


def _failing_uc04(op, payload, **kwargs):
    return WorkerResponse(run_id="r", ok=False, error_code="INTERNAL_ERROR", error_detail="UC04 desk failure (double)")


def test_E_uc04_failure_is_never_complete_and_uc05s_result_is_preserved():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    flow.uc04.call = _failing_uc04
    state = flow.to_branches()
    row = pg.row(state["case_id"])
    assert row["stage_statuses"][S.EQUIPMENT_ACCESS.value] == ST.FAILED.value
    assert row["branch_statuses"][Branch.EQUIPMENT_ACCESS.value]["status"] == "failed"
    assert row["stage_statuses"][S.ORIENTATION_SCHEDULING.value] == ST.COMPLETE.value  # the other branch kept
    assert row["orchestrator_context"]["state"]["orientation_schedule_result"]["session_id"] == "SES-SYN-1"
    assert row["stage_statuses"][S.DAY_ONE_READINESS.value] == ST.PENDING.value
    assert row["workflow_status"] == W.FAILED.value


def test_E_uc05_scheduling_failure_blocks_signoff_and_uc04s_result_is_preserved():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    flow.uc05.status["orientation"] = ST.FAILED
    state = flow.to_branches()
    row = pg.row(state["case_id"])
    assert row["stage_statuses"][S.ORIENTATION_SCHEDULING.value] == ST.FAILED.value
    assert row["stage_statuses"][S.EQUIPMENT_ACCESS.value] == ST.COMPLETE.value
    assert row["orchestrator_context"]["state"]["equipment_access_result"]["overall_status"] == "READY"
    assert row["stage_statuses"][S.DAY_ONE_READINESS.value] == ST.PENDING.value
    assert not flow.ctx.hitl.list(status=Decision.PENDING, gate="day_one_signoff", workflow_instance=state["case_id"],
                                  client_id=CLIENT)


def test_E_a_process_stopping_after_a_successful_stage_resumes_from_the_committed_row(tmp_path):
    pg = FakePostgrest()
    flow = supabase_flow(pg, persist_dir=tmp_path)
    state = flow.start()
    case_id = state["case_id"]

    class StopsTheProcess:  # UC03's call begins, and the process is killed
        def wait(self, name):
            raise SimulatedCrash()

        def done(self, name):
            pass

    flow.uc03.gate = StopsTheProcess()
    with pytest.raises(SimulatedCrash):
        flow.supply(state)
    row = pg.row(case_id)
    assert row["stage_statuses"][S.DOCUMENT_SUBMISSION.value] == ST.COMPLETE.value  # committed before the stop
    assert row["stage_statuses"][S.VALIDATE_DOCUMENTS.value] != ST.COMPLETE.value

    restarted = supabase_flow(pg, persist_dir=tmp_path)
    state = restarted.agent.resume_case(case_id, actor=HR)
    assert status(state, S.VALIDATE_DOCUMENTS) == ST.COMPLETE.value
    assert (restarted.uc01.calls, restarted.plan.calls, restarted.notifier.sent_count()) == (0, 0, 0)
    assert pg.row(case_id)["stage_statuses"][S.DAY_ONE_READINESS.value] == ST.AWAITING_APPROVAL.value


# =========================================================================== F. tenant isolation


def test_F_another_tenant_can_neither_read_nor_change_the_case_and_the_query_is_tenant_scoped():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    state = flow.start()
    case_id = state["case_id"]
    before = pg.row(case_id)
    pg.requests.clear()
    with pytest.raises(KeyError):
        flow.agent.get_case(case_id, client_id=OTHER_TENANT_HR["client_id"])
    # The database was asked for the other tenant's row only -- the tenant filter is in the query.
    assert ("GET", "case_orchestration_state") in {(m, t) for m, t, _ in pg.requests}
    assert all(p.get("client_id") == f"eq.{OTHER_TENANT_HR['client_id']}" for m, t, p in pg.requests
               if (m, t) == ("GET", "case_orchestration_state"))
    for call in (lambda: flow.agent.submit_documents(case_id, documents=[synthetic_document("passport")],
                                                     actor=OTHER_TENANT_HR),
                 lambda: flow.agent.resume_case(case_id, actor=OTHER_TENANT_HR),
                 lambda: flow.agent.retry_stage(case_id, "document_collection", actor=OTHER_TENANT_HR)):
        with pytest.raises(KeyError):
            call()
    assert pg.row(case_id) == before


def test_F_a_write_can_never_touch_another_tenants_row():
    pg = FakePostgrest()
    flow = supabase_flow(pg)
    case_id = flow.start()["case_id"]
    before = pg.row(case_id)
    intruder = store(pg)
    state = intruder.load(case_id)
    state["client_id"] = "CLIENT-TEST-B"
    state["state_version"] += 1
    with pytest.raises(CaseOwnershipError):
        intruder.save(state)
    assert pg.row(case_id) == before
    assert intruder.load(case_id, client_id="CLIENT-TEST-B") is None


# =========================================================================== G. joining-date change


def test_G_a_joining_date_change_reopens_stale_branch_results_and_readiness_cannot_use_them():
    pg = FakePostgrest()
    flow, _ = real_uc02_supabase_flow(pg)
    state = flow.to_branches()
    case_id = state["case_id"]
    (old_signoff,) = flow.ctx.hitl.list(status=Decision.PENDING, gate="day_one_signoff", workflow_instance=case_id,
                                        client_id=CLIENT)
    new = date(2026, 12, 15)
    state, outcome = flow.agent.request_uc02_date_change(case_id, joining_date=new, actor=HR)
    state = flow.agent.decide_approval(case_id, outcome["request_id"], decision="approved", actor=HR_2)
    row = pg.row(case_id)
    context = row["orchestrator_context"]["state"]
    assert (context["joining_date"], context["joining_date_revision"]) == (new.isoformat(), 2)
    # Both branches ran again for the new date; their persisted inputs say so.
    for stage in (S.EQUIPMENT_ACCESS, S.ORIENTATION_SCHEDULING):
        assert row["stage_outcomes"][stage.value]["inputs"]["joining_date"] == new.isoformat()
    assert flow.uc04.evaluations == 2
    # The signoff requested for the original date was withdrawn and cannot complete readiness.
    assert flow.ctx.hitl.get(old_signoff.id, client_id=CLIENT).status == Decision.WITHDRAWN
    with pytest.raises(HITLError):
        flow.agent.decide_approval(case_id, old_signoff.id, decision="approved", actor=HR_2)
    assert row["workflow_status"] != W.COMPLETED.value
    (signoff,) = flow.ctx.hitl.list(status=Decision.PENDING, gate="day_one_signoff", workflow_instance=case_id,
                                    client_id=CLIENT)
    assert signoff.payload["joining_date_revision"] == 2
    assert code(state, S.ORIENTATION_SCHEDULING) is None
