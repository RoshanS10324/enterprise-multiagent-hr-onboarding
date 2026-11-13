"""The UC-02 integration flow: the order of events from the UC-01 trigger to independent UC-04 / UC-05 tracking.

    UC-01 event -> validate + retrieve employee -> approved configuration -> checklist + document requirements
    -> Fake LLM review -> HR email -> authenticated HR callback -> approved plan stored -> document request to the
    Document Collection Team -> document status intake -> MANDATORY DOCUMENT GATE -> UC-04 and UC-05 triggered
    independently and in parallel -> downstream status tracking.

This module is UC-02's *integration layer* only. It owns no other use case's business logic:
* UC-01 creates the employee record; UC-02 only reads it through an :class:`EmployeeSource`.
* The Document Collection Team / UC-03 collect and verify documents; UC-02 lists what is required and records the
  reported status.
* UC-04 and UC-05 execute their own work; UC-02 sends a trigger through a :class:`TriggerAdapter` and records what
  they report back.

Every external seam is a Protocol with a clearly labelled MOCK in tests. Nothing here sends a real email, calls a real
use case or claims a delivery that a connector did not confirm. All workflow state lives on the onboarding record
(documents, downstream tracks, idempotency ledger), so the existing store - SQLite or the Supabase
``UC02_CASE_STATE`` snapshot - persists and recovers it with no schema change.
"""

from __future__ import annotations

import hmac
import threading
from collections import defaultdict
from collections.abc import Mapping
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from onboarding_task_management.agent import analysis as analysis_module
from onboarding_task_management.contracts import (
    OPEN_STATUSES,
    Actor,
    AssignmentTask,
    DecisionOutcome,
    DocumentItem,
    DocumentState,
    DownstreamState,
    DownstreamTrack,
    OnboardingRecord,
    PlanStatus,
    Role,
    SourceUseCase,
    TaskCategory,
    TeamAssignment,
    utc_now,
)
from onboarding_task_management.privacy import redact_free_text
from onboarding_task_management.errors import (
    DuplicateOnboardingError,
    InvalidStateError,
    MissingConfigurationError,
    PreconditionError,
    UnauthorizedActionError,
)
from onboarding_task_management.sub_agent.connectors.outlook.base import (
    DeliveryStatus,
    OutlookConnector,
    OutlookConnectorError,
    OutlookDraftConnector,
    OutlookMessage,
)
from onboarding_task_management.sub_agent.service import OnboardingService, OnboardingTrigger
from onboarding_task_management.synthetic import accepted_offer, approved_form  # noqa: F401  (kept for fixtures)

UC04, UC05 = "UC-04", "UC-05"
DOWNSTREAM = (UC04, UC05)
SOURCE_FOR = {UC04: SourceUseCase.UC04_EQUIPMENT_ACCESS, UC05: SourceUseCase.UC05_ORIENTATION}
DOCUMENT_SOURCE = SourceUseCase.UC03_DOCUMENT_VALIDATION

APPROVAL_TTL = timedelta(hours=72)
CALLBACK_MAX_AGE = timedelta(minutes=15)
EVENT_DELAY_AUDIT_AFTER = timedelta(hours=24)
MAX_TRIGGER_ATTEMPTS = 3
MAX_MESSAGE_SEND_ATTEMPTS = 3
#: A message counts as handed over when the connector reports one of these. None of them means "delivered".
_HANDED_OVER = frozenset({DeliveryStatus.SIMULATED_SENT.value, DeliveryStatus.SUBMITTED.value,
                          DeliveryStatus.DUPLICATE_IGNORED.value})

_DOC_TRANSITIONS: dict[DocumentState, set[DocumentState]] = {
    DocumentState.PENDING: {DocumentState.SUBMITTED, DocumentState.NOT_APPLICABLE},
    DocumentState.SUBMITTED: {DocumentState.UNDER_REVIEW, DocumentState.REJECTED, DocumentState.VERIFIED},
    DocumentState.UNDER_REVIEW: {DocumentState.VERIFIED, DocumentState.REJECTED,
                                 DocumentState.RESUBMISSION_REQUIRED},
    DocumentState.REJECTED: {DocumentState.RESUBMISSION_REQUIRED, DocumentState.SUBMITTED},
    DocumentState.RESUBMISSION_REQUIRED: {DocumentState.SUBMITTED},
    DocumentState.VERIFIED: set(),  # settled: a verified document is only changed by a new plan version
    DocumentState.NOT_APPLICABLE: set(),
}
_OPEN_DOWNSTREAM = {DownstreamState.TRIGGERED, DownstreamState.ACCEPTED, DownstreamState.IN_PROGRESS,
                    DownstreamState.COMPLETED}
_REPORTABLE = {DownstreamState.ACCEPTED, DownstreamState.IN_PROGRESS, DownstreamState.COMPLETED,
               DownstreamState.BLOCKED, DownstreamState.FAILED, DownstreamState.DELAYED,
               DownstreamState.REQUIRES_INTERVENTION}


# ---------------------------------------------------------------------------
# Stage 1: the UC-01 event and the employee source
# ---------------------------------------------------------------------------


class EmployeeCreatedEvent(BaseModel):
    """What UC-01 emits when it has created an employee record. Identifiers and a date only."""

    event_id: str
    client_id: str
    employee_id: str
    case_id: str
    joining_date: date
    occurred_at: datetime
    correlation_id: str | None = None
    signature: str = ""


class EmployeeView(BaseModel):
    """The employee facts UC-02 needs, read from the authorised source. No name, email or contact detail."""

    client_id: str
    employee_id: str
    case_id: str
    role_id: str
    grade: str = ""
    department: str = "General"
    manager_id: str
    location: str
    project: str
    nationality_group: str = ""
    attributes: tuple[str, ...] = ()
    joining_date: date
    current_location_status: str = ""
    active: bool = True
    #: Facts UC-01 owns. Absent = UC-01 has not supplied them, and UC-02 will not invent them.
    offer_accepted_at: datetime | None = None
    form_approved_at: datetime | None = None
    form_approved_by_role: Role | None = None


@runtime_checkable
class EmployeeSource(Protocol):
    def get(self, client_id: str, case_id: str) -> EmployeeView | None: ...


class SupabaseEmployeeSource:
    """Read-only: the UC-01 case from the shared database, non-personal columns, plus the approved-form facts."""

    def __init__(self, gateway) -> None:
        self.gateway = gateway

    def _offer_accepted_at(self, client_id: str, form_id: str | None) -> datetime | None:
        """UC-01 links an offer to a form through ``offer_forms``. Only an ACCEPTED offer with a timestamp counts."""
        if not form_id:
            return None
        rest = self.gateway.client
        for link in rest.select("offer_forms", {"client_id": client_id, "form_id": form_id}, columns="offer_ref"):
            for offer in rest.select("offers", {"client_id": client_id, "offer_ref": link["offer_ref"]},
                                     columns="status,accepted_at"):
                if offer.get("status") == "ACCEPTED" and offer.get("accepted_at"):
                    return datetime.fromisoformat(offer["accepted_at"])
        return None

    def get(self, client_id: str, case_id: str) -> EmployeeView | None:
        case = self.gateway.get_case(client_id, case_id)
        if case is None:
            return None
        forms = self.gateway.client.select(
            "onboarding_forms", {"case_id": case_id, "client_id": client_id},
            columns="form_id,status,approved_at,approved_by", order_by="version.desc", limit=1)
        form = forms[0] if forms else {}
        approved = form.get("status") == "APPROVED" and form.get("approved_at")
        offer_accepted = self._offer_accepted_at(client_id, form.get("form_id")) if form else None
        return EmployeeView(
            client_id=case["client_id"], employee_id=case["employee_no"] or "", case_id=case["case_id"],
            role_id=case["role_id"], grade=case["grade"], department=case["department"],
            manager_id=case["manager_id"], location=case["location_name"], project=case["project_name"],
            nationality_group=case["nationality_group"], attributes=tuple(case["onboarding_attributes"] or ()),
            joining_date=date.fromisoformat(case["joining_date"]),
            active=case["status"] not in ("CANCELLED", "WITHDRAWN"),
            offer_accepted_at=offer_accepted,
            form_approved_at=datetime.fromisoformat(form["approved_at"]) if approved else None,
            form_approved_by_role=Role.HR_OPS if approved else None,  # the role is not stored; see the docs
        )


# ---------------------------------------------------------------------------
# Models for the HR callback, document updates, triggers and downstream status
# ---------------------------------------------------------------------------


