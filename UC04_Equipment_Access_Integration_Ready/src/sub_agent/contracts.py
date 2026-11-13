"""Validated input and output contracts. Synthetic identifiers only by default (GR-13; see `identity.py` for the opt-in PLATFORM profile);
unknown fields and statuses are rejected."""

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from sub_agent import IMPLEMENTATION_MODE
from sub_agent.config_state import ConfigBlock
from guardrails import identity as _identity_module
from sub_agent.states import (ApprovalClass, ApprovalDecision, ApprovalState, ApprovalType, CaseStatus, ClearanceStatus,
                         CollectionStatus, DeliveryStatus, EvidenceType, HandoverMode, InvocationMode, ItemKind,
                         ItemState, LeadKey, ProvisioningStatus, Severity, TicketStatus, WorkMode)

_IDENTITY = _identity_module.active()  # fixes the identifier profile for this process
CASE_ID = _IDENTITY.case_id
EMPLOYEE_REF = _IDENTITY.employee_ref
REQUEST_KEY = _IDENTITY.request_key
USER_REF = _IDENTITY.user_ref
EVIDENCE_REF = _IDENTITY.evidence_ref
APPROVAL_ID = _IDENTITY.approval_id
TICKET_ID = _IDENTITY.ticket_id
ITEM_ID = r"^[a-z0-9][a-z0-9-]{1,63}$"
ROLE = _IDENTITY.role
LABEL = r"^[A-Za-z0-9_. -]{1,60}$"
TOKEN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"  # project, cohort, transfer and asset references (opaque ids, never names)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- configuration blocks

class ApproverEntry(_Strict):
    role: str = Field(pattern=ROLE)
    delegate_role: str | None = Field(default=None, pattern=ROLE)


class WorkCalendar(_Strict):
    weekend_days: list[int]  # 0 = Monday ... 6 = Sunday; the real working-calendar source is an open question
    holidays: list[date] = Field(default_factory=list)

    @field_validator("weekend_days")
    @classmethod
    def _days(cls, v):
        if any(d < 0 or d > 6 for d in v) or len(set(v)) != len(v) or len(v) >= 7:
            raise ValueError("weekend_days must be distinct values 0-6 and leave at least one working day")
        return v


class HandoverRules(_Strict):
    mode: HandoverMode
    keep_reserved_on_missed_slot: StrictBool


class CountryRules(_Strict):
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    courier_extra_days: int
    remote_first_day_required: StrictBool
    # Optional local calendar and timezone, supplied by configuration (never hard-coded here). Absent: the client calendar is used.
    timezone: str | None = None  # IANA name
    weekend_days: list[int] | None = None  # 0 = Monday ... 6 = Sunday
    holidays: list[date] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v):
        if v is not None:
            try:
                ZoneInfo(v)
            except Exception:  # noqa: BLE001 - unknown or malformed zone name
                raise ValueError("unknown timezone") from None
        return v

    @model_validator(mode="after")
    def _calendar(self):
        if self.weekend_days is not None:
            WorkCalendar(weekend_days=self.weekend_days, holidays=self.holidays)  # same validation as the client calendar
        return self

    def calendar(self):
        return WorkCalendar(weekend_days=self.weekend_days, holidays=self.holidays) if self.weekend_days is not None else None


class ClientSiteRules(_Strict):
    required_items: list[str] = Field(default_factory=list)  # badge, client device, client NDA, ...
    block_on_late_clearance: StrictBool
    owner_role: str = Field(pattern=ROLE)


class ClientOverrides(_Strict):
    """Client instructions that may conflict with mandatory guardrails (GR-17, EC-51). Such values are ignored and logged."""

    skip_standard_approval: bool | None = None
    allow_out_of_matrix_without_exception: bool | None = None
    client_hr_admin_ref: str | None = Field(default=None, pattern=USER_REF)


