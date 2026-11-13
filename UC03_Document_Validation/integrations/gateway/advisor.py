"""`UC03AdvisorPort` over the shared Enterprise HR Agent LLM gateway (`hr_onboarding_common.llm.LLMClient`).

The UC-03 core never imports this module, a model library or the shared package: it only knows
`src.sub_agent.advisor_port.UC03AdvisorPort`. A caller that wants model help (the orchestrator,
with its `AgentContext.llm`) builds a `GatewayAdvisor` over its shared client and passes it to
`build_uc03_graph(..., advisor=...)` or `DocumentValidationService(..., advisor=...)`.

Every call is one `llm.structured(task=..., tier=..., schema=...)`. UC-03 names no model,
provider, route or key: the shared gateway owns model choice per tier, retries, fallbacks and
redaction of provider text.

What the model receives (built by the core, checked again here):
  * message drafting: the action code, the document LABELS still outstanding and the finding
    codes. Never the employee's name (the draft uses EMPLOYEE_PLACEHOLDER, replaced locally after
    the call), never document contents.
  * name concern: anonymous token labels only (T1, T2, INITIAL_OF_T1, ...). No letter of a name.

Failures propagate unchanged (`LLMError`, `LLMRefusal`, validation errors). The core applies the
deadline, records only the error class name and falls back to its deterministic path. A model
can never change a finding, a status or an approval.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.sub_agent.advisor_port import EMPLOYEE_PLACEHOLDER, NameConcern

# Task names from HR-Onboarding-Common/docs/UC_LLM_GATEWAY_MIGRATION_GUIDE.md, section 3.
DRAFT_TASK = "uc03.draft_communication"
NAME_TASK = "uc03.name_check"
DRAFT_MAX_TOKENS = 600
NAME_MAX_TOKENS = 200
_LABEL = re.compile(r"^(T\d{1,3}|INITIAL_OF_T\d{1,3}|INITIAL_UNMATCHED)$")
_CODE = re.compile(r"^[A-Za-z0-9 _/().,-]{1,80}$")

NAME_SYSTEM = (
    "You review how two personal names differ, for an HR onboarding check. You are NOT given "
    "the names: each name part has been replaced by an anonymous label. The same label in both "
    "lists is the same part; INITIAL_OF_Tn is an initial matching part Tn; INITIAL_UNMATCHED is "
    "an initial matching no part. The deterministic check has ALREADY routed this case to an HR "
    "reviewer; you cannot approve or clear it. Say only whether the structure suggests a "
    "possible mismatch, is uncertain, or shows no particular concern, in one short neutral note. "
    "Never guess names, nationalities, or anything not in the labels."
)


class DraftReply(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class NameConcernReply(BaseModel):
    level: Literal["none", "possible_mismatch", "uncertain"]
    note: str = Field(default="", max_length=300)


def _checked_codes(values: list[str], what: str) -> list[str]:
    """Second line of defence: only short labels or codes may leave UC-03."""
    out = [str(v) for v in values]
    bad = [v for v in out if not _CODE.match(v)]
    if bad:
        raise ValueError(f"{what}: {len(bad)} value(s) are not short labels; not sent to a model")
    return out


class GatewayAdvisor:
    """The shared gateway as UC-03's advisor. `llm` is any shared `LLMClient`."""

    def __init__(self, llm: Any, *, system_prompt: str | None = None) -> None:
        # lazy: UC-03 itself never needs the shared package
        from hr_onboarding_common.llm import ModelTier

        self._llm = llm
        self._draft_tier = ModelTier.WORKHORSE  # drafting
        self._name_tier = ModelTier.FAST  # classification
        self._system_prompt = system_prompt
        self.provider = "shared-gateway"
        try:
            # a model id from the shared configuration, never a key
            self.model: str | None = str(llm.model_for(self._draft_tier))
        except Exception:  # noqa: BLE001 - a client without model_for still works
            self.model = None

    def draft_message(self, *, action: str, document_items: list[str], findings: list[str]) -> str:
        from hr_onboarding_common.llm import input_block

        if self._system_prompt is None:
            # the drafting guidance plus UC-03's placeholder rule
            from src.sub_agent.comms import system_prompt

            self._system_prompt = system_prompt()
        payload = {
            "action": _checked_codes([action], "action")[0],
            "employee": EMPLOYEE_PLACEHOLDER,
            "outstanding_items": _checked_codes(document_items, "document_items"),
            "findings": _checked_codes(findings, "findings"),
        }
        reply = self._llm.structured(
            task=DRAFT_TASK,
            tier=self._draft_tier,
            system=self._system_prompt,
            user=input_block(payload),
            schema=DraftReply,
            max_tokens=DRAFT_MAX_TOKENS,
        )
        return str(reply.body)

    def name_concern(self, *, form_tokens: list[str], document_tokens: list[str]) -> NameConcern:
        from hr_onboarding_common.llm import input_block

        tokens = [str(t) for t in form_tokens + document_tokens]
        if any(not _LABEL.match(t) for t in tokens):
            raise ValueError("name tokens must be anonymous labels only; not sent to a model")
        payload = {
            "form_name_parts": list(form_tokens),
            "document_name_parts": list(document_tokens),
        }
        reply = self._llm.structured(
            task=NAME_TASK,
            tier=self._name_tier,
            system=NAME_SYSTEM,
            user=input_block(payload),
            schema=NameConcernReply,
            max_tokens=NAME_MAX_TOKENS,
        )
        return NameConcern(level=reply.level, note=reply.note)
