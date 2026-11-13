"""Microsoft Teams as a notification channel: the TeamsPort contract (base.py) and a simulated
adapter for tests and the offline demo (mock.py). No real Teams integration exists.

The onboarding workflow asks for HR approval "through the configured Teams or Outlook approval
channel" (sub_agent.agent, node request_hr_approval). That request is a notification: the
approval itself is recorded in the HITLInbox and decided through POST /v1/approvals/{id}/decision.
A Teams adapter therefore only needs to implement TeamsPort -- connectors.notifications.base.
NotificationPort for NotificationChannel.TEAMS (post a message or adaptive card to the approver,
with a link to the approval) -- and to raise NotificationDeliveryError / NotificationRejectedError
as documented there.

A real adapter is not built because none of the prerequisites exist: no Azure AD app registration,
bot or incoming webhook, tenant consent, or credentials have been identified or approved. Until
they are, the workflow is wired with connectors.notifications.mock.MockNotifier, whose receipts
are stamped simulated=True. Teams meeting creation, if ever needed, belongs behind
connectors.calendar.base.CalendarPort instead.
"""