class CaseConfig(_Strict):
    client_id: str = Field(pattern=ROLE)
    role_id: str = Field(pattern=ROLE)
    version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,30}$")
    role_access_matrix: ConfigBlock[list[str]] = Field(default_factory=ConfigBlock)
    role_equipment_matrix: ConfigBlock[list[str]] = Field(default_factory=ConfigBlock)
    approver_config: ConfigBlock[dict[ApprovalClass, ApproverEntry]] = Field(default_factory=ConfigBlock)
    lead_times: ConfigBlock[dict[LeadKey, int]] = Field(default_factory=ConfigBlock)  # working days before joining
    work_calendar: ConfigBlock[WorkCalendar] = Field(default_factory=ConfigBlock)
    handover_rules: ConfigBlock[HandoverRules] = Field(default_factory=ConfigBlock)
    evidence_roles: ConfigBlock[dict[EvidenceType, list[str]]] = Field(default_factory=ConfigBlock)
    country_rules: ConfigBlock[CountryRules] = Field(default_factory=ConfigBlock)
    client_site_rules: ConfigBlock[ClientSiteRules] = Field(default_factory=ConfigBlock)
    client_overrides: ClientOverrides = Field(default_factory=ClientOverrides)


# --------------------------------------------------------------------------- case input

class HeldItem(_Strict):
    item_id: str = Field(pattern=ITEM_ID)
    item_kind: ItemKind
    asset_ref: str | None = Field(default=None, pattern=TOKEN)  # IT's reference for held equipment, when known


class TransferContext(_Strict):
    """An approved internal transfer of an EXISTING employee (EC-42). The employee keeps one identity; this is not a new hire.
    Supplied by the parent from trusted HR data. UC-04 compares what is held with the new role's configuration and proposes changes;
    IT confirms every grant, removal and handover."""

    transfer_ref: str = Field(pattern=TOKEN)  # a distinct reference, so old and new role stay in the history
    previous_role_id: str = Field(pattern=ROLE)
    previous_client_id: str | None = Field(default=None, pattern=ROLE)  # set only for a cross-client transfer
    held_access: list[HeldItem] = Field(default_factory=list)
    held_equipment: list[HeldItem] = Field(default_factory=list)
    remove_old_access: StrictBool = True  # the approved transfer plan says whether old-role access is removed
    client_requires_joining_documents: StrictBool = False  # a client rule; UC-03 owns any document validation
    # What IT has CONFIRMED so far (item ids). UC-04 never assumes a removal or a retained device happened.
    it_confirmed_removed: list[str] = Field(default_factory=list)
    it_confirmed_suitable: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kinds(self):
        if any(i.item_kind != ItemKind.ACCESS for i in self.held_access) or any(i.item_kind != ItemKind.EQUIPMENT for i in self.held_equipment):
            raise ValueError("held items must match their list kind")
        return self


class RequestedItem(_Strict):
    request_key: str = Field(pattern=REQUEST_KEY)
    item_id: str = Field(pattern=ITEM_ID)
    item_kind: ItemKind
    requested_by: str = Field(pattern=USER_REF)
    client_site: StrictBool = False
    note: str | None = Field(default=None, max_length=500)  # untrusted text (GR-10); never acted on or echoed


class ApprovalRecord(_Strict):
    approval_id: str = Field(pattern=APPROVAL_ID)
    request_id: str
    approval_type: ApprovalType
    approver_id: str = Field(pattern=USER_REF)
    approver_role: str = Field(pattern=ROLE)
    decision: ApprovalDecision
    decided_on: date
    item_id: str = Field(pattern=ITEM_ID)
    client_id: str = Field(pattern=ROLE)
    role_id: str = Field(pattern=ROLE)
    client_site_required: StrictBool


class TicketUpdate(_Strict):
    request_id: str
    ticket_id: str = Field(pattern=TICKET_ID)
    status: TicketStatus
    reason: str | None = Field(default=None, max_length=300)
    updated_on: date


class DeliveryRecord(_Strict):
    request_id: str
    status: DeliveryStatus
    expected_date: date | None = None
    delivered_on: date | None = None
    evidence_ref: str | None = Field(default=None, pattern=EVIDENCE_REF)
    asserted_by_role: str | None = Field(default=None, pattern=ROLE)
    # PROPOSED optional facts from IT (the real response contract is pending IT). Never invented by UC-04.
    asset_ref: str | None = Field(default=None, pattern=TOKEN)
    assigned_model: str | None = Field(default=None, pattern=LABEL)  # what IT says it assigned
    reported_model: str | None = Field(default=None, pattern=LABEL)  # what was found at handover
    fault_note: str | None = Field(default=None, max_length=300)  # untrusted text: sanitised, quoted as data, never acted on


class CollectionRecord(_Strict):
    request_id: str
    status: CollectionStatus
    next_slot: date | None = None
    evidence_ref: str | None = Field(default=None, pattern=EVIDENCE_REF)
    asserted_by_role: str | None = Field(default=None, pattern=ROLE)


