"""SupabaseHITLInbox: the shared approval mechanism (hr_onboarding_common.platform.hitl.HITLInbox)
with its storage moved to approval_requests. Not a second approval system: it IS HITLInbox -- the
same submit/get/list/decide/retry_apply/withdraw code, role checks, listeners and apply_error
recovery (findings S1 and S5) -- with only the persistence calls overridden.

  - Tenant isolation: get/list/decide filter on client_id in the query itself (as HITLInbox does).
  - Decided exactly once ACROSS PROCESSES: a decision is written with a PATCH conditional on
    outcome = PENDING. If another process decided first, nothing is changed and HITLError
    ("already decided") is raised -- the decision is never applied twice.
  - Only rows with workflow_module = 'uc05_orientation_scheduling' are read back, so another use
    case's approval rows are never treated as UC05's.
  - approval_requests has no columns for the inbox's own bookkeeping (workflow_instance, agent,
    proposal_type, run_id, sla_hours, the exact decision value). They travel in the payload
    envelope: payload = {"proposal": <the proposal>, "uc05": {...}}. Outcomes use the live vocabulary:
    an EDITED decision is EDITED_AND_APPROVED (edited content in final_payload), a withdrawn request
    CANCELLED. requested_by is the agent and decided_by the person, satisfying chk_no_self_approval.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from hr_onboarding_common.platform.audit import AuditLog
from hr_onboarding_common.platform.hitl import ApprovalItem, Decision, HITLError, HITLInbox, ReasonCode
from sub_agent.db import mapping as m
from sub_agent.db.postgrest import PostgrestClient, UniqueViolation
from sub_agent.db.repository import SupabaseOnboardingRepository, _iso, _ts

_ENVELOPE = ("workflow", "workflow_instance", "agent", "proposal_type", "run_id", "sla_hours", "subject_id")


class SupabaseHITLInbox(HITLInbox):
    def __init__(self, client: PostgrestClient, repository: SupabaseOnboardingRepository,
                 audit: AuditLog | None = None,
                 on_submitted: Callable[[ApprovalItem], None] | None = None) -> None:
        super().__init__(":memory:", audit)  # the in-memory table is never used
        self.client, self.repository, self.on_submitted = client, repository, on_submitted
        self._expected = threading.local()  # the outcome a conditional decision write expects

    def __repr__(self) -> str:
        return "SupabaseHITLInbox()"

    # ------------------------------------------------------------------ mapping
    def _case_id(self, item: ApprovalItem) -> str | None:
        if not item.subject_id:
            return None
        try:
            return self.repository.case_id_for(item.subject_id, item.client_id)
        except KeyError:
            return None

    def _row(self, item: ApprovalItem) -> dict[str, Any]:
        envelope = {k: getattr(item, k) for k in _ENVELOPE}
        envelope.update(status=item.status.value, approver=item.approver,
                        reason_code=item.reason_code.value if item.reason_code else None)
        return {
            "request_id": item.id, "case_id": self._case_id(item), "client_id": item.client_id, "gate": item.gate,
            "approver_role": item.approver_role, "requested_by": item.agent, "workflow_module": m.WORKFLOW_MODULE,
            "subject_ref": item.subject_id or item.workflow_instance,
            "payload": {"proposal": item.payload, "uc05": envelope}, "rationale": item.rationale,
            "confidence_score": item.confidence, "outcome": m.APPROVAL_OUTCOME[item.status.value],
            "decided_by": item.approver, "decided_at": _iso(item.decided_at) if item.decided_at else None,
            "decision_reason_code": item.reason_code.value if item.reason_code else None,
            "decision_notes": item.note, "final_payload": item.final_payload, "apply_error": item.apply_error,
            "sla_due_at": _iso(item.created_at + timedelta(hours=item.sla_hours)),
            "created_at": _iso(item.created_at)}

    @staticmethod
    def _item(row: dict[str, Any]) -> ApprovalItem:
        payload = row.get("payload") or {}
        env = payload.get("uc05") or {}
        status = env.get("status") or m.APPROVAL_OUTCOME_READ[str(row["outcome"]).upper()]
        reason = row.get("decision_reason_code") or env.get("reason_code")
        return ApprovalItem(
            id=row["request_id"], gate=row["gate"], approver_role=row["approver_role"],
            workflow=env.get("workflow") or m.AGENT_ID, workflow_instance=env.get("workflow_instance") or row["subject_ref"],
            agent=env.get("agent") or row.get("requested_by") or m.AGENT_ID,
            proposal_type=env.get("proposal_type") or "", client_id=row["client_id"],
            subject_id=env.get("subject_id"), payload=payload.get("proposal") or {},
            rationale=row.get("rationale") or "", confidence=float(row.get("confidence_score") or 0),
            run_id=env.get("run_id") or "", status=Decision(status), approver=row.get("decided_by"),
            reason_code=ReasonCode(reason) if reason else None, note=row.get("decision_notes"),
            final_payload=row.get("final_payload"),
            created_at=_ts(row["created_at"]) if row.get("created_at") else datetime.now(timezone.utc),
            decided_at=_ts(row["decided_at"]) if row.get("decided_at") else None,
            sla_hours=int(env.get("sla_hours") or 24), apply_error=row.get("apply_error"))

    # ------------------------------------------------------------------ storage overrides
    def submit(self, item: ApprovalItem) -> ApprovalItem:
        try:
            self.client.insert("approval_requests", [self._row(item)])
        except UniqueViolation:
            raise HITLError(f"approval {item.id} already exists") from None
        self.audit.record("hitl.submitted", agent=item.agent, subject_id=item.subject_id,
                          client_id=item.client_id, approval_id=item.id, gate=item.gate)
        if self.on_submitted is not None:
            self.on_submitted(item)
        return item

    def _save(self, item: ApprovalItem) -> None:
        filters = [("request_id", f"eq.{item.id}"), ("client_id", f"eq.{item.client_id}"),
                   ("workflow_module", f"eq.{m.WORKFLOW_MODULE}")]
        expected = getattr(self._expected, "outcome", None)
        if expected is not None:
            filters.append(("outcome", f"eq.{expected}"))
        values = {k: v for k, v in self._row(item).items() if k not in ("request_id", "client_id", "created_at")}
        if not self.client.update("approval_requests", values, filters=filters):
            if expected is not None:
                raise HITLError(f"{item.id} was already decided by another process")
            raise KeyError(item.id)

    def get(self, approval_id: str, *, client_id: str | None = None) -> ApprovalItem:
        filters = [("request_id", f"eq.{approval_id}"), ("workflow_module", f"eq.{m.WORKFLOW_MODULE}")]
        if client_id is not None:
            filters.append(("client_id", f"eq.{client_id}"))
        rows = self.client.select("approval_requests", filters=filters)
        if not rows:
            raise KeyError(approval_id)
        return self._item(rows[0])

    def list(self, *, status: Decision | None = Decision.PENDING, gate: str | None = None,
             workflow_instance: str | None = None, client_id: str | None = None) -> list[ApprovalItem]:
        filters = [("workflow_module", f"eq.{m.WORKFLOW_MODULE}")]
        if client_id is not None:
            filters.append(("client_id", f"eq.{client_id}"))
        if gate:
            filters.append(("gate", f"eq.{gate}"))
        if status is not None:
            filters.append(("outcome", f"eq.{m.APPROVAL_OUTCOME[Decision(status).value]}"))
        items = [self._item(r) for r in self.client.select("approval_requests", filters=filters)]
        items = [i for i in items if (status is None or i.status == status)
                 and (workflow_instance is None or i.workflow_instance == workflow_instance)]
        return sorted(items, key=lambda i: i.created_at)

    # ------------------------------------------------------------------ conditional (decide-once) writes
    def _decide_locked(self, *args: Any, **kwargs: Any) -> ApprovalItem:
        self._expected.outcome = m.APPROVAL_OUTCOME[Decision.PENDING.value]
        try:
            return super()._decide_locked(*args, **kwargs)
        finally:
            self._expected.outcome = None

    def withdraw(self, approval_id: str, *, client_id: str, by: str, reason: str) -> ApprovalItem:
        self._expected.outcome = m.APPROVAL_OUTCOME[Decision.PENDING.value]
        try:
            return super().withdraw(approval_id, client_id=client_id, by=by, reason=reason)
        finally:
            self._expected.outcome = None
