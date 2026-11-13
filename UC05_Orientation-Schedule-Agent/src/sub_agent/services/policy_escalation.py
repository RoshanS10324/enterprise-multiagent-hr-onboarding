"""Private HR escalation of policy questions, built on the EXISTING shared mechanisms -- no new
escalation system:

  persisted as     an HITLInbox approval item (gate `hr_policy_escalation`, approver role hr_admin);
                   in supabase mode that is approval_requests, otherwise hitl.db / memory
  HR is told by    OnboardingToolRegistry.notify_approver -> NotificationPort (Teams/Outlook abstraction),
                   through the operation ledger (one notice per escalation, retry-safe)
  tracked by       the item's status: open (pending) -> resolved (approved/edited) | closed (rejected)
  employee told    only when HR resolves it with an `employee_message` (an EDITED decision): delivered
                   once through NotificationPort; otherwise HR replies through their own channel

Privacy: the item carries the minimum -- category, neutral reason, requester, employee reference,
language, a hash of the question (for duplicate detection), timestamps. The question TEXT is included
only for categories that are not sensitive (not covered by the policy / language not enabled), so HR
can answer it; for salary, medical and accessibility matters it is never stored -- HR contacts the
employee privately. The rationale shown to HR is a fixed neutral sentence per category.

Status honesty: `notification` reports what actually happened -- 'delivered' only when a real
provider accepted it, 'simulated' when the configured notifier is a simulation (today: always),
'rejected' / 'uncertain' on failure, 'not_attempted' when no notifier is configured. An escalation is
never described to the employee as answered or resolved before HR decides it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from connectors.notifications.base import (DeliveryReceipt, NotificationChannel, NotificationDeliveryError,
                                           NotificationMessage, NotificationPort, NotificationRejectedError)
from hr_onboarding_common.contracts import new_id, now_utc
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.guardrails import normalize_text
from hr_onboarding_common.platform.hitl import ApprovalItem, Decision, HITLInbox
from sub_agent.models import DuplicateTaskError, OnboardingTask, OperationStatus
from sub_agent.repository import OnboardingPort
from sub_agent.services.delivery import deliver_once
from sub_agent.services.policy_qa import PolicyEscalationRef

AGENT_ID = "employee-onboarding"
GATE = "hr_policy_escalation"
PROPOSAL_TYPE = "policy_escalation"
ACCESSIBILITY_TASK = "hr_accessibility_review"  # neutral: says nothing about the person's condition

REASONS = {
    "salary_own": "compensation_or_terms_query",
    "medical": "private_health_matter",
    "accessibility": "workplace_adjustment_request",
    "not_covered": "policy_does_not_cover_question",
    "unsupported_language": "language_not_enabled",
    "policy_unavailable": "policy_source_unavailable",
}
# The only text HR sees as the rationale: fixed and neutral, never the employee's words.
RATIONALE = {
    "salary_own": "An employee has a compensation or employment-terms question for HR. Please contact them directly.",
    "medical": "An employee has asked to discuss a private matter with HR. Please contact them directly.",
    "accessibility": "Accessibility or workplace adjustment review required. Please contact the employee directly.",
    "not_covered": "An employee asked a policy question the HR policy document does not cover. Please clarify.",
    "unsupported_language": "An employee asked a policy question in a language not enabled for policy Q&A.",
    "policy_unavailable": "An employee asked a policy question while the HR policy document was unavailable.",
}
QUESTION_TEXT_ALLOWED = frozenset({"not_covered", "unsupported_language", "policy_unavailable"})
_STATUS = {Decision.PENDING: "open", Decision.APPROVED: "resolved", Decision.EDITED: "resolved",
           Decision.REJECTED: "closed", Decision.WITHDRAWN: "withdrawn"}


def question_ref(question: str) -> str:
    return hashlib.sha256(normalize_text(question).encode("utf-8")).hexdigest()[:16]


def notice_key(client_id: str, approval_id: str) -> str:
    """The operation-ledger key OnboardingToolRegistry.notify_approver uses for this item."""
    return f"approval-request:{client_id}:{approval_id}"


def notification_state(port: OnboardingPort, item: ApprovalItem) -> str:
    op = port.get_operation(notice_key(item.client_id, item.id))
    if op is None:
        return "not_attempted"
    if op.status == OperationStatus.SUCCEEDED:
        return "simulated" if op.detail.get("simulated", True) else "delivered"
    if op.status == OperationStatus.REJECTED:
        return "rejected"
    return "uncertain"


def to_ref(port: OnboardingPort, item: ApprovalItem, *, created: bool = True) -> PolicyEscalationRef:
    return PolicyEscalationRef(escalation_id=item.id, status=_STATUS[item.status], category=item.payload["category"],
                               reason=item.payload["reason"], notification=notification_state(port, item),
                               created=created)


def open_escalation(hitl: HITLInbox, audit: AuditLog, port: OnboardingPort, *, client_id: str, requester_id: str,
                    employee_id: str | None, category: str, question: str, language: str,
                    notify: Callable[[ApprovalItem], DeliveryReceipt] | None) -> PolicyEscalationRef:
    """Idempotent per (requester, category, question): an identical OPEN escalation is returned, not
    duplicated, and its HR notice is (re)attempted only if it never succeeded."""
    ref = question_ref(question)
    existing = [i for i in hitl.list(status=Decision.PENDING, gate=GATE, client_id=client_id)
                if i.agent == AGENT_ID and i.proposal_type == PROPOSAL_TYPE
                and i.payload.get("requester_id") == requester_id and i.payload.get("question_ref") == ref
                and i.payload.get("category") == category]
    created = not existing
    if existing:
        item = existing[0]
    else:
        payload = {"category": category, "reason": REASONS[category], "requester_id": requester_id,
                   "employee_id": employee_id, "question_ref": ref, "language": language,
                   "requested_at": now_utc().isoformat(),
                   "handling": "private" if category not in QUESTION_TEXT_ALLOWED else "standard",
                   "question": question if category in QUESTION_TEXT_ALLOWED else None}
        item = hitl.submit(ApprovalItem(
            gate=GATE, approver_role="hr_admin", workflow="employee-onboarding",
            workflow_instance=employee_id or requester_id, agent=AGENT_ID, proposal_type=PROPOSAL_TYPE,
            client_id=client_id, subject_id=employee_id or requester_id, payload=payload,
            rationale=RATIONALE[category], confidence=1.0, run_id=new_id("run")))
        audit.record("policy_qa.escalated", agent=AGENT_ID, subject_id=employee_id or requester_id,
                     client_id=client_id, approval_id=item.id, category=category, reason=REASONS[category],
                     question_ref=ref)
    if notify is not None and notification_state(port, item) in ("not_attempted", "rejected"):
        try:
            notify(item)
        except (NotificationRejectedError, NotificationDeliveryError) as exc:
            # the escalation stays persisted and visible in HR's queue; only the notice failed
            audit.record("policy_qa.escalation_notice_failed", agent=AGENT_ID, client_id=client_id,
                         approval_id=item.id, error_type=type(exc).__name__)
    return to_ref(port, item, created=created)


def add_neutral_accessibility_item(port: OnboardingPort, audit: AuditLog, *, employee_id: str,
                                   client_id: str) -> bool:
    """The onboarding plan gets ONLY a neutral HR item; no condition, request text or detail."""
    try:
        port.create_task(OnboardingTask(employee_id=employee_id, client_id=client_id, task_type=ACCESSIBILITY_TASK))
    except DuplicateTaskError:
        return False
    audit.record("checklist.task_created", agent=AGENT_ID, subject_id=employee_id, client_id=client_id,
                 task_type=ACCESSIBILITY_TASK)
    return True


def apply_escalation_decision(port: OnboardingPort, notifier: NotificationPort | None, audit: AuditLog,
                              item: ApprovalItem, *,
                              channel: NotificationChannel = NotificationChannel.EMAIL) -> dict:
    """HR's decision on an escalation: approved/edited = resolved, rejected = closed. An EDITED decision
    may carry `employee_message`: it is delivered once to the requester through NotificationPort.
    Re-running (retry_apply) never delivers twice (operation ledger)."""
    if item.agent != AGENT_ID or item.proposal_type != PROPOSAL_TYPE:
        return {"status": "ignored"}
    status = _STATUS[item.status]
    message = (item.final_payload or {}).get("employee_message") if item.status == Decision.EDITED else None
    delivery = "none"
    if isinstance(message, str) and message.strip():
        if notifier is None:
            delivery = "not_attempted"
        else:
            receipt = deliver_once(port, notifier, NotificationMessage(
                channel=channel, client_id=item.client_id, employee_id=item.payload.get("employee_id") or "",
                recipient_id=item.payload["requester_id"], kind="policy_escalation_response",
                subject="A response from HR", body=message.strip(), metadata={"approval_id": item.id}),
                key=f"policy-escalation:{item.client_id}:{item.id}")
            delivery = "simulated" if receipt.simulated else "delivered"
    audit.record("policy_qa.escalation_decided", agent=AGENT_ID, actor=item.approver, client_id=item.client_id,
                 subject_id=item.subject_id, approval_id=item.id, status=status,
                 category=item.payload.get("category"), response_delivery=delivery)
    return {"status": status, "response_delivery": delivery}
