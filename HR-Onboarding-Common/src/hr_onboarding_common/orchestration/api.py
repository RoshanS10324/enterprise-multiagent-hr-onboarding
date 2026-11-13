"""HTTP entry points for the central onboarding orchestrator (OnboardingOrchestratorAgent).

    POST /v1/orchestration/onboarding-documents/extract                  UC01 extracts an onboarding form for review
    POST /v1/orchestration/cases                                         start a case (idempotent per offer)
    GET  /v1/orchestration/cases/{case_id}                               case and stage status
    POST /v1/orchestration/cases/{case_id}/resume                        resume a blocked/interrupted case
    POST /v1/orchestration/cases/{case_id}/approvals/{id}/decision       an authorized person's decision
    POST /v1/orchestration/cases/{case_id}/approvals/{id}/retry-apply    retry a decision whose follow-up failed
    POST /v1/orchestration/cases/{case_id}/joining-day                   the joining-day lifecycle event
    POST /v1/orchestration/cases/{case_id}/task-planning/date-change     open UC02's real DATE_CHANGE gate
    POST /v1/orchestration/cases/{case_id}/orientation/operations        an HR operation on the UC05 lifecycle
    POST /v1/orchestration/cases/{case_id}/sessions/{session_id}/responses  an RSVP (independent UC05 event)
    POST /v1/orchestration/cases/{case_id}/documents                     supply / mark available required documents
    POST /v1/orchestration/cases/{case_id}/documents/reviews             an HR reviewer's decision (UC03 input)
    POST /v1/orchestration/cases/{case_id}/documents/requirements        HR changes the plan's document requirements
    POST /v1/orchestration/cases/{case_id}/stages/{stage}/retry          retry a stage that failed technically
    GET  /v1/orchestration/cases/{case_id}/pending-actions               what has to happen next, and who acts
    POST /v1/orchestration/cases/{case_id}/events                        an asynchronous event, at most once per event_id
    GET  /v1/orchestration/cases/{case_id}/stages                        every stage's execution status (standard contract)
    GET  /v1/orchestration/cases/{case_id}/stages/{stage}                one stage's execution status
    GET  /v1/orchestration/cases/{case_id}/errors                        sanitized, categorized stage errors
    GET  /v1/orchestration/cases/{case_id}/approvals                     open approvals and relayed decisions
    GET  /v1/orchestration/dependencies                                  the stage dependency registry (caller's tenant)
    GET  /v1/orchestration/cases                                         the caller's tenant's cases (summaries)
    GET  /v1/orchestration/me                                            the caller's identity and what the policy lets it do
    POST /v1/orchestration/cases/{case_id}/equipment-access/requests     HR records the joiner's equipment/access requests (UC04)
    POST /v1/orchestration/cases/{case_id}/orientation/offer-event       HR supplies the offer-acceptance event reference (UC05)
    GET  /v1/orchestration/dev/identities                                the development identity registry (registry provider only)
    GET  /v1/orchestration/approvals                                     every pending approval of the tenant's cases,
                                                                         with whether the caller may decide it
    GET  /v1/orchestration/cases/{case_id}/profile                       the intake facts of one case (dashboard, UC01 panel)
    POST /v1/orchestration/cases/{case_id}/documents/{type}/upload       upload one file: Storage -> UC03 (documents.py)
    GET  /v1/orchestration/cases/{case_id}/documents/{type}/url          a short-lived signed URL to view the latest file
    GET  /healthz

Documents are references plus extraction metadata, never contents (the request model has no field
for them and refuses unknown fields); the metadata is never returned by any route.

Identity and tenancy: every route but /healthz authenticates through the supplied
`IdentityProvider` (hr_onboarding_common.platform.identity) -- this module never reads identity
headers itself. The tenant is the verified actor's `client_id`, never a request-body field (the
start body has no client_id and forbids unknown fields). Another tenant's case is answered exactly
like a missing one (404). Roles come from the policy envelope's `settings` (case_operator_roles,
case_reader_roles, joining_day_trigger_roles, uc05_operation_roles) and fail closed. A decision
needs no orchestrator role: the person deciding is authenticated, and the inbox that owns the
approval (the orchestrator's, or UC05's) checks their role, tenant and -- for UC05's team/buddy
introductions -- that they are the specific person on record.

UC05 is reached in-process through the orchestrator's worker, not through UC05's own HTTP API; UC05
still enforces its own tenant, role and approval rules at its service layer.

Errors carry no stack trace or provider text: 401 identity, 403 role/policy/kill switch, 404 not
found (or another tenant's), 409 a request that does not fit the case's state, 422 validation,
502 a UC failed or answered outside its contract. A UC worker that is down does not fail the request:
the case comes back with the affected stage BLOCKED (UC05_UNAVAILABLE) or, after a write, UNCERTAIN.
"""

# No "from __future__ import annotations": FastAPI resolves these models at runtime (same reason
# as hr_onboarding_common/api.py).

import os
from datetime import date
from typing import Any, Literal
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator

from hr_onboarding_common.observability.logging import get_logger
from hr_onboarding_common.orchestration import dependencies, execution, onboarding_documents
from hr_onboarding_common.orchestration.adapters._shared import OrchestratorAuthorizationError
from hr_onboarding_common.orchestration.case_store import (CaseOwnershipError, StatePersistenceError,
                                                           StatePersistenceUnavailable, StateVersionConflict)
from hr_onboarding_common.orchestration.adapters.uc05_orientation import (PrerequisiteNotMetError, Uc05RelayError,
                                                                     approver_on_record)
from hr_onboarding_common.orchestration.contracts import OnboardingCaseInput
from hr_onboarding_common.orchestration.documents import DocumentIntakeService, DocumentUploadError
from hr_onboarding_common.orchestration.onboarding_documents import OnboardingDocumentError
from hr_onboarding_common.orchestration.orchestrator_agent import OnboardingOrchestratorAgent, WorkflowRequestError
from hr_onboarding_common.orchestration.workflow_models import DATA_ORIGIN, DocumentReviewRequest, DocumentSubmission
from hr_onboarding_common.platform.hitl import HITLApplyError, HITLError, ReasonCode
from hr_onboarding_common.platform.identity import IdentityError, IdentityProvider, VerifiedActor
from hr_onboarding_common.platform.policy import PolicyViolation

log = get_logger("orchestration.api")


