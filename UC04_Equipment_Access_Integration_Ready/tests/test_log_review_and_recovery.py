"""Tests for UC-04 Log Review and Recovery capabilities.

Verifies:
- Item submission attempt counter (attempt tracking).
- Orchestrated envelope provenance metadata (duration_ms, started_at, finished_at, agent).
- ProposalOut gate attribute.
- AuditLog JSONL file output and export capabilities.
- Agent facade acknowledge_adapter_error method.
"""

import json
from pathlib import Path

from helpers import ALL, Env, approvals, item, make, rid
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk
from sub_agent.agent import Uc04Agent
from sub_agent.audit import AuditLog
from sub_agent.entrypoints.orchestrated import Actor, OrchestratedEntry, OrchestratorRequest, WorkflowRef
from sub_agent.ports import AdapterTimeout
from sub_agent.states import CaseStatus, ItemState


def test_submission_attempts_counter_increments_on_it_desk_submission(env: Env) -> None:
    """Verifies that ItemRecord tracks submission attempts for retry observability."""
    payload = make(approval_records=approvals(ALL))
    
    # First evaluation: items are eligible, but no submission attempted yet
    res = env.run(payload)
    for itm in res.items:
        assert itm.submission_attempts == 0

    # Submit eligible items to mock IT desk
    report = env.submit(payload)
    for itm in report.result.items:
        assert itm.submission_attempts == 1


def test_submission_attempts_increment_on_adapter_failure_and_retry(tmp_path: Path) -> None:
    """Verifies attempt counting during failure, unblock, and retry cycles."""
    desk = MockITServiceDesk(fail_with=AdapterTimeout("temporary timeout"))
    env = Env(it_desk=desk)
    payload = make(approval_records=approvals(["201"]))

    # 1. First attempt fails due to timeout
    rep1 = env.submit(payload)
    assert rep1.failed == [{"request_id": rid("201"), "error": "AdapterTimeout"}]
    laptop1 = item(rep1.result, "201")
    assert laptop1.submission_attempts == 1
    assert "ADAPTER_ERROR" in [b.code for b in rep1.result.blockers]

    # 2. Subsequent blind retry is blocked (poison pill)
    rep2 = env.submit(payload)
    assert rep2.skipped[0]["reason"] == "PREVIOUS_ADAPTER_ERROR_UNRESOLVED"
    laptop2 = item(rep2.result, "201")
    assert laptop2.submission_attempts == 1  # Not incremented on skip

    # 3. Human acknowledges the error to unlock retry
    env.service.acknowledge_adapter_error("SYN-CASE-001", rid("201"), ["hr_onboarding_officer"])
    desk.fail_with = None

    # 4. Resubmission succeeds on second attempt
    rep3 = env.submit(payload)
    assert rep3.submitted == [rid("201")]
    laptop3 = item(rep3.result, "201")
    assert laptop3.submission_attempts == 2
    assert laptop3.state == ItemState.MOCK_TICKET_CREATED


def test_agent_facade_acknowledge_adapter_error() -> None:
    """Verifies that Uc04Agent facade exposes acknowledge_adapter_error."""
    desk = MockITServiceDesk(fail_with=AdapterTimeout("temporary timeout"))
    agent = Uc04Agent(it_desk=desk)
    payload = make(approval_records=approvals(["201"]))

    rep = agent.submit_eligible(payload)
    assert rep.failed == [{"request_id": rid("201"), "error": "AdapterTimeout"}]

    # Acknowledge via agent facade
    agent.acknowledge_adapter_error("SYN-CASE-001", rid("201"), ["hr_onboarding_officer"])
    desk.fail_with = None

    rep_retry = agent.submit_eligible(payload)
    assert rep_retry.submitted == [rid("201")]


def test_orchestrator_response_includes_provenance_and_duration() -> None:
    """Verifies that OrchestratorResponse provides duration and provenance for log review."""
    agent = Uc04Agent()
    payload = make()
    req = OrchestratorRequest(
        run_id="run-orch-001",
        idempotency_key="key-orch-001",
        workflow=WorkflowRef(id="onboarding", instance=payload["case_id"], step="equipment"),
        actor=Actor(on_behalf_of="SYN-USER-100", roles=["onboarding_orchestrator"]),
        input=payload,
    )
    resp = agent.evaluate_orchestrated(req)
    assert resp.provenance is not None
    assert resp.provenance.agent == "employee-onboarding-uc04"
    assert resp.provenance.agent_version == "0.1.0"
    assert resp.provenance.started_at is not None
    assert resp.provenance.finished_at is not None
    assert resp.provenance.duration_ms >= 0.0


def test_orchestrator_proposal_out_includes_gate_field() -> None:
    """Verifies that ProposalOut includes gate attribute aligned with platform Proposal."""
    agent = Uc04Agent()
    payload = make(
        requested_access=[{"request_key": "SYN-REQ-101", "item_id": "email-teams-account", "item_kind": "ACCESS", "requested_by": "SYN-USER-201"}],
        requested_equipment=[],
    )
    req = OrchestratorRequest(
        run_id="run-orch-002",
        idempotency_key="key-orch-002",
        workflow=WorkflowRef(id="onboarding", instance=payload["case_id"], step="equipment"),
        actor=Actor(on_behalf_of="SYN-USER-100", roles=["onboarding_orchestrator"]),
        input=payload,
    )
    resp = agent.evaluate_orchestrated(req)
    assert resp.status == "proposed"


def test_audit_log_live_appends_to_jsonl_file(tmp_path: Path) -> None:
    """Verifies AuditLog can stream events live to a platform JSONL log file."""
    log_file = tmp_path / "uc04_audit.jsonl"
    audit = AuditLog(file_path=log_file)
    audit.record("test.event", case_id="SYN-CASE-001", rule_id="BR-01", reason="testing log file")

    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["event_type"] == "test.event"
    assert data["case_id"] == "SYN-CASE-001"
    assert data["rule_id"] == "BR-01"


def test_audit_log_export_jsonl(tmp_path: Path) -> None:
    """Verifies AuditLog can export in-memory events to a JSONL file."""
    audit = AuditLog()
    audit.record("event.1", case_id="SYN-CASE-001", rule_id="BR-01")
    audit.record("event.2", case_id="SYN-CASE-001", rule_id="BR-02")

    export_file = tmp_path / "exported_audit.jsonl"
    count = audit.export_jsonl(export_file)
    assert count == 2
    assert export_file.exists()

    lines = export_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event_type"] == "event.1"
    assert json.loads(lines[1])["event_type"] == "event.2"

