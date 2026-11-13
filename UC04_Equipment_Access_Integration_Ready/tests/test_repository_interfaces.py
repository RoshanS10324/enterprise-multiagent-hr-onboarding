"""Phase 1 interfaces: CaseRepository, AuditSinkPort and AgentRunRecorderPort.

They are extracted from existing behaviour and add no business logic. Every test here shows one of two things: the default (no sink, no
recorder, in-memory store) behaves exactly as before, or an injected implementation sees exactly what the in-memory one produces.
Nothing here touches a network or a database.
"""

import re
from pathlib import Path

import pytest

from helpers import ALL, approvals, codes, make, new_environment, rid
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk
from sub_agent.agent import Uc04Agent
from sub_agent.audit import FIELDS, AuditLog, InMemoryAuditSink
from sub_agent.direct import DirectEntry
from sub_agent.entrypoints.orchestrated import Actor, OrchestratedEntry, OrchestratorRequest, WorkflowRef
from sub_agent.fixtures import CALLER_ID, CALLER_ROLES, fixed_clock
from sub_agent.guardrails import PrivacyViolation
from sub_agent.orchestrated import ProvenanceOut
from sub_agent.policy import PolicyViolation
from sub_agent.ports import AdapterTimeout
from sub_agent.runs import AGENT_ID, AGENT_VERSION, SUBMIT_TOOL, InMemoryAgentRunRecorder
from sub_agent.service import Uc04Service
from sub_agent.store import CaseRepository, CaseStore

SRC = Path(__file__).resolve().parents[1] / "src" / "sub_agent"


class RecordingRepository:
    """A repository that is not CaseStore: it delegates to one and records the unit-of-work calls."""

    def __init__(self) -> None:
        self.inner = CaseStore()
        self.calls: list[tuple[str, str]] = []

    @property
    def version(self) -> int:
        return self.inner.version

    def get(self, case_id):
        return self.inner.get(case_id)

    def ensure(self, case_id, employee_ref, client_id):
        return self.inner.ensure(case_id, employee_ref, client_id)

    def touch(self):
        self.inner.touch()

    def snapshot(self, case_id):
        return self.inner.snapshot(case_id)

    def restore(self, case_id, snapshot):
        self.calls.append(("restore", case_id))
        self.inner.restore(case_id, snapshot)

    def version_for(self, case_id):
        return self.inner.version_for(case_id)

    def begin(self, case_id):
        self.calls.append(("begin", case_id))

    def commit(self, case_id):
        self.calls.append(("commit", case_id))

    def rollback(self, case_id):
        self.calls.append(("rollback", case_id))

    @property
    def case_ids(self):
        return self.inner.case_ids

    def request_ids(self, case_id):
        return self.inner.request_ids(case_id)

    def all_request_ids(self):
        return self.inner.all_request_ids()

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def build(*, store=None, sink=None, desk=None, recorder=None):
    """A service wired like fixtures.new_environment, with the injection points exposed. Returns (service, direct, orchestrated, desk)."""
    _, cases, desk = new_environment(it_desk=desk)
    service = Uc04Service(cases=cases, it_desk=desk, audit=AuditLog(fixed_clock, sink=sink), store=store)
    return service, DirectEntry(service, run_recorder=recorder), OrchestratedEntry(service, run_recorder=recorder), desk


def direct_eval(direct, payload, **kw):
    return direct.evaluate(payload, actor_id=CALLER_ID, roles=CALLER_ROLES, **kw)


def orch_request(payload, run_id="run-1", key="orch-1", roles=None):
    return OrchestratorRequest(run_id=run_id, idempotency_key=key, workflow=WorkflowRef(id="onboarding", instance=payload["case_id"], step="s"),
                               actor=Actor(on_behalf_of=CALLER_ID, roles=roles or ["onboarding_orchestrator"]), input=payload)


# ------------------------------------------------------------------------------------------------------------------------ repository
def test_case_store_satisfies_the_repository_interface_and_so_does_a_foreign_implementation():
    assert isinstance(CaseStore(), CaseRepository) and isinstance(RecordingRepository(), CaseRepository)


def test_in_memory_version_for_is_the_store_wide_version():
    store = CaseStore()
    assert store.version_for("SYN-CASE-001") == store.version == 0
    store.ensure("SYN-CASE-001", "SYN-EMP-001", "client_alpha")
    assert store.version_for("SYN-CASE-001") == store.version_for("SYN-CASE-999") == store.version == 1


