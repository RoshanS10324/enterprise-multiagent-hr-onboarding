"""Outlook connector port.

UC-02 prepares reminder **drafts** and never sends them: ``reminders.py`` has
no transport, and every draft carries ``delivery_status="draft_not_sent"``.
Delivery belongs to UC-06 behind the ``joining_confirmation`` gate.

This port is therefore deliberately narrow. It models *queuing a prepared
draft for delivery*, not composing one — composition stays in UC-02 — and it
keeps the gate in the contract so no adapter can quietly send something that
was never approved.

Nothing in this package talks to Microsoft Graph, Exchange, SMTP or any mail
server. The only implementation shipped is
:class:`~onboarding_task_management.sub_agent.connectors.outlook.mock.MockOutlookConnector`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class DeliveryStatus(StrEnum):
    """Outcome of one delivery attempt. ``SENT`` never means a real email."""

    #: Accepted into the outbox. For the mock this is simulation only.
    SIMULATED_SENT = "simulated_sent"
    #: Refused before any attempt: the approval gate was not satisfied.
    BLOCKED_NO_APPROVAL = "blocked_no_approval"
    #: The adapter tried and failed. Retryable.
    FAILED = "failed"
    #: An identical message was already accepted; nothing new was queued.
    DUPLICATE_IGNORED = "duplicate_ignored"
    #: A draft now exists in the mailbox. Nothing has been sent.
    DRAFT_CREATED = "draft_created"
    #: A REAL connector handed the message to the mail service, which accepted it for sending. This is NOT a
    #: delivery: a mail service accepting a message says nothing about it reaching an inbox.
    SUBMITTED = "submitted_for_sending"


class OutlookConnectorError(RuntimeError):
    """The connector could not complete the operation."""


@dataclass(frozen=True)
class OutlookMessage:
    """A prepared message handed to the connector.

    Carries **no personal data**: recipients are opaque role or actor
    identifiers, matching UC-02's contracts, which have no name, email or
    contact field anywhere.

    ``idempotency_key`` is what makes a retry safe. It is required, not
    optional, because a reminder sent twice to an HR inbox is a real-world
    failure, and an adapter cannot invent a stable key after the fact.
    """

    idempotency_key: str
    onboarding_id: str
    client_id: str
    recipient_role: str
    subject_en: str
    body_en: str = ""
    subject_ar: str = ""
    body_ar: str = ""
    #: Tasks this message is about. Identifiers only.
    task_ids: tuple[str, ...] = ()
    #: Approval that authorises delivery. ``None`` means not approved.
    approval_id: str | None = None
    #: Team the message is for, where the client's mapping is by team. Empty for role-level messages (HR review).
    recipient_team: str = ""
    #: True when another team in the same plan has the same owner role, so a role-level address would be ambiguous.
    role_shared: bool = False


@dataclass(frozen=True)
class DeliveryReceipt:
    """What actually happened. Returned for every attempt, success or not."""

    idempotency_key: str
    status: DeliveryStatus
    message_id: str | None = None
    detail: str = ""
    attempted_at: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )

    @property
    def accepted(self) -> bool:
        """True only when the outbox holds the message because of this call."""
        return self.status in (DeliveryStatus.SIMULATED_SENT, DeliveryStatus.SUBMITTED)


@runtime_checkable
class OutlookConnector(Protocol):
    """The seam a production Graph adapter would implement.

    A production adapter must:

    * refuse to send without a valid ``approval_id`` — the gate is the
      contract's, not the caller's to skip;
    * treat ``idempotency_key`` as the deduplication key, so a retried
      delivery cannot produce a second email;
    * raise :class:`OutlookConnectorError` rather than a transport-specific
      exception, so callers need no knowledge of Graph;
    * never log or transmit message bodies containing personal data.
    """

    def send(self, message: OutlookMessage) -> DeliveryReceipt: ...

    def outbox(self, client_id: str) -> list[OutlookMessage]: ...


@runtime_checkable
class OutlookDraftConnector(Protocol):
    """Optional capability: save a message as a DRAFT in the authorised mailbox without sending it.

    Kept separate from :class:`OutlookConnector` so that interface is unchanged and existing implementations stay
    valid. ``create_draft`` must be idempotent on ``message.idempotency_key``.
    """

    def create_draft(self, message: OutlookMessage) -> DeliveryReceipt: ...


__all__ = [
    "OutlookDraftConnector",
    "DeliveryReceipt",
    "DeliveryStatus",
    "OutlookConnector",
    "OutlookConnectorError",
    "OutlookMessage",
]
