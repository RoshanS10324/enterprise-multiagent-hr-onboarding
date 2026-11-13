"""Wires the onboarding services through the real ToolGateway + Policy + HITLInbox + AuditLog --
the same enforcement mechanism hr_onboarding_common's BaseAgent uses via RunContext.

Every caller goes through this registry: the onboarding workflow (agent.py -- one registry per
run, sharing that run's RunContext.tools gateway so provenance captures every tool call) and the
/v1/onboarding/* routes (api.py -- one registry per request, for the verified caller). That makes
it the single place where an operation is allowlist-checked, kill-switch-checked, audited,
approval-gated where configured, and tenant/role authorized.

Every employee-scoped operation is tenant-checked against the registry's own actor_client_id
BEFORE the underlying service runs. Because ToolGateway.call() checks the kill switch and the
policy allowlist (and, for gated tools, the approval) BEFORE it ever invokes the wrapped function,
a PolicyViolation always prevents the underlying write from running at all -- there is no
partial-write window to protect against. HITL decisions are applied through this registry too
(apply_*_decision), so a disabled agent cannot confirm a booking or assign a buddy either.

`actor_client_id` is derived from `actor.client_id` (finding S1/S2), not accepted as a separate,
independently-settable constructor parameter -- there is exactly one source of tenant identity for
a registry instance, and it comes from the same verified actor that authorizes every tool call.
Construction fails closed if the actor has no verified client_id, and an operation that needs a
connector the deployment did not wire in raises ConnectorNotConfiguredError -- it never falls back
to a fake.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, TypeVar

from connectors.calendar.base import CalendarPort
from connectors.email.base import EmailPort
from connectors.employee_directory.base import EmployeeDirectoryPort
from connectors.notifications.base import (DeliveryReceipt, NotificationChannel, NotificationMessage,
                                           NotificationPort)
from context_layer.knowledge.policy_index import PolicyKnowledgeBase
from hr_onboarding_common.contracts import Actor
from hr_onboarding_common.llm import LLMClient
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import ApprovalItem, Decision, HITLInbox
from hr_onboarding_common.platform.policy import Policy, PolicyViolation
from hr_onboarding_common.platform.tools import ToolGateway
from sub_agent.models import (CommunicationDraft, CommunicationStatus,
                              OnboardingRecord, OnboardingSession, OperationRecord, OperationStatus, SessionStatus,
                              SessionType, TaskStatus)
from sub_agent.repository import OnboardingPort
from sub_agent.services import (checklist, decisions, delivery, intake, joining_trigger, policy_assistant,
                                policy_escalation, scheduling, welcome)
from sub_agent.services.policy_qa import PolicyAnswer, PolicyEscalationRef

AGENT_ID = "employee-onboarding"
C = TypeVar("C")
R = TypeVar("R", OnboardingSession, CommunicationDraft)


class TenantAuthorizationError(PolicyViolation):
    """A request tried to touch another client's data. Always raised before any tool is called."""


class DuplicateSendError(RuntimeError):
    """The same approved HITL decision was already used to send a communication once."""


class InvalidWelcomeContentError(ValueError):
    """The HR-approved welcome content is unusable (missing subject or body). Never sent."""


class ConnectorNotConfiguredError(RuntimeError):
    """An operation needs a connector this deployment has not wired in. Fails closed."""


def utc_now() -> datetime:
    """The default clock. Looked up at call time, so a test can pin it (tests/conftest.py) while a
    deployment always gets the real time."""
    return datetime.now(timezone.utc)


@dataclass
class OnboardingDependencies:
    """Everything the onboarding agent talks to besides the LLM, policy store, HITL inbox and
    audit log that AgentContext already carries. This bundle is what AgentContext.connector holds
    ("the onboarding agent's own connector bundle"), so the LangGraph path and the API's
    registry-backed routes share exactly the same instances.

    `checkpointer` is the LangGraph checkpointer agent.py compiles the workflow with; when None,
    the first OnboardingAgent built from this bundle creates an in-memory one and stores it here so
    every agent instance sharing the bundle shares its threads."""

    repository: OnboardingPort
    calendar: CalendarPort
    kb: PolicyKnowledgeBase
    email: EmailPort | None = None
    directory: EmployeeDirectoryPort | None = None
    notifier: NotificationPort | None = None
    checkpointer: Any = None
    clock: Callable[[], datetime] = lambda: utc_now()  # "now" for scheduling decisions (late-bound, see utc_now)
    run_recorder: Any = None  # supabase mode: sub_agent.db.audit.AgentRunRecorder (agent_runs); else None


def _bind_tenant(component: C, client_id: str) -> C:
    """A tenant-bound view of a component that supports one (the supabase-mode repository,
    directory, event source and audit log: every query then carries this client_id). Components
    without tenant binding -- the in-memory and SQLite fakes -- are returned unchanged."""
    bind = getattr(component, "for_tenant", None)
    return bind(client_id) if callable(bind) else component


