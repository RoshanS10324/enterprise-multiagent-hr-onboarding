"""Agent-run records: one record per direct or orchestrated call that returns a result.

Pure data and an in-memory recorder. A recorder is optional and off by default; it changes nothing about a call's result. The status and the
counts follow the rules `OrchestratedEntry` already applies to its response, so a record always agrees with what the caller was told.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from sub_agent.contracts import SubmissionReport, Uc04Request, Uc04Result
from sub_agent.states import CaseStatus, Severity

AGENT_ID = "employee-onboarding-uc04"
AGENT_VERSION = "0.1.0"
SUBMIT_TOOL = "it_service_desk.submit_request"


class AgentRunRecord(BaseModel):
    run_id: str
    operation: str  # "evaluate" or "submit_eligible"
    invocation_mode: str  # DIRECT or PARENT_ORCHESTRATED
    case_id: str
    client_id: str
    agent_id: str = AGENT_ID
    agent_version: str = AGENT_VERSION
    actor_id: str
    status: str  # "proposed" or "exception": UC-04 never reports "acted" in local mode
    advisor_mode: str = "offline"
    proposal_count: int = 0
    action_count: int = 0  # mock submissions made by this call
    exception_count: int = 0  # blocking blockers, as in OrchestratorResponse.exceptions
    tool_calls: list[str] = Field(default_factory=list)  # tool names only
    error_summary: str | None = None  # reserved: calls that raise are not recorded yet
    started_at: datetime
    finished_at: datetime


def run_status(result: Uc04Result) -> str:
    return "exception" if result.overall_status == CaseStatus.BLOCKED else "proposed"


def build_run_record(*, request: Uc04Request, result: Uc04Result, operation: str, started_at: datetime, finished_at: datetime,
                     report: SubmissionReport | None = None) -> AgentRunRecord:
    submitted = report is not None and bool(report.submitted or report.failed)
    return AgentRunRecord(
        run_id=request.caller.run_id, operation=operation, invocation_mode=request.caller.invocation_mode.value,
        case_id=result.case_id, client_id=request.input.client_id, actor_id=request.caller.actor_id, status=run_status(result),
        proposal_count=len(result.proposals) + len(result.cancellation_drafts),
        action_count=len(report.submitted) if report is not None else 0,
        exception_count=sum(1 for b in result.blockers if b.severity == Severity.BLOCKING),
        tool_calls=[SUBMIT_TOOL] if submitted else [], started_at=started_at, finished_at=finished_at)


class InMemoryAgentRunRecorder:
    """Reference `AgentRunRecorderPort`; used by tests and as the model for a durable recorder."""

    def __init__(self) -> None:
        self.records: list[AgentRunRecord] = []

    def record(self, run: AgentRunRecord) -> None:
        self.records.append(run)

    def __iter__(self) -> Any:
        return iter(self.records)
