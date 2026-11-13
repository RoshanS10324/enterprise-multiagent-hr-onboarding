"""Inbound cross-use-case status updates: the port UC-02 listens on.

What this module is, and is not
-------------------------------

UC-02 owns the consolidated checklist. It does **not** validate documents,
provision laptops, grant access, run an orientation, apply for a visa, book a
flight or issue a bank letter. Those belong to UC-03, UC-04, UC-05 and the
client's operational teams. What UC-02 must do is *record what they report*
and recompute the plan around it.

This module is that recording step, and nothing more. It contains no
document-validation logic, no provisioning logic and no access-control logic
-- it maps a reported :class:`ExternalOutcome` onto the UC-02 task lifecycle
that already exists in :mod:`tracker` and :mod:`planner`.

**There is no approved cross-use-case contract in this repository and no
transport of any kind.** Updates arrive as :class:`StatusUpdate` values that
a caller constructs -- in this phase, from the deterministic mock adapters in
:mod:`mock_handoff` or from a fixture under ``data/mock/``. Nothing here
opens a socket, and an applied update is evidence that UC-02 was *told*
something, never that a real external team did the work.

Why rejections are returned, not raised
---------------------------------------

An inbound port receives whatever the caller sends, including nonsense. A
rejected update is a normal outcome that the caller must be able to inspect
and log, so business rejections come back as a
:class:`StatusUpdateResult` with ``accepted=False`` and a stable
``rejection_code``. Only a *security* violation -- an actor reaching across
clients -- raises, because that must never be a value a caller can swallow.

The five invariants
-------------------

1. **A missing response is not success.** There is no code path that completes
   a task without an explicit resolving outcome. Silence leaves the task open.
2. **An update may only touch the task it names,** and only if the reporting
   workflow is actually responsible for that kind of work
   (:data:`SOURCE_CATEGORIES`).
3. **Replays are applied once.** The ledger on the record makes a repeat a
   no-op; the same id aimed at a different task is a conflict, not a second
   update.
4. **UC-02 never escalates an outcome.** ``INVALID`` stays ``INVALID``;
   only an explicit ``SUSPECTED_FRAUD`` report means fraud is suspected.
5. **Unresolved means still blocking.** An unresolved outcome leaves the task
   open, so everything downstream of it stays blocked by the ordinary
   dependency rules -- no separate "blocked" bookkeeping to drift.
"""

from __future__ import annotations

from collections.abc import Mapping