def test_in_memory_unit_of_work_hooks_change_nothing():
    store = CaseStore()
    store.ensure("SYN-CASE-001", "SYN-EMP-001", "client_alpha")
    before = (store.version, store.case_ids)
    for hook in (store.begin, store.commit, store.rollback):
        assert hook("SYN-CASE-001") is None
    assert (store.version, store.case_ids) == before and store.get("SYN-CASE-001") is not None


def test_a_service_over_a_foreign_repository_decides_exactly_like_the_default_one():
    repo = RecordingRepository()
    default_service, default_direct, _, _ = build()
    wrapped_service, wrapped_direct, _, _ = build(store=repo)
    assert wrapped_service.store is repo
    steps = [make(), make(approval_records=approvals(ALL)), make(approval_records=approvals(ALL))]
    for payload in steps:
        a = direct_eval(default_direct, payload, idempotency_key="k-" + str(steps.index(payload)))
        b = direct_eval(wrapped_direct, payload, idempotency_key="k-" + str(steps.index(payload)))
        assert a.business_decision() == b.business_decision()
    a = default_direct.submit_eligible(make(approval_records=approvals(ALL)), actor_id=CALLER_ID, roles=CALLER_ROLES)
    b = wrapped_direct.submit_eligible(make(approval_records=approvals(ALL)), actor_id=CALLER_ID, roles=CALLER_ROLES)
    assert (a.submitted, a.failed, a.skipped) == (b.submitted, b.failed, b.skipped)
    assert a.result.business_decision() == b.result.business_decision()


def test_an_empty_repository_is_never_mistaken_for_no_repository():
    class EmptyLooking(RecordingRepository):
        def __len__(self) -> int:
            return 0

    repo = EmptyLooking()
    service, _, _, _ = build(store=repo)
    assert not repo and service.store is repo  # falsy, still the one in use


def test_a_successful_evaluation_is_one_unit_of_work_and_a_replay_opens_none():
    repo = RecordingRepository()
    _, direct, _, _ = build(store=repo)
    direct_eval(direct, make(), idempotency_key="same")
    assert repo.names() == ["begin", "commit"]
    direct_eval(direct, make(), idempotency_key="same")  # served from the stored result
    assert repo.names() == ["begin", "commit"]


def test_a_failed_evaluation_rolls_back_and_never_commits(monkeypatch):
    repo = RecordingRepository()
    service, direct, _, _ = build(store=repo)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(service, "_assess", boom)
    with pytest.raises(RuntimeError):
        direct_eval(direct, make(), idempotency_key="boom")
    assert repo.names() == ["begin", "restore", "rollback"]
    assert any(e["event_type"] == "state.rolled_back" for e in service.audit.events)
    assert service.store.get("SYN-CASE-001") is None or not service.store.get("SYN-CASE-001").results


def test_acknowledging_an_adapter_error_commits_and_an_unknown_request_rolls_back():
    repo = RecordingRepository()
    desk = MockITServiceDesk(fail_with=AdapterTimeout("x"))
    service, direct, _, _ = build(store=repo, desk=desk)
    payload = make(approval_records=approvals(["201"]))
    report = direct.submit_eligible(payload, actor_id=CALLER_ID, roles=CALLER_ROLES)
    assert report.failed
    repo.calls.clear()
    service.acknowledge_adapter_error("SYN-CASE-001", rid("201"), CALLER_ROLES)
    assert repo.names() == ["begin", "commit"]
    repo.calls.clear()
    service.acknowledge_adapter_error("SYN-CASE-001", rid("201"), CALLER_ROLES)  # nothing left to acknowledge
    assert repo.names() == ["begin", "rollback"]
    with pytest.raises(PolicyViolation):
        service.acknowledge_adapter_error("SYN-CASE-001", rid("201"), ["not_allowed"])


# ------------------------------------------------------------------------------------------------------------------------ audit sink
def test_without_a_sink_the_audit_log_is_unchanged():
    log = AuditLog(fixed_clock)
    assert log.sink is None and log.current_context == {}
    entry = log.record("x.y", case_id="SYN-CASE-001", reason="r")
    assert log.events == [entry] and set(entry) == set(FIELDS)


