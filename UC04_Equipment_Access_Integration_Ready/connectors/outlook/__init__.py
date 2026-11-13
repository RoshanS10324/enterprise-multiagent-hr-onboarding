"""UC-04 Outlook connector package.

Provides base interface and mock implementation for email/calendar communication
via Microsoft Outlook (Microsoft Graph API). The real connector is a future integration
point; only the mock is used in local simulation.
"""
from .base import OutlookConnectorBase
from .mock import MockOutlookConnector

__all__ = ["OutlookConnectorBase", "MockOutlookConnector"]
