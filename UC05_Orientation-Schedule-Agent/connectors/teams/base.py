"""TeamsPort: Microsoft Teams as a delivery channel for onboarding messages.

Teams is not a separate kind of integration here: approval requests, buddy notices and welcome or
team-introduction messages are notifications, so a Teams adapter is a NotificationPort
(connectors/notifications/base.py) restricted to NotificationChannel.TEAMS. The approval itself is
never made in Teams -- it is recorded in the HITLInbox and decided through
POST /v1/approvals/{id}/decision; a Teams message only tells the approver it is waiting.

Contract a Teams adapter must honour, on top of NotificationPort's:
  - send() delivers only messages whose channel is TEAMS, and refuses any other channel with
    NotificationRejectedError (never silently re-routes it to email);
  - recipient_id is a person's id, or "role:<role>" for a role's shared queue (e.g. an HR channel);
  - idempotency, receipts and the two failure types are exactly NotificationPort's.

No real adapter exists: there is no Azure AD app registration, bot or incoming webhook, tenant
consent or credential. connectors/teams/mock.py (MockTeamsAdapter) is a simulation only. Teams
meeting creation, if ever needed, belongs behind connectors.calendar.base.CalendarPort.
"""

from __future__ import annotations

from typing import Protocol

from connectors.notifications.base import NotificationChannel, NotificationPort

TEAMS_CHANNEL = NotificationChannel.TEAMS


class TeamsPort(NotificationPort, Protocol):
    """A NotificationPort that delivers to Microsoft Teams only (see the module docstring)."""
