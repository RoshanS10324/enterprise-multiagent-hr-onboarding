"""Phase 3: the database persistence design, proved against fakes only. No database, no network.

* `DatabaseCaseRepository` must give the service exactly what `CaseStore` gives (differential tests), keep state across a restart, write nothing
  on a failure, refuse a concurrent writer, and never cross a client boundary.
* The adapters on the shared tables (parent case, audit, runs) must produce rows that fit the VERIFIED live columns: a fixture holds those
  columns and the fake table client refuses an unknown column or a missing required one.
* The structure for UC-04's own state is pending the team's approval; the repository sits on a backend interface, tested here with the in-memory one.
"""

import json
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helpers import ALL, approvals, make, new_environment, rid
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk
from sub_agent.audit import AuditLog
from sub_agent.direct import DirectEntry
from sub_agent.fixtures import CALLER_ID, CALLER_ROLES, fixed_clock
from sub_agent.persistence import (DatabaseAgentRunRecorder, DatabaseAuditSink, DatabaseCaseContext, DatabaseCaseRepository, InMemoryStateBackend,
                                   InMemoryTableClient, PersistenceError, SchemaMismatchError, TableSchema, TenantScopeError, UnsupportedParentStatus,
                                   VersionConflict)
from sub_agent.persistence.mappers import PARENT_STATUS, parent_status
from sub_agent.ports import AdapterTimeout
from sub_agent.runs import AgentRunRecord
from sub_agent.service import Uc04Service
from sub_agent.states import ParentCaseStatus
from sub_agent.store import CaseRepository, CaseStore
from sub_agent.service import IdempotencyConflict

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "live_schema_subset.json").read_text(encoding="utf-8"))["tables"]
CASE = "3c88238c-5d89-4f7b-a3b9-92db4c4c98b7"
OTHER_CASE = "9f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f"


def schemas() -> dict[str, TableSchema]:
    return {t: TableSchema(frozenset(v["columns"]), frozenset(v["required"]), tuple(v["primary_key"])) for t, v in FIXTURE.items()}


def build(store, desk=None, **service_kw):
    _, cases, desk = new_environment(it_desk=desk)
    service = Uc04Service(cases=cases, it_desk=desk, audit=AuditLog(fixed_clock), store=store, **service_kw)
    return service, DirectEntry(service), desk, cases


def ev(direct, payload, key):
    return direct.evaluate(payload, actor_id=CALLER_ID, roles=CALLER_ROLES, idempotency_key=key)


def sub(direct, payload):
    return direct.submit_eligible(payload, actor_id=CALLER_ID, roles=CALLER_ROLES)


# ---------------------------------------------------------------------------------------------------- the repository is a CaseRepository
def test_the_database_repository_satisfies_the_repository_interface():
    assert isinstance(DatabaseCaseRepository(InMemoryStateBackend()), CaseRepository)


# ---------------------------------------------------------------------------------------------------- differential: same decisions as CaseStore
def run_flow_a(store):
    service, direct, desk, cases = build(store)
    obs = [ev(direct, make(), "k1").business_decision(), ev(direct, make(), "k1").business_decision()]
    obs.append(ev(direct, make(approval_records=approvals(ALL)), "k2").business_decision())
    with pytest.raises(IdempotencyConflict):
        ev(direct, make(approval_records=approvals(ALL)), "k1")
    first = sub(direct, make(approval_records=approvals(ALL)))
    again = sub(direct, make(approval_records=approvals(ALL)))
    obs += [first.result.business_decision(), (first.submitted, first.failed, first.skipped), (again.submitted, again.failed, again.skipped), len(desk.calls)]
    cases.set_status("SYN-CASE-001", ParentCaseStatus.WITHDRAWN)
    obs.append(ev(direct, make(approval_records=approvals(ALL)), "k3").business_decision())
    return obs, [e["event_type"] for e in service.audit.events]


