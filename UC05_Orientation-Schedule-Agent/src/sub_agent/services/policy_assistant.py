"""The HR policy assistant: one entry point for every employee policy question (API route, workflow
stage 10 and the orchestrator worker all reach it through OnboardingToolRegistry.answer_policy_question).

Processing order -- the deterministic steps cannot be overridden by any model:
  1-3  the requester is authenticated, tenant-resolved and authorised (API identity + tool registry)
  4    privacy classification (services/policy_privacy.py), before retrieval and before any model call
  5-6  privacy restrictions / escalation decision:
         salary_other          polite refusal; privacy-safe audit; no escalation, nothing confirmed
         salary_own            neutral notice + private HR escalation
         medical               neutral notice (the condition is never repeated) + private HR escalation
         accessibility         neutral notice + private HR escalation + a neutral plan item only
         unsupported_language  notice + HR escalation (the question goes to HR as written)
         injection             refusal (never sent to the model)
  7-10 general questions: retrieval from the authoritative policy, grounded answer, citation and
       output-privacy validation (services/policy_qa.py)
       -> not covered / insufficient evidence / policy unavailable: notice + HR escalation
  11   audit: category, rule names, question hash, escalation id -- never the question text
  12   PolicyAnswer: the formatted answer, or the neutral notice and the escalation reference
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from hr_onboarding_common.context_layer.hr_policy import PolicySourceUnavailableError
from hr_onboarding_common.llm import LLMClient
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import ApprovalItem, HITLInbox
from context_layer.knowledge.policy_index import PolicyKnowledgeBase
from connectors.notifications.base import DeliveryReceipt
from sub_agent.repository import OnboardingPort
from sub_agent.services import policy_escalation
from sub_agent.services.policy_privacy import classify_question
from sub_agent.services.policy_qa import PolicyAnswer, answer_policy_question

AGENT_ID = "employee-onboarding"

NOTICES = {
    "salary_other": ("I can't share or confirm information about another person's salary or employment terms. "
                     "Questions about someone else's compensation can't be answered here."),
    "salary_own": ("Compensation and employment-terms matters are handled directly by HR, so I can't discuss "
                   "them here. I've sent a private request to HR to contact you."),
    "medical": ("Thank you for letting us know. Matters like this are handled privately by HR, so I won't "
                "repeat or pass on the details of your message. I've sent a private request to HR to contact "
                "you."),
    "accessibility": ("Workplace adjustment requests are handled privately by HR. I've sent a private request "
                      "to HR to contact you; you don't need to share any medical details here."),
    "not_covered": ("I couldn't find an answer to this question in the available HR policy. I'll route it to HR "
                    "for clarification."),
    "unsupported_language": ("Policy questions can currently be answered in English only. I've routed your "
                             "question to HR, who will get back to you."),
    "policy_unavailable": ("The HR policy is temporarily unavailable, so I can't answer from it right now. I've "
                           "routed your question to HR."),
    "injection": "I can only answer questions about the HR policy. Please rephrase your question.",
}
_NO_ANSWER_TO_CATEGORY = {"no_matching_policy": "not_covered", "insufficient_evidence": "not_covered",
                          "unsupported_pay_figure": "not_covered"}


def _escalation_text(ref) -> str:
    notice = {"delivered": "HR has been notified.",
              "simulated": "Your request is in HR's queue (HR notifications are simulated in this environment).",
              "rejected": "Your request is in HR's queue; the notification to HR could not be sent.",
              "uncertain": "Your request is in HR's queue; the notification to HR is pending confirmation.",
              "not_attempted": "Your request is in HR's queue."}[ref.notification]
    return f"Escalation:\nReference {ref.escalation_id} (status: {ref.status}). {notice}"


def handle_policy_question(port: OnboardingPort, llm: LLMClient, kb: PolicyKnowledgeBase, hitl: HITLInbox,
                           audit: AuditLog, *, question: str, client_id: str, as_of: date, requester_id: str,
                           employee_id: str | None = None, asks_about_other_employee: bool = False,
                           enabled_languages: tuple[str, ...] | list[str] = ("en",),
                           notify: Callable[[ApprovalItem], DeliveryReceipt] | None = None) -> PolicyAnswer:
    c = classify_question(question, enabled_languages=enabled_languages,
                          asks_about_other_employee=asks_about_other_employee)
    subject = employee_id or requester_id
    if c.category != "general":
        audit.record("policy_qa.classified", agent=AGENT_ID, subject_id=subject, client_id=client_id,
                     category=c.category, rules=list(c.rules), language=c.language,
                     question_ref=policy_escalation.question_ref(question))

    if c.category == "injection":
        result = answer_policy_question(llm, kb, audit, question=question, client_id=client_id, as_of=as_of,
                                        employee_id=employee_id, language=c.language)
        return result.model_copy(update={"message": NOTICES["injection"]})

    if c.category == "salary_other":
        audit.record("policy_qa.privacy_refusal", agent=AGENT_ID, subject_id=subject, client_id=client_id,
                     category=c.category, requester_id=requester_id)
        return PolicyAnswer(answer=None, citations=[], no_answer_reason="privacy_restricted", category=c.category,
                            language=c.language, message=NOTICES["salary_other"])

    if c.escalate:
        return _escalate(port, hitl, audit, c.category, c.category, question, client_id=client_id,
                         requester_id=requester_id, employee_id=employee_id, language=c.language, notify=notify)

    try:
        result = answer_policy_question(llm, kb, audit, question=question, client_id=client_id, as_of=as_of,
                                        employee_id=employee_id, language=c.language)
    except PolicySourceUnavailableError:
        audit.record("policy_qa.policy_unavailable", agent=AGENT_ID, subject_id=subject, client_id=client_id)
        return _escalate(port, hitl, audit, "general", "policy_unavailable", question, client_id=client_id,
                         requester_id=requester_id, employee_id=employee_id, language=c.language, notify=notify,
                         no_answer_reason="policy_unavailable")
    if result.answer is None and result.no_answer_reason in _NO_ANSWER_TO_CATEGORY:
        return _escalate(port, hitl, audit, "general", _NO_ANSWER_TO_CATEGORY[result.no_answer_reason], question,
                         client_id=client_id, requester_id=requester_id, employee_id=employee_id,
                         language=c.language, notify=notify, no_answer_reason=result.no_answer_reason)
    return result


def _escalate(port, hitl, audit, category: str, escalation_category: str, question: str, *, client_id: str,
              requester_id: str, employee_id: str | None, language: str, notify,
              no_answer_reason: str | None = None) -> PolicyAnswer:
    ref = policy_escalation.open_escalation(
        hitl, audit, port, client_id=client_id, requester_id=requester_id, employee_id=employee_id,
        category=escalation_category, question=question, language=language, notify=notify)
    if escalation_category == "accessibility" and employee_id:
        try:
            policy_escalation.add_neutral_accessibility_item(port, audit, employee_id=employee_id, client_id=client_id)
        except KeyError:
            pass  # no onboarding case for this employee in UC05: the escalation alone carries the request
    return PolicyAnswer(answer=None, citations=[], no_answer_reason=no_answer_reason or "escalated_to_hr",
                        category=category if category != "general" else escalation_category, language=language,
                        message=NOTICES[escalation_category] + "\n\n" + _escalation_text(ref), escalation=ref)
