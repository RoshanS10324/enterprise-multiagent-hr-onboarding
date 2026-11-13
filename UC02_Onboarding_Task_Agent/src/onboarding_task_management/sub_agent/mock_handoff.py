"""Deterministic MOCK adapters standing in for UC-03, UC-04, UC-05 and ops.

Read this first
---------------

**Nothing in this module talks to anything.** There is no HTTP client, no
queue, no SDK and no UC-03/UC-04/UC-05 code. Each adapter is a pure function
that returns the :class:`StatusUpdate` a real workflow *would* send, given an
outcome the caller chooses. It is a test double for an interface that does
not exist yet, and every update it produces carries a
:data:`MOCK_REFERENCE_PREFIX` reference so a stored record can never be
mistaken for the output of a live integration.

Consequently:

* an applied update proves UC-02 handled a report correctly;
* it proves **nothing** about a laptop being shipped, a document being
  validated or a visa being filed.

Determinism
-----------

``update_id`` is derived from the task id, the source and the outcome, so the
same logical report always carries the same id. That is what makes replay
testing meaningful: calling an adapter twice and applying both results
exercises the idempotency ledger rather than creating two distinct updates.
Callers that genuinely need a second, distinct report (a correction after a
rejection) pass an explicit ``attempt``.

No clock is read: ``occurred_at`` is required, because a mock that invented
"now" would make every test non-reproducible.
"""

from __future__ import annotations

from datetime import datetime

from onboarding_task_management.contracts import (
    ExternalOutcome,
    SourceUseCase,
    StatusUpdate,
    TaskRecord,
)

#: Every reference minted here starts with this. Grep-able proof that a
#: record came from a mock and not from a real external system.
MOCK_REFERENCE_PREFIX = "MOCK"


def make_update_id(
    task_id: str,
    source: SourceUseCase,
    outcome: ExternalOutcome,
    attempt: int = 1,
) -> str:
    """Derived, so a repeat of the same logical report replays rather than duplicates."""
    return f"UPD-{source.value}-{task_id}-{outcome.value}-{attempt}"


def mock_reference(task_id: str, source: SourceUseCase, attempt: int = 1) -> str:
    """A ticket/document id shaped like a real one, but obviously synthetic."""
    return f"{MOCK_REFERENCE_PREFIX}-{source.value.upper()}-{task_id}-{attempt}"


def _update(
    task: TaskRecord,
    source: SourceUseCase,
    outcome: ExternalOutcome,
    occurred_at: datetime,
    *,
    blocking_reason: str = "",
    attempt: int = 1,
    with_reference: bool = True,
) -> StatusUpdate:
    return StatusUpdate(
        update_id=make_update_id(task.task_id, source, outcome, attempt),
        source=source,
        client_id=task.client_id,
        onboarding_id=task.onboarding_id,
        task_id=task.task_id,
        outcome=outcome,
        occurred_at=occurred_at,
        reference=mock_reference(task.task_id, source, attempt)
        if with_reference
        else None,
        blocking_reason=blocking_reason,
    )


def uc03_document_result(
    task: TaskRecord,
    outcome: ExternalOutcome,
    occurred_at: datetime,
    *,
    blocking_reason: str = "",
    attempt: int = 1,
) -> StatusUpdate:
    """A document-validation verdict, as UC-03 would report it.

    ``outcome`` is supplied by the caller and used verbatim. This function
    does not inspect a document and has no opinion about one: in particular it
    never decides that something is fraudulent. A ``SUSPECTED_FRAUD`` scenario
    exists only because a test explicitly asked for that outcome.
    """
    return _update(
        task,
        SourceUseCase.UC03_DOCUMENT_VALIDATION,
        outcome,
        occurred_at,
        blocking_reason=blocking_reason,
        attempt=attempt,
    )


def uc04_equipment_result(
    task: TaskRecord,
    outcome: ExternalOutcome,
    occurred_at: datetime,
    *,
    blocking_reason: str = "",
    attempt: int = 1,
) -> StatusUpdate:
    """An equipment / access provisioning status, as UC-04 would report it.

    No provisioning logic lives here or anywhere in UC-02: which laptop model,
    which access group and which approval chain are UC-04's business entirely.
    """
    return _update(
        task,
        SourceUseCase.UC04_EQUIPMENT_ACCESS,
        outcome,
        occurred_at,
        blocking_reason=blocking_reason,
        attempt=attempt,
    )


def uc05_orientation_result(
    task: TaskRecord,
    outcome: ExternalOutcome,
    occurred_at: datetime,
    *,
    blocking_reason: str = "",
    attempt: int = 1,
) -> StatusUpdate:
    """An orientation / policy-acknowledgement status, as UC-05 would report it."""
    return _update(
        task,
        SourceUseCase.UC05_ORIENTATION,
        outcome,
        occurred_at,
        blocking_reason=blocking_reason,
        attempt=attempt,
    )


def operations_result(
    task: TaskRecord,
    outcome: ExternalOutcome,
    occurred_at: datetime,
    *,
    blocking_reason: str = "",
    attempt: int = 1,
) -> StatusUpdate:
    """A status from the operational team running visa / medical / travel work.

    These activities have no dedicated use case in this repository. HR assigns
    the responsible individual through :func:`tracker.assign_owner`; the team
    does the work; this adapter stands in for the status they would return.
    """
    return _update(
        task,
        SourceUseCase.OPERATIONS,
        outcome,
        occurred_at,
        blocking_reason=blocking_reason,
        attempt=attempt,
    )


#: Which mock adapter stands in for which workflow.
MOCK_ADAPTERS = {
    SourceUseCase.UC03_DOCUMENT_VALIDATION: uc03_document_result,
    SourceUseCase.UC04_EQUIPMENT_ACCESS: uc04_equipment_result,
    SourceUseCase.UC05_ORIENTATION: uc05_orientation_result,
    SourceUseCase.OPERATIONS: operations_result,
}


__all__ = [
    "MOCK_ADAPTERS",
    "MOCK_REFERENCE_PREFIX",
    "make_update_id",
    "mock_reference",
    "operations_result",
    "uc03_document_result",
    "uc04_equipment_result",
    "uc05_orientation_result",
]
