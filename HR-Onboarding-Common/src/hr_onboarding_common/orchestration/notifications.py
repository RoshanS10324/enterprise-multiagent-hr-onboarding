"""MOCK notification port for the document-collection request. Simulation only: no email, SMS,
Teams or any other message is ever sent, and nothing here opens a network connection.

Why the orchestrator owns this mock instead of using UC02's: UC02 exposes no document-collection
operation, and its mock Outlook connector refuses every delivery that lacks an approved gate
decision (UC02's own rule: delivery belongs to UC06 behind `joining_confirmation`). Using it here
would mean inventing an approval. This port mirrors that connector's semantics otherwise:

  - delivery is keyed on `idempotency_key`; a repeated send returns `duplicate_ignored` and the
    outbox does not grow (what a real adapter must also guarantee);
  - recipients are opaque references (`employee:<id>`), never addresses or names;
  - failures are injectable and deterministic: `fail_times` makes the first n attempts of a key
    report `failed` (known not delivered), `raise_times` makes them raise
    `NotificationTransportError` (outcome unknown -- the caller must treat it as uncertain);
    `fail_next` / `raise_next` do the same for the next n sends of any key.
"""

from __future__ import annotations

import collections
import threading
from typing import Literal, Protocol

from pydantic import BaseModel

MOCK_ID_PREFIX = "MOCK-NOTIFY"


class NotificationTransportError(RuntimeError):
    """The transport failed in a way that leaves the delivery outcome unknown."""


class NotificationReceipt(BaseModel):
    idempotency_key: str
    status: Literal["simulated_sent", "duplicate_ignored", "failed"]
    message_id: str | None = None
    detail: str = ""
    synthetic: bool = True


class NotificationMessage(BaseModel):
    idempotency_key: str
    client_id: str
    case_id: str
    recipient: str
    subject: str
    body: str
    document_types: list[str]


class NotificationPort(Protocol):
    def send(self, message: NotificationMessage) -> NotificationReceipt: ...


class MockNotificationOutbox:
    """Deterministic in-memory stand-in. Thread-safe: the orchestrator may call it from a graph
    worker thread."""

    def __init__(self, *, fail_times: dict[str, int] | None = None, raise_times: dict[str, int] | None = None,
                 fail_next: int = 0, raise_next: int = 0):
        self.fail_times = dict(fail_times or {})
        self.raise_times = dict(raise_times or {})
        self.fail_next, self.raise_next = fail_next, raise_next
        self._outbox: dict[str, NotificationMessage] = {}
        self._attempts: collections.Counter[str] = collections.Counter()
        self._lock = threading.Lock()

    def send(self, message: NotificationMessage) -> NotificationReceipt:
        with self._lock:
            key = message.idempotency_key
            self._attempts[key] += 1
            attempt = self._attempts[key]
            if key in self._outbox:
                return NotificationReceipt(idempotency_key=key, status="duplicate_ignored",
                                           message_id=f"{MOCK_ID_PREFIX}-{key}",
                                           detail="an identical request was already accepted")
            if self.raise_next or attempt <= self.raise_times.get(key, 0):
                self.raise_next = max(self.raise_next - 1, 0)
                raise NotificationTransportError(f"simulated transport loss (attempt {attempt})")
            if self.fail_next or attempt <= self.raise_times.get(key, 0) + self.fail_times.get(key, 0):
                self.fail_next = max(self.fail_next - 1, 0)
                return NotificationReceipt(idempotency_key=key, status="failed",
                                           detail=f"simulated delivery failure (attempt {attempt})")
            self._outbox[key] = message
            return NotificationReceipt(idempotency_key=key, status="simulated_sent",
                                       message_id=f"{MOCK_ID_PREFIX}-{key}",
                                       detail="simulated only -- no message was sent")

    def outbox(self, client_id: str) -> list[NotificationMessage]:
        with self._lock:
            return [m for _, m in sorted(self._outbox.items()) if m.client_id == client_id]

    def attempts_for(self, idempotency_key: str) -> int:
        with self._lock:
            return self._attempts[idempotency_key]

    def sent_count(self) -> int:
        with self._lock:
            return len(self._outbox)
