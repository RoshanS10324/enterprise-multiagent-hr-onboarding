"""Base interface (Protocol) for the Outlook connector.

The real implementation would call Microsoft Graph API to:
  - Send HR notification emails (e.g., equipment readiness, access provisioning)
  - Read/create calendar events for equipment handover slots

No real network calls exist in this file. This is the port definition that the mock
and (future) real connector must satisfy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class EmailMessage:
    """A plain email message to be sent via Outlook."""

    to: list[str]
    subject: str
    body: str
    cc: list[str] | None = None
    html: bool = False


@dataclass
class CalendarEvent:
    """A calendar event to be created/queried via Outlook."""

    subject: str
    start: str          # ISO-8601 datetime string
    end: str            # ISO-8601 datetime string
    attendees: list[str]
    location: str | None = None
    body: str | None = None


@dataclass
class SentMessage:
    message_id: str
    to: list[str]
    subject: str


@dataclass
class CreatedEvent:
    event_id: str
    subject: str
    start: str
    end: str


@runtime_checkable
class OutlookConnectorBase(Protocol):
    """Port definition for Microsoft Outlook / Graph API integration.

    UC-04 uses this to:
      - Notify HR/employees about equipment and access readiness (BR-02, BR-03)
      - Manage handover calendar slots (COLLECT mode, EC-22)

    The real implementation is a future integration point (requires Microsoft Entra
    service principal, Graph API scopes: Mail.Send, Calendars.ReadWrite).
    """

    def send_email(self, message: EmailMessage) -> SentMessage:
        """Send an email message on behalf of the UC-04 agent mailbox."""
        ...

    def create_event(self, event: CalendarEvent) -> CreatedEvent:
        """Create a calendar event for equipment handover scheduling."""
        ...

    def cancel_event(self, event_id: str) -> None:
        """Cancel a previously created calendar event (EC-35 client change)."""
        ...
