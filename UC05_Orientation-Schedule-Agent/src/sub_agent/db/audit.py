"""UC05's audit trail and run tracking in supabase mode.

SupabaseAuditLog extends the shared AuditLog (hr_onboarding_common.platform.audit): every event is
still kept by AuditLog itself (in memory / JSONL, structured log), and every TENANT-SCOPED event is
also written to audit_events. An event is tenant-scoped when it names its client_id; the tool registry
gives services a TenantAuditLog view that adds its verified tenant to events that do not name one.
Events with no tenant at all (none in UC05's business flows) are not written to the shared table:
they cannot be attributed to a client.

What a row holds: case_id (resolved from the employee within the tenant), actor (HUMAN when a person
acted, AGENT for UC05 itself, SYSTEM otherwise), action = the event name, outcome, correlation id
(run / approval / workflow id) and the event's details after sanitize(): ids, statuses and counts,
never names, addresses, message text or credentials.

A failed audit write raises PersistenceError. It is never swallowed: a business step whose audit
row could not be written fails, and the run stays resumable (every UC05 write is idempotent).

AgentRunRecorder writes one agent_runs row per UC05 run (BaseAgent.run and the HITL listener):
agent id/version, actor, status, the model(s) used, tool calls, proposal/action/exception counts and
timestamps. Token counts are not exposed by hr_onboarding_common's LLM client, so they are left to the
column defaults rather than invented.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from hr_onboarding_common.contracts import AgentResponse, now_utc
from hr_onboarding_common.platform.audit import AuditLog
from sub_agent.db import mapping as m
from sub_agent.db.postgrest import PostgrestClient, UniqueViolation
from sub_agent.db.repository import SupabaseOnboardingRepository, _iso

_FAILURE_MARKERS = ("failed", "error", "uncertain", "rejected_ineligible")


def _outcome(event: str) -> str:
    name = event.lower()
    if name == "policy.denied" or name.endswith("tenant_mismatch"):
        return "DENIED"
    if any(marker in name for marker in _FAILURE_MARKERS):
        return "FAILURE"
    return "SUCCESS"


class SupabaseAuditLog(AuditLog):
    def __init__(self, client: PostgrestClient, repository: SupabaseOnboardingRepository, *,
                 path: Path | None = None) -> None:
        super().__init__(path)
        self.client, self.repository = client, repository

    def for_tenant(self, client_id: str) -> "TenantAuditLog":
        return TenantAuditLog(self, client_id)

    def _case_id(self, client_id: str, subject_id: str | None) -> str | None:
        if not subject_id:
            return None
        try:
            return self.repository.case_id_for(subject_id, client_id)
        except KeyError:
            return None

    def record(self, event: str, *, actor: str | None = None, agent: str | None = None,
               subject_id: str | None = None, **details: Any) -> dict[str, Any]:
        entry = super().record(event, actor=actor, agent=agent, subject_id=subject_id, **details)
        client_id = details.get("client_id")
        if not client_id:
            return entry
        actor_type = "HUMAN" if actor and not str(actor).startswith(("system", "hitl")) else (
            "AGENT" if agent else "SYSTEM")
        correlation = details.get("run_id") or details.get("approval_id") or details.get("workflow_id")
        before, after = details.get("before"), details.get("after")
        self.client.insert("audit_events", [{
            "event_id": str(uuid.uuid4()), "case_id": self._case_id(client_id, subject_id), "client_id": client_id,
            "actor_type": actor_type, "actor_id": str(actor or agent or "system"),
            "actor_role": "user" if actor_type == "HUMAN" else ("agent" if actor_type == "AGENT" else "system"),
            "action": event, "outcome": _outcome(event),
            "detail": m.sanitize({k: v for k, v in details.items() if k not in ("before", "after")}
                                 | ({"subject_id": subject_id} if subject_id else {})),
            "before_state": m.sanitize(before) if isinstance(before, dict) else None,
            "after_state": m.sanitize(after) if isinstance(after, dict) else None,
            "correlation_id": str(correlation) if correlation else None,
            "reference": f"{m.REF_PREFIX}event:{uuid.uuid4()}", "occurred_at": entry["ts"]}])
        return entry


class TenantAuditLog:
    """A view of an AuditLog for one verified tenant: events that do not name a client_id are recorded
    for this one. Everything else is delegated, so callers cannot tell it from the AuditLog."""

    def __init__(self, inner: AuditLog, client_id: str) -> None:
        self._inner, self._client_id = inner, client_id

    def record(self, event: str, *, actor: str | None = None, agent: str | None = None,
               subject_id: str | None = None, **details: Any) -> dict[str, Any]:
        details.setdefault("client_id", self._client_id)
        return self._inner.record(event, actor=actor, agent=agent, subject_id=subject_id, **details)

    def for_tenant(self, client_id: str) -> "TenantAuditLog":
        return self if client_id == self._client_id else TenantAuditLog(self._inner, client_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class AgentRunRecorder:
    def __init__(self, client: PostgrestClient, repository: SupabaseOnboardingRepository) -> None:
        self.client, self.repository = client, repository

    def record(self, *, response: AgentResponse | None, run_id: str, client_id: str | None, actor_id: str,
               agent_id: str, agent_version: str, subject_id: str | None, models: list[str],
               started_at, error: BaseException | None = None) -> None:
        if not client_id:
            return  # a run with no verified tenant wrote nothing tenant-scoped either
        case_id = None
        if subject_id:
            try:
                case_id = self.repository.case_id_for(subject_id, client_id)
            except KeyError:
                case_id = None
        prov = response.provenance if response is not None else None
        exceptions = response.exceptions if response is not None else []
        if error is not None:
            status, summary = m.AGENT_RUN_STATUS["failed"], type(error).__name__
        else:
            status = m.AGENT_RUN_STATUS[str(response.status).lower() if response is not None else "failed"]
            summary = (f"{exceptions[0].code}: {exceptions[0].detail}"[:300] if exceptions else None)
        row = {"run_id": run_id, "case_id": case_id, "client_id": client_id, "agent_id": agent_id,
               "agent_version": agent_version, "actor_id": actor_id, "status": status,
               "model_name": ",".join(sorted(set(models))) or None,
               "proposal_count": len(response.proposals) if response is not None else 0,
               "action_count": len(response.actions_taken) if response is not None else 0,
               "exception_count": len(exceptions) + (1 if error is not None else 0),
               "tool_calls": list(prov.tool_calls) if prov is not None else [],
               "error_summary": m.sanitize(summary) if summary else None,
               "started_at": _iso(started_at),
               "finished_at": _iso(prov.finished_at) if prov is not None and prov.finished_at else _iso(now_utc())}
        try:
            self.client.insert("agent_runs", [row])
        except UniqueViolation:
            pass  # this run is already recorded (a redelivered response); never a second row
