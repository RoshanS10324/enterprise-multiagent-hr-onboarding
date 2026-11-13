"""The orchestrator's own input model, and the plain-JSON wire envelope used to talk to the
UC01/UC04/UC05 worker processes.

The wire envelope is deliberately NOT the same object as `hr_onboarding_common.contracts.
AgentRequest/AgentResponse`: the worker runtime script (orchestration/workers/uc_worker_runtime.py)
must stay importable under each UC's own interpreter without `hr_onboarding_common` installed
there, so the wire format is plain `dict`/`json`, not a pydantic model the worker must import.
`WorkerRequest`/`WorkerResponse` here are a convenience for the orchestrator-side client only --
they serialize to/from that same plain JSON shape.
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator


class OnboardingCaseInput(BaseModel):
    """Input model for `OnboardingOrchestratorAgent` (its `input_model`, per BaseAgent).

    Carries exactly the fields UC01's own contracts do not model (role_id/department/manager_id/
    location/project -- the verified UC01<->UC02 gap) directly, rather than inventing a
    centralized "client knowledge" step no UC agent actually implements.
    """

    client_id: str
    hr_officer_id: str
    offer_accepted: bool
    offer_reference: str | None = None
    # Either a raw acceptance email (UC01.handle_acceptance_email) or an already-structured
    # intake trigger (UC01.open_onboarding_case) -- exactly one must be supplied. Wiring a real
    # mailbox/webhook integration to produce `raw_acceptance_email` is deployment work, out of
    # scope here (documented gap, not fabricated).
    raw_acceptance_email: str | None = None
    candidate_name: str | None = None
    candidate_email: str | None = None
    designation: str | None = None
    joining_date: str | None = None  # ISO date
    work_location_type: str | None = None
    id_number: str | None = None
    # UC02 fields UC01 does not carry -- supplied directly by the original intake payload.
    role_id: str | None = None
    department: str | None = None
    manager_id: str | None = None
    location: str | None = None
    project: str | None = None
    # Optional checklist inputs UC02 uses to select tasks (grade, an opaque nationality GROUP code -- never a
    # nationality -- and opt-in attributes such as visa_required / needs_travel). Sent to UC02 only when given.
    grade: str | None = None
    nationality_group: str | None = None
    attributes: list[str] | None = None
    # Verified approval facts UC02 requires (UC01's gate already enforced them). Carried through,
    # never defaulted: absent values make UC02 planning block with MISSING_DATA.
    offer_accepted_at: str | None = None  # ISO timestamp
    form_approved_at: str | None = None  # ISO timestamp
    form_approved_by_role: str | None = None
    # Pointer to this tenant's UC04 CaseConfig (role/access matrices, approvers, lead times) --
    # client-owned configuration, not fabricated by the orchestrator. See docs for the gap this
    # mirrors in UC04's own CONFIG_GAP_REPORT.md.
    uc04_client_config_ref: str | None = None
    # The offer-acceptance message id in the configured mailbox (UC05's EmailPort) -- the trigger
    # UC05's own `offer_accepted` event consumes. UC05 resolves it to the employee in its employee
    # directory; the orchestrator never substitutes its own case id for it (UC05 would not know it).
    offer_acceptance_event_id: str | None = None
    # The canonical (HRMS) employee id, when the caller already knows it. Optional: otherwise it is
    # resolved from UC01's employee_no through the tenant's identity scheme (tenant_config.py); if
    # both are present and disagree, the case is blocked (EMPLOYEE_ID_CONFLICT).
    employee_id: str | None = None
    # The employee's own IANA timezone, when known; the joining-day scheduler uses it in preference
    # to the tenant's (config/tenants.yaml).
    timezone: str | None = None
    # True for an internal onboarding form submitted by a person (the HTTP start route): the form is
    # sent to UC01 as AWAITING_APPROVAL and the orchestrator's `onboarding_form_approval` gate must be
    # decided by an authorized approver before UC01 may open the case. False (the default) keeps the
    # in-process path for a trusted integration that attests UC01's own form approval
    # (form_approved_at / form_approved_by_role).
    form_approval_required: bool = False
    # Where the form's data came from, when HR started from an uploaded onboarding-form document: the server's own
    # summary of UC01's extraction (onboarding_documents.provenance_for_case -- statuses, field names, the document's
    # digest, the fields HR changed and who confirmed the review; never extracted values). Recorded on the case for the
    # approver and the audit trail; it approves nothing and changes no stage.
    form_extraction: dict[str, Any] | None = None

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"unknown timezone '{value}'") from None
        return value


class Actor(BaseModel):
    """The real, already-verified initiating actor -- never a fabricated "orchestrator" identity.

    See docs/ONBOARDING_COMMON_ARCHITECTURE.md Approval & identity section: UC03's access control
    is actor-id-specific (authorized_actor_ids), so a synthetic orchestrator principal would
    simply be denied; UC04's allowlist already accepts `hr_onboarding_officer`, so the real HR
    officer's role satisfies it too. This mirrors `hr_onboarding_common.contracts.Actor`.
    """

    on_behalf_of: str
    roles: list[str] = Field(default_factory=list)
    client_id: str | None = None


class WorkerRequest(BaseModel):
    contract_version: str = "1.0"
    op: str
    run_id: str  # the case's correlation id
    idempotency_key: str
    actor: Actor
    payload: dict[str, Any] = Field(default_factory=dict)
    # The stage execution this call belongs to (agent_contract.current_execution). Optional and additive:
    # the worker runtime echoes them in its response and its failure log lines, and ignores them otherwise.
    trace_id: str | None = None
    execution_id: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class WorkerResponse(BaseModel):
    contract_version: str = "1.0"
    run_id: str
    ok: bool
    result: dict[str, Any] | None = None
    # On failure: a classified code (RETRYABLE_PROVIDER_ERROR | POLICY_DENIED | NOT_FOUND |
    # INVALID_INPUT | CONFLICT | INTERNAL_ERROR, see uc_worker_runtime._classify), the exception
    # type name, and a short message. Never a traceback: that stays in the worker's stderr log.
    error_code: str | None = None
    error_detail: str | None = None
    error_type: str | None = None
    retryable: bool | None = None
    trace_id: str | None = None  # echoed from the request
    execution_id: str | None = None

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> "WorkerResponse":
        return cls.model_validate(data)
