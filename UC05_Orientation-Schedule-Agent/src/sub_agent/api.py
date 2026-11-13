"""FastAPI service for the onboarding agent. Run: python -m uvicorn sub_agent.api:app

Builds its own AgentContext (passed to the shared create_app via `context=`) whose `connector` is
the OnboardingDependencies bundle, so every route group below shares the same repository,
calendar, notifier, HITL inbox, audit log and workflow checkpointer.

Three groups of routes share this app:
  - The shared `/v1/runs`, `/v1/approvals*` routes from hr_onboarding_common.api.create_app.
    /v1/runs runs the onboarding workflow (OnboardingAgent); its original request body
    {"instance", "input": {"employee_id"}} still drafts a welcome message. Every approval decision
    is handed to the workflow by the single `on_decision` listener registered here.
  - `/v1/onboarding/workflow/*` -- the workflow's event entry points (offer accepted, RSVP,
    joining day, policy question) and GET /v1/onboarding/{employee_id}/workflow for case status.
    Each runs one workflow execution through OnboardingAgent.run_event.
  - `/v1/onboarding/*` -- individual onboarding operations (HR console / manual overrides),
    calling OnboardingToolRegistry directly. Unchanged contracts. They do not advance the workflow
    by themselves, but any approval they create is still applied by the workflow when decided.
No route contains workflow logic: they authenticate, validate, tenant-check and delegate.

Identity (finding S2): every route authenticates through `_identity_provider`, built by
sub_agent.identity.build_identity_provider: by default a verified bearer JWT from the
organisation's identity provider or gateway (IDENTITY_MODE=jwt) -- user, roles and tenant come from
the token's signed claims, and X-User-Id/X-User-Roles/X-Client-Id headers are ignored. The old
header trust (IDENTITY_MODE=insecure-dev-headers) is refused at startup unless HR_AGENT_ENV is
development or test. The same verified actor authorizes the routes, the workflow run, every tool
call (OnboardingToolRegistry) and approval decisions (HITLInbox checks the decider's role and
tenant); a decision's follow-up retry additionally requires the item's approver role or hr_admin
(_authorize_retry_apply below).

Runtime (sub_agent.runtime.build_context): HR_AGENT_ENV, HR_AGENT_PERSIST, CONNECTOR_MODE (only "offline"
exists -- every connector is a fake from sub_agent.fakes). Unsupported or unsafe combinations fail
startup rather than pretending.

Cross-tenant /v1/runs (finding S4): `_tenant_check` is passed into `create_app(...)` and verifies
the target employee belongs to the caller's tenant before the graph ever executes; the workflow then
refuses to act on any employee other than that `instance`.
"""

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, Field

from connectors.calendar.base import CalendarBookingError, CalendarConflictError
from connectors.email.base import EmailServiceError
from connectors.notifications.base import NotificationDeliveryError
from hr_onboarding_common.agent import AgentContext
from hr_onboarding_common.api import ApprovalRef, DecisionConflictError, create_app
from hr_onboarding_common.config import load_environment
from hr_onboarding_common.contracts import Actor, AgentResponse, Purpose
from hr_onboarding_common.llm import LLMError
from hr_onboarding_common.observability.logging import get_logger
from hr_onboarding_common.platform.hitl import ApprovalItem
from hr_onboarding_common.platform.identity import IdentityError, VerifiedActor
from sub_agent import reconciliation
from sub_agent.agent import AGENT_HOME, OnboardingAgent, OnboardingWorkflowInput, WorkflowEvent
from sub_agent.identity import build_identity_provider
from sub_agent.models import DuplicateRecordError, OperationStatus, ResponseStatus, SessionType, TenantMismatchError
from sub_agent.runtime import build_context
from sub_agent.services.decisions import DecisionNotApplicableError
from sub_agent.services.delivery import DeliveryOutcomeUncertainError
from sub_agent.services.scheduling import SchedulingError, StaleParticipantResponseError
from sub_agent.services.welcome import UnknownTemplateError
from sub_agent.tool_registry import (ConnectorNotConfiguredError, DuplicateSendError, OnboardingToolRegistry,
                                     TenantAuthorizationError)

log = get_logger("api")
# Real environment variables always win. Then the onboarding system's one .env (LLM_MODE, model tiers,
# OPENROUTER_API_KEY) -- the file HR_ONBOARDING_ENV_FILE names, which the orchestrator sets for its UC05
# worker; set it yourself to run this API on its own -- then a local .env, if any.
load_environment()
load_dotenv()

