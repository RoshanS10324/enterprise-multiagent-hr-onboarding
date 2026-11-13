from __future__ import annotations

import asyncio
from dataclasses import dataclass

from src.state.enums import ValidationFinding
from src.sub_agent.advisor_port import (
    DEFAULT_TIMEOUT_S,
    NAME_CONCERN_LEVELS,
    AdvisorOutputRejected,
    NameConcern,
    UC03AdvisorPort,
    clean_note,
    pseudonymize_names,
    structural_hint,
)

# BR-06/BR-07. Deterministic rules decide; a model may only add a note to a variation that is already routed to HR.
#   - no extracted name, or extraction confidence below 0.7  -> LOW_CONFIDENCE (HR)
#   - exact match (case-insensitive)                          -> consistent
#   - any other difference                                    -> NAME_MISMATCH (HR verifies father names, initials, ordering, transliteration)
# The model never sees either name (only anonymous token labels) and can never clear a mismatch.
_ROUTED = "Names differ; the variation could not be verified automatically and is routed to HR."


@dataclass
class NameCheckResult:
    is_consistent: bool
    confidence: float
    reasoning: str
    finding: ValidationFinding | None
    used_llm: bool


async def check_name_consistency_agent(
    name_from_form: str,
    extracted_name: str | None,
    confidence: float,
    advisor: UC03AdvisorPort | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> NameCheckResult:
    """BR-06/BR-07 name consistency. Deterministic; an optional advisor only annotates a variation that already goes to HR."""
    if extracted_name is None:
        return NameCheckResult(
            is_consistent=False,
            confidence=0.0,
            reasoning="No name extracted from document.",
            finding=ValidationFinding.LOW_CONFIDENCE,
            used_llm=False,
        )

    if confidence < 0.7:
        return NameCheckResult(
            is_consistent=False,
            confidence=confidence,
            reasoning="Extraction confidence too low to verify name.",
            finding=ValidationFinding.LOW_CONFIDENCE,
            used_llm=False,
        )

    form, doc = name_from_form.strip(), extracted_name.strip()
    if form.lower() == doc.lower():
        return NameCheckResult(is_consistent=True, confidence=1.0, reasoning="Exact match.", finding=None, used_llm=False)

    # Every other variation goes to HR. This is fixed before any model is consulted, and nothing below can change it.
    result = NameCheckResult(
        is_consistent=False,
        confidence=0.0,
        reasoning=f"{_ROUTED} {structural_hint(form, doc)}",
        finding=ValidationFinding.NAME_MISMATCH,
        used_llm=False,
    )
    if advisor is None:
        return result
    try:
        concern = await _ask(advisor, form, doc, timeout_s)
    except Exception as exc:  # noqa: BLE001 - any advisor failure leaves the deterministic result untouched
        result.reasoning += f" Model note unavailable ({type(exc).__name__})."
        return result
    note = f": {concern.note}" if concern.note else ""
    result.reasoning += f" Model note (advisory only, does not change the finding) - {concern.level}{note}."
    result.used_llm = True
    return result


async def _ask(advisor: UC03AdvisorPort, form: str, doc: str, timeout_s: float) -> NameConcern:
    form_tokens, doc_tokens = pseudonymize_names(form, doc)  # anonymous labels only; no letter of either name leaves UC-03
    raw = await asyncio.wait_for(asyncio.to_thread(advisor.name_concern, form_tokens=form_tokens, document_tokens=doc_tokens), timeout_s)
    level = getattr(raw, "level", None)
    if level not in NAME_CONCERN_LEVELS:
        raise AdvisorOutputRejected("unknown concern level")
    return NameConcern(level=level, note=clean_note(getattr(raw, "note", "")))
