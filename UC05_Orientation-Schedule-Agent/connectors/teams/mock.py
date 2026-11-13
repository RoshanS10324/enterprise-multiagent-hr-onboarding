"""TEST / DEMO ONLY. MockTeamsAdapter: an in-memory, simulated TeamsPort.

This is not Microsoft Teams. No message is posted to any chat or channel, no Graph or bot call is
made, and there is no credential. Receipts say simulated=True and their delivery_ref starts with
"sim-teams", so a mock message can never be presented as a real Teams message.

It is MockNotifier (connectors/notifications/mock.py) restricted to the Teams channel, so it
simulates exactly the same outcomes -- outage, lost response, permanent refusal, idempotent
resend, reset() -- and additionally refuses any message whose channel is not Teams, as the
TeamsPort contract requires. Use messages(kind=...) to inspect what was "posted": approval
requests ("approval_request"), welcome messages ("welcome"), buddy notices, and so on.
"""

from __future__ import annotations

from connectors.notifications.base import NotificationMessage, NotificationRejectedError
from connectors.notifications.mock import MockNotifier
from connectors.teams.base import TEAMS_CHANNEL


class MockTeamsAdapter(MockNotifier):
    REF_PREFIX = "sim-teams"

    def _validate(self, message: NotificationMessage) -> None:
        super()._validate(message)
        if message.channel != TEAMS_CHANNEL:
            raise NotificationRejectedError(f"the Teams adapter only delivers Teams messages, not {message.channel}")
