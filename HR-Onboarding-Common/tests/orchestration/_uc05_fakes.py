"""A recording stand-in for the UC05 worker, for adapter unit tests. Its replies have exactly the
shape the real worker returns (uc_worker_runtime._Uc05Handler, verified against a live UC05 run);
only UC05 itself is replaced, the adapter code under test is the real one."""

from __future__ import annotations

from typing import Any

from hr_onboarding_common.orchestration.contracts import WorkerResponse

EMPLOYEE, CLIENT, TEAM_LEAD = "EMP-TEST-001", "CLIENT-TEST-A", "TL-TEST-001"


class FakeUc05Worker:
    """`responses[op]` is a reply (dict), a list of replies consumed in order, a WorkerResponse, an
    exception to raise, or a callable(payload, actor) returning any of those."""

    is_alive = True

    def __init__(self, **responses: Any):
        self.responses = responses
        self.calls: list[tuple[str, dict, Any]] = []

    def ops(self) -> list[str]:
        return [op for op, _, _ in self.calls]

    def call(self, op, payload, *, actor, run_id=None, idempotency_key=None, timeout_s=None) -> WorkerResponse:
        self.calls.append((op, payload, actor))
        reply = self.responses[op]
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        if callable(reply) and not isinstance(reply, type):
            reply = reply(payload, actor)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, WorkerResponse):
            return reply
        return WorkerResponse(run_id=run_id or "r", ok=True, result=reply)


class Pool:
    def __init__(self, **workers):
        self.workers = workers

    def get(self, uc_id):
        return self.workers[uc_id]


def failure(code: str, detail: str = "refused", error_type: str = "Error") -> WorkerResponse:
    return WorkerResponse(run_id="r", ok=False, error_code=code, error_detail=detail, error_type=error_type,
                          retryable=code == "RETRYABLE_PROVIDER_ERROR")


def preflight(**over) -> dict:
    return {"status": "ok", "event_id": "EVT-001", "employee_id": EMPLOYEE, "joining_date": "2026-12-01",
            "missing_fields": [], "already_processed": False, **over}


def event_run(stage: str, *, errors: list | None = None, actions: list | None = None) -> dict:
    return {"response": {"run_id": "run-1", "status": "acted", "actions_taken": actions or [], "proposals": [],
                         "exceptions": []},
            "workflow": {"stage": stage, "errors": errors or [], "history": [], "pending_nodes": []}}


def session(session_type: str, status: str, session_id: str = "session-1",
            start: str = "2026-12-01T09:00:00+04:00") -> dict:
    return {"session_id": session_id, "session_type": session_type, "status": status, "scheduled_start": start,
            "booking_operation_key": f"calendar:{CLIENT}:{EMPLOYEE}:{session_type}:2026-12-01T05:00:00+00:00"}


def approval(approval_id: str, proposal_type: str, *, status: str = "pending", record_id: str | None = None,
             approver_role: str = "hr_admin", gate: str = "scheduling_approval", session_type: str | None = None,
             buddy_id: str | None = None, apply_error: str | None = None,
             created_at: str = "2026-10-29T05:00:00+00:00") -> dict:
    return {"approval_id": approval_id, "gate": gate, "proposal_type": proposal_type, "status": status,
            "approver_role": approver_role, "subject_id": EMPLOYEE, "created_at": created_at,
            "decided_at": None if status == "pending" else "2026-10-29T06:00:00+00:00",
            "apply_error": apply_error, "apply_error_detail": f"{apply_error}: detail" if apply_error else None,
            "record_id": record_id, "session_type": session_type, "buddy_id": buddy_id}


def snapshot(**over) -> dict:
    base = {"found": True, "employee_id": EMPLOYEE, "client_id": CLIENT, "case_status": "active",
            "start_date": "2026-12-01", "buddy_id": None, "stage": "case_created", "sessions": [], "approvals": [],
            "superseded_approvals": [], "welcome_drafts": [], "operations": []}
    return {**base, **over}


def decision_outcome(approval_view: dict, *, applied: bool = True, run_stage: str | None = None,
                     meeting_type: str | None = None, errors: list | None = None) -> dict:
    return {"approval": approval_view, "applied": applied, "apply_error_type": None if applied else "Error",
            "run": {"stage": run_stage, "meeting_type": meeting_type, "pending_nodes": [], "errors": errors or []}}