# ----------------------------------------------------------------------------- identity (finding S2)
_identity_provider, IDENTITY_MODE = build_identity_provider()
if IDENTITY_MODE == "insecure-dev-headers":
    log.warning("api.insecure_identity_provider_enabled", extra={"security": {
        "detail": "IDENTITY_MODE=insecure-dev-headers: X-User-Id/X-User-Roles/X-Client-Id are trusted verbatim. "
                  "Allowed only because HR_AGENT_ENV is development or test."}})

# Shared by every route group: the SAME AgentContext (and therefore the same repository, HITL
# inbox, audit log and checkpointer) that create_app's /v1/runs path uses.
ctx, joining_day_jobs = build_context()
if OnboardingAgent.agent_id not in ctx.policies.agents:
    # Fail fast: without its policy envelope every request would be refused (fail-closed, but
    # indistinguishable from an authorization error). An installed package has no guardrails/
    # beside it, so AGENT_HOME must point at the directory that holds guardrails/policy.yaml.
    raise RuntimeError(f"no policy envelope for '{OnboardingAgent.agent_id}' under {AGENT_HOME / 'guardrails'}; "
                       f"set AGENT_HOME to the directory containing guardrails/policy.yaml")
_workflow = OnboardingAgent(ctx)


def on_decision(ctx: AgentContext, agent: OnboardingAgent, item: ApprovalItem) -> None:
    """Every HITL decision this agent can produce -- whichever route created the approval -- is
    applied by the workflow, which then continues to the stage the decision unlocks. Failures
    propagate so HITLInbox records apply_error and the decision can be retried (finding S5)."""
    agent.handle_approval_decision(item)


def _tenant_check(ctx: AgentContext, instance: str, client_id: str) -> None:
    """Finding S4: verifies the /v1/runs target (an employee id) belongs to the caller's tenant
    BEFORE the graph executes, reusing the same check OnboardingToolRegistry._authorize_employee
    performs for the registry-backed routes. Another tenant's employee is reported exactly like an
    unknown one (KeyError -> the generic 404 below), so the response never confirms it exists."""
    repository = ctx.connector.repository
    bind = getattr(repository, "for_tenant", None)  # supabase mode: the lookup itself carries client_id
    repository = bind(client_id) if callable(bind) else repository
    record = repository.get_onboarding_record(instance)  # KeyError (-> 404) if unknown
    if record.client_id != client_id:
        ctx.audit.record("policy.denied", agent=OnboardingAgent.agent_id, client_id=client_id,
                         reason="cross_tenant_access", subject_id=instance, route="/v1/runs")
        raise KeyError(instance)


def _validate_decision(ctx: AgentContext, agent: OnboardingAgent, item: ApprovalItem, va: VerifiedActor,
                       body) -> None:
    """Runs before POST /v1/approvals/{id}/decision records anything (services/decisions.py): the
    specific team lead/buddy on record, an edited buddy's eligibility, an edited replan time or
    welcome content, and that the booking/draft is still pending. A refused decision leaves the
    approval PENDING for the right person -- it is not consumed."""
    actor = Actor(on_behalf_of=va.user_id, roles=va.roles, purpose_of_use=Purpose.ONBOARDING, client_id=va.client_id)
    reg = OnboardingToolRegistry.from_dependencies(ctx.connector, llm=ctx.llm, policy=agent.policy, hitl=ctx.hitl,
                                                   audit=ctx.audit, actor=actor)
    try:
        reg.validate_decision(item, decision=body.decision, approver=va.user_id, edited_payload=body.edited_payload)
    except DecisionNotApplicableError as e:
        raise DecisionConflictError(str(e)) from None
    except TenantAuthorizationError:
        raise KeyError(item.id) from None  # another tenant's: reported exactly like not found


app = create_app(service="hr-onboarding-agent", agent_home=AGENT_HOME, agent_cls=OnboardingAgent,
                 context=ctx, identity_provider=_identity_provider, on_decision=on_decision,
                 tenant_check=_tenant_check, decision_validator=_validate_decision)


