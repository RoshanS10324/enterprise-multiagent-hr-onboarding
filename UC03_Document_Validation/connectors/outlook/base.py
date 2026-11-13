from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class EmailMessage:
    subject: str
    body: str
    sender: str
    recipients: list[str]
    attachments: list[str]


class OutlookConnectorBase(ABC):
    """Base interface for all Outlook connector implementations."""

    @abstractmethod
    async def send_email(self, message: EmailMessage) -> bool: ...

    @abstractmethod
    async def get_emails(self, folder: str, limit: int = 10) -> list[EmailMessage]: ...

    @abstractmethod
    async def move_email(self, email_id: str, target_folder: str) -> bool: ...
