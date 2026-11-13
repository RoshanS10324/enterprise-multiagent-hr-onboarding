"""FastAPI layer shared by the onboarding agent's LangGraph-run and approval routes.

    POST /v1/runs                              run the agent; proposals land in the HITL inbox
    GET  /v1/approvals                         list inbox items (default: pending), tenant-scoped
    GET  /v1/approvals/{id}                    tenant-scoped
    POST /v1/approvals/{id}/decision           approve / edit / reject, tenant-scoped
    POST /v1/approvals/{id}/retry-apply        retry a decision whose follow-up action failed (S5)
    GET  /healthz

Forked from hr_recruitment_common.api with the fixes documented in
docs/ONBOARDING_COMMON_ARCHITECTURE.md Part B:

S1/S4 -- identity is read through an `IdentityProvider` (platform.identity), never trusted from
raw headers in this module. Every route that touches an approval or runs the agent is scoped to
the verified actor's `client_id`: `/v1/approvals*` pass it into every `HITLInbox` call, and an
optional `tenant_check` hook lets the caller (the onboarding agent's own api.py) verify the target
`instance` (e.g. employee id) belongs to the actor's tenant before `/v1/runs` executes the graph at
all -- this keeps `create_app()` itself domain-agnostic (it does not know "instance" means
"employee id") while closing the gap.

S5 -- `/v1/approvals/{id}/decision` distinguishes a successfully-applied decision from one whose
follow-up action failed (`HITLApplyError` -> 502, not an opaque 500), and `/v1/approvals/{id}/retry-apply`
exposes the recovery path.

S6 -- `/healthz` still reports `policy.enabled`; the kill switch itself is enforced in
`BaseAgent.run()` (unchanged) AND in `ToolGateway.call()` (platform/tools.py), not only here.

`default_context()` is dropped: it existed only to build a default AgentContext from environment
variables including an ATS adapter that has no onboarding equivalent, and nothing in this codebase
called it. `context` is now a required argument -- every caller must construct its own AgentContext.
"""

# No "from __future__ import annotations" here: FastAPI must resolve the locally defined RunBody
# model at runtime (it is defined inside create_app(), not at module scope); postponed evaluation
# of annotations would turn `body: RunBody` into an unresolvable forward reference and silently
# break request-body parsing (observed as the body being treated as a missing query parameter).

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from hr_onboarding_common.agent import AgentContext, BaseAgent
from hr_onboarding_common.contracts import Actor, AgentRequest, AgentResponse, Scope, WorkflowRef
from hr_onboarding_common.observability.logging import configure_logging, get_logger
from hr_onboarding_common.observability.tracing import configure_tracing
from hr_onboarding_common.platform.hitl import ApprovalItem, Decision, HITLApplyError, HITLError, ReasonCode
from hr_onboarding_common.platform.identity import IdentityError, IdentityProvider, VerifiedActor

log = get_logger("api")
DecisionHandler = Callable[[AgentContext, BaseAgent, ApprovalItem], None]
TenantCheck = Callable[[AgentContext, str, str], None]  # (ctx, instance, client_id) -> raises PermissionError


class DecisionBody(BaseModel):
    decision: Decision
    reason_code: ReasonCode | None = None
    note: str | None = None
    edited_payload: dict[str, Any] | None = None


class DecisionConflictError(RuntimeError):
    """Raised by a decision validator: what the approval decides is no longer pending (409)."""


# (ctx, agent, item, verified actor, body) -> None; raises to refuse BEFORE the decision is recorded:
# PermissionError -> 403, KeyError -> 404, DecisionConflictError -> 409, ValueError -> 422.
DecisionValidator = Callable[[AgentContext, BaseAgent, ApprovalItem, VerifiedActor, DecisionBody], None]


class ApprovalRef(BaseModel):
    id: str
    gate: str
    approver_role: str
    subject_id: str | None


class RunResult(BaseModel):
    response: AgentResponse
    approvals: list[ApprovalRef] = Field(default_factory=list)


