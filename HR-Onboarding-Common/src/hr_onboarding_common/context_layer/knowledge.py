"""Knowledge index over static docs with tier filtering and BM25 retrieval.

In-process keyword retrieval (Arabic-normalised). The interface (`search`) stays the same if it is
ever swapped for a hosted hybrid search + reranker.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

from hr_onboarding_common.contracts import BrainContext
from hr_onboarding_common.platform.guardrails import normalize_text

_TOKEN = re.compile(r"[\w؀-ۿ]+")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "is", "are", "we", "our", "with",
         "be", "by", "as", "at", "it", "this", "that", "من", "في", "على", "و", "الى", "عن"}

TIER_ORDER = ["T0", "T1", "T2", "T3", "T4"]  # T5 is never ingested


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(normalize_text(text)) if t not in _STOP and len(t) > 1]


@dataclass
class Chunk:
    doc_id: str
    title: str
    section: str
    text: str
    tier: str
    tokens: list[str]


class KnowledgeIndex:
    def __init__(self, static_dir: Path):
        self.chunks: list[Chunk] = []
        for path in sorted(static_dir.rglob("*.md")):
            raw = path.read_text(encoding="utf-8")
            _, fm, body = raw.split("---", 2)
            meta = yaml.safe_load(fm)
            if meta.get("tier") == "T5":
                raise ValueError(f"{path}: T5 content must never be ingested")
            for section, text in self._sections(body):
                self.chunks.append(Chunk(meta["doc_id"], meta["title"], section, text, meta["tier"],
                                         _tokens(section + " " + text)))
        self._df = Counter(t for c in self.chunks for t in set(c.tokens))
        self._avg = sum(len(c.tokens) for c in self.chunks) / max(1, len(self.chunks))

    @classmethod
    def from_sections(cls, doc_id: str, title: str, tier: str,
                      sections: list[tuple[str, str]]) -> "KnowledgeIndex":
        """An index over sections already parsed by the caller (e.g. a document without YAML front
        matter or with `##`/`###` headings, such as hr_policy.PolicyDocument). Same BM25 scoring and
        tier filter as the directory constructor; T5 is refused here too."""
        if tier == "T5":
            raise ValueError(f"{doc_id}: T5 content must never be ingested")
        index = cls.__new__(cls)
        index.chunks = [Chunk(doc_id, title, section, text, tier, _tokens(section + " " + text))
                        for section, text in sections]
        index._df = Counter(t for c in index.chunks for t in set(c.tokens))
        index._avg = sum(len(c.tokens) for c in index.chunks) / max(1, len(index.chunks))
        return index

    @staticmethod
    def _sections(body: str) -> list[tuple[str, str]]:
        parts = re.split(r"^#\s+(.+)$", body, flags=re.M)
        return [(parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)]

    def search(self, query: str, *, max_tier: str = "T1", k: int = 3) -> list[BrainContext]:
        allowed = set(TIER_ORDER[: TIER_ORDER.index(max_tier) + 1])
        q = _tokens(query)
        n = len(self.chunks)
        scored = []
        for c in self.chunks:
            if c.tier not in allowed:  # access filter BEFORE scoring
                continue
            tf = Counter(c.tokens)
            score = 0.0
            for t in q:
                if t not in tf:
                    continue
                idf = math.log(1 + (n - self._df[t] + 0.5) / (self._df[t] + 0.5))
                score += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(c.tokens) / self._avg))
            if score > 0:
                scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        return [BrainContext(doc_id=c.doc_id, section=c.section, text=c.text, tier=c.tier) for _, c in scored[:k]]

    def get_section(self, doc_id: str, section: str) -> BrainContext | None:
        for c in self.chunks:
            if c.doc_id == doc_id and c.section.lower() == section.lower():
                return BrainContext(doc_id=c.doc_id, section=c.section, text=c.text, tier=c.tier)
        return None
