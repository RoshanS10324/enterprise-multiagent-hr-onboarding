"""Pydantic contracts for UC-02 — Onboarding Task Management.

No real employee data lives in these models. ``employee_id``, ``client_id`` and
``manager_id`` are opaque synthetic identifiers; names, emails, nationality and
contact details are deliberately absent (CLAUDE.md rules 2 and 8).

Field names avoid ``hr_recruitment_common.context_layer.memory.PROTECTED_KEYS``
(``name``, ``email``, ``nationality``, ...) so that records can be written to
``EpisodicMemory`` later without being silently scrubbed (review MR-8).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """Timezone-aware UTC timestamp. Injected in tests to keep runs deterministic."""
    return datetime.now(timezone.utc)


class Role(StrEnum):
    """Both task-owner roles and acting-actor roles."""

    HR_OPS = "hr_ops"
    HR_LEAD = "hr_lead"
    IT_MANAGER = "it_manager"
    FACILITIES = "facilities"
    FINANCE = "finance"
    MANAGER = "manager"
    BUDDY = "buddy"
    EMPLOYEE = "employee"
    SYSTEM = "system"
    #: Roles named in Business Framework §8 "Systems and actions". Added so the
    #: approver of each action can be stated explicitly (UC02-BR-11) rather
    #: than defaulting everything to HR Ops.
    HR_OFFICER = "hr_officer"
    HR_MANAGER = "hr_manager"
    PRO = "pro"

    @classmethod
    def _missing_(cls, value: object) -> Role | None:
        """Normalize role strings case-insensitively without granting extra privileges."""
        if isinstance(value, str):
            val_norm = value.strip().lower().replace("-", "_")
            for member in cls:
                if member.value == val_norm or member.name.lower() == val_norm:
                    return member
        return None


#: Roles permitted to act on any task and to decide hard gates.
ADMIN_ROLES = frozenset({Role.HR_OPS, Role.HR_LEAD})


class TaskStatus(StrEnum):
    """Stored lifecycle state.

    ``OVERDUE`` is deliberately *not* a stored status: overdue-ness is derived
    from ``due_date`` against an ``as_of`` date so the same record yields the
    same answer on every run (see :func:`tracker.is_overdue`).
    """

    BLOCKED = "blocked"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


#: Statuses that still represent outstanding work.
OPEN_STATUSES = frozenset({TaskStatus.BLOCKED, TaskStatus.READY, TaskStatus.IN_PROGRESS})


class AssignmentStatus(StrEnum):
    """UC02-BR-03. Whether HR has named a responsible individual.

    Derived from :attr:`TaskRecord.assignee_id`, never stored, so it cannot
    contradict the record. A template's configured ``owner`` role is **not** an
    assignment: an unassigned task reports ``AWAITING_HR_ASSIGNMENT`` even
    though it always carries an owner role.
    """

    AWAITING_HR_ASSIGNMENT = "awaiting_hr_assignment"
    ASSIGNED = "assigned"
    #: Settled work needs no assignee.
    NOT_REQUIRED = "not_required"


# --------------------------------------------------------------------------
# Cross-use-case status interface (UC-02 owned, mock-backed).
#
# NO APPROVED CROSS-USE-CASE CONTRACT EXISTS in this repository. The models
# below are a MINIMAL UC-02-owned adapter so that UC-03 / UC-04 / UC-05 can
# report the outcome of work UC-02 tracks but does not perform. They are
# deliberately replaceable: identifiers only, no transport, no assumption
# about how an update arrives.
#
# UC-02 coordinates and records. It never performs another use case's
# activity, and it never fabricates an outcome that did not arrive.
# --------------------------------------------------------------------------


class SourceUseCase(StrEnum):
    """Which workflow reported an outcome. Mock adapters only, today."""

    UC03_DOCUMENT_VALIDATION = "uc03_document_validation"
    UC04_EQUIPMENT_ACCESS = "uc04_equipment_access"
    UC05_ORIENTATION = "uc05_orientation"
    #: Operational teams that run visa / medical / insurance / travel work.
    OPERATIONS = "operations"


class ExternalOutcome(StrEnum):
    """Outcome reported by another workflow for one UC-02 task.

    ``SUSPECTED_FRAUD`` is deliberately **separate** from ``INVALID``: a
    document can be unreadable, expired or wrong without anyone alleging
    fraud. UC-02 never promotes INVALID to SUSPECTED_FRAUD - only an explicit
    report from the validating workflow carries that meaning.
    """

    #: Work finished successfully; the UC-02 task may be completed.
    COMPLETED = "completed"
    #: UC-03 specific success: documents validated.
    VALID = "valid"
    #: Work started but is not finished.
    IN_PROGRESS = "in_progress"
    #: Rejected. The prerequisite stays unresolved.
    INVALID = "invalid"
    #: A human must look at it before the prerequisite can resolve.
    REQUIRES_REVIEW = "requires_review"
    #: Explicitly reported by the validating workflow. Never inferred.
    SUSPECTED_FRAUD = "suspected_fraud"
    #: Known late. The task stays open and is flagged for follow-up.
    DELAYED = "delayed"


#: Outcomes that permit UC-02 to mark its task COMPLETE.
RESOLVING_OUTCOMES = frozenset({ExternalOutcome.COMPLETED, ExternalOutcome.VALID})

#: Outcomes that leave the prerequisite unresolved and raise a follow-up.
UNRESOLVED_OUTCOMES = frozenset(
    {
        ExternalOutcome.INVALID,
        ExternalOutcome.REQUIRES_REVIEW,
        ExternalOutcome.SUSPECTED_FRAUD,
        ExternalOutcome.DELAYED,
    }
)


class TaskCategory(StrEnum):
    HR_RECORD = "hr_record"
    DOCUMENT_COLLECTION = "document_collection"
    IT_PROVISIONING = "it_provisioning"
    ACCESS_MANAGEMENT = "access_management"
    WORKPLACE = "workplace"
    ORIENTATION = "orientation"
    POLICY_ACKNOWLEDGEMENT = "policy_acknowledgement"
    PAYROLL = "payroll"
    JOINING_CONFIRMATION = "joining_confirmation"
    # --- UC02-BR-07: milestone-bearing activities -------------------------
    BACKGROUND_VERIFICATION = "background_verification"
    VISA = "visa"
    MEDICAL = "medical"
    BIOMETRICS = "biometrics"
    INSURANCE = "insurance"
    # --- UC02-BR-08: travel and related activities ------------------------
    TRAVEL = "travel"
    ACCOMMODATION = "accommodation"
    FOOD = "food"
    BANKING_LETTER = "banking_letter"
    # --- HR-system activation on joining ----------------------------------
    HR_SYSTEM_ACTIVATION = "hr_system_activation"


class ChangeDriver(StrEnum):
    """Attributes whose change can force a re-plan."""

    JOINING_DATE = "joining_date"
    LOCATION = "location"
    PROJECT = "project"
    #: DL-09: where the employee is now (``in_country`` / ``overseas``), which selects the client's visa route.
    LOCATION_STATUS = "location_status"


class OnboardingStatus(StrEnum):
    ACTIVE = "active"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


class GateName(StrEnum):
    """Hard gates. Mirrors ``guardrails/policy.yaml: gates``."""

    DATE_CHANGE = "date_change_approval"
    LOCATION_CHANGE = "location_change_approval"
    PROJECT_CHANGE = "project_change_approval"
    CANCELLATION = "cancellation_approval"
    JOINING_CONFIRMATION = "joining_confirmation"
    #: HR releases a proposed onboarding plan for team execution.
    PLAN_APPROVAL = "plan_approval"
    #: HR reviews the consolidated final onboarding submission.
    FINAL_REVIEW = "final_review"


class PlanStatus(StrEnum):
    """Where the onboarding plan is in HR's review. Not a task status."""

    PROPOSED = "proposed"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"


