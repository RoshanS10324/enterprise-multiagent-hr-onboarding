"""EmailPort: the configured offer-acceptance email trigger. Only the in-memory
MockEmailAdapter exists until a real email-receiving service and credentials are approved.

employee_id is None when the message could not be parsed at all (malformed) -- that is a
distinct, explicit outcome from "parsed fine but the employee directory has incomplete data",
which services/intake.py handles separately.
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
