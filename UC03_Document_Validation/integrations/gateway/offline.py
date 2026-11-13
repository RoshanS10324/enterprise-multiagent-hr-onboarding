"""A deterministic offline `LLMClient` for UC-03's two gateway tasks.

It is the shared package's `FakeLLM` with one handler per task, so tests and local demos run the
real `GatewayAdvisor` path with no network and no key. It writes text from the codes and labels it
is given, never from names or document contents (it is never given any).
"""

from __future__ import annotations

from typing import Any

from .advisor import DRAFT_TASK, NAME_TASK


def offline_llm(settings: Any = None) -> Any:
    from hr_onboarding_common.llm import FakeLLM, extract_input_block

    def draft(system: str, user: str, schema: Any) -> Any:
        data = extract_input_block(user)
        items = ", ".join(data.get("outstanding_items", [])) or "the outstanding documents"
        greeting = f"Dear {data.get('employee', '[EMPLOYEE_NAME]')},"
        body = f"{greeting}\n\nPlease provide: {items}.\n\nKind regards,\nHR Onboarding Team"
        return schema(body=body)

    def name(system: str, user: str, schema: Any) -> Any:
        data = extract_input_block(user)
        form = set(data.get("form_name_parts", []))
        doc = set(data.get("document_name_parts", []))
        shared = {t for t in form if t.startswith("T")} & {t for t in doc if t.startswith("T")}
        level = "uncertain" if shared else "possible_mismatch"
        return schema(level=level, note=f"{len(shared)} shared name part(s).")

    return FakeLLM(settings).on(DRAFT_TASK, draft).on(NAME_TASK, name)