class FinalStatus(StrEnum):
    """Where the final HR submission is. Not an onboarding status."""

    NOT_SUBMITTED = "not_submitted"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"


#: Task priority. A declared vocabulary; the default is ``normal`` and UC-02 never
#: infers a higher one - it is whatever the approved configuration states.
PRIORITIES = ("low", "normal", "high", "critical")


class DecisionOutcome(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# --------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------


class OfferAcceptance(BaseModel):
    """UC-01 publishes this; UC-02 refuses to plan without it."""

    employee_id: str
    accepted: bool = False
    accepted_at: datetime | None = None
    offer_reference: str | None = None


class OnboardingForm(BaseModel):
    """The onboarding intake form and its HR Ops approval state."""

    employee_id: str
    submitted: bool = False
    approved: bool = False
    approved_by_role: Role | None = None
    approved_at: datetime | None = None
    missing_fields: list[str] = Field(default_factory=list)


class Actor(BaseModel):
    """Who is acting. ``client_id`` drives data isolation on every call."""

    actor_id: str
    role: Role
    client_id: str


# --------------------------------------------------------------------------
# Core records
# --------------------------------------------------------------------------


class TaskRecord(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    task_id: str
    template_key: str
    onboarding_id: str
    client_id: str
    employee_id: str
    category: TaskCategory
    title: str
    title_ar: str = ""
    owner: Role
    status: TaskStatus = TaskStatus.BLOCKED
    due_date: date
    #: ``task_id`` values this task waits on.
    depends_on: list[str] = Field(default_factory=list)
    #: Which attribute changes force this task to be re-planned.
    sensitive_to: list[ChangeDriver] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    notes: str = ""
    #: UC02-BR-12. When True the task cannot be completed without an evidence
    #: or confirmation reference.
    evidence_required: bool = False
    evidence_ref: str | None = None
    evidence_recorded_at: datetime | None = None
    #: UC02-BR-11. Role that must approve this task's action, where the
    #: approved policy names one. None = no action-specific approver.
    approval_role: Role | None = None
    #: Applicability flags this task depended on (UC02-BR-08 travel etc.).
    requires_attributes: list[str] = Field(default_factory=list)
    #: One of :data:`PRIORITIES`; from the template, never inferred.
    priority: str = "normal"
    #: Earliest date the task can start: after its prerequisites are due and
    #: ``duration_days`` business days before its own due date. ``None`` for
    #: records created before this field existed.
    planned_start_date: date | None = None
    #: Failed attempts: how many times another workflow reported this task as
    #: ``invalid``, ``requires_review`` or ``suspected_fraud`` (never ``delayed``
    #: and never a replay of a report already counted). It is history, so a later
    #: success does not reset it.
    failed_report_count: int = 0
    #: Client-configured ceiling on ``failed_report_count``. ``None`` = the client
    #: approved no limit. UC-02 supplies no default: no retry policy is approved.
    max_failed_reports: int | None = None
    #: A team's own report that the task is late (``delay``) or held up
    #: (``blocker``). Distinct from ``external_*`` (another workflow's report).
    #: Cleared when the task completes; history lives in the audit log.
    delay_reason: str = ""
    delay_kind: str = ""
    delay_reported_by: str | None = None
    delay_reported_at: datetime | None = None

    # -- HR manual owner assignment (UC02-BR-03) --------------------------
    #
    # ``owner`` above is the *role* the client configured on the template. It
    # is NOT an assignment: a role is not a person, and a template default
    # must never be mistaken for "HR has assigned someone". The three fields
    # below record the assignment HR actually made. UC-02 never fills them
    # in by itself and never infers an assignee.
    #
    #: Opaque identifier of the individual HR assigned. ``None`` means the
    #: task is awaiting HR assignment.
    assignee_id: str | None = None
    #: Responsible team or unit label, as supplied by HR. Free text, because
    #: operational team names are a client's vocabulary, not UC-02's.
    assignee_team: str = ""
    #: HR actor who made the assignment. Set together with ``assignee_id``.
    assigned_by: str | None = None
    assigned_at: datetime | None = None

    # -- Last outcome reported by another workflow (UC-03/04/05/ops) ------
    #
    # UC-02 tracks work it does not perform. These fields record what the
    # performing workflow last reported, as identifiers and an enum only --
    # never document content, never a narrative about a person. They are
    # written exclusively by the status-update handler.
    #
    #: ``None`` means no other workflow has reported on this task yet, which
    #: is NOT the same as "reported nothing wrong". A missing response never
    #: counts as success.
    external_outcome: "ExternalOutcome | None" = None
    #: Which workflow reported ``external_outcome``.
    external_source: "SourceUseCase | None" = None
    #: Ticket / document / confirmation id from the reporting workflow.
    external_reference: str | None = None
    #: Why the prerequisite stays unresolved. Set only for unresolved
    #: outcomes, and cleared when a later outcome resolves the task.
    external_blocking_reason: str = ""
    #: When the reporting workflow observed the outcome (its clock, not ours).
    external_updated_at: datetime | None = None

    @property
    def retry_limit_reached(self) -> bool:
        """The configured failed-report ceiling is hit while the task is still unresolved."""
        return (
            self.max_failed_reports is not None
            and self.failed_report_count >= self.max_failed_reports
            and self.is_externally_unresolved
        )

    @property
    def is_externally_unresolved(self) -> bool:
        """A reported outcome is holding this task open.

        Drives the ``external_unresolved`` follow-up flag. Derived from the
        recorded outcome rather than from prose in ``notes``, so the flag
        cannot be raised -- or silently missed -- by a formatting change.
        """
        return (
            self.status in OPEN_STATUSES
            and self.external_outcome is not None
            and self.external_outcome in UNRESOLVED_OUTCOMES
        )

    @property
    def is_assigned(self) -> bool:
        """Whether HR has named a responsible individual for this task."""
        return bool(self.assignee_id and self.assignee_id.strip())

    @property
    def assignment_status(self) -> "AssignmentStatus":
        """Derived, never stored - like overdue-ness and milestone state."""
        if self.status in (TaskStatus.COMPLETE, TaskStatus.CANCELLED):
            return AssignmentStatus.NOT_REQUIRED
        return (
            AssignmentStatus.ASSIGNED
            if self.is_assigned
            else AssignmentStatus.AWAITING_HR_ASSIGNMENT
        )


class PlanEvent(BaseModel):
    """One step in the plan's review history. Append-only."""

    version: int
    action: str  # submitted | approved | rejected | revised | replanned
    by: str
    at: datetime = Field(default_factory=utc_now)
    request_id: str | None = None
    reason: str = ""


class DocumentState(StrEnum):
    """Where one required document is, as reported by the document collection process. Never inferred."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    VERIFIED = "verified"
    REJECTED = "rejected"
    RESUBMISSION_REQUIRED = "resubmission_required"
    NOT_APPLICABLE = "not_applicable"


class DocumentItem(BaseModel):
    """One document requirement for one onboarding, and its status. Identifiers and state only: no content."""

    key: str
    document_type: str
    mandatory: bool
    #: Why the client's configuration requires it for this employee.
    reason: str
    state: DocumentState = DocumentState.PENDING
    #: Reference from the verifying process (a ticket or verification id); never document content.
    evidence_ref: str | None = None
    allow_not_applicable: bool = False
    updated_at: datetime | None = None
    last_update_id: str | None = None


class DownstreamState(StrEnum):
    """UC-02's view of a downstream use case's work. UC-02 tracks it; it does not perform it."""

    WAITING_FOR_DOCUMENTS = "waiting_for_documents"
    TRIGGERED = "triggered"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    DELAYED = "delayed"
    REQUIRES_INTERVENTION = "requires_intervention"
    #: The gate is open but no trigger endpoint/credentials are configured for this use case. Never reported as triggered.
    NOT_CONFIGURED = "not_configured"


class DownstreamTrack(BaseModel):
    use_case: str
    state: DownstreamState = DownstreamState.WAITING_FOR_DOCUMENTS
    trigger_id: str | None = None
    attempts: int = 0
    last_error: str = ""
    last_event_id: str | None = None
    last_event_at: datetime | None = None
    task_ids: list[str] = Field(default_factory=list)


class AssignmentTask(BaseModel):
    """One task inside a team assignment. Identifiers, dates and instructions only."""

    task_id: str
    template_key: str
    title: str
    due_date: date
    priority: str
    #: Template keys this task waits on (keys only: the other teams' tasks are not described here).
    depends_on_keys: list[str] = Field(default_factory=list)
    instructions: str = ""


class TeamAssignment(BaseModel):
    """What one responsible team is asked to do under one plan version. Persisted on the onboarding record."""

    assignment_id: str
    team: str
    owner_role: str
    #: ``UC-04`` / ``UC-05`` / ``UC-03`` when the client's mapping says a downstream use case executes this work.
    use_case: str = ""
    plan_version: int
    tasks: list[AssignmentTask] = Field(default_factory=list)
    #: prepared | draft_saved | authorised | submitted_for_sending | simulated_sent | failed | superseded
    status: str = "prepared"
    #: Non-empty while the assignment may not be dispatched (a downstream use case waits for documents).
    hold_reason: str = ""
    draft_ref: str | None = None
    last_outcome: str = ""
    send_attempts: int = 0
    content_sha256: str = ""


class PreparedMessage(BaseModel):
    """The exact subject and body of a message UC-02 prepared, kept so what is reviewed, drafted and sent is one thing.

    Identifiers, dates and plan content only: no address and no personal contact detail (recipients are resolved by the
    connector from approved configuration at send time).
    """

    message_id: str
    kind: str  # hrmail | docreq | teamasg | hrnotice
    plan_version: int
    subject: str
    body: str
    recipient_role: str
    recipient_team: str = ""
    content_sha256: str
    created_at: datetime = Field(default_factory=utc_now)


class FinalSubmission(BaseModel):
    """The consolidated onboarding status handed to HR, and HR's decision on it."""

    version: int
    submitted_by: str
    submitted_at: datetime = Field(default_factory=utc_now)
    as_of: date
    request_id: str
    ready: bool
    total_tasks: int
    complete: int
    pending: int
    blocked: int
    overdue: int
    outstanding_task_ids: list[str] = Field(default_factory=list)
    outstanding_by_category: dict[str, list[str]] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    team_completion: dict[str, str] = Field(default_factory=dict)
    escalation_count: int = 0
    joining_risk: bool = False
    reasons: list[str] = Field(default_factory=list)
    decision: str = "pending"  # pending | approved | rejected
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_reason: str = ""
    returned_task_ids: list[str] = Field(default_factory=list)


class OnboardingRecord(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    onboarding_id: str
    client_id: str
    employee_id: str
    joining_date: date
    location: str
    project: str
    role_id: str
    #: UC02-BR-02. Grade participates in checklist filtering.
    grade: str = ""
    #: UC02-BR-02 nationality dimension, as an OPAQUE GROUP CODE supplied by
    #: the caller (e.g. "grp-a"), never a nationality value. UC-02 stores no
    #: nationality: the nationality -> group mapping is an unresolved
    #: Governance/Legal policy and is deliberately outside this service.
    nationality_group: str = ""
    department: str
    manager_id: str
    #: Applicability flags supplied by the caller, e.g. "needs_travel".
    #: UC-02 never infers these - see UC02-BR-08.
    attributes: list[str] = Field(default_factory=list)
    status: OnboardingStatus = OnboardingStatus.ACTIVE
    tasks: list[TaskRecord] = Field(default_factory=list)
    #: Incremented on every applied re-plan; lets callers detect staleness.
    revision: int = 0
    #: Shared orchestration identifiers, supplied by the caller. UC-02 never
    #: mints them; ``None`` means the caller did not supply one.
    case_id: str | None = None
    correlation_id: str | None = None
    #: Version of the client configuration the plan was generated from.
    config_version: str = ""
    #: HR plan review (see ``OnboardingService.submit_plan``). A new case starts
    #: ``proposed``; team execution is released only once it is ``approved``.
    plan_version: int = 1
    plan_status: PlanStatus = PlanStatus.PROPOSED
    plan_events: list["PlanEvent"] = Field(default_factory=list)
    #: Final HR submission and its decision history; earlier submissions are kept.
    final_status: FinalStatus = FinalStatus.NOT_SUBMITTED
    final_submissions: list["FinalSubmission"] = Field(default_factory=list)
    #: Idempotency ledger for inbound cross-use-case updates:
    #: ``update_id -> task_id`` it was applied to. Held per onboarding rather
    #: than per task so that a replay of one id against a *different* task is
    #: detectable as a conflict, instead of quietly updating the wrong task.
    applied_updates: dict[str, str] = Field(default_factory=dict)
    #: Document requirements for this employee, taken from the approved configuration, with their status.
    documents: list[DocumentItem] = Field(default_factory=list)
    #: ``use case -> state`` for UC-04 / UC-05, kept separately so one cannot block the other.
    downstream: dict[str, DownstreamTrack] = Field(default_factory=dict)
    #: ``"synthetic"`` when the onboarding was built from test-only configuration; empty for real client data.
    data_origin: str = ""
    #: Validated Fake LLM review per plan version (``p<version>`` -> analysis). Identifiers and counts only.
    analyses: dict[str, dict] = Field(default_factory=dict)
    #: Prepared message bodies by id (HR review email, team assignments, document request, HR notices).
    messages: list[PreparedMessage] = Field(default_factory=list)
    #: Team-wise task assignments for the released plan (one per responsible team and owner role).
    assignments: list[TeamAssignment] = Field(default_factory=list)
    #: Idempotency ledger for the workflow layer: ``event / callback / update id -> outcome``.
    flow_ledger: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def task_by_id(self, task_id: str) -> TaskRecord | None:
        return next((t for t in self.tasks if t.task_id == task_id), None)

    def task_by_key(self, template_key: str) -> TaskRecord | None:
        return next((t for t in self.tasks if t.template_key == template_key), None)


# --------------------------------------------------------------------------
# Derived views (proposal payloads — IDs and counts only, never PII)
# --------------------------------------------------------------------------


class TaskSummary(BaseModel):
    onboarding_id: str
    client_id: str
    employee_id: str
    joining_date: date
    total_tasks: int
    blocked: int
    ready: int
    in_progress: int
    complete: int
    cancelled: int
    overdue_task_ids: list[str] = Field(default_factory=list)
    days_to_joining: int = 0
    is_on_track: bool = True


class JoiningRisk(BaseModel):
    """Day-1 readiness assessment. Numbers come from code, never a model."""

    onboarding_id: str
    joining_date: date
    days_to_joining: int
    at_risk: bool
    critical_open_task_ids: list[str] = Field(default_factory=list)
    overdue_task_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class MilestoneStatus(StrEnum):
    """Derived milestone state.

    Like :class:`TaskStatus`'s overdue handling, this is never stored: it is
    computed from the required tasks and an explicit ``as_of`` date so the same
    record always yields the same answer.
    """

    PENDING = "pending"
    ACHIEVED = "achieved"
    AT_RISK = "at_risk"
    MISSED = "missed"


class MilestoneState(BaseModel):
    """A milestone evaluated against the current plan. IDs and counts only."""

    milestone_id: str
    template_key: str
    onboarding_id: str
    client_id: str
    employee_id: str
    title: str
    title_ar: str = ""
    target_date: date
    status: MilestoneStatus
    #: ``task_id`` values that must be COMPLETE for this milestone.
    required_task_ids: list[str] = Field(default_factory=list)
    completed_task_ids: list[str] = Field(default_factory=list)
    outstanding_task_ids: list[str] = Field(default_factory=list)
    overdue_task_ids: list[str] = Field(default_factory=list)
    blocking_task_ids: list[str] = Field(default_factory=list)
    #: Business days from ``as_of`` to ``target_date``; negative once passed.
    days_to_target: int = 0
    reasons: list[str] = Field(default_factory=list)


class CalendarStatus(BaseModel):
    """UC02-BR-06. Whether the working calendar in use is authoritative.

    The built-in UAE holiday list is a synthetic placeholder: lunar dates are
    approximate and it has not been signed off by Legal/HR. Due dates computed
    from it are therefore provisional, and must be labelled as such rather
    than presented as authoritative.
    """

    approved: bool
    source: str
    coverage_start: date
    coverage_end: date
    holiday_count: int
    warnings: list[str] = Field(default_factory=list)


class EscalationLevel(StrEnum):
    """UC02-BR-10 escalation ladder.

    Business Framework §10: "Escalation: to the assigned HR officer in Teams;
    if there is no response within one working day, to the HR operations
    lead." The two levels below are that rule, not an invented policy.
    """

    NONE = "none"
    HR_OFFICER = "hr_officer"
    HR_OPS_LEAD = "hr_ops_lead"


class EscalationItem(BaseModel):
    """One item needing escalation, with the rule that triggered it."""

    subject_type: str          # "task" | "milestone" | "configuration" | "approval"
    subject_id: str
    level: EscalationLevel
    escalate_to: Role
    reason: str
    #: Business Framework guardrail that requires the escalation.
    source_rule: str = "GR-12"
    owner: Role | None = None
    days_waiting: int = 0


class OwnerAssignmentIssue(BaseModel):
    """UC02-BR-03. A task whose configured owner is missing or not valid.

    UC-02 never substitutes a default owner; it reports the issue so the
    configuration can be corrected.
    """

    template_key: str
    client_id: str
    problem: str
    configured_owner: str | None = None


class StatusUpdate(BaseModel):
    """One inbound status report about one UC-02 task.

    Carries identifiers, an outcome and an optional reference only. No
    document content, no personal data, no free-form narrative about a person.
    """

    model_config = ConfigDict(frozen=True)

    #: Caller-supplied idempotency key. A replay of the same id is recognised
    #: and applied once.
    update_id: str
    source: SourceUseCase
    client_id: str
    onboarding_id: str
    task_id: str
    outcome: ExternalOutcome
    #: When the reporting workflow observed the outcome. Explicit, so UC-02
    #: never reads a clock on an inbound path.
    occurred_at: datetime
    #: Ticket / document / confirmation id. Satisfies ``evidence_required``.
    reference: str | None = None
    #: Why the task cannot resolve. Required for UNRESOLVED_OUTCOMES.
    blocking_reason: str = ""
    #: Shared orchestration identifiers. Optional on the wire; when ``case_id``
    #: is given and the onboarding recorded one, they must agree.
    case_id: str | None = None
    correlation_id: str | None = None


class StatusUpdateResult(BaseModel):
    """What UC-02 did with a :class:`StatusUpdate`. Never silently discarded."""

    update_id: str
    task_id: str
    accepted: bool
    #: Task status after applying. ``None`` when the update was rejected.
    applied_status: TaskStatus | None = None
    #: True when this id had already been applied; no second effect occurred.
    duplicate_replay: bool = False
    #: Populated when ``accepted`` is False.
    rejection_code: str = ""
    rejection_reason: str = ""
    #: Task ids that became READY because this update resolved a prerequisite.
    unblocked_task_ids: list[str] = Field(default_factory=list)
    #: Raised when the outcome leaves the prerequisite unresolved.
    follow_up_raised: bool = False


class FollowUpFlag(BaseModel):
    """A task needing someone to chase it, and who should chase it.

    UC-02 raises flags; it does not deliver them. There is no email, Teams or
    notification transport anywhere in this package, so a flag is **not**
    evidence that anyone was told.
    """

    task_id: str
    template_key: str
    onboarding_id: str
    client_id: str
    #: ``unassigned`` | ``external_unresolved`` | ``overdue`` | ``delayed``
    code: str
    reason: str
    #: Configured owner role; always present.
    owner_role: Role
    #: HR-assigned individual, when one exists.
    assignee_id: str | None = None
    assignee_team: str = ""
    #: Who should act: the assignee if there is one, otherwise HR.
    follow_up_with: str
    source: SourceUseCase | None = None
    delivered: bool = False
    delivery_note: str = (
        "UC-02 raises follow-up flags only. No notification transport exists; "
        "delivery belongs to UC-06."
    )


class OutstandingActivity(BaseModel):
    """UC02-BR-10. One outstanding activity **with its responsible owner**.

    The previous summary reported task IDs only, which did not satisfy the
    rule's requirement to name the owner and the required follow-up.
    """

    task_id: str
    template_key: str
    category: TaskCategory
    owner: Role
    due_date: date
    status: TaskStatus
    is_overdue: bool = False
    days_overdue: int = 0
    blocked_by_task_ids: list[str] = Field(default_factory=list)
    #: Factual next step derived from status - never an invented instruction.
    required_follow_up: str = ""


class JoiningHandoff(BaseModel):
    """Planning output handed to **UC-06 — Joining Confirmation & Follow-up**.

    UC-02 owns planning, re-planning and planning-time risk. It does **not**
    own ongoing monitoring, follow-up execution, joining confirmation, or
    closing the onboarding case. This package is the interface UC-06 consumes;
    UC-02 produces it and stops there.
    """

    onboarding_id: str
    client_id: str
    employee_id: str
    joining_date: date
    plan_revision: int
    #: Planning-time risk, retained in UC-02 (it is a planning calculation).
    risk: "JoiningRisk"
    outstanding: list[OutstandingActivity] = Field(default_factory=list)
    milestones: list["MilestoneState"] = Field(default_factory=list)
    owner_issues: list[OwnerAssignmentIssue] = Field(default_factory=list)
    escalations: list[EscalationItem] = Field(default_factory=list)
    calendar: "CalendarStatus | None" = None
    #: Always False from UC-02. Only UC-06 may close the case.
    case_closed_by_uc02: bool = False
    handoff_note: str = (
        "UC-02 provides planning, outstanding-activity and joining-risk "
        "information only. Ongoing monitoring, follow-up delivery, joining "
        "confirmation and case closure are owned by UC-06."
    )


# --------------------------------------------------------------------------
# Dynamic checklist selection (UC02-BR-02): what was included, what was not,
# and the factual reason for each decision.
#
# Nothing here invents a policy. Every reason is built from two facts that are
# already in the record: the filter values the client configured on a
# TaskTemplate, and the context values the caller supplied on the onboarding.
# --------------------------------------------------------------------------


class SelectionDimension(StrEnum):
    """One applicability filter on a :class:`checklist.TaskTemplate`."""

    LOCATION = "location"
    PROJECT = "project"
    ROLE_ID = "role_id"
    GRADE = "grade"
    NATIONALITY_GROUP = "nationality_group"
    ATTRIBUTES = "attributes"


class SelectionOutcome(StrEnum):
    INCLUDED = "included"
    EXCLUDED = "excluded"


class DimensionVerdict(BaseModel):
    """How one filter dimension decided, with the values it decided from."""

    model_config = ConfigDict(frozen=True)

    dimension: SelectionDimension
    #: Values configured on the template. Empty = no restriction on this axis.
    configured: list[str] = Field(default_factory=list)
    #: Value(s) supplied by the onboarding context.
    supplied: list[str] = Field(default_factory=list)
    #: ATTRIBUTES only: configured flags the context does not declare.
    missing: list[str] = Field(default_factory=list)
    matched: bool = True

    @property
    def unrestricted(self) -> bool:
        return not self.configured


class ChecklistContext(BaseModel):
    """The employee context a checklist is generated from. IDs only, no PII."""

    model_config = ConfigDict(frozen=True)

    client_id: str
    joining_date: date
    location: str
    project: str
    role_id: str
    grade: str = ""
    nationality_group: str = ""
    attributes: list[str] = Field(default_factory=list)
    #: Optional: absent when previewing a context that has no record yet.
    employee_id: str = ""


class TemplateDecision(BaseModel):
    """Why one configured task template was included in, or left out of, a plan."""

    model_config = ConfigDict(frozen=True)

    template_key: str
    category: TaskCategory
    outcome: SelectionOutcome
    #: Stable machine-readable code, e.g. ``applies_to_all``,
    #: ``matched_all_filters``, ``excluded_by_location``.
    reason_code: str
    #: Deterministic sentence assembled from configured and supplied values.
    reason: str
    verdicts: list[DimensionVerdict] = Field(default_factory=list)


class ChecklistIssueSeverity(StrEnum):
    """How an issue affects the generated checklist."""

    #: The checklist could not be produced at all.
    BLOCKING = "blocking"
    #: Produced, but a human must confirm it before it is acted on.
    REVIEW_REQUIRED = "review_required"
    #: Worth stating; does not hold the checklist up.
    INFORMATIONAL = "informational"


class ChecklistIssue(BaseModel):
    """A configuration or data problem found while generating a checklist.

    UC-02 never resolves one of these by guessing: it reports the facts and
    leaves the decision to the configuration owner.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    severity: ChecklistIssueSeverity
    #: What the issue is about: a template key, a client_id, or "calendar".
    subject: str
    detail: str


class ChecklistEntry(BaseModel):
    """One generated task plus the decision and state that explain it."""

    task_id: str
    template_key: str
    category: TaskCategory
    title: str
    title_ar: str = ""
    owner: Role
    status: TaskStatus
    due_date: date
    depends_on: list[str] = Field(default_factory=list)
    #: Prerequisite ``task_id`` values that are not COMPLETE yet.
    blocked_by: list[str] = Field(default_factory=list)
    #: Same list as ``blocked_by``, as template keys — readable in explanations.
    blocked_by_keys: list[str] = Field(default_factory=list)
    is_overdue: bool = False
    days_overdue: int = 0
    evidence_required: bool = False
    #: True once a reference has been recorded. The reference itself is a
    #: ticket/document id held on the task; it is not duplicated here.
    evidence_recorded: bool = False
    #: Role whose approval the configuration requires for this action.
    approval_role: Role | None = None
    #: Open task that cannot be actioned until ``approval_role`` approves.
    awaiting_approval: bool = False
    decision: TemplateDecision


class ChecklistView(BaseModel):
    """The structured, explained checklist for one onboarding.

    Derived on demand from the stored record, the client configuration and an
    explicit ``as_of`` date — never stored, so it cannot go stale.
    """

    onboarding_id: str
    client_id: str
    employee_id: str
    joining_date: date
    as_of: date
    revision: int = 0
    onboarding_status: OnboardingStatus = OnboardingStatus.ACTIVE
    context: ChecklistContext
    entries: list[ChecklistEntry] = Field(default_factory=list)
    #: Templates the client configured that this onboarding does not get.
    excluded: list[TemplateDecision] = Field(default_factory=list)
    ready_task_ids: list[str] = Field(default_factory=list)
    blocked_task_ids: list[str] = Field(default_factory=list)
    in_progress_task_ids: list[str] = Field(default_factory=list)
    complete_task_ids: list[str] = Field(default_factory=list)
    cancelled_task_ids: list[str] = Field(default_factory=list)
    overdue_task_ids: list[str] = Field(default_factory=list)
    awaiting_approval_task_ids: list[str] = Field(default_factory=list)
    #: Dependency layers of ``task_id``; everything in a layer may run at once.
    parallel_batches: list[list[str]] = Field(default_factory=list)
    #: Gate approval requests still pending for this onboarding.
    pending_gate_request_ids: list[str] = Field(default_factory=list)
    milestones: list["MilestoneState"] = Field(default_factory=list)
    issues: list[ChecklistIssue] = Field(default_factory=list)
    #: True when any issue is REVIEW_REQUIRED or BLOCKING.
    review_required: bool = False
    calendar: "CalendarStatus | None" = None


class ChecklistPreview(BaseModel):
    """Selection result for a context that has no stored record yet.

    Lets a caller (or an agent) ask "what would this employee's checklist be?"
    without creating anything. No task IDs exist, so everything is keyed by
    ``template_key``.
    """

    context: ChecklistContext
    included: list[TemplateDecision] = Field(default_factory=list)
    excluded: list[TemplateDecision] = Field(default_factory=list)
    #: ``template_key`` -> due date, computed from the same calendar rules.
    due_dates: dict[str, date] = Field(default_factory=dict)
    #: Dependency layers of ``template_key``, prerequisites first.
    parallel_batches: list[list[str]] = Field(default_factory=list)
    issues: list[ChecklistIssue] = Field(default_factory=list)
    review_required: bool = False
    calendar: "CalendarStatus | None" = None


class ReplanOutcome(BaseModel):
    """Result of a re-plan. Lists task_ids only."""

    onboarding_id: str
    drivers: list[ChangeDriver] = Field(default_factory=list)
    added_task_ids: list[str] = Field(default_factory=list)
    removed_task_ids: list[str] = Field(default_factory=list)
    rescheduled_task_ids: list[str] = Field(default_factory=list)
    retained_task_ids: list[str] = Field(default_factory=list)
    revision: int = 0


class DocumentSpec(BaseModel):
    """One document requirement UC-02 puts on the plan (UC-03 collects and validates it). Ids only."""

    document_type: str = Field(min_length=1, max_length=64)
    mandatory: bool = True
    reason: str = Field(default="", max_length=200)
    allow_not_applicable: bool = False


class DocumentFeedback(BaseModel):
    """UC-03's structured validation feedback that needs the plan changed (UC-02 replans on it).

    ``feedback_id`` makes the replan idempotent: the same feedback applied twice changes the plan
    once. ``plan_version`` is the version UC-03 validated against; feedback for another version is
    stale and refused."""

    feedback_id: str = Field(min_length=1, max_length=128)
    plan_version: int = Field(ge=1)
    add_documents: list[DocumentSpec] = Field(default_factory=list)
    remove_documents: list[str] = Field(default_factory=list)
    #: Applicability flags UC-03 established from the documents (switch on templates; never inferred by UC-02).
    add_attributes: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class DocumentReplanOutcome(BaseModel):
    """Result of a document-driven replan. Task and document ids only."""

    onboarding_id: str
    plan_version: int
    previous_plan_version: int
    feedback_id: str
    added_documents: list[str] = Field(default_factory=list)
    removed_documents: list[str] = Field(default_factory=list)
    retained_documents: list[str] = Field(default_factory=list)
    added_task_ids: list[str] = Field(default_factory=list)
    removed_task_ids: list[str] = Field(default_factory=list)
    rescheduled_task_ids: list[str] = Field(default_factory=list)
    effective_joining_date: date
    reasons: list[str] = Field(default_factory=list)
    #: True when this feedback had already been applied: nothing changed this time.
    duplicate_replay: bool = False


class ReminderDraft(BaseModel):
    """A prepared message that is NEVER sent by this prototype.

    There is no transport anywhere in :mod:`reminders` — producing a draft is
    the terminal action. Delivery is UC-06 / the notification connector (C-8),
    behind the ``joining_confirmation`` gate.
    """

    draft_id: str
    onboarding_id: str
    client_id: str
    employee_id: str
    task_ids: list[str]
    recipient_role: Role
    reason: str
    subject_en: str
    subject_ar: str = ""
    body_en: str = ""
    body_ar: str = ""
    #: Immutable marker asserted by tests — drafts are never dispatched.
    delivery_status: str = "draft_not_sent"


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    request_id: str
    onboarding_id: str
    client_id: str
    gate: GateName
    requested_by: str
    approver_role: Role
    payload: dict[str, str] = Field(default_factory=dict)
    #: Raised-at timestamp. Required to tell whether an approval has stalled
    #: past its gate SLA (GR-12).
    created_at: datetime = Field(default_factory=utc_now)
    outcome: DecisionOutcome = DecisionOutcome.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    reason: str = ""


class AuditEvent(BaseModel):
    """Append-only audit entry. IDs, counts and hashes only (CLAUDE.md rule 8)."""

    event_id: str
    client_id: str
    onboarding_id: str | None = None
    actor_id: str
    actor_role: Role
    action: str
    outcome: str
    detail: dict[str, str] = Field(default_factory=dict)
    at: datetime = Field(default_factory=utc_now)
    #: Traceability fields. All optional so existing events stay valid.
    correlation_id: str | None = None
    task_id: str | None = None
    before_status: str | None = None
    after_status: str | None = None
    #: Who approved/rejected, where the event records a decision.
    approver_id: str | None = None
    #: Owner assignment changes. Opaque ids, never a name; kept apart from the status fields so an
    #: assignee is never mistaken for a status.
    previous_assignee_id: str | None = None
    current_assignee_id: str | None = None


class JoiningReadiness(BaseModel):
    """Joining readiness derived from actual task and approval state."""

    onboarding_id: str
    as_of: date
    status: str  # "granted" | "not_ready"
    plan_status: PlanStatus
    plan_version: int
    final_status: FinalStatus
    total_tasks: int
    complete: int
    pending: int
    blocked: int
    overdue: int
    outstanding_task_ids: list[str] = Field(default_factory=list)
    outstanding_by_category: dict[str, list[str]] = Field(default_factory=dict)
    missing_evidence_task_ids: list[str] = Field(default_factory=list)
    unresolved_external_task_ids: list[str] = Field(default_factory=list)
    delayed_task_ids: list[str] = Field(default_factory=list)
    #: Open tasks with no HR-assigned individual: an HR action is outstanding.
    outstanding_hr_action_task_ids: list[str] = Field(default_factory=list)
    outstanding_approval_ids: list[str] = Field(default_factory=list)
    overdue_task_ids: list[str] = Field(default_factory=list)
    escalation_count: int = 0
    joining_risk: bool = False
    joining_risk_reasons: list[str] = Field(default_factory=list)
    #: Why readiness is not granted. Empty exactly when ``status == "granted"``.
    reasons: list[str] = Field(default_factory=list)


class TeamSummary(BaseModel):
    """One team's share of the plan. A team is a configured owner role."""

    team: str
    total: int
    complete: int
    in_progress: int
    ready: int
    blocked: int
    overdue: int
    unassigned: int
    assignees: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)


class ReminderStatus(BaseModel):
    """DL-14 state of one task's employee-reminder cycle, derived from the audit trail."""

    task_id: str
    #: ``active`` | ``stopped_responded`` | ``stopped_closed`` | ``escalated`` | ``follow_up_done``
    state: str
    attempts: int = 0
    max_attempts: int = 0
    interval_days: int = 0
    last_attempt_on: date | None = None
    next_attempt_on: date | None = None
    follow_up_required: bool = False
    follow_up_outcome: str = ""
    #: Reminders are recorded, never delivered by UC-02.
    delivery: str = "none: attempt recorded as pending; no message was sent"


class SweepResult(BaseModel):
    """What a monitoring sweep found. Reminders are DRAFTS; nothing is delivered."""

    onboarding_id: str
    as_of: date
    reminder_drafts: list[ReminderDraft] = Field(default_factory=list)
    escalations: list[EscalationItem] = Field(default_factory=list)
    newly_recorded_escalations: int = 0
    newly_recorded_reminders: int = 0
    delivery: str = "none: drafts and escalation records only; nothing was sent"


class EscalationRecord(BaseModel):
    """One recorded escalation, read back from the audit history."""

    subject_type: str
    subject_id: str
    level: str
    escalate_to: str
    reason: str
    as_of: str
    recorded_at: datetime
    status: str  # "open" | "resolved"
