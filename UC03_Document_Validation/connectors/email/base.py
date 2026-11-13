"""Data models and abstract base connector for email-driven onboarding ingestion."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence


@dataclass
class EmailAttachment:
    """An attachment extracted from an incoming onboarding email."""
    filename: str
    content_type: str
    size_bytes: int
    raw_bytes: bytes
    is_inline: bool = False
    content_id: str | None = None

    @property
    def is_supported_document(self) -> bool:
        """Verify if file type is an accepted onboarding document format."""
        allowed_mimes = {
            "application/pdf",
            "image/jpeg",
            "image/png",
            "image/tiff",
            "image/webp",
        }
        allowed_exts = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".webp"}
        ext = "." + self.filename.rsplit(".", 1)[-1].lower() if "." in self.filename else ""
        return (self.content_type.lower() in allowed_mimes) or (ext in allowed_exts)


@dataclass
class IncomingOnboardingEmail:
    """A candidate email containing documents for onboarding intake."""
    message_id: str
    sender_email: str
    sender_name: str
    subject: str
    body_text: str
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    attachments: list[EmailAttachment] = field(default_factory=list)
    raw_headers: dict[str, str] = field(default_factory=dict)

    @property
    def valid_attachments(self) -> list[EmailAttachment]:
        """Return non-inline document attachments exceeding 15KB or matching supported formats."""
        valid: list[EmailAttachment] = []
        for att in self.attachments:
            if att.is_inline and att.size_bytes < 15_000:
                continue  # Filter out corporate logos & email signature icons
            if att.is_supported_document:
                valid.append(att)
        return valid


class EmailIngestionConnectorBase(ABC):
    """Abstract interface for mailbox pollers (IMAP, Microsoft Graph, Mock)."""

    @abstractmethod
    async def fetch_unread_emails(self, limit: int = 20) -> list[IncomingOnboardingEmail]:
        """Fetch unread emails with attachments from the designated HR inbox."""
        ...

    @abstractmethod
    async def mark_email_processed(self, message_id: str) -> bool:
        """Mark email as read/processed so it is not re-ingested."""
        ...

    @abstractmethod
    async def send_email(self, to: Sequence[str], subject: str, body: str) -> bool:
        """Send an approved notification or candidate replacement request."""
        ...
