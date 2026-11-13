"""TEST / DEMO ONLY. MockNotifier: an in-memory, simulated NotificationPort.

Nothing is delivered to anyone: there is no SMTP server, no Microsoft Graph or Teams call and no
credential. Every receipt says simulated=True and its delivery_ref starts with "sim-", so a
simulated delivery is never mistaken for a real one. Production startup refuses fake connectors
(sub_agent.runtime).

MockNotifier accepts every channel. It is what the onboarding workflow is wired with today (there
is one NotificationPort in OnboardingDependencies), recording approval requests to HR and team
leads, buddy notices, welcome messages and "no decision needed" notices alike. Channel-specific
adapters extend it without duplicating its behaviour: connectors.email.mock.MockEmailSender
(email only) and connectors.teams.mock.MockTeamsAdapter (Teams only).

Outcomes it can simulate, deterministically:
  - success: the message is recorded in `sent` and a simulated DeliveryReceipt is returned.
  - outage: `fail_next=n` -> the next n sends raise NotificationDeliveryError before anything is
    delivered (retryable).
  - lost response: `lose_next_responses=n` -> the next n sends are delivered and THEN raise
    NotificationDeliveryError, like a provider timeout after it accepted the message.
  - permanent refusal: recipients in `reject_recipients` raise NotificationRejectedError, as does a
    message without a recipient or tenant.
  - idempotency: a resend with an idempotency_key the mock already delivered returns the original
    receipt and delivers nothing (it can establish that the first request was accepted, because it
    recorded it). Keys are scoped per tenant. `idempotent_delivery=False` simulates a provider that
    cannot deduplicate: a resend delivers again.
This is at most one delivery per key only because the mock remembers every key; it says nothing
about what a real provider guarantees.
"""

from __future__ import annotations

from typing import Any

from connectors.notifications.base import (DeliveryReceipt, NotificationChannel, NotificationDeliveryError,
                                           NotificationMessage, NotificationRejectedError)
from hr_onboarding_common.contracts import new_id, now_utc


class MockNotifier:
    REF_PREFIX = "sim-dlv"  # every simulated delivery_ref starts with this

    def __init__(self, *, fail_next: int = 0, reject_recipients: set[str] | None = None,
                 lose_next_responses: int = 0, idempotent_delivery: bool = True):
        self._initial: dict[str, Any] = dict(fail_next=fail_next, reject_recipients=set(reject_recipients or set()),
                                             lose_next_responses=lose_next_responses,
                                             idempotent_delivery=idempotent_delivery)
        self.reset()

    def reset(self) -> None:
        """Back to the state the mock was constructed with: nothing sent, no remembered keys, the
        constructor's failure settings."""
        init = self._initial
        self.idempotent_delivery: bool = init["idempotent_delivery"]  # False: a resend with the same key delivers again
        self._delivered: dict[tuple[str, str], DeliveryReceipt] = {}  # (client_id, idempotency_key) -> receipt
        self.sent: list[NotificationMessage] = []
        self.fail_next: int = init["fail_next"]
        self.lose_next_responses: int = init["lose_next_responses"]
        self.reject_recipients: set[str] = set(init["reject_recipients"])

    # ---- NotificationPort
    def send(self, message: NotificationMessage) -> DeliveryReceipt:
        self._validate(message)
        key = (message.client_id, message.idempotency_key) if message.idempotency_key else None
        if self.idempotent_delivery and key and key in self._delivered:
            return self._delivered[key].model_copy()  # already delivered under this key: never resent
        if self.fail_next > 0:
            self.fail_next -= 1
            raise NotificationDeliveryError(f"{message.channel} provider unavailable (simulated outage)")
        if message.recipient_id in self.reject_recipients:
            raise NotificationRejectedError(f"{message.channel} provider rejected recipient {message.recipient_id} "
                                            f"(simulated)")
        self.sent.append(message.model_copy(deep=True))
        receipt = DeliveryReceipt(delivery_ref=new_id(self.REF_PREFIX), channel=message.channel,
                                  recipient_id=message.recipient_id, accepted_at=now_utc(), simulated=True)
        if key:
            self._delivered[key] = receipt
        if self.lose_next_responses > 0:
            self.lose_next_responses -= 1
            raise NotificationDeliveryError(f"{message.channel} provider timed out (simulated; the message was "
                                            f"delivered)")
        return receipt.model_copy()

    # ---- inspection (tests and the offline demo; not part of NotificationPort)
    def sent_to(self, recipient_id: str, *, kind: str | None = None,
                client_id: str | None = None) -> list[NotificationMessage]:
        return self.messages(kind=kind, client_id=client_id, recipient_id=recipient_id)

    def messages(self, *, kind: str | None = None, client_id: str | None = None, recipient_id: str | None = None,
                 channel: NotificationChannel | None = None) -> list[NotificationMessage]:
        """Delivered messages, in order, filtered by any of kind, tenant, recipient and channel."""
        return [m.model_copy(deep=True) for m in self.sent
                if (kind is None or m.kind == kind) and (client_id is None or m.client_id == client_id)
                and (recipient_id is None or m.recipient_id == recipient_id)
                and (channel is None or m.channel == channel)]

    # ---- provider-side checks; channel adapters extend this
    def _validate(self, message: NotificationMessage) -> None:
        """What the provider refuses outright, before any delivery attempt (permanent)."""
        if not message.client_id.strip():
            raise NotificationRejectedError("a message must name its tenant (client_id)")
        if not message.recipient_id.strip():
            raise NotificationRejectedError("a message must name its recipient")
