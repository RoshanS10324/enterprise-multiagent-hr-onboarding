from __future__ import annotations

from typing import Any

from .agent import SubAgent, SubAgentConfig


class FakeSubAgent(SubAgent):
    """
    Test double for SubAgent.
    Returns a configurable response without executing real skills.
    """

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        super().__init__(SubAgentConfig(name="fake"))
        self._response = response or {"status": "ok"}
        self.calls: list[dict[str, Any]] = []

    async def run(self, task: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(task)
        return self._response
