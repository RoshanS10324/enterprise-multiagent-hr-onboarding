"""Scenario B: the parent Employee Onboarding workflow invokes UC-04 as one stage.

The envelope below MIRRORS the orchestrator<->agent contract verified in the reference repository
(`hr_recruitment_common.contracts`: AgentRequest / WorkflowRef / Actor / AgentResponse / Proposal / AgentException).
It is a local copy so the core has no dependency on that package. At integration time it is replaced by the real
contract, and a contract test should confirm the field names still match. The parent's case ID is carried in
`workflow.instance` and must equal `input.case_id`: UC-04 never opens or renumbers a case.
"""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sub_agent.contracts import CallerContext, CaseStatusView, Uc04Input, Uc04Request, Uc04Result
from sub_agent.ports import AgentRunRecorderPort
from sub_agent.runs import build_run_record
from sub_agent.service import UnknownCaseError, Uc04Service, Uc04ValidationError
from sub_agent.states import CaseStatus, InvocationMode, Severity

SUPPORTED_CONTRACT_VERSION = "1.0"  # the version of the mirrored envelope; any other value is refused, never guessed at


class WorkflowRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    instance: str  # the existing onboarding case id
    step: str


class Actor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on_behalf_of: str
    roles: list[str] = Field(default_factory=list)


class OrchestratorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: str = "1.0"
    run_id: str
    idempotency_key: str
    workflow: WorkflowRef
    actor: Actor
    input: dict[str, Any] | Uc04Input


class CaseOperationRequest(BaseModel):
    """Envelope for the operations that need no case input: acknowledging an adapter error and reading the stored status.
    Same version rule, same actor, same case identification as the evaluation envelope."""

    model_config = ConfigDict(extra="forbid")
    contract_version: str = "1.0"
    run_id: str
    workflow: WorkflowRef
    actor: Actor
    client_id: str  # the parent case's client; every lookup is scoped to it
    request_id: str | None = None  # required by acknowledge_adapter_error


class AcknowledgementOut(BaseModel):
    run_id: str
    case_id: str
    request_id: str
    acknowledged: bool  # False when there was nothing to acknowledge


class ProposalOut(BaseModel):
    type: str
    subject_id: str | None
    payload: dict[str, Any]
    rationale: str
    requires_approval_by: str
    gate: str = ""


class ExceptionOut(BaseModel):
    code: str
    detail: str
    subject_id: str | None = None


class ProvenanceOut(BaseModel):
    agent: str = "employee-onboarding-uc04"
    agent_version: str = "0.1.0"
    started_at: str
    finished_at: str
    duration_ms: float
    skills: list[str] = Field(default_factory=list)


class OrchestratorResponse(BaseModel):
    run_id: str
    status: str  # proposed | exception | acted (UC-04 never acts in local mode)
    proposals: list[ProposalOut] = Field(default_factory=list)
    exceptions: list[ExceptionOut] = Field(default_factory=list)
    result: Uc04Result
    provenance: ProvenanceOut | None = None


