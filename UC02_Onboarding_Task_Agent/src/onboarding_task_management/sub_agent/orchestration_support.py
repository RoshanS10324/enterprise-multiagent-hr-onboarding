"""What UC-02 records and reports when the central orchestrator runs one of its stages.

The orchestrator (HR-Onboarding-Common ``adapters/uc02_tasks.py``, ``uc02_replanning.py``) calls UC-02 in-process
and keeps the authoritative workflow state itself (``case_orchestration_state``). UC-02 keeps its own business
records (tasks, dependencies, plan revisions) and, through the helpers below, its own trace of each orchestrated
execution -- in the same store as everything else UC-02 writes, so in Supabase mode in ``audit_events`` and
``agent_runs``, scoped to the case's client:

* :func:`record_stage_event` -- one ``audit_events`` entry per transition: ``started``, then ``completed``,
  ``replanned``, ``blocked``, ``failed`` or ``retry_pending``. Action ``orchestration.<stage>``.
* :func:`record_stage_run` -- one ``agent_runs`` row per execution, keyed by the orchestrator's execution id
  (idempotent: the same execution never produces a second row). ``agent_runs.status`` accepts only
  ``proposed | acted | exception`` (live CHECK), so a blocked or failed execution is an ``exception`` with the
  error code in ``error_summary``.
* :func:`plan_projection` -- the plan as the orchestrator persists it: ids, statuses, dates, the dependency
  graph and counts. Built from UC-02's authoritative record, never from a cached view.

Identifiers, codes and counts only: no names, contact details or free text reach these records. Nothing here
decides a workflow transition: UC-02 returns its result and the orchestrator decides what happens next.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from onboarding_task_management.agent.loop import AGENT_ID, AGENT_VERSION
from onboarding_task_management.contracts import Actor, OnboardingRecord

#: Outcome of an orchestrated execution -> ``agent_runs.status`` (the live CHECK allows proposed | acted | exception).
RUN_STATUS = {"completed": "acted", "replanned": "acted", "blocked": "exception", "failed": "exception",
              "retry_pending": "exception"}
OUTCOMES = frozenset({"started", *RUN_STATUS})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def store_mode(service) -> str:
    """Where this UC-02 service keeps its records: ``supabase``, ``sqlite`` or ``memory`` (not durable)."""
    store = getattr(service, "store", None)
    if store is None:
        return "memory"
    client = store.client
    if getattr(client, "uses_case_ids", False):
        return "supabase"
    return "sqlite" if type(client).__name__ == "SqliteTableClient" else "memory"


def record_stage_event(service, actor: Actor, *, stage: str, outcome: str, case_id: str, record_exists: bool,
                       correlation_id: str | None = None, execution_id: str | None = None, code: str | None = None,
                       **counts: int) -> None:
    """One audit entry for an orchestrated stage transition. ``record_exists``: the onboarding (the UC-01 case)
    is known to UC-02's store, so the entry is linked to it; otherwise the case id is kept as a plain reference
    (an entry for a case the database does not hold could not be stored against it)."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown orchestrated stage outcome {outcome!r}")
    detail: dict[str, Any] = {"stage": stage, **{k: int(v) for k, v in counts.items()}}
    if code:
        detail["code"] = code
    if execution_id:
        detail["execution_id"] = execution_id
    if not record_exists:
        detail["case_ref"] = case_id
    service.audit.record(actor, f"orchestration.{stage}", outcome,
                         onboarding_id=case_id if record_exists else None, correlation_id=correlation_id, **detail)


def record_stage_run(service, actor: Actor, *, run_id: str, stage: str, operation: str, outcome: str,
                     case_id: str, record_exists: bool, started_at: str, code: str | None = None,
                     action_count: int = 0) -> bool:
    """One ``agent_runs`` row for an orchestrated execution. Returns False when the service has no durable store
    (memory mode keeps no run rows; the audit entries are still kept in memory)."""
    if outcome not in RUN_STATUS:
        raise ValueError(f"unknown orchestrated stage outcome {outcome!r}")
    if service.store is None:
        return False
    status = RUN_STATUS[outcome]
    service.store.save_agent_run({
        "run_id": run_id, "onboarding_id": case_id if record_exists else None, "client_id": actor.client_id,
        "agent_id": AGENT_ID, "agent_version": AGENT_VERSION, "actor_id": actor.actor_id, "status": status,
        "advisor": "orchestrated", "model": None, "proposal_count": 0, "action_count": int(action_count),
        "exception_count": 0 if status == "acted" else 1, "tool_calls": [operation],
        "started_at": started_at, "finished_at": _now(),
        "error_summary": f"{stage}:{code}"[:200] if code else None,
    })
    return True


def plan_projection(record: OnboardingRecord, *, required_task_ids: Iterable[str] = (),
                    store: str | None = None) -> dict[str, Any]:
    """The plan of one onboarding as the orchestrator persists it (``task_plan`` / stage result summary).

    ``required_task_ids``: the tasks the client's milestones require (for example ``day_one_ready``) -- the
    client's own definition of what must be done; empty when the configuration declares no milestone.
    """
    required = set(required_task_ids)
    by_id = {t.task_id: t for t in record.tasks}
    dependencies = [{"task_id": t.task_id, "template_key": t.template_key, "depends_on_task_id": d,
                     "depends_on_template_key": by_id[d].template_key if d in by_id else None}
                    for t in record.tasks for d in t.depends_on]
    return {
        "plan_version": record.plan_version,
        "revision": record.revision,
        "config_version": record.config_version or None,
        "data_origin": record.data_origin or None,
        "store": store,
        "task_count": len(record.tasks),
        "required_task_count": len(required & set(by_id)),
        "required_task_keys": sorted(by_id[t].template_key for t in required & set(by_id)),
        "dependency_count": len(dependencies),
        "dependencies": dependencies,
        "document_count": len(record.documents),
    }


__all__ = ["OUTCOMES", "RUN_STATUS", "plan_projection", "record_stage_event", "record_stage_run", "store_mode"]
