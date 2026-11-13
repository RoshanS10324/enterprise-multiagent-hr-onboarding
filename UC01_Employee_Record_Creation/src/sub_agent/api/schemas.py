"""Request/response models for the HTTP API. No endpoint response model
here carries salary, email, or discrepancy values - only ids, field
NAMES, counts, and statuses.
"""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator

from ..audit_integrity import ChainViolation
from ..compare_sources import DiscrepancyDecision
from ..models import (
    ActorType,
    AuditEntry,
    Checkpoint,
    DiscrepancyStatus,
    HrisRecordStatus,
    HrisSystem,
    WorkLocationType,
)


class VariantFlagsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_rehire: bool = False
    is_internal_transfer: bool = False
    is_agency_contractor: bool = False
    is_minor: bool = False
    is_uae_resident: bool = False
    is_remote: bool = False


class OnboardingRequestBody(BaseModel):
    """System-entry body for POST /internal/onboarding-requests.

    The approval is an attestation from the upstream caller (open
    question OQ-01/Q-11: who approves the form). UC-01 never approves a
    form itself: if form_status is APPROVED, the caller is asserting that
    approval already happened (with approved_by/approved_at as evidence of
    it) and the form is built already-approved; for any other status the
    form is built in that status and passed through unchanged, so the
    start gate correctly holds it.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: Optional[str] = None
    client_id: str
    hr_officer_id: str
    offer_accepted: bool
    candidate_name: str
    candidate_email: str
    designation: str
    joining_date: date
    work_location_type: WorkLocationType
    client_site_id: Optional[str] = None
    salary_and_terms: SecretStr
    form_status: str
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    hiring_freeze: bool = False
    id_number: Optional[str] = None
    declared_variants: Optional[VariantFlagsBody] = None

    # Placement attributes (approved decisions, 2026-11-04).
    # location_name is the ONE new required caller field; the rest are
    # optional or defaulted. Validation lives on the Case model so the
    # same rules apply however a case is created.
    location_name: str
    department: Optional[str] = None
    grade: Optional[str] = None
    nationality_group: Optional[str] = None
    project_name: Optional[str] = None
    current_stage: Optional[str] = None
    onboarding_attributes: Optional[list[str]] = None

    @model_validator(mode="after")
    def _approved_requires_attestation(self) -> "OnboardingRequestBody":
        if self.form_status not in ("APPROVED", "AWAITING_APPROVAL", "DRAFT"):
            raise ValueError('form_status must be "APPROVED", "AWAITING_APPROVAL", or "DRAFT"')
        if self.form_status == "APPROVED" and (self.approved_by is None or self.approved_at is None):
            raise ValueError("approved_by and approved_at are required when form_status is APPROVED")
        if self.approved_at is not None and self.approved_at.tzinfo is None:
            raise ValueError("approved_at must be timezone-aware")
        return self


class OnboardingResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: str
    case_id: Optional[str] = None
    employee_no: Optional[str] = None
    approvals_created: list[str] = []
    existing_case_id: Optional[str] = None
    reasons: list[str] = []


class SourceSnapshotBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    values: dict[str, str]


class CompareRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshots: list[SourceSnapshotBody]


class DiscrepancyDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: DiscrepancyDecision


class DiscrepancyDecisionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    discrepancy_id: str
    field: str
    other_source: str
    status: DiscrepancyStatus
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None


class FormChangeBody(BaseModel):
    """requested_by is deliberately NOT a field here: it always comes from
    the authenticated viewer, never from the request body. Sending it
    anyway is rejected (extra="forbid"), not silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    change_reason: str
    approver_id: str
    candidate_name: Optional[str] = None
    designation: Optional[str] = None
    joining_date: Optional[date] = None
    work_location_type: Optional[WorkLocationType] = None
    client_site_id: Optional[str] = None
    salary_and_terms: Optional[SecretStr] = None
    source_discrepancy_id: Optional[str] = None


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    note: str = ""


class FormDecisionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    approved: bool
    form_version: int
    case_id: str


class VariantDecisionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    approval_id: str
    checkpoint: Checkpoint
    status: str
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None


class HrisRecordSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    record_id: str
    system: HrisSystem
    status: HrisRecordStatus
    external_ref: Optional[str] = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str = "ok"


# --- [Proposed] dev trigger simulator (OQ-01/Q-11); see intake/email_intake.py ---


class InboundEmailBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_email: str


class FormApprovedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    offer_ref: str