@app.middleware("http")
async def _authorize_retry_apply(request: Request, call_next):
    """POST /v1/approvals/{id}/retry-apply (shared create_app route) checks only the caller's tenant.
    A retry re-runs a decision's side effects under the original approver's authority, so it is
    held to the decision's own rule: the caller must hold the item's approver_role, or be hr_admin
    (the operator role for this agent). Anyone else gets 403 before the route runs."""
    parts = request.url.path.strip("/").split("/")
    if request.method == "POST" and len(parts) == 4 and parts[:2] == ["v1", "approvals"] and parts[3] == "retry-apply":
        try:
            va = _identity_provider.authenticate(request.headers)
        except IdentityError as e:
            return JSONResponse(status_code=401, content={"detail": str(e)})
        try:
            item = ctx.hitl.get(parts[2], client_id=va.client_id)
        except KeyError:
            return JSONResponse(status_code=404, content={"detail": "approval not found"})
        if item.approver_role not in va.roles and "hr_admin" not in va.roles:
            ctx.audit.record("policy.denied", actor=va.user_id, agent=OnboardingAgent.agent_id,
                             client_id=va.client_id, reason="retry_apply_missing_role", approval_id=item.id,
                             required_role=item.approver_role)
            return JSONResponse(status_code=403, content={
                "detail": f"retrying approval {item.id} requires role '{item.approver_role}' or 'hr_admin'"})
    return await call_next(request)


def _actor_and_client(request: Request) -> tuple[Actor, str]:
    """Identity is verified through _identity_provider (finding S2), never read from
    X-User-Id/X-User-Roles/X-Client-Id directly in this module."""
    try:
        va: VerifiedActor = _identity_provider.authenticate(request.headers)
    except IdentityError as e:
        raise HTTPException(401, str(e)) from None
    actor = Actor(on_behalf_of=va.user_id, roles=va.roles, purpose_of_use=Purpose.ONBOARDING,
                  client_id=va.client_id)
    return actor, va.client_id


def _actor(actor_client: tuple[Actor, str] = Depends(_actor_and_client)) -> Actor:
    return actor_client[0]


def _registry(actor_client: tuple[Actor, str] = Depends(_actor_and_client)) -> OnboardingToolRegistry:
    actor, _client_id = actor_client
    return OnboardingToolRegistry.from_dependencies(ctx.connector, llm=ctx.llm,
                                                    policy=ctx.policies.for_agent(OnboardingAgent.agent_id),
                                                    hitl=ctx.hitl, audit=ctx.audit, actor=actor)


# --------------------------------------------------------------------------- request / response bodies

class IntakeBody(BaseModel):
    event_id: str


class EnsureTasksBody(BaseModel):
    due_date: date | None = None


class ScheduleSessionBody(BaseModel):
    session_type: SessionType
    start: AwareDatetime
    attendees: list[str] = Field(default_factory=list)


class ScheduleBuddyIntroBody(BaseModel):
    start: AwareDatetime


class ParticipantResponseBody(BaseModel):
    participant_id: str
    response: ResponseStatus
    responded_at: AwareDatetime | None = Field(
        default=None, description="when the participant responded, from the calendar provider; omitted = now. "
                                  "A response older than the one already recorded is ignored (409).")


class TimeoutSweepBody(BaseModel):
    as_of: AwareDatetime


class PolicyQuestionBody(BaseModel):
    question: str
    as_of: date
    employee_id: str | None = None


class OfferAcceptedBody(BaseModel):
    event_id: str
    induction_start: AwareDatetime | None = Field(
        default=None, description="HR-chosen induction start; omitted = first free in-hours slot on/after the "
                                  "later of the joining date and today")


class JoiningDayBody(BaseModel):
    as_of: date
    template_id: str = "welcome"


class ResolveOperationBody(BaseModel):
    key: str
    outcome: Literal["succeeded", "released"] = Field(
        description="succeeded = it did happen at the provider (never redo it); released = it did not happen, or "
                    "was undone (the next attempt starts fresh)")
    provider_ref: str | None = None
    reason: str | None = Field(default=None, max_length=500, description="what the operator checked (audited)")


class RedeliverWelcomeBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500, description="why a new delivery attempt is appropriate (audited)")


class WorkflowRunResult(BaseModel):
    response: AgentResponse
    workflow: dict
    approvals: list[ApprovalRef] = Field(default_factory=list)