class OnboardingToolRegistry:
    def __init__(self, *, port: OnboardingPort, calendar: CalendarPort, kb: PolicyKnowledgeBase, llm: LLMClient,
                 policy: Policy, hitl: HITLInbox, audit: AuditLog, actor: Actor,
                 email_port: EmailPort | None = None, directory: EmployeeDirectoryPort | None = None,
                 notifier: NotificationPort | None = None,
                 gateway: ToolGateway | None = None, clock: Callable[[], datetime] | None = None):
        if actor.client_id is None:
            # Fail closed (finding S2): an actor with no verified tenant must not be able to
            # authorize any tenant-scoped operation through this registry.
            raise TenantAuthorizationError("actor has no verified client_id; cannot authorize any "
                                           "tenant-scoped onboarding operation")
        if gateway is not None and gateway.actor.client_id != actor.client_id:
            raise TenantAuthorizationError("the supplied ToolGateway acts for a different tenant than this registry")
        # Bind tenant-aware stores to the verified tenant (no-op for the local fakes).
        port, audit = _bind_tenant(port, actor.client_id), _bind_tenant(audit, actor.client_id)
        email_port, directory = _bind_tenant(email_port, actor.client_id), _bind_tenant(directory, actor.client_id)
        self.port, self.calendar, self.kb, self.llm = port, calendar, kb, llm
        self.policy, self.hitl, self.audit, self.actor = policy, hitl, audit, actor
        self.email_port, self.directory, self.notifier = email_port, directory, notifier
        self.gateway = gateway or ToolGateway(policy, audit, hitl, actor)
        self.clock = clock or (lambda: utc_now())  # "now" for the not-in-the-past rule

    @classmethod
    def from_dependencies(cls, deps: OnboardingDependencies, *, llm: LLMClient, policy: Policy, hitl: HITLInbox,
                          audit: AuditLog, actor: Actor,
                          gateway: ToolGateway | None = None) -> "OnboardingToolRegistry":
        return cls(port=deps.repository, calendar=deps.calendar, kb=deps.kb, llm=llm, policy=policy, hitl=hitl,
                   audit=audit, actor=actor, email_port=deps.email, directory=deps.directory,
                   notifier=deps.notifier, gateway=gateway, clock=deps.clock)

    @property
    def actor_client_id(self) -> str:
        """The single source of tenant identity for this registry: the verified actor's
        client_id. Not an independently-settable field -- see the module docstring."""
        return self.actor.client_id

    @staticmethod
    def _require(connector: C | None, name: str) -> C:
        if connector is None:
            raise ConnectorNotConfiguredError(f"no {name} connector is configured for this deployment")
        return connector

    # ---- intake (UC steps 1-2): scoped to this registry's own actor_client_id, so an email for
    # another client is rejected the same way any other cross-tenant access would be.
    def process_offer_acceptance_event(self, event_id: str) -> OnboardingRecord:
        email_port, directory = self._require(self.email_port, "email"), self._require(self.directory,
                                                                                         "employee directory")
        return self.gateway.call("intake.process_offer_acceptance_event", intake.process_offer_acceptance_event,
                                 self.port, email_port, directory, self.audit, event_id=event_id,
                                 expected_client_id=self.actor_client_id)

    # ---- tenant authorization (reuses OnboardingPort's own tenant checks wherever one exists)
    def _authorize_employee(self, employee_id: str) -> OnboardingRecord:
        record = self.port.get_onboarding_record(employee_id)
        if record.client_id != self.actor_client_id:
            self.audit.record("policy.denied", actor=self.actor.on_behalf_of, agent=AGENT_ID,
                              subject_id=employee_id, client_id=self.actor_client_id, reason="cross_tenant_access",
                              owning_client_id=record.client_id)
            raise TenantAuthorizationError(f"employee {employee_id} is not accessible to this actor")
        return record

    def _authorize_client(self, client_id: str) -> None:
        if client_id != self.actor_client_id:
            self.audit.record("policy.denied", actor=self.actor.on_behalf_of, agent=AGENT_ID,
                              client_id=self.actor_client_id, reason="cross_tenant_access",
                              requested_client_id=client_id)
            raise TenantAuthorizationError(f"client {client_id} is not accessible to this actor")

    def _owned(self, record: R, kind: str, record_id: str) -> R:
        """Tenant check performed here, in the registry, on a record the repository returned --
        not delegated to the repository's own `client_id=` filter. The OnboardingPort contract asks
        every implementation to honour that filter, but only MockOnboarding is known to; a real
        repository that ignored it must still not leak another tenant's record through this path."""
        if record.client_id != self.actor_client_id:
            self.audit.record("policy.denied", actor=self.actor.on_behalf_of, agent=AGENT_ID,
                              client_id=self.actor_client_id, reason="cross_tenant_access", record_kind=kind,
                              record_id=record_id)
            raise TenantAuthorizationError(f"{kind} {record_id} is not this actor's client's record")
        return record

    def _authorize_decision(self, item: ApprovalItem, proposal_type: str) -> None:
        if item.agent != AGENT_ID or item.proposal_type != proposal_type:
            raise ValueError(f"approval {item.id} is a {item.agent}/{item.proposal_type} item, not {proposal_type}")
        self._authorize_client(item.client_id)

    # ---- reads
    def get_record(self, employee_id: str) -> OnboardingRecord:
        self._authorize_employee(employee_id)
        return self.gateway.call("onboarding.get_record", self.port.get_onboarding_record, employee_id,
                                 subject_id=employee_id)

    def list_tasks(self, employee_id: str):
        self._authorize_employee(employee_id)
        return self.gateway.call("onboarding.list_tasks", self.port.list_tasks, employee_id,
                                 subject_id=employee_id)

    def list_sessions(self, employee_id: str) -> list[OnboardingSession]:
        self._authorize_employee(employee_id)
        return self.gateway.call("onboarding.list_sessions", self.port.list_sessions, employee_id,
                                 subject_id=employee_id)

    def get_session(self, session_id: str) -> OnboardingSession:
        session = self._authorize_session(session_id)
        return self.gateway.call("onboarding.get_session", self.port.get_session, session_id,
                                 client_id=self.actor_client_id, subject_id=session.employee_id)

    def list_communication_drafts(self, employee_id: str) -> list[CommunicationDraft]:
        self._authorize_employee(employee_id)
        return self.gateway.call("onboarding.list_communication_drafts", self.port.list_communication_drafts,
                                 employee_id, subject_id=employee_id)

    # ---- outstanding-work views (UC-05 requirement 6). Each reuses the already-gated,
    # already-tenant-checked list_* read above rather than querying self.port a second time, so
    # these derived views get the exact same enforcement and audit trail as the raw reads.
    def pending_tasks(self, employee_id: str):
        return [t for t in self.list_tasks(employee_id) if t.status == TaskStatus.PENDING]

    def overdue_tasks(self, employee_id: str, *, as_of: date):
        return [t for t in self.pending_tasks(employee_id) if t.due_date is not None and t.due_date < as_of]

    def outstanding_sessions(self, employee_id: str):
        return [s for s in self.list_sessions(employee_id) if s.status == SessionStatus.SCHEDULED]

    def get_client_config(self, client_id: str):
        self._authorize_client(client_id)
        return self.gateway.call("onboarding.get_client_config", self.port.get_client_config, client_id)

    def search_policy(self, question: str, *, as_of: date):
        return self.gateway.call("policy_kb.search", self.kb.search, question, client_id=self.actor_client_id,
                                 as_of=as_of)

    def answer_policy_question(self, question: str, *, as_of: date, employee_id: str | None = None) -> PolicyAnswer:
        """The HR policy assistant (services/policy_assistant.py): deterministic privacy classification
        first, then a grounded answer from the authoritative policy, or a private HR escalation."""
        if employee_id is not None:
            self._authorize_employee(employee_id)
        self.policy.gate(policy_escalation.GATE)  # PolicyViolation up front if escalation is not declared
        is_hr = "hr_admin" in self.actor.roles
        settings = self.policy.settings.get("policy_qa", {})
        return self.gateway.call(
            "policy_qa.answer_question", policy_assistant.handle_policy_question, self.port, self.llm, self.kb,
            self.hitl, self.audit, question=question, client_id=self.actor_client_id, as_of=as_of,
            requester_id=self.actor.on_behalf_of, employee_id=employee_id,
            asks_about_other_employee=bool(employee_id and employee_id != self.actor.on_behalf_of and not is_hr),
            enabled_languages=tuple(settings.get("enabled_languages", ["en"])),
            notify=self.notify_approver if self.notifier is not None else None, subject_id=employee_id)

    def policy_escalation_status(self, escalation_id: str) -> PolicyEscalationRef:
        """Status of one escalation, for HR (hr_admin) or the employee who raised it -- nobody else, and
        never another tenant's (reported as not found). Carries no question text or details."""
        item = self.hitl.get(escalation_id, client_id=self.actor_client_id)  # KeyError: missing or other tenant
        if item.agent != AGENT_ID or item.proposal_type != policy_escalation.PROPOSAL_TYPE:
            raise KeyError(escalation_id)
        if "hr_admin" not in self.actor.roles and item.payload.get("requester_id") != self.actor.on_behalf_of:
            self.audit.record("policy.denied", actor=self.actor.on_behalf_of, agent=AGENT_ID,
                              client_id=self.actor_client_id, reason="escalation_not_owned", approval_id=escalation_id)
            raise KeyError(escalation_id)  # someone else's private HR conversation: reported as not found
        return policy_escalation.to_ref(self.port, item)

    def apply_policy_escalation_decision(self, item: ApprovalItem) -> dict:
        self._authorize_decision(item, policy_escalation.PROPOSAL_TYPE)
        return self.gateway.call("policy_qa.apply_escalation_decision", policy_escalation.apply_escalation_decision,
                                 self.port, self.notifier, self.audit, item,
                                 channel=self._settings_channel("employee_channel", NotificationChannel.EMAIL),
                                 subject_id=item.subject_id)

    # ---- approvals (HITLInbox reads are tenant-scoped in the query itself -- finding S1)
    def get_approval(self, approval_id: str) -> ApprovalItem:
        return self.hitl.get(approval_id, client_id=self.actor_client_id)

    def pending_approvals(self, employee_id: str, *, proposal_type: str | None = None) -> list[ApprovalItem]:
        return [i for i in self.hitl.list(status=Decision.PENDING, client_id=self.actor_client_id)
                if i.agent == AGENT_ID and i.subject_id == employee_id
                and (proposal_type is None or i.proposal_type == proposal_type)]

    # ---- checklist writes
    def ensure_tasks(self, employee_id: str, *, due_date: date | None = None):
        self._authorize_employee(employee_id)
        return self.gateway.call("checklist.ensure_tasks", checklist.ensure_required_tasks, self.port,
                                 self.audit, employee_id=employee_id, client_id=self.actor_client_id,
                                 due_date=due_date, subject_id=employee_id)

    # ---- welcome communications
    def draft_welcome(self, employee_id: str, *, template_id: str, language: str, workflow_instance: str):
        self._authorize_employee(employee_id)
        self.policy.gate(welcome.GATE)  # PolicyViolation up front if the gate isn't declared in policy.yaml
        return self.gateway.call("welcome.draft_communication", welcome.draft_welcome_communication, self.port,
                                 self.llm, self.hitl, self.audit, employee_id=employee_id, template_id=template_id,
                                 language=language, workflow_instance=workflow_instance, subject_id=employee_id)

    def send_welcome_email(self, *, draft_id: str, approval_id: str | None) -> dict:
        """Delivers an HR-approved welcome draft exactly once, through NotificationPort.

        The gateway first requires `approval_id` to be an APPROVED/EDITED item for this employee
        (comms.send_welcome_email is write_requires_approval). The item must then be the welcome
        approval for *this* draft, and the content sent is the content HR approved -- including
        HR's edits for an EDITED decision -- not whatever the stored draft says. The draft is
        marked SENT afterwards, so a replay of the same approval is refused durably
        (DuplicateSendError), not only within one process. Delivery goes through the operation
        ledger (services/delivery.py, key "welcome:<client>:<draft id>"): a retry after a lost
        response reuses the same idempotency key, and a provider that cannot deduplicate is never
        resent to (DeliveryOutcomeUncertainError). With MockNotifier (the only notifier in this
        repository) the result says simulated=True: nothing leaves the process."""
        draft = self._owned(self.port.get_communication_draft(draft_id, client_id=self.actor_client_id),
                            "communication draft", draft_id)
        if draft.status == CommunicationStatus.SENT:
            raise DuplicateSendError(f"draft {draft_id} has already been sent")

        def _send() -> dict:
            notifier = self._require(self.notifier, "notification")
            item = self.hitl.get(approval_id, client_id=self.actor_client_id)
            if item.proposal_type != "welcome_message" or item.payload.get("id") != draft_id:
                raise PolicyViolation(f"approval {approval_id} is not the welcome approval for draft {draft_id}")
            # HR's edit may change only part of the message: merge it over the drafted content.
            approved = {**item.payload, **(item.final_payload or {})}
            if approved.get("id") != draft_id:
                raise PolicyViolation(f"approval {approval_id} was edited to point at another draft")
            if not all(isinstance(approved.get(k), str) and approved[k].strip() for k in ("subject", "body")):
                raise InvalidWelcomeContentError(f"approved content for draft {draft_id} has no subject or body")
            record = self.port.get_onboarding_record(draft.employee_id)
            template = self.port.get_client_config(draft.client_id).templates.get(draft.template_id)
            receipt = delivery.deliver_once(self.port, notifier, NotificationMessage(
                channel=NotificationChannel(template.channel if template else NotificationChannel.EMAIL),
                client_id=draft.client_id, employee_id=draft.employee_id, recipient_id=draft.employee_id,
                recipient_address=record.email, kind="welcome", subject=approved["subject"], body=approved["body"],
                metadata={"draft_id": draft_id, "approval_id": approval_id}),
                key=f"welcome:{draft.client_id}:{draft_id}")
            self.port.update_communication_status(draft_id, CommunicationStatus.SENT)
            self.audit.record("welcome.sent", agent=AGENT_ID, subject_id=draft.employee_id,
                              client_id=draft.client_id, draft_id=draft_id, approval_id=approval_id,
                              delivery_ref=receipt.delivery_ref, simulated=receipt.simulated)
            return {"sent": True, "draft_id": draft_id, "simulated": receipt.simulated}

        return self.gateway.call("comms.send_welcome_email", _send, approval_id=approval_id,
                                 subject_id=draft.employee_id)

    def run_joining_day_trigger(self, *, as_of: date, template_id: str = "welcome"):
        """UC step 9 trigger, scoped to this registry's own actor_client_id: processes every
        ACTIVE case whose start_date == as_of, skipping cancelled/withdrawn cases and anyone
        already drafted. Safe to call repeatedly for the same as_of."""
        self.policy.gate(welcome.GATE)
        return self.gateway.call("joining_trigger.run", joining_trigger.run_joining_day_trigger, self.port,
                                 self.llm, self.hitl, self.audit, client_id=self.actor_client_id, as_of=as_of,
                                 template_id=template_id)

    # ---- scheduling
    def _scheduling_settings(self) -> dict:
        """policy.yaml settings, with the tenant's own timezone, working days and holidays when the
        repository holds an authoritative client calendar (supabase mode). Raises
        ClientCalendarUnavailableError rather than falling back to the policy defaults there."""
        calendar_for = getattr(self.port, "get_client_calendar", None)
        if not callable(calendar_for):
            return self.policy.settings
        return calendar_for(self.actor_client_id).scheduling_settings(self.policy.settings)

    def _approver_role_for(self, session_type: SessionType) -> str:
        return self.policy.settings.get("approver_role_by_session_type", {}).get(
            session_type.value, scheduling.DEFAULT_APPROVER_ROLE[session_type])

    def participants_for(self, employee_id: str, session_type: SessionType) -> list[str]:
        """The configured participants (settings.workflow.session_participants) resolved to the
        people on this employee's record. Used when the workflow drafts a session itself."""
        record = self._authorize_employee(employee_id)
        return scheduling.participants_for(self.policy.settings, record, session_type)

    def find_slot(self, employee_id: str, *, session_type: SessionType, attendees: list[str],
                  earliest: date, not_before: datetime | None = None) -> datetime:
        """The first free slot inside the joining-date window (scheduling.scheduling_window) of the
        employee's CURRENT case record; NoAvailableSlotError if none -- the window is never extended."""
        record = self._authorize_employee(employee_id)
        settings = self._scheduling_settings()
        first, last = scheduling.scheduling_window(settings, record.start_date)
        return self.gateway.call("scheduling.find_slot", scheduling.find_available_slot, self.calendar,
                                 settings, attendees=[employee_id, *attendees],
                                 session_type=session_type, earliest=max(earliest, first), latest=last,
                                 not_before=not_before, subject_id=employee_id)

    def schedule_session(self, employee_id: str, *, session_type: SessionType, start: datetime,
                         attendees: list[str], workflow_instance: str):
        self._authorize_employee(employee_id)
        self.policy.gate(scheduling.GATE)
        return self.gateway.call("scheduling.schedule_session", scheduling.schedule_session, self.port,
                                 self.calendar, self.hitl, self.audit, employee_id=employee_id,
                                 session_type=session_type, start=start, attendees=attendees,
                                 workflow_instance=workflow_instance,
                                 approver_role=self._approver_role_for(session_type),
                                 settings=self._scheduling_settings(), now=self.clock(), subject_id=employee_id)

    def schedule_buddy_intro(self, employee_id: str, *, start: datetime, workflow_instance: str):
        self._authorize_employee(employee_id)
        self.policy.gate(scheduling.GATE)
        return self.gateway.call("scheduling.schedule_buddy_intro", scheduling.schedule_buddy_intro, self.port,
                                 self.calendar, self.hitl, self.audit, employee_id=employee_id, start=start,
                                 workflow_instance=workflow_instance,
                                 approver_role=self._approver_role_for(SessionType.BUDDY_INTRO),
                                 settings=self._scheduling_settings(), now=self.clock(), subject_id=employee_id)

    def propose_buddy_assignment(self, employee_id: str, *, workflow_instance: str):
        self._authorize_employee(employee_id)
        self.policy.gate(scheduling.BUDDY_GATE)
        return self.gateway.call("scheduling.propose_buddy_assignment", scheduling.propose_buddy_assignment,
                                 self.port, self.hitl, self.audit, employee_id=employee_id,
                                 workflow_instance=workflow_instance, subject_id=employee_id)

    def _authorize_session(self, session_id: str):
        """Tenant-checks a session by id, reusing OnboardingPort's own check (TenantMismatchError)
        rather than re-fetching the owning employee record first."""
        return self._owned(self.port.get_session(session_id, client_id=self.actor_client_id), "session", session_id)

    def record_participant_response(self, session_id: str, participant_id: str, response, *,
                                    workflow_instance: str | None = None, responded_at: datetime | None = None):
        session = self._authorize_session(session_id)
        if participant_id != self.actor.on_behalf_of and "hr_admin" not in self.actor.roles:
            self.audit.record("policy.denied", actor=self.actor.on_behalf_of, agent=AGENT_ID,
                              client_id=self.actor_client_id, reason="rsvp_on_behalf_of_another",
                              session_id=session_id)
            raise PermissionError(f"{self.actor.on_behalf_of} may only respond for themselves "
                                  f"(hr_admin may record a response on someone's behalf)")
        return self.gateway.call("scheduling.record_participant_response", scheduling.record_response,
                                 self.port, self.hitl, self.audit, session_id=session_id,
                                 participant_id=participant_id, response=response,
                                 workflow_instance=workflow_instance or session.employee_id,
                                 responded_at=responded_at)

    def list_participant_responses(self, session_id: str):
        self._authorize_session(session_id)
        return self.gateway.call("onboarding.list_participant_responses", self.port.list_participant_responses,
                                 session_id)

    def in_flight_booking(self, employee_id: str, session_type: SessionType) -> OperationRecord | None:
        """A booking of this type whose provider outcome is still unknown (see scheduling.in_flight_booking)."""
        self._authorize_employee(employee_id)
        return scheduling.in_flight_booking(self.port, self.actor_client_id, employee_id, session_type)

    def conflict_superseded_reason(self, item: ApprovalItem) -> str | None:
        self._authorize_decision(item, "attendee_declined")
        self._authorize_session(item.payload["session_id"])
        return scheduling.conflict_superseded_reason(self.port, item)

    def superseded_conflicts(self, session_id: str, participant_id: str) -> list[tuple[ApprovalItem, str]]:
        """Undecided attendee_declined items for this participant that a newer RSVP has made moot."""
        session = self._authorize_session(session_id)
        return [(item, reason) for item in scheduling.pending_conflicts(self.hitl, session, participant_id)
                if (reason := scheduling.conflict_superseded_reason(self.port, item)) is not None]

    def is_attendance_satisfied(self, session_id: str) -> bool:
        session = self._authorize_session(session_id)
        return scheduling.is_attendance_satisfied(self.port, self.policy.settings, session)

    def list_invitations(self, session_id: str):
        self._authorize_session(session_id)
        return self.gateway.call("scheduling.list_invitations", self.port.list_invitations, session_id)

    def check_approval_timeouts(self, *, as_of: datetime):
        """Scoped to this registry's own actor_client_id: a timeout sweep only ever cancels
        bookings belonging to the actor's own tenant. It cancels other people's bookings, so it
        requires hr_admin, and `as_of` must carry a timezone."""
        if "hr_admin" not in self.actor.roles:
            self.audit.record("policy.denied", actor=self.actor.on_behalf_of, agent=AGENT_ID,
                              client_id=self.actor_client_id, reason="missing_role", required_role="hr_admin",
                              tool="scheduling.check_timeouts")
            raise PermissionError(f"{self.actor.on_behalf_of} lacks role 'hr_admin' required for the timeout sweep")
        if as_of.tzinfo is None:
            raise ValueError("as_of must include a timezone")
        return self.gateway.call("scheduling.check_timeouts", scheduling.check_approval_timeouts, self.port,
                                 self.calendar, self.hitl, self.audit, as_of=as_of,
                                 client_id=self.actor_client_id)

    # ---- notifications
    def _settings_channel(self, key: str, default: NotificationChannel) -> NotificationChannel:
        return NotificationChannel(self.policy.settings.get("notifications", {}).get(key, default))

    def notify_approver(self, item: ApprovalItem) -> DeliveryReceipt:
        """Tells whoever must decide `item` that it is waiting, over the configured approval
        channel (settings.notifications.approval_channel: teams | email). The decision itself is
        still made through POST /v1/approvals/{id}/decision; this only delivers the request. A
        team_lead/buddy gate goes to the specific person on record, an hr_admin gate to the
        role's shared queue ("role:hr_admin")."""
        self._authorize_client(item.client_id)
        notifier = self._require(self.notifier, "notification")
        field = scheduling.EXPECTED_APPROVER_FIELD.get(item.approver_role)
        recipient_id = (getattr(self._authorize_employee(item.subject_id), field) if field and item.subject_id
                        else None) or f"role:{item.approver_role}"
        message = NotificationMessage(
            channel=self._settings_channel("approval_channel", NotificationChannel.TEAMS),
            client_id=item.client_id, employee_id=item.subject_id or "", recipient_id=recipient_id,
            kind="approval_request", subject=f"Approval needed: {item.proposal_type.replace('_', ' ')} "
                                             f"for {item.subject_id}",
            body=f"{item.rationale}\n\nReview and decide: /v1/approvals/{item.id}",
            metadata={"approval_id": item.id, "gate": item.gate, "approver_role": item.approver_role})
        receipt = self.gateway.call("notifications.request_approval", delivery.deliver_once, self.port, notifier,
                                    message, key=f"approval-request:{item.client_id}:{item.id}",
                                    subject_id=item.subject_id)
        self.audit.record("notifications.approval_requested", agent=AGENT_ID, subject_id=item.subject_id,
                          client_id=item.client_id, approval_id=item.id, recipient_id=recipient_id,
                          channel=receipt.channel, delivery_ref=receipt.delivery_ref, simulated=receipt.simulated)
        return receipt

    def notify_conflict_superseded(self, item: ApprovalItem, reason: str) -> DeliveryReceipt:
        """Tells the HR queue that an undecided (or just-decided) attendee_declined item no longer
        applies -- e.g. the participant has since accepted -- so nobody acts on it. Once per item."""
        self._authorize_decision(item, "attendee_declined")
        notifier = self._require(self.notifier, "notification")
        message = NotificationMessage(
            channel=self._settings_channel("approval_channel", NotificationChannel.TEAMS),
            client_id=item.client_id, employee_id=item.subject_id or "", recipient_id=f"role:{item.approver_role}",
            kind="conflict_superseded", subject=f"No decision needed: attendance conflict for {item.subject_id}",
            body=f"Approval {item.id} no longer applies ({reason.replace('_', ' ')}). Deciding it changes nothing.",
            metadata={"approval_id": item.id, "reason": reason})
        return self.gateway.call("notifications.conflict_superseded", delivery.deliver_once, self.port, notifier,
                                 message, key=f"conflict-superseded:{item.client_id}:{item.id}",
                                 subject_id=item.subject_id)

    # ---- operations (idempotency ledger) -- hr_admin only
    def _require_hr_admin(self, tool: str) -> None:
        if "hr_admin" not in self.actor.roles:
            self.audit.record("policy.denied", actor=self.actor.on_behalf_of, agent=AGENT_ID,
                              client_id=self.actor_client_id, reason="missing_role", required_role="hr_admin",
                              tool=tool)
            raise PermissionError(f"{self.actor.on_behalf_of} lacks role 'hr_admin' required for {tool}")

    def list_unsettled_operations(self) -> list[OperationRecord]:
        self._require_hr_admin("operations.list")
        return self.gateway.call("operations.list", self.port.list_operations, client_id=self.actor_client_id,
                                 statuses=(OperationStatus.PENDING, OperationStatus.UNCERTAIN))

    def reconciliation_report(self, collect: Callable[[str], dict]) -> dict:
        """`collect(client_id)` gathers the report (sub_agent.reconciliation); this adds the role
        check, kill switch and audit, scoped to the actor's own tenant."""
        self._require_hr_admin("onboarding.reconciliation_report")
        return self.gateway.call("onboarding.reconciliation_report", collect, self.actor_client_id)

    def resolve_operation(self, key: str, *, outcome: OperationStatus, provider_ref: str | None = None,
                          reason: str | None = None) -> OperationRecord:
        """HR's resolution of an uncertain booking or delivery after checking the provider. The
        resolver, time and (optional) reason are kept on the operation and in the audit log."""
        self._require_hr_admin("operations.resolve")
        record = self.gateway.call("operations.resolve", delivery.resolve_operation, self.port, key=key,
                                   client_id=self.actor_client_id, outcome=outcome, provider_ref=provider_ref,
                                   resolved_by=self.actor.on_behalf_of, reason=reason)
        self.audit.record("operations.resolved", actor=self.actor.on_behalf_of, agent=AGENT_ID,
                          client_id=self.actor_client_id, operation_key=key, outcome=outcome, reason=reason)
        return record

    def redeliver_welcome(self, draft_id: str, *, reason: str) -> dict:
        """An operator-authorized new delivery attempt of an HR-approved welcome that is known NOT to
        have been delivered: its delivery operation was resolved as RELEASED (checked: not
        delivered), the provider refused it permanently (REJECTED -- the cause, e.g. the address, has
        been fixed), or it was never attempted. Refused while the outcome is unknown (PENDING/
        UNCERTAIN: resolve it first) and once delivered. The send itself is send_welcome_email, with
        the approval that approved this draft -- so the approved content, the ledger's fresh
        idempotency key and the SENT marking are exactly those of a first delivery. hr_admin only;
        the reason is required and audited."""
        self._require_hr_admin("welcome.redeliver")
        if not reason or not reason.strip():
            raise ValueError("a reason is required to authorize a new delivery attempt")
        draft = self._owned(self.port.get_communication_draft(draft_id, client_id=self.actor_client_id),
                            "communication draft", draft_id)
        if draft.status == CommunicationStatus.SENT:
            raise DuplicateSendError(f"draft {draft_id} has already been sent")
        if draft.status != CommunicationStatus.APPROVED:
            raise ValueError(f"only an approved welcome can be redelivered (draft {draft_id} is {draft.status})")
        op = self.port.get_operation(f"welcome:{draft.client_id}:{draft_id}")
        if op is not None and op.status in (OperationStatus.PENDING, OperationStatus.UNCERTAIN):
            raise delivery.DeliveryOutcomeUncertainError(
                f"the last delivery of draft {draft_id} has an unknown outcome; resolve its operation first")
        if op is not None and op.status == OperationStatus.SUCCEEDED:
            raise DuplicateSendError(f"draft {draft_id} was delivered (operation {op.key} succeeded)")
        approvals = [i for i in self.hitl.list(status=None, client_id=self.actor_client_id)
                     if i.agent == AGENT_ID and i.proposal_type == "welcome_message" and i.is_approved
                     and i.payload.get("id") == draft_id]
        if not approvals:
            raise ValueError(f"no approved welcome approval exists for draft {draft_id}")
        self.audit.record("welcome.redelivery_authorized", actor=self.actor.on_behalf_of, agent=AGENT_ID,
                          client_id=self.actor_client_id, subject_id=draft.employee_id, draft_id=draft_id,
                          approval_id=approvals[-1].id, previous_operation_status=op.status if op else None,
                          reason=reason.strip()[:500])
        return self.send_welcome_email(draft_id=draft_id, approval_id=approvals[-1].id)

    def validate_decision(self, item: ApprovalItem, *, decision: Decision | str, approver: str,
                          edited_payload: dict | None = None) -> None:
        """Pre-checks a decision before it is recorded (services/decisions.py): the specific team lead
        or buddy, applicability, and any edited payload. Tenant-checked first."""
        if item.agent != AGENT_ID:
            return
        self._authorize_client(item.client_id)
        # only a replan (EDITED attendee_declined) validates a meeting time and needs the calendar
        replan = item.proposal_type == "attendee_declined" and Decision(decision) == Decision.EDITED
        decisions.validate_decision(self.port, self._scheduling_settings() if replan else self.policy.settings, item,
                                    decision=Decision(decision),
                                    approver=approver, edited_payload=edited_payload)

    # ---- applying HITL decisions (called by the workflow's approval_decided branch)
    def apply_meeting_decision(self, item: ApprovalItem) -> OnboardingSession:
        self._authorize_decision(item, "session_booking")
        self.gateway.call("scheduling.apply_decision", scheduling.apply_decision, self.port, self.calendar,
                          self.audit, item, settings=self._scheduling_settings(), subject_id=item.subject_id)
        return self._owned(self.port.get_session(item.payload["id"], client_id=self.actor_client_id), "session",
                           item.payload["id"])

    def apply_conflict_decision(self, item: ApprovalItem) -> OnboardingSession | None:
        """Returns the rescheduled session for a 'replan' (EDITED) decision, else None."""
        self._authorize_decision(item, "attendee_declined")
        session = self._authorize_session(item.payload["session_id"])
        return self.gateway.call("scheduling.apply_attendance_decision", scheduling.apply_attendance_decision,
                                 self.port, self.calendar, self.hitl, self.audit, item,
                                 settings=(self._scheduling_settings() if item.status == Decision.EDITED
                                           else self.policy.settings),
                                 approver_role=self._approver_role_for(session.session_type),
                                 now=self.clock(), subject_id=item.subject_id)

    def apply_buddy_decision(self, item: ApprovalItem) -> OnboardingRecord:
        self._authorize_decision(item, "buddy_assignment")
        notifier = self._require(self.notifier, "notification")
        self.gateway.call("scheduling.apply_buddy_decision", scheduling.apply_buddy_decision, self.port, notifier,
                          self.audit, item, channel=self._settings_channel("employee_channel",
                                                                           NotificationChannel.EMAIL),
                          subject_id=item.subject_id)
        return self.port.get_onboarding_record(item.payload["employee_id"])

    def apply_welcome_decision(self, item: ApprovalItem) -> CommunicationDraft:
        self._authorize_decision(item, "welcome_message")
        self.gateway.call("welcome.apply_decision", welcome.apply_decision, self.port, self.audit, item,
                          subject_id=item.subject_id)
        return self._owned(self.port.get_communication_draft(item.payload["id"], client_id=self.actor_client_id),
                           "communication draft", item.payload["id"])
