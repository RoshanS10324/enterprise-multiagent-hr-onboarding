"""Mock email connector for deterministic local testing and CI/CD pipelines."""

from __future__ import annotations

from typing import Sequence

from .base import EmailIngestionConnectorBase, IncomingOnboardingEmail


class MockEmailConnector(EmailIngestionConnectorBase):
    """In-memory email connector for unit tests and local simulations."""

    def __init__(self) -> None:
        self.inbox: list[IncomingOnboardingEmail] = []
        self.processed_ids: set[str] = set()
        self.sent_emails: list[dict[str, str | list[str]]] = []

    def queue_email(self, email_msg: IncomingOnboardingEmail) -> None:
        """Helper to stage an email in the mock inbox."""
        self.inbox.append(email_msg)

    async def fetch_unread_emails(self, limit: int = 20) -> list[IncomingOnboardingEmail]:
        """Fetch emails that haven't been marked as processed."""
        unread = [e for e in self.inbox if e.message_id not in self.processed_ids]
        return unread[:limit]

    async def mark_email_processed(self, message_id: str) -> bool:
        """Mark mock email as processed."""
        self.processed_ids.add(message_id)
        return True

    async def send_email(self, to: Sequence[str], subject: str, body: str) -> bool:
        """Capture sent email in memory."""
        self.sent_emails.append({
            "to": list(to),
            "subject": subject,
            "body": body,
        })
        return True
