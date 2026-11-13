"""The authoritative HR policy document (HR_Policy_Consultants.md), loaded as structured knowledge.

Source and path resolution
--------------------------
By default the document is read from THIS package (importlib.resources), so it ships inside the
hr-onboarding-common wheel and works on any machine. `HR_POLICY_SOURCE_PATH` may point at another
copy (e.g. a reviewed newer version mounted on a server). No absolute path is hard-coded, and there
is no fallback: a missing, unreadable or empty document raises PolicySourceUnavailableError -- callers
must refuse to answer, never answer from anything else.

Ingestion preserves the document
--------------------------------
The Markdown is split into sections at its `##` and `###` headings; nothing in a section's text is
rewritten -- wording, lists, tables and emphasis are kept verbatim. Only the `<!-- Page N -->`
markers are removed from the text, and turned into each section's page reference. A `###`
subsection carries its parent heading as context ("Remunerations and Reimbursements > Payroll").
The table of contents is navigation, not policy, and is not indexed.

Versioning
----------
The document carries no version or effective date. `version` is therefore the first 12 hex digits
of the SHA-256 of its exact bytes: any edit changes it, and every citation records it. If the
document ever gains YAML front matter with `version` / `effective_date`, those are used instead.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from importlib import resources
from pathlib import Path

import yaml

from hr_onboarding_common.context_layer.knowledge import KnowledgeIndex

PACKAGE_RESOURCE = "HR_Policy_Consultants.md"
SOURCE_ENV = "HR_POLICY_SOURCE_PATH"
DOC_ID = "hr-policy-consultants"
TIER = "T1"

_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_PAGE = re.compile(r"<!--\s*Page\s+(\d+)\s*-->", re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_SKIP_SECTIONS = {"table of contents"}


class PolicySourceUnavailableError(RuntimeError):
    """The HR policy document cannot be loaded. Nothing may be answered from any other source."""


@dataclass(frozen=True)
class PolicySection:
    heading: str            # the section's own heading, verbatim
    path: str               # "Parent > Heading" for a ### subsection, else the heading
    anchor: str             # the Markdown anchor (as used by the document's own table of contents)
    pages: tuple[int, ...]  # source PDF pages the section spans
    text: str               # the section body, verbatim except page markers

    @property
    def page_label(self) -> str:
        if not self.pages:
            return ""
        lo, hi = min(self.pages), max(self.pages)
        return f"p. {lo}" if lo == hi else f"pp. {lo}–{hi}"


@dataclass(frozen=True)
class PolicyDocument:
    doc_id: str
    title: str
    version: str
    effective_date: date | None
    source: str  # "package:<name>" or the configured path's file name -- never a full local path
    sections: tuple[PolicySection, ...] = field(default_factory=tuple)

    def section(self, path_or_heading: str) -> PolicySection | None:
        key = path_or_heading.strip().lower()
        return next((s for s in self.sections if s.path.lower() == key or s.heading.lower() == key), None)

    def index(self) -> KnowledgeIndex:
        return KnowledgeIndex.from_sections(self.doc_id, self.title, self.tier,
                                            [(s.path, s.text) for s in self.sections])

    tier: str = TIER


def _anchor(heading: str) -> str:
    return re.sub(r"[^\w\s-]", "", heading.lower()).strip().replace(" ", "-")


def parse_policy_markdown(raw: str, *, source: str) -> PolicyDocument:
    """Parses the document text. Raises PolicySourceUnavailableError if it holds no policy sections."""
    meta: dict = {}
    body = raw
    if raw.startswith("---"):
        try:
            _, front, body = raw.split("---", 2)
            meta = yaml.safe_load(front) or {}
        except (ValueError, yaml.YAMLError):
            raise PolicySourceUnavailableError("the HR policy document's front matter is malformed") from None
    title, parent, page = "", "", 1
    sections: list[PolicySection] = []
    current: dict | None = None

    def close() -> None:
        if current is None:
            return
        text = _COMMENT.sub("", "\n".join(current["lines"]))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if current["heading"].lower() not in _SKIP_SECTIONS and text:
            sections.append(PolicySection(heading=current["heading"], path=current["path"],
                                          anchor=_anchor(current["heading"]), pages=tuple(sorted(current["pages"])),
                                          text=text))

    for line in body.splitlines():
        for m in _PAGE.finditer(line):
            page = int(m.group(1))
        if current is not None and _COMMENT.sub("", line).strip() and not _HEADING.match(line):
            current["pages"].add(page)  # a page counts only once policy text appears on it
        heading = _HEADING.match(line)
        if heading:
            level, name = len(heading.group(1)), heading.group(2).strip()
            if level == 1:
                close()
                current, title = None, name
                continue
            close()
            if level == 2:
                parent = name
            path = f"{parent} > {name}" if level == 3 and parent else name
            current = {"heading": name, "path": path, "pages": {page}, "lines": []}
            continue
        if current is not None:
            current["lines"].append(line)
    close()
    if not sections:
        raise PolicySourceUnavailableError("the HR policy document contains no policy sections")
    effective = meta.get("effective_date")
    if isinstance(effective, str):
        effective = date.fromisoformat(effective)
    version = str(meta["version"]) if meta.get("version") else hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return PolicyDocument(doc_id=str(meta.get("doc_id") or DOC_ID), title=str(meta.get("title") or title or "HR Policies"),
                          version=version, effective_date=effective, source=source, sections=tuple(sections))


def _read(path_override: str | None) -> tuple[str, str]:
    try:
        if path_override:
            path = Path(path_override)
            return path.read_text(encoding="utf-8"), path.name
        resource = resources.files("hr_onboarding_common.context_layer").joinpath(PACKAGE_RESOURCE)
        return resource.read_text(encoding="utf-8"), f"package:{PACKAGE_RESOURCE}"
    except (OSError, UnicodeDecodeError) as exc:
        raise PolicySourceUnavailableError(
            f"the HR policy document could not be read ({type(exc).__name__}); set {SOURCE_ENV} or reinstall "
            f"hr-onboarding-common") from None


def load_policy_document(path: str | Path | None = None) -> PolicyDocument:
    """The authoritative HR policy: `path`, else $HR_POLICY_SOURCE_PATH, else the packaged copy.
    Never cached across a different source; use `cached_policy_document` for the shared instance."""
    override = str(path) if path is not None else (os.environ.get(SOURCE_ENV) or None)
    raw, source = _read(override)
    if not raw.strip():
        raise PolicySourceUnavailableError("the HR policy document is empty")
    return parse_policy_markdown(raw, source=source)


@lru_cache(maxsize=4)
def cached_policy_document(path: str | None = None) -> PolicyDocument:
    """Process-wide copy. Reload after a policy update with `cached_policy_document.cache_clear()`
    (or restart the service)."""
    return load_policy_document(path)