def test_the_sink_receives_every_entry_unchanged_with_the_call_context():
    sink = InMemoryAuditSink()
    service, direct, _, _ = build(sink=sink)
    direct_eval(direct, make(), idempotency_key="k1", run_id="run-77")
    entries = [e for e, _ in sink.records]
    assert entries == service.audit.events and entries
    assert all(set(e) == set(FIELDS) for e in entries)  # the context is never added to an entry
    contexts = [c for _, c in sink.records]
    assert all(c["actor_id"] == CALLER_ID and c["run_id"] == "run-77" and c["invocation_mode"] == "DIRECT" for c in contexts)
    resolved = next(i for i, e in enumerate(entries) if e["event_type"] == "case.resolved")
    assert all(c.get("client_id") == "client_alpha" for c in contexts[resolved:])  # the verified parent's client


def test_the_context_never_leaks_into_another_call_or_outside_one():
    sink = InMemoryAuditSink()
    service, direct, _, _ = build(sink=sink)
    direct_eval(direct, make(), idempotency_key="k1")
    assert service.audit.current_context == {}
    sink.records.clear()
    service.audit.record("outside.a.call", case_id=None)
    assert sink.records[0][1] == {}
    sink.records.clear()
    service.acknowledge_adapter_error("SYN-CASE-001", rid("201"), CALLER_ROLES)  # its own scope: client only, no actor from before
    assert service.audit.current_context == {}


def test_orchestrated_calls_bind_the_orchestrator_identity_and_run():
    sink = InMemoryAuditSink()
    _, _, orch, _ = build(sink=sink)
    orch.evaluate(orch_request(make(), run_id="orch-run-9"))
    contexts = [c for _, c in sink.records]
    assert all(c["run_id"] == "orch-run-9" and c["invocation_mode"] == "PARENT_ORCHESTRATED" and c["actor_id"] == CALLER_ID for c in contexts)


