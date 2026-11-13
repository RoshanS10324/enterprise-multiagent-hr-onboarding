"""UC-02 orchestration (prototype seam).

This is the deterministic replacement for the ``BaseAgent`` + LangGraph wrapper
described in review §3.3. ``langgraph`` cannot be installed on this machine
(``uv`` missing), so the node sequence is expressed as plain methods. The method
names map one-to-one onto the planned graph nodes so the port is mechanical:

====================================  ==========================
Service method                        Planned graph node
====================================  ==========================
:meth:`create_onboarding`             ``build_task_plan``
:meth:`status` / :meth:`joining_risk` ``check_status``
:meth:`request_change` / :meth:`apply_change`  ``handle_date_change``
:meth:`reminder_drafts`               ``handle_alerts``
====================================  ==========================

Every public method takes an :class:`Actor` first and enforces client isolation
before doing anything else.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta

from pydantic import BaseModel, Field

from onboarding_task_management import config as config_module
from onboarding_task_management.audit import AuditLog
from onboarding_task_management.calendar_uae import (
    WorkingCalendar,
    UAE_PUBLIC_HOLIDAYS,
    assert_covered,
    calendar_status,
)
from onboarding_task_management.contracts import (
    ADMIN_ROLES,
    OPEN_STATUSES,
    Actor,
    ApprovalRequest,
    CalendarStatus,
    ChangeDriver,
    ChecklistContext,
    ChecklistIssue,
    ChecklistPreview,
    ChecklistView,
    DecisionOutcome,
    DocumentFeedback,
    DocumentItem,
    DocumentReplanOutcome,
    DocumentSpec,
    DocumentState,
    EscalationRecord,
    FinalStatus,
    FinalSubmission,
    JoiningReadiness,
    PlanEvent,
    PlanStatus,
    SweepResult,
    TeamSummary,
    EscalationItem,
    EscalationLevel,
    FollowUpFlag,
    GateName,
    JoiningHandoff,
    JoiningRisk,
    MilestoneState,
    OfferAcceptance,
    OnboardingForm,
    OnboardingRecord,
    ReminderStatus,
    OnboardingStatus,
    OutstandingActivity,
    OwnerAssignmentIssue,
    ReminderDraft,
    ReplanOutcome,
    Role,
    SourceUseCase,
    StatusUpdate,
    StatusUpdateResult,
    TaskRecord,
    TaskStatus,
    TaskSummary,
    utc_now,
)
from onboarding_task_management.errors import (
    ApprovalRequiredError,
    CalendarConfigurationError,
    DataIsolationError,
    DuplicateOnboardingError,
    EmployeeNotEligibleError,
    InvalidStateError,
    MissingConfigurationError,
    PlanNotApprovedError,
    StalePlanVersionError,
    PreconditionError,
    UnauthorizedActionError,
)
from onboarding_task_management.persistence import OnboardingStore
from onboarding_task_management.sub_agent.checklist import ClientOnboardingConfig
from onboarding_task_management.privacy import redact_free_text
from onboarding_task_management.sub_agent import (
    drafts as drafts_module,
    schedule,
    approvals,
    workflow,
    milestones,
    planner,
    reminders,
    selection,
    status_updates,
    tracker,
)
from onboarding_task_management.sub_agent.approvals import ApprovalInbox
from onboarding_task_management.sub_agent.checklist import ConfigRegistry, NonResponsePolicy


class OnboardingTrigger(BaseModel):
    """What UC-01 / the O2 orchestrator supplies to start UC-02.

    IDs and attributes only — no name, email, nationality or contact details.
    """

    client_id: str
    employee_id: str
    joining_date: date
    role_id: str
    department: str
    manager_id: str
    location: str
    project: str
    #: UC02-BR-02. Grade participates in checklist filtering.
    grade: str = ""
    #: UC02-BR-02 nationality dimension as an OPAQUE GROUP CODE. UC-02 never
    #: receives or stores a nationality value - see checklist.applies_to.
    nationality_group: str = ""
    #: UC02-BR-08. Applicability flags, e.g. ("needs_travel",
    #: "needs_accommodation", "family_insurance"). Never inferred by UC-02.
    attributes: tuple[str, ...] = ()
    #: DL-09. Where the employee is now relative to the work country: ``in_country`` or
    #: ``overseas``. Optional and never inferred; when given it becomes the attribute
    #: ``location_status:<value>``, which a client's templates may require (so the visa route
    #: is the client's configuration, not UC-02's).
    current_location_status: str = ""
    #: Shared orchestration identifiers. Optional: a caller with no shared
    #: case (standalone use) omits them. UC-02 stores and echoes them; it
    #: never invents either.
    case_id: str | None = None
    correlation_id: str | None = None
    offer: OfferAcceptance
    form: OnboardingForm


#: Which gate guards which attribute change.
CHANGE_GATES: dict[ChangeDriver, GateName] = {
    ChangeDriver.JOINING_DATE: GateName.DATE_CHANGE,
    ChangeDriver.LOCATION: GateName.LOCATION_CHANGE,
    ChangeDriver.PROJECT: GateName.PROJECT_CHANGE,
    ChangeDriver.LOCATION_STATUS: GateName.LOCATION_CHANGE,
}


def validate_preconditions(trigger: OnboardingTrigger) -> None:
    """Offer must be accepted and the onboarding form approved by HR.

    Raises :class:`PreconditionError` describing the first failure found.
    """
    if trigger.offer.employee_id != trigger.employee_id:
        raise PreconditionError(
            "offer acceptance belongs to a different employee",
            employee_id=trigger.employee_id,
            offer_employee_id=trigger.offer.employee_id,
        )
    if trigger.form.employee_id != trigger.employee_id:
        raise PreconditionError(
            "onboarding form belongs to a different employee",
            employee_id=trigger.employee_id,
            form_employee_id=trigger.form.employee_id,
        )
    if not trigger.offer.accepted:
        raise PreconditionError(
            "offer has not been accepted", employee_id=trigger.employee_id
        )
    if trigger.offer.accepted_at is None:
        raise PreconditionError(
            "offer acceptance has no timestamp", employee_id=trigger.employee_id
        )
    if not trigger.form.submitted:
        raise PreconditionError(
            "onboarding form has not been submitted", employee_id=trigger.employee_id
        )
    if trigger.form.missing_fields:
        raise PreconditionError(
            "onboarding form is incomplete",
            employee_id=trigger.employee_id,
            missing=",".join(sorted(trigger.form.missing_fields)),
        )
    if not trigger.form.approved:
        raise PreconditionError(
            "onboarding form has not been approved", employee_id=trigger.employee_id
        )
    if trigger.form.approved_by_role not in ADMIN_ROLES:
        raise PreconditionError(
            "onboarding form was not approved by an HR role",
            employee_id=trigger.employee_id,
            approved_by_role=str(trigger.form.approved_by_role),
        )


class OnboardingService:
    """Client-isolated store plus the UC-02 operations."""

    def __init__(
        self,
        registry: ConfigRegistry,
        *,
        audit: AuditLog | None = None,
        inbox: ApprovalInbox | None = None,
        holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
        store: OnboardingStore | None = None,
        holidays_by_client: Mapping[str, frozenset[date]] | None = None,
        source_principals: Mapping[SourceUseCase, frozenset[str]] | None = None,
        calendars: Mapping[str, WorkingCalendar] | None = None,
        require_client_calendar: bool = False,
        require_plan_approval: bool = False,
    ) -> None:
        self.registry = registry
        #: When True, team execution (start, complete, status reports, delay reports,
        #: monitoring) is refused until HR approves the plan. ``build_service`` turns
        #: this on; it defaults to False only so the many pre-existing unit tests
        #: that drive tasks directly keep their original semantics.
        self.require_plan_approval = require_plan_approval
        #: Per-client working calendars (own weekend and holidays). With
        #: ``require_client_calendar`` a client without one is refused instead
        #: of silently inheriting the built-in UAE calendar.
        self.calendars = calendars if getattr(calendars, "lazy", False) else dict(calendars or {})
        self.require_client_calendar = require_client_calendar
        #: Client-specific holiday calendars. A client not listed falls back
        #: to ``holidays``; UC-02 never assumes another client's calendar.
        self.holidays_by_client = dict(holidays_by_client or {})
        #: Authenticated reporters per source workflow. ``None`` (the default)
        #: means NO reporter is trusted and every inbound status update is
        #: refused; there is no fallback that trusts the caller's ``source``
        #: claim. Service identities must be configured to receive updates.
        self.source_principals = (
            None
            if source_principals is None
            else {k: frozenset(v) for k, v in source_principals.items()}
        )
        self.audit = audit or AuditLog()
        if store is not None and self.audit.sink is None:
            # Write every audit event through to the durable store as it happens.
            self.audit.sink = lambda event: store.append_audit([event])
        self._hydrated: set[str] = set()
        self.inbox = inbox or ApprovalInbox()
        self.holidays = holidays
        #: Optional durable store. ``None`` keeps the in-memory prototype
        #: behaviour exactly as it was; when set, every state change is
        #: written through so the records survive process exit.
        self.store = store
        #: Partitioned by client_id so a lookup can never cross clients.
        self._records: dict[str, dict[str, OnboardingRecord]] = {}

    # -- durable store -----------------------------------------------------

    def _persist(self, record: OnboardingRecord) -> None:
        """Write a record through to the store, if one is configured.

        A storage failure is **not** swallowed: ``PersistenceError`` propagates
        so a caller can never be told an operation succeeded when the durable
        copy was not written (CLAUDE.md "verify before you claim").
        """
        if self.store is None:
            return
        config = self.registry.configs.get(record.client_id)
        self.store.save_onboarding(record, config=config)

    def _commit(self, record: OnboardingRecord, backup: OnboardingRecord) -> None:
        """Persist ``record``; if that fails, put the pre-change copy back.

        The operations mutate the in-memory record first. Without this, a storage
        failure left memory ahead of the database: later reads showed a change that
        was never saved, and a retried status update looked like a duplicate of one
        that had not been stored. The error still propagates.
        """
        try:
            self._persist(record)
        except BaseException:
            self._client_records(record.client_id)[record.onboarding_id] = backup
            raise

    def _persist_audit(self, actor: Actor) -> None:
        """Append any audit events not yet in the store. Idempotent by event_id."""
        if self.store is None:
            return
        stored = {event.event_id for event in self.store.load_audit(actor.client_id)}
        fresh = [e for e in self.audit.events_for(actor) if e.event_id not in stored]
        self.store.append_audit(fresh)

    def _holidays_for(self, client_id: str) -> frozenset[date]:
        """The working calendar for ``client_id`` (a frozenset of holidays)."""
        if client_id in self.calendars:
            return self.calendars[client_id]
        if self.require_client_calendar:
            raise CalendarConfigurationError(
                "no working calendar is configured for this client",
                client_id=client_id,
            )
        return self.holidays_by_client.get(client_id, self.holidays)

    def hydrate(self, client_id: str) -> int:
        """Load a client's stored onboardings into memory. Returns the count.

        This is what makes persistence real rather than write-only: a fresh
        process calls it and continues from the stored state, including task
        statuses, evidence references and the plan revision.
        """
        if self.store is None:
            return 0
        self._hydrated.add(client_id)  # set first: _client_records must not recurse
        try:
            records = self.store.list_onboardings(client_id)
            bucket = self._client_records(client_id)
            for record in records:
                bucket[record.onboarding_id] = record
                self.audit.bind_correlation(
                    record.client_id, record.onboarding_id, record.correlation_id
                )
            for request in self.store.load_approvals(client_id):
                self.inbox.restore(request)
            self.audit.restore(self.store.load_audit(client_id))
        except BaseException:
            self._hydrated.discard(client_id)  # a failed load must be retried, not assumed
            raise
        return len(records)

    # -- internals ---------------------------------------------------------

    def _client_records(self, client_id: str) -> dict[str, OnboardingRecord]:
        return self._records.setdefault(client_id, {})

    def _ensure_hydrated(self, client_id: str) -> None:
        """Load a client's stored state once, before an operation that depends on it."""
        if self.store is not None and client_id not in self._hydrated:
            self.hydrate(client_id)

    @staticmethod
    def _snapshot(record: OnboardingRecord) -> OnboardingRecord:
        """Deep copy handed to callers instead of the stored object (H1).

        Returning the live record let any caller — including roles with no
        authority over a task — rewrite status, due dates or onboarding state
        directly, bypassing ownership checks, prerequisite enforcement and the
        audit log entirely. Mutating a snapshot now affects nothing.
        """
        return record.model_copy(deep=True)

    @staticmethod
    def _snapshot_tasks(tasks: list[TaskRecord]) -> list[TaskRecord]:
        return [task.model_copy(deep=True) for task in tasks]

    def _load(self, actor: Actor, onboarding_id: str) -> OnboardingRecord:
        record = self._client_records(actor.client_id).get(onboarding_id)
        if record is None and self.store is not None:
            if self.store.client_of_onboarding(onboarding_id) == actor.client_id:
                self._ensure_hydrated(actor.client_id)  # stored by an earlier process
                record = self._client_records(actor.client_id).get(onboarding_id)
        if record is None:
            # If it exists under another client, this is an isolation breach,
            # not a missing record — report it as such and audit it.
            for client_id, records in self._records.items():
                if client_id != actor.client_id and onboarding_id in records:
                    self.audit.record(
                        actor,
                        "onboarding.access",
                        "denied_isolation",
                        onboarding_id=onboarding_id,
                    )
                    raise DataIsolationError(
                        "actor is not permitted to access another client's onboarding",
                        actor_client_id=actor.client_id,
                        onboarding_id=onboarding_id,
                    )
            if self.store is not None:
                owner = self.store.client_of_onboarding(onboarding_id)
                if owner is not None and owner != actor.client_id:
                    self.audit.record(
                        actor,
                        "onboarding.access",
                        "denied_isolation",
                        onboarding_id=onboarding_id,
                    )
                    raise DataIsolationError(
                        "actor is not permitted to access another client's onboarding",
                        actor_client_id=actor.client_id,
                        onboarding_id=onboarding_id,
                    )
            raise InvalidStateError("unknown onboarding", onboarding_id=onboarding_id)
        return record

    # -- create ------------------------------------------------------------

    def create_onboarding(
        self, actor: Actor, trigger: OnboardingTrigger
    ) -> OnboardingRecord:
        """Validate preconditions, then generate the task plan from client config."""
        if actor.client_id != trigger.client_id:
            self.audit.record(actor, "onboarding.create", "denied_isolation")
            raise DataIsolationError(
                "actor may not create an onboarding for another client",
                actor_client_id=actor.client_id,
                trigger_client_id=trigger.client_id,
            )
        if actor.role not in ADMIN_ROLES:
            self.audit.record(actor, "onboarding.create", "denied_unauthorized")
            raise UnauthorizedActionError(
                "only HR roles may create an onboarding", actor_role=str(actor.role)
            )

        self._ensure_hydrated(actor.client_id)  # an earlier process's cases count as history
        try:
            validate_preconditions(trigger)
        except PreconditionError as exc:
            self.audit.record(
                actor, "onboarding.create", "rejected_precondition", reason=exc.message
            )
            raise

        # A cancelled onboarding must not block a legitimate re-onboarding
        # (finding M5), but any live one still must. The cancelled record and
        # its audit history are kept; the new attempt gets its own ID.
        history = [
            existing
            for existing in self._client_records(actor.client_id).values()
            if existing.employee_id == trigger.employee_id
        ]
        blocking = [
            existing
            for existing in history
            if existing.status is not OnboardingStatus.CANCELLED
        ]
        if blocking:
            self.audit.record(
                actor,
                "onboarding.create",
                "rejected_duplicate",
                onboarding_id=blocking[0].onboarding_id,
            )
            raise DuplicateOnboardingError(
                "an onboarding already exists for this employee",
                onboarding_id=blocking[0].onboarding_id,
            )

        attempt = len(history) + 1
        onboarding_id = planner.make_onboarding_id(
            trigger.client_id, trigger.employee_id, attempt
        )
        if self.store is not None and getattr(self.store.client, "uses_case_ids", False):
            # Shared database: the onboarding IS the UC-01 case, so its id is that case's uuid.
            if not trigger.case_id:
                raise PreconditionError(
                    "the shared database needs the UC-01 case_id to start an onboarding",
                    employee_id=trigger.employee_id,
                )
            onboarding_id = trigger.case_id

        config = self.registry.get(trigger.client_id, on=trigger.joining_date)
        self._require_eligible(actor, config, ChecklistContext(
            client_id=trigger.client_id, joining_date=trigger.joining_date, location=trigger.location, project=trigger.project,
            role_id=trigger.role_id, grade=trigger.grade, nationality_group=trigger.nationality_group,
            attributes=list(trigger.attributes) + ([f"location_status:{trigger.current_location_status}"]
                                                   if trigger.current_location_status else [])), "onboarding.create")
        record = OnboardingRecord(
            onboarding_id=onboarding_id,
            client_id=trigger.client_id,
            employee_id=trigger.employee_id,
            joining_date=trigger.joining_date,
            location=trigger.location,
            project=trigger.project,
            role_id=trigger.role_id,
            grade=trigger.grade,
            nationality_group=trigger.nationality_group,
            department=trigger.department,
            manager_id=trigger.manager_id,
            attributes=list(trigger.attributes) + (
                [f"location_status:{trigger.current_location_status}"]
                if trigger.current_location_status else []
            ),
            case_id=trigger.case_id,
            correlation_id=trigger.correlation_id,
            config_version=config.version,
            data_origin=config.data_origin,
            plan_events=[PlanEvent(version=1, action="proposed", by=actor.actor_id)],
        )
        record.tasks = planner.build_tasks(record, config, self._holidays_for(record.client_id))

        # Durable first: if the store rejects the write (duplicate, constraint
        # violation, backend down) the case must not appear to exist in memory.
        if self.store is not None:
            self.store.assert_no_live_onboarding(
                trigger.client_id, trigger.employee_id
            )
        self._client_records(actor.client_id)[onboarding_id] = record
        try:
            self._persist(record)
        except Exception:
            del self._client_records(actor.client_id)[onboarding_id]
            self.audit.record(
                actor, "onboarding.create", "failed_persist", onboarding_id=onboarding_id
            )
            raise
        self.audit.bind_correlation(
            actor.client_id, onboarding_id, trigger.correlation_id
        )
        self.audit.record(
            actor,
            "onboarding.create",
            "created",
            onboarding_id=onboarding_id,
            task_count=len(record.tasks),
            case_id=trigger.case_id or "",
            config_version=config.version,
        )
        return self._snapshot(record)

    def _require_eligible(self, actor: Actor, config: ClientOnboardingConfig, context: ChecklistContext, action: str) -> None:
        """Strict client-specific validation of role, grade, work location, nationality group and employment type."""
        errors = selection.eligibility_errors(config, context)
        if errors:
            self.audit.record(actor, action, "denied_not_eligible", problems=str(len(errors)))
            raise EmployeeNotEligibleError("the employee is outside the client's configured eligibility", client_id=config.client_id,
                                           problems="; ".join(errors))

    def _audit_read(self, actor: Actor, onboarding_id: str, operation: str) -> None:
        """Record a successful read of T2 personal data.

        Denied cross-client reads were already audited; successful ones were
        not, so legitimate access to an employee's onboarding left no trace.
        Only IDs and the operation name are recorded — never record contents
        (CLAUDE.md rule 8).

        OPEN DECISION: read-audit retention and volume limits are undefined.
        ``reminder_drafts`` deliberately does not call this, because it already
        emits its own ``reminder.draft`` event and a second entry would be a
        duplicate for one logical operation.
        """
        self.audit.record(
            actor,
            "onboarding.read",
            "read",
            onboarding_id=onboarding_id,
            operation=operation,
        )

    def get(self, actor: Actor, onboarding_id: str) -> OnboardingRecord:
        """Read-only snapshot of the stored record (H1). Audited (§2A)."""
        record = self._load(actor, onboarding_id)
        self._audit_read(actor, onboarding_id, "get")
        return self._snapshot(record)

    # -- task progress -----------------------------------------------------

    def start_task(
        self, actor: Actor, onboarding_id: str, task_id: str
    ) -> TaskRecord:
        record = self._load(actor, onboarding_id)
        backup = record.model_copy(deep=True)
        self._require_released(actor, record, "task.start", task_id)
        before = getattr(record.task_by_id(task_id), "status", None)
        try:
            task = tracker.start_task(actor, record, task_id)
        except Exception as exc:
            self.audit.record(
                actor,
                "task.start",
                "denied",
                onboarding_id=onboarding_id,
                task_id=task_id,
                error=type(exc).__name__,
            )
            raise
        self._commit(record, backup)
        self.audit.record(
            actor,
            "task.start",
            "in_progress",
            onboarding_id=onboarding_id,
            task_id=task_id,
            before_status=before.value if before else None,
            after_status=task.status.value,
        )
        return task.model_copy(deep=True)

    def complete_task(
        self,
        actor: Actor,
        onboarding_id: str,
        task_id: str,
        evidence_ref: str | None = None,
    ) -> TaskRecord:
        """Complete a task. ``evidence_ref`` is mandatory when the task's
        configuration requires completion evidence (UC02-BR-12)."""
        record = self._load(actor, onboarding_id)
        backup = record.model_copy(deep=True)
        self._require_released(actor, record, "task.complete", task_id)
        before = getattr(record.task_by_id(task_id), "status", None)
        try:
            task = tracker.complete_task(actor, record, task_id, evidence_ref)
        except Exception as exc:
            self.audit.record(
                actor,
                "task.complete",
                "denied",
                onboarding_id=onboarding_id,
                task_id=task_id,
                error=type(exc).__name__,
            )
            raise
        self._commit(record, backup)
        self.audit.record(
            actor,
            "task.complete",
            "complete",
            onboarding_id=onboarding_id,
            task_id=task_id,
            before_status=before.value if before else None,
            after_status=task.status.value,
            evidence=bool(task.evidence_ref),
        )
        return task.model_copy(deep=True)

    # -- HR manual owner assignment (UC02-BR-03) ---------------------------

    def assign_owner(
        self,
        actor: Actor,
        onboarding_id: str,
        task_id: str,
        assignee_id: str,
        *,
        team: str = "",
    ) -> TaskRecord:
        """Record HR's choice of responsible individual for one task.

        HR decides; UC-02 records. There is no owner-assignment agent and no
        model in this path -- ``assignee_id`` is taken from the caller and
        stored as given. The task's configured ``owner`` role is unchanged.

        A refused assignment is audited as ``denied`` before the error
        propagates, so an attempt by an unauthorised actor leaves a trace.
        """
        record = self._load(actor, onboarding_id)
        backup = record.model_copy(deep=True)
        previous_assignee = getattr(record.task_by_id(task_id), "assignee_id", None)
        try:
            task = tracker.assign_owner(
                actor, record, task_id, assignee_id, team=team
            )
        except Exception as exc:
            self.audit.record(
                actor,
                "task.assign_owner",
                "denied",
                onboarding_id=onboarding_id,
                task_id=task_id,
                error=type(exc).__name__,
            )
            raise
        self._commit(record, backup)
        # Identifiers and a role only. The assignee id is an opaque synthetic
        # identifier, the same class of value as actor_id, which the audit
        # log already stores.
        self.audit.record(
            actor,
            "task.assign_owner",
            "assigned",
            onboarding_id=onboarding_id,
            task_id=task_id,
            assignee_id=task.assignee_id,
            owner_role=task.owner.value,
            previous_assignee_id=previous_assignee,
            current_assignee_id=task.assignee_id,
        )
        return task.model_copy(deep=True)

    def unassigned_tasks(
        self, actor: Actor, onboarding_id: str
    ) -> list[TaskRecord]:
        """Open tasks for which HR has not yet named an individual.

        A task always has an owner *role*; that is never counted as an
        assignment here.
        """
        record = self._load(actor, onboarding_id)
        self._audit_read(actor, onboarding_id, "unassigned_tasks")
        return self._snapshot_tasks(tracker.unassigned_tasks(record))

    # -- Inbound cross-use-case status updates -----------------------------

    def apply_status_update(
        self, actor: Actor, onboarding_id: str, update: StatusUpdate
    ) -> StatusUpdateResult:
        """Apply one outcome reported by UC-03 / UC-04 / UC-05 / operations.

        **Mock-backed.** No approved cross-use-case interface exists and this
        package contains no transport, so in this phase ``update`` comes from
        :mod:`mock_handoff` or a fixture. Applying one records that UC-02 was
        told something; it is not evidence that external work occurred.

        Business rejections are returned, not raised, so the caller can log
        them -- see :mod:`status_updates`. The record is persisted only when
        the update was accepted and actually changed something, so a rejected
        or replayed update cannot bump ``updated_at``.
        """
        record = self._load(actor, onboarding_id)
        backup = record.model_copy(deep=True)
        prior = record.task_by_id(update.task_id)
        before_status = prior.status.value if prior else None
        verification = status_updates.verify_source(
            actor, update, self.source_principals
        )
        result = status_updates.apply_status_update(
            actor,
            record,
            update,
            source_principals=self.source_principals,
            plan_released=self._plan_released(record),
        )

        if result.duplicate_replay:
            # A redelivery of an update already applied changes nothing and
            # already has its audit event; a second one would only make a
            # retrying sender look like several distinct reports.
            return result

        if result.accepted:
            self._commit(record, backup)

        self.audit.record(
            actor,
            "task.status_update",
            "accepted" if result.accepted else "rejected",
            onboarding_id=onboarding_id,
            task_id=update.task_id,
            update_id=update.update_id,
            source=update.source.value,
            # The reported outcome, not the document or ticket behind it.
            # Named ``reported_outcome`` because ``outcome`` is already the
            # audit event's own accepted/rejected verdict above.
            reported_outcome=update.outcome.value,
            applied_status=result.applied_status.value
            if result.applied_status
            else "",
            duplicate_replay=result.duplicate_replay,
            unblocked_count=len(result.unblocked_task_ids),
            rejection_code=result.rejection_code,
            source_verification=verification,
            correlation_id=update.correlation_id,
            before_status=before_status,
            after_status=result.applied_status.value
            if result.applied_status
            else before_status,
        )
        return result

    # -- HR plan review, team execution views, delays, monitoring, final review ---

    def _plan_released(self, record: OnboardingRecord) -> bool:
        return (not self.require_plan_approval) or record.plan_status is PlanStatus.APPROVED

    def _require_released(
        self, actor: Actor, record: OnboardingRecord, action: str, task_id: str | None = None
    ) -> None:
        if self._plan_released(record):
            return
        extra = {"task_id": task_id} if task_id else {}
        self.audit.record(
            actor,
            action,
            "denied_plan_not_approved",
            onboarding_id=record.onboarding_id,
            plan_status=record.plan_status.value,
            **extra,
        )
        raise PlanNotApprovedError(
            "HR has not approved the onboarding plan; teams may not execute tasks yet",
            onboarding_id=record.onboarding_id,
            plan_status=record.plan_status.value,
        )

    def _require_hr(self, actor: Actor, action: str, onboarding_id: str | None = None) -> None:
        if actor.role not in ADMIN_ROLES:
            self.audit.record(actor, action, "denied_unauthorized", onboarding_id=onboarding_id)
            raise UnauthorizedActionError(
                "only HR roles may do this", actor_role=str(actor.role)
            )

    def _save_new_request(self, request: ApprovalRequest) -> None:
        """Persist a freshly opened approval request; forget it if that fails."""
        if self.store is None:
            return
        try:
            self.store.save_approval(request)
        except BaseException:
            self.inbox.discard(request.request_id)
            raise

    @staticmethod
    def _reset_decision(request: ApprovalRequest) -> None:
        """Put a request back to pending after its decision could not be stored."""
        request.outcome = DecisionOutcome.PENDING
        request.decided_by = None
        request.decided_at = None
        request.reason = ""

    def _save_with_approval(
        self, record: OnboardingRecord, backup: OnboardingRecord, request: ApprovalRequest
    ) -> None:
        """Persist the record and its approval request together, or neither."""
        if self.store is None:
            return
        try:
            with self.store.transaction():
                self._persist(record)
                self.store.save_approval(request)
        except BaseException:
            self._client_records(record.client_id)[record.onboarding_id] = backup
            raise

    def submit_plan(self, actor: Actor, onboarding_id: str) -> ApprovalRequest:
        """Submit the proposed plan to HR for approval (``plan_approval`` gate)."""
        record = self._load(actor, onboarding_id)
        self._require_hr(actor, "plan.submit", onboarding_id)
        if record.status is not OnboardingStatus.ACTIVE:
            raise InvalidStateError("onboarding is not active", onboarding_status=str(record.status))
        if record.plan_status is not PlanStatus.PROPOSED:
            raise InvalidStateError(
                f"plan is {record.plan_status.value}; only a proposed plan can be submitted "
                "(a rejected plan must be revised first)",
                onboarding_id=onboarding_id,
            )
        backup = record.model_copy(deep=True)
        request = self.inbox.open_request(
            actor, GateName.PLAN_APPROVAL, onboarding_id,
            plan_version=record.plan_version, task_count=len(record.tasks),
        )
        record.plan_status = PlanStatus.SUBMITTED
        record.plan_events.append(
            PlanEvent(version=record.plan_version, action="submitted", by=actor.actor_id,
                      request_id=request.request_id)
        )
        record.updated_at = utc_now()
        try:
            self._save_with_approval(record, backup, request)
        except BaseException:
            self.inbox.discard(request.request_id)
            raise
        self.audit.record(
            actor, "plan.submit", "submitted", onboarding_id=onboarding_id,
            request_id=request.request_id, plan_version=record.plan_version,
            before_status=PlanStatus.PROPOSED.value, after_status=PlanStatus.SUBMITTED.value,
        )
        return request

    def decide_plan(
        self, actor: Actor, request_id: str, outcome: DecisionOutcome, reason: str = ""
    ) -> OnboardingRecord:
        """HR approves or rejects the submitted plan. Rejection requires a reason."""
        request = self.inbox.get(actor, request_id)  # client isolation
        if request.gate not in approvals.PLAN_GATES:
            self.audit.record(actor, "plan.decide", "denied_gate_mismatch",
                              onboarding_id=request.onboarding_id, request_id=request_id)
            raise UnauthorizedActionError(
                "approval request is not a plan approval", request_id=request_id,
                gate=request.gate.value,
            )
        record = self._load(actor, request.onboarding_id)
        if outcome is DecisionOutcome.REJECTED and not reason.strip():
            raise InvalidStateError("rejecting a plan requires a reason", request_id=request_id)
        if (
            record.plan_status is not PlanStatus.SUBMITTED
            or request.payload.get("plan_version") != str(record.plan_version)
        ):
            raise InvalidStateError(
                "request does not match the plan currently under review", request_id=request_id
            )
        backup = record.model_copy(deep=True)
        before = record.plan_status
        try:
            decided = self.inbox.decide(actor, request_id, outcome, reason)
        except Exception as exc:
            self.audit.record(actor, "plan.decide", "denied", onboarding_id=record.onboarding_id,
                              request_id=request_id, error=type(exc).__name__)
            raise
        record.plan_status = (
            PlanStatus.APPROVED if outcome is DecisionOutcome.APPROVED else PlanStatus.REJECTED
        )
        record.plan_events.append(
            PlanEvent(version=record.plan_version, action=outcome.value, by=actor.actor_id,
                      request_id=request_id, reason=redact_free_text(reason))
        )
        record.updated_at = utc_now()
        try:
            self._save_with_approval(record, backup, decided)
        except BaseException:
            self._reset_decision(decided)
            raise
        self.audit.record(
            actor, "plan.decide", outcome.value, onboarding_id=record.onboarding_id,
            request_id=request_id, plan_version=record.plan_version,
            reason_recorded=bool(reason.strip()), approver_id=actor.actor_id,
            before_status=before.value, after_status=record.plan_status.value,
        )
        return self._snapshot(record)

    def revise_plan(self, actor: Actor, onboarding_id: str, note: str) -> OnboardingRecord:
        """Correct a rejected plan: regenerate it from the approved configuration.

        Only possible while no task has started. Owner assignments HR already made are
        carried over by template. Creates the next plan version, back to ``proposed``.
        """
        record = self._load(actor, onboarding_id)
        self._require_hr(actor, "plan.revise", onboarding_id)
        if record.plan_status is not PlanStatus.REJECTED:
            raise InvalidStateError("only a rejected plan can be revised",
                                    plan_status=record.plan_status.value)
        if not note.strip():
            raise InvalidStateError("a plan revision must say what was corrected")
        if any(t.status not in (TaskStatus.BLOCKED, TaskStatus.READY) for t in record.tasks):
            raise InvalidStateError("work has already started; use an approved change instead")
        backup = record.model_copy(deep=True)
        config = self.registry.get(record.client_id, on=record.joining_date)
        fresh = planner.build_tasks(record, config, self._holidays_for(record.client_id))
        for task in fresh:
            old = record.task_by_key(task.template_key)
            if old is not None and old.assignee_id:
                task.assignee_id, task.assignee_team = old.assignee_id, old.assignee_team
                task.assigned_by, task.assigned_at = old.assigned_by, old.assigned_at
        record.tasks = fresh
        record.plan_version += 1
        record.plan_status = PlanStatus.PROPOSED
        record.plan_events.append(
            PlanEvent(version=record.plan_version, action="revised", by=actor.actor_id,
                      reason=note.strip())
        )
        record.updated_at = utc_now()
        self._commit(record, backup)
        self.audit.record(
            actor, "plan.revise", "revised", onboarding_id=onboarding_id,
            plan_version=record.plan_version,
            before_status=PlanStatus.REJECTED.value, after_status=PlanStatus.PROPOSED.value,
        )
        return self._snapshot(record)

    def team_summary(self, actor: Actor, onboarding_id: str, as_of: date) -> list[TeamSummary]:
        """Team-wise progress for HR. A team is a configured owner role."""
        record = self._load(actor, onboarding_id)
        self._require_hr(actor, "team.summary", onboarding_id)
        self._audit_read(actor, onboarding_id, "team_summary")
        return workflow.team_summaries(record, as_of)

    def team_tasks(
        self, actor: Actor, onboarding_id: str, team: Role | None = None
    ) -> list[TaskRecord]:
        """A team's own tasks. A non-HR actor may only view their own team's."""
        record = self._load(actor, onboarding_id)
        team = team or actor.role
        if actor.role not in ADMIN_ROLES and team is not actor.role:
            self.audit.record(actor, "team.tasks", "denied_unauthorized", onboarding_id=onboarding_id)
            raise UnauthorizedActionError("actors may only view their own team's tasks",
                                          actor_role=str(actor.role), team=str(team))
        self._audit_read(actor, onboarding_id, "team_tasks")
        return self._snapshot_tasks([t for t in record.tasks if t.owner is team])

    def report_delay(
        self, actor: Actor, onboarding_id: str, task_id: str, reason: str, *, blocker: bool = False
    ) -> TaskRecord:
        """A team reports that its task is late (or held up) and why.

        Records the reason and raises a ``delayed`` follow-up flag. It does NOT change
        the task's status: ``blocked`` still means "prerequisites unmet".
        """
        record = self._load(actor, onboarding_id)
        backup = record.model_copy(deep=True)
        self._require_released(actor, record, "task.delay", task_id)
        task = record.task_by_id(task_id)
        try:
            if task is None:
                raise InvalidStateError("unknown task", task_id=task_id)
            tracker.assert_same_client(actor, record)
            tracker.assert_can_act_on(actor, task)
            if task.status not in OPEN_STATUSES:
                raise InvalidStateError("only an open task can be reported delayed", task_id=task_id)
            if not reason.strip():
                raise InvalidStateError("a delay report must give a reason", task_id=task_id)
        except Exception as exc:
            self.audit.record(actor, "task.delay", "denied", onboarding_id=onboarding_id,
                              task_id=task_id, error=type(exc).__name__)
            raise
        task.delay_reason = redact_free_text(reason)
        task.delay_kind = "blocker" if blocker else "delay"
        task.delay_reported_by = actor.actor_id
        task.delay_reported_at = utc_now()
        record.updated_at = utc_now()
        self._commit(record, backup)
        self.audit.record(
            actor, "task.delay", task.delay_kind, onboarding_id=onboarding_id, task_id=task_id,
            reason_recorded=True, before_status=task.status.value, after_status=task.status.value,
        )
        return task.model_copy(deep=True)

    def monitoring_sweep(self, actor: Actor, onboarding_id: str, as_of: date) -> SweepResult:
        """Detect due-soon / overdue / at-risk work and record escalations once each.

        ``as_of`` is explicit, so a test clock can drive it. There is NO scheduler:
        nothing calls this automatically, and nothing is delivered - reminders are
        drafts, and an escalation is a record, not a message.
        """
        record = self._load(actor, onboarding_id)
        self._require_hr(actor, "monitor.sweep", onboarding_id)
        self._require_released(actor, record, "monitor.sweep")
        holidays = self._holidays_for(record.client_id)
        drafts = reminders.build_reminder_drafts(record, as_of, holidays=holidays)
        items = self._escalation_items(actor, record, as_of)
        seen = {
            (e.action, e.detail.get("subject_id") or e.detail.get("draft_id"),
             e.detail.get("level") or e.detail.get("as_of"), e.detail.get("subject_type", ""))
            for e in self.audit.events_for(actor)
            if e.onboarding_id == onboarding_id and e.action.startswith("monitor.")
        }
        new_escalations = new_reminders = 0
        for item in items:
            key = ("monitor.escalation", item.subject_id, item.level.value, item.subject_type)
            if key in seen:
                continue
            extra = {"task_id": item.subject_id} if item.subject_type.startswith("task") else {}
            self.audit.record(
                actor, "monitor.escalation", "recorded", onboarding_id=onboarding_id,
                subject_type=item.subject_type, subject_id=item.subject_id,
                level=item.level.value, escalate_to=item.escalate_to.value,
                reason=item.reason, as_of=as_of.isoformat(), **extra,
            )
            new_escalations += 1
        for draft in drafts:
            key = ("monitor.reminder_draft", draft.draft_id, as_of.isoformat(), "")
            if key in seen:
                continue
            self.audit.record(
                actor, "monitor.reminder_draft", "drafted_not_sent", onboarding_id=onboarding_id,
                draft_id=draft.draft_id, recipient_role=draft.recipient_role.value,
                task_count=len(draft.task_ids), as_of=as_of.isoformat(),
            )
            new_reminders += 1
        return SweepResult(
            onboarding_id=onboarding_id, as_of=as_of, reminder_drafts=drafts, escalations=items,
            newly_recorded_escalations=new_escalations, newly_recorded_reminders=new_reminders,
        )

    # -- team assignment drafts and the send gate -------------------------------------

    _DRAFT_ACTIONS = ("draft.team_prepared", "draft.send_authorised", "draft.send_result")

    def _draft_events(self, actor: Actor, onboarding_id: str, draft_id: str):
        return [e for e in self.audit.events_for(actor)
                if e.onboarding_id == onboarding_id and e.action in self._DRAFT_ACTIONS
                and e.detail.get("draft_id") == draft_id]

    def prepare_team_drafts(self, actor: Actor, onboarding_id: str) -> list[ReminderDraft]:
        """One draft per owning role for a RELEASED plan. Idempotent; nothing is sent."""
        record = self._load(actor, onboarding_id)
        self._require_hr(actor, "draft.prepare", onboarding_id)
        self._require_released(actor, record, "draft.prepare")
        config = self.registry.configs.get(record.client_id)
        drafts = drafts_module.build_team_drafts(record, config)
        for draft in drafts:
            if not self._draft_events(actor, onboarding_id, draft.draft_id):
                self.audit.record(
                    actor, "draft.team_prepared", "drafted_not_sent", onboarding_id=onboarding_id,
                    draft_id=draft.draft_id, recipient_role=draft.recipient_role.value,
                    task_count=len(draft.task_ids), prepared_by=actor.actor_id)
        return drafts

    def _find_draft(self, actor: Actor, onboarding_id: str, draft_id: str) -> ReminderDraft:
        if not self._draft_events(actor, onboarding_id, draft_id):
            raise InvalidStateError("no such draft has been prepared", draft_id=draft_id)
        record = self._load(actor, onboarding_id)
        for draft in drafts_module.build_team_drafts(record, self.registry.configs.get(record.client_id)):
            if draft.draft_id == draft_id:
                return draft
        raise InvalidStateError("the draft no longer matches the plan; prepare drafts again", draft_id=draft_id)

    def authorise_send(self, actor: Actor, onboarding_id: str, draft_id: str) -> None:
        """An HR lead, who did not prepare the draft, authorises it for sending. Sends nothing."""
        self._load(actor, onboarding_id)
        if actor.role is not Role.HR_LEAD:
            self.audit.record(actor, "draft.send_authorised", "denied_unauthorized",
                              onboarding_id=onboarding_id, draft_id=draft_id)
            raise UnauthorizedActionError("only an HR lead may authorise sending a draft", actor_role=str(actor.role))
        events = self._draft_events(actor, onboarding_id, draft_id)
        prepared = [e for e in events if e.action == "draft.team_prepared"]
        if not prepared:
            raise InvalidStateError("no such draft has been prepared", draft_id=draft_id)
        if any(e.detail.get("prepared_by") == actor.actor_id for e in prepared):
            raise UnauthorizedActionError("the preparer may not authorise sending their own draft", draft_id=draft_id)
        if not any(e.action == "draft.send_authorised" and e.outcome == "authorised" for e in events):
            self.audit.record(actor, "draft.send_authorised", "authorised", onboarding_id=onboarding_id,
                              draft_id=draft_id, approver_id=actor.actor_id)

    def send_draft(self, actor: Actor, onboarding_id: str, draft_id: str, sender=None):
        """Send an authorised draft through ``sender``; record the result truthfully, success or not.

        Refused without authorisation and once a delivery is recorded; after ``MAX_SEND_ATTEMPTS`` failures it stops.
        A provider reporting delivery is recorded as that provider's outcome; UC-02 ships none that is real.
        """
        self._require_hr(actor, "draft.send", onboarding_id)
        draft = self._find_draft(actor, onboarding_id, draft_id)
        events = self._draft_events(actor, onboarding_id, draft_id)
        if not any(e.action == "draft.send_authorised" and e.outcome == "authorised" for e in events):
            raise ApprovalRequiredError("sending needs HR-lead authorisation first", draft_id=draft_id)
        results = [e for e in events if e.action == "draft.send_result"]
        done = [e for e in results if e.outcome in (drafts_module.OUTCOME_SENT, drafts_module.OUTCOME_MOCK)]
        if done:
            return drafts_module.SendResult(done[-1].outcome, reference=done[-1].detail.get("reference", ""))
        failures = [e for e in results if e.outcome == drafts_module.OUTCOME_FAILED]
        if len(failures) >= drafts_module.MAX_SEND_ATTEMPTS:
            raise InvalidStateError("send attempts exhausted for this draft", draft_id=draft_id,
                                    attempts=str(len(failures)))
        sender = sender or drafts_module.NoTransportSender()
        try:
            result = sender.send(draft)
        except Exception as exc:  # a transport error is a result, not a crash
            result = drafts_module.SendResult(drafts_module.OUTCOME_FAILED, error=type(exc).__name__)
        self.audit.record(actor, "draft.send_result", result.outcome, onboarding_id=onboarding_id,
                          draft_id=draft_id, provider=getattr(sender, "name", "unknown"), real=str(result.real),
                          reference=result.reference, error=result.error)
        return result

    # -- DL-14: employee does not respond ------------------------------------

    _RMD = ("reminder.attempt", "reminder.employee_response", "reminder.escalated",
            "reminder.follow_up")

    def _reminder_status(self, actor: Actor, record: OnboardingRecord, task: TaskRecord,
                         policy: NonResponsePolicy) -> ReminderStatus:
        events = [e for e in self.audit.events_for(actor)
                  if e.onboarding_id == record.onboarding_id and e.task_id == task.task_id
                  and e.action in self._RMD]
        attempts = sorted(date.fromisoformat(e.detail["as_of"]) for e in events
                          if e.action == "reminder.attempt")
        responded = any(e.action == "reminder.employee_response" for e in events)
        escalated = any(e.action == "reminder.escalated" for e in events)
        outcomes = [e.detail.get("note", "") for e in events if e.action == "reminder.follow_up"]
        last = attempts[-1] if attempts else None
        if outcomes:
            state = "follow_up_done"
        elif responded:
            state = "stopped_responded"
        elif task.status not in OPEN_STATUSES:
            state = "stopped_closed"
        elif escalated:
            state = "escalated"
        else:
            state = "active"
        nxt = (last + timedelta(days=policy.interval_days)
               if state == "active" and last and len(attempts) < policy.max_attempts else None)
        return ReminderStatus(
            task_id=task.task_id, state=state, attempts=len(attempts),
            max_attempts=policy.max_attempts, interval_days=policy.interval_days,
            last_attempt_on=last, next_attempt_on=nxt,
            follow_up_required=state == "escalated",
            follow_up_outcome=outcomes[-1] if outcomes else "",
        )

    def _dl14_context(self, actor: Actor, onboarding_id: str, task_id: str, action: str):
        record = self._load(actor, onboarding_id)
        self._require_hr(actor, action, onboarding_id)
        self._require_released(actor, record, action)
        task = record.task_by_id(task_id)
        if task is None:
            raise InvalidStateError("unknown task", onboarding_id=onboarding_id, task_id=task_id)
        policy = self.registry.get(record.client_id, on=record.joining_date).non_response_policy
        if policy is None:
            raise MissingConfigurationError(
                "the client has approved no non-response reminder policy",
                client_id=record.client_id,
            )
        return record, task, policy

    def reminder_status(self, actor: Actor, onboarding_id: str, task_id: str) -> ReminderStatus:
        record, task, policy = self._dl14_context(actor, onboarding_id, task_id, "reminder.status")
        return self._reminder_status(actor, record, task, policy)

    def record_reminder_attempt(self, actor: Actor, onboarding_id: str, task_id: str,
                                as_of: date) -> ReminderStatus:
        """Record one reminder attempt, or return the unchanged status. Nothing is sent.

        Not recorded when the employee already responded, the task is closed, the interval has not
        elapsed, the limit is reached, or ``as_of`` repeats the last attempt (a replay).
        """
        record, task, policy = self._dl14_context(actor, onboarding_id, task_id, "reminder.attempt")
        status = self._reminder_status(actor, record, task, policy)
        if status.state != "active" or status.attempts >= policy.max_attempts:
            return status
        if status.next_attempt_on is not None and as_of < status.next_attempt_on:
            return status
        if status.last_attempt_on is not None and as_of <= status.last_attempt_on:
            return status
        self.audit.record(
            actor, "reminder.attempt", "recorded_not_sent", onboarding_id=onboarding_id,
            task_id=task_id, as_of=as_of.isoformat(), attempt=str(status.attempts + 1),
            recipient_role=Role.EMPLOYEE.value,
        )
        return self._reminder_status(actor, record, task, policy)

    def escalate_unanswered(self, actor: Actor, onboarding_id: str, task_id: str,
                            as_of: date) -> ReminderStatus:
        """After the last attempt and one further interval with no reply, escalate once."""
        record, task, policy = self._dl14_context(actor, onboarding_id, task_id, "reminder.escalate")
        status = self._reminder_status(actor, record, task, policy)
        if (status.state == "active" and status.attempts >= policy.max_attempts
                and status.last_attempt_on is not None
                and as_of >= status.last_attempt_on + timedelta(days=policy.interval_days)):
            self.audit.record(
                actor, "reminder.escalated", "manual_follow_up_required",
                onboarding_id=onboarding_id, task_id=task_id, as_of=as_of.isoformat(),
                escalate_to=Role.HR_OFFICER.value, attempts=str(status.attempts),
            )
            status = self._reminder_status(actor, record, task, policy)
        return status

    def record_employee_response(self, actor: Actor, onboarding_id: str,
                                 task_id: str, as_of: date) -> ReminderStatus:
        """The employee answered: stop reminding. Idempotent."""
        record, task, policy = self._dl14_context(actor, onboarding_id, task_id, "reminder.response")
        if self._reminder_status(actor, record, task, policy).state != "stopped_responded":
            self.audit.record(actor, "reminder.employee_response", "recorded",
                              onboarding_id=onboarding_id, task_id=task_id,
                              as_of=as_of.isoformat())
        return self._reminder_status(actor, record, task, policy)

    def record_follow_up_outcome(self, actor: Actor, onboarding_id: str, task_id: str,
                                 outcome_code: str) -> ReminderStatus:
        """HR's manual follow-up result, as a code (no free text, no personal data)."""
        record, task, policy = self._dl14_context(actor, onboarding_id, task_id, "reminder.follow_up")
        if not self._reminder_status(actor, record, task, policy).follow_up_required:
            raise InvalidStateError("no manual follow-up is outstanding for this task",
                                    task_id=task_id)
        self.audit.record(actor, "reminder.follow_up", "recorded", onboarding_id=onboarding_id,
                          task_id=task_id, note=outcome_code)
        return self._reminder_status(actor, record, task, policy)

    def escalation_history(self, actor: Actor, onboarding_id: str) -> list[EscalationRecord]:
        """Recorded escalations with whether the work behind each is now resolved."""
        record = self._load(actor, onboarding_id)
        self._audit_read(actor, onboarding_id, "escalation_history")
        out = []
        for event in self.audit.events_for(actor):
            if event.onboarding_id != onboarding_id or event.action != "monitor.escalation":
                continue
            subject = event.detail.get("subject_id", "")
            task = (
                record.task_by_id(subject)
                if event.detail.get("subject_type", "").startswith("task")
                else None
            )
            resolved = task is not None and task.status not in OPEN_STATUSES
            out.append(
                EscalationRecord(
                    subject_type=event.detail.get("subject_type", ""), subject_id=subject,
                    level=event.detail.get("level", ""), escalate_to=event.detail.get("escalate_to", ""),
                    reason=event.detail.get("reason", ""), as_of=event.detail.get("as_of", ""),
                    recorded_at=event.at, status="resolved" if resolved else "open",
                )
            )
        return out

    def _readiness(
        self, actor: Actor, record: OnboardingRecord, as_of: date, *, require_final: bool
    ) -> JoiningReadiness:
        ignore = set() if require_final else set(approvals.FINAL_GATES)
        pending = [
            r.request_id
            for r in self.inbox.pending(actor)
            if r.onboarding_id == record.onboarding_id and r.gate not in ignore
        ]
        return workflow.readiness(
            record, as_of, self._holidays_for(record.client_id),
            pending_approval_ids=pending,
            escalation_count=len(self._escalation_items(actor, record, as_of)),
            require_final_approval=require_final,
        )

    def joining_readiness(self, actor: Actor, onboarding_id: str, as_of: date) -> JoiningReadiness:
        """Readiness from actual task and approval state; never granted by a report existing."""
        record = self._load(actor, onboarding_id)
        self._audit_read(actor, onboarding_id, "joining_readiness")
        return self._readiness(actor, record, as_of, require_final=True)

    def submit_final(self, actor: Actor, onboarding_id: str, as_of: date) -> ApprovalRequest:
        """Consolidate onboarding status and submit it to HR (``final_review`` gate).

        Submitting does not complete anything: only HR's approval can, and only when the
        work is actually done.
        """
        record = self._load(actor, onboarding_id)
        self._require_hr(actor, "final.submit", onboarding_id)
        if record.status is not OnboardingStatus.ACTIVE:
            raise InvalidStateError("onboarding is not active", onboarding_status=str(record.status))
        if record.plan_status is not PlanStatus.APPROVED:
            raise PlanNotApprovedError("the plan must be approved before a final submission",
                                       plan_status=record.plan_status.value)
        if record.final_status in (FinalStatus.SUBMITTED, FinalStatus.APPROVED):
            raise InvalidStateError(f"final submission is already {record.final_status.value}")
        backup = record.model_copy(deep=True)
        snapshot = self._readiness(actor, record, as_of, require_final=False)
        version = len(record.final_submissions) + 1
        request = self.inbox.open_request(
            actor, GateName.FINAL_REVIEW, onboarding_id, submission_version=version,
            ready=snapshot.status == "granted",
        )
        teams = workflow.team_summaries(record, as_of)
        record.final_submissions.append(
            FinalSubmission(
                version=version, submitted_by=actor.actor_id, as_of=as_of,
                request_id=request.request_id, ready=snapshot.status == "granted",
                total_tasks=snapshot.total_tasks, complete=snapshot.complete,
                pending=snapshot.pending, blocked=snapshot.blocked, overdue=snapshot.overdue,
                outstanding_task_ids=snapshot.outstanding_task_ids,
                outstanding_by_category=snapshot.outstanding_by_category,
                evidence_refs=sorted(t.evidence_ref for t in record.tasks if t.evidence_ref),
                team_completion={t.team: f"{t.complete}/{t.total}" for t in teams},
                escalation_count=snapshot.escalation_count, joining_risk=snapshot.joining_risk,
                reasons=snapshot.reasons,
            )
        )
        before = record.final_status
        record.final_status = FinalStatus.SUBMITTED
        record.updated_at = utc_now()
        try:
            self._save_with_approval(record, backup, request)
        except BaseException:
            self.inbox.discard(request.request_id)
            raise
        self.audit.record(
            actor, "final.submit", "submitted", onboarding_id=onboarding_id,
            request_id=request.request_id, submission_version=version,
            before_status=before.value, after_status=FinalStatus.SUBMITTED.value,
            ready=snapshot.status == "granted", outstanding=len(snapshot.outstanding_task_ids),
        )
        return request

    def decide_final(
        self, actor: Actor, request_id: str, outcome: DecisionOutcome, reason: str = ""
    ) -> OnboardingRecord:
        """HR approves or rejects the final submission.

        Approval is refused while required work is outstanding (HR must reject
        instead); rejection requires a reason and returns the outstanding tasks for
        correction. Earlier submissions and decisions are kept.
        """
        request = self.inbox.get(actor, request_id)
        if request.gate not in approvals.FINAL_GATES:
            self.audit.record(actor, "final.decide", "denied_gate_mismatch",
                              onboarding_id=request.onboarding_id, request_id=request_id)
            raise UnauthorizedActionError("approval request is not a final review",
                                          request_id=request_id, gate=request.gate.value)
        record = self._load(actor, request.onboarding_id)
        if outcome is DecisionOutcome.REJECTED and not reason.strip():
            raise InvalidStateError("rejecting a final submission requires a reason",
                                    request_id=request_id)
        latest = record.final_submissions[-1] if record.final_submissions else None
        if record.final_status is not FinalStatus.SUBMITTED or latest is None or latest.request_id != request_id:
            raise InvalidStateError("request does not match the final submission under review",
                                    request_id=request_id)
        current = self._readiness(actor, record, date.fromisoformat(latest.as_of.isoformat()),
                                  require_final=False)
        if outcome is DecisionOutcome.APPROVED and current.status != "granted":
            self.audit.record(actor, "final.decide", "denied_not_ready",
                              onboarding_id=record.onboarding_id, request_id=request_id,
                              outstanding=len(current.outstanding_task_ids))
            raise InvalidStateError(
                "cannot approve: required work is outstanding; reject the submission instead",
                request_id=request_id, reasons=" | ".join(current.reasons),
            )
        backup = record.model_copy(deep=True)
        before = record.final_status
        try:
            decided = self.inbox.decide(actor, request_id, outcome, reason)
        except Exception as exc:
            self.audit.record(actor, "final.decide", "denied", onboarding_id=record.onboarding_id,
                              request_id=request_id, error=type(exc).__name__)
            raise
        latest = record.final_submissions[-1]
        latest.decision = outcome.value
        latest.decided_by = actor.actor_id
        latest.decided_at = decided.decided_at
        latest.decision_reason = redact_free_text(reason)
        if outcome is DecisionOutcome.APPROVED:
            record.final_status = FinalStatus.APPROVED
        else:
            record.final_status = FinalStatus.REJECTED
            latest.returned_task_ids = sorted(
                set(latest.outstanding_task_ids) | set(current.outstanding_task_ids)
                | set(current.missing_evidence_task_ids)
            )
        record.updated_at = utc_now()
        try:
            self._save_with_approval(record, backup, decided)
        except BaseException:
            self._reset_decision(decided)
            raise
        self.audit.record(
            actor, "final.decide", outcome.value, onboarding_id=record.onboarding_id,
            request_id=request_id, reason_recorded=bool(reason.strip()), approver_id=actor.actor_id,
            before_status=before.value, after_status=record.final_status.value,
            returned=len(latest.returned_task_ids),
        )
        return self._snapshot(record)

    def follow_up_flags(
        self, actor: Actor, onboarding_id: str, as_of: date
    ) -> list[FollowUpFlag]:
        """What needs chasing, and who should chase it.

        Derived from the record and ``as_of``; nothing is stored. **No
        notification is sent** -- UC-02 has no transport, so a flag records
        that follow-up is needed, never that anyone was told.
        """
        record = self._load(actor, onboarding_id)
        self._audit_read(actor, onboarding_id, "follow_up_flags")
        return tracker.follow_up_flags(record, as_of, self._holidays_for(record.client_id))

    def ready_tasks(self, actor: Actor, onboarding_id: str) -> list[TaskRecord]:
        return self._snapshot_tasks(planner.ready_tasks(self._load(actor, onboarding_id)))

    def blocked_tasks(self, actor: Actor, onboarding_id: str) -> list[TaskRecord]:
        return self._snapshot_tasks(planner.blocked_tasks(self._load(actor, onboarding_id)))

    def parallel_batches(self, actor: Actor, onboarding_id: str) -> list[list[str]]:
        return planner.parallel_batches(self._load(actor, onboarding_id))

    def critical_path(self, actor: Actor, onboarding_id: str):
        """The chain of open tasks with the least room before day one (see :mod:`schedule`)."""
        record = self._load(actor, onboarding_id)
        return schedule.critical_path(record, self._holidays_for(record.client_id))

    def delay_impact(self, actor: Actor, onboarding_id: str, task_id: str, as_of: date):
        """What a late task holds up. Describes the consequence; changes no date."""
        record = self._load(actor, onboarding_id)
        return schedule.delay_impact(record, task_id, as_of, self._holidays_for(record.client_id))

    # -- gated change ------------------------------------------------------

    def request_change(
        self,
        actor: Actor,
        onboarding_id: str,
        *,
        joining_date: date | None = None,
        location: str | None = None,
        project: str | None = None,
        location_status: str | None = None,
    ) -> ApprovalRequest:
        """Open a hard gate for an attribute change. Nothing is re-planned yet."""
        record = self._load(actor, onboarding_id)
        if record.status is not OnboardingStatus.ACTIVE:
            raise InvalidStateError(
                "onboarding is not active", onboarding_status=str(record.status)
            )

        drivers: list[ChangeDriver] = []
        if joining_date is not None and joining_date != record.joining_date:
            drivers.append(ChangeDriver.JOINING_DATE)
        if location is not None and location != record.location:
            drivers.append(ChangeDriver.LOCATION)
        if project is not None and project != record.project:
            drivers.append(ChangeDriver.PROJECT)
        if location_status is not None:
            if not location_status.strip():
                raise InvalidStateError("location status must not be blank")
            if location_status != planner.current_location_status(record):
                drivers.append(ChangeDriver.LOCATION_STATUS)
        if not drivers:
            raise InvalidStateError(
                "change request does not alter joining date, location, project or location status"
            )
        if len(drivers) > 1:
            # Each driver has its own approver role; mixing them in one request
            # would let one approver authorise a change outside their remit.
            raise InvalidStateError(
                "request one attribute change at a time",
                drivers=",".join(d.value for d in drivers),
            )

        driver = drivers[0]
        # Fail fast: refuse an unschedulable date at request time rather than
        # letting it sit in an approval queue only to blow up on apply (N3).
        if joining_date is not None:
            assert_covered(joining_date, self._holidays_for(record.client_id))

        payload = {"driver": driver.value}
        if joining_date is not None:
            payload["joining_date"] = joining_date.isoformat()
        if location is not None:
            payload["location"] = location
        if project is not None:
            payload["project"] = project
        if location_status is not None:
            payload["location_status"] = location_status

        request = self.inbox.open_request(
            actor, CHANGE_GATES[driver], onboarding_id, **payload
        )
        self._save_new_request(request)
        self.audit.record(
            actor,
            "change.request",
            "pending_approval",
            onboarding_id=onboarding_id,
            gate=request.gate.value,
            request_id=request.request_id,
        )
        return request

    def decide_change(
        self,
        actor: Actor,
        request_id: str,
        outcome: DecisionOutcome,
        reason: str = "",
    ) -> ApprovalRequest:
        try:
            request = self.inbox.decide(actor, request_id, outcome, reason)
        except Exception as exc:
            self.audit.record(
                actor,
                "change.decide",
                "denied",
                request_id=request_id,
                error=type(exc).__name__,
            )
            raise
        if self.store is not None:
            try:
                self.store.save_approval(request)
            except BaseException:
                self._reset_decision(request)  # memory must not run ahead of storage
                raise
        self.audit.record(
            actor,
            "change.decide",
            request.outcome.value,
            onboarding_id=request.onboarding_id,
            request_id=request_id,
            gate=request.gate.value,
            approver_id=actor.actor_id,
        )
        return request

    def apply_change(self, actor: Actor, request_id: str) -> ReplanOutcome:
        """Re-plan affected tasks. Requires an approved *change* gate decision."""
        try:
            request = self.inbox.require_approved(
                actor, request_id, approvals.CHANGE_GATES
            )
        except UnauthorizedActionError as exc:
            # Security event: an approval for another gate was replayed here.
            self.audit.record(
                actor,
                "change.apply",
                "denied_gate_mismatch",
                request_id=request_id,
                error=type(exc).__name__,
                **{k: str(v) for k, v in exc.context.items() if k != "request_id"},
            )
            raise
        except Exception as exc:
            self.audit.record(
                actor,
                "change.apply",
                "denied",
                request_id=request_id,
                error=type(exc).__name__,
            )
            raise

        record = self._load(actor, request.onboarding_id)
        backup = record.model_copy(deep=True)
        # Re-check state at apply time, not only at request time. A change
        # requested while the onboarding was active but approved after it was
        # cancelled would otherwise re-plan a cancelled onboarding (N1).
        if record.status is not OnboardingStatus.ACTIVE:
            self.audit.record(
                actor,
                "change.apply",
                "denied_not_active",
                onboarding_id=record.onboarding_id,
                request_id=request_id,
                onboarding_status=str(record.status),
            )
            raise InvalidStateError(
                "onboarding is not active", onboarding_status=str(record.status)
            )

        config = self.registry.get(record.client_id)
        payload = request.payload
        outcome = planner.replan(
            record,
            config,
            joining_date=(
                date.fromisoformat(payload["joining_date"])
                if "joining_date" in payload
                else None
            ),
            location=payload.get("location"),
            project=payload.get("project"),
            location_status=payload.get("location_status"),
            holidays=self._holidays_for(record.client_id),
        )
        record.plan_version += 1
        record.plan_events.append(
            PlanEvent(
                version=record.plan_version,
                action="replanned",
                by=actor.actor_id,
                request_id=request_id,
                reason=",".join(d.value for d in outcome.drivers),
            )
        )
        if self.store is not None:
            try:
                with self.store.transaction():  # the new plan and its revision row, or neither
                    self._persist(record)
                    self.store.save_plan_revision(
                        outcome,
                        client_id=record.client_id,
                        applied_by=actor.actor_id,
                        approval_request_id=request_id,
                    )
            except BaseException:
                self._client_records(record.client_id)[record.onboarding_id] = backup
                raise
        self.audit.record(
            actor,
            "change.apply",
            "replanned",
            onboarding_id=record.onboarding_id,
            request_id=request_id,
            added=len(outcome.added_task_ids),
            removed=len(outcome.removed_task_ids),
            rescheduled=len(outcome.rescheduled_task_ids),
            revision=outcome.revision,
        )
        return outcome

    # -- document requirements and document-driven replanning ---------------
    #
    # In the orchestrated workflow UC-02 plans first and UC-03 collects and validates the documents.
    # UC-02 owns WHICH documents the plan requires (with a version); UC-03 owns their collection,
    # storage, versions and validation, and the catalog of validatable document types. UC-03 reports
    # back through DocumentFeedback when the plan must change; UC-02 replans and versions the plan.

    def define_document_requirements(
        self, actor: Actor, onboarding_id: str, specs: list[DocumentSpec]
    ) -> OnboardingRecord:
        """Put the initial document requirements on the plan (the current plan version).

        Idempotent: the same requirements again change nothing. Once requirements are defined, a
        different list is refused -- that is a replan (``replan_documents``), which versions it.
        """
        record = self._load(actor, onboarding_id)
        self._require_hr(actor, "documents.define", onboarding_id)
        if record.status is not OnboardingStatus.ACTIVE:
            raise InvalidStateError("onboarding is not active", onboarding_status=str(record.status))
        keys = [s.document_type for s in specs]
        if not specs or len(set(keys)) != len(keys):
            raise InvalidStateError("document requirements must list each document type once")
        if record.documents:
            if [(d.key, d.mandatory) for d in record.documents] == [(s.document_type, s.mandatory) for s in specs]:
                return self._snapshot(record)
            raise InvalidStateError(
                "document requirements are already defined for this plan; changing them is a replan",
                plan_version=record.plan_version,
            )
        backup = record.model_copy(deep=True)
        record.documents = [
            DocumentItem(key=s.document_type, document_type=s.document_type, mandatory=s.mandatory,
                         reason=s.reason or "required for this employee", allow_not_applicable=s.allow_not_applicable)
            for s in specs
        ]
        record.updated_at = utc_now()
        self._commit(record, backup)
        self.audit.record(actor, "documents.define", "defined", onboarding_id=onboarding_id,
                          plan_version=record.plan_version, count=len(specs))
        return self._snapshot(record)

    def replan_documents(
        self, actor: Actor, onboarding_id: str, feedback: DocumentFeedback
    ) -> DocumentReplanOutcome:
        """Replan on UC-03's validation feedback: add or drop document requirements, switch on the
        templates of newly established attributes, and create the next plan version.

        * Idempotent by ``feedback.feedback_id``: a replay returns the first outcome
          (``duplicate_replay=True``) and changes nothing.
        * Stale feedback (another plan version) is refused with StalePlanVersionError.
        * A verified document is never dropped; completed tasks are never reopened or removed.
        * Feedback that changes nothing is refused (no empty plan versions).
        """
        record = self._load(actor, onboarding_id)
        self._require_hr(actor, "plan.replan_documents", onboarding_id)
        ledger_key = f"docfb:{feedback.feedback_id}"
        if ledger_key in record.flow_ledger:
            replay = DocumentReplanOutcome.model_validate_json(record.flow_ledger[ledger_key])
            return replay.model_copy(update={"duplicate_replay": True})
        if record.status is not OnboardingStatus.ACTIVE:
            raise InvalidStateError("onboarding is not active", onboarding_status=str(record.status))
        if feedback.plan_version != record.plan_version:
            self.audit.record(actor, "plan.replan_documents", "denied_stale", onboarding_id=onboarding_id,
                              plan_version=record.plan_version, feedback_plan_version=feedback.plan_version)
            raise StalePlanVersionError(
                "the feedback was made against another plan version",
                plan_version=record.plan_version, feedback_plan_version=feedback.plan_version,
            )
        have = {d.key: d for d in record.documents}
        added = [s for s in feedback.add_documents if s.document_type not in have]
        removable = [k for k in dict.fromkeys(feedback.remove_documents)
                     if k in have and have[k].state is not DocumentState.VERIFIED]
        retained = [k for k in dict.fromkeys(feedback.remove_documents) if k in have and k not in removable]
        new_attributes = [a for a in dict.fromkeys(feedback.add_attributes) if a and a not in record.attributes]
        if not added and not removable and not new_attributes:
            raise InvalidStateError("the feedback changes nothing in the current plan",
                                    plan_version=record.plan_version)

        backup = record.model_copy(deep=True)
        previous = record.plan_version
        try:
            task_outcome = planner.replan(record, self.registry.get(record.client_id),
                                          holidays=self._holidays_for(record.client_id),
                                          add_attributes=tuple(new_attributes))
            record.documents = [d for d in record.documents if d.key not in removable] + [
                DocumentItem(key=s.document_type, document_type=s.document_type, mandatory=s.mandatory,
                             reason=s.reason or "required by document validation",
                             allow_not_applicable=s.allow_not_applicable)
                for s in added
            ]
            record.plan_version += 1
            reasons = [r[:80] for r in feedback.reasons][:10]
            record.plan_events.append(PlanEvent(version=record.plan_version, action="replanned_documents",
                                                by=actor.actor_id, request_id=feedback.feedback_id,
                                                reason=";".join(reasons)))
            outcome = DocumentReplanOutcome(
                onboarding_id=onboarding_id, plan_version=record.plan_version, previous_plan_version=previous,
                feedback_id=feedback.feedback_id, added_documents=[s.document_type for s in added],
                removed_documents=removable, retained_documents=retained,
                added_task_ids=list(task_outcome.added_task_ids), removed_task_ids=list(task_outcome.removed_task_ids),
                rescheduled_task_ids=list(task_outcome.rescheduled_task_ids),
                effective_joining_date=record.joining_date, reasons=reasons,
            )
            record.flow_ledger[ledger_key] = outcome.model_dump_json()
            record.updated_at = utc_now()
        except BaseException:
            self._client_records(record.client_id)[record.onboarding_id] = backup
            raise
        self._commit(record, backup)
        self.audit.record(actor, "plan.replan_documents", "replanned", onboarding_id=onboarding_id,
                          plan_version=record.plan_version, previous_plan_version=previous,
                          added_documents=len(outcome.added_documents),
                          removed_documents=len(outcome.removed_documents),
                          added_tasks=len(outcome.added_task_ids), removed_tasks=len(outcome.removed_task_ids))
        return outcome

    # -- cancellation ------------------------------------------------------

    def request_cancellation(
        self, actor: Actor, onboarding_id: str, reason: str
    ) -> ApprovalRequest:
        record = self._load(actor, onboarding_id)
        if record.status is not OnboardingStatus.ACTIVE:
            raise InvalidStateError(
                "onboarding is not active", onboarding_status=str(record.status)
            )
        request = self.inbox.open_request(
            actor, GateName.CANCELLATION, onboarding_id, reason=reason
        )
        self._save_new_request(request)
        self.audit.record(
            actor,
            "onboarding.cancel_request",
            "pending_approval",
            onboarding_id=onboarding_id,
            request_id=request.request_id,
        )
        return request

    def apply_cancellation(self, actor: Actor, request_id: str) -> OnboardingRecord:
        """Cancel the onboarding and every still-open task.

        Accepts only an approved *cancellation* gate. Any other approved gate is
        a security event, not a usable authorisation (finding C1).
        """
        try:
            request = self.inbox.require_approved(
                actor, request_id, approvals.CANCELLATION_GATES
            )
        except UnauthorizedActionError as exc:
            self.audit.record(
                actor,
                "onboarding.cancel",
                "denied_gate_mismatch",
                request_id=request_id,
                error=type(exc).__name__,
                **{k: str(v) for k, v in exc.context.items() if k != "request_id"},
            )
            raise
        except Exception as exc:
            self.audit.record(
                actor,
                "onboarding.cancel",
                "denied",
                request_id=request_id,
                error=type(exc).__name__,
            )
            raise

        record = self._load(actor, request.onboarding_id)
        backup = record.model_copy(deep=True)
        # An approved cancellation must not be replayable against an
        # onboarding that is already cancelled (N2).
        if record.status is not OnboardingStatus.ACTIVE:
            self.audit.record(
                actor,
                "onboarding.cancel",
                "denied_not_active",
                onboarding_id=record.onboarding_id,
                request_id=request_id,
                onboarding_status=str(record.status),
            )
            raise InvalidStateError(
                "onboarding is not active", onboarding_status=str(record.status)
            )

        cancelled = 0
        for task in record.tasks:
            if task.status in OPEN_STATUSES:
                task.status = TaskStatus.CANCELLED
                task.cancelled_at = utc_now()
                cancelled += 1
        record.status = OnboardingStatus.CANCELLED
        record.updated_at = utc_now()

        self._commit(record, backup)
        self.audit.record(
            actor,
            "onboarding.cancel",
            "cancelled",
            onboarding_id=record.onboarding_id,
            request_id=request_id,
            cancelled_tasks=cancelled,
        )
        return self._snapshot(record)

    # -- status, risk, reminders ------------------------------------------

    def status(self, actor: Actor, onboarding_id: str, as_of: date) -> TaskSummary:
        record = self._load(actor, onboarding_id)
        self._audit_read(actor, onboarding_id, "status")
        return tracker.summarise(record, as_of, self._holidays_for(record.client_id))

    def joining_risk(
        self, actor: Actor, onboarding_id: str, as_of: date
    ) -> JoiningRisk:
        record = self._load(actor, onboarding_id)
        self._audit_read(actor, onboarding_id, "joining_risk")
        risk = tracker.assess_joining_risk(record, as_of, self._holidays_for(record.client_id))
        if risk.at_risk:
            self.audit.record(
                actor,
                "risk.detected",
                "at_risk",
                onboarding_id=onboarding_id,
                overdue=len(risk.overdue_task_ids),
                critical_open=len(risk.critical_open_task_ids),
            )
        return risk

    def milestones(
        self, actor: Actor, onboarding_id: str, as_of: date
    ) -> list[MilestoneState]:
        """Evaluate the client's declared milestones against the current plan.

        Derived on demand, so a re-plan is reflected immediately. Audited as a
        read of T2 data, like :meth:`status`.
        """
        record = self._load(actor, onboarding_id)
        self._audit_read(actor, onboarding_id, "milestones")
        config = self.registry.get(record.client_id)
        return milestones.evaluate_all(record, config, as_of, self._holidays_for(record.client_id))

    def outstanding_activities(
        self, actor: Actor, onboarding_id: str, as_of: date
    ) -> list[OutstandingActivity]:
        """Outstanding activities with responsible owner and follow-up (BR-10)."""
        record = self._load(actor, onboarding_id)
        self._audit_read(actor, onboarding_id, "outstanding_activities")
        return tracker.outstanding_activities(record, as_of, self._holidays_for(record.client_id))

    def checklist(
        self, actor: Actor, onboarding_id: str, as_of: date
    ) -> ChecklistView:
        """UC02-BR-02. The structured, explained checklist for an onboarding.

        Every task carries the decision that put it in the plan; every
        configured template that was left out carries the reason it was left
        out. Readiness, blocking prerequisites, overdue state and
        approval-required state are derived from the stored record and the
        explicit ``as_of`` date — nothing new is persisted, so the view cannot
        contradict the record it came from.

        Audited as a read of T2 data, like :meth:`status`.
        """
        record = self._load(actor, onboarding_id)
        self._audit_read(actor, onboarding_id, "checklist")
        config = self.registry.get(record.client_id)
        pending = [
            request.request_id
            for request in self.inbox.pending(actor)
            if request.onboarding_id == onboarding_id
        ]
        view = selection.build_view(
            record,
            config,
            as_of,
            self._holidays_for(record.client_id),
            milestone_states=milestones.evaluate_all(
                record, config, as_of, self._holidays_for(record.client_id)
            ),
            pending_gate_request_ids=pending,
        )
        if view.review_required:
            self.audit.record(
                actor,
                "checklist.review_required",
                "raised",
                onboarding_id=onboarding_id,
                issue_count=len(view.issues),
            )
        return view

    def preview_checklist(
        self, actor: Actor, context: ChecklistContext
    ) -> ChecklistPreview:
        """What a context *would* produce, without creating anything.

        Lets a caller — or an agent — ask "which tasks would this employee
        get, and why?" before an onboarding exists, and compare two contexts.
        Read-only: no record, no task and no audit subject is created, so no
        onboarding-scoped read event is emitted.
        """
        if actor.client_id != context.client_id:
            self.audit.record(actor, "checklist.preview", "denied_isolation")
            raise DataIsolationError(
                "actor may not preview another client's checklist",
                actor_client_id=actor.client_id,
                client_id=context.client_id,
            )
        if actor.role not in ADMIN_ROLES:
            self.audit.record(actor, "checklist.preview", "denied_unauthorized")
            raise UnauthorizedActionError(
                "only HR roles may preview a checklist", actor_role=str(actor.role)
            )
        config = self.registry.get(context.client_id, on=context.joining_date)
        self._require_eligible(actor, config, context, "checklist.preview")
        result = selection.preview(config, context, self._holidays_for(context.client_id))
        self.audit.record(
            actor,
            "checklist.preview",
            "previewed",
            included=len(result.included),
            excluded=len(result.excluded),
            review_required=result.review_required,
        )
        return result

    def configuration_issues(
        self, actor: Actor, client_id: str
    ) -> list[ChecklistIssue]:
        """Static problems in a client's configuration, independent of context.

        Today this is the dependency-scope scan: a template that can apply
        where its own prerequisite cannot. Reported for correction; UC-02
        never repairs a configuration on the client's behalf.
        """
        if actor.client_id != client_id:
            raise DataIsolationError(
                "actor may not inspect another client's configuration",
                actor_client_id=actor.client_id,
                client_id=client_id,
            )
        return self.registry.get(client_id).dependency_scope_conflicts()

    def owner_issues(self, actor: Actor, client_id: str) -> list[OwnerAssignmentIssue]:
        """UC02-BR-03. Report missing or invalid task-owner assignments."""
        if actor.client_id != client_id:
            raise DataIsolationError(
                "actor may not inspect another client's configuration",
                actor_client_id=actor.client_id,
                client_id=client_id,
            )
        return self.registry.get(client_id).owner_issues()

    def calendar_status(self) -> CalendarStatus:
        """UC02-BR-06. Whether the working calendar in use is authoritative."""
        return calendar_status(self.holidays)

    def escalations(
        self, actor: Actor, onboarding_id: str, as_of: date
    ) -> list[EscalationItem]:
        """UC02-BR-10 / GR-12 escalation list.

        Covers items at risk of missing the start date and approvals stalled
        past their gate SLA. Nothing is sent — UC-06 owns follow-up delivery.
        """
        record = self._load(actor, onboarding_id)
        self._audit_read(actor, onboarding_id, "escalations")
        items = self._escalation_items(actor, record, as_of)
        if items:
            self.audit.record(
                actor,
                "escalation.detected",
                "raised",
                onboarding_id=onboarding_id,
                count=len(items),
            )
        return items

    def _escalation_items(
        self, actor: Actor, record: OnboardingRecord, as_of: date
    ) -> list[EscalationItem]:
        """Escalations without any audit side effect (shared by several callers)."""
        config = self.registry.get(record.client_id)
        holidays = self._holidays_for(record.client_id)
        items = tracker.escalations(
            record,
            as_of,
            holidays,
            owner_issues=config.owner_issues(),
            milestone_states=milestones.evaluate_all(record, config, as_of, holidays),
        )
        items.extend(self._stalled_approvals(actor, record.onboarding_id, as_of))
        return items

    def _stalled_approvals(
        self, actor: Actor, onboarding_id: str, as_of: date
    ) -> list[EscalationItem]:
        """GR-12 second clause: approvals stalled past their due date.

        The due date is the gate's ``sla_hours`` from the policy envelope.
        """
        stalled: list[EscalationItem] = []
        for request in self.inbox.pending(actor):
            if request.onboarding_id != onboarding_id:
                continue
            gate = approvals.GATES[request.gate]
            elapsed_days = (as_of - request.created_at.date()).days
            if elapsed_days * 24 <= gate.sla_hours:
                continue
            level = (
                EscalationLevel.HR_OPS_LEAD
                if elapsed_days > config_module.ESCALATION_RESPONSE_DAYS
                else EscalationLevel.HR_OFFICER
            )
            stalled.append(
                EscalationItem(
                    subject_type="approval",
                    subject_id=request.request_id,
                    level=level,
                    escalate_to=tracker._ESCALATION_ROUTE[level],
                    reason=(
                        f"approval for gate {request.gate.value} stalled "
                        f"{elapsed_days} day(s), SLA {gate.sla_hours}h"
                    ),
                    days_waiting=elapsed_days,
                )
            )
        return stalled

    def joining_handoff(
        self, actor: Actor, onboarding_id: str, as_of: date
    ) -> JoiningHandoff:
        """Planning package handed to **UC-06**.

        This is the UC-02/UC-06 boundary. UC-02 supplies the plan, outstanding
        activities, planning-time joining risk and milestone state, then stops.
        It does not monitor, chase, confirm joining, or close the case —
        ``case_closed_by_uc02`` is always False.
        """
        record = self._load(actor, onboarding_id)
        self._audit_read(actor, onboarding_id, "joining_handoff")
        config = self.registry.get(record.client_id)
        handoff = JoiningHandoff(
            onboarding_id=record.onboarding_id,
            client_id=record.client_id,
            employee_id=record.employee_id,
            joining_date=record.joining_date,
            plan_revision=record.revision,
            risk=tracker.assess_joining_risk(record, as_of, self._holidays_for(record.client_id)),
            outstanding=tracker.outstanding_activities(record, as_of, self._holidays_for(record.client_id)),
            milestones=milestones.evaluate_all(record, config, as_of, self._holidays_for(record.client_id)),
            owner_issues=config.owner_issues(),
            escalations=tracker.escalations(
                record,
                as_of,
                self._holidays_for(record.client_id),
                owner_issues=config.owner_issues(),
                milestone_states=milestones.evaluate_all(
                    record, config, as_of, self._holidays_for(record.client_id)
                ),
            ),
            calendar=calendar_status(self._holidays_for(record.client_id)),
        )
        self.audit.record(
            actor,
            "handoff.uc06",
            "prepared",
            onboarding_id=onboarding_id,
            outstanding=len(handoff.outstanding),
            owner_issues=len(handoff.owner_issues),
        )
        return handoff

    def reminder_drafts(
        self, actor: Actor, onboarding_id: str, as_of: date
    ) -> list[ReminderDraft]:
        """Prepare drafts. Nothing is sent — see :mod:`reminders`."""
        record = self._load(actor, onboarding_id)
        drafts = reminders.build_reminder_drafts(
            record, as_of, holidays=self._holidays_for(record.client_id)
        )
        self.audit.record(
            actor,
            "reminder.draft",
            "prepared_not_sent",
            onboarding_id=onboarding_id,
            draft_count=len(drafts),
        )
        return drafts
