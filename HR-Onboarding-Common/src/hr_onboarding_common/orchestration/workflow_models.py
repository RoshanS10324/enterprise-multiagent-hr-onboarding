"""Typed contract (pydantic) of the orchestrator's workflow state.

The LangGraph channel schema stays the `OnboardingCaseState` TypedDict (state.py): every adapter
reads and writes it as a dict, and the post-verification branches need a reducer on one channel.
These models are its typed contract. Each structured record the workflow writes is built from one
of them (`.model_dump(mode="json")`), and `validate_workflow_state` checks the whole state on every
write-through to the case store, so a malformed state is never persisted.

Everything here is ids, codes, statuses and timestamps. Document *contents* never enter the state;
the extraction metadata UC03 needs (`DocumentMetadata`) stays in the state only and is excluded
from logs, audit events and API responses.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hr_onboarding_common.orchestration.agent_contract import AgentStatus, AgentWarning, DependencyCheck
from hr_onboarding_common.orchestration.state import Branch, StageStatus, WorkflowStatus

DATA_ORIGIN = "synthetic"  # no real-time API exists in this project: every outcome is synthetic or mocked


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequiredDocument(_Record):
    """One document a plan version requires. `required` is "mandatory" in the UC02/UC03 contract."""

    document_type: str
    label: str
    required: bool = True
    due_date: str | None = None  # ISO date: the plan's document-collection deadline
    reason: str | None = None  # why the plan requires it (ids and codes only)


# ----------------------------------------------------------------- UC02 <-> UC03 plan contract
# docs/UC02_UC03_REPLANNING_CONTRACT.md. UC02 owns the plan (OnboardingPlanRecord, versioned); UC03 owns
# document collection and validation and reports back with DocumentValidationFeedback; UC02 answers
# with a RevisedPlan (the next version), which UC03 applies through a new DocumentCollectionRequest.


class OnboardingPlanRecord(_Record):
    """UC02's current onboarding plan as the workflow sees it. `plan_id` is UC02's onboarding id."""

    plan_id: str
    plan_version: int = Field(ge=1)
    previous_plan_version: int | None = None
    required_documents: list[RequiredDocument]
    effective_joining_date: str | None = None
    task_count: int = 0
    attributes: list[str] = Field(default_factory=list)  # UC02 applicability attributes the plan was made with
    source: str = "uc02"  # where the plan came from: "uc02" (the real UC02), or a named test double
    created_at: str
    replanning_reason: str | None = None
    applied_feedback_ids: list[str] = Field(default_factory=list)


class PlanHistoryEntry(_Record):
    plan_version: int
    previous_plan_version: int | None = None
    action: Literal["initial", "replanned", "date_change"]
    reason: str | None = None
    feedback_id: str | None = None
    added_documents: list[str] = Field(default_factory=list)
    removed_documents: list[str] = Field(default_factory=list)
    added_tasks: list[str] = Field(default_factory=list)
    removed_tasks: list[str] = Field(default_factory=list)
    modified_tasks: list[str] = Field(default_factory=list)
    at: str


class DocumentCollectionRequest(_Record):
    """UC02 -> UC03: what one plan version requires (built from the plan, sent to UC03's collection)."""

    case_id: str
    tenant_id: str
    employee_id: str
    correlation_id: str
    plan_id: str
    plan_version: int = Field(ge=1)
    joining_date: str | None = None
    required_documents: list[str]
    document_requirements: list[RequiredDocument]
    mandatory_documents: list[str]
    validation_rules: dict[str, dict[str, Any]] = Field(default_factory=dict)
    due_dates: dict[str, str | None] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecommendedTaskChange(_Record):
    action: Literal["add_document", "remove_document", "add_attribute"]
    target: str  # a document type or an applicability attribute
    reason: str


class DocumentValidationFeedback(_Record):
    """UC03 -> UC02: the structured outcome of validating against one plan version."""

    feedback_id: str  # deterministic per (case, plan version, requested change): UC02's replan is idempotent on it
    case_id: str
    tenant_id: str
    employee_id: str | None = None
    plan_id: str | None = None
    plan_version: int | None = None
    validation_status: Literal["verified", "correction_required", "hr_review_pending", "blocked",
                               "replanning_required", "awaiting_documents"]
    verified_documents: list[str] = Field(default_factory=list)
    missing_documents: list[str] = Field(default_factory=list)
    invalid_documents: list[str] = Field(default_factory=list)
    expired_documents: list[str] = Field(default_factory=list)
    correction_required: bool = False
    replanning_required: bool = False
    replanning_reasons: list[str] = Field(default_factory=list)
    recommended_task_changes: list[RecommendedTaskChange] = Field(default_factory=list)
    pending_hr_reviews: list[str] = Field(default_factory=list)
    change_request_ids: list[str] = Field(default_factory=list)  # HR requirement changes this feedback carries
    document_revision: dict[str, int | None] = Field(default_factory=dict)  # document type -> revision validated
    metadata: dict[str, Any] = Field(default_factory=dict)


class RevisedPlan(_Record):
    """UC02 -> UC03: the plan after a replan."""

    plan_id: str
    plan_version: int = Field(ge=1)
    previous_plan_version: int
    updated_required_documents: list[RequiredDocument]
    added_tasks: list[str] = Field(default_factory=list)
    removed_tasks: list[str] = Field(default_factory=list)
    modified_tasks: list[str] = Field(default_factory=list)
    updated_validation_requirements: dict[str, dict[str, Any]] = Field(default_factory=dict)
    effective_joining_date: str | None = None
    replanning_reason: str
    feedback_id: str
    duplicate_replay: bool = False


class RequirementChangeRequest(_Record):
    """An HR change to the document requirements, raised to UC03 and applied by a UC02 replan."""

    request_id: str
    add_documents: list[str] = Field(default_factory=list)
    remove_documents: list[str] = Field(default_factory=list)
    reason: str = Field(max_length=200)
    requested_by: str
    requested_at: str
    plan_version: int  # the plan version it was requested against
    status: Literal["pending", "applied"] = "pending"
    applied_in_plan_version: int | None = None


class DocumentRequestNotification(_Record):
    """The (mock) request sent to the employee. `idempotency_key` is fixed per case and plan version,
    so a retry can never become a second, different request."""

    idempotency_key: str
    status: Literal["not_sent", "sending", "sent", "failed", "uncertain", "not_required"] = "not_sent"
    transport: str = "mock-notification"
    synthetic: bool = True
    recipient: str  # an opaque employee reference, never an address
    message_id: str | None = None
    attempts: int = 0
    last_attempt_at: str | None = None
    detail: str | None = None
    retry_authorized: bool = False  # set only by an explicit, authorized retry of an uncertain send
    document_types: list[str] = Field(default_factory=list)  # what this round asked for


class OutstandingDocument(_Record):
    document_type: str
    reason: Literal["not_submitted", "correction_required", "hr_review_rejected"]


class DocumentCollectionRecord(_Record):
    """UC03's open collection for one plan version (the projection the workflow routes on)."""

    required_documents: list[RequiredDocument]
    source: str  # where the requirement list came from: "uc02_plan" (legacy cases: "uc03_checklist")
    plan_id: str | None = None
    plan_version: int | None = None
    requested_at: str | None = None
    notification: DocumentRequestNotification | None = None
    # One notification per plan version: earlier rounds are kept for the audit trail.
    previous_notifications: list[DocumentRequestNotification] = Field(default_factory=list)
    request: DocumentCollectionRequest | None = None
    outstanding: list[OutstandingDocument] = Field(default_factory=list)


class DocumentMetadata(_Record):
    """What UC03's DocumentValidationRequest needs about a submitted document, as the document
    intake system extracted it. Kept in state only; never logged, audited or returned."""

    readable: bool | None = None
    required_fields_present: bool | None = None
    expiry_date: date | None = None
    evidence_stored: bool = False
    extracted_name: str | None = Field(default=None, max_length=200)
    extraction_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    attestation_confirmed: bool = False  # UC03's own input for its attestation-checked document types
    # Facts the document establishes that change what else the employee needs (UC03's catalog rules
    # `visa_required:true` / `sponsorship:true`): never inferred, only as extracted.
    visa_required: bool | None = None
    sponsorship_applicable: bool | None = None


class DocumentReview(_Record):
    """An authenticated HR reviewer's decision on one revision -- UC03's `hr_review_status` input.
    The orchestrator records it; it never decides it."""

    decision: Literal["approved", "rejected"]
    revision: int
    reviewer: str
    reviewed_at: str
    note: str | None = None


_DOCUMENT_TYPE = r"^[a-z][a-z0-9_]{0,63}$"


class DocumentSubmission(_Record):
    """One document supplied (uploaded) or marked available from records on file: a reference and
    the extraction metadata only. There is no field for document contents (unknown fields are
    refused)."""

    document_type: str = Field(pattern=_DOCUMENT_TYPE)
    document_ref: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    source: Literal["uploaded", "on_file"] = "uploaded"
    metadata: DocumentMetadata = Field(default_factory=DocumentMetadata)


class DocumentReviewRequest(_Record):
    document_type: str = Field(pattern=_DOCUMENT_TYPE)
    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=500)


