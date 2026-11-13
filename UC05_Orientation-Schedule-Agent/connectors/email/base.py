"""EmailPort: the configured offer-acceptance email trigger. No real email-receiving integration
exists; the simulated MockEmailAdapter (connectors/email/mock.py) is the only implementation until
an email service and credentials are approved. Sending email is not part of this port: outbound
email is a notification channel (connectors/notifications; simulated by MockEmailSender).

employee_id is None when the message could not be parsed at all (malformed) -- that is a
distinct, explicit outcome from "parsed fine but the employee directory has incomplete data",
which sub_agent/services/intake.py handles separately.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel


class OfferAcceptanceEvent(BaseModel):
    event_id: str
    employee_id: str | None
    received_at: datetime
    raw_subject: str
    raw_body: str


class EmailServiceError(RuntimeError):
    """The email provider itself failed (outage, timeout, auth). Never confused with a malformed
    message -- a malformed message is data; a service error means we have no data at all."""


class EmailPort(Protocol):
    def get_event(self, event_id: str) -> OfferAcceptanceEvent: ...
    def is_processed(self, event_id: str) -> bool: ...
    def mark_processed(self, event_id: str) -> None: ...
