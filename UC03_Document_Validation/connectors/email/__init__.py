"""Email ingestion connectors package."""

from .base import EmailAttachment, EmailIngestionConnectorBase, IncomingOnboardingEmail
from .imap_client import IMAPEmailConnector
from .mock import MockEmailConnector

__all__ = [
    "EmailAttachment",
    "EmailIngestionConnectorBase",
    "IncomingOnboardingEmail",
    "IMAPEmailConnector",
    "MockEmailConnector",
]