class SubmittedDocument(_Record):
    document_type: str
    document_ref: str  # opaque reference to the stored evidence
    source: Literal["uploaded", "on_file"] = "uploaded"
    revision: int = Field(ge=1)
    submitted_at: str
    submitted_by: str
    metadata: DocumentMetadata = Field(default_factory=DocumentMetadata)
    hr_review: DocumentReview | None = None
    # UC03's storage record of this revision and the plan version it was collected under
    # (absent on a document submitted before UC03 owned storage).
    storage_id: str | None = None
    plan_version: int | None = None


class ValidationIssue(_Record):
    document_type: str
    revision: int | None = None
    findings: list[str]
    document_status: str | None = None
    compliance_item_status: str | None = None
    required_next_action: str | None = None


class CorrectionRequirement(_Record):
    document_type: str
    revision: int | None = None
    action: Literal["supply", "resubmit", "hr_review", "resolve"]
    reason_codes: list[str]
    detail: str


class BranchStatusRecord(_Record):
    status: WorkflowStatus
    stages: dict[str, str]
    next_action: str | None = None
    error: dict[str, Any] | None = None


class StageOutcome(_Record):
    """The structured result of one stage run: which operation, what was passed in (ids and flags
    only), where its output lives in the state, and how it ended. It is the persisted projection of
    that run's `agent_contract.UCAgentResponse`: the same ids, statuses, errors, warnings and
    dependency checks -- the UC's output itself stays in the stage's result field (`result_key`)."""

    stage: str
    uc: str
    operation: str
    status: StageStatus
    attempt: int
    started_at: str
    finished_at: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    result_key: str | None = None
    next_action: str | None = None
    error: dict[str, Any] | None = None
    data_origin: Literal["synthetic"] = DATA_ORIGIN
    # Added with the standard UC contract (agent_contract.py); absent on outcomes recorded before it.
    canonical_status: AgentStatus | None = None
    execution_id: str | None = None
    trace_id: str | None = None
    correlation_id: str | None = None
    dependencies: list[DependencyCheck] = Field(default_factory=list)
    warnings: list[AgentWarning] = Field(default_factory=list)
    updated_fields: list[str] = Field(default_factory=list)
    # A compact, queryable summary of the stage's result (workflow.stage_result_summary): ids, versions, counts and
    # codes only. Set for UC-02's plan_tasks / replan_tasks (plan revision, task/required/dependency counts, store);
    # None for the other stages and on outcomes recorded before it existed. The full result stays in `result_key`.
    result_summary: dict[str, Any] | None = None
    # The stage's typed handoff (handoff_contracts.py: UC01Output .. UC05Output); None for orchestrator stages and on
    # outcomes recorded before it existed.
    handoff: dict[str, Any] | None = None