class StartCaseRequest(BaseModel):
    """OnboardingCaseInput without client_id/hr_officer_id: those come from the verified actor."""

    model_config = ConfigDict(extra="forbid")

    offer_accepted: bool
    offer_reference: str | None = Field(default=None, max_length=200)
    offer_acceptance_event_id: str | None = Field(default=None, max_length=200)
    candidate_name: str | None = Field(default=None, max_length=200)
    candidate_email: str | None = Field(default=None, max_length=320)
    designation: str | None = Field(default=None, max_length=200)
    joining_date: date | None = None
    work_location_type: str | None = Field(default=None, max_length=50)
    id_number: str | None = Field(default=None, max_length=100)
    role_id: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=100)
    manager_id: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=100)
    project: str | None = Field(default=None, max_length=100)
    # Forwarded to UC01's placement and UC02's checklist selection (OnboardingCaseInput already carries
    # them). nationality_group is an opaque group code, never a nationality.
    grade: str | None = Field(default=None, max_length=50)
    nationality_group: str | None = Field(default=None, max_length=50)
    # Applicability attributes HR states for UC02's checklist (e.g. visa_required, location_status:in_country) --
    # OnboardingCaseInput.attributes. Sent to UC02 exactly as stated; UC02 never infers one, and when the client's
    # templates depend on a family HR did not state, UC02 holds the plan for review (attribute_unstated).
    attributes: list[str] | None = Field(default=None, max_length=32)
    uc04_client_config_ref: str | None = Field(default=None, max_length=500)
    # What UC01 verified when it opened the case. UC02 plans right after intake and its own start
    # condition needs them; without them UC02 refuses to plan (MISSING_DATA) -- never assumed.
    offer_accepted_at: str | None = Field(default=None, max_length=40, description="ISO timestamp")
    form_approved_at: str | None = Field(default=None, max_length=40, description="ISO timestamp")
    form_approved_by_role: str | None = Field(default=None, max_length=50)
    employee_id: str | None = Field(default=None, max_length=64,
                                    description="the canonical (HRMS) employee id, if known (else resolved from "
                                                "UC01's employee_no through the tenant's identity scheme)")
    timezone: str | None = Field(default=None, max_length=64, description="the employee's IANA timezone, if known")
    # The onboarding-form extraction this form was prefilled from (POST /onboarding-documents/extract), if any. The
    # case records the server's own summary of it, never what the browser says it was; the form fields above stay
    # what HR submits. extraction_reviewed: HR confirms having checked the fields UC01 flagged (required when it
    # flagged any).
    source_extraction_id: str | None = Field(default=None, max_length=64)
    extraction_reviewed: bool = False

    @field_validator("attributes")
    @classmethod
    def _attributes(cls, value: list[str] | None) -> list[str] | None:
        """Codes only (letters, digits, `_ : . -`, at most 64 characters each): never free text or personal data."""
        if value is None:
            return None
        import re

        for item in value:
            if not re.fullmatch(r"[A-Za-z0-9_:.\-]{1,64}", item or ""):
                raise ValueError("each attribute is a code of letters, digits, _ : . - (1-64 characters)")
        return list(dict.fromkeys(value))

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"unknown timezone '{value}'") from None
        return value


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "edited", "rejected"]
    reason_code: ReasonCode | None = None
    note: str | None = Field(default=None, max_length=1000)
    edited_payload: dict[str, Any] | None = None


class JoiningDayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: date


class DateChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    joining_date: date


class Uc05OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["reschedule_session", "schedule_buddy_intro", "propose_buddy", "redraft_welcome",
                       "resolve_operation", "resolve_welcome_delivery", "retry_welcome_delivery",
                       "restart_after_records_lost"]
    session_type: Literal["induction", "team_intro"] | None = None
    start: AwareDatetime | None = None
    key: str | None = Field(default=None, max_length=300)
    outcome: Literal["succeeded", "released"] | None = None
    resolution: Literal["delivered", "not_delivered", "keep_unresolved"] | None = None
    reason: str | None = Field(default=None, max_length=500,
                               description="the evidence checked / why (required for welcome-delivery operations)")
    provider_ref: str | None = Field(default=None, max_length=200)


class ParticipantResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: str = Field(min_length=1, max_length=100)
    response: Literal["accepted", "tentative", "declined", "no_response"]
    responded_at: AwareDatetime | None = Field(default=None, description="the provider's timestamp of the RSVP")


class SubmitDocumentsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[DocumentSubmission] = Field(min_length=1, max_length=50)


class ReviewDocumentsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviews: list[DocumentReviewRequest] = Field(min_length=1, max_length=50)


class RequirementChangeBody(BaseModel):
    """HR adds or drops document requirements of the current plan (UC03 reports it, UC02 replans)."""

    model_config = ConfigDict(extra="forbid")

    add_documents: list[str] = Field(default_factory=list, max_length=20)
    remove_documents: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=200)


class EquipmentAccessRequestsBody(BaseModel):
    """The joiner's requested equipment and access: item ids from the client's UC04 catalogue."""

    model_config = ConfigDict(extra="forbid")

    requested_equipment: list[str] = Field(default_factory=list, max_length=30)
    requested_access: list[str] = Field(default_factory=list, max_length=30)


class OfferEventBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offer_acceptance_event_id: str = Field(min_length=1, max_length=128)


