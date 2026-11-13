"""Reconciliation after a restart or a crash: what the durable stores disagree on, for one tenant.

The onboarding state lives in four places that cannot share one transaction -- the repository and
operation ledger (onboarding.db), the HITL inbox (hitl.db), the workflow checkpoints
(checkpoints.db) and the external providers. A crash between two writes can leave them out of
step. This report finds every such gap and says how it is resolved; it changes nothing itself.

  interrupted_runs           a workflow run stopped on a failed node. Resumes when its event is
                             redelivered, or (a decision) through POST /v1/approvals/{id}/retry-apply.
  failed_decisions           an approved decision whose follow-up action failed (apply_error).
                             POST /v1/approvals/{id}/retry-apply.
  unsettled_operations       a calendar booking or message whose provider outcome is unknown. Retried
                             with the same idempotency key when the provider supports it; otherwise
                             HR checks the provider and calls POST /v1/onboarding/admin/operations/resolve.
  sessions_without_approval  a hold whose approval item was never created (a crash in between).
                             HR cancels it or books it again.
  approvals_without_records  an undecided approval whose record no longer exists.
  unreleased_rejections      a rejected booking whose hold is still live (its apply failed).
                             POST /v1/onboarding/sessions/check-timeouts releases it.
"""

from __future__ import annotations

from typing import Any

from hr_onboarding_common.platform.hitl import Decision
from sub_agent.agent import AGENT_ID, OnboardingAgent, WorkflowEvent
from sub_agent.models import OperationStatus, SessionStatus

_REMEDY = {
    WorkflowEvent.APPROVAL_DECIDED.value: "POST /v1/approvals/{id}/retry-apply",
    WorkflowEvent.OFFER_ACCEPTED.value: "redeliver the offer-accepted event",
    WorkflowEvent.JOINING_DAY.value: "the joining-day scheduler retries it",
}


def report(agent: OnboardingAgent, client_id: str) -> dict[str, Any]:
    repo, hitl = agent.deps.repository, agent.ctx.hitl
    bind = getattr(repo, "for_tenant", None)  # supabase mode: every read carries this client_id
    repo = bind(client_id) if callable(bind) else repo
    items = [i for i in hitl.list(status=None, client_id=client_id) if i.agent == AGENT_ID]

    interrupted = []
    prefix = f"{AGENT_ID}:{client_id}:"
    # Collected first: SqliteSaver.list() holds the saver's (non-reentrant) lock until the
    # generator is exhausted, and workflow_state() below needs that same lock.
    thread_ids = sorted({c.config["configurable"]["thread_id"] for c in list(agent.deps.checkpointer.list(None))})
    for thread_id in thread_ids:
        if not thread_id.startswith(prefix):
            continue
        state = agent.workflow_state(thread_id)
        if state["pending_nodes"]:
            event = thread_id[len(prefix):].split(":", 1)[0]
            interrupted.append({"workflow_id": thread_id, "event": event, "stage": state.get("stage"),
                                "pending_nodes": state["pending_nodes"],
                                "remedy": _REMEDY.get(event, "resend the event")})

    booking_items = {i.payload.get("id"): i for i in items if i.proposal_type == "session_booking"}
    sessions_without_approval, unreleased = [], []
    for record in repo.list_onboarding_records(client_id=client_id):
        for session in repo.list_sessions(record.employee_id):
            item = booking_items.get(session.id)
            if session.status == SessionStatus.SCHEDULED and item is None:
                sessions_without_approval.append({"session_id": session.id, "employee_id": session.employee_id,
                                                  "session_type": session.session_type})
            elif session.status == SessionStatus.SCHEDULED and item is not None and item.status == Decision.REJECTED:
                unreleased.append({"session_id": session.id, "approval_id": item.id})

    missing = []
    for item in items:
        if item.status != Decision.PENDING or not item.subject_id:
            continue
        try:
            repo.get_onboarding_record(item.subject_id)
            if item.proposal_type == "session_booking":
                repo.get_session(item.payload["id"])
        except KeyError:
            missing.append({"approval_id": item.id, "proposal_type": item.proposal_type,
                            "subject_id": item.subject_id})

    operations = repo.list_operations(client_id=client_id,
                                      statuses=(OperationStatus.PENDING, OperationStatus.UNCERTAIN))
    sections = {
        "interrupted_runs": interrupted,
        "failed_decisions": [{"approval_id": i.id, "proposal_type": i.proposal_type, "subject_id": i.subject_id,
                              "apply_error": i.apply_error} for i in items if i.apply_error],
        "unsettled_operations": [{"key": op.key, "kind": op.kind, "status": op.status, "attempts": op.attempts,
                                  "last_error": op.last_error, "updated_at": op.updated_at.isoformat()}
                                 for op in operations],
        "sessions_without_approval": sessions_without_approval,
        "approvals_without_records": missing,
        "unreleased_rejections": unreleased,
    }
    return {"client_id": client_id, "consistent": not any(sections.values()), **sections}
