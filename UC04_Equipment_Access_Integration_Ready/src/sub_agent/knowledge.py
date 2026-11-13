"""UC-04 Brain Knowledge loader and retriever.

Loads structured domain knowledge from `brain/onboarding/` and exposes
traceable retrieval methods for the sub-agent and Fake LLM advisor.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_BRAIN_DIR = Path(__file__).resolve().parents[2] / "brain" / "onboarding"


class KnowledgeSection(BaseModel):
    """A structured section of brain knowledge."""

    title: str
    level: int
    content: str
    subsections: list[KnowledgeSection] = Field(default_factory=list)


class BrainKnowledgeDocument(BaseModel):
    """A complete brain knowledge document parsed from Markdown."""

    doc_id: str
    title: str
    source: str
    sections: dict[str, str] = Field(default_factory=dict)
    raw_markdown: str


class BrainKnowledgeLoader:
    """Discovers, parses, and retrieves brain knowledge for UC-04."""

    def __init__(self, brain_dir: Path | str | None = None) -> None:
        self.brain_dir = Path(brain_dir) if brain_dir else DEFAULT_BRAIN_DIR
        self._documents: dict[str, BrainKnowledgeDocument] = {}
        self.load_all()

    def load_all(self) -> dict[str, BrainKnowledgeDocument]:
        """Load and parse all Markdown files in the brain directory."""
        if not self.brain_dir.exists():
            return {}

        for md_path in self.brain_dir.glob("*.md"):
            doc = self._parse_file(md_path)
            self._documents[doc.doc_id] = doc

        return self._documents

    def _parse_file(self, path: Path) -> BrainKnowledgeDocument:
        raw = path.read_text(encoding="utf-8")
        doc_id = path.stem

        # Extract title from first # header
        title_match = re.search(r"^#\s+(.+)$", raw, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else doc_id

        # Extract source metadata if present
        source_match = re.search(r">\s*\*\*Source\*\*:\s*`?([^`\n]+)`?", raw)
        source = source_match.group(1).strip() if source_match else str(path.name)

        # Split into sections by ## headers
        sections: dict[str, str] = {}
        header_pattern = re.compile(r"^##\s+([^\n]+)$", re.MULTILINE)
        splits = list(header_pattern.finditer(raw))

        for i, match in enumerate(splits):
            header_name = match.group(1).strip()
            # Clean numbered headers like "1. Overview and Purpose" -> "Overview and Purpose"
            clean_name = re.sub(r"^\d+\.\s*", "", header_name).lower()
            start_pos = match.end()
            end_pos = splits[i + 1].start() if i + 1 < len(splits) else len(raw)
            section_body = raw[start_pos:end_pos].strip()
            sections[clean_name] = section_body
            # Also store exact header string
            sections[header_name.lower()] = section_body

        return BrainKnowledgeDocument(
            doc_id=doc_id,
            title=title,
            source=source,
            sections=sections,
            raw_markdown=raw,
        )

    def get_document(self, doc_id: str = "uc04_equipment_access") -> BrainKnowledgeDocument | None:
        """Get a document by its ID."""
        return self._documents.get(doc_id)

    def get_section(self, section_name: str, doc_id: str = "uc04_equipment_access") -> str | None:
        """Retrieve a specific section body from a knowledge document."""
        doc = self.get_document(doc_id)
        if not doc:
            return None
        target = section_name.strip().lower()
        # Direct lookup or substring match
        if target in doc.sections:
            return doc.sections[target]
        for name, text in doc.sections.items():
            if target in name or name in target:
                return text
        return None

    def search(self, query: str, doc_id: str = "uc04_equipment_access") -> list[tuple[str, str]]:
        """Search sections matching query terms, returning list of (section_name, text)."""
        doc = self.get_document(doc_id)
        if not doc:
            return []
        q_lower = query.lower()
        results: list[tuple[str, str]] = []
        for name, text in doc.sections.items():
            if q_lower in name or q_lower in text.lower():
                results.append((name, text))
        return results

    def get_status_guidance(self, status: str) -> str:
        """Retrieve canonical guidance for a specific case status."""
        lifecycle = self.get_section("case lifecycle", "uc04_equipment_access")
        if not lifecycle:
            return f"Status {status}: No guidance recorded."

        pattern = re.compile(rf"`{re.escape(status)}`:\s*([^\n]+)")
        match = pattern.search(lifecycle)
        if match:
            return match.group(1).strip()
        return f"Status {status}: Standard UC-04 state."

    def get_risk_guidance(self, risk_code: str) -> str:
        """Retrieve guidance for a specific risk code."""
        risks_sec = self.get_section("risk and blocker detection", "uc04_equipment_access")
        if not risks_sec:
            return f"Risk {risk_code}: Standard risk resolution."

        # Support exact backtick match or substring code match
        clean_code = risk_code.strip()
        pattern = re.compile(rf"`[^`]*{re.escape(clean_code)}[^`]*`:\s*([\s\S]*?)(?=\n- `|\n##|\Z)")
        match = pattern.search(risks_sec)
        if match:
            # Sub-bullets (proposal, owner, next action) are joined into one line so the guidance reads cleanly inside a sentence.
            lines = (line.strip().lstrip("-").strip() for line in match.group(1).splitlines())
            return " ".join(line for line in lines if line)
        return f"Risk {risk_code}: Review with HR officer."