class CaseEventRequest(BaseModel):
    """An asynchronous event for one case. `event_id` is the sender's own id of the event: a redelivery
    with the same id is answered without being applied again."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    type: Literal["documents_submitted", "joining_day", "participant_response", "resume"]
    payload: dict[str, Any] = Field(default_factory=dict)


class EventResult(BaseModel):
    case: "CaseView"
    event: dict[str, Any]


class StageExecutionsView(BaseModel):
    case_id: str
    workflow_status: str | None = None
    stages: dict[str, dict[str, Any]]


class StageExecutionView(BaseModel):
    case_id: str
    execution: dict[str, Any]


class ErrorsView(BaseModel):
    case_id: str
    workflow_status: str | None = None
    errors: list[dict[str, Any]]


class ApprovalsView(BaseModel):
    case_id: str
    pending_approvals: list[dict[str, Any]]
    decisions: list[dict[str, Any]]


class DependenciesView(BaseModel):
    client_id: str | None = None
    dependencies: dict[str, list[dict[str, Any]]]


class PendingActionsView(BaseModel):
    case_id: str
    workflow_status: str
    overall_status: str
    current_stage: str
    pending_actions: list[dict[str, Any]]
    blockers: list[dict[str, Any]]
    pending_approvals: list[dict[str, Any]]


class CaseView(BaseModel):
    """What a case looks like over HTTP: ids, statuses and actionable reasons. Never the intake
    inputs (names, email, id number), the acting user's identity, document metadata, or any UC's
    raw payloads. Every result is synthetic or mocked (`data_origin`)."""

    case_id: str
    client_id: str
    employee_id: str | None = None  # unchanged for compatibility: UC01's employee_no (see the two fields below)
    # The two employee numbers, named explicitly (employee_numbers()): the canonical HRMS number every UC05 call uses,
    # and UC01's own number (in development, UC01's in-memory numbering repeats across restarts).
    hrms_employee_no: str | None = None
    uc01_employee_no: str | None = None
    joining_date: str | None = None
    # The authoritative joining date's revision and history, and whether every date-dependent result
    # was produced for it (orchestration/joining_date.py).
    joining_date_revision: int | None = None
    joining_date_history: list[dict[str, Any]] = Field(default_factory=list)
    joining_date_consistency: dict[str, Any] | None = None
    current_stage: str
    overall_status: str
    workflow_status: str | None = None
    data_origin: str = DATA_ORIGIN
    stage_statuses: dict[str, str]
    branch_statuses: dict[str, dict[str, Any]] = Field(default_factory=dict)
    completed_stages: list[str] = Field(default_factory=list)
    pending_actions: list[dict[str, Any]] = Field(default_factory=list)
    document_collection: dict[str, Any] | None = None
    documents: dict[str, dict[str, Any]] = Field(default_factory=dict)
    document_validation: dict[str, dict[str, Any]] = Field(default_factory=dict)
    validation_issues: list[dict[str, Any]] = Field(default_factory=list)
    correction_requirements: list[dict[str, Any]] = Field(default_factory=list)
    verified_context: dict[str, Any] | None = None
    task_plan: dict[str, Any] | None = None
    # UC02's versioned plan, its history, the last revision UC03 applied, UC03's feedback, HR changes.
    onboarding_plan: dict[str, Any] | None = None
    plan_history: list[dict[str, Any]] = Field(default_factory=list)
    revised_plan: dict[str, Any] | None = None
    document_validation_feedback: dict[str, Any] | None = None
    requirement_change_requests: list[dict[str, Any]] = Field(default_factory=list)
    equipment_access: dict[str, Any] | None = None
    stage_outcomes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    retry_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    next_action: str | None = None
    error_code: str | None = None
    stage_errors: dict[str, dict[str, Any]] = Field(default_factory=dict)
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = Field(default_factory=list)
    approval_decisions: list[dict[str, Any]] = Field(default_factory=list)
    employee_ref: dict[str, Any] | None = None
    participant_responses: list[dict[str, Any]] = Field(default_factory=list)
    outstanding_tasks: list[str] = Field(default_factory=list)
    orientation: dict[str, Any] | None = None
    buddy_assignment: dict[str, Any] | None = None
    team_introduction: dict[str, Any] | None = None
    buddy_introduction: dict[str, Any] | None = None
    welcome_message: dict[str, Any] | None = None
    joining_day_event: dict[str, Any] | None = None
    stage_history: list[dict[str, Any]] = Field(default_factory=list)
    # The internal onboarding form's approval request: requester, required role, assignment, status, the
    # actual decision once made, and whether it was written to approval_requests.
    form_approval: dict[str, Any] | None = None
    # The onboarding-form document the form was prefilled from: UC01's extraction statuses, field names, the
    # document digest, the fields HR changed, who confirmed the review. Never extracted values.
    form_extraction: dict[str, Any] | None = None
    approval_persistence: dict[str, dict[str, Any]] = Field(default_factory=dict)
    equipment_access_requests: dict[str, Any] | None = None  # HR's recorded UC04 requests (who, when, which items)
    state_version: int | None = None
    field_provenance: dict[str, dict[str, Any]] = Field(default_factory=dict)  # field -> stage, UC, execution id


EventResult.model_rebuild()


class CaseProfile(BaseModel):
    """The intake facts the case state keeps (orchestrator_agent.initial_state): never the email, the id
    number or the onboarding form. `candidate_name` only for the roles in `case_identity_reader_roles`."""

    candidate_name: str | None = None
    offer_reference: str | None = None
    designation: str | None = None
    work_location_type: str | None = None
    role_id: str | None = None
    department: str | None = None
    grade: str | None = None
    manager_id: str | None = None
    location: str | None = None
    project: str | None = None
    nationality_group: str | None = None
    timezone: str | None = None
    requested_by: str | None = None


class CaseSummary(BaseModel):
    case_id: str
    client_id: str
    employee_id: str | None = None  # UC01's employee_no (compatibility)
    hrms_employee_no: str | None = None
    uc01_employee_no: str | None = None
    joining_date: str | None = None
    current_stage: str
    overall_status: str
    workflow_status: str | None = None
    data_origin: str = DATA_ORIGIN
    stage_statuses: dict[str, str]
    completed_stages: list[str] = Field(default_factory=list)
    pending_action_count: int = 0
    blocker_count: int = 0
    next_action: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    form_approval_status: str | None = None  # not_requested | pending | approved | rejected; None when not required
    profile: CaseProfile


class CaseListView(BaseModel):
    client_id: str
    cases: list[CaseSummary]


class CaseProfileView(BaseModel):
    case_id: str
    client_id: str
    employee_id: str | None = None
    joining_date: str | None = None
    profile: CaseProfile


class DocumentUploadResult(BaseModel):
    case: "CaseView"
    upload: dict[str, Any]


class DocumentUrlView(BaseModel):
    case_id: str
    document_type: str
    revision: int | None = None
    url: str
    expires_in: int


class OperationResult(BaseModel):
    case: CaseView
    outcome: dict[str, Any]


def _collection_view(collection: dict[str, Any] | None) -> dict[str, Any] | None:
    if not collection:
        return None
    notification = collection.get("notification") or {}
    return {"required_documents": collection.get("required_documents", []),
            "outstanding": collection.get("outstanding", []), "requested_at": collection.get("requested_at"),
            "source": collection.get("source"), "plan_id": collection.get("plan_id"),
            "plan_version": collection.get("plan_version"),
            "notification": {k: notification.get(k) for k in ("idempotency_key", "status", "transport", "synthetic",
                                                               "attempts", "message_id")} if notification else None}


def _documents_view(documents: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per document: reference, source, revision, when, and the review decision -- never the
    extraction metadata, the submitter, the reviewer or the reviewer's note."""
    view = {}
    for doc_type, doc in documents.items():
        review = doc.get("hr_review") or {}
        view[doc_type] = {"document_ref": doc.get("document_ref"), "source": doc.get("source"),
                          "revision": doc.get("revision"), "submitted_at": doc.get("submitted_at"),
                          "hr_review": {k: review.get(k) for k in ("decision", "revision", "reviewed_at")}
                          if review else None}
    return view