class ApprovalCallback(BaseModel):
    """HR's decision as relayed by the approval connector. ``signature`` proves the connector vouches for it."""

    callback_id: str
    onboarding_id: str
    request_id: str
    plan_version: int
    decision: Literal["approved", "rejected", "changes_requested"]
    approver_id: str
    approver_role: Role
    issued_at: datetime
    reason: str = ""
    #: The client the approval is for. It is part of what is signed, so it can be trusted once the signature checks.
    client_id: str = ""
    signature: str = ""

    def canonical(self) -> bytes:
        return "|".join([self.callback_id, self.onboarding_id, self.request_id, str(self.plan_version), self.decision,
                         self.approver_id, self.approver_role.value, self.issued_at.isoformat(),
                         self.reason, self.client_id]).encode("utf-8")


def sign_callback(secret: bytes, callback: ApprovalCallback) -> ApprovalCallback:
    return callback.model_copy(update={"signature": hmac.new(secret, callback.canonical(), sha256).hexdigest()})


class ApprovalOutcome(BaseModel):
    status: str
    detail: str = ""
    onboarding_id: str | None = None
    plan_version: int | None = None
    new_request_id: str | None = None


class DocumentUpdate(BaseModel):
    """A document's status as reported by the document collection / verification process."""

    update_id: str
    client_id: str
    onboarding_id: str
    plan_version: int
    document_key: str
    state: DocumentState
    occurred_at: datetime
    reference: str | None = None
    signature: str = ""


class DocumentUpdateResult(BaseModel):
    update_id: str
    accepted: bool
    code: str
    detail: str = ""
    duplicate_replay: bool = False


class GateStatus(BaseModel):
    open: bool
    reasons: list[str] = Field(default_factory=list)
    mandatory_total: int = 0
    mandatory_verified: int = 0
    pending: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    resubmission_required: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class TriggerResult:
    accepted: bool
    reference: str = ""
    error: str = ""


@runtime_checkable
class TriggerAdapter(Protocol):
    use_case: str

    def send(self, payload: dict[str, Any]) -> TriggerResult: ...


class MockTrigger:
    """MOCK downstream trigger for tests. It records payloads and calls nothing real."""

    def __init__(self, use_case: str, *, fail: int = 0, raise_error: bool = False) -> None:
        self.use_case, self.fail, self.raise_error = use_case, fail, raise_error
        self.payloads: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> TriggerResult:
        self.payloads.append(payload)
        if self.raise_error:
            raise ConnectionError("synthetic transport failure")
        if len(self.payloads) <= self.fail:
            return TriggerResult(False, error="synthetic rejection")
        return TriggerResult(True, reference=f"MOCK-{self.use_case}-{len(self.payloads)}")


class DownstreamStatusEvent(BaseModel):
    event_id: str
    client_id: str
    use_case: str
    onboarding_id: str
    plan_version: int
    state: DownstreamState
    occurred_at: datetime
    correlation_id: str | None = None
    reference: str | None = None
    signature: str = ""


class HrReviewEmail(BaseModel):
    email_id: str
    onboarding_id: str
    plan_version: int
    recipient_role: Role
    subject: str
    body: str
    #: Always a draft until a connector confirms otherwise; preparing it is never an approval.
    delivery_status: str = "draft_not_sent"


class DocumentRequest(BaseModel):
    request_id: str
    onboarding_id: str
    plan_version: int
    team: str
    use_case: str
    recipient_role: Role
    due_date: date
    documents: list[dict[str, Any]]
    body: str
    subject: str = ""
    delivery_status: str = "draft_not_sent"


def assignment_id_for(onboarding_id: str, team: str, owner_role: str, plan_version: int, use_case: str = "") -> str:
    """Deterministic, so preparing twice yields the same id. The use case is part of it: one team and role can do
    work for different use cases, and those stay separate assignments."""
    slug = "".join(c if c.isalnum() else "-" for c in team.lower()).strip("-")
    suffix = f"-{use_case.lower()}" if use_case else ""
    return f"ASG-{onboarding_id}#{slug}-{owner_role}{suffix}#p{plan_version}"


def assignment_instructions(use_case: str) -> str:
    if use_case in DOWNSTREAM:
        return (f"{use_case} executes this work after UC-02 triggers it once every mandatory document is verified; "
                "report status to UC-02 through the authenticated status contract.")
    return ("Complete the task and report its status to UC-02 through the authenticated status contract. "
            "Do not mark it complete without the evidence the client's configuration requires.")


def assignment_message(record: OnboardingRecord, a: TeamAssignment) -> tuple[str, str]:
    """One team's message: only that team's tasks. Identifiers and dates; no personal data and no address."""
    lines = [
        f"Task assignment for {a.team} (owner role {a.owner_role}).",
        f"Employee {record.employee_id}, onboarding case {record.case_id or record.onboarding_id}, "
        f"plan version {a.plan_version}, joining date {record.joining_date.isoformat()}, "
        f"client {record.client_id}, configuration {record.config_version or 'unspecified'}.",
    ]
    if a.use_case:
        lines.append(f"Executed by use case: {a.use_case}.")
    if a.hold_reason:
        lines.append(f"HOLD: {a.hold_reason}.")
    lines.append(f"{len(a.tasks)} task(s):")
    for t in a.tasks:
        waits = ", ".join(t.depends_on_keys) or "none"
        lines.append(f"- {t.task_id}: {t.template_key} ({t.title}) | due {t.due_date.isoformat()} | priority {t.priority}"
                     f" | waits on: {waits}")
        lines.append(f"  instructions: {t.instructions}")
    scope = f"{a.owner_role}, {a.use_case}" if a.use_case else a.owner_role
    return f"Task assignment: {a.team} [{scope}] - {record.employee_id} (plan v{a.plan_version})", "\n".join(lines)


def content_digest(subject: str, body: str) -> str:
    """Fingerprint of the prepared message. Stored at preparation, checked at draft-save and send time."""
    return sha256(f"{subject}\n{body}".encode("utf-8")).hexdigest()


@dataclass
class FlowResult:
    onboarding_id: str
    created: bool
    detail: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


