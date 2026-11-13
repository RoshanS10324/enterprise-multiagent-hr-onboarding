"""The runtime form of the UC-02 flow: what the API and any worker actually run.

:class:`RuntimeFlow` extends :class:`flow.OnboardingFlow` with the pieces a deployment needs and a test harness does not:

* **Stored message bodies.** Every message UC-02 prepares (HR review email, team assignments, document request, HR notices)
  is kept on the onboarding record, so what HR reviewed, what is saved as a draft and what is sent are one fingerprinted
  thing, independent of the clock and of restarts.
* **One entry point per external event**, each authenticated by its own HMAC secret (``channel_secrets``): the UC-01
  employee-created event, document status updates, UC-04/UC-05 status events, and HR's authorisation to send a message.
  Without a secret a channel refuses everything; it never accepts an unauthenticated event.
* **Automatic progression** where no human decision is needed: an employee event produces the review, the submitted plan and
  the HR email draft; an approved plan produces assignments, the document request and an HR notice (all prepared, none sent);
  a document update that opens the gate triggers UC-04 and UC-05 if their adapters are configured.
* **HR notices** for approval, rejection and change requests, and a fresh review email for each revised plan version.

What stays a human decision (never automatic): approving or rejecting the plan, and authorising any message to be sent.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from hashlib import sha256
from typing import Any

from pydantic import BaseModel

from onboarding_task_management.contracts import (
    Actor,
    PlanStatus,
    PreparedMessage,
    Role,
    SourceUseCase,
    utc_now,
)
from onboarding_task_management.errors import (
    InvalidStateError,
    MissingConfigurationError,
    OnboardingError,
    PreconditionError,
    UnauthorizedActionError,
)
from onboarding_task_management.privacy import redact_free_text
from onboarding_task_management.sub_agent.connectors.outlook.base import (
    DeliveryStatus,
    OutlookConnector,
    OutlookDraftConnector,
)
from onboarding_task_management.sub_agent.flow import (
    CALLBACK_MAX_AGE,
    ApprovalOutcome,
    DocumentUpdate,
    DocumentUpdateResult,
    DownstreamStatusEvent,
    EmployeeCreatedEvent,
    FlowResult,
    HrReviewEmail,
    OnboardingFlow,
    TriggerAdapter,
    content_digest,
)

#: Service identities the runtime registers for the authenticated channels (the HMAC secret proves who is speaking).
PRINCIPAL_DOCUMENTS = "svc-uc02-documents"
PRINCIPAL_UC04 = "svc-uc02-uc04"
PRINCIPAL_UC05 = "svc-uc02-uc05"
CHANNELS = ("events", "documents", "uc04", "uc05")


def runtime_principals() -> dict[SourceUseCase, frozenset[str]]:
    """The ``source_principals`` a runtime service must be built with for the signed channels to be accepted."""
    return {SourceUseCase.UC03_DOCUMENT_VALIDATION: frozenset({PRINCIPAL_DOCUMENTS}),
            SourceUseCase.UC04_EQUIPMENT_ACCESS: frozenset({PRINCIPAL_UC04}),
            SourceUseCase.UC05_ORIENTATION: frozenset({PRINCIPAL_UC05})}


def _canonical(model: BaseModel) -> bytes:
    return json.dumps(model.model_dump(mode="json", exclude={"signature"}), sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sign_model(secret: bytes, model: BaseModel):
    """Return ``model`` with its ``signature`` set (what a sender does; used by tests and by teams' clients)."""
    return model.model_copy(update={"signature": hmac.new(secret, _canonical(model), sha256).hexdigest()})


class MessageDecision(BaseModel):
    """HR lead's authorisation to send one prepared message, relayed by the approval connector (signed)."""

    decision_id: str
    client_id: str
    onboarding_id: str
    message_id: str
    approver_id: str
    approver_role: Role
    issued_at: datetime
    signature: str = ""


@dataclass
class ReceiveOutcome:
    status: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class RuntimeFlow(OnboardingFlow):
    def __init__(self, *args, connector: OutlookConnector | None = None,
                 downstream_adapters: Mapping[str, TriggerAdapter] | None = None,
                 channel_secrets: Mapping[str, bytes] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.connector = connector
        self.downstream_adapters = dict(downstream_adapters or {})
        self.channel_secrets = {k: v for k, v in (channel_secrets or {}).items() if v}

    # -- authentication of the signed channels --------------------------------------------------------------------------

    def _authentic(self, channel: str, model: BaseModel) -> bool:
        secret = self.channel_secrets.get(channel)
        if not secret or not getattr(model, "signature", ""):
            return False
        expected = hmac.new(secret, _canonical(model), sha256).hexdigest()
        return hmac.compare_digest(expected, model.signature)

    # -- stored messages -------------------------------------------------------------------------------------------------

    def stored_message(self, actor: Actor, onboarding_id: str, message_id: str) -> PreparedMessage | None:
        record = self.service._load(actor, onboarding_id)
        return next((m for m in record.messages if m.message_id == message_id), None)

    def _store_message(self, actor: Actor, onboarding_id: str, kind: str, message_id: str, subject: str, body: str,
                       role: str, team: str = "") -> PreparedMessage:
        existing = self.stored_message(actor, onboarding_id, message_id)
        if existing is not None:
            return existing  # immutable: the first prepared content is the content that was reviewed
        record = self.service._load(actor, onboarding_id)
        message = PreparedMessage(message_id=message_id, kind=kind, plan_version=record.plan_version, subject=subject,
                                  body=body, recipient_role=role, recipient_team=team,
                                  content_sha256=content_digest(subject, body))
        self._mutate(actor, onboarding_id, lambda r: r.messages.append(message))
        return message

    def prepare_hr_email(self, actor: Actor, onboarding_id: str, as_of: date, analyst=None) -> HrReviewEmail:
        record = self.service._load(actor, onboarding_id)
        stored = self.stored_message(actor, onboarding_id, f"HRM-{onboarding_id}#p{record.plan_version}")
        if stored is not None:
            return HrReviewEmail(email_id=stored.message_id, onboarding_id=onboarding_id, plan_version=stored.plan_version,
                                 recipient_role=Role(stored.recipient_role), subject=stored.subject, body=stored.body)
        email = super().prepare_hr_email(actor, onboarding_id, as_of, analyst)
        self._store_message(actor, onboarding_id, "hrmail", email.email_id, email.subject, email.body,
                            email.recipient_role.value)
        return email

    def prepare_document_request(self, actor: Actor, onboarding_id: str):
        request = super().prepare_document_request(actor, onboarding_id)
        self._store_message(actor, onboarding_id, "docreq", request.request_id, request.subject, request.body,
                            request.recipient_role.value, request.team)
        return request

    def prepare_team_assignments(self, actor: Actor, onboarding_id: str):
        assignments = super().prepare_team_assignments(actor, onboarding_id)
        for a in assignments:
            subject, body, role = self.assignment_message(actor, onboarding_id, a.assignment_id)
            self._store_message(actor, onboarding_id, "teamasg", a.assignment_id, subject, body, role.value, a.team)
        return assignments

    # -- notices to HR ---------------------------------------------------------------------------------------------------------

    def prepare_decision_notice(self, actor: Actor, onboarding_id: str, outcome: ApprovalOutcome) -> PreparedMessage | None:
        record = self.service._load(actor, onboarding_id)
        version = outcome.plan_version or record.plan_version
        lines = [f"Onboarding {onboarding_id} (employee {record.employee_id}), plan version {version}."]
        if outcome.status == "approved":
            current = [a for a in record.assignments if a.plan_version == record.plan_version and a.status != "superseded"]
            lines += ["HR approved the plan. Prepared, NOT sent: "
                      f"{len(current)} team assignment(s) and the document collection request.",
                      *[f"- {a.team} ({a.owner_role}{', ' + a.use_case if a.use_case else ''}): {len(a.tasks)} task(s)"
                        + (" [held until documents are verified]" if a.hold_reason else "") for a in current],
                      "Each message still needs an HR lead's authorisation before anything is sent."]
        elif outcome.status == "rejected":
            lines += ["HR rejected the plan.", f"Reason recorded: {redact_free_text(outcome.detail)}",
                      "No task was dispatched, no document request was prepared, and UC-04 / UC-05 were not triggered.",
                      "The plan must be revised and resubmitted before anything continues; this is intentional (a rejection "
                      "is a decision, not a request to retry)."]
        elif outcome.status == "changes_requested":
            lines += ["HR requested changes.", f"Reason recorded: {redact_free_text(outcome.detail)}",
                      f"A revised plan (version {record.plan_version}) was created and submitted as approval request "
                      f"{outcome.new_request_id}. A new review email is prepared; nothing was dispatched for the earlier version."]
        else:
            return None
        message_id = f"HRN-{onboarding_id}#p{version}-{outcome.status}"
        subject = f"UC-02 decision recorded: {outcome.status.replace('_', ' ')} (plan v{version}, {record.employee_id})"
        body = "\n".join(lines)
        if not self._events(actor, onboarding_id, "hrnotice", message_id):
            self._audit(actor, "hrnotice.prepared", "draft_not_sent", onboarding_id, message_id=message_id,
                        prepared_by=actor.actor_id, content_sha256=content_digest(subject, body))
        return self._store_message(actor, onboarding_id, "hrnotice", message_id, subject, body, Role.HR_OFFICER.value)

    def on_decision(self, actor: Actor, onboarding_id: str, outcome: ApprovalOutcome) -> None:
        self._record_run(actor, onboarding_id, "decision", outcome.status + "-" + str(outcome.plan_version or ""),
                         "acted", tools=("process_approval_callback",))
        self.prepare_decision_notice(actor, onboarding_id, outcome)
        if outcome.status == "changes_requested":
            self.prepare_hr_email(actor, onboarding_id, utc_now().date())  # the revised version needs its own review email

    # -- sending a stored message ---------------------------------------------------------------------------------------------------

    def save_prepared_draft(self, actor: Actor, onboarding_id: str, message_id: str,
                            connector: OutlookConnector | None = None) -> str:
        connector = connector or self.connector
        stored = self._require_stored(actor, onboarding_id, message_id)
        if connector is None:
            raise InvalidStateError("no Outlook connector is configured", message_id=message_id)
        if stored.kind == "teamasg":
            a = self._assignment(actor, onboarding_id, message_id)
            return self.save_draft_message(actor, onboarding_id, "teamasg", message_id, stored.subject, stored.body,
                                           Role(stored.recipient_role), connector, recipient_team=a.team,
                                           role_shared=self._role_shared(actor, onboarding_id, a))
        return self.save_draft_message(actor, onboarding_id, stored.kind, message_id, stored.subject, stored.body,
                                       Role(stored.recipient_role), connector, recipient_team=stored.recipient_team)

    def _require_stored(self, actor: Actor, onboarding_id: str, message_id: str) -> PreparedMessage:
        stored = self.stored_message(actor, onboarding_id, message_id)
        if stored is None:
            raise InvalidStateError("no such prepared message", message_id=message_id)
        return stored

    def process_message_decision(self, decision: MessageDecision, now: datetime) -> ReceiveOutcome:
        """HR's signed authorisation. Authorises the message and sends it through the connector, holding where required."""
        probe = decision.model_copy(update={"signature": decision.signature})
        secret = self.callback_secret_bytes()
        expected = hmac.new(secret, _canonical(probe), sha256).hexdigest()
        if not decision.client_id or not hmac.compare_digest(expected, decision.signature or ""):
            return ReceiveOutcome("invalid_signature", "decision is not authentic")
        if abs(now - decision.issued_at) > CALLBACK_MAX_AGE:
            return ReceiveOutcome("expired", "decision is too old (or dated in the future)")
        with self._guard(decision.onboarding_id):
            return self._process_message_decision(decision)

    def _process_message_decision(self, decision: MessageDecision) -> ReceiveOutcome:
        actor = self._actor(decision.client_id)
        approver = Actor(actor_id=decision.approver_id, role=decision.approver_role, client_id=decision.client_id)
        try:
            record = self.service._load(actor, decision.onboarding_id)
        except OnboardingError:
            return ReceiveOutcome("unknown_request", "no such onboarding for this client")
        key = f"md:{decision.decision_id}"
        if key in record.flow_ledger:
            return ReceiveOutcome("replayed", f"already processed as {record.flow_ledger[key]}")
        if self.connector is None:
            return ReceiveOutcome("connector_not_configured", "no Outlook connector is configured; nothing was sent")
        stored = self.stored_message(actor, decision.onboarding_id, decision.message_id)
        if stored is None:
            return ReceiveOutcome("unknown_request", "no such prepared message")
        status = "failed"
        try:
            if stored.kind == "teamasg":
                self.authorise_assignment(approver, decision.onboarding_id, stored.message_id)
                status = self.send_assignment(actor, decision.onboarding_id, stored.message_id, self.connector)
            else:
                self.authorise_message(approver, decision.onboarding_id, stored.kind, stored.message_id)
                status = self.send_message(actor, decision.onboarding_id, stored.kind, stored.message_id, stored.subject,
                                           stored.body, Role(stored.recipient_role), self.connector,
                                           recipient_team=stored.recipient_team)
        except UnauthorizedActionError as exc:
            return ReceiveOutcome("unauthorized", exc.message)
        except InvalidStateError as exc:
            return ReceiveOutcome("held" if "held" in exc.message or "UC-0" in exc.message else "rejected", exc.message)
        self._mutate(actor, decision.onboarding_id, lambda r: r.flow_ledger.__setitem__(key, status))
        return ReceiveOutcome("sent" if status != DeliveryStatus.FAILED.value else "failed",
                              "handed to the connector; delivery is not confirmed" if status != "failed" else
                              "the connector did not accept the message", {"connector_status": status})

    def callback_secret_bytes(self) -> bytes:
        return self._secret

    # -- entry points for the external events -----------------------------------------------------------------------------------------

    def process_employee_event(self, event: EmployeeCreatedEvent, as_of: date) -> FlowResult:
        """UC-01 employee created -> onboarding, review, submitted plan and HR email draft. Idempotent per employee."""
        result = self.handle_employee_created(event, as_of)
        actor = self._actor(event.client_id)
        oid = result.onboarding_id
        ledger = f"ev:{event.event_id}"
        if not result.created:
            if ledger not in self.service._load(actor, oid).flow_ledger:
                self._mutate(actor, oid, lambda r: r.flow_ledger.__setitem__(ledger, "duplicate"))
                self._record_run(actor, oid, "event", event.event_id, "acted", actions=0, tools=("duplicate_event",))
            return result
        self._mutate(actor, oid, lambda r: r.flow_ledger.__setitem__(ledger, "accepted"))
        self.generate_review(actor, oid, as_of)
        request = self.service.submit_plan(actor, oid)
        email = self.prepare_hr_email(actor, oid, as_of)
        result.notes.append(f"plan submitted as request {request.request_id}")
        if isinstance(self.connector, OutlookDraftConnector):
            outcome = self.save_prepared_draft(actor, oid, email.email_id)
            result.notes.append(f"HR review email draft: {outcome}")
        else:
            result.notes.append("HR review email prepared (no Outlook connector configured)")
        self._record_run(actor, oid, "event", event.event_id, "acted",
                         tools=("get_configuration", "generate_checklist", "review", "submit_plan", "prepare_hr_email"))
        return result

    def receive_employee_event(self, event: EmployeeCreatedEvent, as_of: date) -> ReceiveOutcome:
        if not self._authentic("events", event):
            return ReceiveOutcome("invalid_signature" if self.channel_secrets.get("events") else "not_configured",
                                  "the event is not authentic" if self.channel_secrets.get("events")
                                  else "no event secret is configured; events are refused")
        try:
            result = self.process_employee_event(event, as_of)
        except PreconditionError as exc:
            return ReceiveOutcome("rejected", exc.message)
        except MissingConfigurationError as exc:  # no approved configuration (or a gap in it): reported, never defaulted
            self._audit(self._actor(event.client_id), "flow.event_rejected", "configuration_gap", None,
                        event_id=event.event_id)
            return ReceiveOutcome("configuration_gap", exc.message)
        return ReceiveOutcome("accepted" if result.created else "duplicate", result.detail,
                              {"onboarding_id": result.onboarding_id, "created": result.created, "notes": result.notes})

    def receive_document_update(self, update: DocumentUpdate) -> ReceiveOutcome:
        if not self._authentic("documents", update):
            return ReceiveOutcome("invalid_signature" if self.channel_secrets.get("documents") else "not_configured",
                                  "the update is not authentic or no document secret is configured")
        source = Actor(actor_id=PRINCIPAL_DOCUMENTS, role=Role.SYSTEM, client_id=update.client_id)
        result = self.apply_document_update(source, update)
        return ReceiveOutcome("applied" if result.accepted else "rejected", result.detail,
                              {"code": result.code, "duplicate_replay": result.duplicate_replay})

    def apply_document_update(self, source_actor: Actor, update: DocumentUpdate) -> DocumentUpdateResult:
        result = super().apply_document_update(source_actor, update)
        if result.accepted and not result.duplicate_replay:
            # With no adapter configured the gate opening is still recorded: the use cases become NOT CONFIGURED.
            actor = self._actor(update.client_id)
            if self.document_gate(actor, update.onboarding_id).open:
                self.trigger_downstream(actor, update.onboarding_id, self.downstream_adapters)
        return result

    def receive_downstream_status(self, event: DownstreamStatusEvent) -> ReceiveOutcome:
        channel = {"UC-04": "uc04", "UC-05": "uc05"}.get(event.use_case, "")
        if not channel or not self._authentic(channel, event):
            return ReceiveOutcome("invalid_signature" if self.channel_secrets.get(channel) else "not_configured",
                                  "the event is not authentic or no secret is configured for that use case")
        principal = PRINCIPAL_UC04 if channel == "uc04" else PRINCIPAL_UC05
        source = Actor(actor_id=principal, role=Role.SYSTEM, client_id=event.client_id)
        result = self.apply_downstream_status(source, event)
        return ReceiveOutcome("applied" if result.accepted else "rejected", result.detail,
                              {"code": result.code, "duplicate_replay": result.duplicate_replay})

    # -- persisted run records, review metadata and workflow status ------------------------------------------------------------

    def _record_run(self, actor: Actor, onboarding_id: str, stage: str, key: str, status: str, *, actions: int = 1,
                    exceptions: int = 0, tools: tuple[str, ...] = ()) -> None:
        """One ``agent_runs`` row per workflow stage, keyed so a replay writes the same row. No-op without a store."""
        store = self.service.store
        if store is None:
            return
        row = {"run_id": f"FLOW-{onboarding_id}-{stage}-{key}"[:200], "agent_id": "uc02-flow", "agent_version": "1.0.0",
               "client_id": actor.client_id, "onboarding_id": onboarding_id, "actor_id": actor.actor_id,
               "status": status, "advisor": "offline", "model": "fake/uc02-analyst-v1", "proposal_count": 0,
               "action_count": actions, "exception_count": exceptions, "tool_calls": list(tools),
               "finished_at": utc_now().isoformat()}
        store.save_agent_run(row)

    def generate_review(self, actor: Actor, onboarding_id: str, as_of: date, analyst=None) -> dict[str, Any]:
        pkg = super().generate_review(actor, onboarding_id, as_of, analyst)
        record, analysis = pkg["record"], pkg["analysis"]
        key = f"p{record.plan_version}"
        if key not in record.analyses:
            data = analysis.model_dump(mode="json")
            self._mutate(actor, onboarding_id, lambda r: r.analyses.__setitem__(key, data))
            self._audit(actor, "review.generated", "degraded" if analysis.degraded else "ok", onboarding_id,
                        plan_version=str(record.plan_version), advisor=analysis.source,
                        config_version=pkg["config_version"], tasks=str(len(record.tasks)),
                        data_origin=record.data_origin or "real")
        return pkg

    def workflow_status(self, actor: Actor, onboarding_id: str) -> dict[str, Any]:
        """Where the workflow is and why it is not further. Derived from persisted state; "complete" only when it is."""
        record = self.service._load(actor, onboarding_id)
        counts = {s: sum(1 for t in record.tasks if t.status.value == s)
                  for s in ("blocked", "ready", "in_progress", "complete", "cancelled")}
        track = {uc: t.state.value for uc, t in record.downstream.items()}
        gate = self.document_gate(actor, onboarding_id) if record.plan_status is PlanStatus.APPROVED else None
        blocked: list[str] = []
        if record.plan_status is PlanStatus.PROPOSED:
            stage = "plan_generated"
        elif record.plan_status is PlanStatus.SUBMITTED:
            stage, blocked = "awaiting_hr_decision", ["HR has not decided on the plan"]
        elif record.plan_status is PlanStatus.REJECTED:
            stage, blocked = "rejected", ["the plan was rejected; nothing is dispatched until it is revised and approved"]
        elif not gate.open:
            stage, blocked = "awaiting_documents", gate.reasons
        elif any(s in ("not_configured", "waiting_for_documents") for s in track.values()) or not track:
            stage = "gate_open"
            blocked = [f"{uc} trigger is {s}" for uc, s in track.items() if s in ("not_configured", "waiting_for_documents")]
        elif all(s == "completed" for s in track.values()) and counts["blocked"] + counts["ready"] + counts["in_progress"] == 0:
            stage = "complete"
        else:
            stage = "in_progress"
            blocked = [f"{uc} is {s}" for uc, s in track.items() if s in ("failed", "blocked", "delayed",
                                                                         "requires_intervention")]
        return {"onboarding_id": onboarding_id, "stage": stage, "blocked_because": blocked, "plan_status": record.plan_status.value,
                "plan_version": record.plan_version, "config_version": record.config_version,
                "data_origin": record.data_origin or "real", "tasks": counts,
                "documents": {"mandatory": gate.mandatory_total if gate else 0,
                              "verified": gate.mandatory_verified if gate else 0},
                "assignments": len([a for a in record.assignments if a.status != "superseded"]),
                "downstream": track, "messages": len(record.messages)}

    def approval_ready(self, actor: Actor, onboarding_id: str) -> bool:
        return self.service._load(actor, onboarding_id).plan_status is PlanStatus.APPROVED


__all__ = ["CHANNELS", "MessageDecision", "PRINCIPAL_DOCUMENTS", "PRINCIPAL_UC04", "PRINCIPAL_UC05", "ReceiveOutcome",
           "RuntimeFlow", "runtime_principals", "sign_model"]
