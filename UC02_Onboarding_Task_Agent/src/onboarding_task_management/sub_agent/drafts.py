"""Team-specific assignment drafts and the send gate. No real transport exists in this package.

After HR releases a plan, each owning role gets one draft listing its tasks (id, template key, due date, priority
and the action required). Drafts carry no personal data: recipients are roles, never addresses, and the body holds
identifiers and dates only. A draft is only a draft until a *different*, authorised HR person authorises it, and the
send itself goes through a :class:`SendProvider`. UC-02 ships none that delivers: :class:`NoTransportSender` reports
``not_configured`` and :class:`MockSender` (tests only) reports ``mock_delivered`` - neither is ever recorded as
``sent``. The real sender is UC-06 / the notification connector.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from onboarding_task_management.contracts import (
    OPEN_STATUSES,
    OnboardingRecord,
    ReminderDraft,
    Role,
)
from onboarding_task_management.sub_agent.checklist import ClientOnboardingConfig

TEAM_REASON = "team_assignment"
MAX_SEND_ATTEMPTS = 3

OUTCOME_SENT = "sent"  # reserved for a provider that confirms delivery; none exists here
OUTCOME_MOCK = "mock_delivered"
OUTCOME_FAILED = "failed"
OUTCOME_NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class SendResult:
    outcome: str
    reference: str = ""
    error: str = ""
    #: False for every provider in this package.
    real: bool = False


@runtime_checkable
class SendProvider(Protocol):
    name: str

    def send(self, draft: ReminderDraft) -> SendResult: ...


class NoTransportSender:
    """The default: nothing can be sent, and it says so."""

    name = "no_transport"

    def send(self, draft: ReminderDraft) -> SendResult:
        return SendResult(OUTCOME_NOT_CONFIGURED, error="no notification transport is configured")


def team_draft_id(onboarding_id: str, owner: Role, plan_version: int) -> str:
    return f"TDR-{onboarding_id}#{owner.value}#p{plan_version}"


def build_team_drafts(record: OnboardingRecord, config: ClientOnboardingConfig | None = None) -> list[ReminderDraft]:
    """One draft per owning role covering that role's open tasks. Deterministic; no address, no personal data."""
    by_owner: dict[Role, list] = {}
    for task in record.tasks:
        if task.status in OPEN_STATUSES:
            by_owner.setdefault(task.owner, []).append(task)
    drafts = []
    for owner, tasks in sorted(by_owner.items(), key=lambda kv: kv[0].value):
        tasks.sort(key=lambda t: (t.due_date, t.task_id))
        teams = {rule.team for t in tasks if config and (rule := config.assignment_for(t.category))}
        team = ", ".join(sorted(teams)) or "no team configured"
        lines = [
            f"- {t.task_id}: {t.template_key} | due {t.due_date.isoformat()} | priority {t.priority} | "
            f"action: complete and report status to UC-02"
            for t in tasks
        ]
        drafts.append(ReminderDraft(
            draft_id=team_draft_id(record.onboarding_id, owner, record.plan_version),
            onboarding_id=record.onboarding_id, client_id=record.client_id, employee_id=record.employee_id,
            task_ids=[t.task_id for t in tasks], recipient_role=owner, reason=TEAM_REASON,
            subject_en=f"Onboarding assignments: {len(tasks)} task(s) for {owner.value}",
            body_en="\n".join([
                f"Onboarding {record.onboarding_id}, plan version {record.plan_version}, "
                f"configuration {record.config_version or 'unspecified'}.",
                f"Team: {team}.", "Tasks assigned to your team:", *lines,
            ]),
        ))
    return drafts


__all__ = [
    "MAX_SEND_ATTEMPTS", "NoTransportSender", "OUTCOME_FAILED", "OUTCOME_MOCK", "OUTCOME_NOT_CONFIGURED",
    "OUTCOME_SENT", "SendProvider", "SendResult", "TEAM_REASON", "build_team_drafts", "team_draft_id",
]
