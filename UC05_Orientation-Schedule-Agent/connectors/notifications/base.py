"""NotificationPort: delivery of a message to a person (or a role's shared queue) over a channel.

Used for three things: telling an approver that a HITL item is waiting for them (the "Teams or
Outlook approval channel"), telling a buddy and a new hire about an approved buddy assignment, and
delivering an HR-approved welcome email. The decision itself never travels through this port --
approvals are always recorded in the HITLInbox via POST /v1/approvals/{id}/decision.

No real provider adapter exists. A Microsoft Teams adapter would implement connectors/teams
(TeamsPort, this Protocol for the Teams channel) and an Outlook/SMTP adapter this same Protocol
for email. Until one is approved, only simulations exist (receipts always stamped simulated=True):
MockNotifier (connectors/notifications/mock.py, every channel; what the workflow is wired with),
MockEmailSender (connectors/email/mock.py) and MockTeamsAdapter (connectors/teams/mock.py).

Idempotency: every message carries `idempotency_key`, stable across retries of the same logical
message. An adapter whose provider deduplicates on it (or that can reconcile by querying what was
sent) sets `idempotent_delivery = True`; resending after an uncertain outcome is then safe. An
adapter that cannot must set False, and callers then never resend after an uncertain outcome: the
welcome flow routes it to HR instead (sub_agent/tool_registry.py, send_welcome_email).

Two failure types, never interchangeable:
  - NotificationDeliveryError: the provider failed (outage, timeout, throttling). Retryable --
    the workflow re-raises it so the run can be resumed or the HITL apply retried.
  - NotificationRejectedError: the provider permanently refused this message (unknown recipient,
    channel not configured, content rejected). Not retryable; the workflow routes it to a
    failure branch for HR to resolve.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class NotificationChannel(StrEnum):
    EMAIL = "email"
    TEAMS = "teams"


class NotificationMessage(BaseModel):
    channel: NotificationChannel
    client_id: str
    employee_id: str  # whose onboarding this message concerns
    recipient_id: str  # a person's id, or "role:<role>" for a role's shared approval queue
    recipient_address: str | None = None  # resolved address when the caller has one (e.g. email)
    kind: str  # e.g. "approval_request", "buddy_assignment", "welcome"
    subject: str
    body: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None  # same value on every retry of this logical message


class DeliveryReceipt(BaseModel):
    delivery_ref: str
    channel: NotificationChannel
    recipient_id: str
    accepted_at: datetime
    simulated: bool  # True for every receipt from a fake; a real adapter must set False


class NotificationDeliveryError(RuntimeError):
    """The provider failed to accept the message (outage, timeout, throttling). Retryable."""


class NotificationRejectedError(ValueError):
    """The provider permanently refused the message, or no channel is configured. Not retryable."""


class NotificationPort(Protocol):
    idempotent_delivery: bool  # True only if a resend with the same idempotency_key cannot deliver twice

    def send(self, message: NotificationMessage) -> DeliveryReceipt: ...