def _approval_ref(item: ApprovalItem | None) -> ApprovalRef | None:
    return None if item is None else ApprovalRef(id=item.id, gate=item.gate, approver_role=item.approver_role,
                                                 subject_id=item.subject_id)


def _today() -> date:
    return datetime.now(ZoneInfo(ctx.settings.default_timezone)).date()


def _run_workflow(actor: Actor, event: OnboardingWorkflowInput, *, instance: str) -> WorkflowRunResult:
    response, summary = _workflow.run_event(event, actor=actor, instance=instance)
    approval_ids = dict.fromkeys(a.approval_id for a in response.actions_taken if a.approval_id)
    approvals = [_approval_ref(ctx.hitl.get(i, client_id=actor.client_id)) for i in approval_ids]
    return WorkflowRunResult(response=response, workflow=summary, approvals=approvals)


# --------------------------------------------------------------------------- workflow entry points

@app.post("/v1/onboarding/workflow/offer-accepted", response_model=WorkflowRunResult)
def workflow_offer_accepted(body: OfferAcceptedBody, actor: Actor = Depends(_actor)):
    """Stages 1-4: validate the event, fetch the employee, create the case and checklist, draft
    the induction meeting and request HR approval. Tenant-checked against the directory record."""
    return _run_workflow(actor, OnboardingWorkflowInput(event=WorkflowEvent.OFFER_ACCEPTED, event_id=body.event_id,
                                                        start=body.induction_start, as_of=_today()),
                         instance=body.event_id)


@app.post("/v1/onboarding/workflow/sessions/{session_id}/responses", response_model=WorkflowRunResult)
def workflow_participant_response(session_id: str, body: ParticipantResponseBody, actor: Actor = Depends(_actor),
                                  reg: OnboardingToolRegistry = Depends(_registry)):
    """Stage 6: record an RSVP; a decline opens the HR keep/cancel/replan decision."""
    employee_id = reg.get_session(session_id).employee_id  # tenant-checked before the workflow runs
    return _run_workflow(actor, OnboardingWorkflowInput(
        event=WorkflowEvent.PARTICIPANT_RESPONDED, employee_id=employee_id, session_id=session_id,
        participant_id=body.participant_id, response=body.response, responded_at=body.responded_at),
        instance=employee_id)


@app.post("/v1/onboarding/workflow/joining-day", response_model=WorkflowRunResult)
def workflow_joining_day(body: JoiningDayBody, actor: Actor = Depends(_actor)):
    """Stage 11 trigger for the caller's tenant: draft welcome messages for everyone starting on as_of."""
    return _run_workflow(actor, OnboardingWorkflowInput(event=WorkflowEvent.JOINING_DAY, as_of=body.as_of,
                                                        template_id=body.template_id),
                         instance=f"joining-day:{body.as_of.isoformat()}")


@app.post("/v1/onboarding/workflow/policy-questions", response_model=WorkflowRunResult)
def workflow_policy_question(body: PolicyQuestionBody, actor: Actor = Depends(_actor),
                             reg: OnboardingToolRegistry = Depends(_registry)):
    """Stage 10 through the workflow (audited as a run). /v1/onboarding/policy-questions returns
    the bare PolicyAnswer for the same question."""
    if body.employee_id is not None:
        reg.get_record(body.employee_id)  # tenant-checked before the workflow runs
    return _run_workflow(actor, OnboardingWorkflowInput(event=WorkflowEvent.POLICY_QUESTION, question=body.question,
                                                        as_of=body.as_of, employee_id=body.employee_id),
                         instance=body.employee_id or "policy-qa")


@app.get("/v1/onboarding/{employee_id}/workflow")
def workflow_case_status(employee_id: str, actor: Actor = Depends(_actor)):
    return _workflow.case_status(actor, employee_id)


# --------------------------------------------------------------------------- operations and recovery (hr_admin)

@app.get("/v1/onboarding/admin/reconciliation")
def reconciliation_report(reg: OnboardingToolRegistry = Depends(_registry)):
    """What a crash or restart left out of step between the repository, approvals, workflow
    checkpoints and providers, for the caller's tenant (sub_agent.reconciliation)."""
    return reg.reconciliation_report(lambda client_id: reconciliation.report(_workflow, client_id))


