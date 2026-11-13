"""ToolGateway: the pre-tool policy hook (blueprint §7.3 / §7.5 layer 4).

Every connector call made by an agent or orchestrator goes through here:
kill-switch check -> allowlist check -> approval check for gated write tools -> audit -> call.

Forked from hr_recruitment_common.platform.tools with one security fix (finding S6): upstream
never checks `policy.enabled` here at all -- the kill switch is enforced only inside
`BaseAgent.run()`, so disabling an agent's policy stops the LangGraph run path but leaves every
`ToolGateway`-mediated call (i.e. everything reachable through a tool registry, not just a graph)
fully operational. The `enabled` check below is evaluated first, before the allowlist check, so a
disabled policy blocks every tool call regardless of read/write mode or gate.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from hr_onboarding_common.contracts import Actor
from hr_onboarding_common.observability.tracing import span
from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import HITLInbox
from hr_onboarding_common.platform.policy import Policy, PolicyViolation

R = TypeVar("R")


class ToolGateway:
    def __init__(self, policy: Policy, audit: AuditLog, hitl: HITLInbox, actor: Actor):
        self.policy, self.audit, self.hitl, self.actor = policy, audit, hitl, actor
        self.calls: list[str] = []

    def call(self, tool: str, fn: Callable[..., R], *args: Any, approval_id: str | None = None,
             subject_id: str | None = None, **kwargs: Any) -> R:
        if not self.policy.enabled:  # kill switch (finding S6) -- checked before anything else
            self.audit.record("policy.denied", agent=self.policy.agent, tool=tool, reason="agent_disabled")
            raise PolicyViolation(f"{self.policy.policy}: agent is disabled (kill switch); '{tool}' was not called")
        mode = self.policy.tool_mode(tool)  # raises if not allowlisted
        if tool in self.policy.tools.write_requires_approval:
            self._require_approval(tool, approval_id, subject_id)
        self.audit.record("tool.call", actor=self.actor.on_behalf_of, agent=self.policy.agent,
                          subject_id=subject_id, client_id=self.actor.client_id, tool=tool, mode=mode,
                          approval_id=approval_id)
        self.calls.append(tool)
        with span("tool.call", **{"tool.name": tool, "tool.mode": mode}):
            return fn(*args, **kwargs)

    def _require_approval(self, tool: str, approval_id: str | None, subject_id: str | None) -> None:
        if not approval_id:
            self.audit.record("policy.denied", agent=self.policy.agent, tool=tool, reason="missing_approval")
            raise PolicyViolation(f"{tool} requires an approved HITL item (no approval_id given)")
        if self.actor.client_id is None:
            # Fail closed: an actor with no verified tenant must never be able to consume an
            # approval from any tenant (finding S1). `hitl.get(..., client_id=None)` means
            # "search every tenant" and is for internal/trusted callers only -- never for an
            # actor reached through the API.
            self.audit.record("policy.denied", agent=self.policy.agent, tool=tool, reason="actor_has_no_tenant")
            raise PolicyViolation(f"{tool}: actor has no verified client_id; cannot consume approval {approval_id}")
        try:
            item = self.hitl.get(approval_id, client_id=self.actor.client_id)
        except KeyError as e:
            raise PolicyViolation(f"{tool}: approval {approval_id} not found") from e
        if not item.is_approved:
            raise PolicyViolation(f"{tool}: approval {approval_id} is {item.status}, not approved")
        if subject_id is not None and item.subject_id not in (None, subject_id):
            raise PolicyViolation(f"{tool}: approval {approval_id} is for {item.subject_id}, not {subject_id}")