class RetryMetadata(_Record):
    failures: int = 0  # technical failures counted against max_attempts
    max_attempts: int
    last_code: str | None = None
    last_failure_at: str | None = None
    retries_authorized: int = 0
    last_retry_at: str | None = None
    exhausted: bool = False


class PendingAction(_Record):
    action: Literal["supply_documents", "correct_documents", "review_documents", "decide_approval",
                    "retry_stage", "resolve_blocker", "await_event", "escalate"]
    stage: str
    owner: str | None = None
    reference: str | None = None
    agent: str | None = None
    documents: list[str] = Field(default_factory=list)
    reason_code: str | None = None
    detail: str | None = None
    due: str | None = None


class WorkflowStateModel(BaseModel):
    """The typed contract of a persisted `OnboardingCaseState`. Fields the older adapters own
    (UC05 projections, intake fields) are allowed through unchanged (`extra="allow"`)."""

    model_config = ConfigDict(extra="allow")

    case_id: str
    client_id: str
    correlation_id: str
    current_stage: str
    overall_status: str
    workflow_status: WorkflowStatus
    stage_statuses: dict[str, StageStatus]
    branch_statuses: dict[Branch, BranchStatusRecord] = Field(default_factory=dict)
    completed_stages: list[str] = Field(default_factory=list)
    pending_actions: list[PendingAction] = Field(default_factory=list)
    onboarding_plan: OnboardingPlanRecord | None = None
    plan_history: list[PlanHistoryEntry] = Field(default_factory=list)
    revised_plan: RevisedPlan | None = None
    requirement_change_requests: list[RequirementChangeRequest] = Field(default_factory=list)
    document_validation_feedback: DocumentValidationFeedback | None = None
    document_collection: DocumentCollectionRecord | None = None
    submitted_documents: dict[str, SubmittedDocument] = Field(default_factory=dict)
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    correction_requirements: list[CorrectionRequirement] = Field(default_factory=list)
    verified_context: dict[str, Any] | None = None
    stage_outcomes: dict[str, StageOutcome] = Field(default_factory=dict)
    retry_metadata: dict[str, RetryMetadata] = Field(default_factory=dict)
    idempotency_keys: dict[str, str] = Field(default_factory=dict)
    stage_errors: dict[str, dict[str, Any]] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


def validate_workflow_state(state: dict[str, Any]) -> WorkflowStateModel:
    """Raises pydantic.ValidationError when the state breaks its typed contract."""
    return WorkflowStateModel.model_validate(state)