from onboarding_task_management.contracts import (
    OPEN_STATUSES,
    RESOLVING_OUTCOMES,
    UNRESOLVED_OUTCOMES,
    Actor,
    ExternalOutcome,
    OnboardingRecord,
    OnboardingStatus,
    SourceUseCase,
    StatusUpdate,
    StatusUpdateResult,
    TaskCategory,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from onboarding_task_management.errors import DataIsolationError
from onboarding_task_management.privacy import redact_free_text
from onboarding_task_management.sub_agent.planner import (
    recompute_readiness,
    unmet_prerequisites,
)
from onboarding_task_management.sub_agent.tracker import assert_same_client

# ---------------------------------------------------------------------------
# Rejection codes. Stable strings, so callers and tests branch on a value
# rather than on prose that may be reworded.
# ---------------------------------------------------------------------------

REJECT_UNKNOWN_ONBOARDING = "unknown_onboarding"
REJECT_UNKNOWN_TASK = "unknown_task"
REJECT_NOT_ACTIVE = "onboarding_not_active"
REJECT_TASK_SETTLED = "task_settled"
REJECT_SOURCE_NOT_RESPONSIBLE = "source_not_responsible"
REJECT_FRAUD_NOT_REPORTABLE = "fraud_not_reportable_by_source"
REJECT_MISSING_REASON = "missing_blocking_reason"
REJECT_MISSING_EVIDENCE = "missing_evidence_reference"
REJECT_PREREQUISITES_UNMET = "prerequisites_unmet"
REJECT_DUPLICATE_CONFLICT = "duplicate_update_id_different_task"
REJECT_SOURCE_UNAUTHENTICATED = "source_not_authenticated"
REJECT_CASE_MISMATCH = "case_id_mismatch"
REJECT_PLAN_NOT_APPROVED = "plan_not_approved"

#: Outcomes of :func:`verify_source`. Only ``VERIFIED`` lets an update through.
VERIFIED = "verified"
UNVERIFIED_MISSING_IDENTITY = "rejected_missing_identity"
UNVERIFIED_NO_PRINCIPALS = "rejected_no_principals_configured"
UNVERIFIED_UNREGISTERED_SOURCE = "rejected_unregistered_source"
UNVERIFIED_CALLER_NOT_REGISTERED = "rejected_caller_not_registered_for_source"


# ---------------------------------------------------------------------------
# Which workflow may report on which kind of task.
#
# This is an authorisation table, not a routing table: it stops UC-04 from
# closing a document-validation task and UC-03 from closing a laptop task.
# Categories UC-02 owns outright (joining confirmation) accept no external
# report at all and so appear nowhere below.
# ---------------------------------------------------------------------------

SOURCE_CATEGORIES: dict[SourceUseCase, frozenset[TaskCategory]] = {
    SourceUseCase.UC03_DOCUMENT_VALIDATION: frozenset(
        {
            TaskCategory.DOCUMENT_COLLECTION,
            TaskCategory.BACKGROUND_VERIFICATION,
        }
    ),
    SourceUseCase.UC04_EQUIPMENT_ACCESS: frozenset(
        {
            TaskCategory.IT_PROVISIONING,
            TaskCategory.ACCESS_MANAGEMENT,
            TaskCategory.WORKPLACE,
        }
    ),
    SourceUseCase.UC05_ORIENTATION: frozenset(
        {
            TaskCategory.ORIENTATION,
            TaskCategory.POLICY_ACKNOWLEDGEMENT,
        }
    ),
    # The client's operational teams. HR still assigns the individual; this
    # only says which workflow is entitled to report the outcome.
    SourceUseCase.OPERATIONS: frozenset(
        {
            TaskCategory.VISA,
            TaskCategory.MEDICAL,
            TaskCategory.BIOMETRICS,
            TaskCategory.INSURANCE,
            TaskCategory.TRAVEL,
            TaskCategory.ACCOMMODATION,
            TaskCategory.FOOD,
            TaskCategory.BANKING_LETTER,
            TaskCategory.PAYROLL,
            TaskCategory.HR_RECORD,
            TaskCategory.HR_SYSTEM_ACTIVATION,
        }
    ),
}

#: Unresolved outcomes that count as a FAILED attempt for the per-task retry counter.
#: ``DELAYED`` is deliberately absent: late is not failed.
FAILED_OUTCOMES = frozenset(
    {
        ExternalOutcome.INVALID,
        ExternalOutcome.REQUIRES_REVIEW,
        ExternalOutcome.SUSPECTED_FRAUD,
    }
)

#: Only the validating workflow can allege fraud. An equipment or orientation
#: workflow has no basis on which to make that claim about a document.
FRAUD_REPORTING_SOURCES = frozenset({SourceUseCase.UC03_DOCUMENT_VALIDATION})


def verify_source(
    actor: Actor,
    update: StatusUpdate,
    source_principals: "Mapping[SourceUseCase, frozenset[str]] | None",
) -> str:
    """Check the caller against the principals registered for the claimed source.

    ``update.source`` is only a claim. It counts when the *authenticated*
    ``actor`` is one of the principals registered for that source. There is no
    fallback: with no principals configured nothing is verified, so a
    deployment that has not set up service identities accepts no inbound
    status updates rather than trusting whoever calls.
    """
    if not (actor.actor_id or "").strip():
        return UNVERIFIED_MISSING_IDENTITY
    if source_principals is None:
        return UNVERIFIED_NO_PRINCIPALS
    registered = source_principals.get(update.source)
    if not registered:
        return UNVERIFIED_UNREGISTERED_SOURCE
    if actor.actor_id not in registered:
        return UNVERIFIED_CALLER_NOT_REGISTERED
    return VERIFIED


def _reject(
    update: StatusUpdate, code: str, reason: str, **extra: object
) -> StatusUpdateResult:
    return StatusUpdateResult(
        update_id=update.update_id,
        task_id=update.task_id,
        accepted=False,
        rejection_code=code,
        rejection_reason=reason,
        **extra,  # type: ignore[arg-type]
    )


def _ready_ids(record: OnboardingRecord) -> set[str]:
    return {t.task_id for t in record.tasks if t.status is TaskStatus.READY}


def apply_status_update(
    actor: Actor,
    record: OnboardingRecord,
    update: StatusUpdate,
    *,
    source_principals: "Mapping[SourceUseCase, frozenset[str]] | None" = None,
    plan_released: bool = True,
) -> StatusUpdateResult:
    """Record one outcome reported by another workflow.

    Returns a :class:`StatusUpdateResult` describing exactly what happened,
    including the ids of any tasks that became READY because this update
    resolved a prerequisite. Raises only :class:`DataIsolationError`, and only
    when ``actor`` or ``update`` belongs to a different client than ``record``.
    """
    assert_same_client(actor, record)

    # The update itself carries a client id; it must agree with the record it
    # claims to describe. Without this an update minted for client B could be
    # applied to client A's record by an actor who legitimately holds A.
    if update.client_id != record.client_id:
        raise DataIsolationError(
            "status update belongs to another client",
            update_client_id=update.client_id,
            record_client_id=record.client_id,
            onboarding_id=record.onboarding_id,
        )

    # ``update.source`` is a claim made by the caller; it is honoured only
    # when the authenticated actor is registered for it (see verify_source).
    verification = verify_source(actor, update, source_principals)
    if verification != VERIFIED:
        return _reject(
            update,
            REJECT_SOURCE_UNAUTHENTICATED,
            f"status update source could not be verified ({verification})",
        )

    if not plan_released:
        # Checked after the caller is verified, so an unverified caller learns nothing
        # about the plan's state.
        return _reject(
            update,
            REJECT_PLAN_NOT_APPROVED,
            "the onboarding plan has not been approved by HR; teams may not report yet",
        )

    if update.onboarding_id != record.onboarding_id:
        return _reject(
            update,
            REJECT_UNKNOWN_ONBOARDING,
            "update names an onboarding that is not this record",
        )

    if (
        update.case_id
        and record.case_id
        and update.case_id != record.case_id
    ):
        return _reject(
            update,
            REJECT_CASE_MISMATCH,
            "update names a different shared case than this onboarding",
        )

    # -- Idempotency, before any effect ----------------------------------
    previous_task = record.applied_updates.get(update.update_id)
    if previous_task is not None:
        if previous_task != update.task_id:
            # Same id, different target. Applying it would move a task the
            # first delivery never touched.
            return _reject(
                update,
                REJECT_DUPLICATE_CONFLICT,
                f"update_id was already applied to task {previous_task}",
            )
        task = record.task_by_id(update.task_id)
        return StatusUpdateResult(
            update_id=update.update_id,
            task_id=update.task_id,
            accepted=True,
            applied_status=task.status if task else None,
            duplicate_replay=True,
        )

    task = record.task_by_id(update.task_id)
    if task is None:
        return _reject(
            update, REJECT_UNKNOWN_TASK, "no such task on this onboarding"
        )

    if record.status is not OnboardingStatus.ACTIVE:
        return _reject(
            update,
            REJECT_NOT_ACTIVE,
            f"onboarding is {record.status.value}; it accepts no updates",
        )

    if task.status not in OPEN_STATUSES:
        # Completed and cancelled work is settled. A late report does not
        # reopen it, and must not overwrite the completion record.
        return _reject(
            update,
            REJECT_TASK_SETTLED,
            f"task is {task.status.value} and cannot be updated",
        )

    # -- Responsibility --------------------------------------------------
    permitted = SOURCE_CATEGORIES.get(update.source, frozenset())
    if task.category not in permitted:
        return _reject(
            update,
            REJECT_SOURCE_NOT_RESPONSIBLE,
            f"{update.source.value} does not perform "
            f"{task.category.value} work",
        )

    if (
        update.outcome is ExternalOutcome.SUSPECTED_FRAUD
        and update.source not in FRAUD_REPORTING_SOURCES
    ):
        return _reject(
            update,
            REJECT_FRAUD_NOT_REPORTABLE,
            f"{update.source.value} may not report suspected fraud",
        )

    # -- Payload completeness --------------------------------------------
    if update.outcome in UNRESOLVED_OUTCOMES and not update.blocking_reason.strip():
        return _reject(
            update,
            REJECT_MISSING_REASON,
            "an unresolved outcome must say why the task cannot resolve",
        )

    if update.outcome in RESOLVING_OUTCOMES:
        # The same evidence rule as a human completion (UC02-BR-12). An
        # external workflow does not get to skip it.
        if task.evidence_required and not (update.reference or "").strip():
            return _reject(
                update,
                REJECT_MISSING_EVIDENCE,
                "task requires a completion reference",
            )
        unmet = unmet_prerequisites(record, task)
        if unmet:
            # A report cannot complete a task out of order; the dependency
            # graph is UC-02's, not the reporting workflow's.
            return _reject(
                update,
                REJECT_PREREQUISITES_UNMET,
                "unmet prerequisites: " + ",".join(sorted(unmet)),
            )

    before_ready = _ready_ids(record)
    result = _apply(record, task, update)
    record.applied_updates[update.update_id] = update.task_id
    record.updated_at = utc_now()

    if result.applied_status is TaskStatus.COMPLETE:
        result.unblocked_task_ids = sorted(_ready_ids(record) - before_ready)
    return result


def _apply(
    record: OnboardingRecord, task: TaskRecord, update: StatusUpdate
) -> StatusUpdateResult:
    """Write the outcome onto ``task``. Called only after every check passed."""
    task.external_outcome = update.outcome
    task.external_source = update.source
    task.external_updated_at = update.occurred_at
    if update.reference and update.reference.strip():
        task.external_reference = update.reference.strip()

    if update.outcome in RESOLVING_OUTCOMES:
        task.external_blocking_reason = ""  # the obstacle is gone
        task.delay_reason, task.delay_kind = "", ""
        task.status = TaskStatus.COMPLETE
        task.completed_at = utc_now()
        if update.reference and update.reference.strip():
            task.evidence_ref = update.reference.strip()
            task.evidence_recorded_at = utc_now()
        record.tasks = recompute_readiness(record.tasks)
        return StatusUpdateResult(
            update_id=update.update_id,
            task_id=task.task_id,
            accepted=True,
            applied_status=TaskStatus.COMPLETE,
        )

    if update.outcome is ExternalOutcome.IN_PROGRESS:
        # Work has started. Only a task whose prerequisites are met may show
        # as in progress, so a BLOCKED task records the report and stays put.
        task.external_blocking_reason = ""
        if task.status is TaskStatus.READY:
            task.status = TaskStatus.IN_PROGRESS
            task.started_at = task.started_at or utc_now()
        return StatusUpdateResult(
            update_id=update.update_id,
            task_id=task.task_id,
            accepted=True,
            applied_status=task.status,
        )

    # Unresolved: INVALID, REQUIRES_REVIEW, SUSPECTED_FRAUD, DELAYED.
    #
    # The task keeps its current open status. It is deliberately NOT forced
    # back to BLOCKED: readiness means "prerequisites are met", which is still
    # true. What has changed is that the work itself did not succeed, which is
    # carried by ``external_outcome`` and surfaced as a follow-up flag.
    task.external_blocking_reason = redact_free_text(update.blocking_reason)
    if update.outcome in FAILED_OUTCOMES:
        # One more failed attempt. Replays never reach here (the ledger returns first), and a
        # known delay is not a failed attempt.
        task.failed_report_count += 1
    return StatusUpdateResult(
        update_id=update.update_id,
        task_id=task.task_id,
        accepted=True,
        applied_status=task.status,
        follow_up_raised=True,
    )


__all__ = [
    "FAILED_OUTCOMES",
    "FRAUD_REPORTING_SOURCES",
    "REJECT_DUPLICATE_CONFLICT",
    "REJECT_FRAUD_NOT_REPORTABLE",
    "REJECT_MISSING_EVIDENCE",
    "REJECT_MISSING_REASON",
    "REJECT_NOT_ACTIVE",
    "REJECT_PREREQUISITES_UNMET",
    "REJECT_SOURCE_UNAUTHENTICATED",
    "REJECT_CASE_MISMATCH",
    "REJECT_PLAN_NOT_APPROVED",
    "VERIFIED",
    "verify_source",
    "REJECT_SOURCE_NOT_RESPONSIBLE",
    "REJECT_TASK_SETTLED",
    "REJECT_UNKNOWN_ONBOARDING",
    "REJECT_UNKNOWN_TASK",
    "SOURCE_CATEGORIES",
    "apply_status_update",
]
