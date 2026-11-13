"""UC-02 workflow against a REAL local SQLite file, in two separate processes.

    python demo/uc02_persistent_workflow.py --db local_db/demo.sqlite3 --phase write
    python demo/uc02_persistent_workflow.py --db local_db/demo.sqlite3 --phase verify

``write`` creates a case from synthetic UC-01-style input, plans it, assigns an owner,
completes a task with evidence, records a failure report, replans after an approved
joining-date change, and exits. ``verify`` is a NEW process: it reopens the same file
and checks that everything survived, replays a status update, checks tenant isolation
and keeps working. Each phase prints one JSON line and exits non-zero on any failed check.

SYNTHETIC ONLY. The "UC-03" reporter below is a test-only service identity, not a real
integration, and the client configuration is the synthetic in-process registry.
No Supabase connection is made.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from onboarding_task_management.contracts import (  # noqa: E402
    Actor,
    DecisionOutcome,
    ExternalOutcome,
    Role,
    SourceUseCase,
    StatusUpdate,
    TaskStatus,
)
from onboarding_task_management.errors import DataIsolationError  # noqa: E402
from onboarding_task_management.persistence import OnboardingStore  # noqa: E402
from onboarding_task_management.sqlite_store import SqliteTableClient  # noqa: E402
from onboarding_task_management.sub_agent.service import OnboardingService  # noqa: E402
from onboarding_task_management.synthetic import (  # noqa: E402
    ALPHA_CONFIG,
    CLIENT_ALPHA,
    CLIENT_BETA,
    build_registry,
    make_trigger,
    persona,
)

CASE_ID, CORRELATION_ID, CONFIG_VERSION = "CASE-LOCAL-1", "corr-LOCAL-1", "2026.1-synthetic"
NEW_JOINING = date(2026, 11, 30)
OCCURRED = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
FAILURE_UPDATE_ID = "upd-local-failure-1"

HR = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id=CLIENT_ALPHA)
HR_APPROVER = Actor(actor_id="U-HR-2", role=Role.HR_OPS, client_id=CLIENT_ALPHA)
BETA_HR = Actor(actor_id="U-HR-B1", role=Role.HR_OPS, client_id=CLIENT_BETA)
UC03_REPORTER = Actor(actor_id="svc-uc03-TEST-ONLY", role=Role.SYSTEM, client_id=CLIENT_ALPHA)
PRINCIPALS = {SourceUseCase.UC03_DOCUMENT_VALIDATION: frozenset({UC03_REPORTER.actor_id})}


def open_service(db: Path) -> tuple[OnboardingService, SqliteTableClient]:
    registry = build_registry()
    registry.register(ALPHA_CONFIG.model_copy(update={"version": CONFIG_VERSION}))
    client = SqliteTableClient(db)
    return OnboardingService(
        registry, store=OnboardingStore(client), source_principals=PRINCIPALS
    ), client


def failure_update(oid: str) -> StatusUpdate:
    return StatusUpdate(
        update_id=FAILURE_UPDATE_ID, source=SourceUseCase.UC03_DOCUMENT_VALIDATION,
        client_id=CLIENT_ALPHA, onboarding_id=oid, task_id=f"{oid}#collect_documents",
        outcome=ExternalOutcome.INVALID, occurred_at=OCCURRED,
        blocking_reason="synthetic: scan unreadable", case_id=CASE_ID,
        correlation_id=CORRELATION_ID,
    )


def write(db: Path) -> dict:
    service, client = open_service(db)
    trigger = make_trigger(persona("EMP-007")).model_copy(
        update={"case_id": CASE_ID, "correlation_id": CORRELATION_ID})
    oid = service.create_onboarding(HR, trigger).onboarding_id
    t = lambda key: f"{oid}#{key}"  # noqa: E731
    service.assign_owner(HR, oid, t("hr_record_create"), "U-OPS-7", team="HR Ops")
    service.complete_task(HR, oid, t("hr_record_create"))
    service.complete_task(HR, oid, t("salary_transfer_letter"), "EVID-LOCAL-1")  # evidence required
    failure = service.apply_status_update(UC03_REPORTER, oid, failure_update(oid))
    request = service.request_change(HR, oid, joining_date=NEW_JOINING)
    service.decide_change(HR_APPROVER, request.request_id, DecisionOutcome.APPROVED)
    outcome = service.apply_change(HR, request.request_id)
    record = service.get(HR, oid)
    summary = {
        "phase": "write", "onboarding_id": oid, "tasks": len(record.tasks),
        "revision": record.revision, "rescheduled": len(outcome.rescheduled_task_ids),
        "failure_accepted": failure.accepted, "approval_request_id": request.request_id,
        "audit_events": len(service.audit.events_for(HR)),
        "rows": {tbl: client.row_count(tbl) for tbl in
                 ("onboarding", "task", "task_dependency", "plan_revision", "approval_request",
                  "audit_event", "client_config_ref")},
    }
    client.close()
    return summary


def verify(db: Path, oid: str) -> dict:
    checks: dict[str, bool] = {}
    service, client = open_service(db)
    assert service.hydrate(CLIENT_ALPHA) == 1
    record = service.get(HR, oid)
    task = record.task_by_key
    checks["case_found"] = record.onboarding_id == oid
    checks["case_and_correlation_ids"] = (record.case_id, record.correlation_id) == (CASE_ID, CORRELATION_ID)
    checks["config_version"] = record.config_version == CONFIG_VERSION
    checks["employee_reference"] = record.employee_id == "EMP-007"
    checks["tasks_and_dependencies"] = (
        len(record.tasks) > 5
        and task("collect_documents").depends_on == [f"{oid}#hr_record_create"]
        and any(t.depends_on for t in record.tasks))
    checks["owner_assignment"] = (task("hr_record_create").assignee_id, task("hr_record_create").assignee_team) == ("U-OPS-7", "HR Ops")
    checks["completed_with_evidence"] = (
        task("salary_transfer_letter").status is TaskStatus.COMPLETE
        and task("salary_transfer_letter").evidence_ref == "EVID-LOCAL-1"
        and task("salary_transfer_letter").completed_at is not None)
    checks["failure_state"] = (
        task("collect_documents").external_outcome is ExternalOutcome.INVALID
        and task("collect_documents").external_blocking_reason == "synthetic: scan unreadable"
        and task("collect_documents").status is not TaskStatus.COMPLETE)
    checks["replan_survived"] = record.revision == 1 and record.joining_date == NEW_JOINING
    checks["idempotency_ledger"] = FAILURE_UPDATE_ID in record.applied_updates
    revisions = service.store.plan_revisions(CLIENT_ALPHA, oid)
    checks["plan_revision_row"] = len(revisions) == 1 and revisions[0]["drivers"] == ["joining_date"]
    approvals = service.store.load_approvals(CLIENT_ALPHA)
    checks["approval_history"] = len(approvals) == 1 and approvals[0].outcome is DecisionOutcome.APPROVED \
        and approvals[0].decided_by == HR_APPROVER.actor_id
    events = service.audit.events_for(HR)
    actions = [e.action for e in events]
    checks["audit_history"] = all(a in actions for a in (
        "onboarding.create", "task.assign_owner", "task.complete", "task.status_update",
        "change.request", "change.decide", "change.apply"))
    checks["audit_timestamps_and_ids"] = all(e.at.tzinfo is not None for e in events) \
        and len({e.event_id for e in events}) == len(events)
    checks["audit_correlation"] = all(e.correlation_id == CORRELATION_ID for e in events if e.onboarding_id == oid)

    before = len(events)
    replay = service.apply_status_update(UC03_REPORTER, oid, failure_update(oid))
    checks["replay_is_duplicate"] = replay.accepted and replay.duplicate_replay
    checks["replay_adds_no_audit_event"] = len(service.audit.events_for(HR)) == before \
        and client.row_count("audit_event") == before

    try:  # BETA has not been loaded in this process; storage still reports the breach
        service.get(BETA_HR, oid)
        checks["tenant_isolation"] = False
    except DataIsolationError:
        checks["tenant_isolation"] = service.hydrate(CLIENT_BETA) == 0 \
            and service.store.list_onboardings(CLIENT_BETA) == []

    # The restarted service keeps working: one more completion is written through, once.
    rows_before = client.row_count("audit_event")
    target = next(t for t in service.ready_tasks(HR, oid) if not t.evidence_required)
    service.complete_task(HR, oid, target.task_id)
    stored = service.store.load_onboarding(CLIENT_ALPHA, oid).task_by_id(target.task_id)
    checks["service_continues_after_restart"] = (
        stored.status is TaskStatus.COMPLETE and client.row_count("audit_event") == rows_before + 1)
    client.close()
    return {"phase": "verify", "onboarding_id": oid, "checks": checks, "ok": all(checks.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--phase", required=True, choices=("write", "verify"))
    parser.add_argument("--onboarding-id", default="ONB-CLIENT-ALPHA-EMP-007")
    args = parser.parse_args()
    result = write(args.db) if args.phase == "write" else verify(args.db, args.onboarding_id)
    print(json.dumps(result, default=str, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