class ProvisioningRecord(_Strict):
    request_id: str
    status: ProvisioningStatus
    evidence_ref: str | None = Field(default=None, pattern=EVIDENCE_REF)
    asserted_by_role: str | None = Field(default=None, pattern=ROLE)


class ClientSiteClearance(_Strict):
    status: ClearanceStatus = ClearanceStatus.PENDING
    expected_date: date | None = None
    evidence_ref: str | None = Field(default=None, pattern=EVIDENCE_REF)
    asserted_by_role: str | None = Field(default=None, pattern=ROLE)


class Uc04Input(_Strict):
    case_id: str = Field(pattern=CASE_ID)
    employee_ref: str = Field(pattern=EMPLOYEE_REF)
    client_id: str = Field(pattern=ROLE)
    role_id: str = Field(pattern=ROLE)
    joining_date: date
    offer_accepted: StrictBool
    onboarding_form_approved: StrictBool
    form_version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,30}$")
    work_location: str = Field(pattern=LABEL)
    work_mode: WorkMode = WorkMode.OFFICE
    client_site_required: StrictBool = False
    requested_access: list[RequestedItem] = Field(default_factory=list)
    requested_equipment: list[RequestedItem] = Field(default_factory=list)
    config: CaseConfig
    approval_records: list[ApprovalRecord] = Field(default_factory=list)
    mock_it_ticket_updates: list[TicketUpdate] = Field(default_factory=list)
    delivery_status: list[DeliveryRecord] = Field(default_factory=list)
    collection_status: list[CollectionRecord] = Field(default_factory=list)
    provisioning_status: list[ProvisioningRecord] = Field(default_factory=list)
    client_site_clearance: ClientSiteClearance | None = None
    unavailable_approver_roles: list[str] = Field(default_factory=list)
    prior_equipment_held: StrictBool = False  # EC-44
    internal_transfer: StrictBool = False  # EC-42 (flag only)
    display_label: str | None = Field(default=None, pattern=LABEL)  # human label, never an identifier, never echoed
    as_of_date: date
    # Optional, additive context. Field names are PROPOSED and pending the integration team's approval.
    project_id: str | None = Field(default=None, pattern=TOKEN)  # EC-35: a project change under the same client
    deployment_date: date | None = None  # EC-34: the client-site deployment date, when it differs from the joining date
    cohort_ref: str | None = Field(default=None, pattern=TOKEN)  # EC-40: the shared cohort reference
    as_of_utc: datetime | None = None  # EC-36: with a country timezone, as_of_date becomes the employee's local date
    transfer: "TransferContext | None" = None  # EC-42: an approved internal transfer of an existing employee

    @model_validator(mode="after")
    def _kinds(self):
        if any(i.item_kind != ItemKind.ACCESS for i in self.requested_access):
            raise ValueError("requested_access may only contain ACCESS items")
        if any(i.item_kind != ItemKind.EQUIPMENT for i in self.requested_equipment):
            raise ValueError("requested_equipment may only contain EQUIPMENT items")
        rules = self.config.country_rules
        if (self.as_of_utc is not None and self.work_mode != WorkMode.OFFICE and rules.is_usable and rules.value.timezone
                and self.as_of_utc.tzinfo is not None):
            self.as_of_date = self.as_of_utc.astimezone(ZoneInfo(rules.value.timezone)).date()
        return self


class CallerContext(_Strict):
    """Caller metadata. It never influences a business decision (TC-35/36); it is carried for correlation and audit."""

    invocation_mode: InvocationMode
    actor_id: str = Field(pattern=USER_REF)
    roles: list[str] = Field(default_factory=list)
    run_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,60}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")
    workflow_instance: str | None = None


class Uc04Request(_Strict):
    caller: CallerContext
    input: Uc04Input


# --------------------------------------------------------------------------- output

class Blocker(BaseModel):
    code: str
    severity: Severity
    request_id: str | None = None
    reason: str
    owner: str
    next_action: str
    rule_id: str


class Risk(BaseModel):
    code: str
    request_id: str | None = None
    reason: str
    owner: str
    next_action: str
    rule_id: str


class Proposal(BaseModel):
    """A suggestion for a human decision. Nothing is ordered, assigned, sent or changed."""

    proposal_type: str
    request_id: str | None = None
    description: str
    requires_approval_by: str
    executed: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)  # structured draft content (message or IT request); data only, never executed

    @field_validator("executed")
    @classmethod
    def _never_executed(cls, v):
        if v:
            raise ValueError("a proposal is never executed by UC-04")
        return v


