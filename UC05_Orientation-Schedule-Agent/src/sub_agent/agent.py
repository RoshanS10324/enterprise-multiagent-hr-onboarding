"""Employee onboarding agent (agent_id: employee-onboarding) -- the onboarding workflow.

Assistive and capped at autonomy L1: every communication, booking and buddy assignment is proposed
for a human to approve. The agent never sends or confirms on its own say-so.

One LangGraph StateGraph (OnboardingAgent.build_graph) holds every stage. Nodes are thin: each
calls OnboardingToolRegistry, which performs tenant/role authorization and routes the call through
the policy gateway (allowlist, kill switch, audit, approval gates) before any service or connector
runs. Business rules live in sub_agent/services; external systems sit behind connectors/.

Event-driven execution
----------------------
Onboarding spans weeks (offer acceptance -> approvals -> RSVPs -> joining day), so no process waits
for it. Every external event is one graph execution that runs until the next human or external
wait and then ends. The durable records -- the onboarding repository and the HITLInbox -- carry the
case from one event to the next:

  event                  entry point                                          stages
  offer_accepted         POST /v1/onboarding/workflow/offer-accepted          1-4
  approval_decided       HITL listener (POST /v1/approvals/{id}/decision)     5, 7, 8, 9, 11
  participant_responded  POST /v1/onboarding/workflow/sessions/{id}/responses 6
  joining_day            sub_agent.scheduler, POST .../workflow/joining-day   11
  policy_question        POST /v1/onboarding/workflow/policy-questions        10
  draft_welcome          POST /v1/runs  (the original request contract, unchanged)

approval_decided is accepted only from the HITL decision listener (handle_approval_decision), never
from /v1/runs, so a caller cannot replay a decision's side effects.

Checkpointing, resume and idempotency
-------------------------------------
Each execution runs on its own checkpoint thread:
  thread_id = employee-onboarding:<client_id>:<event>:<key>
where <key> is the event's natural idempotency key (the offer email's event id, the approval id)
or, for events without one, the run id.
  - A run that raises part-way (a provider outage, the kill switch) leaves its checkpoint on the
    failed node. Redelivering the same event -- or HITLInbox.retry_apply for a decision -- resumes
    there: steps that already completed are not repeated.
  - Redelivering an event whose run already completed is reported as a duplicate and not
    re-executed, except after the outcomes HR is expected to fix and resend (invalid employee data,
    no schedulable slot), which run again.
  - Services are idempotent underneath (intake by event id, checklist by type, sessions by type,
    buddy proposals while pending, welcome sends by draft status), so even a lost checkpoint
    cannot create a duplicate meeting, assignment or welcome message.
  - External side effects (calendar holds, every notification) go through the operation ledger
    (services/delivery.py, services/scheduling.py): a retry reuses the same provider idempotency
    key, and an outcome that cannot be retried safely is routed to HR instead of repeated.
Durability (HR_AGENT_PERSIST=1, see sub_agent/runtime.py): the checkpointer is SQLite at
<HR_AGENT_DATA_DIR>/checkpoints.db, the onboarding repository and operation ledger are SQLite at
onboarding.db, and the HITL inbox and audit log sit beside them, so cases, pending decisions and
their checkpoints survive a restart together. sub_agent/reconciliation.py reports anything a crash
left between those stores. Without persistence (development and tests only) all of them are in
memory.

Graph (conditional edges in brackets):
  START -> receive_event -[event]-> one branch below, or reject_event -> END
  draft_welcome:     load_case -[active]-> draft_welcome -> request_hr_approval -> END
                               -[inactive]-> END
  offer_accepted:    validate_offer_event -[invalid]-> flag_invalid_employee_data -> END
                     -[valid]-> prepare_checklist -> draft_induction_meeting
                         -[drafted]-> request_hr_approval -> END
                         -[failed]-> scheduling_failed -> END
  approval_decided:  load_decision -[proposal type]->
                       apply_meeting_decision -[induction confirmed]-> assign_buddy
                                              -[rejected]-> meeting_rejected -> END
                       apply_conflict_decision -[keep/cancel]-> END
                                               -[replanned]-> request_hr_approval
                       apply_buddy_decision -[assigned]-> prepare_team_intro
                                            -[rejected]-> buddy_assignment_failed -> END
                       apply_welcome_decision -[approved]-> deliver_welcome
                                                 -[sent]-> END  -[failed/uncertain]-> welcome_delivery_failed
                     assign_buddy -[proposed]-> request_hr_approval  -[assigned]-> prepare_team_intro
                                  -[failed]-> buddy_assignment_failed
                     prepare_team_intro -[drafted]-> request_hr_approval  -[failed]-> scheduling_failed
  participant_responded: record_participant_response -[declined]-> request_hr_approval -> END
                                                     -[accepted]-> check_attendance -> END
                                                     -[stale]-> END
  joining_day:       prepare_welcome_messages -[drafted]-> request_hr_approval -> END
  policy_question:   answer_policy_question -> END
"""

from __future__ import annotations

import operator
import sqlite3
import threading
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, TypedDict, TypeVar
from zoneinfo import ZoneInfo

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, model_validator

from connectors.calendar.base import CalendarConflictError
from connectors.notifications.base import NotificationRejectedError
from hr_onboarding_common.agent import AgentContext, BaseAgent, RunContext, run_context
from hr_onboarding_common.config import resolve_agent_home
from hr_onboarding_common.contracts import (Actor, AgentRequest, AgentResponse, ExceptionCode, PolicyRef,
                                            Provenance, Scope, WorkflowRef, now_utc)
from hr_onboarding_common.contracts import ResponseStatus as RunStatus
from hr_onboarding_common.llm import LLMError
from hr_onboarding_common.platform.hitl import ApprovalItem, Decision
from hr_onboarding_common.platform.policy import PolicyViolation
from sub_agent.models import (CaseStatus, ClientCalendarUnavailableError, ClientConfigurationError,
                              CommunicationStatus, DuplicateSessionError, MissingClientConfigError, OnboardingRecord,
                              OnboardingSession, ResponseStatus, SessionStatus, SessionType)
from sub_agent.services.delivery import DeliveryOutcomeUncertainError
from sub_agent.services.intake import IncompleteEmployeeDataError, MalformedOfferAcceptanceEmailError
from sub_agent.services.policy_qa import PolicyAnswer
from sub_agent.services.scheduling import (BookingOutcomeUncertainError, InvalidMeetingTimeError, NoAvailableSlotError,
                                           NoBuddyCandidatesError, StaleParticipantResponseError)
from sub_agent.tool_registry import (DuplicateSendError, InvalidWelcomeContentError, OnboardingDependencies,
                                     OnboardingToolRegistry, TenantAuthorizationError)

AGENT_ID = "employee-onboarding"
AGENT_HOME = resolve_agent_home(__file__)
DECISION_STEP = "hitl_decision"  # WorkflowRef.step of a run started by the HITL decision listener
T = TypeVar("T")


class WorkflowEvent(StrEnum):
    DRAFT_WELCOME = "draft_welcome"
    OFFER_ACCEPTED = "offer_accepted"
    APPROVAL_DECIDED = "approval_decided"
    PARTICIPANT_RESPONDED = "participant_responded"
    JOINING_DAY = "joining_day"
    POLICY_QUESTION = "policy_question"


