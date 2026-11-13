"""HR policy retrieval over the ONE authoritative policy document,
hr_onboarding_common/context_layer/HR_Policy_Consultants.md (hr_onboarding_common.context_layer.hr_policy).

The synthetic per-client policies this module used to read (knowledge/policies/{global,CLIENT-*})
have been removed. There is no fallback: if the document cannot be loaded, PolicyKnowledgeBase raises
PolicySourceUnavailableError and no question is answered.

Tenancy: the document is the employer's own HR policy for its consultants and names no client, so it
applies to every tenant. `client_id` is still required by search() (and recorded) so that a
client-specific policy can be added later without changing callers; no client-specific source exists
today.

Retrieval: the document is split into its `##`/`###` sections (verbatim text, heading path, page
range) and ranked with hr_onboarding_common's KnowledgeIndex (BM25), then
  1. multi-part questions are split ("...? ...", "and also") and each part retrieves on its own;
  2. a small, documented synonym table expands everyday words to the policy's own terms
     (e.g. "paid" -> payroll, "quit" -> resignation); it never adds policy content;
  3. a section is kept only if it shares at least one topic word (not "what/how/can/my/...") with the
     question -- otherwise the question is "not covered" and no candidate is returned;
  4. sections rank by how much of their own heading the question names plus how many of its topic
     words their text contains (BM25 rank breaks ties).
Retrieved text that trips the prompt-injection detector is dropped before any model sees it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pydantic import BaseModel

from hr_onboarding_common.context_layer.hr_policy import (PolicyDocument, PolicySourceUnavailableError,
                                                          load_policy_document)
from hr_onboarding_common.context_layer.knowledge import _tokens
from hr_onboarding_common.platform.guardrails import detect_injection

__all__ = ["PolicyCandidate", "PolicyCitation", "PolicyKnowledgeBase", "PolicySourceUnavailableError"]

# Question words that say nothing about the topic. A section matching only these is not relevant.
GENERIC_TOKENS = frozenset("""
what how when where why who whom which whose can could do does did done am is are was were be been being
have has had get gets got getting give given many much any some there here about please tell know need
want would will shall should may might must me my mine myself you your yours our us they them their
if then than so also too very just only still yet ok okay hi hello thanks thank question questions
find see look say said mean means way ways thing things go going come today tomorrow
policy policies hr company employee employees staff rule rules allowed allow entitled
""".split())

# Everyday words -> the policy's own vocabulary. Query expansion only; never added to answers.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "paid": ("payroll", "transferred"), "pay": ("payroll", "salary"), "payday": ("payroll",),
    "wage": ("salary", "payroll"), "wages": ("salary", "payroll"),
    "vacation": ("annual", "leave"), "holiday": ("leave", "holidays"), "holidays": ("leave",),
    "quit": ("resignation",), "resign": ("resignation",), "resigning": ("resignation",),
    "fired": ("termination", "dismissed"), "dismissal": ("termination", "dismissed"), "sacked": ("termination",),
    "overtime": ("outside", "business", "hours"), "weekend": ("saturday", "sunday"),
    "smoke": ("smoking",), "clothes": ("dress",), "wear": ("dress",), "dress": ("dress",),
    "late": ("attendance",), "gratuity": ("end", "service", "gratuity"), "eosb": ("gratuity",),
    "ticket": ("airfare", "tickets"), "flight": ("airfare", "ticket"), "insurance": ("medical", "insurance"),
    "bank": ("account",), "laptop": ("property",), "notice": ("notice",), "sick": ("sick", "leave"),
    "probation": ("probation",), "freelance": ("concurrent", "external", "employment"),
    "side": ("concurrent",), "second": ("concurrent",), "job": ("employment",),
}

_PART_SPLIT = re.compile(r"\?\s+|;\s+|\band also\b|\balso,?\s+(?=(?:what|how|when|can|do|is|are)\b)", re.I)


class PolicyCitation(BaseModel):
    """Source attribution for one policy excerpt used in an answer."""

    doc_id: str
    title: str
    section: str
    tier: str
    version: str
    effective_date: date | None = None  # the document states none; see hr_policy.py
    client_id: str | None = None  # None = applies to every tenant
    pages: str | None = None  # e.g. "pp. 6–7" of the source PDF

    @property
    def reference(self) -> str:
        return f"{self.title} — {self.section}" + (f" ({self.pages})" if self.pages else "")


@dataclass(frozen=True)
class PolicyCandidate:
    ref: str  # "C1", "C2", ... -- how the LLM is asked to refer back to this excerpt
    text: str
    citation: PolicyCitation


def _topic_tokens(text: str) -> set[str]:
    tokens = {t for t in _tokens(text) if t not in GENERIC_TOKENS}
    for t in list(tokens):
        tokens.update(SYNONYMS.get(t, ()))
    return tokens


def split_question(question: str) -> list[str]:
    parts = [p.strip(" ?.") for p in _PART_SPLIT.split(question) if p and p.strip(" ?.")]
    return parts or [question]


class PolicyKnowledgeBase:
    """`source` is an explicit document path (tests, a mounted reviewed copy); by default the packaged
    document or $HR_POLICY_SOURCE_PATH. Loading happens on first use and failures are not cached, so a
    restored document is picked up by `reload()` without a restart."""

    def __init__(self, source: Path | str | None = None, *, document: PolicyDocument | None = None):
        self.source = source
        self._document = document
        self._index = document.index() if document is not None else None

    @property
    def document(self) -> PolicyDocument:
        if self._document is None:
            self._document = load_policy_document(self.source)  # PolicySourceUnavailableError
            self._index = self._document.index()
        return self._document

    def reload(self) -> PolicyDocument:
        self._document = self._index = None
        return self.document

    def search(self, question: str, *, client_id: str, as_of: date, max_tier: str = "T1",
               k: int = 4, per_part: int = 2) -> list[PolicyCandidate]:
        doc = self.document
        if doc.effective_date is not None and doc.effective_date > as_of:
            return []  # the only policy is not in force yet on this date
        sections = {s.path: s for s in doc.sections}
        chosen: list[str] = []
        for part in split_question(question):
            topic = _topic_tokens(part)
            if not topic:
                continue
            expanded = part + " " + " ".join(sorted(topic))
            ranked = []
            for rank, hit in enumerate(self._index.search(expanded, max_tier=max_tier, k=10)):
                own_heading = set(_tokens(hit.section.split(" > ")[-1])) - GENERIC_TOKENS
                coverage = len(topic & own_heading) / max(1, len(own_heading))
                body_hits = len(topic & set(_tokens(hit.text)))
                if coverage or body_hits:
                    # how much of the section's OWN heading the question names, weighted against how
                    # many of its topic words the body contains; BM25 rank breaks ties
                    ranked.append((-(2 * coverage + body_hits), rank, hit.section))
            for *_, path in sorted(ranked)[:per_part]:
                if path not in chosen:
                    chosen.append(path)
        candidates: list[PolicyCandidate] = []
        for path in chosen[:k]:
            section = sections[path]
            if detect_injection(section.text):
                continue
            candidates.append(PolicyCandidate(
                ref=f"C{len(candidates) + 1}", text=section.text,
                citation=PolicyCitation(doc_id=doc.doc_id, title=doc.title, section=section.path, tier=doc.tier,
                                        version=doc.version, effective_date=doc.effective_date,
                                        pages=section.page_label or None)))
        return candidates
