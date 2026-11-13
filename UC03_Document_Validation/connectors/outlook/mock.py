from __future__ import annotations

from .base import EmailMessage, OutlookConnectorBase


class MockOutlookConnector(OutlookConnectorBase):
    """In-memory Outlook connector for tests and local development."""

    def __init__(self) -> None:
        self._sent: list[EmailMessage] = []
        self._inbox: list[EmailMessage] = []

    async def send_email(self, message: EmailMessage) -> bool:
        self._sent.append(message)
        return True

    async def get_emails(self, folder: str, limit: int = 10) -> list[EmailMessage]:
        return self._inbox[:limit]

    async def move_email(self, email_id: str, target_folder: str) -> bool:
        return True

    @property
    def sent(self) -> list[EmailMessage]:
        return self._sent