def create_app(*, service: str, agent_home: Path, agent_cls: type[BaseAgent], context: AgentContext,
               identity_provider: IdentityProvider, on_decision: DecisionHandler | None = None,
               tenant_check: TenantCheck | None = None,
               decision_validator: DecisionValidator | None = None) -> FastAPI:
    """`decision_validator` (optional): an agent-specific check run after the approval is fetched
    (tenant-scoped) and BEFORE HITLInbox.decide records anything -- e.g. "only the specific team
    lead on record may decide this". HITLInbox itself checks only role and tenant, and a decision it
    records is final; a validator lets the agent refuse without consuming the approval."""
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(service)
        configure_tracing(service)
        agent = agent_cls(context)
        if on_decision is not None:
            context.hitl.subscribe(lambda item: on_decision(context, agent, item))
        state.update(ctx=context, agent=agent)
        log.info("service.started", extra={"agent": agent.agent_id, "version": agent.version})
        yield

    app = FastAPI(title=service, version=agent_cls.version, lifespan=lifespan)

    class RunBody(BaseModel):
        instance: str = Field(description="Workflow instance this run belongs to, e.g. the employee id")
        input: agent_cls.input_model  # type: ignore[name-defined]
        subject_scope: list[str] = Field(default_factory=list)

    def verified_actor(request: Request) -> VerifiedActor:
        """The only place raw request headers are read. Everything downstream works with a
        VerifiedActor or an Actor built from one -- never with a header value directly."""
        try:
            return identity_provider.authenticate(request.headers)
        except IdentityError as e:
            raise HTTPException(401, str(e)) from None

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        agent: BaseAgent = state["agent"]
        policy = agent.policy
        return {"status": "ok", "agent": agent.agent_id, "version": agent.version,
                "policy": f"{policy.policy}@v{policy.version}", "enabled": policy.enabled,
                "autonomy": policy.autonomy}

    @app.post("/v1/runs", response_model=RunResult)
    def run(body: RunBody, va: VerifiedActor = Depends(verified_actor)) -> RunResult:
        agent: BaseAgent = state["agent"]
        ctx: AgentContext = state["ctx"]
        if tenant_check is not None:
            try:
                tenant_check(ctx, body.instance, va.client_id)
            except PermissionError as e:
                raise HTTPException(403, str(e)) from None
        who = Actor(on_behalf_of=va.user_id, roles=va.roles, client_id=va.client_id)
        request = AgentRequest[agent.input_model](
            workflow=WorkflowRef(id=service, instance=body.instance, step="run"), actor=who,
            scope=Scope(subject_ids=body.subject_scope or [body.instance]), input=body.input)
        response = agent.run(request)
        items = agent.submit_for_review(response, workflow=service, instance=body.instance,
                                        client_id=va.client_id)
        return RunResult(response=response, approvals=[
            ApprovalRef(id=i.id, gate=i.gate, approver_role=i.approver_role, subject_id=i.subject_id) for i in items])

    @app.get("/v1/approvals", response_model=list[ApprovalItem])
    def approvals(status: Decision | None = Decision.PENDING, gate: str | None = None,
                  instance: str | None = None, va: VerifiedActor = Depends(verified_actor)) -> list[ApprovalItem]:
        return state["ctx"].hitl.list(status=status, gate=gate, workflow_instance=instance, client_id=va.client_id)

    @app.get("/v1/approvals/{approval_id}", response_model=ApprovalItem)
    def approval(approval_id: str, va: VerifiedActor = Depends(verified_actor)) -> ApprovalItem:
        try:
            return state["ctx"].hitl.get(approval_id, client_id=va.client_id)
        except KeyError:
            raise HTTPException(404, "approval not found") from None

    @app.post("/v1/approvals/{approval_id}/decision", response_model=ApprovalItem)
    def decide(approval_id: str, body: DecisionBody, va: VerifiedActor = Depends(verified_actor)) -> ApprovalItem:
        if decision_validator is not None:
            try:
                item = state["ctx"].hitl.get(approval_id, client_id=va.client_id)
                decision_validator(state["ctx"], state["agent"], item, va, body)
            except KeyError:
                raise HTTPException(404, "approval not found") from None
            except PermissionError as e:
                raise HTTPException(403, str(e)) from None
            except DecisionConflictError as e:
                raise HTTPException(409, str(e)) from None
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
        try:
            return state["ctx"].hitl.decide(approval_id, body.decision, approver=va.user_id,
                                            approver_roles=va.roles, client_id=va.client_id,
                                            reason_code=body.reason_code, note=body.note,
                                            edited_payload=body.edited_payload)
        except KeyError:
            raise HTTPException(404, "approval not found") from None
        except HITLApplyError as e:
            # The decision itself succeeded; its follow-up action did not (finding S5). Surfaced
            # distinctly from a generic 500 so the caller can see the approval exists, is decided,
            # and is retryable -- not silently lost.
            raise HTTPException(502, str(e)) from None
        except HITLError as e:
            raise HTTPException(409 if "already decided" in str(e) else 403, str(e)) from None

    @app.post("/v1/approvals/{approval_id}/retry-apply", response_model=ApprovalItem)
    def retry_apply(approval_id: str, va: VerifiedActor = Depends(verified_actor)) -> ApprovalItem:
        try:
            return state["ctx"].hitl.retry_apply(approval_id, client_id=va.client_id)
        except KeyError:
            raise HTTPException(404, "approval not found") from None
        except HITLApplyError as e:
            raise HTTPException(502, str(e)) from None
        except HITLError as e:
            raise HTTPException(409, str(e)) from None

    return app
