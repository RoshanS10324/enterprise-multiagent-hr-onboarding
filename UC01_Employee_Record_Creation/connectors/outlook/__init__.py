"""Outlook connector: the acceptance-email trigger source for UC-01.

Only the mock is implemented. See `base.py` for the contract a real
Microsoft Graph implementation must satisfy, and `docs/known-gaps.md` /
OQ-01 for why email is a stand-in trigger in the first place.
"""

from .base import MailMessage, OutlookConnector, OutlookConnectorError
from .mock import MockOutlookConnector

__all__ = [
    "MailMessage",
    "MockOutlookConnector",
    "OutlookConnector",
    "OutlookConnectorError",
]