def test_failure_events_reach_the_sink_and_a_failing_sink_is_not_swallowed(monkeypatch):
    sink = InMemoryAuditSink()
    service, direct, _, _ = build(sink=sink)
    monkeypatch.setattr(service, "_assess", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        direct_eval(direct, make(), idempotency_key="k")
    assert "state.rolled_back" in [e["event_type"] for e, _ in sink.records]

    class Broken:
        def write(self, entry, context):
            raise OSError("sink down")

    _, direct2, _, _ = build(sink=Broken())
    with pytest.raises(OSError):
        direct_eval(direct2, make(), idempotency_key="k2")


def test_sensitive_context_is_refused_only_when_a_sink_receives_it():
    plain = AuditLog(fixed_clock)
    with plain.context(actor_id="someone@example.com"):
        plain.record("a", case_id=None)  # no sink: behaviour unchanged
    sinked = AuditLog(fixed_clock, sink=InMemoryAuditSink())
    with sinked.context(actor_id="someone@example.com"), pytest.raises(PrivacyViolation):
        sinked.record("a", case_id=None)


# ------------------------------------------------------------------------------------------------------------------------ run recorder
def test_no_recorder_by_default():
    assert DirectEntry(build()[0]).run_recorder is None and OrchestratedEntry(build()[0]).run_recorder is None


def test_an_orchestrated_call_is_recorded_and_agrees_with_the_response():
    rec = InMemoryAgentRunRecorder()
    _, _, orch, _ = build(recorder=rec)
    response = orch.evaluate(orch_request(make(offer_accepted=True), run_id="r-1"))
    (run,) = rec.records
    assert (run.run_id, run.operation, run.invocation_mode, run.case_id, run.client_id, run.actor_id) == (
        "r-1", "evaluate", "PARENT_ORCHESTRATED", "SYN-CASE-001", "client_alpha", CALLER_ID)
    assert run.status == response.status and run.proposal_count == len(response.proposals) and run.exception_count == len(response.exceptions)
    assert (run.agent_id, run.agent_version, run.advisor_mode, run.tool_calls, run.action_count) == (AGENT_ID, AGENT_VERSION, "offline", [], 0)
    assert run.started_at <= run.finished_at


def test_a_blocked_case_is_recorded_as_an_exception():
    rec = InMemoryAgentRunRecorder()
    _, _, orch, _ = build(recorder=rec)
    response = orch.evaluate(orch_request(make(offer_accepted=False), run_id="r-2"))
    assert response.status == "exception" and rec.records[0].status == "exception" and rec.records[0].exception_count >= 1


def test_direct_evaluate_and_submit_are_recorded_with_the_tool_call_and_actions():
    rec = InMemoryAgentRunRecorder()
    _, direct, _, _ = build(recorder=rec)
    direct_eval(direct, make(approval_records=approvals(ALL)), idempotency_key="e")
    report = direct.submit_eligible(make(approval_records=approvals(ALL)), actor_id=CALLER_ID, roles=CALLER_ROLES, idempotency_key="s")
    evaluate_run, submit_run = rec.records
    assert evaluate_run.operation == "evaluate" and evaluate_run.tool_calls == [] and evaluate_run.action_count == 0
    assert submit_run.operation == "submit_eligible" and submit_run.invocation_mode == "DIRECT"
    assert submit_run.action_count == len(report.submitted) == 4 and submit_run.tool_calls == [SUBMIT_TOOL]


def test_a_failed_submission_is_recorded_with_the_tool_call_but_no_action():
    rec = InMemoryAgentRunRecorder()
    _, direct, _, desk = build(recorder=rec, desk=MockITServiceDesk(fail_with=AdapterTimeout("x")))
    report = direct.submit_eligible(make(approval_records=approvals(["201"])), actor_id=CALLER_ID, roles=CALLER_ROLES)
    assert report.failed and rec.records[-1].action_count == 0 and rec.records[-1].tool_calls == [SUBMIT_TOOL]
    assert len(desk.calls) == 1


def test_calls_that_raise_are_not_recorded():
    rec = InMemoryAgentRunRecorder()
    _, direct, _, _ = build(recorder=rec)
    with pytest.raises(PolicyViolation):
        direct.evaluate(make(), actor_id=CALLER_ID, roles=["not_allowed"])
    assert rec.records == []


def test_the_recorder_does_not_change_what_the_caller_receives():
    rec = InMemoryAgentRunRecorder()
    _, _, with_rec, _ = build(recorder=rec)
    _, _, without, _ = build()
    a = with_rec.evaluate(orch_request(make()))
    b = without.evaluate(orch_request(make()))
    assert a.status == b.status and a.proposals == b.proposals and a.exceptions == b.exceptions
    assert a.result.business_decision() == b.result.business_decision()


def test_run_record_identity_matches_the_response_provenance():
    p = ProvenanceOut(started_at="x", finished_at="y", duration_ms=0.0)
    assert (p.agent, p.agent_version) == (AGENT_ID, AGENT_VERSION)


# ------------------------------------------------------------------------------------------------------------------------ agent facade
def test_the_agent_is_unchanged_by_default():
    agent = Uc04Agent()
    assert type(agent.service.store) is CaseStore and agent.service.audit.sink is None
    assert agent.evaluate(make(), idempotency_key="k").overall_status.value == "AWAITING_APPROVAL"


def test_the_agent_accepts_an_injected_store_sink_and_recorder():
    repo, sink, rec = RecordingRepository(), InMemoryAuditSink(), InMemoryAgentRunRecorder()
    agent = Uc04Agent(store=repo, audit_sink=sink, run_recorder=rec)
    assert agent.service.store is repo
    result = agent.evaluate(make(approval_records=approvals(ALL)), idempotency_key="k")
    assert result.overall_status.value == "IN_PROGRESS"
    assert repo.names() == ["begin", "commit"] and sink.records and len(rec.records) == 1
    default = Uc04Agent().evaluate(make(approval_records=approvals(ALL)), idempotency_key="k")
    assert result.business_decision() == default.business_decision()


# ------------------------------------------------------------------------------------------------------------------------ boundaries
def test_the_new_interfaces_import_no_network_or_database_library():
    forbidden = re.compile(r"^\s*(?:import|from)\s+(socket|requests|httpx|urllib|http|psycopg\w*|asyncpg|supabase|sqlalchemy|sqlite3|postgrest|boto3)\b", re.M)
    for name in ("store.py", "ports.py", "audit.py", "runs.py", "direct.py", "orchestrated.py", "service.py", "agent.py"):
        assert not forbidden.search((SRC / name).read_text(encoding="utf-8")), name


def test_an_unaccepted_case_still_gets_the_same_blockers_through_a_foreign_repository():
    _, direct, _, _ = build(store=RecordingRepository())
    result = direct_eval(direct, make(offer_accepted=False), idempotency_key="k")
    assert "START_GATE_OFFER_NOT_ACCEPTED" in codes(result.blockers)
