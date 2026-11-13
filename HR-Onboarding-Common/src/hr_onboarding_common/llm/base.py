"""LLM abstractions shared by every agent. No provider code lives here.

Agents ask for a tier and a Pydantic schema and get a validated object back. The only production
implementation is `LiteLLMGateway` (gateway.py); `FakeLLM` runs everything offline.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Protocol, TypeVar

from pydantic import BaseModel

from hr_onboarding_common.config import Settings, get_settings
from hr_onboarding_common.llm.conversation import LLMConversation

T = TypeVar("T", bound=BaseModel)


class ModelTier(StrEnum):
    FRONTIER = "frontier"  # orchestration planning, decision briefs
    WORKHORSE = "workhorse"  # drafting, rubric assessment, briefs
    FAST = "fast"  # extraction, classification


class LLMError(RuntimeError):
    """Every model in the chain failed, or the failure is not recoverable."""


class LLMRefusal(LLMError):
    """The model declined on content grounds. Never retried on another model."""


class LLMOutputError(LLMError):
    """The model answered but the output did not validate against the schema."""


@dataclass
class LLMCallRecord:
    task: str
    tier: ModelTier
    model: str
    system_hash: str
    user: str


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def input_block(payload: dict) -> str:
    """Structured agent input, embedded in the user turn. Sorted keys keep it cache-stable."""
    return "<input>\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n</input>"


def extract_input_block(user: str) -> dict:
    m = re.search(r"<input>\n(.*?)\n</input>", user, re.DOTALL)
    return json.loads(m.group(1)) if m else {}


class LLMClient(Protocol):
    calls: list[LLMCallRecord]

    def model_for(self, tier: ModelTier) -> str: ...

    def structured(self, *, task: str, tier: ModelTier, system: str, user: str, schema: type[T],
                   max_tokens: int = 8000, conversation: LLMConversation | None = None,
                   scope: tuple[str, ...] | None = None) -> T:
        """One validated answer. With `conversation` (and its own `scope`), the earlier turns are sent
        too and this turn is added to it once it succeeds (llm/conversation.py)."""
        ...


class BaseLLM:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.calls: list[LLMCallRecord] = []

    def model_for(self, tier: ModelTier) -> str:
        return {ModelTier.FRONTIER: self.settings.model_frontier,
                ModelTier.WORKHORSE: self.settings.model_workhorse,
                ModelTier.FAST: self.settings.model_fast}[tier]

    def fallbacks_for(self, tier: ModelTier) -> tuple[str, ...]:
        return {ModelTier.FRONTIER: self.settings.fallbacks_frontier,
                ModelTier.WORKHORSE: self.settings.fallbacks_workhorse,
                ModelTier.FAST: self.settings.fallbacks_fast}[tier]

    def _record(self, task: str, tier: ModelTier, system: str, user: str) -> None:
        self.calls.append(LLMCallRecord(task, tier, self.model_for(tier), prompt_hash(system), user))


Handler = Callable[[str, str, type[BaseModel]], BaseModel]


class FakeLLM(BaseLLM):
    """Deterministic offline LLM. Register one handler per task name."""

    def __init__(self, settings: Settings | None = None):
        super().__init__(settings)
        self.handlers: dict[str, Handler] = {}

    def on(self, task: str, handler: Handler) -> "FakeLLM":
        self.handlers[task] = handler
        return self

    def structured(self, *, task: str, tier: ModelTier, system: str, user: str, schema: type[T],
                   max_tokens: int = 8000, conversation: LLMConversation | None = None,
                   scope: tuple[str, ...] | None = None) -> T:
        if conversation is not None:
            conversation.check_scope(scope)
        self._record(task, tier, system, user)
        if task not in self.handlers:
            raise LLMError(f"FakeLLM has no handler for task '{task}'")
        out = self.handlers[task](system, user, schema)
        result = schema.model_validate(out.model_dump() if isinstance(out, BaseModel) else out)
        if conversation is not None:  # no reasoning metadata offline
            conversation.record_turn(user, result.model_dump_json(), None)
        return result