class Stage(StrEnum):
    RECEIVED = "received"
    EVENT_REJECTED = "event_rejected"
    CASE_INACTIVE = "case_inactive"
    CASE_CREATED = "case_created"
    INVALID_EMPLOYEE_DATA = "invalid_employee_data"
    CHECKLIST_READY = "checklist_ready"
    INDUCTION_AWAITING_APPROVAL = "induction_awaiting_approval"
    INDUCTION_CONFIRMED = "induction_confirmed"
    TEAM_INTRO_AWAITING_APPROVAL = "team_intro_awaiting_approval"
    TEAM_INTRO_CONFIRMED = "team_intro_confirmed"
    MEETING_AWAITING_APPROVAL = "meeting_awaiting_approval"
    MEETING_CONFIRMED = "meeting_confirmed"
    MEETING_REJECTED = "meeting_rejected"
    SCHEDULING_FAILED = "scheduling_failed"
    AWAITING_RESPONSES = "awaiting_responses"
    ATTENDANCE_CONFIRMED = "attendance_confirmed"
    CONFLICT_AWAITING_HR = "conflict_awaiting_hr_decision"
    CONFLICT_SUPERSEDED = "conflict_superseded"  # the decline was withdrawn before HR's decision applied
    RESPONSE_SUPERSEDED = "response_superseded"  # an RSVP older than the one already recorded
    MEETING_KEPT = "meeting_kept"
    MEETING_CANCELLED = "meeting_cancelled"
    MEETING_REPLANNED = "meeting_replanned"
    BUDDY_AWAITING_APPROVAL = "buddy_awaiting_approval"
    BUDDY_ASSIGNED = "buddy_assigned"
    BUDDY_ASSIGNMENT_FAILED = "buddy_assignment_failed"
    WELCOME_AWAITING_APPROVAL = "welcome_awaiting_approval"
    WELCOME_AWAITING_DELIVERY = "welcome_awaiting_delivery"
    WELCOME_SENT = "welcome_sent"
    WELCOME_REJECTED = "welcome_rejected"
    WELCOME_DELIVERY_FAILED = "welcome_delivery_failed"
    WELCOME_DELIVERY_UNCERTAIN = "welcome_delivery_uncertain"  # may have been delivered; HR must check
    WELCOME_NOT_CONFIGURED = "welcome_not_configured"  # the tenant's ClientConfig is missing or unusable
    NOTHING_TO_DO = "nothing_to_do"
    POLICY_ANSWERED = "policy_answered"
    POLICY_NO_ANSWER = "policy_no_answer"
    DECISION_NOT_APPLICABLE = "decision_not_applicable"


# Terminal stages HR is expected to fix and then resend the same event for (directory data
# corrected, calendar freed up). Every other completed event is a duplicate if redelivered.
REDELIVERABLE_STAGES = frozenset({Stage.INVALID_EMPLOYEE_DATA, Stage.SCHEDULING_FAILED, Stage.EVENT_REJECTED})
# Events whose completed run is reported as a duplicate when redelivered. A joining-day run is keyed
# by its date only so a failed run can resume; a completed one may always run again (the service
# skips anyone already drafted).
DEDUPLICATED_EVENTS = frozenset({WorkflowEvent.OFFER_ACCEPTED, WorkflowEvent.APPROVAL_DECIDED})
WELCOME_EVENTS = frozenset({WorkflowEvent.DRAFT_WELCOME, WorkflowEvent.JOINING_DAY})

_AWAITING = {SessionType.INDUCTION: Stage.INDUCTION_AWAITING_APPROVAL,
             SessionType.TEAM_INTRO: Stage.TEAM_INTRO_AWAITING_APPROVAL}
_CONFIRMED = {SessionType.INDUCTION: Stage.INDUCTION_CONFIRMED, SessionType.TEAM_INTRO: Stage.TEAM_INTRO_CONFIRMED}

REQUIRED_FIELDS: dict[WorkflowEvent, tuple[str, ...]] = {
    WorkflowEvent.DRAFT_WELCOME: ("employee_id",),
    WorkflowEvent.OFFER_ACCEPTED: ("event_id",),
    WorkflowEvent.APPROVAL_DECIDED: ("approval_id",),
    WorkflowEvent.PARTICIPANT_RESPONDED: ("employee_id", "session_id", "participant_id", "response"),
    WorkflowEvent.JOINING_DAY: ("as_of",),
    WorkflowEvent.POLICY_QUESTION: ("question", "as_of"),
}


class OnboardingWorkflowInput(BaseModel):
    """One workflow event. `{"employee_id": ...}` alone is the original /v1/runs request body and
    still means "draft this employee's welcome message"."""

    event: WorkflowEvent = WorkflowEvent.DRAFT_WELCOME
    employee_id: str | None = None
    event_id: str | None = None  # offer_accepted: the offer-acceptance email event
    approval_id: str | None = None  # approval_decided
    session_id: str | None = None  # participant_responded
    participant_id: str | None = None
    response: ResponseStatus | None = None
    responded_at: datetime | None = None  # participant_responded: the provider's timestamp for the RSVP
    as_of: date | None = None  # joining_day / policy_question; offer_accepted: earliest induction date
    question: str | None = None  # policy_question
    start: datetime | None = None  # offer_accepted: HR-chosen induction start (else first free slot)
    template_id: str = "welcome"

    @model_validator(mode="after")
    def _fields_for_event(self) -> "OnboardingWorkflowInput":
        missing = [f for f in REQUIRED_FIELDS[self.event] if getattr(self, f) in (None, "")]
        if missing:
            raise ValueError(f"event '{self.event}' requires {', '.join(missing)}")
        return self

    @property
    def idempotency_key(self) -> str | None:
        return {WorkflowEvent.OFFER_ACCEPTED: self.event_id,
                WorkflowEvent.APPROVAL_DECIDED: self.approval_id,
                WorkflowEvent.JOINING_DAY: self.as_of.isoformat() if self.as_of else None}.get(self.event)


WelcomeSummaryInput = OnboardingWorkflowInput  # the original /v1/runs input name, kept for callers


class OnboardingState(TypedDict, total=False):
    """Per-execution workflow state. Holds identifiers and statuses only -- no names, email
    addresses, message bodies or secrets; nodes read those from the repository when needed."""

    input: dict[str, Any]  # the OnboardingWorkflowInput, JSON form
    event: str
    workflow_id: str  # == the LangGraph thread_id
    run_id: str  # correlates with the audit log's agent.started/finished and the AgentResponse
    client_id: str
    employee_id: str
    stage: str
    route: str  # branch selector read by the next conditional edge
    meeting_session_id: str
    meeting_type: str
    meeting_status: str
    approval_id: str
    approval_decision: str
    pending_approval_ids: list[str]  # approvals this run opened, handed to request_hr_approval
    participant_id: str
    participant_response: str
    conflict_decision: str  # keep | cancel | replan
    buddy_id: str
    buddy_status: str
    team_intro_status: str
    welcome_draft_id: str
    welcome_status: str
    rejection_reason: str
    errors: Annotated[list[dict[str, str]], operator.add]
    history: Annotated[list[dict[str, str]], operator.add]


def _stage(stage: Stage, **fields: Any) -> dict[str, Any]:
    return {"stage": stage.value, "history": [{"stage": stage.value, "at": now_utc().isoformat()}], **fields}


def _error(stage: Stage, exc: BaseException) -> list[dict[str, str]]:
    return [{"stage": stage.value, "error": type(exc).__name__, "detail": str(exc)}]


def _req(value: T | None, field: str) -> T:
    """An event field REQUIRED_FIELDS guarantees for this event type (validated on input)."""
    if value is None:
        raise ValueError(f"workflow event is missing required field '{field}'")
    return value


def _route(state: OnboardingState) -> str:
    return state["route"]


def build_checkpointer(data_dir: Path | None) -> BaseCheckpointSaver:
    """The workflow's checkpointer: SQLite at <data_dir>/checkpoints.db when a data directory is
    given (api.py passes HR_AGENT_DATA_DIR when HR_AGENT_PERSIST=1), else in-memory. SqliteSaver serializes
    access with its own lock, so one connection is shared safely across the API's worker threads."""
    if data_dir is None:
        return MemorySaver()
    data_dir.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(str(data_dir / "checkpoints.db"), check_same_thread=False))
    saver.setup()
    return saver


def workflow_id_for(event: WorkflowEvent | str, client_id: str | None, key: str) -> str:
    """The checkpoint thread id of one workflow execution, e.g. the run that applied approval
    `key`: workflow_id_for("approval_decided", client_id, approval_id)."""
    return f"{AGENT_ID}:{client_id}:{WorkflowEvent(event).value}:{key}"


def _thread_id(inp: OnboardingWorkflowInput, request: AgentRequest) -> str:
    key = inp.idempotency_key
    if inp.event == WorkflowEvent.APPROVAL_DECIDED and request.workflow.step != DECISION_STEP:
        key = None  # only the HITL listener may run (or resume) an approval's own thread
    return workflow_id_for(inp.event, request.actor.client_id, key or request.run_id)