def run_flow_b(store):
    service, direct, desk, _ = build(store, MockITServiceDesk(fail_with=AdapterTimeout("x")))
    payload = make(approval_records=approvals(["201"]))
    first, second = sub(direct, payload), sub(direct, payload)
    acknowledged = service.acknowledge_adapter_error("SYN-CASE-001", rid("201"), CALLER_ROLES)
    desk.fail_with = None
    third = sub(direct, payload)
    return ([(r.submitted, r.failed, r.skipped) for r in (first, second, third)], acknowledged, len(desk.calls),
            [i.submission_attempts for i in third.result.items if i.request_id == rid("201")]), [e["event_type"] for e in service.audit.events]


@pytest.mark.parametrize("flow", [run_flow_a, run_flow_b], ids=["approvals-replay-submit-withdraw", "adapter-failure-acknowledge-retry"])
def test_the_database_repository_decides_and_audits_exactly_like_the_in_memory_store(flow):
    assert flow(DatabaseCaseRepository(InMemoryStateBackend())) == flow(CaseStore())


# ---------------------------------------------------------------------------------------------------- durability, atomicity, concurrency
def test_state_survives_a_restart_replays_are_served_and_nothing_is_sent_twice():
    backend = InMemoryStateBackend()
    service, direct, desk, cases = build(DatabaseCaseRepository(backend))
    payload = make(approval_records=approvals(ALL))
    first = ev(direct, payload, "k1")
    # a new process: new repository, new service, same stored state and same desk
    service2, direct2, _, _ = build(DatabaseCaseRepository(backend), desk=desk)
    again = ev(direct2, payload, "k1")
    assert any(e["event_type"] == "replay.served" for e in service2.audit.events)  # the stored result was served after the restart
    assert len(sub(direct2, payload).submitted) == 4
    service3, direct3, _, _ = build(DatabaseCaseRepository(backend), desk=desk)
    assert sub(direct3, payload).submitted == [] and len(desk.calls) == 4  # tickets were remembered: nothing sent twice
    assert first.business_decision()["overall_status"] != "" and again.case_id == first.case_id


def test_an_adapter_error_survives_a_restart_and_still_blocks_a_blind_retry():
    backend = InMemoryStateBackend()
    desk = MockITServiceDesk(fail_with=AdapterTimeout("x"))
    _, direct, _, _ = build(DatabaseCaseRepository(backend), desk)
    payload = make(approval_records=approvals(["201"]))
    assert sub(direct, payload).failed
    service2, direct2, _, _ = build(DatabaseCaseRepository(backend), desk=desk)
    desk.fail_with = None
    again = sub(direct2, payload)
    assert again.submitted == [] and again.skipped[0]["reason"] == "PREVIOUS_ADAPTER_ERROR_UNRESOLVED" and len(desk.calls) == 1
    assert service2.acknowledge_adapter_error("SYN-CASE-001", rid("201"), CALLER_ROLES) is True
    assert sub(direct2, payload).submitted == [rid("201")]


def test_a_storage_failure_writes_nothing_and_the_retry_behaves_as_a_first_attempt():
    backend = InMemoryStateBackend()
    _, direct, _, _ = build(DatabaseCaseRepository(backend))
    backend.fail_with = OSError("database unavailable")
    with pytest.raises(PersistenceError) as e:
        ev(direct, make(), "k1")
    assert "unavailable" not in str(e.value) and "OSError" in str(e.value)  # the class, never the message
    backend.fail_with = None
    assert backend.case_ids() == []
    service, direct, _, _ = build(DatabaseCaseRepository(backend))
    ev(direct, make(), "k1")
    assert not any(x["event_type"] == "replay.served" for x in service.audit.events)


