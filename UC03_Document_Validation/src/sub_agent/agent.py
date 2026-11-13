from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SubAgentConfig:
    name: str
    skills: list[str] = field(default_factory=list)
    max_steps: int = 10


class SubAgent:
    """
    Lightweight sub-agent wrapper.
    Receives a task, runs it through the configured skills, returns a result.
    """

    def __init__(self, config: SubAgentConfig) -> None:
        self._config = config

    async def run(self, task: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