class NextAction(BaseModel):
    action: str
    owner: str
    request_id: str | None = None


class RejectedApproval(BaseModel):
    approval_id: str
    request_id: str
    reason_code: str


class ItemResult(BaseModel):
    request_id: str
    request_key: str
    item_kind: ItemKind
    item_id: str
    in_matrix: bool | None  # None = cannot be decided because configuration is missing
    exception_required: bool
    exception_reason: str | None = None
    required_approver: str
    approval_state: ApprovalState
    exception_state: ApprovalState
    state: ItemState
    ticket_id: str | None = None
    ticket_status: TicketStatus | None = None
    provisioning_state: str | None = None
    delivery_state: str | None = None
    collection_state: str | None = None
    due_date: date | None = None
    lead_time_days: int | None = None
    lead_time_source: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    completed: bool = False
    superseded: bool = False
    submission_attempts: int = 0


class DuplicateLink(BaseModel):
    """A repeated request that was linked to an existing one instead of creating a second record (BR-14)."""

    request_key: str
    linked_request_id: str
    reason_code: str


class Reassessment(BaseModel):
    required: bool
    changed_fields: list[str] = Field(default_factory=list)
    previous_joining_date: date | None = None
    reapproval_request_ids: list[str] = Field(default_factory=list)
    # EC-35: what the case was assessed against before this change, so the old configuration stays on record. Filled only for a changed field.
    previous_client_id: str | None = None
    previous_project_id: str | None = None
    previous_config_version: str | None = None
    previous_config_digest: str | None = None


class RemotePlan(BaseModel):
    work_mode: WorkMode
    country_code: str | None = None
    courier_extra_days: int | None = None
    remote_first_day_required: bool | None = None
    timezone: str | None = None
    calendar_source: str = "CLIENT"  # COUNTRY when the configuration supplied a local calendar
    first_day_steps: list[str] = Field(default_factory=list)
    note: str = "Plan only. No courier or shipment is created in local simulation."


class Uc04Result(BaseModel):
    case_id: str
    implementation_mode: str = IMPLEMENTATION_MODE
    overall_status: CaseStatus
    readiness_reason: str
    items: list[ItemResult] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    proposals: list[Proposal] = Field(default_factory=list)
    cancellation_drafts: list[Proposal] = Field(default_factory=list)
    rejected_approvals: list[RejectedApproval] = Field(default_factory=list)
    duplicates_ignored: list[DuplicateLink] = Field(default_factory=list)
    next_actions: list[NextAction] = Field(default_factory=list)
    reassessment: Reassessment = Field(default_factory=lambda: Reassessment(required=False))
    remote_plan: RemotePlan | None = None
    config_version: str | None = None
    config_digest: str | None = None  # content digest of the configuration used; a changed config is never silent
    config_gaps: list[str] = Field(default_factory=list)
    transfer_plan: dict[str, Any] | None = None  # EC-42: what to keep, request, replace and remove; proposals only
    clearance_status: ClearanceStatus | None = None
    caller: dict[str, Any] = Field(default_factory=dict)
    audit_events: list[dict[str, Any]] = Field(default_factory=list)

    def business_decision(self) -> dict[str, Any]:
        """The caller-independent part of the result, used to prove both invocation modes decide identically."""
        data = self.model_dump(mode="json", exclude={"caller", "audit_events"})
        data["audit_event_types"] = [e["event_type"] for e in self.audit_events if e["event_type"] != "invocation.received"]
        return data


class SubmissionReport(BaseModel):
    result: Uc04Result
    submitted: list[str] = Field(default_factory=list)
    failed: list[dict[str, str]] = Field(default_factory=list)
    skipped: list[dict[str, str]] = Field(default_factory=list)


class ItemStatusView(BaseModel):
    """What UC-04 itself has stored about one request: no evaluation, no input facts, no free text."""

    request_id: str
    request_key: str
    item_kind: ItemKind
    item_id: str
    state: ItemState
    ticket_id: str | None = None
    submission_attempts: int = 0
    adapter_error: str | None = None  # error class only; `SUBMISSION_IN_DOUBT` while an IT-desk call may be in flight or was lost
    archived: bool = False


class CaseStatusView(BaseModel):
    case_id: str
    client_id: str
    parent_status: str
    items: list[ItemStatusView] = Field(default_factory=list)