class DevSeedBody(OnboardingRequestBody):
    """Everything OnboardingRequestBody needs, plus the offer_ref this form
    is seeded for. offer_accepted is irrelevant at seed time (the offer
    starts SENT), so it defaults instead of being required.
    """

    offer_ref: str
    offer_accepted: bool = False


class DevSeedResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    offer_ref: str
    form_status: str


class DevApproveFormBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    offer_ref: str
    approved_by: str


class PendingStartView(BaseModel):
    model_config = ConfigDict(frozen=True)

    pending_id: str
    offer_ref: str
    reasons: list[str]
    status: str


class AuditEntryView(BaseModel):
    """What a case's audit trail looks like over HTTP.

    Deliberately NOT `AuditEntry` itself. The stored entry also carries
    the hash-chain fields (`sequence`, `prev_hash`, `entry_hash`), and
    `sequence` is global across every case in the process: returning it
    on a per-case endpoint would let a caller scoped to one client infer
    how much audit activity other clients are generating. Chain
    verification is an operator concern, served by its own endpoint over
    the complete log, not a per-case read.
    """

    model_config = ConfigDict(frozen=True)

    log_id: str
    case_id: Optional[str] = None
    request_id: Optional[str] = None
    actor_type: ActorType
    actor_id: str
    action: str
    before: Optional[dict] = None
    after: Optional[dict] = None
    timestamp: datetime
    reference: str

    @classmethod
    def of(cls, entry: AuditEntry) -> "AuditEntryView":
        return cls.model_validate(entry.model_dump(exclude={"sequence", "prev_hash", "entry_hash"}))


class AuditIntegrityView(BaseModel):
    """Result of verifying the audit chain over the COMPLETE log."""

    model_config = ConfigDict(frozen=True)

    entries_checked: int
    intact: bool
    violations: list[ChainViolation]


# --- onboarding-document intake (documents/service.py) ---------------------

#: Base64 length for the reader's 10 MB file limit (4 chars per 3 bytes).
MAX_DOCUMENT_BASE64_CHARS = 4 * ((10 * 1024 * 1024 + 2) // 3)


class OnboardingDocumentBody(BaseModel):
    """An onboarding-form .docx or text-based .pdf plus what the document itself cannot say.

    The document never carries the candidate's email, the work location or
    the placement, and its approval is never inferred from its content:
    form_status/approved_by/approved_at are the caller's attestation, the
    same contract as POST /internal/onboarding-requests.
    """

    model_config = ConfigDict(extra="forbid")

    filename: str
    content_base64: str
    request_id: Optional[str] = None
    client_id: str
    hr_officer_id: str
    candidate_email: Optional[str] = None
    offer_accepted: bool = False
    work_location_type: Optional[WorkLocationType] = None
    client_site_id: Optional[str] = None
    form_status: str = "DRAFT"
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    hiring_freeze: bool = False
    declared_variants: Optional[VariantFlagsBody] = None
    # Placement: all optional here; without location_name the case is HELD
    # (location_name_required) by the existing flow - never invented.
    location_name: Optional[str] = None
    department: Optional[str] = None
    grade: Optional[str] = None
    nationality_group: Optional[str] = None
    project_name: Optional[str] = None
    review_confirmed_by: Optional[str] = None

    @model_validator(mode="after")
    def _bounded(self) -> "OnboardingDocumentBody":
        if len(self.content_base64) > MAX_DOCUMENT_BASE64_CHARS:
            raise ValueError("document exceeds the 10 MB limit")
        if self.form_status not in ("DRAFT", "AWAITING_APPROVAL", "APPROVED"):
            raise ValueError("form_status must be DRAFT, AWAITING_APPROVAL or APPROVED")
        return self


class OnboardingDocumentResponse(BaseModel):
    status: str
    document_sha256: Optional[str] = None
    rejection_code: Optional[str] = None
    errors: list[str] = []
    review_reasons: list[str] = []
    outstanding_documents: list[str] = []
    # documents.service.safe_extraction_view: no salary, mobile or names
    # of parents - those are reported by status only.
    extraction: Optional[dict] = None
    case_id: Optional[str] = None
    employee_no: Optional[str] = None
    existing_case_id: Optional[str] = None
    approvals_created: list[str] = []
    reasons: list[str] = []
    source_document_stored: bool = False
    # Server-made storage reference (bucket, path, id, SHA-256) - no file name.
    source_document: Optional[dict] = None
    duplicate_document: bool = False
    form_id: Optional[str] = None
    # True only after the case, form, person, details and budget were read
    # back from the store and matched what was written.
    persistence_verified: bool = False
    readback: dict[str, bool] = {}
