"""TEST / DEMO ONLY. Simulated email: the inbound offer-acceptance mailbox and an outbound sender.

Not Outlook, Exchange, Gmail or SMTP: nothing here connects to a mail server or holds a credential,
and no email reaches anyone. Two simulations, one per direction:

MockEmailAdapter -- implements EmailPort (connectors/email/base.py), the offer-acceptance trigger.
  Holds deterministic synthetic events (build_mock_email_adapter). Unknown event ids raise
  KeyError; FAILING_EVENT_ID raises EmailServiceError (a provider outage, so no data at all);
  EVT-MALFORMED is a message that could not be parsed (employee_id None). is_processed /
  mark_processed simulate the mailbox's processed flag; reset() clears it and restores the events.
  Events carry no tenant: the tenant comes from the employee directory record, and intake
  (sub_agent.services.intake) refuses an event whose employee belongs to another client.

MockEmailSender -- outbound email is a NotificationPort for NotificationChannel.EMAIL (welcome
  messages and notices to employees and buddies are notifications; see connectors/notifications).
  It is MockNotifier restricted to email, so it shares its simulated outcomes -- outage, lost
  response (timeout after acceptance), permanent refusal, idempotent resend scoped per tenant,
  reset() -- and additionally refuses a message that is not email or has no recipient address.
  Receipts say simulated=True and their delivery_ref starts with "sim-email".
"""

from __future__ import annotations

from datetime import datetime, timezone

from connectors.email.base import EmailServiceError, OfferAcceptanceEvent
from connectors.notifications.base import NotificationChannel, NotificationMessage, NotificationRejectedError
from connectors.notifications.mock import MockNotifier

FAILING_EVENT_ID = "EVT-PROVIDER-OUTAGE"  # requesting this event id simulates an email-service failure


class MockEmailAdapter:
    def __init__(self, events: list[OfferAcceptanceEvent]):
        self._initial = [e.model_copy(deep=True) for e in events]
        self.reset()

    def reset(self) -> None:
        """Back to the constructor's events, none of them processed."""
        self.events: dict[str, OfferAcceptanceEvent] = {e.event_id: e.model_copy(deep=True) for e in self._initial}
        self._processed: set[str] = set()

    # ---- EmailPort
    def get_event(self, event_id: str) -> OfferAcceptanceEvent:
        if event_id == FAILING_EVENT_ID:
            raise EmailServiceError(f"email provider request for {event_id} failed (simulated outage)")
        return self.events[event_id].model_copy(deep=True)  # KeyError: no such message

    def is_processed(self, event_id: str) -> bool:
        return event_id in self._processed

    def mark_processed(self, event_id: str) -> None:
        if event_id not in self.events:
            raise KeyError(event_id)  # the mailbox cannot flag a message it does not hold
        self._processed.add(event_id)


def build_mock_email_adapter() -> MockEmailAdapter:
    """Synthetic offer-acceptance events. EVT-002's employee has an incomplete directory record;
    EVT-010's employee belongs to CLIENT-TEST-B (tenant-isolation tests)."""
    received = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
    return MockEmailAdapter([
        OfferAcceptanceEvent(event_id="EVT-001", employee_id="EMP-TEST-001", received_at=received,
                             raw_subject="Offer accepted - Alex Morgan",
                             raw_body="Alex Morgan has accepted the offer for Software Engineer."),
        OfferAcceptanceEvent(event_id="EVT-002", employee_id="EMP-TEST-003", received_at=received,
                             raw_subject="Offer accepted - Priya Nair",
                             raw_body="Priya Nair has accepted the offer for QA Engineer."),
        OfferAcceptanceEvent(event_id="EVT-010", employee_id="EMP-TEST-010", received_at=received,
                             raw_subject="Offer accepted - Morgan Lee",
                             raw_body="Morgan Lee has accepted the offer for Support Engineer."),
        OfferAcceptanceEvent(event_id="EVT-MALFORMED", employee_id=None, received_at=received,
                             raw_subject="RE: Fwd: ???", raw_body="(message could not be parsed)"),
    ])


class MockEmailSender(MockNotifier):
    REF_PREFIX = "sim-email"

    def _validate(self, message: NotificationMessage) -> None:
        super()._validate(message)
        if message.channel != NotificationChannel.EMAIL:
            raise NotificationRejectedError(f"the email sender only delivers email, not {message.channel}")
        if not (message.recipient_address or "").strip():
            raise NotificationRejectedError(f"no email address for recipient {message.recipient_id}")
