"""The 5-part orchestrator <-> sub-agent contract (framework L50, blueprint §4.5).

Input, task scope, output, provenance, exception path. Sub-agents only ever see an
AgentRequest and only ever return an AgentResponse.

Forked from hr_recruitment_common.contracts with three deliberate changes (see
docs/ONBOARDING_COMMON_ARCHITECTURE.md A.4 / Part B):
  - `Purpose` carries only `ONBOARDING` (every call site in the onboarding codebase uses this
    value exclusively; the upstream recruitment-only values are dropped, not stubbed).
  - `Actor.client_id` is new. Upstream `Actor` has no tenant representation at all, which is the
    root cause of findings S1 (cross-tenant approvals) and S4 (cross-tenant /v1/runs). This field
    is the single source of tenant identity threaded through both the LangGraph path and the
    registry path.
  - `Scope.requisition_ids` is renamed to `Scope.subject_ids`: the only construction site treats it
    as a generic bag of subject ids, never recruitment-semantic.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field


class Autonomy(StrEnum):
    """Org autonomy levels (dact-business-framework/framework/autonomy-levels.yaml)."""

    L0 = "L0"  # Assist: answer, suggest, summarise. No action in any system
    L1 = "L1"  # Draft for approval: prepare drafts, propose actions; a human approves every action
    L2 = "L2"  # Act and notify, within policy limits
    L3 = "L3"  # Autonomous within bounds


class Purpose(StrEnum):
    ONBOARDING = "onboarding"


class ExceptionCode(StrEnum):
    MISSING_DATA = "MISSING_DATA"
    POLICY_BREACH = "POLICY_BREACH"
    CONSENT_LAPSED = "CONSENT_LAPSED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    INJECTION_SUSPECTED = "INJECTION_SUSPECTED"
    AGENT_DISABLED = "AGENT_DISABLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ResponseStatus(StrEnum):
    PROPOSED = "proposed"
    ACTED = "acted"
    EXCEPTION = "exception"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Actor(BaseModel):
    on_behalf_of: str  # user id, e.g. "user:hr-admin-01"
    roles: list[str] = Field(default_factory=list)  # e.g. ["hr_admin"]
    purpose_of_use: Purpose = Purpose.ONBOARDING
    # Tenant this actor is authorized to act for. None only for actors that never touch
    # tenant-scoped data (there are none in the onboarding domain today); every protected
    # operation must treat a missing client_id as "not authorized for any tenant", never as
    # "authorized for every tenant". See platform.identity.VerifiedActor for how this is
    # populated from an actually-verified identity rather than trusted at face value.
    client_id: str | None = None


class WorkflowRef(BaseModel):
    id: str  # orchestrator id, e.g. "employee-onboarding"
    instance: str  # e.g. employee id
    step: str


class Scope(BaseModel):
    subject_ids: list[str] = Field(default_factory=list)
    max_subjects: int = 500


class PolicyRef(BaseModel):
    ref: str
    version: int
    autonomy: Autonomy
    gates: list[str] = Field(default_factory=list)


class BrainContext(BaseModel):
    doc_id: str
    section: str
    text: str
    tier: str


InputT = TypeVar("InputT", bound=BaseModel)


class AgentRequest(BaseModel, Generic[InputT]):
    contract_version: str = "1.0"
    run_id: str = Field(default_factory=lambda: new_id("run"))
    idempotency_key: str = Field(default_factory=lambda: uuid.uuid4().hex)
    workflow: WorkflowRef
    actor: Actor
    scope: Scope = Field(default_factory=Scope)
    brain_context: list[BrainContext] = Field(default_factory=list)
    policy: PolicyRef | None = None  # filled in by BaseAgent from the policy store
    input: InputT


class Proposal(BaseModel):
    type: str  # e.g. "welcome_message", "session_booking", "buddy_assignment"
    subject_id: str | None = None  # employee id
    payload: dict[str, Any]
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)
    gate: str  # HITL gate id from the policy envelope
    requires_approval_by: str  # role, e.g. "hr_admin"


class AgentException(BaseModel):
    code: ExceptionCode
    detail: str
    subject_id: str | None = None


class BrainFeedback(BaseModel):
    type: str
    subject_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ActionTaken(BaseModel):
    tool: str
    subject_id: str | None = None
    approval_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class Provenance(BaseModel):
    agent: str
    agent_version: str
    models: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    prompt_hashes: list[str] = Field(default_factory=list)
    brain_queries: list[str] = Field(default_factory=list)
    policy: str | None = None
    tool_calls: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=now_utc)
    finished_at: datetime | None = None


class AgentResponse(BaseModel):
    run_id: str
    status: ResponseStatus
    proposals: list[Proposal] = Field(default_factory=list)
    actions_taken: list[ActionTaken] = Field(default_factory=list)
    exceptions: list[AgentException] = Field(default_factory=list)
    brain_feedback: list[BrainFeedback] = Field(default_factory=list)
    provenance: Provenance
