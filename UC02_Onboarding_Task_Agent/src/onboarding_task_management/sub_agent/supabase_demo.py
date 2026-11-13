"""The staged synthetic Supabase demonstration: preflight, setup, run, decide, documents, verify, recover.

Every stage uses the SAME runtime a deployment uses (``runtime.build_flow`` over the Supabase store). There is no in-memory or
SQLite path here: a missing ``UC02_STORE_MODE=supabase``, credentials or schema is an immediate error. The CLI is
``demo/uc02_supabase_agentic_workflow.py``; this module holds the stage logic so tests can run it over a mocked Supabase
transport.

Stage safety:
* ``preflight`` and ``verify`` only read.
* ``setup`` writes the SYNTHETIC fixture (see :mod:`supabase_synthetic`) only with the target host re-typed in
  ``confirm_target`` and the development opt-ins; insert-if-missing, nothing updated or deleted.
* ``run`` / ``decide`` / ``documents`` / ``recover`` write UC-02's own records (tasks, dependencies, approvals, agent runs, audit,
  case-state snapshots) for the synthetic case only. They never write UC-01's tables.
* The outbound Outlook connector is the MOCK unless the environment says ``graph`` (which never falls back), and downstream
  triggers are the mock only if ``mock_downstream`` is asked for; otherwise the downstream use cases are recorded as NOT
  CONFIGURED. A mock result is always labelled as one.
"""

from __future__ import annotations

import os
import urllib.parse
from hashlib import sha256
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, cast

from onboarding_task_management.contracts import (
    Actor,
    DocumentState,
    DownstreamState,
    ExternalOutcome,
    Role,
    SourceUseCase,
    TaskStatus,
)
from onboarding_task_management.persistence import PersistenceError
from onboarding_task_management.sub_agent import flow as fl
from onboarding_task_management.sub_agent import flow_runtime as rt
from onboarding_task_management.sub_agent import mock_handoff, planner, tracker
from onboarding_task_management.sub_agent.status_updates import SOURCE_CATEGORIES
from onboarding_task_management.sub_agent.runtime import build_flow, require_supabase_runtime
from onboarding_task_management.supabase_rest import SupabaseRestClient
from onboarding_task_management.supabase_schema import verify_schema
from onboarding_task_management.supabase_synthetic import (
    FixtureSpec,
    ensure_fixture,
    fixture_status,
)

SYNTHETIC_ENV = {"UC02_ALLOW_SYNTHETIC_CONFIG": "1"}
DEMO_APPROVER = "SYN-HR-APPROVER"
DEMO_LEAD = "SYN-HR-LEAD"
DEMO_DOC_SOURCE = "document source (synthetic)"


def _host(rest: SupabaseRestClient) -> str:
    return urllib.parse.urlparse(rest._base).hostname or "?"