class OrchestratedEntry:
    def __init__(self, service: Uc04Service, run_recorder: AgentRunRecorderPort | None = None):
        self.service = service
        self.run_recorder = run_recorder  # optional; off by default and never changes the response

    def _to_request(self, req: OrchestratorRequest) -> Uc04Request:
        if req.contract_version != SUPPORTED_CONTRACT_VERSION:
            raise Uc04ValidationError(["contract_version"])
        inp = self.service.parse_input(req.input)
        if req.workflow.instance != inp.case_id:
            raise Uc04ValidationError(["workflow.instance"])  # the parent case must be the one UC-04 is told about
        try:
            caller = CallerContext(invocation_mode=InvocationMode.PARENT_ORCHESTRATED, actor_id=req.actor.on_behalf_of,
                                   roles=req.actor.roles, run_id=req.run_id, idempotency_key=req.idempotency_key,
                                   workflow_instance=req.workflow.instance)
        except ValidationError as e:
            raise Uc04ValidationError(sorted({".".join(str(p) for p in err["loc"]) for err in e.errors()})) from None
        return Uc04Request(caller=caller, input=inp)

    def evaluate(self, req: OrchestratorRequest) -> OrchestratorResponse:
        audit = self.service.audit
        with audit.context(actor_id=req.actor.on_behalf_of, run_id=req.run_id, invocation_mode=InvocationMode.PARENT_ORCHESTRATED.value):
            t0 = datetime.now(timezone.utc)  # taken before validation, as before, so duration_ms is unchanged
            request = self._to_request(req)
            result = self.service.evaluate(request)
            t1 = datetime.now(timezone.utc)
            if self.run_recorder is not None:
                self.run_recorder.record(build_run_record(request=request, result=result, operation="evaluate", started_at=t0, finished_at=t1))
            return self._response(req, result, started_at=t0, finished_at=t1)

    def submit_eligible(self, req: OrchestratorRequest) -> OrchestratorResponse:
        audit = self.service.audit
        with audit.context(actor_id=req.actor.on_behalf_of, run_id=req.run_id, invocation_mode=InvocationMode.PARENT_ORCHESTRATED.value):
            t0 = datetime.now(timezone.utc)  # taken before validation, as before, so duration_ms is unchanged
            request = self._to_request(req)
            report = self.service.submit_eligible(request)
            t1 = datetime.now(timezone.utc)
            if self.run_recorder is not None:
                self.run_recorder.record(build_run_record(request=request, result=report.result, operation="submit_eligible",
                                                          started_at=t0, finished_at=t1, report=report))
            return self._response(req, report.result, started_at=t0, finished_at=t1)

    @staticmethod
    def _response(req: OrchestratorRequest, result: Uc04Result,
                  started_at: datetime | None = None,
                  finished_at: datetime | None = None) -> OrchestratorResponse:
        proposals = [ProposalOut(type=p.proposal_type, subject_id=p.request_id,
                                 payload={"case_id": result.case_id, **p.payload}, rationale=p.description,
                                 requires_approval_by=p.requires_approval_by,
                                 gate=p.proposal_type.lower())
                     for p in result.proposals + result.cancellation_drafts]
        exceptions = [ExceptionOut(code=b.code, detail=b.reason, subject_id=b.request_id)
                      for b in result.blockers if b.severity == Severity.BLOCKING]
        status = "exception" if result.overall_status == CaseStatus.BLOCKED else "proposed"
        prov = None
        if started_at is not None and finished_at is not None:
            dur = (finished_at - started_at).total_seconds() * 1000.0
            prov = ProvenanceOut(
                started_at=started_at.isoformat(),
                finished_at=finished_at.isoformat(),
                duration_ms=round(dur, 2),
            )
        return OrchestratorResponse(run_id=req.run_id, status=status, proposals=proposals, exceptions=exceptions,
                                    result=result, provenance=prov)


def _check_operation(op: CaseOperationRequest) -> None:
    if op.contract_version != SUPPORTED_CONTRACT_VERSION:
        raise Uc04ValidationError(["contract_version"])


class OperationEntry:
    """The orchestrated operations that are not an evaluation. Same service, same policy, same tenant scoping."""

    def __init__(self, service: Uc04Service):
        self.service = service

    def acknowledge_adapter_error(self, op: CaseOperationRequest) -> AcknowledgementOut:
        _check_operation(op)
        if not op.request_id:
            raise Uc04ValidationError(["request_id"])
        case_id = op.workflow.instance
        parent = self.service.cases.get_case(case_id)
        if parent is None or parent.client_id != op.client_id:
            raise UnknownCaseError(case_id)  # another client's case is indistinguishable from no case
        with self.service.audit.context(actor_id=op.actor.on_behalf_of, run_id=op.run_id, invocation_mode=InvocationMode.PARENT_ORCHESTRATED.value):
            done = self.service.acknowledge_adapter_error(case_id, op.request_id, op.actor.roles)
        return AcknowledgementOut(run_id=op.run_id, case_id=case_id, request_id=op.request_id, acknowledged=done)

    def case_status(self, op: CaseOperationRequest) -> CaseStatusView:
        _check_operation(op)
        with self.service.audit.context(actor_id=op.actor.on_behalf_of, run_id=op.run_id, invocation_mode=InvocationMode.PARENT_ORCHESTRATED.value):
            return self.service.case_status(op.workflow.instance, op.client_id, op.actor.roles)