def _retry_view(meta: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    keys = ("failures", "max_attempts", "last_code", "last_failure_at", "retries_authorized", "exhausted")
    return {stage: {k: m.get(k) for k in keys} for stage, m in meta.items()}


DocumentUploadResult.model_rebuild()


def case_profile(state: dict[str, Any], *, include_identity: bool) -> CaseProfile:
    trigger = state.get("intake_trigger") or {}
    role = state.get("uc02_role_context") or {}
    return CaseProfile(
        candidate_name=trigger.get("candidate_name") if include_identity else None,
        offer_reference=trigger.get("offer_reference"), designation=trigger.get("designation"),
        work_location_type=trigger.get("work_location_type"), role_id=role.get("role_id"),
        department=role.get("department"), grade=role.get("grade"), manager_id=role.get("manager_id"),
        location=role.get("location"), project=role.get("project"), nationality_group=role.get("nationality_group"),
        timezone=state.get("case_timezone"), requested_by=(state.get("actor") or {}).get("on_behalf_of"))


def case_summary(state: dict[str, Any], *, include_identity: bool) -> CaseSummary:
    return CaseSummary(
        case_id=state["case_id"], client_id=state["client_id"], employee_id=state.get("employee_id"),
        **employee_numbers(state),
        joining_date=state.get("joining_date"), current_stage=str(state.get("current_stage")),
        overall_status=str(state.get("overall_status")), workflow_status=state.get("workflow_status"),
        stage_statuses=dict(state.get("stage_statuses") or {}),
        completed_stages=[str(s) for s in state.get("completed_stages") or []],
        pending_action_count=len(state.get("pending_actions") or []), blocker_count=len(state.get("blockers") or []),
        next_action=state.get("next_action"), created_at=state.get("created_at"), updated_at=state.get("updated_at"),
        form_approval_status=(state.get("form_approval") or {}).get("status")
        if (state.get("form_approval") or {}).get("required") else None,
        profile=case_profile(state, include_identity=include_identity))


def form_approval_summary(fa: dict[str, Any] | None, *, include_identity: bool = False) -> dict[str, Any] | None:
    """The form approval request without who is involved (CaseView never carries names or user ids); with
    `include_identity` (GET .../form-approval, case_identity_reader_roles) the employee name, requester and
    decider too. The reviewed form content is never returned here."""
    if not fa:
        return None
    if not fa.get("required"):
        return {"required": False}
    decision = dict(fa.get("decision") or {}) or None
    view = {k: fa.get(k) for k in ("required", "approval_type", "gate", "status", "approval_id", "requested_at",
                                    "required_approver_role", "separation_of_duties", "assignment", "persistence")}
    if decision and not include_identity:
        decision.pop("decided_by", None)
    view["decision"] = decision
    if include_identity:
        view["requested_by"] = fa.get("requested_by")
        view["employee_name"] = (fa.get("form") or {}).get("candidate_name")
    return view


def form_extraction_summary(fx: dict[str, Any] | None, *, include_identity: bool = False) -> dict[str, Any] | None:
    """Where the form's data came from (onboarding_documents.provenance_for_case): statuses and field names only. Who
    extracted it and who confirmed the review only with `include_identity`, as for the form approval."""
    if not fx:
        return None
    view = {k: v for k, v in fx.items() if k not in ("extracted_by", "reviewed_by")}
    if include_identity:
        view["extracted_by"], view["reviewed_by"] = fx.get("extracted_by"), fx.get("reviewed_by")
    return view


def _pending_view(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Pending approvals, each person-specific one naming the person who decides it (approver_on_record)."""
    out = []
    for p in state.get("pending_approvals") or []:
        person = approver_on_record(state, p)
        out.append({**p, "approver_id": person} if person else dict(p))
    return out


def employee_numbers(state: dict[str, Any]) -> dict[str, str | None]:
    """The case's two employee numbers, never one silently replacing the other. HRMS: the canonical id resolved for
    the case (employee_ref, set when UC05 is first addressed), else the HRMS number entered on the form; None while
    neither exists. UC01: the number UC01 reported (employee_ref.employee_no, else the case's employee_id)."""
    ref = state.get("employee_ref") or {}
    return {"hrms_employee_no": ref.get("employee_id") or (state.get("intake_trigger") or {}).get("employee_id"),
            "uc01_employee_no": ref.get("employee_no") or state.get("employee_id")}


_RAISED_BY = {"onboarding-orchestrator": "Orchestrator", "employee-onboarding": "UC05 Orientation",
              "dact-uc04-equipment-access": "UC04 Equipment & Access", "onboarding-task-management": "UC02 Task Planning"}


def _approval_request(state: dict[str, Any], pending: dict[str, Any]) -> dict[str, Any] | None:
    """What a UC04 item approval is for: the item UC04 evaluated (id, kind from its request key, approval type) --
    so several item approvals of one case are told apart. None for other approvals (their description says it)."""
    if pending.get("proposal_type") != "uc04_item_approval":
        return None
    request_id = pending.get("record_id") or ""
    item = next((i for i in (state.get("equipment_access_result") or {}).get("items") or []
                 if i.get("request_id") == request_id), {})
    key = request_id.rsplit(":", 1)[-1]
    kind = "EQUIPMENT" if key.startswith("EQ-") else "ACCESS" if key.startswith("AC-") else None
    return {"item_id": item.get("item_id"), "item_kind": kind, "item_state": item.get("state"),
            "approval_type": (pending.get("reference") or "").rsplit(":", 1)[-1], "request_id": request_id}


def _requester(state: dict[str, Any], pending: dict[str, Any]) -> str | None:
    """Who asked for what a non-form approval decides, where the case records it: for UC04 items, the HR user who
    recorded the requests. Identity-gated by the caller (like the form's requester)."""
    if pending.get("proposal_type") == "uc04_item_approval":
        return (state.get("equipment_access_requests") or {}).get("recorded_by")
    return None


def case_view(state: dict[str, Any]) -> CaseView:
    return CaseView(
        case_id=state["case_id"], client_id=state["client_id"], employee_id=state.get("employee_id"),
        **employee_numbers(state),
        joining_date=state.get("joining_date"), joining_date_revision=state.get("joining_date_revision"),
        joining_date_history=list(state.get("joining_date_history") or []),
        joining_date_consistency=state.get("joining_date_consistency"), current_stage=str(state.get("current_stage")),
        overall_status=str(state.get("overall_status")), workflow_status=state.get("workflow_status"),
        stage_statuses=dict(state["stage_statuses"]), branch_statuses=dict(state.get("branch_statuses") or {}),
        completed_stages=list(state.get("completed_stages") or []),
        pending_actions=list(state.get("pending_actions") or []),
        document_collection=_collection_view(state.get("document_collection")),
        documents=_documents_view(state.get("submitted_documents") or {}),
        document_validation=dict(state.get("document_validation_result") or {}),
        validation_issues=list(state.get("validation_issues") or []),
        correction_requirements=list(state.get("correction_requirements") or []),
        verified_context=state.get("verified_context"), task_plan=state.get("task_plan"),
        onboarding_plan=state.get("onboarding_plan"), plan_history=list(state.get("plan_history") or []),
        revised_plan=state.get("revised_plan"), document_validation_feedback=state.get("document_validation_feedback"),
        requirement_change_requests=list(state.get("requirement_change_requests") or []),
        equipment_access=state.get("equipment_access_result"),
        stage_outcomes=dict(state.get("stage_outcomes") or {}),
        retry_metadata=_retry_view(state.get("retry_metadata") or {}),
        created_at=state.get("created_at"), updated_at=state.get("updated_at"),
        next_action=state.get("next_action"), error_code=state.get("error_code"),
        stage_errors=dict(state.get("stage_errors") or {}), blockers=list(state.get("blockers") or []),
        pending_approvals=_pending_view(state),
        approval_decisions=list(state.get("approval_decisions") or []),
        employee_ref=state.get("employee_ref"),
        participant_responses=list(state.get("participant_responses") or []),
        outstanding_tasks=list(state.get("outstanding_tasks") or []),
        orientation=state.get("orientation_schedule_result"), buddy_assignment=state.get("buddy_assignment_result"),
        team_introduction=state.get("team_introduction_result"),
        buddy_introduction=state.get("buddy_introduction_result"),
        welcome_message=state.get("welcome_message_result"), joining_day_event=state.get("joining_day_event"),
        stage_history=list(state.get("stage_history") or [])[-50:],
        form_approval=form_approval_summary(state.get("form_approval")),
        form_extraction=form_extraction_summary(state.get("form_extraction")),
        approval_persistence=dict(state.get("approval_persistence") or {}),
        equipment_access_requests=state.get("equipment_access_requests"),
        state_version=state.get("state_version"), field_provenance=dict(state.get("field_provenance") or {}),
    )


_RELAY_STATUS = {"POLICY_DENIED": 403, "NOT_FOUND": 404, "CONFLICT": 409, "INVALID_INPUT": 422}


def _http_error(exc: Exception) -> HTTPException:
    """One mapping for every route. Messages are this package's own (ids and reasons), or UC05's
    refusal reason; anything unexpected becomes a generic 500 and is logged by type only."""
    if isinstance(exc, DocumentUploadError):
        return HTTPException(exc.status, f"{exc.code}: {exc.detail}")
    if isinstance(exc, OnboardingDocumentError):
        return HTTPException(exc.http_status, f"{exc.code}: {exc.reason}")
    if isinstance(exc, IdentityError):
        return HTTPException(401, str(exc))
    if isinstance(exc, Uc05RelayError):
        status = _RELAY_STATUS.get(exc.code, 502)
        return HTTPException(status, exc.detail if status != 502 else f"UC05 could not complete the request ({exc.code})")
    if isinstance(exc, KeyError):
        return HTTPException(404, "not found")
    if isinstance(exc, PrerequisiteNotMetError):
        return HTTPException(409, str(exc))
    if isinstance(exc, WorkflowRequestError):
        return HTTPException(422, str(exc))
    if isinstance(exc, ValidationError):  # a request that passed the route model but not a domain contract
        first = exc.errors()[0] if exc.errors() else {}
        return HTTPException(422, f"invalid {'.'.join(str(x) for x in first.get('loc', ()))}: {first.get('msg', '')}")
    if isinstance(exc, HITLApplyError):
        return HTTPException(502, f"approval {exc.item.id} was decided but applying it failed; retry-apply is available")
    if isinstance(exc, HITLError):
        message = str(exc)
        if "already decided" in message:
            return HTTPException(409, message)
        if "lacks role" in message or "separation of duties" in message:
            return HTTPException(403, message)
        return HTTPException(422, message)
    if isinstance(exc, (OrchestratorAuthorizationError, PolicyViolation, PermissionError)):
        return HTTPException(403, str(exc))
    # The case-state store (Supabase case_orchestration_state): explicit, never a generic 500. The messages carry
    # case ids, tenants, HTTP status and SQLSTATE only (supabase_state_store never echoes row values or keys).
    if isinstance(exc, StatePersistenceUnavailable):
        return HTTPException(503, f"STATE_STORE_UNAVAILABLE: {exc} -- nothing was written; retry or resume the case")
    if isinstance(exc, CaseOwnershipError):
        return HTTPException(409, f"CASE_NOT_IN_DATABASE: {exc} (UC01 must store the case in onboarding_cases)")
    if isinstance(exc, StateVersionConflict):
        return HTTPException(409, f"STATE_VERSION_CONFLICT: {exc}")
    if isinstance(exc, StatePersistenceError):
        return HTTPException(503, f"STATE_STORE_ERROR: {exc}")
    log.error("orchestration.api.unexpected_error", extra={"error_type": type(exc).__name__})
    return HTTPException(500, "internal error")


def build_router(agent: OnboardingOrchestratorAgent, identity_provider: IdentityProvider,
                 documents: DocumentIntakeService | None = None) -> APIRouter:
    router = APIRouter(prefix="/v1/orchestration")

    def verified_actor(request: Request) -> VerifiedActor:
        """The only place request headers are read -- through the identity provider."""
        try:
            return identity_provider.authenticate(request.headers)
        except IdentityError as e:
            raise HTTPException(401, str(e)) from None

    def actor_of(va: VerifiedActor) -> dict[str, Any]:
        return {"on_behalf_of": va.user_id, "roles": list(va.roles), "client_id": va.client_id}

    def run(fn, *, role_setting: str | None = None, va: VerifiedActor, write: bool = True):
        try:
            if write and not agent.policy.enabled:
                raise PolicyViolation(f"{agent.agent_id} is disabled by its policy (kill switch)")
            if role_setting is not None:
                agent.require_role(actor_of(va), role_setting)
            return fn(actor_of(va))
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 -- mapped, never leaked
            raise _http_error(exc) from None

    def may_read_identity(actor: dict[str, Any]) -> bool:
        try:
            agent.require_role(actor, "case_identity_reader_roles")  # fails closed without an allowlist
        except OrchestratorAuthorizationError:
            return False
        return True

    @router.get("/cases", response_model=CaseListView)
    def list_cases(va: VerifiedActor = Depends(verified_actor)) -> CaseListView:
        """The caller's tenant's cases, newest first (filtered on each state's own client_id)."""
        def _list(actor):
            identity = may_read_identity(actor)
            states = [s for s in agent.list_case_states() if s.get("client_id") == va.client_id]
            states.sort(key=lambda s: s.get("updated_at") or s.get("created_at") or "", reverse=True)
            return CaseListView(client_id=va.client_id,
                                cases=[case_summary(s, include_identity=identity) for s in states])
        return run(_list, role_setting="case_reader_roles", va=va, write=False)

    # Each action the UI offers -> the policy setting that the route enforces for it.
    _PERMISSIONS = {"read_cases": "case_reader_roles", "start_case": "case_operator_roles",
                    "resume_case": "case_operator_roles", "retry_stage": "stage_retry_roles",
                    "submit_documents": "document_submitter_roles", "review_documents": "document_review_roles",
                    "view_documents": "document_viewer_roles", "read_identity": "case_identity_reader_roles",
                    "change_document_requirements": "document_requirement_change_roles",
                    "uc05_operations": "uc05_operation_roles"}

    @router.get("/me")
    def me(va: VerifiedActor = Depends(verified_actor)) -> dict[str, Any]:
        """Who the caller is and which actions the policy allows it -- read from the same settings the routes
        enforce, so the UI can explain an action instead of offering one that will be refused. Advisory: every
        route still checks for itself."""
        roles = set(va.roles)
        settings = agent.policy.settings
        required = {action: list(settings.get(key, [])) for action, key in _PERMISSIONS.items()}
        form_role = agent.policy.gates["onboarding_form_approval"].approver_role             if "onboarding_form_approval" in agent.policy.gates else None
        permissions = {action: bool(roles & set(allowed)) for action, allowed in required.items()}
        permissions["approve_forms"] = bool(form_role and form_role in roles)
        required["approve_forms"] = [form_role] if form_role else []
        return {"user_id": va.user_id, "client_id": va.client_id, "roles": list(va.roles),
                "permissions": permissions, "required_roles": required}

    @router.get("/approvals")
    def approval_inbox(va: VerifiedActor = Depends(verified_actor)) -> dict[str, Any]:
        """Every pending approval across the tenant's cases (decided through POST .../approvals/{id}/decision).
        `can_decide` mirrors the deciding inbox's own checks -- the approver role, and for a separation-of-duties
        gate that the caller is not the requester -- so the UI can say why a decision is not theirs; the inbox
        still enforces both (and UC05 its own person-specific rules) when a decision is sent."""
        def _list(actor):
            identity = may_read_identity(actor)
            roles = set(actor.get("roles") or [])
            # A team lead / buddy without case access sees only the approvals naming them as the person on record.
            sees_all = bool(roles & set(agent.policy.settings.get("case_reader_roles", [])))
            items = []
            for state in agent.list_case_states():
                if state.get("client_id") != va.client_id:
                    continue
                fa = state.get("form_approval") or {}
                trigger = state.get("intake_trigger") or {}
                role_ctx = state.get("uc02_role_context") or {}
                for p in state.get("pending_approvals") or []:
                    is_form = p.get("gate") == "onboarding_form_approval"
                    requested_by = fa.get("requested_by") if is_form else None
                    person = approver_on_record(state, p)
                    if not sees_all and not (person == va.user_id if person else p.get("role") in roles):
                        continue
                    if p.get("role") not in roles:
                        can, why = False, (f"only the {p.get('role')} on record ({person}) decides this" if person
                                           else f"requires the '{p.get('role')}' role")
                    elif person and person != va.user_id:
                        can, why = False, f"only the {p.get('role')} on record ({person}) decides this"
                    elif is_form and fa.get("separation_of_duties") and requested_by == va.user_id:
                        can, why = False, "you submitted this form; separation of duties requires another approver"
                    else:
                        can, why = True, None
                    items.append({
                        "case_id": state["case_id"], "approval_id": p.get("reference"), "agent": p.get("agent"),
                        "gate": p.get("gate"), "stage": p.get("stage"), "required_role": p.get("role"),
                        "approver_id": person,
                        "approval_type": "INTERNAL_ONBOARDING_FORM" if is_form else (p.get("proposal_type") or p.get("gate")
                                                                                   or "UC_APPROVAL"),
                        "description": p.get("description"),
                        "request": _approval_request(state, p),
                        "raised_by": _RAISED_BY.get(p.get("agent") or "", p.get("agent")),
                        "requested_at": fa.get("requested_at") if is_form else p.get("raised_at"),
                        "requested_by": (requested_by if is_form else _requester(state, p)) if identity else None,
                        "employee_name": trigger.get("candidate_name") if identity else None,
                        "employee_id": state.get("employee_id"), **employee_numbers(state),
                        "designation": trigger.get("designation"),
                        "department": role_ctx.get("department"),
                        "joining_date": state.get("joining_date") or (fa.get("form") or {}).get("joining_date"),
                        "can_decide": can, "reason": why})
            items.sort(key=lambda i: i.get("requested_at") or "")
            return {"client_id": va.client_id, "approvals": items}
        return run(_list, role_setting="approval_inbox_roles", va=va, write=False)

    @router.get("/cases/{case_id}/form-approval")
    def get_form_approval(case_id: str, va: VerifiedActor = Depends(verified_actor)) -> dict[str, Any]:
        """The internal onboarding form's approval request with the people involved (requester, decider,
        employee name) for case_identity_reader_roles; the same summary as CaseView otherwise."""
        def _read(actor):
            state = agent.get_case(case_id, client_id=va.client_id)
            return {"case_id": state["case_id"], "client_id": state["client_id"],
                    "employee_id": state.get("employee_id"),
                    "form_approval": form_approval_summary(state.get("form_approval"),
                                                           include_identity=may_read_identity(actor)),
                    "form_extraction": form_extraction_summary(state.get("form_extraction"),
                                                               include_identity=may_read_identity(actor))}
        return run(_read, role_setting="case_reader_roles", va=va, write=False)

    @router.get("/cases/{case_id}/profile", response_model=CaseProfileView)
    def get_profile(case_id: str, va: VerifiedActor = Depends(verified_actor)) -> CaseProfileView:
        def _read(actor):
            state = agent.get_case(case_id, client_id=va.client_id)
            return CaseProfileView(case_id=state["case_id"], client_id=state["client_id"],
                                   employee_id=state.get("employee_id"), joining_date=state.get("joining_date"),
                                   profile=case_profile(state, include_identity=may_read_identity(actor)))
        return run(_read, role_setting="case_reader_roles", va=va, write=False)

    @router.post("/cases/{case_id}/documents/{document_type}/upload", response_model=DocumentUploadResult)
    async def upload_document(case_id: str, document_type: str, request: Request,
                              va: VerifiedActor = Depends(verified_actor)) -> DocumentUploadResult:
        """The request body is the file itself (Content-Type: its media type; optional X-File-Name,
        URL-encoded). Stored in Supabase Storage, read back, extracted by UC03's extractor and submitted
        through the orchestrator's UC03 path (documents.py). The role (document_submitter_roles) and the
        tenant are checked before the file is stored."""
        if documents is None:
            raise HTTPException(503, "DOCUMENT_STORAGE_NOT_CONFIGURED: set DOCUMENT_STORAGE_BUCKET on the backend")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > documents.max_bytes:
            raise HTTPException(413, f"FILE_TOO_LARGE: the file exceeds {documents.max_bytes} bytes")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > documents.max_bytes:
                raise HTTPException(413, f"FILE_TOO_LARGE: the file exceeds {documents.max_bytes} bytes")
        file_name = unquote(request.headers.get("x-file-name") or "")[:300]
        content_type = request.headers.get("content-type") or ""

        def _upload(actor):
            state, receipt = documents.upload(agent, case_id=case_id, document_type=document_type, data=bytes(body),
                                              content_type=content_type, file_name=file_name, actor=actor)
            return DocumentUploadResult(case=case_view(state), upload=receipt.view())
        return await run_in_threadpool(run, _upload, va=va)

    @router.get("/cases/{case_id}/documents/{document_type}/url", response_model=DocumentUrlView)
    def document_url(case_id: str, document_type: str, va: VerifiedActor = Depends(verified_actor)) -> DocumentUrlView:
        """A signed URL (60 s) to the latest stored revision. Role: document_viewer_roles (checked inside)."""
        if documents is None:
            raise HTTPException(503, "DOCUMENT_STORAGE_NOT_CONFIGURED: set DOCUMENT_STORAGE_BUCKET on the backend")

        def _sign(actor):
            out = documents.signed_url(agent, case_id=case_id, document_type=document_type, actor=actor)
            return DocumentUrlView(case_id=case_id, **out)
        return run(_sign, va=va, write=False)

    @router.get("/local-documents/{token}", include_in_schema=False)
    def local_document(token: str):
        """Development local storage only (DOCUMENT_STORAGE_BACKEND=local): serves the file a signed, expiring link
        names -- the link issued by the route above after its role and case checks, like a Supabase signed URL."""
        from fastapi.responses import Response

        from hr_onboarding_common.orchestration.documents import LocalDiskObjectStorage

        if documents is None or not isinstance(documents.storage, LocalDiskObjectStorage):
            raise HTTPException(404, "not found")
        try:
            data, media = documents.storage.open_signed(token)
        except Exception as exc:  # noqa: BLE001 -- mapped, never leaked
            raise _http_error(exc) from None
        return Response(content=data, media_type=media, headers={"Content-Disposition": "inline",
                                                                  "Cache-Control": "no-store"})

    @router.post("/onboarding-documents/extract")
    async def extract_onboarding_document(request: Request, va: VerifiedActor = Depends(verified_actor)):
        """Start New Onboarding from a document. The body is the onboarding form itself (.docx or text-based .pdf;
        Content-Type its media type; X-File-Name URL-encoded). UC01 extracts and validates it (UC01 worker,
        onboarding_documents.py); nothing is stored and no case is opened. The answer: READY_FOR_REVIEW or
        VALIDATION_REQUIRED with UC01's safe field view (no secret values) and a prefill of the form; a refused file
        is FAILED (4xx/5xx) with `error {code, reason, action}`. Role: case_operator_roles, checked before the body
        is read."""
        limit = onboarding_documents.max_bytes()

        def failed(exc: OnboardingDocumentError) -> JSONResponse:
            return JSONResponse(status_code=exc.http_status,
                                content={"detail": f"{exc.code}: {exc.reason}", **exc.view(limit)})

        run(lambda actor: None, role_setting="case_operator_roles", va=va)  # kill switch and role first
        if not onboarding_documents.enabled():
            return failed(OnboardingDocumentError("EXTRACTION_DISABLED", "document extraction is switched off",
                                                  http_status=503))
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            return failed(OnboardingDocumentError("FILE_TOO_LARGE", f"the file exceeds {limit} bytes", http_status=413))
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                return failed(OnboardingDocumentError("FILE_TOO_LARGE", f"the file exceeds {limit} bytes",
                                                      http_status=413))
        file_name = unquote(request.headers.get("x-file-name") or "")[:300]
        content_type = request.headers.get("content-type") or ""

        def _extract(actor):
            try:
                view = onboarding_documents.extract(agent, data=bytes(body), filename=file_name,
                                                    content_type=content_type, actor=actor)
            except OnboardingDocumentError as exc:
                log.info("orchestration.onboarding_document.refused", extra={"code": exc.code})
                return failed(exc)
            log.info("orchestration.onboarding_document.extracted",
                     extra={"extraction_id": view["extraction_id"], "status": view["status"],
                            "sha256": view["source_document"].get("sha256")})
            return view
        return await run_in_threadpool(run, _extract, role_setting="case_operator_roles", va=va)

    @router.post("/cases", response_model=CaseView, status_code=201)
    def start_case(body: StartCaseRequest, va: VerifiedActor = Depends(verified_actor)) -> CaseView:
        def _start(actor):
            fields = body.model_dump(exclude={"source_extraction_id", "extraction_reviewed"})
            fields["joining_date"] = body.joining_date.isoformat() if body.joining_date else None
            if body.source_extraction_id:
                # The server's own record of the extraction, checked against this tenant and HR's review.
                fields["form_extraction"] = onboarding_documents.provenance_for_case(
                    agent, extraction_id=body.source_extraction_id, client_id=va.client_id, submitted=fields,
                    reviewed=body.extraction_reviewed, actor=actor)
            # Submitting a form never approves it. Approval facts on the request are accepted only from a
            # trusted integration relaying UC01's own decision (form_approval_attestor_roles, fail closed);
            # a person's form is sent to the onboarding_form_approval gate instead.
            attested = bool(body.form_approved_at or body.form_approved_by_role)
            if attested:
                agent.require_role(actor, "form_approval_attestor_roles")
            inp = OnboardingCaseInput(client_id=va.client_id, hr_officer_id=va.user_id,
                                      form_approval_required=not attested, **fields)
            return case_view(agent.advance_case(inp, case_id=None, actor=actor))
        return run(_start, role_setting="case_operator_roles", va=va)

    @router.get("/cases/{case_id}", response_model=CaseView)
    def get_case(case_id: str, va: VerifiedActor = Depends(verified_actor)) -> CaseView:
        return run(lambda actor: case_view(agent.get_case(case_id, client_id=va.client_id)),
                   role_setting="case_reader_roles", va=va, write=False)

    @router.post("/cases/{case_id}/resume", response_model=CaseView)
    def resume_case(case_id: str, va: VerifiedActor = Depends(verified_actor)) -> CaseView:
        return run(lambda actor: case_view(agent.resume_case(case_id, actor=actor)),
                   role_setting="case_operator_roles", va=va)

    @router.post("/cases/{case_id}/approvals/{approval_id}/decision", response_model=CaseView)
    def decide(case_id: str, approval_id: str, body: DecisionRequest,
               va: VerifiedActor = Depends(verified_actor)) -> CaseView:
        return run(lambda actor: case_view(agent.decide_approval(
            case_id, approval_id, decision=body.decision, actor=actor,
            reason_code=body.reason_code.value if body.reason_code else None, note=body.note,
            edited_payload=body.edited_payload)), va=va)

    @router.post("/cases/{case_id}/approvals/{approval_id}/retry-apply", response_model=CaseView)
    def retry_apply(case_id: str, approval_id: str, va: VerifiedActor = Depends(verified_actor)) -> CaseView:
        return run(lambda actor: case_view(agent.retry_approval(case_id, approval_id, actor=actor)), va=va)

    @router.post("/cases/{case_id}/joining-day", response_model=CaseView)
    def joining_day(case_id: str, body: JoiningDayRequest, va: VerifiedActor = Depends(verified_actor)) -> CaseView:
        return run(lambda actor: case_view(agent.trigger_joining_day(case_id, as_of=body.as_of, actor=actor)),
                   va=va)  # role checked inside (joining_day_trigger_roles)

    @router.post("/cases/{case_id}/task-planning/date-change", response_model=OperationResult)
    def uc02_date_change(case_id: str, body: DateChangeRequest,
                         va: VerifiedActor = Depends(verified_actor)) -> OperationResult:
        """Open UC02's real DATE_CHANGE gate (opens an approval; nothing is re-planned yet). Decided
        through the existing POST .../approvals/{id}/decision route, like any other approval."""
        def _request(actor):
            state, outcome = agent.request_uc02_date_change(case_id, joining_date=body.joining_date, actor=actor)
            return OperationResult(case=case_view(state), outcome=outcome)
        return run(_request, va=va)  # role checked inside (uc02_date_change_roles)

    @router.post("/cases/{case_id}/orientation/refresh-details", response_model=OperationResult)
    def refresh_uc05_details(case_id: str, va: VerifiedActor = Depends(verified_actor)) -> OperationResult:
        """Re-read UC05's session, participant and buddy details into the case's UC05 results (display only)."""
        def _refresh(actor):
            state, outcome = agent.refresh_uc05_details(case_id, actor=actor)
            return OperationResult(case=case_view(state), outcome=outcome)
        return run(_refresh, va=va)  # role checked inside (uc05_operation_roles)

    @router.get("/cases/{case_id}/welcome-message")
    def welcome_message(case_id: str, va: VerifiedActor = Depends(verified_actor)) -> dict[str, Any]:
        """The joining-day welcome (UC05's draft, or a preview before the joining day). Names the joiner: only
        case_identity_reader_roles."""
        return run(lambda actor: agent.welcome_message(case_id, actor=actor), va=va, write=False)

    @router.post("/cases/{case_id}/orientation/operations", response_model=OperationResult)
    def uc05_operation(case_id: str, body: Uc05OperationRequest,
                       va: VerifiedActor = Depends(verified_actor)) -> OperationResult:
        params = body.model_dump(exclude={"operation"}, exclude_none=True)
        if body.start is not None:
            params["start"] = body.start.isoformat()

        def _operate(actor):
            state, outcome = agent.request_uc05_operation(case_id, operation=body.operation, params=params, actor=actor)
            return OperationResult(case=case_view(state), outcome=outcome)
        return run(_operate, va=va)  # role checked inside (uc05_operation_roles)

    @router.post("/cases/{case_id}/sessions/{session_id}/responses", response_model=OperationResult)
    def participant_response(case_id: str, session_id: str, body: ParticipantResponseRequest,
                             va: VerifiedActor = Depends(verified_actor)) -> OperationResult:
        """UC05 decides who may answer (the participant themself, or hr_admin on their behalf)."""
        def _respond(actor):
            state, outcome = agent.record_participant_response(
                case_id, session_id=session_id, participant_id=body.participant_id, response=body.response,
                responded_at=body.responded_at.isoformat() if body.responded_at else None, actor=actor)
            return OperationResult(case=case_view(state), outcome=outcome)
        return run(_respond, va=va)

    @router.post("/cases/{case_id}/equipment-access/requests", response_model=CaseView)
    def equipment_access_requests(case_id: str, body: EquipmentAccessRequestsBody,
                                  va: VerifiedActor = Depends(verified_actor)) -> CaseView:
        """Resolves UC04's NO_ITEMS_REQUESTED: the requests are recorded, and a blocked UC04 stage re-runs."""
        return run(lambda actor: case_view(agent.record_equipment_access_requests(
            case_id, requested_equipment=body.requested_equipment, requested_access=body.requested_access,
            actor=actor)), va=va)  # role checked inside (case_operator_roles)

    @router.post("/cases/{case_id}/orientation/offer-event", response_model=CaseView)
    def offer_event(case_id: str, body: OfferEventBody, va: VerifiedActor = Depends(verified_actor)) -> CaseView:
        """Resolves UC05_OFFER_EVENT_MISSING in UC05's mailbox mode; UC05 verifies the event."""
        return run(lambda actor: case_view(agent.record_offer_acceptance_event(
            case_id, event_id=body.offer_acceptance_event_id, actor=actor)), va=va)  # role checked inside

    @router.post("/cases/{case_id}/documents", response_model=CaseView)
    def submit_documents(case_id: str, body: SubmitDocumentsRequest,
                         va: VerifiedActor = Depends(verified_actor)) -> CaseView:
        documents = [d.model_dump(mode="json") for d in body.documents]
        return run(lambda actor: case_view(agent.submit_documents(case_id, documents=documents, actor=actor)),
                   va=va)  # role checked inside (document_submitter_roles), before anything is read

    @router.post("/cases/{case_id}/documents/reviews", response_model=CaseView)
    def review_documents(case_id: str, body: ReviewDocumentsRequest,
                         va: VerifiedActor = Depends(verified_actor)) -> CaseView:
        reviews = [r.model_dump(mode="json") for r in body.reviews]
        return run(lambda actor: case_view(agent.review_documents(case_id, reviews=reviews, actor=actor)),
                   va=va)  # role checked inside (document_review_roles)

    @router.post("/cases/{case_id}/documents/requirements", response_model=CaseView)
    def change_document_requirements(case_id: str, body: RequirementChangeBody,
                                     va: VerifiedActor = Depends(verified_actor)) -> CaseView:
        return run(lambda actor: case_view(agent.request_document_requirement_change(
            case_id, add_documents=body.add_documents, remove_documents=body.remove_documents, reason=body.reason,
            actor=actor)), va=va)  # role checked inside (document_requirement_change_roles)

    @router.post("/cases/{case_id}/stages/{stage}/retry", response_model=CaseView)
    def retry_stage(case_id: str, stage: str, va: VerifiedActor = Depends(verified_actor)) -> CaseView:
        return run(lambda actor: case_view(agent.retry_stage(case_id, stage, actor=actor)),
                   va=va)  # role checked inside (stage_retry_roles)

    @router.get("/cases/{case_id}/pending-actions", response_model=PendingActionsView)
    def pending_actions(case_id: str, va: VerifiedActor = Depends(verified_actor)) -> PendingActionsView:
        def _read(actor):
            state = agent.get_case(case_id, client_id=va.client_id)
            return PendingActionsView(case_id=state["case_id"], workflow_status=str(state.get("workflow_status")),
                                      overall_status=str(state.get("overall_status")),
                                      current_stage=str(state.get("current_stage")),
                                      pending_actions=list(state.get("pending_actions") or []),
                                      blockers=list(state.get("blockers") or []),
                                      pending_approvals=list(state.get("pending_approvals") or []))
        return run(_read, role_setting="case_reader_roles", va=va, write=False)

    @router.post("/cases/{case_id}/events", response_model=EventResult)
    def case_event(case_id: str, body: CaseEventRequest, va: VerifiedActor = Depends(verified_actor)) -> EventResult:
        """Applied at most once per event_id; each type's own role check runs inside (documents:
        document_submitter_roles, joining_day: joining_day_trigger_roles, resume: case_operator_roles; an
        RSVP is checked by UC05)."""
        def _apply(actor):
            state, event = agent.handle_event(case_id, event_id=body.event_id, event_type=body.type,
                                              payload=body.payload, actor=actor)
            return EventResult(case=case_view(state), event=event)
        return run(_apply, va=va)

    @router.get("/cases/{case_id}/stages", response_model=StageExecutionsView)
    def stage_executions(case_id: str, va: VerifiedActor = Depends(verified_actor)) -> StageExecutionsView:
        return run(lambda actor: StageExecutionsView(**agent.stage_executions(case_id, client_id=va.client_id)),
                   role_setting="case_reader_roles", va=va, write=False)

    @router.get("/cases/{case_id}/stages/{stage}", response_model=StageExecutionView)
    def stage_execution(case_id: str, stage: str, va: VerifiedActor = Depends(verified_actor)) -> StageExecutionView:
        def _read(actor):
            views = agent.stage_executions(case_id, client_id=va.client_id)
            if stage not in views["stages"]:
                raise WorkflowRequestError(f"unknown stage '{stage}'")
            return StageExecutionView(case_id=views["case_id"], execution=views["stages"][stage])
        return run(_read, role_setting="case_reader_roles", va=va, write=False)

    @router.get("/cases/{case_id}/errors", response_model=ErrorsView)
    def case_errors(case_id: str, va: VerifiedActor = Depends(verified_actor)) -> ErrorsView:
        def _read(actor):
            state = agent.get_case(case_id, client_id=va.client_id)
            return ErrorsView(case_id=state["case_id"], workflow_status=state.get("workflow_status"),
                              errors=execution.case_errors(state))
        return run(_read, role_setting="case_reader_roles", va=va, write=False)

    @router.get("/cases/{case_id}/approvals", response_model=ApprovalsView)
    def approvals(case_id: str, va: VerifiedActor = Depends(verified_actor)) -> ApprovalsView:
        def _read(actor):
            state = agent.get_case(case_id, client_id=va.client_id)
            return ApprovalsView(case_id=state["case_id"], pending_approvals=list(state.get("pending_approvals") or []),
                                 decisions=list(state.get("approval_decisions") or []))
        return run(_read, role_setting="case_reader_roles", va=va, write=False)

    @router.get("/dependencies", response_model=DependenciesView)
    def dependency_registry(va: VerifiedActor = Depends(verified_actor)) -> DependenciesView:
        return run(lambda actor: DependenciesView(client_id=va.client_id, dependencies=dependencies.dependency_graph(
            va.client_id, agent.deps.tenants)), role_setting="case_reader_roles", va=va, write=False)

    return router


def create_orchestrator_app(*, agent: OnboardingOrchestratorAgent, identity_provider: IdentityProvider,
                            service: str = "hr-onboarding-orchestrator",
                            documents: DocumentIntakeService | None = None,
                            cors_origins: list[str] | None = None,
                            runtime_info: dict[str, Any] | None = None) -> FastAPI:
    """`cors_origins`: browser origins allowed to call the API (none by default). `runtime_info`: what
    /healthz reports about how this process runs (mode, state store) -- names only, never a secret."""
    app = FastAPI(title=service, version=agent.version)
    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(CORSMiddleware, allow_origins=list(cors_origins), allow_credentials=False,
                           allow_methods=["GET", "POST", "OPTIONS"],
                           allow_headers=["Content-Type", "X-User-Id", "X-Client-Id", "X-User-Roles", "X-File-Name"])

    if hasattr(identity_provider, "identities"):
        @app.get("/v1/orchestration/dev/identities")
        def dev_identities() -> dict[str, Any]:
            """Development only: the server-side identity registry the UI's identity picker shows."""
            return {"identities": identity_provider.identities()}

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        policy = agent.policy
        return {"status": "ok", "agent": agent.agent_id, "version": agent.version,
                "policy": f"{policy.policy}@v{policy.version}", "enabled": policy.enabled,
                "uc04_identity_profile": (os.environ.get("UC04_IDENTITY_PROFILE") or "LOCAL_SIMULATION").upper(),
                "uc05_offer_event_source": getattr(agent.deps, "uc05_offer_event_source", "mailbox"),
                # Where UC05's worker reads buddy candidates (workers/uc_worker_runtime.py): local or supabase.
                "uc05_buddy_source": (os.environ.get("UC05_BUDDY_SOURCE") or "local").strip().lower(),
                "uc05_welcome_config_source": (os.environ.get("UC05_WELCOME_CONFIG_SOURCE") or "local").strip().lower(),
                "document_storage": ({"configured": True, "bucket": documents.storage.bucket, **documents.limits}
                                     if documents is not None else {"configured": False}),
                # Start New Onboarding from a document (UC01's extractor in the UC01 worker; nothing stored).
                "onboarding_document_extraction": {"enabled": onboarding_documents.enabled(),
                                                   "max_bytes": onboarding_documents.max_bytes(),
                                                   "formats": sorted(onboarding_documents.EXTENSIONS)},
                **(runtime_info or {})}

    app.include_router(build_router(agent, identity_provider, documents))
    return app