@app.get("/v1/onboarding/admin/operations")
def unsettled_operations(reg: OnboardingToolRegistry = Depends(_registry)):
    """Calendar bookings and messages whose provider outcome is unknown (PENDING/UNCERTAIN)."""
    return reg.list_unsettled_operations()


@app.post("/v1/onboarding/admin/operations/resolve")
def resolve_operation(body: ResolveOperationBody, reg: OnboardingToolRegistry = Depends(_registry)):
    return reg.resolve_operation(body.key, outcome=OperationStatus(body.outcome), provider_ref=body.provider_ref,
                                 reason=body.reason)


@app.post("/v1/onboarding/admin/welcome/{draft_id}/redeliver")
def redeliver_welcome(draft_id: str, body: RedeliverWelcomeBody, reg: OnboardingToolRegistry = Depends(_registry)):
    """hr_admin: a new delivery attempt of an approved welcome known not to have been delivered
    (operation resolved 'released', permanently refused by the provider, or never attempted). 409
    while the outcome is unknown or once delivered."""
    return reg.redeliver_welcome(draft_id, reason=body.reason)


# --------------------------------------------------------------------------- intake (UC steps 1-2)

@app.post("/v1/onboarding/intake")
def intake_offer_acceptance(body: IntakeBody, reg: OnboardingToolRegistry = Depends(_registry)):
    return reg.process_offer_acceptance_event(body.event_id)


@app.get("/v1/onboarding/{employee_id}")
def get_onboarding_record(employee_id: str, reg: OnboardingToolRegistry = Depends(_registry)):
    return reg.get_record(employee_id)


# --------------------------------------------------------------------------- checklist (UC step 2, 6)

@app.post("/v1/onboarding/{employee_id}/tasks")
def ensure_tasks(employee_id: str, body: EnsureTasksBody, reg: OnboardingToolRegistry = Depends(_registry)):
    return reg.ensure_tasks(employee_id, due_date=body.due_date)


@app.get("/v1/onboarding/{employee_id}/tasks")
def list_tasks(employee_id: str, reg: OnboardingToolRegistry = Depends(_registry)):
    return reg.list_tasks(employee_id)


@app.get("/v1/onboarding/{employee_id}/tasks/pending")
def pending_tasks(employee_id: str, reg: OnboardingToolRegistry = Depends(_registry)):
    return reg.pending_tasks(employee_id)


@app.get("/v1/onboarding/{employee_id}/tasks/overdue")
def overdue_tasks(employee_id: str, as_of: date = Query(...), reg: OnboardingToolRegistry = Depends(_registry)):
    return reg.overdue_tasks(employee_id, as_of=as_of)



# --------------------------------------------------------------------------- scheduling (UC steps 3, 4, 6, 7)

@app.post("/v1/onboarding/{employee_id}/sessions")
def schedule_session(employee_id: str, body: ScheduleSessionBody, reg: OnboardingToolRegistry = Depends(_registry)):
    session, item = reg.schedule_session(employee_id, session_type=body.session_type, start=body.start,
                                         attendees=body.attendees, workflow_instance=employee_id)
    return {"session": session, "approval": _approval_ref(item)}


@app.post("/v1/onboarding/{employee_id}/sessions/buddy-intro")
def schedule_buddy_intro_session(employee_id: str, body: ScheduleBuddyIntroBody,
                                 reg: OnboardingToolRegistry = Depends(_registry)):
    session, item = reg.schedule_buddy_intro(employee_id, start=body.start, workflow_instance=employee_id)
    return {"session": session, "approval": _approval_ref(item)}


@app.get("/v1/onboarding/{employee_id}/sessions")
def list_sessions(employee_id: str, reg: OnboardingToolRegistry = Depends(_registry)):
    return reg.list_sessions(employee_id)


@app.get("/v1/onboarding/{employee_id}/sessions/outstanding")
def outstanding_sessions(employee_id: str, reg: OnboardingToolRegistry = Depends(_registry)):
    return reg.outstanding_sessions(employee_id)


@app.get("/v1/onboarding/sessions/{session_id}/invitations")
def list_invitations(session_id: str, reg: OnboardingToolRegistry = Depends(_registry)):
    return reg.list_invitations(session_id)


@app.post("/v1/onboarding/sessions/check-timeouts")
def check_timeouts(body: TimeoutSweepBody, reg: OnboardingToolRegistry = Depends(_registry)):
    return reg.check_approval_timeouts(as_of=body.as_of)


