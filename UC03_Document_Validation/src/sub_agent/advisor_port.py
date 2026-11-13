"""UC-03's optional model advisor: the port the core calls, and the privacy helpers it uses.

The core never imports a model library, an SDK or the shared gateway. A caller that wants model help injects an object implementing
`UC03AdvisorPort` (the adapter over the shared `LLMClient` lives in `integrations/gateway/`). Without one, every path is deterministic.

What a model may do here, and nothing more:
  * draft the wording of a reminder or replacement-request message for HR approval (UC-03 decides WHETHER to send one, never the model);
  * add an advisory note to a name variation that UC-03 has ALREADY routed to HR. It can never clear a mismatch or approve a name.

What a model never receives: the employee's name, an extracted name, document text, passport or ID numbers, dates of birth, or any other
document content. Messages are drafted with a placeholder that is replaced locally; names are reduced to anonymous token labels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

EMPLOYEE_PLACEHOLDER = "[EMPLOYEE_NAME]"  # the model writes this; UC-03 substitutes the real name locally, after the call
DEFAULT_TIMEOUT_S = 20.0  # the caller never waits longer than this for a model, whatever the gateway's own retries do
NameConcernLevel = Literal["none", "possible_mismatch", "uncertain"]
NAME_CONCERN_LEVELS = ("none", "possible_mismatch", "uncertain")


@dataclass(frozen=True)
class NameConcern:
    """An advisory opinion on a name variation. It never changes a finding."""

    level: NameConcernLevel
    note: str = ""


@runtime_checkable
class UC03AdvisorPort(Protocol):
    """Model help supplied from outside UC-03. Methods are synchronous and raise on any failure; the core applies the deadline and fallback."""

    provider: str

    def draft_message(self, *, action: str, document_items: list[str], findings: list[str]) -> str:
        """Return a message body that addresses the employee only as EMPLOYEE_PLACEHOLDER."""
        ...

    def name_concern(self, *, form_tokens: list[str], document_tokens: list[str]) -> NameConcern:
        """Return an advisory concern about two names, given ONLY anonymous token labels (see `pseudonymize_names`)."""
        ...


class AdvisorOutputRejected(ValueError):
    """The advisor answered, but the answer failed UC-03's checks. The deterministic path is used instead."""


# --------------------------------------------------------------------------- names
_SPLIT = re.compile(r"[\s,.\-'’/]+")


def _tokens(name: str) -> list[str]:
    return [t for t in _SPLIT.split(name.casefold()) if t]


def pseudonymize_names(form_name: str, document_name: str) -> tuple[list[str], list[str]]:
    """Replace every name part by an anonymous label, consistently across both names.

    Multi-letter parts become T1, T2, ... in order of first appearance; the same part gets the same label in both names. A one-letter part
    becomes INITIAL_OF_Tn when it is the first letter of part Tn, otherwise INITIAL_UNMATCHED. No letter of either name is returned.
    """
    form, doc = _tokens(form_name), _tokens(document_name)
    labels: dict[str, str] = {}
    for t in form + doc:
        if len(t) > 1 and t not in labels:
            labels[t] = f"T{len(labels) + 1}"

    def label(t: str) -> str:
        if len(t) > 1:
            return labels[t]
        for full, lab in labels.items():
            if full[0] == t:
                return f"INITIAL_OF_{lab}"
        return "INITIAL_UNMATCHED"

    return [label(t) for t in form], [label(t) for t in doc]


def structural_hint(form_name: str, document_name: str) -> str:
    """A deterministic, local description of how two names differ, without repeating either name (for the HR reviewer and the audit)."""
    form_labels, doc_labels = pseudonymize_names(form_name, document_name)
    full_form = [x for x in form_labels if x.startswith("T")]
    full_doc = [x for x in doc_labels if x.startswith("T")]
    shared = [x for x in full_form if x in full_doc]
    extra_doc = len([x for x in full_doc if x not in full_form])
    extra_form = len([x for x in full_form if x not in full_doc])
    initials = len([x for x in form_labels + doc_labels if x.startswith("INITIAL_OF_")])
    parts = [f"form has {len(form_labels)} name part(s), document has {len(doc_labels)}", f"{len(shared)} shared"]
    if extra_doc:
        parts.append(f"document adds {extra_doc}")
    if extra_form:
        parts.append(f"form adds {extra_form}")
    if shared and [x for x in full_form if x in shared] != [x for x in full_doc if x in shared]:
        parts.append("order differs")
    if initials:
        parts.append(f"{initials} initial(s) match a full name part")
    return "Structure: " + "; ".join(parts) + "."


# --------------------------------------------------------------------------- advisor output checks
_NOTE_ALLOWED = re.compile(r"[^A-Za-z0-9 ,.;:()'\-_/]")


def clean_note(note: object, *, max_len: int = 200) -> str:
    """Advisor notes are shown to HR and stored in the audit trail: plain short text only, no markup, no long numbers."""
    text = " ".join(str(note or "").split())
    text = _NOTE_ALLOWED.sub("", text)
    text = re.sub(r"\d{4,}", "[number]", text)  # never carry anything that looks like an ID or document number
    return text[:max_len].strip()