class OnboardingFlow:
    def __init__(self, service: OnboardingService, employee_source: EmployeeSource, *, coordinator: Actor,
                 callback_secret: bytes, approval_ttl: timedelta = APPROVAL_TTL,
                 approval_callback_url: str | None = None) -> None:
        if not callback_secret:
            raise ValueError("a callback secret is required; unauthenticated approvals are not supported")
        self.service = service
        self.source = employee_source
        self.coordinator = coordinator
        self._secret = callback_secret
        self.approval_ttl = approval_ttl
        #: Where the approval connector should post a signed decision (supplied by the deployment, shown to HR as text).
        #: UC-02 cannot turn an email click into a signed callback; a connector or portal must do that.
        self.approval_callback_url = approval_callback_url
        # One lock per onboarding: two concurrent callers (a duplicated event, a retried callback) must not both
        # read "not triggered" and both send. In-process only: separate processes need the downstream side (or a
        # database constraint on ``trigger_id``) to deduplicate as well - see the readiness report.
        self._locks: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)
        self._locks_guard = threading.Lock()

    @contextmanager
    def _guard(self, onboarding_id: str):
        with self._locks_guard:
            lock = self._locks[onboarding_id]
        with lock:
            yield

    # -- helpers ----------------------------------------------------------------------------------------------

    def _actor(self, client_id: str) -> Actor:
        return Actor(actor_id=self.coordinator.actor_id, role=self.coordinator.role, client_id=client_id)

    def _mutate(self, actor: Actor, onboarding_id: str, change) -> OnboardingRecord:
        record = self.service._load(actor, onboarding_id)
        backup = record.model_copy(deep=True)
        change(record)
        record.updated_at = utc_now()  # state snapshots are ordered by this, so every change must advance it
        self.service._commit(record, backup)
        return record

    def _audit(self, actor: Actor, action: str, outcome: str, onboarding_id: str | None, **detail: object) -> None:
        self.service.audit.record(actor, action, outcome, onboarding_id=onboarding_id, **detail)

    # -- Stage 1: trigger and employee retrieval ----------------------------------------------------------------------

    def handle_employee_created(self, event: EmployeeCreatedEvent, as_of: date) -> FlowResult:
        actor = self._actor(event.client_id)
        for name in ("event_id", "client_id", "employee_id", "case_id"):
            if not str(getattr(event, name)).strip():
                self._audit(actor, "flow.event_rejected", "invalid", None, field=name)
                raise PreconditionError("the UC-01 event is missing a required identifier", field=name)
        if event.joining_date < as_of:
            self._audit(actor, "flow.event_rejected", "joining_date_passed", None, event_id=event.event_id)
            raise PreconditionError("the joining date has already passed", joining_date=event.joining_date.isoformat())
        if self.source is None:
            self._audit(actor, "flow.event_rejected", "no_employee_source", None, event_id=event.event_id)
            raise PreconditionError("no employee source is configured; the UC-01 record cannot be retrieved")
        employee = self.source.get(event.client_id, event.case_id)
        if employee is None:
            self._audit(actor, "flow.event_rejected", "employee_not_found", None, event_id=event.event_id)
            raise PreconditionError("no such employee record in the authorised source", case_id=event.case_id)
        mismatches = [n for n, a, b in (("client_id", event.client_id, employee.client_id),
                                         ("employee_id", event.employee_id, employee.employee_id),
                                         ("joining_date", event.joining_date, employee.joining_date)) if a != b]
        if mismatches or not employee.active:
            self._audit(actor, "flow.event_rejected", "mismatch", None, event_id=event.event_id,
                        fields=",".join(mismatches) or "inactive")
            raise PreconditionError("the event does not match the employee record, or the record is not active",
                                    fields=",".join(mismatches) or "inactive")
        if not (employee.offer_accepted_at and employee.form_approved_at and employee.form_approved_by_role):
            self._audit(actor, "flow.event_rejected", "uc01_facts_missing", None, event_id=event.event_id)
            raise PreconditionError("UC-01 has not supplied the accepted-offer and approved-form facts",
                                    case_id=event.case_id)
        notes = []
        if as_of - event.occurred_at.date() > EVENT_DELAY_AUDIT_AFTER:
            notes.append("event processed late")
            self._audit(actor, "flow.event_delayed", "processed_late", None, event_id=event.event_id,
                        lag_days=str((as_of - event.occurred_at.date()).days))
        from onboarding_task_management.contracts import OfferAcceptance, OnboardingForm

        trigger = OnboardingTrigger(
            client_id=employee.client_id, employee_id=employee.employee_id, joining_date=employee.joining_date,
            role_id=employee.role_id, department=employee.department, manager_id=employee.manager_id,
            location=employee.location, project=employee.project, grade=employee.grade,
            nationality_group=employee.nationality_group, attributes=employee.attributes,
            current_location_status=employee.current_location_status, case_id=event.case_id,
            correlation_id=event.correlation_id,
            offer=OfferAcceptance(employee_id=employee.employee_id, accepted=True, accepted_at=employee.offer_accepted_at),
            form=OnboardingForm(employee_id=employee.employee_id, submitted=True, approved=True,
                                approved_by_role=employee.form_approved_by_role, approved_at=employee.form_approved_at),
        )
        try:
            record = self.service.create_onboarding(actor, trigger)
        except DuplicateOnboardingError as exc:
            # The same employee again (a replayed or duplicate event): the existing onboarding is the answer.
            existing = exc.context.get("onboarding_id")
            self._audit(actor, "flow.event_duplicate", "idempotent_replay", existing, event_id=event.event_id)
            return FlowResult(str(existing), created=False, detail="duplicate event; existing onboarding returned",
                              notes=notes)
        self._audit(actor, "flow.event_accepted", "onboarding_created", record.onboarding_id, event_id=event.event_id)
        return FlowResult(record.onboarding_id, created=True, notes=notes)

    # -- Stages 3-5: review package and HR email --------------------------------------------------------------------------

    def generate_review(self, actor: Actor, onboarding_id: str, as_of: date, analyst=None) -> dict[str, Any]:
        """Record the document requirements and assemble the review for HR. The Fake LLM only explains."""
        record = self.service._load(actor, onboarding_id)
        config = self.service.registry.get(record.client_id, on=record.joining_date)
        included, excluded = config.documents_for(
            location=record.location, project=record.project, role_id=record.role_id, grade=record.grade,
            nationality_group=record.nationality_group, attributes=record.attributes)
        if not record.documents:
            self._mutate(actor, onboarding_id, lambda r: r.documents.extend(
                DocumentItem(key=req.key, document_type=req.document_type, mandatory=req.mandatory, reason=why,
                             allow_not_applicable=req.allow_not_applicable) for req, why in included))
        view = self.service.checklist(actor, onboarding_id, as_of)
        analysis = analysis_module.analyse_with_fallback(self.service, actor, onboarding_id, as_of, analyst)
        gaps = []
        if not config.document_requirements:
            gaps.append("the approved configuration lists no document requirements (the document gate cannot open)")
        if not config.assignment_for(TaskCategory.DOCUMENT_COLLECTION):
            gaps.append("no assignment rule for document collection (the request has no configured team)")
        return {"record": self.service.get(actor, onboarding_id), "view": view, "analysis": analysis,
                "documents_included": [(r, w) for r, w in included], "documents_excluded": excluded, "gaps": gaps,
                "config_version": config.version}

    def prepare_hr_email(self, actor: Actor, onboarding_id: str, as_of: date, analyst=None) -> HrReviewEmail:
        pkg = self.generate_review(actor, onboarding_id, as_of, analyst)
        record, view, analysis = pkg["record"], pkg["view"], pkg["analysis"]
        lines = [
            f"HR REVIEW - onboarding {record.onboarding_id}, plan version {record.plan_version}",
            "",
            "1. EMPLOYEE (identifiers only; no personal contact details are included)",
            f"   employee_id={record.employee_id} role={record.role_id} grade={record.grade or '-'} "
            f"location={record.location} project={record.project} nationality_group={record.nationality_group or '-'}",
            f"2. CLIENT AND JOINING: client={record.client_id}, joining date {record.joining_date.isoformat()}, "
            f"configuration version {pkg['config_version']}",
            "3. CHECKLIST",
        ]
        for e in view.entries:
            deps = ", ".join(e.blocked_by_keys) or "none"
            lines.append(f"   - {e.template_key} [{e.owner.value}] due {e.due_date.isoformat()} "
                         f"depends on: {deps} ({e.decision.reason})")
        def listing(rows) -> list[str]:
            return rows or ["   none"]

        mandatory = [f"   - {r.key} ({r.document_type}): {w}" for r, w in pkg["documents_included"] if r.mandatory]
        optional = [f"   - {r.key} ({r.document_type}): {w}" for r, w in pkg["documents_included"] if not r.mandatory]
        excluded_docs = [f"   - {r.key}: {w}" for r, w in pkg["documents_excluded"]]
        excluded_tasks = [f"   - {d.template_key}: {d.reason}" for d in view.excluded]
        teams = [f"   - {s.template_key}: owner role {s.owner_role}; team {s.team or 'NOT CONFIGURED'}"
                 f"{'; use case ' + s.use_case if s.use_case else ''}" for s in analysis.team_assignment_suggestions]
        concerns = [f"   - {x}" for x in (analysis.risks + analysis.missing_information + pkg["gaps"])]
        pending = next(iter(view.pending_gate_request_ids), "(not submitted yet)")
        lines += ["4. MANDATORY DOCUMENTS", *listing(mandatory), "5. CONDITIONAL / OPTIONAL DOCUMENTS", *listing(optional),
                  "6. EXCLUDED DOCUMENTS", *listing(excluded_docs), "7. EXCLUDED TASKS", *listing(excluded_tasks),
                  "8. OWNERS AND TEAMS", *teams,
                  f"9. FAKE LLM REVIEW ({analysis.source}; simulated, offline, advisory only - it cannot approve)",
                  f"   {analysis.checklist_summary}", *[f"   recommendation: {r}" for r in analysis.replan_recommendations],
                  "10. RISKS AND MISSING INFORMATION", *listing(concerns), "11. HOW TO RESPOND",
                  "   Approve, reject (with a reason) or request changes through the authenticated approval connector "
                  f"for request {pending}. Receiving or sending this email is NOT an approval.",
                  *([f"   The connector posts the signed decision to: {self.approval_callback_url}"]
                    if self.approval_callback_url else ["   (no approval endpoint is configured for this deployment)"])]
        email = HrReviewEmail(email_id=f"HRM-{onboarding_id}#p{record.plan_version}", onboarding_id=onboarding_id,
                              plan_version=record.plan_version, recipient_role=Role.HR_OFFICER,
                              subject=f"Onboarding plan for review: {record.employee_id} (v{record.plan_version})",
                              body="\n".join(lines))
        if not self._events(actor, onboarding_id, "hrmail", email.email_id):
            self._audit(actor, "hrmail.prepared", "draft_not_sent", onboarding_id, message_id=email.email_id,
                        prepared_by=actor.actor_id, plan_version=str(record.plan_version),
                        content_sha256=content_digest(email.subject, email.body))
        return email

    # -- authorisation and sending of prepared messages (HR email, document request) --------------------------------------------

    def _events(self, actor: Actor, onboarding_id: str, kind: str, message_id: str):
        return [e for e in self.service.audit.events_for(actor) if e.onboarding_id == onboarding_id
                and e.action.startswith(kind + ".") and e.detail.get("message_id") == message_id]

    def authorise_message(self, actor: Actor, onboarding_id: str, kind: str, message_id: str) -> None:
        """An HR lead, who did not prepare the message, authorises sending it. Authorising is not sending."""
        self.service._load(actor, onboarding_id)
        events = self._events(actor, onboarding_id, kind, message_id)
        prepared = [e for e in events if e.action == f"{kind}.prepared"]
        if not prepared:
            raise InvalidStateError("no such message has been prepared", message_id=message_id)
        if actor.role is not Role.HR_LEAD or any(e.detail.get("prepared_by") == actor.actor_id for e in prepared):
            self._audit(actor, f"{kind}.authorised", "denied_unauthorized", onboarding_id, message_id=message_id)
            raise UnauthorizedActionError("only a different HR lead may authorise sending", actor_role=str(actor.role))
        if not any(e.action == f"{kind}.authorised" and e.outcome == "authorised" for e in events):
            self._audit(actor, f"{kind}.authorised", "authorised", onboarding_id, message_id=message_id,
                        approver_id=actor.actor_id)

    def save_draft_message(self, actor: Actor, onboarding_id: str, kind: str, message_id: str, subject: str, body: str,
                           recipient_role: Role, connector: OutlookConnector, *, recipient_team: str = "",
                           role_shared: bool = False) -> str:
        """Save a PREPARED message as a draft in the authorised mailbox (needs a connector that can). Sends nothing.

        Returns the connector's status (``draft_created`` or ``duplicate_ignored``) and records the opaque draft
        reference in the audit trail, so the draft in the mailbox can be found again.
        """
        events = self._events(actor, onboarding_id, kind, message_id)
        prepared = [e for e in events if e.action == f"{kind}.prepared"]
        if not prepared:
            raise InvalidStateError("no such message has been prepared", message_id=message_id)
        self._require_prepared_content(prepared, subject, body, message_id)
        if not isinstance(connector, OutlookDraftConnector):
            raise InvalidStateError("this connector cannot save drafts", connector=type(connector).__name__)
        prior = [e for e in events if e.action == f"{kind}.draft_saved" and e.outcome in
                 (DeliveryStatus.DRAFT_CREATED.value, DeliveryStatus.DUPLICATE_IGNORED.value)]
        if prior:
            return prior[-1].outcome
        record = self.service._load(actor, onboarding_id)
        message = OutlookMessage(idempotency_key=message_id, onboarding_id=onboarding_id, client_id=record.client_id,
                                 recipient_role=recipient_role.value, subject_en=subject, body_en=body,
                                 recipient_team=recipient_team, role_shared=role_shared)
        try:
            receipt = connector.create_draft(message)
            outcome, ref = receipt.status.value, receipt.message_id or ""
        except OutlookConnectorError:
            outcome, ref = DeliveryStatus.FAILED.value, ""
        self._audit(actor, f"{kind}.draft_saved", outcome, onboarding_id, message_id=message_id,
                    draft_ref=ref, connector=type(connector).__name__, real=str(bool(getattr(connector, "real", False))))
        return outcome

    @staticmethod
    def _require_prepared_content(prepared, subject: str, body: str, message_id: str) -> None:
        """The content handed to a connector must be exactly what was prepared (and so what HR reviewed)."""
        expected = prepared[-1].detail.get("content_sha256")
        if expected and expected != content_digest(subject, body):
            raise InvalidStateError("the content differs from the prepared draft; prepare it again",
                                    message_id=message_id)

    def send_message(self, actor: Actor, onboarding_id: str, kind: str, message_id: str, subject: str, body: str,
                     recipient_role: Role, connector: OutlookConnector, *, recipient_team: str = "",
                     role_shared: bool = False) -> str:
        """Hand an authorised message to the connector and record exactly what it reported (never 'delivered')."""
        events = self._events(actor, onboarding_id, kind, message_id)
        auth = next((e for e in events if e.action == f"{kind}.authorised" and e.outcome == "authorised"), None)
        if auth is None:
            raise InvalidStateError("sending needs HR-lead authorisation first", message_id=message_id)
        self._require_prepared_content([e for e in events if e.action == f"{kind}.prepared"], subject, body, message_id)
        results = [e for e in events if e.action == f"{kind}.result"]
        done = [e for e in results if e.outcome in _HANDED_OVER]
        if done:
            return done[-1].outcome
        if len([e for e in results if e.outcome == DeliveryStatus.FAILED.value]) >= MAX_MESSAGE_SEND_ATTEMPTS:
            raise InvalidStateError("send attempts exhausted for this message", message_id=message_id)
        record = self.service._load(actor, onboarding_id)
        message = OutlookMessage(idempotency_key=message_id, onboarding_id=onboarding_id, client_id=record.client_id,
                                 recipient_role=recipient_role.value, subject_en=subject, body_en=body,
                                 approval_id=auth.event_id, recipient_team=recipient_team, role_shared=role_shared)
        # Write-ahead: the intent is recorded BEFORE the external call. If the process dies between the send and its
        # result being saved, the next attempt sees an intent with no result and relies on the connector's own
        # idempotency (the Graph connector looks the key up in Sent Items) instead of assuming nothing was sent.
        self._audit(actor, f"{kind}.send_attempted", "intent_recorded", onboarding_id, message_id=message_id)
        provider_ref = ""
        try:
            receipt = connector.send(message)
            outcome, provider_ref = receipt.status.value, receipt.message_id or ""
        except OutlookConnectorError:
            outcome = DeliveryStatus.FAILED.value
        self._audit(actor, f"{kind}.result", outcome, onboarding_id, message_id=message_id, provider_ref=provider_ref,
                    connector=type(connector).__name__, real=str(bool(getattr(connector, "real", False))),
                    recipient_role=recipient_role.value, recipient_team=recipient_team)
        return outcome

    # -- Stage 6: authenticated HR approval ------------------------------------------------------------------------------------

    def process_signed_callback(self, callback: ApprovalCallback, now: datetime) -> ApprovalOutcome:
        """Entry point for a transport (the HTTP route): the client comes from the SIGNED ``client_id`` field.

        The signature is checked before the client is used for anything. An unsigned or unsigned-client callback is
        refused without touching any client's data.
        """
        expected = hmac.new(self._secret, callback.canonical(), sha256).hexdigest()
        if not callback.client_id or not hmac.compare_digest(expected, callback.signature or ""):
            return ApprovalOutcome(status="invalid_signature", detail="callback is not authentic")
        return self.process_approval_callback(callback.client_id, callback, now)

    def process_approval_callback(self, client_id: str, callback: ApprovalCallback, now: datetime) -> ApprovalOutcome:
        with self._guard(callback.onboarding_id):
            return self._process_approval_callback(client_id, callback, now)

    def _process_approval_callback(self, client_id: str, callback: ApprovalCallback, now: datetime) -> ApprovalOutcome:
        actor = self._actor(client_id)
        expected = hmac.new(self._secret, callback.canonical(), sha256).hexdigest()
        if callback.client_id and callback.client_id != client_id:
            self._audit(actor, "approval.callback", "client_mismatch", None, callback_id=callback.callback_id)
            return ApprovalOutcome(status="invalid_signature", detail="callback is for another client")
        if not hmac.compare_digest(expected, callback.signature or ""):
            self._audit(actor, "approval.callback", "invalid_signature", None, callback_id=callback.callback_id)
            return ApprovalOutcome(status="invalid_signature", detail="callback is not authentic")
        if now - callback.issued_at > CALLBACK_MAX_AGE or callback.issued_at - now > CALLBACK_MAX_AGE:
            self._audit(actor, "approval.callback", "expired", callback.onboarding_id, callback_id=callback.callback_id)
            return ApprovalOutcome(status="expired", detail="callback is too old (or dated in the future)")
        try:
            record = self.service._load(actor, callback.onboarding_id)
            request = self.service.inbox.get(actor, callback.request_id)
        except Exception as exc:
            self._audit(actor, "approval.callback", "unknown_case_or_request", None, callback_id=callback.callback_id,
                        error=type(exc).__name__)
            return ApprovalOutcome(status="unknown_request", detail=type(exc).__name__)
        key = f"cb:{callback.callback_id}"
        if key in record.flow_ledger:
            return ApprovalOutcome(status="replayed", detail=f"already processed as {record.flow_ledger[key]}",
                                   onboarding_id=record.onboarding_id)
        if request.onboarding_id != callback.onboarding_id:
            return self._finish_callback(actor, record, key, "case_mismatch", "request belongs to another case")
        if callback.plan_version != record.plan_version or request.payload.get("plan_version") != str(callback.plan_version):
            return self._finish_callback(actor, record, key, "stale_version",
                                         f"plan is at version {record.plan_version}, callback is for {callback.plan_version}")
        if request.outcome is not DecisionOutcome.PENDING or record.plan_status is not PlanStatus.SUBMITTED:
            return self._finish_callback(actor, record, key, "not_pending", "this request is no longer pending")
        if now - request.created_at > self.approval_ttl:
            return self._finish_callback(actor, record, key, "expired", "the approval request expired; resubmit the plan")
        approver = Actor(actor_id=callback.approver_id, role=callback.approver_role, client_id=client_id)
        if callback.decision != "approved" and not callback.reason.strip():
            return self._finish_callback(actor, record, key, "reason_required",
                                         f"a {callback.decision.replace('_', ' ')} decision needs a reason")
        try:
            if callback.decision == "approved":
                self.service.decide_plan(approver, request.request_id, DecisionOutcome.APPROVED)
                self.initialise_workflow(actor, callback.onboarding_id)
                self.coordinate_after_approval(actor, callback.onboarding_id)
                return self._finish_callback(actor, record, key, "approved", "plan approved",
                                             plan_version=callback.plan_version)
            reason = redact_free_text(callback.reason)
            if callback.decision == "rejected":
                self.service.decide_plan(approver, request.request_id, DecisionOutcome.REJECTED, reason)
                return self._finish_callback(actor, record, key, "rejected", reason)
            self.service.decide_plan(approver, request.request_id, DecisionOutcome.REJECTED, f"changes requested: {reason}")
            self.service.revise_plan(actor, callback.onboarding_id, f"revision after HR change request: {reason}")
            new_request = self.service.submit_plan(actor, callback.onboarding_id)
            return self._finish_callback(actor, record, key, "changes_requested", reason,
                                         new_request_id=new_request.request_id)
        except UnauthorizedActionError as exc:
            return self._finish_callback(actor, record, key, "unauthorized", exc.message)

    def _finish_callback(self, actor: Actor, record: OnboardingRecord, key: str, status: str, detail: str,
                         plan_version: int | None = None, new_request_id: str | None = None) -> ApprovalOutcome:
        self._mutate(actor, record.onboarding_id, lambda r: r.flow_ledger.__setitem__(key, status))
        self._audit(actor, "approval.callback", status, record.onboarding_id, callback_id=key.removeprefix("cb:"))
        outcome = ApprovalOutcome(status=status, detail=detail, onboarding_id=record.onboarding_id,
                                  plan_version=plan_version, new_request_id=new_request_id)
        try:
            self.on_decision(actor, record.onboarding_id, outcome)
        except Exception as exc:  # a notification problem must never undo or hide the decision
            self._audit(actor, "approval.notice", "failed_to_prepare", record.onboarding_id, error=type(exc).__name__)
        return outcome

    def on_decision(self, actor: Actor, onboarding_id: str, outcome: ApprovalOutcome) -> None:
        """Hook called after a decision is committed. The base flow does nothing; :class:`RuntimeFlow` prepares the HR
        notice (and, after a change request, the new review email)."""

    def initialise_workflow(self, actor: Actor, onboarding_id: str) -> None:
        """After a valid approval: make sure the document list exists and the downstream tracks are waiting."""
        record = self.service._load(actor, onboarding_id)
        if record.plan_status is not PlanStatus.APPROVED:
            raise InvalidStateError("the plan is not approved", plan_status=record.plan_status.value)
        config = self.service.registry.get(record.client_id, on=record.joining_date)

        def change(r: OnboardingRecord) -> None:
            if not r.documents:
                included, _ = config.documents_for(location=r.location, project=r.project, role_id=r.role_id,
                                                  grade=r.grade, nationality_group=r.nationality_group,
                                                  attributes=r.attributes)
                r.documents.extend(DocumentItem(key=q.key, document_type=q.document_type, mandatory=q.mandatory,
                                                reason=w, allow_not_applicable=q.allow_not_applicable)
                                   for q, w in included)
            for uc in DOWNSTREAM:
                if uc not in r.downstream:
                    ids = [t.task_id for t in r.tasks if (rule := config.assignment_for(t.category)) and rule.use_case == uc]
                    r.downstream[uc] = DownstreamTrack(use_case=uc, task_ids=ids)

        self._mutate(actor, onboarding_id, change)
        self._audit(actor, "workflow.initialised", "documents_and_tracks_ready", onboarding_id)

    # -- Stage 8: document collection coordination ----------------------------------------------------------------------------------

    def prepare_document_request(self, actor: Actor, onboarding_id: str) -> DocumentRequest:
        record = self.service._load(actor, onboarding_id)
        if record.plan_status is not PlanStatus.APPROVED:
            raise InvalidStateError("documents are requested only after HR approves the plan",
                                    plan_status=record.plan_status.value)
        config = self.service.registry.get(record.client_id, on=record.joining_date)
        rule = config.assignment_for(TaskCategory.DOCUMENT_COLLECTION)
        if rule is None:
            raise MissingConfigurationError("no assignment rule names the document collection team",
                                            client_id=record.client_id)
        if not record.documents:
            raise MissingConfigurationError("no document requirements apply or are configured",
                                            client_id=record.client_id)
        task = next((t for t in record.tasks if t.category is TaskCategory.DOCUMENT_COLLECTION), None)
        due = task.due_date if task else record.joining_date
        docs = [{"key": d.key, "document_type": d.document_type, "mandatory": d.mandatory, "reason": d.reason}
                for d in record.documents]
        body = "\n".join([
            f"Document request for onboarding {onboarding_id} (employee {record.employee_id}), plan version "
            f"{record.plan_version}. Team: {rule.team}.", f"Submit by {due.isoformat()}. Required documents:",
            *[f"- {d['key']} ({d['document_type']}) {'MANDATORY' if d['mandatory'] else 'conditional'}: {d['reason']}"
              for d in docs],
            "Report each document's status (submitted, under review, verified, rejected, resubmission required) "
            "through the authenticated status contract. A submitted document is not a verified one."])
        request = DocumentRequest(request_id=f"DOCREQ-{onboarding_id}#p{record.plan_version}", onboarding_id=onboarding_id,
                                  plan_version=record.plan_version, team=rule.team, use_case=rule.use_case,
                                  recipient_role=next((t.owner for t in record.tasks
                                                       if t.category is TaskCategory.DOCUMENT_COLLECTION), Role.HR_OPS),
                                  due_date=due, documents=docs, body=body,
                                  subject=f"Document request: {record.employee_id} (plan v{record.plan_version})")
        if not self._events(actor, onboarding_id, "docreq", request.request_id):
            self._audit(actor, "docreq.prepared", "draft_not_sent", onboarding_id, message_id=request.request_id,
                        prepared_by=actor.actor_id, document_count=str(len(docs)),
                        content_sha256=content_digest(request.subject, request.body))
        return request

    def apply_document_update(self, source_actor: Actor, update: DocumentUpdate) -> DocumentUpdateResult:
        with self._guard(update.onboarding_id):
            return self._apply_document_update(source_actor, update)

    def _apply_document_update(self, source_actor: Actor, update: DocumentUpdate) -> DocumentUpdateResult:
        actor = Actor(actor_id=source_actor.actor_id, role=source_actor.role, client_id=source_actor.client_id)

        def reject(code: str, detail: str, record: OnboardingRecord | None = None) -> DocumentUpdateResult:
            self._audit(actor, "document.update", code, update.onboarding_id, update_id=update.update_id,
                        document_key=update.document_key)
            return DocumentUpdateResult(update_id=update.update_id, accepted=False, code=code, detail=detail)

        principals = self.service.source_principals
        if not (actor.actor_id or "").strip() or not principals or actor.actor_id not in principals.get(DOCUMENT_SOURCE, frozenset()):
            return reject("rejected_unverified_source", "the caller is not a registered document source")
        if update.client_id != actor.client_id:
            return reject("rejected_client_mismatch", "update is for another client")
        try:
            record = self.service._load(actor, update.onboarding_id)
        except Exception as exc:
            return reject("rejected_unknown_case", type(exc).__name__)
        key = f"doc:{update.update_id}"
        if key in record.flow_ledger:
            return DocumentUpdateResult(update_id=update.update_id, accepted=True, code="duplicate_replay",
                                        duplicate_replay=True)
        if update.plan_version != record.plan_version:
            return reject("rejected_stale_plan_version", f"plan is at version {record.plan_version}")
        if record.plan_status is not PlanStatus.APPROVED:
            return reject("rejected_plan_not_approved", "documents are tracked only for an approved plan")
        doc = next((d for d in record.documents if d.key == update.document_key), None)
        if doc is None:
            return reject("rejected_unknown_document", "that document is not required for this onboarding")
        if doc.updated_at is not None and update.occurred_at < doc.updated_at:
            self._mutate(actor, update.onboarding_id, lambda r: r.flow_ledger.__setitem__(key, "stale_ignored"))
            return DocumentUpdateResult(update_id=update.update_id, accepted=False, code="stale_ignored",
                                        detail="an older event never replaces a newer status")
        if update.state is DocumentState.NOT_APPLICABLE and not doc.allow_not_applicable:
            return reject("rejected_not_applicable_not_allowed", "no approved rule allows this document to be not applicable")
        if update.state is DocumentState.VERIFIED and not (update.reference or "").strip():
            return reject("rejected_missing_evidence_reference", "verification needs the verifier's reference")
        if update.state not in _DOC_TRANSITIONS[doc.state]:
            return reject("rejected_invalid_transition", f"{doc.state.value} -> {update.state.value} is not allowed")

        def change(r: OnboardingRecord) -> None:
            d = next(x for x in r.documents if x.key == update.document_key)
            d.state, d.evidence_ref, d.updated_at, d.last_update_id = update.state, update.reference, update.occurred_at, update.update_id
            r.flow_ledger[key] = "applied"

        self._mutate(actor, update.onboarding_id, change)
        self._audit(actor, "document.update", "applied", update.onboarding_id, update_id=update.update_id,
                    document_key=update.document_key, after_status=update.state.value, source=DOCUMENT_SOURCE.value,
                    occurred_at=update.occurred_at.isoformat())
        return DocumentUpdateResult(update_id=update.update_id, accepted=True, code="applied")

    # -- team-wise task assignment (after a valid HR approval) -----------------------------------------------------------------------

    def coordinate_after_approval(self, actor: Actor, onboarding_id: str) -> dict[str, Any]:
        """Run by an approved plan: prepare the team assignments and the document request. Prepares only; sends nothing.

        The approval has already been committed, so a gap here (for example no team rule for a category) is recorded
        and reported, never allowed to undo or block the approval.
        """
        outcome: dict[str, Any] = {"assignments": [], "document_request": None, "gaps": []}
        try:
            outcome["assignments"] = [a.assignment_id for a in self.prepare_team_assignments(actor, onboarding_id)]
        except (MissingConfigurationError, InvalidStateError) as exc:
            outcome["gaps"].append(exc.message)
        try:
            outcome["document_request"] = self.prepare_document_request(actor, onboarding_id).request_id
        except (MissingConfigurationError, InvalidStateError) as exc:
            outcome["gaps"].append(exc.message)
        for gap in outcome["gaps"]:
            self._audit(actor, "coordination.gap", "reported", onboarding_id, gap=str(gap)[:160])
        self._audit(actor, "coordination.prepared", "ready_for_authorisation", onboarding_id,
                    assignments=str(len(outcome["assignments"])), gaps=str(len(outcome["gaps"])))
        return outcome

    def prepare_team_assignments(self, actor: Actor, onboarding_id: str) -> list[TeamAssignment]:
        with self._guard(onboarding_id):  # two concurrent callers must not both add the same assignment
            return self._prepare_team_assignments(actor, onboarding_id)

    def _prepare_team_assignments(self, actor: Actor, onboarding_id: str) -> list[TeamAssignment]:
        """Group the released plan's open tasks by the client's approved team mapping. Idempotent per plan version.

        One assignment per ``(team, owner role, use case)``. A task whose category has no approved team rule is NOT
        assigned: it is reported as a gap. Assignments of an older plan version are marked ``superseded``.
        """
        record = self.service._load(actor, onboarding_id)
        if record.plan_status is not PlanStatus.APPROVED:
            self._audit(actor, "teamasg.prepare", "denied_plan_not_approved", onboarding_id,
                        plan_status=record.plan_status.value)
            from onboarding_task_management.errors import PlanNotApprovedError

            raise PlanNotApprovedError("team assignments are prepared only for an approved plan",
                                       plan_status=record.plan_status.value)
        config = self.service.registry.get(record.client_id, on=record.joining_date)
        by_key = {t.task_id: t.template_key for t in record.tasks}
        groups: dict[tuple[str, str, str], list] = {}
        unmapped: list[str] = []
        for task in record.tasks:
            if task.status not in OPEN_STATUSES:
                continue
            rule = config.assignment_for(task.category)
            if rule is None:
                unmapped.append(task.template_key)
                continue
            groups.setdefault((rule.team, task.owner.value, rule.use_case), []).append(task)
        if unmapped:
            self._audit(actor, "teamasg.gap", "unmapped_tasks", onboarding_id, task_keys=",".join(sorted(unmapped)))
        existing = {a.assignment_id for a in record.assignments}
        fresh: list[TeamAssignment] = []
        for (team, owner, use_case), tasks in sorted(groups.items()):
            assignment = TeamAssignment(
                assignment_id=assignment_id_for(onboarding_id, team, owner, record.plan_version, use_case),
                team=team, owner_role=owner, use_case=use_case, plan_version=record.plan_version,
                hold_reason=(f"{use_case} executes this work; it is held until every mandatory document is verified"
                             if use_case in DOWNSTREAM else ""),
                tasks=[AssignmentTask(
                    task_id=t.task_id, template_key=t.template_key, title=t.title, due_date=t.due_date,
                    priority=t.priority, depends_on_keys=sorted(by_key[d] for d in t.depends_on if d in by_key),
                    instructions=assignment_instructions(use_case)) for t in sorted(tasks, key=lambda x: (x.due_date, x.task_id))])
            subject, body = assignment_message(record, assignment)
            assignment.content_sha256 = content_digest(subject, body)
            if assignment.assignment_id not in existing:
                fresh.append(assignment)

        def change(r: OnboardingRecord) -> None:
            for old in r.assignments:
                if old.plan_version != r.plan_version and old.status != "superseded":
                    old.status = "superseded"
            r.assignments.extend(fresh)

        if fresh or any(a.plan_version != record.plan_version and a.status != "superseded" for a in record.assignments):
            self._mutate(actor, onboarding_id, change)
            for a in fresh:
                self._audit(actor, "teamasg.prepared", "draft_not_sent", onboarding_id, message_id=a.assignment_id,
                            prepared_by=actor.actor_id, plan_version=str(a.plan_version), team_role=a.owner_role,
                            task_count=str(len(a.tasks)), content_sha256=a.content_sha256)
        return [a for a in self.service._load(actor, onboarding_id).assignments if a.plan_version == record.plan_version]

    def _assignment(self, actor: Actor, onboarding_id: str, assignment_id: str) -> TeamAssignment:
        record = self.service._load(actor, onboarding_id)
        found = next((a for a in record.assignments if a.assignment_id == assignment_id), None)
        if found is None:
            raise InvalidStateError("no such team assignment", assignment_id=assignment_id)
        if found.status == "superseded" or found.plan_version != record.plan_version:
            raise InvalidStateError("this assignment belongs to an older plan version; prepare the current ones",
                                    assignment_id=assignment_id)
        return found

    def _set_assignment(self, actor: Actor, onboarding_id: str, assignment_id: str, **fields: Any) -> None:
        def change(r: OnboardingRecord) -> None:
            a = next(x for x in r.assignments if x.assignment_id == assignment_id)
            for k, v in fields.items():
                setattr(a, k, v)

        self._mutate(actor, onboarding_id, change)

    def _role_shared(self, actor: Actor, onboarding_id: str, a: TeamAssignment) -> bool:
        """True when another current assignment has the same owner role: a role-level address would be ambiguous."""
        record = self.service._load(actor, onboarding_id)
        return any(x.owner_role == a.owner_role and x.assignment_id != a.assignment_id
                   and x.plan_version == record.plan_version and x.status != "superseded" for x in record.assignments)

    def assignment_message(self, actor: Actor, onboarding_id: str, assignment_id: str) -> tuple[str, str, Role]:
        """``(subject, body, recipient_role)`` of one team's message: only that team's tasks."""
        record = self.service._load(actor, onboarding_id)
        a = self._assignment(actor, onboarding_id, assignment_id)
        subject, body = assignment_message(record, a)
        return subject, body, Role(a.owner_role)

    def save_assignment_drafts(self, actor: Actor, onboarding_id: str, connector: OutlookConnector) -> dict[str, str]:
        """Save every current assignment as a draft through the connector (Outlook/Graph or the mock). Sends nothing."""
        results: dict[str, str] = {}
        for a in self.service._load(actor, onboarding_id).assignments:
            if a.plan_version != self.service._load(actor, onboarding_id).plan_version or a.status == "superseded":
                continue
            subject, body, role = self.assignment_message(actor, onboarding_id, a.assignment_id)
            outcome = self.save_draft_message(actor, onboarding_id, "teamasg", a.assignment_id, subject, body, role,
                                              connector, recipient_team=a.team, role_shared=self._role_shared(actor, onboarding_id, a))
            events = self._events(actor, onboarding_id, "teamasg", a.assignment_id)
            ref = next((e.detail.get("draft_ref", "") for e in reversed(events) if e.action == "teamasg.draft_saved"), "")
            if self._assignment(actor, onboarding_id, a.assignment_id).status == "prepared":
                self._set_assignment(actor, onboarding_id, a.assignment_id, status="draft_saved" if outcome in
                                     ("draft_created", "duplicate_ignored") else "failed",
                                     draft_ref=ref or None, last_outcome=outcome)
            results[a.assignment_id] = outcome
        return results

    def authorise_assignment(self, actor: Actor, onboarding_id: str, assignment_id: str) -> None:
        """A different HR lead authorises sending one team's message. Authorising is not sending."""
        self._assignment(actor, onboarding_id, assignment_id)
        self.authorise_message(actor, onboarding_id, "teamasg", assignment_id)
        self._set_assignment(actor, onboarding_id, assignment_id, status="authorised")

    def send_assignment(self, actor: Actor, onboarding_id: str, assignment_id: str, connector: OutlookConnector) -> str:
        """Send one authorised team message. Held assignments (UC-04 / UC-05 work) wait for the document gate."""
        a = self._assignment(actor, onboarding_id, assignment_id)
        if a.hold_reason and not self.document_gate(actor, onboarding_id).open:
            self._audit(actor, "teamasg.send", "held_waiting_for_documents", onboarding_id, message_id=assignment_id)
            raise InvalidStateError(a.hold_reason, assignment_id=assignment_id)
        subject, body, role = self.assignment_message(actor, onboarding_id, assignment_id)
        outcome = self.send_message(actor, onboarding_id, "teamasg", assignment_id, subject, body, role, connector,
                                    recipient_team=a.team, role_shared=self._role_shared(actor, onboarding_id, a))
        failed = outcome == DeliveryStatus.FAILED.value
        self._set_assignment(actor, onboarding_id, assignment_id, last_outcome=outcome,
                             status="failed" if failed else outcome, send_attempts=a.send_attempts + 1)
        return outcome

    # -- Stage 9: the mandatory document gate ----------------------------------------------------------------------------------

    def _required_documents(self, record: OnboardingRecord):
        config = self.service.registry.get(record.client_id, on=record.joining_date)
        included, _ = config.documents_for(location=record.location, project=record.project, role_id=record.role_id,
                                           grade=record.grade, nationality_group=record.nationality_group,
                                           attributes=record.attributes)
        return included

    def refresh_documents(self, actor: Actor, onboarding_id: str) -> list[str]:
        """Reconcile the document list with what the configuration requires NOW (after a re-plan changed the
        employee's attributes). New requirements start as pending; verified documents are kept; requirements that
        no longer apply and were never verified are dropped. Returns what changed. Idempotent."""
        record = self.service._load(actor, onboarding_id)
        if not record.documents:
            return []
        required = {q.key: (q, why) for q, why in self._required_documents(record)}
        have = {d.key for d in record.documents}
        added = sorted(set(required) - have)
        dropped = sorted(d.key for d in record.documents if d.key not in required and d.state is not DocumentState.VERIFIED)
        if not added and not dropped:
            return []

        def change(r: OnboardingRecord) -> None:
            r.documents[:] = [d for d in r.documents if d.key not in dropped]
            r.documents.extend(DocumentItem(key=k, document_type=required[k][0].document_type,
                                            mandatory=required[k][0].mandatory, reason=required[k][1],
                                            allow_not_applicable=required[k][0].allow_not_applicable) for k in added)

        self._mutate(actor, onboarding_id, change)
        self._audit(actor, "documents.refreshed", "requirements_changed", onboarding_id, added=",".join(added),
                    dropped=",".join(dropped))
        return added + dropped

    def document_gate(self, actor: Actor, onboarding_id: str) -> GateStatus:
        record = self.service._load(actor, onboarding_id)
        reasons: list[str] = []
        if record.plan_status is not PlanStatus.APPROVED:
            reasons.append("the plan is not approved")
        if record.documents:
            missing = sorted({q.key for q, _ in self._required_documents(record)} - {d.key for d in record.documents})
            if missing:
                reasons.append(f"requirements changed since the list was made; refresh needed for: {', '.join(missing)}")
        mandatory = [d for d in record.documents if d.mandatory]
        if not mandatory:
            reasons.append("no mandatory documents are configured; the gate cannot open on an empty list")
        verified = [d for d in mandatory if d.state is DocumentState.VERIFIED
                    or (d.state is DocumentState.NOT_APPLICABLE and d.allow_not_applicable)]
        pending = [d.key for d in mandatory if d.state in (DocumentState.PENDING, DocumentState.SUBMITTED,
                                                           DocumentState.UNDER_REVIEW)]
        rejected = [d.key for d in mandatory if d.state is DocumentState.REJECTED]
        resub = [d.key for d in mandatory if d.state is DocumentState.RESUBMISSION_REQUIRED]
        if pending:
            reasons.append(f"mandatory documents not yet verified: {', '.join(pending)}")
        if rejected:
            reasons.append(f"mandatory documents rejected: {', '.join(rejected)}")
        if resub:
            reasons.append(f"resubmission required: {', '.join(resub)}")
        return GateStatus(open=not reasons, reasons=reasons, mandatory_total=len(mandatory),
                          mandatory_verified=len(verified), pending=pending, rejected=rejected,
                          resubmission_required=resub)

    # -- Stage 10: independent, parallel downstream triggers ------------------------------------------------------------------------

    def trigger_downstream(self, actor: Actor, onboarding_id: str, adapters: Mapping[str, TriggerAdapter]) -> dict[str, str]:
        """Trigger UC-04 and UC-05 only when the document gate is open; each independently, in parallel."""
        with self._guard(onboarding_id):
            return self._trigger_downstream(actor, onboarding_id, adapters)

    def _trigger_downstream(self, actor: Actor, onboarding_id: str, adapters: Mapping[str, TriggerAdapter]) -> dict[str, str]:
        self.refresh_documents(actor, onboarding_id)  # a re-plan may have changed what is required
        gate = self.document_gate(actor, onboarding_id)
        record = self.service._load(actor, onboarding_id)
        if not gate.open:
            self._audit(actor, "downstream.trigger", "blocked_waiting_for_documents", onboarding_id,
                        reasons=str(len(gate.reasons)))
            return {uc: DownstreamState.WAITING_FOR_DOCUMENTS.value for uc in DOWNSTREAM}
        config = self.service.registry.get(record.client_id, on=record.joining_date)
        todo: dict[str, dict[str, Any]] = {}
        not_configured: list[str] = []
        for uc in DOWNSTREAM:
            track = record.downstream.get(uc)
            if track is None:
                raise InvalidStateError("the workflow was not initialised", use_case=uc)
            if track.state in _OPEN_DOWNSTREAM:
                continue  # already triggered or beyond: never a second trigger
            if track.state is DownstreamState.REQUIRES_INTERVENTION and track.attempts >= MAX_TRIGGER_ATTEMPTS:
                continue
            if uc not in adapters:
                not_configured.append(uc)
                continue
            todo[uc] = self._payload(record, config, uc, gate)

        def send(uc: str) -> TriggerResult:
            try:
                return adapters[uc].send(todo[uc])
            except Exception as exc:  # a failing downstream must never stop the other
                return TriggerResult(False, error=type(exc).__name__)

        pending = [uc for uc in not_configured if record.downstream[uc].state is not DownstreamState.NOT_CONFIGURED]
        if pending:
            def mark(r: OnboardingRecord) -> None:
                for uc in pending:
                    r.downstream[uc].state = DownstreamState.NOT_CONFIGURED
                    r.downstream[uc].last_error = "no trigger endpoint or credentials are configured"

            self._mutate(actor, onboarding_id, mark)
            for uc in pending:
                self._audit(actor, "downstream.trigger", "not_configured", onboarding_id, use_case=uc)
        results: dict[str, TriggerResult] = {}
        if todo:
            # Write-ahead: count the attempt and pin the trigger id BEFORE sending. A crash after the send but before
            # the result is saved then leaves an attempt on record; the retry reuses the same deterministic trigger id,
            # which the downstream use case must treat as its idempotency key (at-least-once delivery).
            def intent(r: OnboardingRecord) -> None:
                for uc in todo:
                    r.downstream[uc].attempts += 1
                    r.downstream[uc].trigger_id = todo[uc]["trigger_id"]

            self._mutate(actor, onboarding_id, intent)
            for uc in todo:
                self._audit(actor, "downstream.trigger_intent", "recorded", onboarding_id, use_case=uc,
                            trigger_id=todo[uc]["trigger_id"])
            with ThreadPoolExecutor(max_workers=len(todo)) as pool:
                futures = {uc: pool.submit(send, uc) for uc in todo}
                results = {uc: f.result() for uc, f in futures.items()}

        def change(r: OnboardingRecord) -> None:
            for uc, res in results.items():
                t = r.downstream[uc]
                if res.accepted:
                    t.state, t.last_error = DownstreamState.TRIGGERED, ""
                else:
                    t.last_error = res.error or "rejected"
                    t.state = (DownstreamState.REQUIRES_INTERVENTION if t.attempts >= MAX_TRIGGER_ATTEMPTS
                               else DownstreamState.FAILED)

        if results:
            self._mutate(actor, onboarding_id, change)
            for uc, res in results.items():
                self._audit(actor, "downstream.trigger", "sent" if res.accepted else "failed", onboarding_id,
                            use_case=uc, trigger_id=todo[uc]["trigger_id"], reference_id=res.reference,
                            error=res.error)
        record = self.service._load(actor, onboarding_id)
        return {uc: record.downstream[uc].state.value for uc in DOWNSTREAM}

    @staticmethod
    def _payload(record: OnboardingRecord, config, uc: str, gate: GateStatus) -> dict[str, Any]:
        ids = record.downstream[uc].task_ids
        return {
            "trigger_id": f"TRG-{record.onboarding_id}-{uc}-p{record.plan_version}", "use_case": uc,
            "case_id": record.case_id or record.onboarding_id, "onboarding_id": record.onboarding_id,
            "employee_id": record.employee_id, "client_id": record.client_id, "plan_version": record.plan_version,
            "correlation_id": record.correlation_id, "configuration_version": record.config_version,
            "verified_documents": sorted(d.key for d in record.documents if d.state is DocumentState.VERIFIED),
            "tasks": [{"task_id": t.task_id, "key": t.template_key, "due": t.due_date.isoformat()}
                      for t in record.tasks if t.task_id in ids],
        }

    # -- Stage 11: downstream status tracking ----------------------------------------------------------------------------------------

    def apply_downstream_status(self, source_actor: Actor, event: DownstreamStatusEvent) -> DocumentUpdateResult:
        with self._guard(event.onboarding_id):
            return self._apply_downstream_status(source_actor, event)

    def _apply_downstream_status(self, source_actor: Actor, event: DownstreamStatusEvent) -> DocumentUpdateResult:
        actor = Actor(actor_id=source_actor.actor_id, role=source_actor.role, client_id=source_actor.client_id)

        def reject(code: str, detail: str) -> DocumentUpdateResult:
            self._audit(actor, "downstream.status", code, event.onboarding_id, event_id=event.event_id,
                        use_case=event.use_case)
            return DocumentUpdateResult(update_id=event.event_id, accepted=False, code=code, detail=detail)

        if event.use_case not in DOWNSTREAM:
            return reject("rejected_unknown_use_case", "only UC-04 and UC-05 report here")
        principals = self.service.source_principals
        if not (actor.actor_id or "").strip() or not principals or actor.actor_id not in principals.get(SOURCE_FOR[event.use_case], frozenset()):
            return reject("rejected_unverified_source", "the caller is not registered for that use case")
        if event.client_id != actor.client_id:
            return reject("rejected_client_mismatch", "event is for another client")
        if event.state not in _REPORTABLE:
            return reject("rejected_invalid_status", f"{event.state.value} cannot be reported")
        try:
            record = self.service._load(actor, event.onboarding_id)
        except Exception as exc:
            return reject("rejected_unknown_case", type(exc).__name__)
        key = f"ds:{event.event_id}"
        if key in record.flow_ledger:
            return DocumentUpdateResult(update_id=event.event_id, accepted=True, code="duplicate_replay", duplicate_replay=True)
        track = record.downstream.get(event.use_case)
        if track is None or track.state in (DownstreamState.WAITING_FOR_DOCUMENTS,):
            return reject("rejected_not_triggered", "no trigger has been sent for this use case")
        if event.plan_version != record.plan_version:
            return reject("rejected_stale_plan_version", f"plan is at version {record.plan_version}")
        if track.state is DownstreamState.COMPLETED:
            self._mutate(actor, event.onboarding_id, lambda r: r.flow_ledger.__setitem__(key, "ignored_after_completion"))
            return DocumentUpdateResult(update_id=event.event_id, accepted=False, code="ignored_after_completion")
        if track.last_event_at is not None and event.occurred_at < track.last_event_at:
            self._mutate(actor, event.onboarding_id, lambda r: r.flow_ledger.__setitem__(key, "stale_ignored"))
            self._audit(actor, "downstream.status", "stale_ignored", event.onboarding_id, event_id=event.event_id,
                        use_case=event.use_case)
            return DocumentUpdateResult(update_id=event.event_id, accepted=False, code="stale_ignored",
                                        detail="an older event never replaces a newer status")

        def change(r: OnboardingRecord) -> None:
            t = r.downstream[event.use_case]
            t.state, t.last_event_id, t.last_event_at = event.state, event.event_id, event.occurred_at
            r.flow_ledger[key] = "applied"

        self._mutate(actor, event.onboarding_id, change)
        self._audit(actor, "downstream.status", "applied", event.onboarding_id, event_id=event.event_id,
                    use_case=event.use_case, after_status=event.state.value, source=SOURCE_FOR[event.use_case].value,
                    correlation_id_ref=event.correlation_id or "")
        return DocumentUpdateResult(update_id=event.event_id, accepted=True, code="applied")

    def downstream_summary(self, actor: Actor, onboarding_id: str) -> dict[str, str]:
        record = self.service._load(actor, onboarding_id)
        return {uc: (record.downstream[uc].state.value if uc in record.downstream
                     else DownstreamState.WAITING_FOR_DOCUMENTS.value) for uc in DOWNSTREAM}


__all__ = [
    "APPROVAL_TTL", "ApprovalCallback", "ApprovalOutcome", "DOWNSTREAM", "DocumentRequest", "DocumentUpdate",
    "DocumentUpdateResult", "DownstreamStatusEvent", "EmployeeCreatedEvent", "EmployeeSource", "EmployeeView",
    "FlowResult", "GateStatus", "HrReviewEmail", "MAX_TRIGGER_ATTEMPTS", "MockTrigger", "OnboardingFlow",
    "SupabaseEmployeeSource", "TriggerAdapter", "TriggerResult", "UC04", "UC05", "sign_callback",
]
