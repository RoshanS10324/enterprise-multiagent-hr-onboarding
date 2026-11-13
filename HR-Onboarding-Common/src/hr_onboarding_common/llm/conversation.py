"""A multi-turn exchange with a model, for agents that need one. Every model call today is
single-turn; this is the supported way to continue a conversation when one is needed.

With a reasoning model, OpenRouter returns `reasoning_details` beside the answer: the provider's own
record of the model's reasoning (text, summaries, encrypted blocks, signatures). When a conversation
continues, it must be sent back unchanged with the assistant turn it belongs to. An LLMConversation
keeps each assistant turn exactly as returned -- `reasoning_details` deep-copied, never edited,
reconstructed or merged -- and the gateway sends the whole history on the next turn.

Isolation. A conversation belongs to one scope fixed at creation, e.g. (client_id, case_id,
session_id). The gateway refuses to continue it under any other scope, so one employee's, tenant's or
case's history can never be sent with another's request. It lives only in the memory of the
execution that created it: it is never persisted to case state, never logged (its repr shows the scope
and the number of turns only) and never returned by an API. Reasoning metadata leaves it only to go
back to the provider.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


class ConversationScopeError(ValueError):
    """A conversation was used under a scope other than its own, or created without one."""


@dataclass(eq=False)
class LLMConversation:
    scope: tuple[str, ...]
    _messages: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        scope = tuple(self.scope or ())
        if not scope or not all(isinstance(part, str) and part.strip() for part in scope):
            raise ConversationScopeError("a conversation needs a non-empty scope, e.g. (client_id, case_id, session_id)")
        self.scope = scope

    def __repr__(self) -> str:
        return f"LLMConversation(scope={self.scope!r}, turns={self.turns})"

    @property
    def turns(self) -> int:
        return sum(1 for m in self._messages if m["role"] == "assistant")

    def check_scope(self, scope: tuple[str, ...] | None) -> None:
        if tuple(scope or ()) != self.scope:
            raise ConversationScopeError("this conversation belongs to another scope; start a new conversation")

    def history(self) -> list[dict[str, Any]]:
        """The earlier turns, as they go back to the provider (a copy: the conversation is unchanged)."""
        return copy.deepcopy(self._messages)

    def record_turn(self, user: str, content: str, reasoning_details: list[Any] | None) -> None:
        """Called by the LLM client once a turn has succeeded; a failed turn records nothing."""
        assistant: dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning_details is not None:
            assistant["reasoning_details"] = copy.deepcopy(reasoning_details)
        self._messages += [{"role": "user", "content": user}, assistant]