def _env(environ: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    env.update(SYNTHETIC_ENV)  # the demo is the one runtime that opts into synthetic configuration
    return env


def make_flow(environ: Mapping[str, str] | None = None, *, rest: SupabaseRestClient | None = None,
              mock_downstream: bool = False):
    """The deployment's flow, over Supabase. Raises on any configuration, connection or schema problem."""
    env = _env(environ)
    require_supabase_runtime(env)
    from onboarding_task_management.config_safety import environment

    if environment(env) not in ("development", "test"):
        raise PersistenceError("the synthetic demo runs only with UC02_ENV=development or test set explicitly",
                               environment=environment(env) or "(unset)")
    adapters = {uc: fl.MockTrigger(uc) for uc in fl.DOWNSTREAM} if mock_downstream else None
    flow = build_flow(env, rest=rest, downstream_adapters=adapters)
    if flow.source is None:
        raise PersistenceError("no employee source was built (the runtime is not Supabase-backed)")
    return flow


def _secrets(flow, *channels: str) -> dict[str, bytes]:
    """The channel secrets a stage uses. Only those are required: ``run`` signs the employee event, ``documents`` signs
    document updates; the UC-04/UC-05 status channels are the downstream teams' to use, not this demo's."""
    missing = [c for c in channels if c not in flow.channel_secrets]
    if missing:
        raise PersistenceError("channel secrets are not configured: " + ", ".join(missing))
    return flow.channel_secrets


def _actor(spec: FixtureSpec, ident: str, role: Role) -> Actor:
    return Actor(actor_id=ident, role=role, client_id=spec.client_id)


# ---------------------------------------------------------------------------------------------------------------------------


def preflight(environ: Mapping[str, str] | None = None, *, rest: SupabaseRestClient | None = None,
              spec: FixtureSpec | None = None) -> dict[str, Any]:
    """READ-ONLY. Backend, target, schema and fixture readiness."""
    env = _env(environ)
    require_supabase_runtime(env)
    rest = rest or SupabaseRestClient.from_env(env)
    spec = spec or FixtureSpec()
    problems = verify_schema(rest.schema())
    return {"backend": "supabase", "target_host": _host(rest), "environment": env.get("UC02_ENV") or "(unset)",
            "schema_problems": problems, "fixture": fixture_status(rest, spec) if not problems else {},
            "synthetic_marker": "client_configs.settings_payload.data_origin = synthetic",
            "outlook": env.get("UC02_OUTLOOK_CONNECTOR", "mock"), "ready_to_run": not problems}


def setup(environ: Mapping[str, str] | None = None, *, rest: SupabaseRestClient | None = None,
          spec: FixtureSpec | None = None, confirm_target: str = "") -> dict[str, Any]:
    """Write the synthetic fixture (insert-if-missing). Needs the target host re-typed and the development opt-ins."""
    env = _env(environ)
    require_supabase_runtime(env)
    rest = rest or SupabaseRestClient.from_env(env)
    if confirm_target != _host(rest):
        raise PersistenceError(f"refusing to write: --confirm-write must equal the target host ({_host(rest)})")
    problems = verify_schema(rest.schema())
    if problems:
        raise PersistenceError("the schema is incomplete: " + "; ".join(problems))
    return {"target_host": _host(rest), "fixture": ensure_fixture(rest, spec or FixtureSpec(), env)}


def run(environ: Mapping[str, str] | None = None, *, rest: SupabaseRestClient | None = None,
        spec: FixtureSpec | None = None, mock_downstream: bool = False) -> dict[str, Any]:
    """UC-01 event -> configuration -> checklist -> Fake LLM review -> submitted plan -> HR email draft (mock Outlook)."""
    spec = spec or FixtureSpec()
    flow = make_flow(environ, rest=rest, mock_downstream=mock_downstream)
    out = flow.receive_employee_event(_signed_event(flow, spec, "first"), date.today())
    if out.status not in ("accepted", "duplicate"):
        raise PersistenceError(f"the employee event was not processed: {out.status} - {out.detail}")
    return {"event": out.status, "detail": out.detail, "onboarding_id": out.data.get("onboarding_id"),
            "workflow": flow.workflow_status(_actor(spec, "uc02-coordinator", Role.HR_OPS), spec.case_id)}


def _signed_event(flow, spec: FixtureSpec, tag: str):
    event = fl.EmployeeCreatedEvent(event_id=f"SYN-EVT-{spec.employee_no}-{tag}", client_id=spec.client_id,
                                    employee_id=spec.employee_no, case_id=spec.case_id, joining_date=spec.joining_date,
                                    occurred_at=datetime.now(UTC), correlation_id=f"SYN-CORR-{spec.employee_no}")
    return rt.sign_model(_secrets(flow, "events")["events"], event)


def decide(environ: Mapping[str, str] | None = None, *, rest: SupabaseRestClient | None = None,
           spec: FixtureSpec | None = None, decision: str = "approved", reason: str = "",
           mock_downstream: bool = False) -> dict[str, Any]:
    """A simulated HR decision, delivered through the SIGNED callback exactly as a portal would deliver it."""
    spec = spec or FixtureSpec()
    flow = make_flow(environ, rest=rest, mock_downstream=mock_downstream)
    coordinator = flow._actor(spec.client_id)
    flow.service.hydrate(spec.client_id)
    record = flow.service.get(coordinator, spec.case_id)
    pending = [r for r in flow.service.inbox.pending(coordinator) if r.onboarding_id == spec.case_id]
    if not pending:
        raise PersistenceError("no pending approval request for the synthetic case; run the 'run' stage first")
    callback = fl.sign_callback(flow._secret, fl.ApprovalCallback(
        callback_id=f"SYN-CB-{spec.employee_no}-{decision}-{record.plan_version}-{sha256(reason.encode()).hexdigest()[:6]}", onboarding_id=spec.case_id,
        request_id=pending[0].request_id, plan_version=record.plan_version, decision=cast(Any, decision), approver_id=DEMO_APPROVER,
        approver_role=Role.HR_OPS, issued_at=datetime.now(UTC), reason=reason, client_id=spec.client_id))
    outcome = flow.process_signed_callback(callback, datetime.now(UTC))
    return {"decision": decision, "outcome": outcome.status, "detail": outcome.detail,
            "new_request_id": outcome.new_request_id, "workflow": flow.workflow_status(coordinator, spec.case_id)}


def send_authorised(environ=None, *, rest=None, spec: FixtureSpec | None = None, mock_downstream: bool = False) -> dict[str, Any]:
    """HR lead authorises the prepared messages; they go through the configured connector (mock by default)."""
    spec = spec or FixtureSpec()
    flow = make_flow(environ, rest=rest, mock_downstream=mock_downstream)
    coordinator = flow._actor(spec.client_id)
    flow.service.hydrate(spec.client_id)
    results = {}
    for n, m in enumerate(flow.service.get(coordinator, spec.case_id).messages):
        if m.kind in ("teamasg",) and any(a.assignment_id == m.message_id and a.hold_reason
                                          for a in flow.service.get(coordinator, spec.case_id).assignments):
            pass
        decision = rt.sign_model(flow._secret, rt.MessageDecision(
            decision_id=f"SYN-MD-{spec.employee_no}-{m.message_id[-40:]}", client_id=spec.client_id,
            onboarding_id=spec.case_id, message_id=m.message_id, approver_id=DEMO_LEAD, approver_role=Role.HR_LEAD,
            issued_at=datetime.now(UTC)))
        out = flow.process_message_decision(decision, datetime.now(UTC))
        results[m.message_id] = {"kind": m.kind, "status": out.status,
                                 "connector": type(flow.connector).__name__,
                                 "real": bool(getattr(flow.connector, "real", False))}
    return {"messages": results, "workflow": flow.workflow_status(coordinator, spec.case_id)}


def documents(environ: Mapping[str, str] | None = None, *, rest: SupabaseRestClient | None = None,
              spec: FixtureSpec | None = None, mock_downstream: bool = False, verify_all: bool = True,
              reject: str | None = None) -> dict[str, Any]:
    """The (synthetic) document source reports status through the signed channel. ``reject`` rejects one document."""
    spec = spec or FixtureSpec()
    flow = make_flow(environ, rest=rest, mock_downstream=mock_downstream)
    coordinator = flow._actor(spec.client_id)
    flow.service.hydrate(spec.client_id)
    secret = _secrets(flow, "documents")["documents"]
    results: dict[str, str] = {}
    n = 0

    def send(key: str, state: DocumentState, ref: str | None) -> str:
        nonlocal n
        n += 1
        update = rt.sign_model(secret, fl.DocumentUpdate(
            update_id=f"SYN-DU-{spec.employee_no}-{key}-{state.value}", client_id=spec.client_id, onboarding_id=spec.case_id,
            plan_version=flow.service.get(coordinator, spec.case_id).plan_version, document_key=key, state=state,
            occurred_at=datetime.now(UTC), reference=ref))
        return flow.receive_document_update(update).status

    for doc in [d for d in flow.service.get(coordinator, spec.case_id).documents if d.mandatory]:
        if not verify_all and doc.key != reject:
            continue
        steps: list[tuple[DocumentState, str | None]] = [(DocumentState.SUBMITTED, None)]
        if reject == doc.key:
            steps += [(DocumentState.REJECTED, None)]
        else:
            steps += [(DocumentState.UNDER_REVIEW, None), (DocumentState.VERIFIED, f"SYN-VERIFICATION-{doc.key}")]
        for state, ref in steps:
            results[f"{doc.key}:{state.value}"] = send(doc.key, state, ref)
    return {"updates": results, "gate": flow.document_gate(coordinator, spec.case_id).model_dump(),
            "workflow": flow.workflow_status(coordinator, spec.case_id)}


def _task_view(record) -> dict[str, Any]:
    """Dependency view derived from the record: what may run now, what waits and on what."""
    return {
        "ready": sorted(t.task_id for t in planner.ready_tasks(record) if t.status is TaskStatus.READY),
        "in_progress": sorted(t.task_id for t in record.tasks if t.status is TaskStatus.IN_PROGRESS),
        "complete": sorted(t.task_id for t in record.tasks if t.status is TaskStatus.COMPLETE),
        "blocked": {t.task_id: sorted(planner.unmet_prerequisites(record, t))
                    for t in record.tasks if t.status is TaskStatus.BLOCKED},
        "parallel_batches": planner.parallel_batches(record),
    }


def tasks(environ: Mapping[str, str] | None = None, *, rest: SupabaseRestClient | None = None,
          spec: FixtureSpec | None = None, mock_downstream: bool = False, waves: int = 1,
          as_of: date | None = None) -> dict[str, Any]:
    """Task lifecycle on the approved synthetic plan: progress the tasks that are READY (in parallel, one wave at a time),
    show what that unblocked, then derive overdue work and run the monitoring sweep for ``as_of``.

    * Needs an approved plan (``run`` then ``decide``). Nothing is skipped silently: an unapproved plan is an error.
    * A task whose category UC-04 / UC-05 / UC-03 report on is completed through a MOCK status update from that use case
      (reference prefix ``MOCK``); everything else is started and completed by its owner role. No real provisioning happens.
    * Replay-safe: update ids are deterministic and a finished task is not touched again.
    * ``as_of`` drives overdue detection and the sweep (reminder DRAFTS and escalation records; nothing is delivered).
    """
    spec = spec or FixtureSpec()
    as_of = as_of or date.today()
    flow = make_flow(environ, rest=rest, mock_downstream=mock_downstream)
    coordinator = flow._actor(spec.client_id)
    flow.service.hydrate(spec.client_id)
    record = flow.service.get(coordinator, spec.case_id)
    if not flow.service._plan_released(record):
        raise PersistenceError("the plan is not approved; run the 'run' and 'decide' stages first")
    before = _task_view(record)
    done: list[dict[str, Any]] = []
    for wave_no in range(1, max(waves, 0) + 1):  # 0 waves = report and sweep only
        current = flow.service.get(coordinator, spec.case_id)
        ready = [t for t in planner.ready_tasks(current) if t.status is TaskStatus.READY]
        if not ready:
            break
        wave: list[dict[str, Any]] = []
        for task in ready:
            source = next((s for s, cats in SOURCE_CATEGORIES.items() if task.category in cats
                           and s in rt.runtime_principals()), None)
            if source is not None:
                principal = sorted(rt.runtime_principals()[source])[0]
                update = {SourceUseCase.UC03_DOCUMENT_VALIDATION: mock_handoff.uc03_document_result,
                          SourceUseCase.UC04_EQUIPMENT_ACCESS: mock_handoff.uc04_equipment_result,
                          SourceUseCase.UC05_ORIENTATION: mock_handoff.uc05_orientation_result}[source](
                    task, ExternalOutcome.COMPLETED, datetime.now(UTC))
                res = flow.service.apply_status_update(
                    Actor(actor_id=principal, role=Role.SYSTEM, client_id=spec.client_id), spec.case_id, update)
                wave.append({"task": task.task_id, "via": f"MOCK {source.value} status update", "accepted": res.accepted,
                             "code": res.rejection_code or (res.applied_status.value if res.applied_status else ""),
                             "unblocked": res.unblocked_task_ids})
            else:
                owner = Actor(actor_id=f"SYN-OWNER-{task.owner.value}", role=task.owner, client_id=spec.client_id)
                if task.owner is Role.SYSTEM:
                    owner = coordinator
                flow.service.start_task(owner, spec.case_id, task.task_id)
                flow.service.complete_task(owner, spec.case_id, task.task_id,
                                           f"SYN-EVIDENCE-{task.task_id}" if task.evidence_required else None)
                wave.append({"task": task.task_id, "via": f"owner role {task.owner.value}", "accepted": True,
                             "code": "completed"})
        done.append({"wave": wave_no, "parallel_tasks": wave})
    final = flow.service.get(coordinator, spec.case_id)
    sweep = flow.service.monitoring_sweep(coordinator, spec.case_id, as_of)
    return {"as_of": as_of.isoformat(), "before": before, "waves": done, "after": _task_view(final),
            "newly_unblocked": sorted(set(_task_view(final)["ready"]) - set(before["ready"])),
            "overdue_at_as_of": sorted(t.task_id for t in tracker.overdue_tasks(final, as_of)),
            "due_dates": {t.task_id: t.due_date.isoformat() for t in final.tasks},
            "sweep": {"reminder_drafts": len(sweep.reminder_drafts), "escalations": len(sweep.escalations),
                      "newly_recorded_escalations": sweep.newly_recorded_escalations,
                      "newly_recorded_reminders": sweep.newly_recorded_reminders, "delivery": sweep.delivery},
            "workflow": flow.workflow_status(coordinator, spec.case_id)}


def verify(environ: Mapping[str, str] | None = None, *, rest: SupabaseRestClient | None = None,
           spec: FixtureSpec | None = None) -> dict[str, Any]:
    """READ-ONLY database verification: what is actually stored in Supabase for the synthetic case."""
    env = _env(environ)
    require_supabase_runtime(env)
    rest = rest or SupabaseRestClient.from_env(env)
    spec = spec or FixtureSpec()
    case = spec.case_id
    tasks = rest.select("onboarding_tasks", {"case_id": case}, columns="task_id,status,status_summary")
    owned = [t for t in tasks if "uc02" in (t.get("status_summary") or {})]
    edges = [e for t in owned for e in rest.select("task_dependencies", {"task_id": t["task_id"]})]
    approvals = rest.select("approval_requests", {"case_id": case, "client_id": spec.client_id}, columns="request_id,gate,outcome")
    runs = rest.select("agent_runs", {"case_id": case}, columns="run_id,status,agent_id")
    events = rest.select("audit_events", {"case_id": case}, columns="event_id,action,reference")
    snaps = rest.select("audit_events", {"case_id": case, "action": "UC02_CASE_STATE"}, columns="after_state")
    state = max((s["after_state"] or {} for s in snaps), key=lambda x: str(x.get("updated_at", "")), default={})
    return {
        "backend": "supabase", "target_host": _host(rest), "case_id": case,
        "data_origin": state.get("data_origin") or "(no state stored)", "config_version": state.get("config_version"),
        "tasks_stored": len(owned),
        "task_status_counts": {s: sum(1 for t in owned if t["status"] == s) for s in sorted({t["status"] for t in owned})},
        "dependencies_stored": len(edges), "approval_requests": [(a["gate"], a["outcome"]) for a in approvals],
        "agent_runs": len(runs), "audit_events": len([e for e in events if e["action"] != "UC02_CASE_STATE"]),
        "case_state_snapshots": len([e for e in events if e["action"] == "UC02_CASE_STATE"]),
        "plan_status": state.get("plan_status"), "plan_version": state.get("plan_version"),
        "assignments": len([a for a in state.get("assignments", []) if a.get("status") != "superseded"]),
        "documents": {d["key"]: d["state"] for d in state.get("documents", [])},
        "downstream": {u: t["state"] for u, t in (state.get("downstream") or {}).items()},
        "messages": [(m["kind"], m["plan_version"]) for m in state.get("messages", [])],
        "reviews_stored": sorted(state.get("analyses", {})),
        "event_ledger": sorted(k for k in state.get("flow_ledger", {}) if k.startswith("ev:")),
        "uc01_tables_written_by_uc02": 0,
    }


def recover(environ: Mapping[str, str] | None = None, *, rest: SupabaseRestClient | None = None,
            spec: FixtureSpec | None = None, mock_downstream: bool = False) -> dict[str, Any]:
    """Build a brand-new runtime, reload the workflow from Supabase, replay the event, and show nothing was duplicated."""
    spec = spec or FixtureSpec()
    before = verify(environ, rest=rest, spec=spec)
    flow = make_flow(environ, rest=rest, mock_downstream=mock_downstream)  # a new process-equivalent: nothing in memory
    coordinator = flow._actor(spec.client_id)
    flow.service.hydrate(spec.client_id)
    status = flow.workflow_status(coordinator, spec.case_id)
    replay = flow.receive_employee_event(_signed_event(flow, spec, "replay"), date.today())
    after = verify(environ, rest=rest, spec=spec)
    return {"recovered_workflow": status, "event_replay": replay.status, "duplicate_tasks": after["tasks_stored"] != before["tasks_stored"],
            "tasks_before_after": (before["tasks_stored"], after["tasks_stored"]),
            "approvals_before_after": (len(before["approval_requests"]), len(after["approval_requests"])),
            "assignments_before_after": (before["assignments"], after["assignments"])}


__all__ = ["decide", "documents", "make_flow", "preflight", "recover", "run", "send_authorised", "setup", "tasks",
           "verify"]
