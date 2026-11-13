"""Mock Outlook connector for local simulation and tests.

Captures all outbound calls in memory. No real email is sent; no real calendar
event is created. Used by tests, the demo script and the evals runner.
"""
from __future__ import annotations

import uuid

from .base import CalendarEvent, CreatedEvent, EmailMessage, SentMessage


class MockOutlookConnector:
    """In-memory mock. Satisfies OutlookConnectorBase without a network call.

    Attributes:
        sent_emails: All email messages dispatched in this session (synthetic).
        created_events: All calendar events created in this session.
        cancelled_events: IDs of events cancelled (EC-35 client-change cancellations).
        fail_on_send: If True, raises RuntimeError on send_email (failure-path testing).
        fail_on_create: If True, raises RuntimeError on create_event.
    """

    real_side_effects = False

    def __init__(
        self,
        *,
        fail_on_send: bool = False,
        fail_on_create: bool = False,
    ) -> None:
        self.sent_emails: list[SentMessage] = []
        self.created_events: list[CreatedEvent] = []
        self.cancelled_events: list[str] = []
        self.fail_on_send = fail_on_send
        self.fail_on_create = fail_on_create

    # ---------------------------------------------------------------------- protocol implementation

    def send_email(self, message: EmailMessage) -> SentMessage:
        if self.fail_on_send:
            raise RuntimeError("MockOutlookConnector: simulated send failure")
        sent = SentMessage(
            message_id=f"SYN-MSG-{uuid.uuid4().hex[:8].upper()}",
            to=list(message.to),
            subject=message.subject,
        )
        self.sent_emails.append(sent)
        return sent

    def create_event(self, event: CalendarEvent) -> CreatedEvent:
        if self.fail_on_create:
            raise RuntimeError("MockOutlookConnector: simulated calendar failure")
        created = CreatedEvent(
            event_id=f"SYN-EVT-{uuid.uuid4().hex[:8].upper()}",
            subject=event.subject,
            start=event.start,
            end=event.end,
        )
        self.created_events.append(created)
        return created

    def cancel_event(self, event_id: str) -> None:
        self.cancelled_events.append(event_id)

    # ---------------------------------------------------------------------- inspection helpers (tests)

    @property
    def email_count(self) -> int:
        return len(self.sent_emails)

    @property
    def event_count(self) -> int:
        return len(self.created_events)

    def reset(self) -> None:
        """Clear all captured state (useful for dev-reset scenarios)."""
        self.sent_emails.clear()
        self.created_events.clear()
        self.cancelled_events.clear()
