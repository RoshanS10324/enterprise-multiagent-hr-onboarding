from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.state.enums import (
    ActorType,
    ApprovalStatus,
    ComplianceStatus,
    DocumentStatus,
    EvidenceStatus,
    HRReviewStatus,
    RequiredAction,
    SubmissionStatus,
    ValidationFinding,
    VisaPackStatus,
)


class DocumentValidationRequest(BaseModel):
    case_id: str
    employee_id: str
    employee_name_form: str
    offer_accepted: bool
    onboarding_form_approved: bool
    document_type: str
    submission_status: SubmissionStatus
    extracted_name: str | None = None
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    expiry_date: date | None = None
    readable: bool | None = None
    required_fields_present: bool | None = None
    extraction_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_stored: bool = False
    hr_review_status: HRReviewStatus = HRReviewStatus.PENDING
    document_ref: str | None = None
    client_id: str = "default"
    role: str = ""
    grade: str = ""
    nationality: str = ""
    location: str = ""
    sponsorship_applicable: bool = False
    visa_required: bool = False
    validation_date: date = Field(default_factory=date.today)
    requesting_actor_id: str
    requesting_actor_type: ActorType
    correlation_id: str | None = None
    additional_context: dict[str, Any] = Field(default_factory=dict)
    attestation_confirmed: bool = False
    # The caller's revision number of this document (UC-03's collection, sub_agent/collection.py).
    # When given, a duplicate is the same or an older revision; a newer revision is validated again.
    document_revision: int | None = Field(default=None, ge=1)

    @field_validator("extraction_confidence")
    @classmethod
    def confidence_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("extraction_confidence must be between 0 and 1")
        return v

    @field_validator("employee_name_form")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("employee_name_form must not be empty")
        return v.strip()

    @field_validator("case_id", "employee_id", "document_type")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Field must not be empty")
        return v.strip()


class DocumentValidationResult(BaseModel):
    case_id: str
    document_type: str
    document_status: DocumentStatus
    validation_findings: list[ValidationFinding]
    outstanding_requirements: list[str]
    evidence_status: EvidenceStatus
    hr_decision: HRReviewStatus
    compliance_item_status: ComplianceStatus
    visa_pack_status: VisaPackStatus = VisaPackStatus.NOT_APPLICABLE
    required_next_action: RequiredAction
    approval_status: ApprovalStatus
    audit_reference: str
    detail: str
    draft_communication: str | None = None
    injection_detected: bool = False
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    # Where the result was stored: "NOT_CONFIGURED" (no result store, e.g. in-memory mode),
    # "PERSISTED", or "NOT_PERSISTED:<reason code>" when a precondition of the database model is
    # not met. A database error is never reported here: it raises, so a failed write can never
    # look like a success.
    persistence_status: str = "NOT_CONFIGURED"