class _CheckpointedWorkflow:
    """Adapts the compiled, checkpointed graph to BaseAgent.run(), which calls
    graph.invoke(state, config) without a thread_id. Picks the event's thread, serializes
    executions on the same thread (striped locks), resumes a thread left on a failed node, and
    reports a redelivered completed event as a duplicate instead of re-running it."""

    _locks = [threading.Lock() for _ in range(64)]

    def __init__(self, compiled, agent: "OnboardingAgent"):
        self.compiled, self.agent = compiled, agent

    def invoke(self, state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        rc = run_context(config)
        inp = OnboardingWorkflowInput.model_validate(state["input"])
        thread_id = _thread_id(inp, rc.request)
        cfg = {"configurable": {**config["configurable"], "thread_id": thread_id}}
        # Welcome drafting checks for an existing draft before creating one, so every run that can
        # draft a welcome for this tenant is serialized (within this process).
        lock_key = f"{rc.request.actor.client_id}:welcome" if inp.event in WELCOME_EVENTS else thread_id
        with self._locks[hash(lock_key) % len(self._locks)]:
            snapshot = self.compiled.get_state(cfg)
            if snapshot.next:
                self.agent.ctx.audit.record("workflow.resumed", agent=AGENT_ID, client_id=rc.request.actor.client_id,
                                            workflow_id=thread_id, run_id=rc.request.run_id,
                                            next_nodes=list(snapshot.next))
                return self.compiled.invoke(None, cfg)
            previous = snapshot.values
            if (previous and inp.event in DEDUPLICATED_EVENTS and inp.idempotency_key
                    and previous.get("stage") not in REDELIVERABLE_STAGES):
                self.agent.ctx.audit.record("workflow.duplicate_event", agent=AGENT_ID,
                                            client_id=rc.request.actor.client_id, workflow_id=thread_id,
                                            run_id=rc.request.run_id, original_run_id=previous.get("run_id"))
                rc.acted("workflow.duplicate_event", subject_id=previous.get("employee_id") or None,
                         workflow_id=thread_id, stage=previous.get("stage"), original_run_id=previous.get("run_id"))
                return previous
            return self.compiled.invoke({**state, "workflow_id": thread_id}, cfg)

    def get_state(self, config: dict[str, Any]):
        return self.compiled.get_state(config)

    def get_graph(self):
        return self.compiled.get_graph()


class OnboardingAgent(BaseAgent[OnboardingWorkflowInput]):
    agent_id = AGENT_ID
    version = "0.3.0"
    input_model = OnboardingWorkflowInput
    preamble = """You are the Employee Onboarding Support agent for a UAE company's HR team.
You prepare welcome communications, meeting drafts and buddy proposals for new hires so that HR can
review them. You never send a communication, confirm a booking or mark anything as signed yourself;
a human always makes that call.
Return only the fields of the requested schema."""

    def __init__(self, ctx: AgentContext):
        if not isinstance(ctx.connector, OnboardingDependencies):
            raise TypeError("AgentContext.connector must be an OnboardingDependencies bundle "
                            "(see sub_agent.fakes.build_fake_dependencies)")
        self.deps: OnboardingDependencies = ctx.connector
        if self.deps.checkpointer is None:
            self.deps.checkpointer = MemorySaver()  # shared by every agent built from this bundle
        super().__init__(ctx)

    # ------------------------------------------------------------------ graph

    def build_graph(self):
        g = StateGraph(OnboardingState)
        for name, fn in [
            ("receive_event", self._receive_event), ("reject_event", self._reject_event),
            ("load_case", self._load_case), ("draft_welcome", self._draft_welcome),
            ("validate_offer_event", self._validate_offer_event),
            ("flag_invalid_employee_data", self._flag_invalid_employee_data),
            ("prepare_checklist", self._prepare_checklist),
            ("draft_induction_meeting", self._meeting_drafter(SessionType.INDUCTION)),
            ("request_hr_approval", self._request_hr_approval), ("scheduling_failed", self._scheduling_failed),
            ("load_decision", self._load_decision), ("apply_meeting_decision", self._apply_meeting_decision),
            ("meeting_rejected", self._meeting_rejected), ("apply_conflict_decision", self._apply_conflict_decision),
            ("assign_buddy", self._assign_buddy), ("apply_buddy_decision", self._apply_buddy_decision),
            ("buddy_assignment_failed", self._buddy_assignment_failed),
            ("prepare_team_intro", self._meeting_drafter(SessionType.TEAM_INTRO)),
            ("apply_welcome_decision", self._apply_welcome_decision), ("deliver_welcome", self._deliver_welcome),
            ("welcome_delivery_failed", self._welcome_delivery_failed),
            ("record_participant_response", self._record_participant_response),
            ("check_attendance", self._check_attendance),
            ("prepare_welcome_messages", self._prepare_welcome_messages),
            ("answer_policy_question", self._answer_policy_question),
            ("apply_policy_escalation_decision", self._apply_policy_escalation_decision),
        ]:
            g.add_node(name, fn)

        g.add_edge(START, "receive_event")
        g.add_conditional_edges("receive_event", _route, {
            "reject": "reject_event", WorkflowEvent.DRAFT_WELCOME.value: "load_case",
            WorkflowEvent.OFFER_ACCEPTED.value: "validate_offer_event",
            WorkflowEvent.APPROVAL_DECIDED.value: "load_decision",
            WorkflowEvent.PARTICIPANT_RESPONDED.value: "record_participant_response",
            WorkflowEvent.JOINING_DAY.value: "prepare_welcome_messages",
            WorkflowEvent.POLICY_QUESTION.value: "answer_policy_question"})
        g.add_edge("reject_event", END)

        # draft_welcome (the original /v1/runs flow)
        g.add_conditional_edges("load_case", _route, {"active": "draft_welcome", "inactive": END})
        g.add_edge("draft_welcome", "request_hr_approval")

        # stages 1-4: offer acceptance -> checklist -> induction draft -> HR approval request
        g.add_conditional_edges("validate_offer_event", _route, {
            "valid": "prepare_checklist", "invalid": "flag_invalid_employee_data", "inactive": END})
        g.add_edge("flag_invalid_employee_data", END)
        g.add_edge("prepare_checklist", "draft_induction_meeting")
        g.add_conditional_edges("draft_induction_meeting", _route, {
            "drafted": "request_hr_approval", "failed": "scheduling_failed", "exists": END})
        g.add_edge("request_hr_approval", END)
        g.add_edge("scheduling_failed", END)

        # stages 5, 7, 8, 9, 11: applying a human decision, then the next stage it unlocks
        g.add_conditional_edges("load_decision", _route, {
            "meeting": "apply_meeting_decision", "conflict": "apply_conflict_decision",
            "buddy": "apply_buddy_decision", "welcome": "apply_welcome_decision",
            "policy_escalation": "apply_policy_escalation_decision", "ignore": END})
        g.add_edge("apply_policy_escalation_decision", END)
        g.add_conditional_edges("apply_meeting_decision", _route, {
            "induction_confirmed": "assign_buddy", "confirmed": END, "rejected": "meeting_rejected", "stale": END})
        g.add_edge("meeting_rejected", END)
        g.add_conditional_edges("apply_conflict_decision", _route, {
            "resolved": END, "replanned": "request_hr_approval", "replan_failed": "scheduling_failed"})
        g.add_conditional_edges("assign_buddy", _route, {
            "proposed": "request_hr_approval", "awaiting": END, "assigned": "prepare_team_intro",
            "failed": "buddy_assignment_failed"})
        g.add_conditional_edges("apply_buddy_decision", _route, {
            "assigned": "prepare_team_intro", "rejected": "buddy_assignment_failed", "stale": END})
        g.add_edge("buddy_assignment_failed", END)
        g.add_conditional_edges("prepare_team_intro", _route, {
            "drafted": "request_hr_approval", "failed": "scheduling_failed", "exists": END})
        g.add_conditional_edges("apply_welcome_decision", _route, {
            "approved": "deliver_welcome", "rejected": END, "sent": END})
        g.add_conditional_edges("deliver_welcome", _route, {"sent": END, "failed": "welcome_delivery_failed"})
        g.add_edge("welcome_delivery_failed", END)

        # stage 6: RSVPs; a decline routes to the HR conflict decision
        g.add_conditional_edges("record_participant_response", _route, {
            "conflict": "request_hr_approval", "accepted": "check_attendance", "reject": "reject_event",
            "stale": END})
        g.add_edge("check_attendance", END)

        # stage 11 trigger, stage 10
        g.add_conditional_edges("prepare_welcome_messages", _route, {
            "drafted": "request_hr_approval", "nothing": END})
        g.add_edge("answer_policy_question", END)

        return _CheckpointedWorkflow(g.compile(checkpointer=self.deps.checkpointer), self)

    def initial_state(self, inp: OnboardingWorkflowInput) -> dict[str, Any]:
        return {"input": inp.model_dump(mode="json")}

    # ------------------------------------------------------------------ run tracking (agent_runs)

    def _llm_call_count(self) -> int:
        return len(getattr(self.ctx.llm, "calls", None) or [])

    def _record_run(self, *, response: AgentResponse | None, run_id: str, client_id: str | None, actor_id: str,
                    subject_id: str | None, started_at: datetime, calls_before: int,
                    error: BaseException | None = None) -> None:
        """One agent_runs row per run when a run recorder is configured (supabase mode). A failure to
        record is not swallowed: it propagates like any other persistence failure."""
        recorder = self.deps.run_recorder
        if recorder is None:
            return
        calls = list(getattr(self.ctx.llm, "calls", None) or [])[calls_before:]
        recorder.record(response=response, run_id=run_id, client_id=client_id, actor_id=actor_id,
                        agent_id=self.agent_id, agent_version=self.version, subject_id=subject_id,
                        models=[c.model for c in calls], started_at=started_at, error=error)
        if client_id:  # the tenant-scoped completion event (BaseAgent's agent.finished names no client)
            failed = error is not None or response is None
            self.ctx.audit.record("workflow.failed" if failed else "workflow.completed", agent=self.agent_id,
                                  subject_id=subject_id, client_id=client_id, run_id=run_id,
                                  status="failed" if failed else str(response.status),
                                  error_type=type(error).__name__ if error is not None else None,
                                  exceptions=[e.code for e in response.exceptions] if response is not None else [])

    def run(self, request: AgentRequest) -> AgentResponse:
        started, calls_before = now_utc(), self._llm_call_count()
        subject = request.scope.subject_ids[0] if request.scope.subject_ids else None
        try:
            response = super().run(request)
        except Exception as exc:
            self._record_run(response=None, run_id=request.run_id, client_id=request.actor.client_id,
                             actor_id=request.actor.on_behalf_of, subject_id=subject, started_at=started,
                             calls_before=calls_before, error=exc)
            raise
        self._record_run(response=response, run_id=request.run_id, client_id=request.actor.client_id,
                         actor_id=request.actor.on_behalf_of, subject_id=subject, started_at=started,
                         calls_before=calls_before)
        return response

    # ------------------------------------------------------------------ entry points

    def run_event(self, event: OnboardingWorkflowInput, *, actor: Actor, instance: str) -> tuple[AgentResponse, dict]:
        """Runs one workflow event for a verified actor through BaseAgent.run (kill switch,
        provenance, audit, exception mapping). Returns the AgentResponse and the run's final
        workflow state summary."""
        request = AgentRequest[OnboardingWorkflowInput](
            workflow=WorkflowRef(id=self.agent_id, instance=instance, step=event.event.value), actor=actor,
            scope=Scope(subject_ids=[event.employee_id] if event.employee_id else []), input=event)
        response = self.run(request)
        return response, self.workflow_summary(event, request)

    def handle_approval_decision(self, item: ApprovalItem) -> AgentResponse | None:
        """HITLInbox listener: applies a human decision and continues the workflow from it.

        Unlike BaseAgent.run, every failure propagates -- the kill switch, an unauthorized
        approver, a provider outage -- so HITLInbox records `apply_error` and the decision stays
        retryable through POST /v1/approvals/{id}/retry-apply (finding S5), which resumes this
        decision's checkpoint thread where it stopped."""
        if item.agent != self.agent_id:
            return None
        policy = self.policy
        if not policy.enabled:
            self.ctx.audit.record("policy.denied", agent=self.agent_id, client_id=item.client_id,
                                  reason="agent_disabled", approval_id=item.id)
            raise PolicyViolation(f"{policy.policy}: agent is disabled (kill switch); decision {item.id} not applied")
        actor = Actor(on_behalf_of=item.approver or "hitl", roles=[item.approver_role], client_id=item.client_id)
        event = OnboardingWorkflowInput(event=WorkflowEvent.APPROVAL_DECIDED, approval_id=item.id,
                                        employee_id=item.subject_id)
        request = AgentRequest[OnboardingWorkflowInput](
            workflow=WorkflowRef(id=self.agent_id, instance=item.workflow_instance, step=DECISION_STEP),
            actor=actor, scope=Scope(subject_ids=[item.subject_id] if item.subject_id else []), input=event,
            policy=PolicyRef(ref=policy.policy, version=policy.version, autonomy=policy.autonomy,
                             gates=list(policy.gates)))
        prov = Provenance(agent=self.agent_id, agent_version=self.version, policy=f"{policy.policy}@v{policy.version}")
        rc = RunContext(self, request, policy, prov)
        started, calls_before = now_utc(), self._llm_call_count()
        self.ctx.audit.record("agent.started", actor=actor.on_behalf_of, agent=self.agent_id,
                              client_id=item.client_id, run_id=request.run_id, workflow=request.workflow.model_dump(),
                              approval_id=item.id)
        try:
            self.graph.invoke(self.initial_state(event), config={"configurable": {"rc": rc}})
        except Exception as exc:
            self._record_run(response=None, run_id=request.run_id, client_id=item.client_id,
                             actor_id=actor.on_behalf_of, subject_id=item.subject_id, started_at=started,
                             calls_before=calls_before, error=exc)
            raise
        prov.tool_calls, prov.finished_at = list(rc.tools.calls), now_utc()
        status = (RunStatus.PROPOSED if rc.proposals else RunStatus.ACTED if rc.actions else RunStatus.EXCEPTION)
        self.ctx.audit.record("agent.finished", agent=self.agent_id, run_id=request.run_id, status=status,
                              proposals=len(rc.proposals), exceptions=len(rc.exceptions),
                              client_id=item.client_id)
        response = AgentResponse(run_id=request.run_id, status=status, proposals=rc.proposals,
                                 actions_taken=rc.actions, exceptions=rc.exceptions, brain_feedback=rc.feedback,
                                 provenance=prov)
        self._record_run(response=response, run_id=request.run_id, client_id=item.client_id,
                         actor_id=actor.on_behalf_of, subject_id=item.subject_id, started_at=started,
                         calls_before=calls_before)
        return response

    def workflow_summary(self, event: OnboardingWorkflowInput, request: AgentRequest) -> dict:
        """The checkpointed final state of the run that handled `event` (ids and statuses only)."""
        return self.workflow_state(_thread_id(event, request))

    def workflow_state(self, workflow_id: str) -> dict:
        """The checkpointed state of one workflow execution (see workflow_id_for). `pending_nodes`
        is non-empty only for a run that stopped on an error and can be resumed."""
        snapshot = self.graph.get_state({"configurable": {"thread_id": workflow_id}})
        values = {k: v for k, v in snapshot.values.items() if k not in ("input", "route")}
        return {**values, "workflow_id": workflow_id, "pending_nodes": list(snapshot.next)}

    def case_status(self, actor: Actor, employee_id: str) -> dict:
        """Where one employee's onboarding currently stands, derived from the durable records (the
        source of truth between events), not from any single run's state."""
        reg = self._registry_for(actor, gateway=None)
        record = reg.get_record(employee_id)
        sessions = reg.list_sessions(employee_id)
        drafts = [d for d in reg.list_communication_drafts(employee_id) if d.template_id == "welcome"]
        pending = reg.pending_approvals(employee_id)

        def latest(session_type: SessionType) -> OnboardingSession | None:
            live = [s for s in sessions if s.session_type == session_type and s.status != SessionStatus.CANCELLED]
            return max(live, key=lambda s: s.scheduled_start, default=None)

        # an undecided conflict whose decline has since been withdrawn needs no decision
        superseded = {i.id: reason for i in pending if i.proposal_type == "attendee_declined"
                      and (reason := reg.conflict_superseded_reason(i)) is not None}
        pending = [i for i in pending if i.id not in superseded]
        induction, team_intro = latest(SessionType.INDUCTION), latest(SessionType.TEAM_INTRO)
        welcome = max(drafts, key=lambda d: d.created_at, default=None)
        if record.case_status != CaseStatus.ACTIVE:
            stage = Stage.CASE_INACTIVE
        elif welcome and welcome.status == CommunicationStatus.SENT:
            stage = Stage.WELCOME_SENT
        elif welcome and welcome.status == CommunicationStatus.DRAFT:
            stage = Stage.WELCOME_AWAITING_APPROVAL
        elif team_intro:
            stage = _CONFIRMED[SessionType.TEAM_INTRO] if team_intro.status == SessionStatus.CONFIRMED \
                else _AWAITING[SessionType.TEAM_INTRO]
        elif record.buddy_id:
            stage = Stage.BUDDY_ASSIGNED
        elif any(i.proposal_type == "buddy_assignment" for i in pending):
            stage = Stage.BUDDY_AWAITING_APPROVAL
        elif induction:
            stage = _CONFIRMED[SessionType.INDUCTION] if induction.status == SessionStatus.CONFIRMED \
                else _AWAITING[SessionType.INDUCTION]
        else:
            stage = Stage.CASE_CREATED
        stale_for = getattr(reg.port, "stale_sessions", None)  # supabase mode: joining-date revisions
        stale = {"stale_sessions": [s.id for s in stale_for(employee_id)]} if callable(stale_for) else {}
        return {
            **stale,
            "employee_id": employee_id, "client_id": record.client_id, "case_status": record.case_status,
            "stage": stage, "buddy_id": record.buddy_id,
            "induction": induction.status if induction else None,
            "team_intro": team_intro.status if team_intro else None,
            "welcome": welcome.status if welcome else None,
            "pending_approvals": [{"id": i.id, "gate": i.gate, "proposal_type": i.proposal_type,
                                   "approver_role": i.approver_role} for i in pending],
            "superseded_approvals": [{"id": i, "reason": reason} for i, reason in superseded.items()],
        }

    # ------------------------------------------------------------------ helpers

    def _registry_for(self, actor: Actor, *, gateway) -> OnboardingToolRegistry:
        return OnboardingToolRegistry.from_dependencies(self.deps, llm=self.ctx.llm, policy=self.policy,
                                                        hitl=self.ctx.hitl, audit=self.ctx.audit, actor=actor,
                                                        gateway=gateway)

    def _reg(self, config) -> OnboardingToolRegistry:
        """One registry per run, sharing the run's own ToolGateway (rc.tools) so the response's
        provenance lists every tool the workflow called."""
        rc = run_context(config)
        if not self.policy.enabled:  # live policy, not the snapshot the run started with
            self.ctx.audit.record("policy.denied", agent=AGENT_ID, client_id=rc.request.actor.client_id,
                                  reason="agent_disabled", run_id=rc.request.run_id)
            raise PolicyViolation(f"{AGENT_ID} was disabled during this run; its remaining steps were not executed")
        reg = getattr(rc, "onboarding_registry", None)
        if reg is None:
            reg = self._registry_for(rc.request.actor, gateway=rc.tools)
            rc.onboarding_registry = reg
        return reg

    @staticmethod
    def _input(state: OnboardingState) -> OnboardingWorkflowInput:
        return OnboardingWorkflowInput.model_validate(state["input"])

    def _today(self, now: datetime) -> date:
        return now.astimezone(ZoneInfo(self.policy.settings.get("timezone", "Asia/Dubai"))).date()

    def _earliest(self, record: OnboardingRecord, inp: OnboardingWorkflowInput, now: datetime) -> date:
        return max(record.start_date, self._today(now), inp.as_of or date.min)

    # ------------------------------------------------------------------ entry + rejection

    def _receive_event(self, state: OnboardingState, config) -> dict[str, Any]:
        rc, inp = run_context(config), self._input(state)
        base = {"event": inp.event.value, "run_id": rc.request.run_id, "client_id": rc.request.actor.client_id or "",
                "employee_id": inp.employee_id or "", "pending_approval_ids": []}
        from_listener = rc.request.workflow.step == DECISION_STEP
        if inp.event == WorkflowEvent.APPROVAL_DECIDED and not from_listener:
            return {**base, "route": "reject", "rejection_reason": "approval decisions are applied only by the "
                    "HITL decision listener (POST /v1/approvals/{id}/decision)", **_stage(Stage.EVENT_REJECTED)}
        if inp.employee_id and not from_listener and rc.request.workflow.instance != inp.employee_id:
            # finding S4: /v1/runs tenant-checks `instance`, so the graph must act on that same employee
            return {**base, "route": "reject", "rejection_reason": f"run instance '{rc.request.workflow.instance}' "
                    f"does not match the event's employee '{inp.employee_id}'", **_stage(Stage.EVENT_REJECTED)}
        return {**base, "route": inp.event.value, **_stage(Stage.RECEIVED)}

    def _reject_event(self, state: OnboardingState, config) -> dict[str, Any]:
        run_context(config).exception(ExceptionCode.POLICY_BREACH, state["rejection_reason"],
                                      state.get("employee_id") or None)
        return _stage(Stage.EVENT_REJECTED)

    # ------------------------------------------------------------------ draft_welcome (original /v1/runs flow)

    def _load_case(self, state: OnboardingState, config) -> dict[str, Any]:
        rc, inp = run_context(config), self._input(state)
        record = self._reg(config).get_record(_req(inp.employee_id, "employee_id"))  # tenant-checked; KeyError
        if record.case_status != CaseStatus.ACTIVE:
            rc.exception(ExceptionCode.POLICY_BREACH,
                         f"onboarding case for {inp.employee_id} is {record.case_status.value}; "
                         f"welcome message not drafted", inp.employee_id)
            return {"route": "inactive", **_stage(Stage.CASE_INACTIVE)}
        return {"route": "active", "client_id": record.client_id}

    def _draft_welcome(self, state: OnboardingState, config) -> dict[str, Any]:
        rc, inp, reg = run_context(config), self._input(state), self._reg(config)
        employee_id = _req(inp.employee_id, "employee_id")
        existing = [d for d in reg.list_communication_drafts(employee_id)
                    if d.template_id == inp.template_id and d.status != CommunicationStatus.REJECTED]
        if existing:  # a retry, or a welcome already drafted by the joining-day trigger
            draft = existing[-1]
            items = [i for i in self.ctx.hitl.list(status=None, client_id=rc.request.actor.client_id)
                     if i.agent == AGENT_ID and i.proposal_type == "welcome_message"
                     and i.payload.get("id") == draft.id]
            rc.acted("welcome.draft_communication", subject_id=employee_id,
                     approval_id=items[0].id if items else None, draft_id=draft.id, subject=draft.subject,
                     body=draft.body, status=draft.status, reused=True)
            stage = {CommunicationStatus.DRAFT: Stage.WELCOME_AWAITING_APPROVAL,
                     CommunicationStatus.APPROVED: Stage.WELCOME_AWAITING_DELIVERY}.get(draft.status,
                                                                                      Stage.WELCOME_SENT)
            return {"welcome_draft_id": draft.id, "welcome_status": draft.status.value, **_stage(stage)}
        try:
            client = reg.get_client_config(state["client_id"])
            draft, item = reg.draft_welcome(employee_id, template_id=inp.template_id,
                                            language=client.default_language, workflow_instance=employee_id)
        except KeyError as e:  # no ClientConfig (repository KeyError) or an unusable one
            return self._welcome_not_configured(rc, employee_id, e)
        # the service already submitted its own HITL item, so this is recorded as a completed
        # action referencing it -- never proposed a second time through rc.propose().
        rc.acted("welcome.draft_communication", subject_id=inp.employee_id, approval_id=item.id,
                 draft_id=draft.id, subject=draft.subject, body=draft.body, status=draft.status)
        return {"welcome_draft_id": draft.id, "welcome_status": draft.status.value, "approval_id": item.id,
                "pending_approval_ids": [item.id], **_stage(Stage.WELCOME_AWAITING_APPROVAL)}

    # ------------------------------------------------------------------ stages 1-2: offer acceptance

    def _validate_offer_event(self, state: OnboardingState, config) -> dict[str, Any]:
        """Stages 1-2: validate the offer-acceptance event, retrieve the employee from the
        directory, check required fields, and create the case (idempotent by event id)."""
        rc, inp = run_context(config), self._input(state)
        try:
            record = self._reg(config).process_offer_acceptance_event(_req(inp.event_id, "event_id"))
        except (MalformedOfferAcceptanceEmailError, IncompleteEmployeeDataError, KeyError) as e:
            return {"route": "invalid", "errors": _error(Stage.INVALID_EMPLOYEE_DATA, e)}
        rc.acted("intake.process_offer_acceptance_event", subject_id=record.employee_id, event_id=inp.event_id,
                 case_status=record.case_status)
        if record.case_status != CaseStatus.ACTIVE:
            rc.exception(ExceptionCode.POLICY_BREACH, f"onboarding case for {record.employee_id} is "
                         f"{record.case_status.value}; onboarding not started", record.employee_id)
            return {"route": "inactive", "employee_id": record.employee_id, **_stage(Stage.CASE_INACTIVE)}
        return {"route": "valid", "employee_id": record.employee_id, "client_id": record.client_id,
                **_stage(Stage.CASE_CREATED)}

    def _flag_invalid_employee_data(self, state: OnboardingState, config) -> dict[str, Any]:
        error = state["errors"][-1]
        run_context(config).exception(ExceptionCode.MISSING_DATA,
                                      f"offer-acceptance event {self._input(state).event_id} not processed: "
                                      f"{error['error']}: {error['detail']}", state.get("employee_id") or None)
        return _stage(Stage.INVALID_EMPLOYEE_DATA)

    def _prepare_checklist(self, state: OnboardingState, config) -> dict[str, Any]:
        reg, eid = self._reg(config), state["employee_id"]
        tasks = reg.ensure_tasks(eid)
        run_context(config).acted("checklist.prepared", subject_id=eid, tasks=len(tasks))
        return _stage(Stage.CHECKLIST_READY)

    # ------------------------------------------------------------------ stages 3 and 9: drafting a meeting

    def _meeting_drafter(self, session_type: SessionType):
        def draft_meeting(state: OnboardingState, config) -> dict[str, Any]:
            """Drafts one session of `session_type`: participants from policy settings, an
            HR-chosen start (induction only) or the first free in-hours slot, office-hours and
            availability validated, a TENTATIVE calendar hold, and its HITL approval item.
            Idempotent: an existing non-cancelled session of this type is reused, not duplicated,
            and a booking whose provider outcome is unknown (a timeout) is retried at the same slot
            with the same idempotency key rather than searched for again."""
            rc, inp, reg, eid = run_context(config), self._input(state), self._reg(config), state["employee_id"]
            existing = [s for s in reg.list_sessions(eid)
                        if s.session_type == session_type and s.status != SessionStatus.CANCELLED]
            if existing:
                session = existing[0]
                stage = (_CONFIRMED if session.status == SessionStatus.CONFIRMED else _AWAITING)[session_type]
                return {"route": "exists", "meeting_session_id": session.id, "meeting_type": session_type.value,
                        "meeting_status": session.status.value, **_stage(stage)}
            record = reg.get_record(eid)
            attendees = reg.participants_for(eid, session_type)
            now = self.deps.clock()
            try:
                in_flight = reg.in_flight_booking(eid, session_type)
                if in_flight is not None:
                    start = datetime.fromisoformat(in_flight.detail["start"])
                elif session_type == SessionType.INDUCTION and inp.start:
                    start = inp.start
                    if start.tzinfo is not None and start <= now:
                        raise InvalidMeetingTimeError(f"{start.isoformat()} is in the past")
                else:
                    start = reg.find_slot(eid, session_type=session_type, attendees=attendees,
                                          earliest=self._earliest(record, inp, now), not_before=now)
                session, item = reg.schedule_session(eid, session_type=session_type, start=start,
                                                     attendees=attendees, workflow_instance=eid)
            except (CalendarConflictError, InvalidMeetingTimeError, NoAvailableSlotError, DuplicateSessionError,
                    BookingOutcomeUncertainError, ClientCalendarUnavailableError) as e:
                return {"route": "failed", "meeting_type": session_type.value,
                        "errors": _error(Stage.SCHEDULING_FAILED, e), **_stage(Stage.SCHEDULING_FAILED)}
            rc.acted("scheduling.schedule_session", subject_id=eid, approval_id=item.id, session_id=session.id,
                     session_type=session_type, start=session.scheduled_start.isoformat(),
                     attendees=session.attendees, status=session.status, approver_role=item.approver_role)
            extra = {"team_intro_status": session.status.value} if session_type == SessionType.TEAM_INTRO else {}
            return {"route": "drafted", "meeting_session_id": session.id, "meeting_type": session_type.value,
                    "meeting_status": session.status.value, "approval_id": item.id, "pending_approval_ids": [item.id],
                    **extra, **_stage(_AWAITING[session_type])}

        draft_meeting.__name__ = f"draft_{session_type.value}"
        return draft_meeting

    def _scheduling_failed(self, state: OnboardingState, config) -> dict[str, Any]:
        error = state["errors"][-1]
        run_context(config).acted("workflow.hr_attention_required", subject_id=state.get("employee_id") or None,
                                  reason="scheduling_failed", meeting_type=state.get("meeting_type"),
                                  error=error["error"], detail=error["detail"])
        return _stage(Stage.SCHEDULING_FAILED)

    # ------------------------------------------------------------------ stage 4: approval request

    def _request_hr_approval(self, state: OnboardingState, config) -> dict[str, Any]:
        """Delivers each approval this run opened to its approver over the configured channel
        (Teams/Outlook via NotificationPort). A permanent rejection is recorded and the item stays
        decidable in the HITL inbox; a provider outage propagates so the run can be resumed."""
        rc, reg = run_context(config), self._reg(config)
        errors: list[dict[str, str]] = []
        for approval_id in state.get("pending_approval_ids", []):
            item = reg.get_approval(approval_id)
            try:
                receipt = reg.notify_approver(item)
            except NotificationRejectedError as e:
                errors += _error(Stage(state["stage"]), e)
                continue
            rc.acted("notifications.request_approval", subject_id=item.subject_id, approval_id=item.id,
                     gate=item.gate, recipient_id=receipt.recipient_id, channel=receipt.channel,
                     simulated=receipt.simulated)
        return {"pending_approval_ids": [], "errors": errors}

    # ------------------------------------------------------------------ stages 5, 7, 8, 9, 11: decisions

    def _load_decision(self, state: OnboardingState, config) -> dict[str, Any]:
        item = self._reg(config).get_approval(_req(self._input(state).approval_id, "approval_id"))  # tenant-scoped
        route = {"session_booking": "meeting", "attendee_declined": "conflict", "buddy_assignment": "buddy",
                 "welcome_message": "welcome", "policy_escalation": "policy_escalation"}.get(item.proposal_type, "ignore")
        if item.agent != AGENT_ID or item.status == Decision.PENDING:
            route = "ignore"
        fields = {"route": route, "employee_id": item.subject_id or "", "approval_id": item.id,
                  "approval_decision": item.status.value}
        if route not in ("ignore", "policy_escalation") and item.is_approved and item.subject_id:
            case = self._reg(config).get_record(item.subject_id)
            if case.case_status != CaseStatus.ACTIVE:  # a rejection still runs: it only releases things
                run_context(config).exception(
                    ExceptionCode.POLICY_BREACH, f"onboarding case for {item.subject_id} is {case.case_status.value}; "
                    f"approved {item.proposal_type} {item.id} was not applied", item.subject_id)
                return {**fields, "route": "ignore", **_stage(Stage.CASE_INACTIVE)}
        return {**fields, **_stage(Stage.DECISION_NOT_APPLICABLE)} if route == "ignore" else fields

    def _apply_meeting_decision(self, state: OnboardingState, config) -> dict[str, Any]:
        """Stage 5: confirm (approved) or release (rejected) the tentative booking. Raises
        UnauthorizedApproverError if a team_lead/buddy gate was decided by someone else."""
        rc, reg = run_context(config), self._reg(config)
        item = reg.get_approval(state["approval_id"])
        session = reg.apply_meeting_decision(item)
        rc.acted("scheduling.apply_decision", subject_id=item.subject_id, approval_id=item.id, session_id=session.id,
                 decision=item.status, session_status=session.status)
        fields = {"meeting_session_id": session.id, "meeting_type": session.session_type.value,
                  "meeting_status": session.status.value}
        if session.session_type == SessionType.TEAM_INTRO:
            fields["team_intro_status"] = session.status.value
        if session.status == SessionStatus.CONFIRMED:
            stage = _CONFIRMED.get(session.session_type, Stage.MEETING_CONFIRMED)
            route = "induction_confirmed" if session.session_type == SessionType.INDUCTION else "confirmed"
            return {"route": route, **fields, **_stage(stage)}
        if session.status == SessionStatus.CANCELLED and item.status == Decision.REJECTED:
            return {"route": "rejected", **fields, **_stage(Stage.MEETING_REJECTED)}
        return {"route": "stale", **fields, **_stage(Stage.DECISION_NOT_APPLICABLE)}  # e.g. timed out first

    def _meeting_rejected(self, state: OnboardingState, config) -> dict[str, Any]:
        run_context(config).acted("workflow.hr_attention_required", subject_id=state.get("employee_id") or None,
                                  reason="meeting_rejected", meeting_type=state.get("meeting_type"),
                                  session_id=state.get("meeting_session_id"))
        return _stage(Stage.MEETING_REJECTED)

    def _apply_conflict_decision(self, state: OnboardingState, config) -> dict[str, Any]:
        """Stage 7: HR's decision on a declined participant -- keep (approve), cancel (reject) or
        replan (edit with new_start: a fresh booking through its own availability check and
        approval)."""
        rc, reg = run_context(config), self._reg(config)
        item = reg.get_approval(state["approval_id"])
        decision = {Decision.APPROVED: "keep", Decision.REJECTED: "cancel", Decision.EDITED: "replan"}[item.status]
        reason = reg.conflict_superseded_reason(item)  # derived from the records now, not when the item opened
        if reason is not None:
            reg.apply_conflict_decision(item)  # records the late decision; changes nothing
            if reason == "session_cancelled":
                return {"route": "resolved", "conflict_decision": decision, **_stage(Stage.DECISION_NOT_APPLICABLE)}
            errors: list[dict[str, str]] = []
            try:
                reg.notify_conflict_superseded(item, reason)
            except NotificationRejectedError as e:
                errors = _error(Stage.CONFLICT_SUPERSEDED, e)
            rc.acted("scheduling.conflict_superseded", subject_id=item.subject_id, approval_id=item.id,
                     decision=decision, reason=reason)
            return {"route": "resolved", "conflict_decision": decision, "errors": errors,
                    **_stage(Stage.CONFLICT_SUPERSEDED)}
        try:
            rescheduled = reg.apply_conflict_decision(item)
        except (CalendarConflictError, InvalidMeetingTimeError, KeyError, ValueError, TypeError) as e:
            return {"route": "replan_failed", "conflict_decision": decision,
                    "errors": _error(Stage.SCHEDULING_FAILED, e), **_stage(Stage.SCHEDULING_FAILED)}
        rc.acted("scheduling.apply_attendance_decision", subject_id=item.subject_id, approval_id=item.id,
                 decision=decision, session_id=item.payload["session_id"],
                 rescheduled_session_id=rescheduled.id if rescheduled else None)
        if rescheduled is None:
            stage = Stage.MEETING_KEPT if decision == "keep" else Stage.MEETING_CANCELLED
            return {"route": "resolved", "conflict_decision": decision, **_stage(stage)}
        new_items = [i.id for i in reg.pending_approvals(item.subject_id, proposal_type="session_booking")
                     if i.payload.get("id") == rescheduled.id]
        return {"route": "replanned", "conflict_decision": decision, "meeting_session_id": rescheduled.id,
                "meeting_type": rescheduled.session_type.value, "meeting_status": rescheduled.status.value,
                "pending_approval_ids": new_items, **_stage(Stage.MEETING_REPLANNED)}

    def _assign_buddy(self, state: OnboardingState, config) -> dict[str, Any]:
        """Stage 8: propose an eligible buddy from the client's authorized pool (same team
        first) for HR approval -- once; an already-pending proposal is not re-sent."""
        rc, reg, eid = run_context(config), self._reg(config), state["employee_id"]
        if reg.pending_approvals(eid, proposal_type="buddy_assignment"):
            return {"route": "awaiting", **_stage(Stage.BUDDY_AWAITING_APPROVAL)}
        try:
            buddy_id, item = reg.propose_buddy_assignment(eid, workflow_instance=eid)
        except NoBuddyCandidatesError as e:
            return {"route": "failed", "buddy_status": "no_candidates",
                    "errors": _error(Stage.BUDDY_ASSIGNMENT_FAILED, e)}
        if item is None:
            return {"route": "assigned", "buddy_id": buddy_id, "buddy_status": "assigned",
                    **_stage(Stage.BUDDY_ASSIGNED)}
        rc.acted("scheduling.propose_buddy_assignment", subject_id=eid, approval_id=item.id, buddy_id=buddy_id)
        return {"route": "proposed", "buddy_id": buddy_id, "buddy_status": "proposed", "approval_id": item.id,
                "pending_approval_ids": [item.id], **_stage(Stage.BUDDY_AWAITING_APPROVAL)}

    def _apply_buddy_decision(self, state: OnboardingState, config) -> dict[str, Any]:
        """Stage 8 on HR's decision. An ineligible (e.g. HR-edited, other-tenant) buddy raises
        IneligibleBuddyError and nothing is assigned. A provider outage while notifying raises so
        the apply is retried; a permanent rejection of a notification leaves the assignment in
        place, is routed to HR, and the workflow continues to the team intro."""
        rc, reg = run_context(config), self._reg(config)
        item = reg.get_approval(state["approval_id"])
        errors: list[dict[str, str]] = []
        try:
            record = reg.apply_buddy_decision(item)
        except NotificationRejectedError as e:
            errors = _error(Stage.BUDDY_ASSIGNED, e)
            record = reg.get_record(item.subject_id)
            rc.acted("workflow.hr_attention_required", subject_id=item.subject_id, reason="buddy_notification_rejected",
                     buddy_id=record.buddy_id, detail=str(e))
        rc.acted("scheduling.apply_buddy_decision", subject_id=item.subject_id, approval_id=item.id,
                 decision=item.status, buddy_id=record.buddy_id)
        if item.is_approved and record.buddy_id:
            return {"route": "assigned", "buddy_id": record.buddy_id, "errors": errors,
                    "buddy_status": "assigned_notification_failed" if errors else "assigned",
                    **_stage(Stage.BUDDY_ASSIGNED)}
        if item.status == Decision.REJECTED:
            return {"route": "rejected", "buddy_status": "rejected"}
        return {"route": "stale", **_stage(Stage.DECISION_NOT_APPLICABLE)}

    def _buddy_assignment_failed(self, state: OnboardingState, config) -> dict[str, Any]:
        errors = state.get("errors") or []
        run_context(config).acted("workflow.hr_attention_required", subject_id=state.get("employee_id") or None,
                                  reason="buddy_assignment_failed", buddy_status=state.get("buddy_status"),
                                  detail=errors[-1]["detail"] if errors else None)
        return _stage(Stage.BUDDY_ASSIGNMENT_FAILED)

    def _apply_welcome_decision(self, state: OnboardingState, config) -> dict[str, Any]:
        rc, reg = run_context(config), self._reg(config)
        item = reg.get_approval(state["approval_id"])
        draft = reg.apply_welcome_decision(item)
        rc.acted("welcome.apply_decision", subject_id=item.subject_id, approval_id=item.id, draft_id=draft.id,
                 decision=item.status, draft_status=draft.status)
        fields = {"welcome_draft_id": draft.id, "welcome_status": draft.status.value}
        if draft.status == CommunicationStatus.SENT:
            return {"route": "sent", **fields, **_stage(Stage.WELCOME_SENT)}
        if draft.status == CommunicationStatus.APPROVED:
            return {"route": "approved", **fields}
        return {"route": "rejected", **fields, **_stage(Stage.WELCOME_REJECTED)}

    def _deliver_welcome(self, state: OnboardingState, config) -> dict[str, Any]:
        """Stage 11: send the HR-approved content exactly once. A permanent provider rejection is
        routed to HR; an outage propagates (the decision's apply is retried and resumes here)."""
        rc, reg = run_context(config), self._reg(config)
        try:
            result = reg.send_welcome_email(draft_id=state["welcome_draft_id"], approval_id=state["approval_id"])
        except DuplicateSendError:
            return {"route": "sent", "welcome_status": CommunicationStatus.SENT.value, **_stage(Stage.WELCOME_SENT)}
        except (NotificationRejectedError, InvalidWelcomeContentError) as e:
            return {"route": "failed", "welcome_status": "delivery_failed",
                    "errors": _error(Stage.WELCOME_DELIVERY_FAILED, e)}
        except DeliveryOutcomeUncertainError as e:  # never resent blindly: it may already have arrived
            return {"route": "failed", "welcome_status": "delivery_uncertain",
                    "errors": _error(Stage.WELCOME_DELIVERY_UNCERTAIN, e)}
        rc.acted("comms.send_welcome_email", subject_id=state.get("employee_id") or None,
                 approval_id=state["approval_id"], draft_id=state["welcome_draft_id"], simulated=result["simulated"])
        return {"route": "sent", "welcome_status": CommunicationStatus.SENT.value, **_stage(Stage.WELCOME_SENT)}

    def _welcome_delivery_failed(self, state: OnboardingState, config) -> dict[str, Any]:
        stage = (Stage.WELCOME_DELIVERY_UNCERTAIN if state.get("welcome_status") == "delivery_uncertain"
                 else Stage.WELCOME_DELIVERY_FAILED)
        run_context(config).acted("workflow.hr_attention_required", subject_id=state.get("employee_id") or None,
                                  reason=stage.value, draft_id=state.get("welcome_draft_id"),
                                  detail=state["errors"][-1]["detail"])
        return _stage(stage)

    # ------------------------------------------------------------------ stage 6: RSVPs

    def _record_participant_response(self, state: OnboardingState, config) -> dict[str, Any]:
        rc, inp, reg, eid = run_context(config), self._input(state), self._reg(config), state["employee_id"]
        session_id, participant_id = _req(inp.session_id, "session_id"), _req(inp.participant_id, "participant_id")
        response = _req(inp.response, "response")
        session = reg.get_session(session_id)  # tenant-checked
        if session.employee_id != eid:
            return {"route": "reject", "rejection_reason": f"session {session_id} does not belong to {eid}"}
        before = {i.id for i in reg.pending_approvals(eid, proposal_type="attendee_declined")}
        fields = {"meeting_session_id": session.id, "meeting_type": session.session_type.value,
                  "participant_id": participant_id, "participant_response": response.value}
        try:
            reg.record_participant_response(session_id, participant_id, response, workflow_instance=eid,
                                            responded_at=inp.responded_at)
        except StaleParticipantResponseError as e:  # out of order: a newer RSVP is already recorded
            rc.acted("scheduling.stale_response_ignored", subject_id=eid, session_id=session_id,
                     participant_id=participant_id, response=response)
            return {"route": "stale", **fields, "errors": _error(Stage.RESPONSE_SUPERSEDED, e),
                    **_stage(Stage.RESPONSE_SUPERSEDED)}
        rc.acted("scheduling.record_participant_response", subject_id=eid, session_id=session_id,
                 participant_id=participant_id, response=response)
        if response == ResponseStatus.DECLINED:
            new_items = [i.id for i in reg.pending_approvals(eid, proposal_type="attendee_declined")
                         if i.id not in before]
            return {"route": "conflict", "pending_approval_ids": new_items, **fields,
                    **_stage(Stage.CONFLICT_AWAITING_HR)}
        errors: list[dict[str, str]] = []
        for item, reason in reg.superseded_conflicts(session_id, participant_id):  # the decline was withdrawn
            try:
                reg.notify_conflict_superseded(item, reason)
            except NotificationRejectedError as e:
                errors += _error(Stage.CONFLICT_SUPERSEDED, e)
                continue
            rc.acted("scheduling.conflict_superseded", subject_id=eid, approval_id=item.id, reason=reason)
        return {"route": "accepted", "errors": errors, **fields}

    def _check_attendance(self, state: OnboardingState, config) -> dict[str, Any]:
        satisfied = self._reg(config).is_attendance_satisfied(state["meeting_session_id"])
        return _stage(Stage.ATTENDANCE_CONFIRMED if satisfied else Stage.AWAITING_RESPONSES)

    # ------------------------------------------------------------------ stage 11 trigger, stage 10

    def _prepare_welcome_messages(self, state: OnboardingState, config) -> dict[str, Any]:
        """Joining day: draft (never send) a templated, branded, language-checked welcome for every
        active case starting on as_of in the actor's tenant, each with its own HR approval."""
        rc, inp = run_context(config), self._input(state)
        as_of = _req(inp.as_of, "as_of")
        try:
            results = self._reg(config).run_joining_day_trigger(as_of=as_of, template_id=inp.template_id)
        except ClientConfigurationError as e:  # one config per tenant: nobody in this run can be drafted
            return {"route": "nothing", **self._welcome_not_configured(rc, None, e)}
        drafted = [r for r in results if r["status"] == "drafted"]
        rc.acted("joining_trigger.run", as_of=as_of.isoformat(), drafted=len(drafted),
                 skipped=len(results) - len(drafted), results=results)
        if not drafted:
            return {"route": "nothing", **_stage(Stage.NOTHING_TO_DO)}
        return {"route": "drafted", "pending_approval_ids": [r["approval_id"] for r in drafted],
                **_stage(Stage.WELCOME_AWAITING_APPROVAL)}

    @staticmethod
    def _welcome_not_configured(rc: RunContext, employee_id: str | None, exc: KeyError) -> dict[str, Any]:
        """The run ends without a draft, recorded as an exception (so the joining-day scheduler and
        the orchestrator retry it once the tenant is configured). The message names no record."""
        error = exc if isinstance(exc, ClientConfigurationError) else MissingClientConfigError(
            "no welcome configuration (languages, branding, templates) is set up for this client")
        rc.exception(ExceptionCode.MISSING_DATA, f"welcome not drafted: {error}", employee_id)
        return {"pending_approval_ids": [], "errors": _error(Stage.WELCOME_NOT_CONFIGURED, error),
                **_stage(Stage.WELCOME_NOT_CONFIGURED)}

    def _answer_policy_question(self, state: OnboardingState, config) -> dict[str, Any]:
        """Stage 10. A model outage becomes the same safe "no answer" as missing evidence, with a
        generic message: BaseAgent would otherwise copy the provider's raw error text into the
        caller's response. The error type is kept in the audit log for operators."""
        rc, inp = run_context(config), self._input(state)
        try:
            answer = self._reg(config).answer_policy_question(
                _req(inp.question, "question"), as_of=_req(inp.as_of, "as_of"), employee_id=inp.employee_id)
        except LLMError as e:
            self.ctx.audit.record("policy_qa.no_answer", agent=AGENT_ID, subject_id=inp.employee_id,
                                  client_id=rc.request.actor.client_id, reason="model_unavailable",
                                  error_type=type(e).__name__)
            answer = PolicyAnswer(answer=None, citations=[], no_answer_reason="model_unavailable")
            rc.exception(ExceptionCode.INTERNAL_ERROR, "policy answer unavailable: the language model could not be "
                         "reached", inp.employee_id)
        if answer.no_answer_reason == "injection_suspected":
            rc.exception(ExceptionCode.INJECTION_SUSPECTED, "policy question refused: it matched a prompt-injection "
                         "pattern", inp.employee_id)
        # Never the question text: a sensitive question's content stays out of the run's outputs.
        rc.acted("policy_qa.answer_question", subject_id=inp.employee_id, answer=answer.answer,
                 citations=[c.model_dump(mode="json") for c in answer.citations],
                 no_answer_reason=answer.no_answer_reason, category=answer.category, message=answer.message,
                 policy_reference=answer.policy_reference,
                 escalation=answer.escalation.model_dump(mode="json") if answer.escalation else None)
        return _stage(Stage.POLICY_ANSWERED if answer.answer else Stage.POLICY_NO_ANSWER)

    def _apply_policy_escalation_decision(self, state: OnboardingState, config) -> dict[str, Any]:
        """HR resolved (approved / edited with a reply) or closed (rejected) a policy escalation."""
        rc, reg = run_context(config), self._reg(config)
        item = reg.get_approval(state["approval_id"])
        outcome = reg.apply_policy_escalation_decision(item)
        rc.acted("policy_qa.apply_escalation_decision", subject_id=item.subject_id, approval_id=item.id,
                 decision=item.status, **outcome)
        return _stage(Stage.POLICY_ANSWERED if outcome.get("status") == "resolved" else Stage.POLICY_NO_ANSWER)


__all__ = ["AGENT_HOME", "AGENT_ID", "OnboardingAgent", "OnboardingState", "OnboardingWorkflowInput", "Stage",
           "TenantAuthorizationError", "WelcomeSummaryInput", "WorkflowEvent", "build_checkpointer",
           "workflow_id_for"]
