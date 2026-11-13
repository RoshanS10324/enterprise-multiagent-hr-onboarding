"""Communication drafting for UC-03 Document & Information Validation.

UC-03 decides WHETHER a reminder or replacement request is needed (deterministic rules in validator/rules). This module only words it, and
every message is a DRAFT for HR approval; nothing is sent.

Wording sources, in order:
  1. an optional injected advisor (`UC03AdvisorPort`, e.g. the shared Enterprise HR Agent LLM gateway through `integrations/gateway/`). The advisor never
     receives the employee's name: it writes EMPLOYEE_PLACEHOLDER and UC-03 substitutes the real name locally, after the call. Its output
     is checked (placeholder present, no sensitive category, length) and the HR-approval footer is enforced;
  2. the deterministic compliant templates below (the fallback whenever there is no advisor, or it fails, times out or is rejected).

No provider SDK is imported and no API key is read here: model access is the shared gateway's job.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from src.sub_agent.advisor_port import (
    DEFAULT_TIMEOUT_S,
    EMPLOYEE_PLACEHOLDER,
    AdvisorOutputRejected,
    UC03AdvisorPort,
)

log = logging.getLogger("uc03.comms")

_CANDIDATE_PROMPT_PATHS = [
    Path(__file__).resolve().parents[2] / "brain" / "onboarding" / "uc03_communication.md",
    Path(__file__).resolve().parents[3] / "brain" / "onboarding" / "uc03_communication.md",
    Path(__file__).parent.parent.parent.parent.parent / "brain" / "onboarding" / "uc03_communication.md",
]
_PROMPT_PATH = next((p for p in _CANDIDATE_PROMPT_PATHS if p.exists()), _CANDIDATE_PROMPT_PATHS[0])

# Prohibited sensitive keywords per Rule 3 in brain/onboarding/uc03_communication.md
_SENSITIVE_CATEGORIES = {
    "medical", "health", "disability", "criminal", "police", "conviction",
    "salary", "compensation", "bank_account", "financial"
}
HR_FOOTER = "This draft is for HR review only and must be approved before being sent."
MAX_DRAFT_CHARS = 4000

_REMINDER_TEMPLATE = """\
Dear {employee_name},

We hope this message finds you well.

As part of your onboarding process, we require the following document(s) to be submitted as soon as possible:

{document_list}

Please upload via the onboarding portal or respond to this email at your earliest convenience.

If you have any questions, please contact your HR representative.

Kind regards,
[HR Team]

---
This draft is for HR review only and must be approved before being sent."""

_REPLACEMENT_TEMPLATE = """\
Dear {employee_name},

We hope this message finds you well.

During the verification of your onboarding documents, we noticed that the following document(s) require replacement or re-submission:

{document_list}
{reasons_block}
Please upload the updated document(s) via the onboarding portal or respond to this email at your earliest convenience.

If you have any questions, please contact your HR representative.

Kind regards,
[HR Team]

---
This draft is for HR review only and must be approved before being sent."""


@dataclass(frozen=True)
class DraftOutcome:
    """The draft text plus where it came from. `degraded_reason` is an error class name only, never provider text."""

    text: str
    source: str  # "advisor" or "template"
    provider: str | None = None
    degraded_reason: str | None = None


def _sanitize_items(items: list[str]) -> list[str]:
    """Sanitize item labels per Rule 3 (no sensitive categories)."""
    sanitized: list[str] = []
    for item in items:
        lower = item.lower()
        if any(cat in lower for cat in _SENSITIVE_CATEGORIES):
            sanitized.append("an HR item that requires your attention")
        else:
            sanitized.append(item)
    return sanitized


def system_prompt() -> str:
    """The drafting guidance for an advisor, plus UC-03's privacy rule for the placeholder."""
    base = _PROMPT_PATH.read_text(encoding="utf-8") if _PROMPT_PATH.exists() else ""
    return (f"{base}\n\nPrivacy rule: you are not given the employee's name. Address the employee only as {EMPLOYEE_PLACEHOLDER}, "
            f"exactly as written. Do not invent names, dates, numbers or document details. Return only the email body. "
            f"End with: {HR_FOOTER}")


async def draft_communication_outcome(
    action: str,
    employee_name: str,
    document_label: str,
    outstanding_labels: list[str],
    reasons: list[str] | None = None,
    advisor: UC03AdvisorPort | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> DraftOutcome:
    """Return a draft reminder or replacement request and its source. Never raises because of the advisor."""
    raw_items = outstanding_labels if outstanding_labels else [document_label]
    items = _sanitize_items(raw_items)
    findings = list(reasons or [])
    if advisor is None:
        return DraftOutcome(_template_draft(action, employee_name, items, reasons), "template")
    provider = getattr(advisor, "provider", None)
    try:
        body = await asyncio.wait_for(
            asyncio.to_thread(advisor.draft_message, action=action, document_items=items, findings=findings), timeout_s)
        return DraftOutcome(_accept(body, employee_name), "advisor", provider)
    except Exception as exc:  # noqa: BLE001 - any advisor failure falls back to the compliant template
        log.warning("communication drafting fell back to the template (%s)", type(exc).__name__)  # class name only, never provider text
        return DraftOutcome(_template_draft(action, employee_name, items, reasons), "template", provider, type(exc).__name__)


async def draft_communication(
    action: str,
    employee_name: str,
    document_label: str,
    outstanding_labels: list[str],
    reasons: list[str] | None = None,
    advisor: UC03AdvisorPort | None = None,
) -> str:
    """Return a draft reminder or replacement request (text only; kept for existing callers)."""
    return (await draft_communication_outcome(action, employee_name, document_label, outstanding_labels, reasons, advisor)).text


def _accept(body: object, employee_name: str) -> str:
    """Check an advisor's draft before it is shown to HR, then put the real name in locally."""
    if not isinstance(body, str) or not body.strip():
        raise AdvisorOutputRejected("empty draft")
    text = body.strip()
    if len(text) > MAX_DRAFT_CHARS:
        raise AdvisorOutputRejected("draft too long")
    if EMPLOYEE_PLACEHOLDER not in text:
        raise AdvisorOutputRejected("placeholder missing")  # the advisor may have invented a name
    lower = text.lower()
    if any(cat in lower for cat in _SENSITIVE_CATEGORIES):
        raise AdvisorOutputRejected("sensitive category in draft")
    if HR_FOOTER not in text:
        text = f"{text}\n\n---\n{HR_FOOTER}"
    return text.replace(EMPLOYEE_PLACEHOLDER, employee_name)


def _template_draft(
    action: str,
    employee_name: str,
    items: list[str],
    reasons: list[str] | None = None,
) -> str:
    """Generate compliant deterministic draft based on prompt specification."""
    doc_list = "\n".join(f"  - {item}" for item in items)
    if action in ("SEND_REPLACEMENT_REQUEST", "DOCUMENT_REJECTED"):
        reasons_block = ""
        if reasons:
            reasons_block = f"Issues noted: {', '.join(reasons)}.\n\n"
        return _REPLACEMENT_TEMPLATE.format(
            employee_name=employee_name,
            document_list=doc_list,
            reasons_block=reasons_block,
        )
    return _REMINDER_TEMPLATE.format(employee_name=employee_name, document_list=doc_list)
