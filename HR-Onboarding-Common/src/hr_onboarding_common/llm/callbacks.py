"""Gateway callbacks: one event per LLM attempt outcome. Events never carry prompt or completion text."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from hr_onboarding_common.observability.logging import get_logger

log = get_logger("llm")


@dataclass(frozen=True)
class LLMEvent:
    kind: str  # success | retry | failure | fallback
    task: str
    tier: str
    model: str
    attempt: int = 1
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    reasoning_tokens: int | None = None  # a count only; reasoning content never reaches an event
    error_type: str | None = None
    next_model: str | None = None  # set on fallback events


class LLMCallback(Protocol):
    def __call__(self, event: LLMEvent) -> None: ...


def logging_callback(event: LLMEvent) -> None:
    """Default callback: structured log line per event."""
    level = "info" if event.kind == "success" else "warning"
    getattr(log, level)(f"llm.{event.kind}", extra={"llm": {k: v for k, v in asdict(event).items() if v is not None}})