# --------------------------------------------------------------------------- attendance (UC step 5)

@app.post("/v1/onboarding/sessions/{session_id}/responses")
def record_response(session_id: str, body: ParticipantResponseBody,
                    reg: OnboardingToolRegistry = Depends(_registry)):
    return reg.record_participant_response(session_id, body.participant_id, body.response,
                                           responded_at=body.responded_at)


@app.get("/v1/onboarding/sessions/{session_id}/attendance")
def attendance_status(session_id: str, reg: OnboardingToolRegistry = Depends(_registry)):
    return {"session_id": session_id, "satisfied": reg.is_attendance_satisfied(session_id)}


# --------------------------------------------------------------------------- buddy assignment (UC step 6)

@app.post("/v1/onboarding/{employee_id}/buddy")
def propose_buddy(employee_id: str, reg: OnboardingToolRegistry = Depends(_registry)):
    buddy_id, item = reg.propose_buddy_assignment(employee_id, workflow_instance=employee_id)
    return {"buddy_id": buddy_id, "approval": _approval_ref(item)}


# --------------------------------------------------------------------------- policy Q&A (UC step 8)

@app.post("/v1/onboarding/policy-questions")
def policy_question(body: PolicyQuestionBody, reg: OnboardingToolRegistry = Depends(_registry)):
    return reg.answer_policy_question(body.question, as_of=body.as_of, employee_id=body.employee_id)


@app.get("/v1/onboarding/policy-escalations/{escalation_id}")
def policy_escalation_status(escalation_id: str, reg: OnboardingToolRegistry = Depends(_registry)):
    """Status of a policy escalation, for HR (hr_admin) or the employee who raised it. Another person's
    or another tenant's escalation is reported as not found. No question text or details."""
    return reg.policy_escalation_status(escalation_id)


# --------------------------------------------------------------------------- exception -> HTTP status mapping
# Registered on the exact exception types/hierarchies the services above actually raise, so a
# caller gets a meaningful status code instead of a generic 500. Order does not matter: Starlette
# resolves the most specific registered ancestor in an exception's MRO.

def _error_response(status_code: int):
    def handler(request, exc):
        return JSONResponse(status_code=status_code, content={"detail": str(exc), "error_type": type(exc).__name__})
    return handler


def _not_found(request, exc):
    """Not found and another tenant's record get the same response, byte for byte, so a caller
    cannot learn that a record exists in someone else's tenant (finding F / R04). The audit log
    keeps the real reason (policy.denied, reason=cross_tenant_access)."""
    return JSONResponse(status_code=404, content={"detail": "not found", "error_type": "NotFound"})


app.add_exception_handler(KeyError, _not_found)                       # not found (record/session/draft/...)
app.add_exception_handler(TenantAuthorizationError, _not_found)       # another tenant's record: same as not found
app.add_exception_handler(TenantMismatchError, _not_found)
app.add_exception_handler(UnknownTemplateError, _error_response(422))  # overrides KeyError for this one subtype
app.add_exception_handler(PermissionError, _error_response(403))       # policy/approver/role violations, kill switch
app.add_exception_handler(DuplicateRecordError, _error_response(409))  # duplicate task/session/case
app.add_exception_handler(DuplicateSendError, _error_response(409))    # replayed approval_id
app.add_exception_handler(CalendarConflictError, _error_response(409))
app.add_exception_handler(CalendarBookingError, _error_response(502))  # calendar provider failure
app.add_exception_handler(EmailServiceError, _error_response(502))     # email provider failure
app.add_exception_handler(NotificationDeliveryError, _error_response(502))  # notification provider failure
app.add_exception_handler(ConnectorNotConfiguredError, _error_response(503))  # connector not wired in
app.add_exception_handler(SchedulingError, _error_response(422))       # invalid meeting time, no buddy, etc.
app.add_exception_handler(StaleParticipantResponseError, _error_response(409))  # a newer RSVP is already recorded
app.add_exception_handler(DeliveryOutcomeUncertainError, _error_response(409))  # HR must resolve the operation
app.add_exception_handler(ValueError, _error_response(422))            # validation catch-all (incl. Pydantic)
app.add_exception_handler(LLMError, lambda request, exc: JSONResponse(  # never echo provider error text
    status_code=503, content={"detail": "the language model is unavailable", "error_type": "LLMError"}))