def test_a_failure_during_evaluation_is_rolled_back_and_never_written(monkeypatch):
    backend = InMemoryStateBackend()
    service, direct, _, _ = build(DatabaseCaseRepository(backend))
    monkeypatch.setattr(service, "_assess", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        ev(direct, make(), "k1")
    assert backend.saves == 0 and backend.case_ids() == []
    assert any(e["event_type"] == "state.rolled_back" for e in service.audit.events)


def test_a_concurrent_writer_is_refused_and_the_first_commit_stands():
    backend = InMemoryStateBackend()
    a, b = DatabaseCaseRepository(backend), DatabaseCaseRepository(backend)
    for repo in (a, b):
        repo.begin("SYN-CASE-001")
    ra = a.ensure("SYN-CASE-001", "SYN-EMP-001", "client_alpha")
    rb = b.ensure("SYN-CASE-001", "SYN-EMP-001", "client_alpha")
    ra.adapter_errors["x"] = "first"
    rb.adapter_errors["x"] = "second"
    a.commit("SYN-CASE-001")
    with pytest.raises(VersionConflict):
        b.commit("SYN-CASE-001")
    assert backend.load("SYN-CASE-001").record.adapter_errors == {"x": "first"}


def test_the_version_moves_with_the_case_state_but_not_with_a_stored_replay_result():
    backend = InMemoryStateBackend()
    repo = DatabaseCaseRepository(backend)
    _, direct, _, _ = build(repo)
    ev(direct, make(), "k1")
    v1 = repo.version_for("SYN-CASE-001")
    ev(direct, make(), "k1")  # a replay: no state change
    ev(direct, make(), "k2")  # same state, new key: only a new stored result
    assert repo.version_for("SYN-CASE-001") == v1
    ev(direct, make(approval_records=approvals(ALL)), "k3")  # the state moved
    assert repo.version_for("SYN-CASE-001") > v1


# ---------------------------------------------------------------------------------------------------- tenant scoping
def test_the_repository_never_hands_out_another_clients_case():
    repo = DatabaseCaseRepository(InMemoryStateBackend())
    repo.begin("SYN-CASE-001")
    repo.ensure("SYN-CASE-001", "SYN-EMP-001", "client_alpha")
    repo.commit("SYN-CASE-001")
    repo.begin("SYN-CASE-001")
    with pytest.raises(TenantScopeError):
        repo.ensure("SYN-CASE-001", "SYN-EMP-001", "client_bravo")
    with pytest.raises(PersistenceError):
        DatabaseCaseRepository(InMemoryStateBackend()).ensure("SYN-CASE-009", "SYN-EMP-009", "client_alpha")  # not inside a unit of work


def case_row(case_id=CASE, employee="EMP-000001", client="CLIENT-TEST-A", status="PROVISIONING_ACTIVE"):
    return {"case_id": case_id, "employee_no": employee, "client_id": client, "status": status}


def test_a_soft_deleted_case_is_withdrawn_and_never_the_employees_case():
    deleted = {**case_row(), "is_deleted": True}
    ctx = DatabaseCaseContext(InMemoryTableClient(schemas(), {"onboarding_cases": [deleted, case_row(OTHER_CASE)]}))
    assert ctx.get_case(CASE).status == ParentCaseStatus.WITHDRAWN  # whatever its status column says
    assert ctx.case_for_employee("EMP-000001", client_id="CLIENT-TEST-A") == OTHER_CASE  # the live case, not an error


def test_the_case_context_scopes_every_employee_lookup_to_one_client():
    client = InMemoryTableClient(schemas(), {"onboarding_cases": [case_row(), case_row(OTHER_CASE, client="CLIENT-OTHER")]})
    ctx = DatabaseCaseContext(client)
    assert ctx.case_for_employee("EMP-000001", client_id="CLIENT-TEST-A") == CASE
    assert ctx.case_for_employee("EMP-000001", client_id="CLIENT-OTHER") == OTHER_CASE
    assert ctx.case_for_employee("EMP-000001", client_id="CLIENT-NONE") is None
    with pytest.raises(TenantScopeError):
        ctx.case_for_employee("EMP-000001")  # an unscoped lookup is refused: employee numbers are unique per client only
    with pytest.raises(TenantScopeError):
        ctx.case_for_employee("EMP-000001", client_id="")


def test_the_case_context_is_read_only_and_maps_the_parent_case():
    writes = []

    class Watching(InMemoryTableClient):
        def insert(self, table, row): writes.append(table); super().insert(table, row)
        def upsert(self, table, row, *, on_conflict): writes.append(table); super().upsert(table, row, on_conflict=on_conflict)

    ctx = DatabaseCaseContext(Watching(schemas(), {"onboarding_cases": [case_row()]}))
    parent = ctx.get_case(CASE)
    assert (parent.case_id, parent.employee_ref, parent.client_id, parent.status) == (CASE, "EMP-000001", "CLIENT-TEST-A", ParentCaseStatus.ACTIVE)
    assert ctx.get_case("SYN-CASE-001") is None and ctx.get_case(OTHER_CASE) is None
    assert writes == []


def test_the_case_context_fails_closed_on_unknown_states_missing_numbers_and_ambiguity():
    ctx = DatabaseCaseContext(InMemoryTableClient(schemas(), {"onboarding_cases": [case_row(status="COMPLETED"), case_row(OTHER_CASE, employee=None)]}))
    with pytest.raises(UnsupportedParentStatus):
        ctx.get_case(CASE)  # COMPLETED has no defined UC-04 behaviour (decision pending)
    assert ctx.get_case(OTHER_CASE) is None  # no employee number yet: treated as not found
    twice = DatabaseCaseContext(InMemoryTableClient(schemas(), {"onboarding_cases": [case_row(), case_row(OTHER_CASE)]}))
    with pytest.raises(PersistenceError):
        twice.case_for_employee("EMP-000001", client_id="CLIENT-TEST-A")


def test_every_database_status_is_mapped_or_refused_never_guessed():
    live = ["INTAKE_PENDING", "PLAN_PENDING", "DOCUMENTS_PENDING", "VALIDATION_IN_PROGRESS", "PROVISIONING_ACTIVE", "ORIENTATION_SCHEDULED", "READY_FOR_DAY_ONE",
            "COMPLETED", "ON_HOLD", "WITHDRAWN", "CANCELLED"]
    for status in live:
        if status == "COMPLETED":
            with pytest.raises(UnsupportedParentStatus):
                parent_status(status)
        else:
            assert parent_status(status) in ParentCaseStatus
    assert set(PARENT_STATUS) | {"COMPLETED"} == set(live)
    with pytest.raises(UnsupportedParentStatus):
        parent_status("SOMETHING_NEW")
    with pytest.raises(UnsupportedParentStatus):
        parent_status(None)


def test_a_failing_client_surfaces_as_a_persistence_error_without_its_message():
    ctx = DatabaseCaseContext(InMemoryTableClient(schemas(), fail_with=OSError("host db.internal refused")))
    with pytest.raises(PersistenceError) as e:
        ctx.get_case(CASE)
    assert "db.internal" not in str(e.value)


# ---------------------------------------------------------------------------------------------------- audit rows fit the verified live columns
ENTRY = {"timestamp": "2026-10-15T00:00:00+00:00", "case_id": CASE, "request_id": f"{CASE}:SYN-REQ-201", "rule_id": "BR-17", "event_type": "readiness.evaluated",
         "previous_status": "AWAITING_APPROVAL", "new_status": "IN_PROGRESS", "reason": "all approved", "next_action": None, "owner": "HR_OFFICER"}
CONTEXT = {"client_id": "CLIENT-TEST-A", "actor_id": "user:hr-admin-01", "run_id": "run-1", "invocation_mode": "PARENT_ORCHESTRATED"}


def test_an_audit_row_fits_the_live_table_exactly():
    client = InMemoryTableClient(schemas())
    DatabaseAuditSink(client).write(ENTRY, CONTEXT)
    (row,) = client.tables["audit_events"]
    assert set(row) <= set(FIXTURE["audit_events"]["columns"]) and set(FIXTURE["audit_events"]["required"]) <= set(row)
    assert (row["client_id"], row["actor_id"], row["actor_type"], row["action"], row["outcome"], row["reference"], row["correlation_id"]) == (
        "CLIENT-TEST-A", "user:hr-admin-01", "AGENT", "readiness.evaluated", "SUCCESS", f"{CASE}:SYN-REQ-201", "run-1")
    assert row["before_state"] == {"status": "AWAITING_APPROVAL"} and row["after_state"] == {"status": "IN_PROGRESS"}
    assert row["detail"] == {"rule_id": "BR-17", "reason": "all approved", "owner": "HR_OFFICER", "invocation_mode": "PARENT_ORCHESTRATED"}
    assert set(row["detail"]).isdisjoint({"name", "full_name", "email", "phone", "nationality", "passport", "address"})  # the table's personal-data key list


def test_failure_events_are_marked_and_a_direct_caller_is_a_person():
    sink = DatabaseAuditSink(InMemoryTableClient(schemas()))
    assert sink.row({**ENTRY, "event_type": "state.rolled_back"}, {**CONTEXT, "invocation_mode": "DIRECT"})["outcome"] == "FAILURE"
    assert sink.row(ENTRY, {**CONTEXT, "invocation_mode": "DIRECT"})["actor_type"] == "HUMAN"


def test_an_audit_event_without_a_client_or_actor_is_refused_unless_a_stand_in_client_is_configured():
    with pytest.raises(PersistenceError):
        DatabaseAuditSink(InMemoryTableClient(schemas())).row(ENTRY, {k: v for k, v in CONTEXT.items() if k != "client_id"})
    with pytest.raises(PersistenceError):
        DatabaseAuditSink(InMemoryTableClient(schemas()), unresolved_client_id="UNRESOLVED").row(ENTRY, {k: v for k, v in CONTEXT.items() if k != "actor_id"})
    row = DatabaseAuditSink(InMemoryTableClient(schemas()), unresolved_client_id="UNRESOLVED").row(ENTRY, {k: v for k, v in CONTEXT.items() if k != "client_id"})
    assert row["client_id"] == "UNRESOLVED"


def test_a_synthetic_case_id_cannot_be_written_to_a_uuid_column():
    with pytest.raises(PersistenceError):
        DatabaseAuditSink(InMemoryTableClient(schemas())).row({**ENTRY, "case_id": "SYN-CASE-001"}, CONTEXT)
    assert "case_id" not in DatabaseAuditSink(InMemoryTableClient(schemas())).row({**ENTRY, "case_id": None}, CONTEXT)


def test_the_fake_client_really_refuses_a_row_that_does_not_fit():
    client = InMemoryTableClient(schemas())
    with pytest.raises(SchemaMismatchError):
        client.insert("audit_events", {"client_id": "c", "actor_type": "HUMAN", "actor_id": "a", "action": "x", "invented_column": 1})
    with pytest.raises(SchemaMismatchError):
        client.insert("audit_events", {"client_id": "c", "action": "x"})  # required columns left out


# ---------------------------------------------------------------------------------------------------- agent run rows fit the verified live columns
RUN = AgentRunRecord(run_id="corr-9", operation="evaluate", invocation_mode="PARENT_ORCHESTRATED", case_id=CASE, client_id="CLIENT-TEST-A", actor_id="user:hr-admin-01",
                     status="proposed", proposal_count=1, exception_count=0, tool_calls=[], started_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
                     finished_at=datetime(2026, 11, 2, 0, 0, 1, tzinfo=timezone.utc))


def test_an_agent_run_row_fits_the_live_table_and_uses_a_uc04_specific_run_id():
    client = InMemoryTableClient(schemas())
    DatabaseAgentRunRecorder(client).record(RUN)
    (row,) = client.tables["agent_runs"]
    assert set(row) <= set(FIXTURE["agent_runs"]["columns"]) and set(FIXTURE["agent_runs"]["required"]) <= set(row)
    assert row["run_id"] == "uc04:corr-9:evaluate" and row["status"] == "proposed" and row["tool_calls"] == []


def test_recording_the_same_run_twice_is_harmless_and_a_synthetic_case_id_is_refused():
    client = InMemoryTableClient(schemas())
    recorder = DatabaseAgentRunRecorder(client)
    recorder.record(RUN)
    recorder.record(RUN)
    assert len(client.tables["agent_runs"]) == 1
    with pytest.raises(PersistenceError):
        recorder.record(RUN.model_copy(update={"case_id": "SYN-CASE-001"}))


def test_the_live_schema_fixture_describes_the_three_tables_the_adapters_use():
    assert set(FIXTURE) == {"onboarding_cases", "audit_events", "agent_runs"}
    assert len(FIXTURE["onboarding_cases"]["columns"]) == 30 and len(FIXTURE["audit_events"]["columns"]) == 15 and len(FIXTURE["agent_runs"]["columns"]) == 18
    assert {"case_id", "employee_no", "client_id", "status"} <= set(FIXTURE["onboarding_cases"]["columns"])  # what the case context reads


# ---------------------------------------------------------------------------------------------------- end to end, platform identifiers, all over fakes
def run_platform(script: str) -> dict:
    code = "import sys\nsys.path[:0] = [r'%s', r'%s', r'%s']\nimport guardrails.identity as i\ni.configure('PLATFORM')\n" % (ROOT / "src", ROOT / "tests", ROOT) + textwrap.dedent(script)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=180)
    assert out.returncode == 0, out.stderr[-800:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_end_to_end_with_platform_identifiers_over_fake_tables_and_a_fake_state_backend():
    out = run_platform('''
        import json
        from pathlib import Path
        from helpers import ALL, approval, make
        from sub_agent.agent import Uc04Agent
        from sub_agent.persistence import *
        F = json.loads(Path(r"%s").read_text(encoding="utf-8"))["tables"]
        schemas = {t: TableSchema(frozenset(v["columns"]), frozenset(v["required"]), tuple(v["primary_key"])) for t, v in F.items()}
        CASE, OTHER = "%s", "%s"
        client = InMemoryTableClient(schemas, {"onboarding_cases": [
            {"case_id": CASE, "employee_no": "EMP-000001", "client_id": "CLIENT-TEST-A", "status": "PROVISIONING_ACTIVE"},
            {"case_id": OTHER, "employee_no": "EMP-000001", "client_id": "CLIENT-OTHER", "status": "INTAKE_PENDING"}]})
        backend = InMemoryStateBackend()
        def agent():
            return Uc04Agent(cases=DatabaseCaseContext(client), store=DatabaseCaseRepository(backend),
                             audit_sink=DatabaseAuditSink(client, unresolved_client_id="UNRESOLVED"), run_recorder=DatabaseAgentRunRecorder(client))
        _p = make(case_id=CASE, employee_ref="EMP-000001", client_id="CLIENT-TEST-A")
        _p["config"]["client_id"] = "CLIENT-TEST-A"
        _p["approval_records"] = [approval(k, client="CLIENT-TEST-A", request_id=CASE + ":SYN-REQ-" + k) for k in ALL]
        def payload():
            return json.loads(json.dumps(_p))  # one fixed payload: the helper numbers approvals, so a rebuilt one would differ
        a = agent()
        r1 = a.evaluate(payload(), actor_id="user:hr-admin-01", idempotency_key="k1")
        b = agent()                                    # a new process over the same stored state
        r2 = b.evaluate(payload(), actor_id="user:hr-admin-01", idempotency_key="k1")
        s1 = b.submit_eligible(payload(), actor_id="user:hr-admin-01")
        c = agent()
        s2 = c.submit_eligible(payload(), actor_id="user:hr-admin-01")
        mismatch = None
        try:
            b.evaluate(dict(payload(), client_id="CLIENT-OTHER"), actor_id="user:hr-admin-01", idempotency_key="k9")
        except Exception as e:
            mismatch = type(e).__name__
        audit = client.tables["audit_events"]; runs = client.tables["agent_runs"]
        print(json.dumps({
            "first": r1.overall_status.value, "submitted": len(s1.submitted), "resubmitted": len(s2.submitted), "replay": any(e["event_type"] == "replay.served" for e in b.service.audit.events),
            "audit_rows": len(audit), "audit_matches_memory": len(audit) + sum(len(x.service.audit.unpersisted) for x in (a, b, c)) == len(a.service.audit.events) + len(b.service.audit.events) + len(c.service.audit.events),
            "unpersisted": sum(len(x.service.audit.unpersisted) for x in (a, b, c)),
            "audit_clients": sorted({r["client_id"] for r in audit}), "audit_actor_ids": sorted({r["actor_id"] for r in audit}),
            "runs": sorted(r["run_id"].split(":")[2] for r in runs), "run_cases": sorted({r["case_id"] for r in runs}),
            "stored_items": len(backend.load(CASE).record.items), "stored_for_other_client": backend.load(OTHER) is not None, "mismatch": mismatch}))
    ''' % (ROOT / "tests" / "fixtures" / "live_schema_subset.json", CASE, OTHER_CASE))
    assert out["first"] == "IN_PROGRESS" and out["submitted"] == 4 and out["resubmitted"] == 0 and out["replay"] is True
    assert out["audit_matches_memory"] is True and out["audit_rows"] > 10
    assert out["audit_clients"] == ["CLIENT-TEST-A"] and out["audit_actor_ids"] == ["user:hr-admin-01"]  # early events are held and written once the verified client is known
    assert out["unpersisted"] > 0  # the refused cross-client call never verified a client: its events stay in memory, attributed to no client
    # a direct call without a run id gets one derived from its payload, so an identical repeat updates its row instead of adding one
    assert out["runs"] == ["evaluate", "submit_eligible"] and out["run_cases"] == [CASE]
    assert out["stored_items"] == 4 and out["stored_for_other_client"] is False
    assert out["mismatch"] == "CaseMismatchError"  # another client's id on this case is refused, never merged


# ---------------------------------------------------------------------------------------------------- audit entries recorded before the client is known
def test_early_audit_entries_are_held_and_written_in_order_once_the_verified_client_is_bound():
    from sub_agent.audit import InMemoryAuditSink
    sink = InMemoryAuditSink()
    log = AuditLog(fixed_clock, sink=sink, defer_unattributed=True)
    with log.context(actor_id="user:hr-admin-01", run_id="r1"):
        log.record("invocation.received", case_id=CASE)
        log.record("input.validated", case_id=CASE)
        assert sink.records == []  # nothing is written for an unknown client
        log.bind_context(client_id="CLIENT-TEST-A")
        assert [r[0]["event_type"] for r in sink.records] == ["invocation.received", "input.validated"]
        assert all(r[1]["client_id"] == "CLIENT-TEST-A" for r in sink.records)
        log.record("case.resolved", case_id=CASE)
    assert len(sink.records) == 3 and log.unpersisted == []


def test_entries_that_never_get_a_client_stay_in_memory_and_are_attributed_to_nobody():
    from sub_agent.audit import InMemoryAuditSink
    sink = InMemoryAuditSink()
    log = AuditLog(fixed_clock, sink=sink, defer_unattributed=True)
    with log.context(actor_id="user:hr-admin-01"):
        log.record("case.unknown", case_id=CASE)
    assert sink.records == [] and [e["event_type"] for e in log.unpersisted] == ["case.unknown"] and len(log.events) == 1


def test_without_deferral_an_unattributed_entry_is_still_refused_by_the_database_sink():
    log = AuditLog(fixed_clock, sink=DatabaseAuditSink(InMemoryTableClient(schemas())))
    with pytest.raises(PersistenceError):
        with log.context(actor_id="user:hr-admin-01"):
            log.record("case.unknown", case_id=CASE)
