"""Policy envelope (autonomy, caller roles, tool allowlist) enforced in code, never in prompts.

Same shape as the reference `Policy`: read / write / write_requires_approval, a kill switch and an autonomy ceiling.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field

try:
    from sub_agent.states import Autonomy
except ImportError:
    from sub_agent.states import Autonomy

R = TypeVar("R")
_here = Path(__file__).resolve().parent
DEFAULT_POLICY_PATH = (_here / "uc04_policy.json") if (_here / "uc04_policy.json").exists() else (_here.parent / "policy" / "uc04_policy.json")


class PolicyViolation(PermissionError):
    pass


class ToolsDef(BaseModel):
    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)
    write_requires_approval: list[str] = Field(default_factory=list)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: str
    agent: str
    version: int
    enabled: bool = True  # kill switch
    approved_by: list[str] = Field(default_factory=list)
    autonomy: Autonomy = Autonomy.L1
    ceiling: Autonomy = Autonomy.L1
    allowed_caller_roles: list[str] = Field(default_factory=list)
    # Optional, empty by default. Both are governance decisions: nothing is mapped or narrowed until the governance owner approves values.
    role_aliases: dict[str, str] = Field(default_factory=dict)  # caller's role name -> an allowed role; never widens what is allowed
    acknowledge_roles: list[str] | None = None  # who may acknowledge an adapter error; None keeps "any allowed caller role"
    tools: ToolsDef = Field(default_factory=ToolsDef)

    @classmethod
    def load(cls, path: Path = DEFAULT_POLICY_PATH) -> "Policy":
        policy = cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
        order = list(Autonomy)
        if order.index(policy.autonomy) > order.index(policy.ceiling):
            raise ValueError("policy autonomy is above its ceiling")
        allowed = set(policy.allowed_caller_roles)
        for source, target in policy.role_aliases.items():
            if target not in allowed:
                raise ValueError("a role alias must map to an allowed caller role")
            if source in allowed:
                raise ValueError("a role alias may not redefine a role that is already allowed")  # an alias can add a name, never change one
        if policy.acknowledge_roles is not None and not set(policy.acknowledge_roles) <= allowed:
            raise ValueError("acknowledge_roles must be a subset of allowed_caller_roles")
        return policy

    def effective_roles(self, roles: list[str]) -> set[str]:
        """The caller's roles plus the allowed role each alias maps to. A role with no alias stays as it is."""
        return set(roles) | {self.role_aliases[r] for r in roles if r in self.role_aliases}

    def check_caller(self, roles: list[str]) -> None:
        if not self.enabled:
            raise PolicyViolation(f"{self.policy}: disabled by kill switch")
        if not self.effective_roles(roles) & set(self.allowed_caller_roles):
            raise PolicyViolation(f"{self.policy}: caller has no allowed role")

    def check_acknowledger(self, roles: list[str]) -> None:
        """Acknowledging an adapter error (permission to retry an uncertain external call). Falls back to `check_caller` unless
        `acknowledge_roles` is set, in which case the caller must also hold one of those roles."""
        self.check_caller(roles)
        if self.acknowledge_roles is not None and not self.effective_roles(roles) & set(self.acknowledge_roles):
            raise PolicyViolation(f"{self.policy}: caller may not acknowledge an adapter error")

    def tool_mode(self, tool: str) -> str:
        if tool in self.tools.read:
            return "read"
        if tool in self.tools.write or tool in self.tools.write_requires_approval:
            if self.autonomy == Autonomy.L0:
                raise PolicyViolation(f"{self.policy}: autonomy L0 may not call write tool '{tool}'")
            return "write"
        raise PolicyViolation(f"{self.policy}: tool '{tool}' is not on the allowlist")


class ToolGateway:
    """Allowlist -> approval check for gated write tools -> call. The call log lets tests prove what actually ran."""

    def __init__(self, policy: Policy):
        self.policy = policy
        self.calls: list[str] = []

    def call(self, tool: str, fn: Callable[..., R], *, approved: bool, **kwargs: Any) -> R:
        self.policy.tool_mode(tool)
        if tool in self.policy.tools.write_requires_approval and not approved:
            raise PolicyViolation(f"{tool} requires a valid human approval")
        self.calls.append(tool)
        return fn(**kwargs)
