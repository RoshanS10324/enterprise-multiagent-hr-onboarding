from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, Field

from src.state.enums import ActorType, ApprovalStatus, OnboardingStatus


class OnboardingCase(BaseModel):
    case_id: str
    employee_id: str
    client_id: str
    role: str
    grade: str
    nationality: str
    location: str
    offer_accepted: bool
    onboarding_form_approved: bool
    employee_name_form: str
    joining_date: date | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: OnboardingStatus = OnboardingStatus.INCOMPLETE
    authorized_actor_ids: set[str] = Field(default_factory=set)
    received_documents: set[str] = Field(default_factory=set)
    # document type -> the highest revision validated, for requests that carry document_revision
    received_revisions: dict[str, int] = Field(default_factory=dict)


class ApprovalRecord(BaseModel):
    approval_id: str
    case_id: str
    action_type: str
    actor_id: str
    actor_type: ActorType
    status: ApprovalStatus
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    approved_at: datetime | None = None
    notes: str | None = None


class AuditEvent(BaseModel):
    event_id: str
    case_id: str
    actor_id: str
    actor_type: ActorType
    action: str
    detail: dict[str, Any]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str | None = None
