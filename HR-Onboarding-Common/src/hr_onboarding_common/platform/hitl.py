"""HITL inbox (blueprint §7.7): one approval queue for every gate in every orchestrator.

Forked from hr_recruitment_common.platform.hitl with two security fixes documented in
docs/ONBOARDING_COMMON_ARCHITECTURE.md (findings S1 and S5):

S1 -- cross-tenant approval access/decisions. Upstream `ApprovalItem` carries no tenant
identifier and `get`/`list`/`decide` have no tenant awareness at all: any caller who can reach the
API can read or decide any other tenant's approval, including rewriting its `edited_payload`. Here,
`ApprovalItem.client_id` is required, stored as its own indexed SQLite column (not just a field
inside the serialized JSON body), and every lookup/decision is scoped to it *in the query itself*
-- `get`/`decide` treat a tenant mismatch exactly like "not found" (KeyError), so cross-tenant
callers cannot even distinguish "wrong tenant" from "does not exist". `list` is parameterised by
`client_id` so the SQL WHERE clause does the scoping; nothing relies on filtering an unscoped result
set after the fact.

S5 -- approval decisions consumed before their follow-up action succeeds. Upstream durably commits
the decision to SQLite, THEN calls subscribers; if a subscriber raises (e.g. a calendar/email
provider failure), the exception propagates with no record that the real-world action never
happened, and the item can never be decided again ("already decided"). Here, a subscriber failure
is caught, recorded on the item as `apply_error` (the human decision itself is not undone -- it was
real), audited as `hitl.apply_failed`, and raised as a distinguishable `HITLApplyError` so the
caller sees the discrepancy instead of an opaque 500. `retry_apply` re-invokes subscribers for an
item whose `apply_error` is set, without requiring a new approval, so a transient failure (the
calendar/email provider down, etc.) is recoverable rather than a permanent stuck state.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from hr_onboarding_common.contracts import new_id, now_utc
from hr_onboarding_common.platform.audit import AuditLog


class Decision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EDITED = "edited"  # approved with changes
    REJECTED = "rejected"
    # Retracted by the requesting agent before anyone decided it (its facts changed). Not a decision:
    # nobody approved or rejected it, and it can no longer be decided (HITLInbox.withdraw).
    WITHDRAWN = "withdrawn"


class ReasonCode(StrEnum):
    # edits
    TONE = "tone"
    ACCURACY = "accuracy"
    MISSING_INFO = "missing_info"
    BIAS_LANGUAGE = "bias_language"
    # rejections / overrides
    NOT_RELEVANT = "not_relevant"
    POLICY = "policy"
    DUPLICATE = "duplicate"
    TIMING = "timing"
    OTHER = "other"


class ApprovalItem(BaseModel):
    id: str = Field(default_factory=lambda: new_id("apr"))
    gate: str
    approver_role: str
    workflow: str
    workflow_instance: str
    agent: str
    proposal_type: str
    client_id: str  # tenant this approval belongs to -- required, never inferred (finding S1)
    subject_id: str | None = None
    payload: dict[str, Any]
    rationale: str
    confidence: float
    run_id: str
    status: Decision = Decision.PENDING
    approver: str | None = None
    reason_code: ReasonCode | None = None
    note: str | None = None
    final_payload: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=now_utc)
    decided_at: datetime | None = None
    sla_hours: int = 24
    apply_error: str | None = None  # set when a post-decision listener raised (finding S5)
    # Who asked for this decision, and whether that person is barred from deciding it (the gate's
    # separation_of_duties). Absent on items created before these fields existed.
    requested_by: str | None = None
    separation_of_duties: bool = False

    @property
    def is_approved(self) -> bool:
        return self.status in (Decision.APPROVED, Decision.EDITED)


class HITLError(PermissionError):
    pass


class HITLApplyError(RuntimeError):
    """A decision was recorded successfully, but applying its follow-up action failed. The
    decision itself (status/approver/reason_code) is real and was not rolled back -- see
    `HITLInbox.retry_apply` to retry the follow-up action once the underlying fault is cleared."""

    def __init__(self, item: "ApprovalItem", cause: BaseException):
        super().__init__(f"approval {item.id} was decided ({item.status}) but applying it failed: {cause}")
        self.item = item
        self.cause = cause


Listener = Callable[[ApprovalItem], None]


class HITLInbox:
    def __init__(self, db_path: Path | str = ":memory:", audit: AuditLog | None = None):
        if isinstance(db_path, Path):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, status TEXT, gate TEXT, "
                          "workflow_instance TEXT, subject_id TEXT, client_id TEXT, body TEXT)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_approvals_client_id ON approvals(client_id)")
        self.audit = audit or AuditLog()
        self._listeners: list[Listener] = []
        self._lock = threading.RLock()  # one connection shared by the API's worker threads

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def _save(self, item: ApprovalItem) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO approvals VALUES (?,?,?,?,?,?,?)",
                              (item.id, item.status, item.gate, item.workflow_instance, item.subject_id,
                               item.client_id, item.model_dump_json()))
            self.conn.commit()

    def submit(self, item: ApprovalItem) -> ApprovalItem:
        self._save(item)
        self.audit.record("hitl.submitted", agent=item.agent, subject_id=item.subject_id,
                          client_id=item.client_id, approval_id=item.id, gate=item.gate)
        return item

    def get(self, approval_id: str, *, client_id: str | None = None) -> ApprovalItem:
        """`client_id=None` is for internal/trusted callers only (e.g. a listener that already
        received the item). Every externally reachable caller (the API layer) must pass the
        authenticated actor's client_id -- a mismatch raises KeyError, identical to "not found",
        so a cross-tenant caller cannot distinguish "wrong tenant" from "does not exist" (finding S1)."""
        with self._lock:
            if client_id is None:
                row = self.conn.execute("SELECT body FROM approvals WHERE id=?", (approval_id,)).fetchone()
            else:
                row = self.conn.execute("SELECT body FROM approvals WHERE id=? AND client_id=?",
                                        (approval_id, client_id)).fetchone()
        if not row:
            raise KeyError(approval_id)
        return ApprovalItem.model_validate_json(row[0])

    def list(self, *, status: Decision | None = Decision.PENDING, gate: str | None = None,
             workflow_instance: str | None = None, client_id: str | None = None) -> list[ApprovalItem]:
        """`client_id=None` lists across every tenant -- for internal/ops use only (e.g. a
        cross-tenant timeout sweep that itself re-checks client_id per item). The API layer must
        always pass the authenticated actor's client_id; the WHERE clause does the scoping, not a
        post-hoc filter of an unscoped result set (finding S1)."""
        sql, args = "SELECT body FROM approvals WHERE 1=1", []
        if status:
            sql += " AND status=?"; args.append(status)
        if gate:
            sql += " AND gate=?"; args.append(gate)
        if workflow_instance:
            sql += " AND workflow_instance=?"; args.append(workflow_instance)
        if client_id is not None:
            sql += " AND client_id=?"; args.append(client_id)
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        items = [ApprovalItem.model_validate_json(r[0]) for r in rows]
        return sorted(items, key=lambda i: i.created_at)

    def decide(self, approval_id: str, decision: Decision, *, approver: str, approver_roles: list[str],
               client_id: str, reason_code: ReasonCode | None = None, note: str | None = None,
               edited_payload: dict[str, Any] | None = None) -> ApprovalItem:
        with self._lock:  # read-check-write must be atomic: an item is decided exactly once
            item = self._decide_locked(approval_id, decision, approver, approver_roles, client_id, reason_code,
                                       note, edited_payload)
        try:
            for listener in self._listeners:
                listener(item)
        except Exception as exc:  # noqa: BLE001 - the decision is real; only the follow-up action failed
            item = self._record_apply_error(item, exc)
            raise HITLApplyError(item, exc) from exc
        return item

    def _decide_locked(self, approval_id: str, decision: Decision, approver: str, approver_roles: list[str],
                       client_id: str, reason_code: ReasonCode | None, note: str | None,
                       edited_payload: dict[str, Any] | None) -> ApprovalItem:
        item = self.get(approval_id, client_id=client_id)  # KeyError if missing OR wrong tenant (finding S1)
        if item.status != Decision.PENDING:
            raise HITLError(f"{approval_id} already decided ({item.status})")
        if item.approver_role not in approver_roles:
            raise HITLError(f"{approver} lacks role '{item.approver_role}' for gate '{item.gate}'")
        if item.separation_of_duties and (item.requested_by is None or approver == item.requested_by):
            raise HITLError(f"separation of duties: {approver} may not decide '{item.gate}' "
                            + ("which they requested" if item.requested_by else "whose requester is not recorded"))
        if decision in (Decision.PENDING, Decision.WITHDRAWN):
            raise HITLError("decision must be approved, edited or rejected")
        if decision in (Decision.REJECTED, Decision.EDITED) and reason_code is None:
            raise HITLError("reason_code is required for rejections and edits (feeds evals + memory)")
        if decision == Decision.EDITED and edited_payload is None:
            raise HITLError("edited decisions must include edited_payload")

        item.status, item.approver, item.reason_code, item.note = decision, approver, reason_code, note
        item.final_payload = edited_payload if decision == Decision.EDITED else (
            item.payload if decision == Decision.APPROVED else None)
        item.decided_at = now_utc()
        self._save(item)
        self.audit.record("hitl.decided", actor=approver, agent=item.agent, subject_id=item.subject_id,
                          client_id=item.client_id, approval_id=item.id, gate=item.gate, decision=decision,
                          reason_code=reason_code)
        return item

    def _record_apply_error(self, item: ApprovalItem, exc: BaseException) -> ApprovalItem:
        item = item.model_copy(update={"apply_error": f"{type(exc).__name__}: {exc}"[:500]})
        self._save(item)
        self.audit.record("hitl.apply_failed", agent=item.agent, subject_id=item.subject_id,
                          client_id=item.client_id, approval_id=item.id, gate=item.gate,
                          error_type=type(exc).__name__)
        return item

    def retry_apply(self, approval_id: str, *, client_id: str) -> ApprovalItem:
        """Re-invokes subscribers for an already-decided item whose previous apply attempt failed.
        Requires the item to actually have a recorded `apply_error` -- this is a recovery path for
        S5, not a general re-apply/replay mechanism, and it never re-runs a successfully applied
        decision. Each subscriber (`services.*.apply_decision`) must be idempotent under retry;
        that is verified per-function at implementation time, not assumed here."""
        item = self.get(approval_id, client_id=client_id)
        if not item.is_approved:
            raise HITLError(f"{approval_id} is {item.status}, not approved; nothing to retry")
        if item.apply_error is None:
            raise HITLError(f"{approval_id} has no recorded apply failure; nothing to retry")
        try:
            for listener in self._listeners:
                listener(item)
        except Exception as exc:  # noqa: BLE001 - same handling as the original decide() attempt
            item = self._record_apply_error(item, exc)
            raise HITLApplyError(item, exc) from exc
        item = item.model_copy(update={"apply_error": None})
        self._save(item)
        self.audit.record("hitl.apply_recovered", agent=item.agent, subject_id=item.subject_id,
                          client_id=item.client_id, approval_id=item.id, gate=item.gate)
        return item

    def withdraw(self, approval_id: str, *, client_id: str, by: str, reason: str) -> ApprovalItem:
        """Retract a still-pending request whose facts changed (e.g. a day-1 signoff requested for a
        joining date that has since been revised). Not a decision: no approver is recorded, and a
        later `decide` is refused like any decided item. HITLError if it is no longer pending."""
        with self._lock:
            item = self.get(approval_id, client_id=client_id)
            if item.status != Decision.PENDING:
                raise HITLError(f"{approval_id} already decided ({item.status})")
            item.status, item.note, item.decided_at = Decision.WITHDRAWN, reason, now_utc()
            self._save(item)
        self.audit.record("hitl.withdrawn", actor=by, agent=item.agent, subject_id=item.subject_id,
                          client_id=item.client_id, approval_id=item.id, gate=item.gate, reason=reason)
        return item

    def approval_stats(self, agent: str, *, client_id: str | None = None) -> dict[str, Any]:
        decided = [i for i in self.list(status=None, client_id=client_id) if i.agent == agent
                  and i.status not in (Decision.PENDING, Decision.WITHDRAWN)]
        if not decided:
            return {"decided": 0, "approval_rate": None, "material_edit_rate": None}
        approved = sum(1 for i in decided if i.status == Decision.APPROVED)
        edited = sum(1 for i in decided if i.status == Decision.EDITED)
        return {"decided": len(decided), "approval_rate": (approved + edited) / len(decided),
                "material_edit_rate": edited / len(decided)}

    def export(self, *, client_id: str | None = None) -> str:
        return json.dumps([json.loads(i.model_dump_json()) for i in self.list(status=None, client_id=client_id)],
                          indent=1)
