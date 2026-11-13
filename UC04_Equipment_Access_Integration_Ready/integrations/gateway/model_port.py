"""UC-04's `ModelPort` over the shared Enterprise HR Agent LLM gateway (`hr_onboarding_common.llm.LLMClient`).

The pattern is the one UC05 uses on the `Prasanna` branch: the shared client is built once, by `hr_onboarding_common.llm.build_llm`, from the
central configuration, and every model call is `llm.structured(task=..., tier=..., schema=...)`. UC-04 adds nothing to that: no model id, no
provider name, no key, no retry logic, no provider SDK. Provider routing, reasoning options, retries, fallbacks, validation and redaction of
provider text all live in the gateway.

This file lives in `integrations/` because the UC-04 core (`src/`) must not depend on a model layer (tests/test_packaging.py). The core only
knows `sub_agent.advisory_providers.ModelPort`; this class is injected into it.

Boundary: the model returns one validated text field. `LlmAdvisor` puts it into `narrative` behind a banner after the privacy guard. It cannot
reach a status, an approval, a ticket or an entitlement, and it never sees item notes, names or other free text (`facts_for` supplies codes, states
and counts only).
"""

from __future__ import annotations

import threading
from typing import Any

from pydantic import BaseModel, Field


class AdvisoryTimeout(TimeoutError):
    """The shared gateway did not answer within UC-04's advisory deadline. The advisory degrades; no decision is affected."""


TASK = "uc04.advisory"  # stable name: appears in gateway logs and traces and selects the FakeLLM handler. From the migration guide, section 4.
MAX_TOKENS = 600  # for the answer only; the gateway adds its own reasoning headroom
MAX_NARRATIVE_CHARS = 1500  # same limit the advisor enforces


class AdvisoryNarrative(BaseModel):
    """The only thing the model may return. Anything else, or text over the limit, fails validation inside the gateway."""

    narrative: str = Field(min_length=1, max_length=MAX_NARRATIVE_CHARS)


def _facts(user: str) -> dict[str, Any]:
    """`LlmAdvisor` sends `INPUT` followed by `key: value` lines (codes, states, counts). Pass them to the gateway as a structured input block."""
    facts: dict[str, str] = {}
    for line in user.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key.isidentifier():
            facts[key] = value
    return {"facts": facts} if facts else {"text": user}


class GatewayModelPort:
    """`ModelPort` for `LlmAdvisor`: `complete(...)` is one `structured` call on the shared gateway.

    Any `LLMError` (every model failed after retries and fallbacks), `LLMRefusal` (the model declined) or validation failure propagates. The
    advisor records only its class name and the deterministic advisory continues; nothing is retried here and no provider text is kept.

    Deadline: `timeout_s` (the advisor's, 20 s by default) is a hard upper bound on how long the caller waits, on top of the gateway's own
    `LLM_TIMEOUT_S` and retries, which stay centrally configured. The gateway can take far longer than a UC-04 worker is allowed to (the
    orchestrator kills a worker that does not answer within its call timeout, 30 s by default, and treats the outcome as unknown). So the call
    runs on a daemon thread and, if it has not answered in time, `AdvisoryTimeout` is raised and the advisory degrades. The gateway call may
    still finish in the background; its answer is discarded.
    """

    def __init__(self, llm: Any, *, max_tokens: int = MAX_TOKENS) -> None:
        from hr_onboarding_common.llm import ModelTier  # lazy: importing this module needs no gateway until it is used

        self._llm = llm
        self._tier = ModelTier.WORKHORSE  # explanatory text: the drafting tier, as UC05 uses for its own narrative tasks
        self._max_tokens = max_tokens
        try:
            self.name = str(llm.model_for(self._tier))  # the configured model id (never a key); shown on the report
        except Exception:  # noqa: BLE001 - a client without model_for still works; the report just names the gateway
            self.name = "shared-gateway"

    def complete(self, *, system: str, user: str, timeout_s: float) -> str:
        from hr_onboarding_common.llm import input_block

        block = input_block(_facts(user))
        box: dict[str, Any] = {}

        def call() -> None:
            try:
                box["out"] = self._llm.structured(task=TASK, tier=self._tier, system=system, user=block,
                                                  schema=AdvisoryNarrative, max_tokens=self._max_tokens)
            except BaseException as exc:  # noqa: BLE001 - handed back to the caller's thread unchanged
                box["error"] = exc

        worker = threading.Thread(target=call, name="uc04-advisory", daemon=True)
        worker.start()
        worker.join(timeout_s)
        if worker.is_alive():
            raise AdvisoryTimeout("the shared gateway did not answer within the advisory deadline")
        if "error" in box:
            raise box["error"]
        return box["out"].narrative
