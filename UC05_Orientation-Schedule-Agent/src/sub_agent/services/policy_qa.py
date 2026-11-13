"""Grounded HR policy answering: PolicyKnowledgeBase retrieval over the authoritative policy document
(HR_Policy_Consultants.md, see context_layer/knowledge/policy_index.py) + the shared LLM gateway.

This module answers ONE ordinary policy question. Deciding whether a question may be answered at all
(salary, medical, accessibility, language) and escalating to HR is services/policy_assistant.py,
which calls this only for questions the deterministic privacy classifier passed.

The system prompt is the `hr-policy-answerer` skill (context_layer/skills); its name@version is
recorded in the audit trail of every answered question.

Defences, in order:
  1. a question that trips the shared injection detector is refused before retrieval (never sent to
     the model);
  2. retrieved sections that trip it are dropped by PolicyKnowledgeBase;
  3. the sections that remain are wrapped as untrusted data;
  4. every cited claim is checked against the retrieved text (quote_is_grounded); a claim naming a
     ref that was never offered is rejected;
  5. an answer containing a pay figure that is not in a cited quote is withheld (output privacy);
  6. nothing validated -> a no-answer result; no candidate at all -> the model is never called.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from context_layer.knowledge.policy_index import PolicyCitation, PolicyKnowledgeBase
from context_layer.skills import load_skill
from hr_onboarding_common.llm import LLMClient, ModelTier, input_block
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.guardrails import (detect_injection, detect_pay_figures, normalize_text,
                                                      quote_is_grounded, wrap_untrusted)

SKILL = load_skill("hr-policy-answerer")
PREAMBLE = SKILL.instructions
LANGUAGE_NAMES = {"en": "English", "ar": "Arabic"}


class CitedClaim(BaseModel):
    citation_ref: str
    quote: str


class PolicyQAModelOutput(BaseModel):
    answer: str | None
    cited_claims: list[CitedClaim]
    insufficient_evidence: bool
    additional_information: str | None = None


class PolicyEscalationRef(BaseModel):
    """What the employee may know about their escalation: an id to track, its status, and whether the
    HR notice was delivered. `notification` is honest about mocks: 'simulated' means the configured
    notifier is a simulation and nothing left the system."""

    escalation_id: str
    status: str  # open | resolved | closed | withdrawn
    category: str
    reason: str
    notification: str  # delivered | simulated | rejected | uncertain | not_attempted
    created: bool = True  # False when an identical open escalation already existed


class PolicyAnswer(BaseModel):
    answer: str | None
    citations: list[PolicyCitation]
    no_answer_reason: str | None = None
    # Added for the real-policy assistant; every field has a default, so older callers are unaffected.
    category: str = "general"
    language: str = "en"
    additional_information: str | None = None
    policy_reference: list[str] = Field(default_factory=list)
    message: str | None = None  # the text to show the employee (formatted answer or neutral notice)
    escalation: PolicyEscalationRef | None = None


def format_answer(answer: str, references: list[str], additional: str | None) -> str:
    parts = [f"Answer:\n{answer.strip()}", "Policy Reference:\n" + "\n".join(references)]
    if additional and additional.strip():
        parts.append(f"Additional Information:\n{additional.strip()}")
    return "\n\n".join(parts)


def _unsupported_figures(text: str, quotes: list[str]) -> list[str]:
    cited = normalize_text(" ".join(quotes))
    return [f for f in detect_pay_figures(text) if normalize_text(f) not in cited]


def answer_policy_question(llm: LLMClient, kb: PolicyKnowledgeBase, audit: AuditLog, *, question: str,
                           client_id: str, as_of: date, employee_id: str | None = None,
                           language: str = "en") -> PolicyAnswer:
    injection_hits = detect_injection(question)
    if injection_hits:
        audit.record("policy_qa.no_answer", subject_id=employee_id, client_id=client_id,
                     reason="injection_suspected", patterns=injection_hits)
        return PolicyAnswer(answer=None, citations=[], no_answer_reason="injection_suspected", category="injection",
                            language=language)

    candidates = kb.search(question, client_id=client_id, as_of=as_of)  # PolicySourceUnavailableError propagates
    if not candidates:
        audit.record("policy_qa.no_answer", subject_id=employee_id, client_id=client_id,
                     reason="no_matching_policy")
        return PolicyAnswer(answer=None, citations=[], no_answer_reason="no_matching_policy", language=language)

    payload = {"question": question, "answer_language": LANGUAGE_NAMES.get(language, language),
               "candidates": [{"ref": c.ref, "doc_id": c.citation.doc_id, "title": c.citation.title,
                               "section": c.citation.section} for c in candidates]}
    user = "Answer the question using only the policy excerpts below.\n\n" + input_block(payload) + "\n\n" + \
        "\n\n".join(wrap_untrusted(c.text, f"policy_document:{c.ref}") for c in candidates)
    result: PolicyQAModelOutput = llm.structured(task="onboarding.policy_qa", tier=ModelTier.WORKHORSE,
                                                 system=PREAMBLE, user=user, schema=PolicyQAModelOutput,
                                                 max_tokens=4000)

    by_ref = {c.ref: c for c in candidates}
    validated: list[PolicyCitation] = []
    quotes: list[str] = []
    seen_refs: set[str] = set()
    for claim in result.cited_claims:
        candidate = by_ref.get(claim.citation_ref)
        if candidate is None:
            audit.record("policy_qa.citation_rejected", subject_id=employee_id, client_id=client_id,
                         reason="unknown_ref", ref=claim.citation_ref)
            continue
        if not quote_is_grounded(claim.quote, candidate.text):
            audit.record("policy_qa.citation_rejected", subject_id=employee_id, client_id=client_id,
                         reason="ungrounded_quote", ref=claim.citation_ref)
            continue
        quotes.append(claim.quote)
        if candidate.ref in seen_refs:
            continue
        seen_refs.add(candidate.ref)
        validated.append(candidate.citation)

    if result.insufficient_evidence or not validated or not (result.answer or "").strip():
        audit.record("policy_qa.no_answer", subject_id=employee_id, client_id=client_id,
                     reason="insufficient_evidence")
        return PolicyAnswer(answer=None, citations=[], no_answer_reason="insufficient_evidence", language=language)

    leaked = _unsupported_figures((result.answer or "") + " " + (result.additional_information or ""), quotes)
    if leaked:
        audit.record("policy_qa.no_answer", subject_id=employee_id, client_id=client_id,
                     reason="unsupported_pay_figure", count=len(leaked))
        return PolicyAnswer(answer=None, citations=[], no_answer_reason="unsupported_pay_figure", language=language)

    references = [c.reference for c in validated]
    audit.record("policy_qa.answered", subject_id=employee_id, client_id=client_id,
                 citation_count=len(validated), skill=SKILL.ref, policy_version=validated[0].version,
                 sections=[c.section for c in validated])
    return PolicyAnswer(answer=result.answer, citations=validated, no_answer_reason=None, language=language,
                        additional_information=result.additional_information, policy_reference=references,
                        message=format_answer(result.answer or "", references, result.additional_information))
