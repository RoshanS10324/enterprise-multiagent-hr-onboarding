"""Policy envelopes (blueprint §7.6). Loaded from <agent>/guardrails/*.yaml; evaluated in code, never by the model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from hr_onboarding_common.contracts import Autonomy


class PolicyViolation(PermissionError):
    pass


class GateDef(BaseModel):
    approver_role: str
    description: str = ""
    sla_hours: int = 24
    # Separation of duties: the person who requested the approval may not decide it (enforced by
    # HITLInbox.decide; a request without a recorded requester is refused, fail closed).
    separation_of_duties: bool = False


class ToolsDef(BaseModel):
    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)
    write_requires_approval: list[str] = Field(default_factory=list)


class Policy(BaseModel):
    policy: str
    agent: str
    version: int
    enabled: bool = True  # per-agent kill switch -- enforced in ToolGateway.call() AND BaseAgent.run()
    approved_by: list[str] = Field(default_factory=list)
    autonomy: Autonomy = Autonomy.L1  # level at launch
    ceiling: Autonomy = Autonomy.L1  # highest level this agent may ever reach
    tools: ToolsDef = Field(default_factory=ToolsDef)
    gates: dict[str, GateDef] = Field(default_factory=dict)
    never_auto: list[str] = Field(default_factory=list)
    volume_ceiling: dict[str, int] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)

    def gate(self, gate_id: str) -> GateDef:
        if gate_id not in self.gates:
            raise PolicyViolation(f"{self.policy}: unknown gate '{gate_id}'")
        return self.gates[gate_id]

    @property
    def may_write(self) -> bool:
        """L0 agents assist only: they never write to any system, not even draft fields."""
        return self.autonomy != Autonomy.L0

    def tool_mode(self, tool: str) -> str:
        if tool in self.tools.read:
            return "read"
        if tool in self.tools.write or tool in self.tools.write_requires_approval:
            if not self.may_write:
                raise PolicyViolation(f"{self.policy}: autonomy L0 may not call write tool '{tool}'")
            return "write"
        raise PolicyViolation(f"{self.policy}: tool '{tool}' is not on the allowlist")

    def check_volume(self, key: str, n: int) -> None:
        ceiling = self.volume_ceiling.get(key)
        if ceiling is not None and n > ceiling:
            raise PolicyViolation(f"{self.policy}: volume {n} exceeds ceiling {key}={ceiling}")

    def threshold(self, name: str, default: float | None = None) -> float:
        if name in self.thresholds:
            return float(self.thresholds[name])
        if default is None:
            raise PolicyViolation(f"{self.policy}: missing threshold '{name}'")
        return default


class PolicyStore:
    def __init__(self, policy_dir: Path | None):
        self._by_agent: dict[str, Policy] = {}
        for path in sorted(policy_dir.rglob("*.yaml")) if policy_dir else []:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            p = Policy.model_validate(data)
            if list(Autonomy).index(p.autonomy) > list(Autonomy).index(p.ceiling):
                raise ValueError(f"{path}: autonomy {p.autonomy} is above ceiling {p.ceiling}")
            if p.agent in self._by_agent:
                raise ValueError(f"duplicate policy for agent {p.agent}: {path}")
            self._by_agent[p.agent] = p

    def for_agent(self, agent_id: str) -> Policy:
        if agent_id not in self._by_agent:
            raise PolicyViolation(f"no policy envelope for agent '{agent_id}'")
        return self._by_agent[agent_id]

    def override(self, agent_id: str, **changes: Any) -> None:
        """Test/ops hook (e.g. flip the kill switch)."""
        self._by_agent[agent_id] = self._by_agent[agent_id].model_copy(update=changes)

    @property
    def agents(self) -> list[str]:
        return sorted(self._by_agent)
