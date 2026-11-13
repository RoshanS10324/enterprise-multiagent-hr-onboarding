"""MOCK Outlook connector. Simulation only — no email is ever sent.

There is no ``httpx``, ``requests``, ``smtplib``, ``msal`` or
``msgraph`` import in this module, and no URL, tenant id or credential. A
test asserts that, so the claim cannot rot.

Behaviour that matters:

**The approval gate is enforced here, not assumed.** A message without an
``approval_id`` is refused with ``BLOCKED_NO_APPROVAL`` and never reaches the
outbox. That mirrors UC-02's own rule that joining instructions are not sent
without HR Ops confirmation.

**Retries are safe.** Delivery is keyed on ``idempotency_key``; a repeated
send returns ``DUPLICATE_IGNORED`` and the outbox does not grow. This is the
property a real Graph adapter must also provide, so exercising it against the
mock is worth something.

**Failures are injectable and deterministic.** ``fail_keys`` makes a specific
message fail every time; ``fail_times`` makes the first *n* attempts fail and
then succeed, which is how a retry test gets to prove it does not duplicate.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field

from onboarding_task_management.sub_agent.connectors.outlook.base import (
    DeliveryReceipt,
    DeliveryStatus,
    OutlookMessage,
)

#: Prefix on every simulated id, so a stored value can never be mistaken for
#: a real Graph message id.
MOCK_ID_PREFIX = "MOCK-OUTLOOK"


@dataclass
class MockOutlookConnector:
    """Deterministic in-memory Outlook stand-in.

    ``require_approval`` is True by default and should stay that way outside
    tests that are specifically about the unapproved path.
    """

    require_approval: bool = True
    #: Keys that always fail, simulating a permanently rejected recipient.
    fail_keys: set[str] = field(default_factory=set)
    #: Key -> number of initial attempts that should fail before succeeding.
    fail_times: dict[str, int] = field(default_factory=dict)

    _drafts: dict[str, OutlookMessage] = field(default_factory=dict, init=False)
    _outbox: dict[str, OutlookMessage] = field(default_factory=dict, init=False)
    _receipts: list[DeliveryReceipt] = field(default_factory=list, init=False)
    _attempts: collections.Counter = field(
        default_factory=collections.Counter, init=False
    )
    _sequence: int = field(default=0, init=False)

    # -- port ---------------------------------------------------------------

    def send(self, message: OutlookMessage) -> DeliveryReceipt:
        """Simulate queuing ``message``. Returns what really happened."""
        self._attempts[message.idempotency_key] += 1
        attempt = self._attempts[message.idempotency_key]

        # Deduplicate first: a retry of something already accepted must not
        # re-enter the outbox, whatever the failure injection says.
        if message.idempotency_key in self._outbox:
            return self._record(
                DeliveryReceipt(
                    idempotency_key=message.idempotency_key,
                    status=DeliveryStatus.DUPLICATE_IGNORED,
                    message_id=self._message_id(message.idempotency_key),
                    detail="an identical message was already accepted",
                )
            )

        if self.require_approval and not message.approval_id:
            return self._record(
                DeliveryReceipt(
                    idempotency_key=message.idempotency_key,
                    status=DeliveryStatus.BLOCKED_NO_APPROVAL,
                    detail=(
                        "delivery requires an approved gate decision; "
                        "no approval_id was supplied"
                    ),
                )
            )

        if message.idempotency_key in self.fail_keys:
            return self._record(
                DeliveryReceipt(
                    idempotency_key=message.idempotency_key,
                    status=DeliveryStatus.FAILED,
                    detail="simulated permanent delivery failure",
                )
            )

        allowed_failures = self.fail_times.get(message.idempotency_key, 0)
        if attempt <= allowed_failures:
            return self._record(
                DeliveryReceipt(
                    idempotency_key=message.idempotency_key,
                    status=DeliveryStatus.FAILED,
                    detail=f"simulated transient failure (attempt {attempt})",
                )
            )

        self._sequence += 1
        self._outbox[message.idempotency_key] = message
        return self._record(
            DeliveryReceipt(
                idempotency_key=message.idempotency_key,
                status=DeliveryStatus.SIMULATED_SENT,
                message_id=self._message_id(message.idempotency_key),
                detail="simulated only — no email was sent",
            )
        )

    def create_draft(self, message: OutlookMessage) -> DeliveryReceipt:
        """Simulate saving a draft. Idempotent on the key; needs no approval (a draft is not a send)."""
        if message.idempotency_key in self._drafts:
            return self._record(DeliveryReceipt(
                idempotency_key=message.idempotency_key, status=DeliveryStatus.DUPLICATE_IGNORED,
                message_id=self._message_id(message.idempotency_key), detail="a draft with this key already exists"))
        self._drafts[message.idempotency_key] = message
        return self._record(DeliveryReceipt(
            idempotency_key=message.idempotency_key, status=DeliveryStatus.DRAFT_CREATED,
            message_id=self._message_id(message.idempotency_key), detail="simulated draft; nothing was sent"))

    def drafts(self, client_id: str) -> list[OutlookMessage]:
        return [m for _, m in sorted(self._drafts.items()) if m.client_id == client_id]

    def outbox(self, client_id: str) -> list[OutlookMessage]:
        """Accepted messages for one client. Scoped, like everything in UC-02."""
        return [
            m
            for key, m in sorted(self._outbox.items())
            if m.client_id == client_id
        ]

    # -- inspection helpers --------------------------------------------------

    def _record(self, receipt: DeliveryReceipt) -> DeliveryReceipt:
        self._receipts.append(receipt)
        return receipt

    def _message_id(self, key: str) -> str:
        return f"{MOCK_ID_PREFIX}-{key}"

    @property
    def receipts(self) -> list[DeliveryReceipt]:
        return list(self._receipts)

    def attempts_for(self, idempotency_key: str) -> int:
        return self._attempts[idempotency_key]

    def sent_count(self) -> int:
        """Messages actually in the outbox, across all clients."""
        return len(self._outbox)

    def reset(self) -> None:
        self._outbox.clear()
        self._drafts.clear()
        self._receipts.clear()
        self._attempts.clear()
        self._sequence = 0


def message_from_draft(draft: object, approval_id: str | None = None) -> OutlookMessage:
    """Adapt a UC-02 :class:`ReminderDraft` to an :class:`OutlookMessage`.

    The draft's own ``draft_id`` is the idempotency key. It is derived from
    the onboarding and the tasks it covers, so re-preparing the same reminder
    yields the same key and the connector deduplicates it.
    """
    return OutlookMessage(
        idempotency_key=draft.draft_id,            # type: ignore[attr-defined]
        onboarding_id=draft.onboarding_id,         # type: ignore[attr-defined]
        client_id=draft.client_id,                 # type: ignore[attr-defined]
        recipient_role=str(draft.recipient_role),  # type: ignore[attr-defined]
        subject_en=draft.subject_en,               # type: ignore[attr-defined]
        body_en=draft.body_en,                     # type: ignore[attr-defined]
        subject_ar=draft.subject_ar,               # type: ignore[attr-defined]
        body_ar=draft.body_ar,                     # type: ignore[attr-defined]
        task_ids=tuple(draft.task_ids),            # type: ignore[attr-defined]
        approval_id=approval_id,
    )


__all__ = ["MOCK_ID_PREFIX", "MockOutlookConnector", "message_from_draft"]
