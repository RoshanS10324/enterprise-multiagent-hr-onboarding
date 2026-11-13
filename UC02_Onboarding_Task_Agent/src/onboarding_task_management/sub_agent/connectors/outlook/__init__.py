"""Outlook connector: port plus a MOCK adapter.

No real mail is sent by anything in this package. See ``base.py`` for the
port and ``mock.py`` for the only shipped implementation.
"""

from onboarding_task_management.sub_agent.connectors.outlook.base import (
    DeliveryReceipt,
    DeliveryStatus,
    OutlookConnector,
    OutlookConnectorError,
    OutlookDraftConnector,
    OutlookMessage,
)
from onboarding_task_management.sub_agent.connectors.outlook.mock import (
    MOCK_ID_PREFIX,
    MockOutlookConnector,
    message_from_draft,
)

__all__ = [
    "OutlookDraftConnector",
    "MOCK_ID_PREFIX",
    "DeliveryReceipt",
    "DeliveryStatus",
    "MockOutlookConnector",
    "OutlookConnector",
    "OutlookConnectorError",
    "OutlookMessage",
    "message_from_draft",
]
