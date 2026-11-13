"""UC step 9 trigger: on an employee's configured joining date, draft (never send) their welcome
communication exactly once.

There is no email event underlying a date-based trigger, so idempotency here is determined by
whether a welcome draft already exists for the employee -- not by a raw event id (contrast with
sub_agent/services/intake.py, which is event-id idempotent because it has an event to key on).
Cancelled/withdrawn cases are skipped and explicitly reported, never silently welcomed.
"""

from __future__ import annotations

from datetime import date

from hr_onboarding_common.llm import LLMClient
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import HITLInbox
from sub_agent.models import CaseStatus
from sub_agent.repository import OnboardingPort
from sub_agent.services import welcome

AGENT_ID = "employee-onboarding"


def run_joining_day_trigger(port: OnboardingPort, llm: LLMClient, hitl: HITLInbox, audit: AuditLog, *,
                            client_id: str, as_of: date, template_id: str = "welcome") -> list[dict]:
    """Processes every case in client_id whose start_date == as_of. Returns one result dict per
    matching employee: {"employee_id", "status": "drafted" | "skipped_already_drafted" |
    "skipped_inactive", ...}. Safe to call repeatedly for the same as_of -- already-drafted
    employees are skipped, not re-drafted."""
    results: list[dict] = []
    for record in port.list_onboarding_records(client_id=client_id):
        if record.start_date != as_of:
            continue

        if record.case_status != CaseStatus.ACTIVE:
            audit.record("joining_trigger.skipped_inactive", agent=AGENT_ID, subject_id=record.employee_id,
                         case_status=record.case_status)
            results.append({"employee_id": record.employee_id, "status": "skipped_inactive",
                            "case_status": record.case_status})
            continue

        already_drafted = any(d.template_id == template_id for d in port.list_communication_drafts(
            record.employee_id))
        if already_drafted:
            audit.record("joining_trigger.skipped_already_drafted", agent=AGENT_ID,
                         subject_id=record.employee_id)
            results.append({"employee_id": record.employee_id, "status": "skipped_already_drafted"})
            continue

        client = welcome.client_config_for(port, record.client_id)  # MissingClientConfigError
        draft, item = welcome.draft_welcome_communication(
            port, llm, hitl, audit, employee_id=record.employee_id, template_id=template_id,
            language=client.default_language, workflow_instance=record.employee_id)
        audit.record("joining_trigger.drafted", agent=AGENT_ID, subject_id=record.employee_id, draft_id=draft.id,
                     approval_id=item.id)
        results.append({"employee_id": record.employee_id, "status": "drafted", "draft_id": draft.id,
                        "approval_id": item.id})
    return results
