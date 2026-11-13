"""UC-02 business workflow, scenarios A-G, on REAL local SQLite files, with real output.

    python demo/uc02_business_workflow.py --dir local_db/business --phase run
    python demo/uc02_business_workflow.py --dir local_db/business --phase verify   # NEW process

Each scenario uses its own database file, the real application factory
(``build_service`` -> ``OnboardingService`` with the HR plan-approval gate ON) and the
real service methods. What is SYNTHETIC and labelled as such:

  * the employee intake (UC-01 is not invoked; the trigger is synthetic);
  * the UC-03 / UC-04 / UC-05 status events (``svc-ucNN-SYNTHETIC`` test identities sent
    through the real ``apply_status_update``; the real agents are not called);
  * the team members (``U-*`` ids are made up) and the client configuration (the
    synthetic in-process registry).

Reminders are DRAFTS and escalations are RECORDS: nothing is delivered anywhere, and there
is no scheduler - the sweep runs only when this script calls it, with an explicit test clock.
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
    Actor, DecisionOutcome, ExternalOutcome, FinalStatus, PlanStatus, Role, SourceUseCase,
    StatusUpdate, TaskStatus,
)
from onboarding_task_management.errors import (  # noqa: E402
    EvidenceRequiredError, InvalidStateError, PlanNotApprovedError, UnauthorizedActionError,
)
from onboarding_task_management.sub_agent.runtime import build_service  # noqa: E402
from onboarding_task_management.synthetic import CLIENT_ALPHA, make_trigger, persona  # noqa: E402

CLOCK0 = date(2026, 11, 2)  # explicit test clock; nothing here reads the wall clock
OCCURRED = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
PRINCIPALS = {
    SourceUseCase.UC03_DOCUMENT_VALIDATION: frozenset({"svc-uc03-SYNTHETIC"}),
    SourceUseCase.UC04_EQUIPMENT_ACCESS: frozenset({"svc-uc04-SYNTHETIC"}),
    SourceUseCase.UC05_ORIENTATION: frozenset({"svc-uc05-SYNTHETIC"}),
}


def actor(actor_id: str, role: Role) -> Actor:
    return Actor(actor_id=actor_id, role=role, client_id=CLIENT_ALPHA)


HR = actor("U-HR-1", Role.HR_OPS)  # requester / operator
HR_APPROVER = actor("U-HR-2", Role.HR_OPS)  # the plan/final reviewer (cannot be the requester)
TEAM = {  # synthetic team members, one per configured owner role
    Role.HR_OPS: HR, Role.IT_MANAGER: actor("U-IT-1", Role.IT_MANAGER),
    Role.FACILITIES: actor("U-FAC-1", Role.FACILITIES), Role.FINANCE: actor("U-FIN-1", Role.FINANCE),
    Role.EMPLOYEE: actor("U-EMP-1", Role.EMPLOYEE), Role.HR_OFFICER: actor("U-HRO-1", Role.HR_OFFICER),
    Role.PRO: actor("U-PRO-1", Role.PRO),
}
UC03, UC04, UC05 = (actor(f"svc-uc0{n}-SYNTHETIC", Role.SYSTEM) for n in (3, 4, 5))
LINES: list[str] = []


def say(text: str = "") -> None:
    LINES.append(text)
    print(text)


def check(condition: bool, label: str) -> None:
    say(f"    [{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        raise AssertionError(label)


def raises(exc: type[Exception], label: str, fn) -> None:
    try:
        fn()
    except exc as err:
        say(f"    [PASS] {label}  ->  {type(err).__name__}: {getattr(err, 'message', err)}")
        return
    except Exception as err:  # noqa: BLE001
        raise AssertionError(f"{label}: wrong error {type(err).__name__}: {err}") from err
    raise AssertionError(f"{label}: nothing was refused")


def open_service(db: Path):
    return build_service(
        {"UC02_STORE_MODE": "sqlite", "UC02_SQLITE_PATH": str(db)}, source_principals=PRINCIPALS)


def intake(service, employee: str, case: str, joining: date | None = None):
    """SYNTHETIC UC-01 hand-off: offer accepted, form approved, ids supplied."""
    kwargs = {"joining_date": joining} if joining else {}
    trigger = make_trigger(persona(employee), **kwargs).model_copy(
        update={"case_id": case, "correlation_id": f"corr-{case}"})
    record = service.create_onboarding(HR, trigger)
    say(f"  intake: {record.onboarding_id} case={record.case_id} corr={record.correlation_id} "
        f"config_version={record.config_version or '(none stated)'} tasks={len(record.tasks)} "
        f"plan v{record.plan_version} {record.plan_status.value}")
    return record


def approve_plan(service, oid):
    request = service.submit_plan(HR, oid)
    record = service.decide_plan(HR_APPROVER, request.request_id, DecisionOutcome.APPROVED)
    say(f"  HR plan review: {request.request_id} approved by {HR_APPROVER.actor_id} -> "
        f"plan v{record.plan_version} {record.plan_status.value}")
    return record


def synthetic_event(service, oid, key, source, who, outcome, **extra):
    update = StatusUpdate(
        update_id=f"syn-{oid}-{key}-{outcome.value}", source=source, client_id=CLIENT_ALPHA,
        onboarding_id=oid, task_id=f"{oid}#{key}", outcome=outcome, occurred_at=OCCURRED, **extra)
    return service.apply_status_update(who, oid, update)


def run_teams(service, oid, skip=()):
    """Every team completes what is ready, in dependency order, with evidence where required."""
    done = []
    while True:
        ready = [t for t in service.ready_tasks(HR, oid) if t.template_key not in skip]
        if not ready:
            return done
        for t in ready:
            ref = f"SYN-EVID-{t.template_key}" if t.evidence_required else None
            service.complete_task(TEAM[t.owner], oid, t.task_id, ref)
            done.append(t.template_key)


def finish(service, oid, key):
    """The owning team completes one task, supplying evidence where the task requires it."""
    task = service.get(HR, oid).task_by_key(key)
    ref = f"SYN-EVID-{key}" if task.evidence_required else None
    return service.complete_task(TEAM[task.owner], oid, task.task_id, ref)


def show_readiness(service, oid, as_of, label=""):
    r = service.joining_readiness(HR, oid, as_of)
    say(f"  readiness{label}: {r.status.upper()}  tasks {r.complete}/{r.total_tasks} complete, "
        f"pending={r.pending} blocked={r.blocked} overdue={r.overdue} approvals_pending={len(r.outstanding_approval_ids)} "
        f"escalations={r.escalation_count} joining_risk={r.joining_risk}")
    for reason in r.reasons:
        say(f"      - {reason}")
    return r


def show_teams(service, oid, as_of):
    for t in service.team_summary(HR, oid, as_of):
        say(f"    team {t.team:<11} total={t.total:<2} done={t.complete:<2} in_progress={t.in_progress} "
            f"ready={t.ready} blocked={t.blocked} overdue={t.overdue} unassigned={t.unassigned}")


# ------------------------------------------------------------------------------------------ A
def scenario_a(db: Path) -> dict:
    say("\n=== Scenario A: successful onboarding (EMP-001) ===")
    service, oid = open_service(db), None
    record = intake(service, "EMP-001", "CASE-A")
    oid = record.onboarding_id
    raises(PlanNotApprovedError, "team cannot start work before HR approves the plan",
           lambda: service.start_task(HR, oid, f"{oid}#hr_record_create"))
    approve_plan(service, oid)
    for key in ("hr_record_create",):
        service.assign_owner(HR, oid, f"{oid}#{key}", "U-OPS-7", team="HR Ops (synthetic)")
    say("  team-wise summary after release:")
    show_teams(service, oid, CLOCK0)
    service.complete_task(HR, oid, f"{oid}#hr_record_create")
    r = synthetic_event(service, oid, "collect_documents", SourceUseCase.UC03_DOCUMENT_VALIDATION, UC03,
                        ExternalOutcome.VALID, reference="SYN-DOCSET-A")
    say(f"  [MOCKED UC-03 event] collect_documents -> accepted={r.accepted} status={r.applied_status.value}")
    r = synthetic_event(service, oid, "it_laptop", SourceUseCase.UC04_EQUIPMENT_ACCESS, UC04,
                        ExternalOutcome.COMPLETED, reference="SYN-TICKET-A")
    say(f"  [MOCKED UC-04 event] it_laptop -> accepted={r.accepted} status={r.applied_status.value}")
    r = synthetic_event(service, oid, "orientation_schedule", SourceUseCase.UC05_ORIENTATION, UC05,
                        ExternalOutcome.COMPLETED, reference="SYN-SESSION-A")
    say(f"  [MOCKED UC-05 event] orientation_schedule -> accepted={r.accepted} status={r.applied_status.value}")
    pre = show_readiness(service, oid, CLOCK0, " (mid-flight)")
    check(pre.status == "not_ready", "readiness is NOT granted while tasks remain")
    done = run_teams(service, oid)
    say(f"  teams completed: {', '.join(done)}")
    request = service.submit_final(HR, oid, CLOCK0)
    sub = service.get(HR, oid).final_submissions[-1]
    say(f"  final submission v{sub.version}: ready={sub.ready} complete={sub.complete}/{sub.total_tasks} "
        f"teams={sub.team_completion} evidence_refs={len(sub.evidence_refs)}")
    check(service.joining_readiness(HR, oid, CLOCK0).status == "not_ready",
          "a submitted report does not grant readiness - HR has not approved it yet")
    record = service.decide_final(HR_APPROVER, request.request_id, DecisionOutcome.APPROVED)
    say(f"  HR final review: approved by {HR_APPROVER.actor_id} -> final_status={record.final_status.value}")
    final = show_readiness(service, oid, CLOCK0, " (final)")
    check(final.status == "granted" and not final.reasons, "joining readiness GRANTED from actual state")
    check(record.status.value == "active", "onboarding case is NOT marked complete by UC-02 (UC-06 owns closing)")
    out = {"oid": oid, "readiness": final.status, "complete": final.complete}
    service.store.client.close()
    return out


# ------------------------------------------------------------------------------------------ B
def scenario_b(db: Path) -> dict:
    say("\n=== Scenario B: HR rejects the plan, it is corrected and resubmitted (EMP-002) ===")
    service = open_service(db)
    oid = intake(service, "EMP-002", "CASE-B").onboarding_id
    request = service.submit_plan(HR, oid)
    say(f"  submitted for HR approval: {request.request_id}")
    raises(UnauthorizedActionError, "a non-HR role cannot approve",
           lambda: service.decide_plan(TEAM[Role.IT_MANAGER], request.request_id, DecisionOutcome.APPROVED))
    raises(UnauthorizedActionError, "the requester cannot approve their own plan",
           lambda: service.decide_plan(HR, request.request_id, DecisionOutcome.APPROVED))
    raises(InvalidStateError, "rejection without a reason is refused",
           lambda: service.decide_plan(HR_APPROVER, request.request_id, DecisionOutcome.REJECTED, "  "))
    record = service.decide_plan(HR_APPROVER, request.request_id, DecisionOutcome.REJECTED,
                                 "Synthetic: orientation owner missing, assign before release")
    say(f"  HR rejected plan v{record.plan_version}: reason recorded -> status={record.plan_status.value}")
    raises(InvalidStateError, "a decided request cannot be decided again (no duplicate decision)",
           lambda: service.decide_plan(HR_APPROVER, request.request_id, DecisionOutcome.APPROVED))
    raises(PlanNotApprovedError, "no team execution while the plan is rejected",
           lambda: service.start_task(HR, oid, f"{oid}#hr_record_create"))
    raises(InvalidStateError, "a rejected plan cannot be resubmitted unchanged",
           lambda: service.submit_plan(HR, oid))
    service.assign_owner(HR, oid, f"{oid}#orientation_schedule", "U-ORI-1", team="Orientation (synthetic)")
    revised = service.revise_plan(HR, oid, "Synthetic: orientation owner assigned")
    check(revised.plan_version == 2 and revised.plan_status is PlanStatus.PROPOSED, "revised plan is version 2, proposed")
    check(revised.task_by_key("orientation_schedule").assignee_id == "U-ORI-1", "HR's assignment survived the revision")
    request2 = service.submit_plan(HR, oid)
    record = service.decide_plan(HR_APPROVER, request2.request_id, DecisionOutcome.APPROVED)
    check(record.plan_status is PlanStatus.APPROVED and record.plan_version == 2, "revised plan approved -> released")
    service.complete_task(HR, oid, f"{oid}#hr_record_create")
    check(service.get(HR, oid).task_by_key("hr_record_create").status is TaskStatus.COMPLETE, "teams can now execute")
    history = [(e.version, e.action) for e in service.get(HR, oid).plan_events]
    say(f"  plan history: {history}")
    check(history == [(1, "proposed"), (1, "submitted"), (1, "rejected"), (2, "revised"),
                      (2, "submitted"), (2, "approved")], "full plan history preserved")
    service.store.client.close()
    return {"oid": oid, "history": [list(h) for h in history]}


# ------------------------------------------------------------------------------------------ C
def scenario_c(db: Path) -> dict:
    say("\n=== Scenario C: visa / medical delay with a test clock (EMP-007) ===")
    service = open_service(db)
    oid = intake(service, "EMP-007", "CASE-C").onboarding_id
    approve_plan(service, oid)
    t = lambda k: f"{oid}#{k}"  # noqa: E731
    service.assign_owner(HR, oid, t("visa_processing"), "U-PRO-1", team="PRO (synthetic)")
    service.complete_task(HR, oid, t("hr_record_create"))
    service.complete_task(HR, oid, t("collect_documents"))
    service.complete_task(HR, oid, t("visa_pack_handover"), "SYN-EVID-visa_pack_handover")  # evidence required
    visa = service.get(HR, oid).task_by_key("visa_processing")
    say(f"  visa_processing: owner={visa.owner.value} due={visa.due_date} status={visa.status.value}")
    # Medical test is due 2026-11-03: the sweep on 11-02 sees it approaching.
    day = date(2026, 11, 2)
    sweep = service.monitoring_sweep(HR, oid, day)
    due_soon = [d for d in sweep.reminder_drafts if d.reason == "due_soon"]
    say(f"  clock {day}: reminder DRAFTS={len(sweep.reminder_drafts)} (due_soon={len(due_soon)}); "
        f"delivery: {sweep.delivery}")
    check(any(any("medical_test" in tid for tid in d.task_ids) for d in due_soon), "approaching medical deadline detected")
    check(all(d.delivery_status == "draft_not_sent" for d in sweep.reminder_drafts), "reminders are drafts, none delivered")
    # The medical team reports a delay with a reason.
    service.report_delay(TEAM[Role.HR_OPS], oid, t("medical_test"), "Synthetic: clinic has no slot before the 9th")
    flags = {(f.template_key, f.code) for f in service.follow_up_flags(HR, oid, day)}
    check(("medical_test", "delayed") in flags, "delay reason captured and flagged")
    # Day after the due date: overdue, first escalation.
    day2 = date(2026, 11, 4)
    sweep = service.monitoring_sweep(HR, oid, day2)
    overdue_ids = {s.subject_id for s in sweep.escalations if s.subject_type == "task"}
    check(t("medical_test") in overdue_ids, "overdue task escalated")
    first = [e for e in service.escalation_history(HR, oid) if e.subject_id == t("medical_test")]
    say(f"  clock {day2}: escalation recorded -> {first[0].level} to {first[0].escalate_to} ({first[0].reason})")
    # Two more days: past the one-working-day response window -> the HR operations lead.
    day3 = date(2026, 11, 9)
    service.monitoring_sweep(HR, oid, day3)
    levels = sorted({e.level for e in service.escalation_history(HR, oid) if e.subject_id == t("medical_test")})
    say(f"  clock {day3}: escalation levels recorded for medical_test = {levels}")
    check(len(levels) == 2, "further escalation to the HR operations lead after the response window")
    again = service.monitoring_sweep(HR, oid, day3)
    check(again.newly_recorded_escalations == 0 and again.newly_recorded_reminders == 0,
          "re-running the sweep does not duplicate escalation or reminder records")
    risk = service.joining_risk(HR, oid, day3)
    say(f"  joining-date risk at {day3}: at_risk={risk.at_risk} days_to_joining={risk.days_to_joining} "
        f"overdue={len(risk.overdue_task_ids)}")
    check(risk.at_risk, "joining-date risk recalculated as at-risk")
    finish(service, oid, "medical_test")
    resolved = [e for e in service.escalation_history(HR, oid) if e.subject_id == t("medical_test")]
    check(all(e.status == "resolved" for e in resolved), "escalations close when the work completes")
    check(service.get(HR, oid).task_by_key("medical_test").delay_reason == "", "delay cleared on completion")
    out = {"oid": oid, "escalation_levels": levels}
    service.store.client.close()
    return out


# ------------------------------------------------------------------------------------------ D
def scenario_d(db: Path) -> dict:
    say("\n=== Scenario D: missing documents (EMP-003) ===")
    service = open_service(db)
    oid = intake(service, "EMP-003", "CASE-D").onboarding_id
    approve_plan(service, oid)
    service.complete_task(HR, oid, f"{oid}#hr_record_create")
    r = synthetic_event(service, oid, "collect_documents", SourceUseCase.UC03_DOCUMENT_VALIDATION, UC03,
                        ExternalOutcome.REQUIRES_REVIEW, blocking_reason="Synthetic: passport page missing")
    say(f"  [MOCKED UC-03 event] collect_documents -> {r.applied_status.value}, follow-up raised={r.follow_up_raised}")
    task = service.get(HR, oid).task_by_key("collect_documents")
    check(task.status is not TaskStatus.COMPLETE and task.is_externally_unresolved, "overall document task stays incomplete")
    check(service.get(HR, oid).task_by_key("payroll_setup").status is TaskStatus.BLOCKED, "dependants stay blocked")
    flags = [(f.template_key, f.code, f.reason) for f in service.follow_up_flags(HR, oid, CLOCK0)]
    say(f"  HR visibility (follow-up flags): {[f for f in flags if f[0] == 'collect_documents']}")
    run_teams(service, oid, skip=("collect_documents",))
    ready = show_readiness(service, oid, CLOCK0)
    check(ready.status == "not_ready" and ready.unresolved_external_task_ids, "readiness prevented; HR sees the held task")
    raises(InvalidStateError, "cannot approve a final submission with outstanding work",
           lambda: service.decide_final(HR_APPROVER, service.submit_final(HR, oid, CLOCK0).request_id,
                                        DecisionOutcome.APPROVED))
    service.store.client.close()
    return {"oid": oid}


# ------------------------------------------------------------------------------------------ E
def scenario_e(db: Path) -> dict:
    say("\n=== Scenario E: parallel team execution (EMP-007) ===")
    service = open_service(db)
    oid = intake(service, "EMP-007", "CASE-E").onboarding_id
    approve_plan(service, oid)
    ready = service.ready_tasks(HR, oid)
    teams_ready = {(t.owner.value, t.category.value) for t in ready}
    say("  ready at the same moment (independent roots):")
    for owner, category in sorted(teams_ready):
        say(f"      {owner:<11} {category}")
    cats = {c for _, c in teams_ready}
    check({"medical", "insurance", "travel", "accommodation", "food"} <= cats, "medical, insurance, travel, accommodation, food all ready in parallel")
    blocked = {t.template_key for t in service.get(HR, oid).tasks if t.status is TaskStatus.BLOCKED}
    check({"it_laptop", "visa_pack_handover", "visa_processing"} <= blocked, "IT and visa chains are blocked behind their prerequisites")
    for key in ("medical_test", "insurance_enrolment", "travel_booking", "accommodation_booking", "food_arrangement"):
        finish(service, oid, key)
    after = {t.template_key: t.status for t in service.get(HR, oid).tasks}
    check(after["biometrics_capture"] is TaskStatus.READY and after["family_insurance_enrolment"] is TaskStatus.READY,
          "completing a root unblocks exactly its dependants")
    check(after["it_laptop"] is TaskStatus.BLOCKED, "unrelated chains still wait for hr_record_create")
    batches = service.parallel_batches(HR, oid)
    say(f"  dependency layers (each layer may run in parallel): {[len(b) for b in batches]}")
    show_teams(service, oid, CLOCK0)
    service.store.client.close()
    return {"oid": oid}


# ------------------------------------------------------------------------------------------ F
def scenario_f(db: Path) -> dict:
    say("\n=== Scenario F: joining-date change (EMP-010) ===")
    service = open_service(db)
    oid = intake(service, "EMP-010", "CASE-F").onboarding_id
    approve_plan(service, oid)
    service.complete_task(HR, oid, f"{oid}#hr_record_create")
    before = {t.template_key: (t.due_date, t.status) for t in service.get(HR, oid).tasks}
    old_joining = service.get(HR, oid).joining_date
    request = service.request_change(HR, oid, joining_date=date(2026, 11, 30))
    raises(Exception, "the change cannot be applied before approval",
           lambda: service.apply_change(HR, request.request_id))
    service.decide_change(HR_APPROVER, request.request_id, DecisionOutcome.APPROVED)
    outcome = service.apply_change(HR, request.request_id)
    record = service.get(HR, oid)
    moved = [k for k, (d, _) in before.items() if record.task_by_key(k).due_date != d]
    say(f"  joining date {old_joining} -> {record.joining_date}; plan v{record.plan_version}; "
        f"{len(outcome.rescheduled_task_ids)} task(s) rescheduled")
    check(record.task_by_key("hr_record_create").status is TaskStatus.COMPLETE, "completed task preserved")
    check(record.task_by_key("hr_record_create").due_date == before["hr_record_create"][0], "completed task not rescheduled")
    check(bool(moved), "open tasks were recalculated")
    revisions = service.store.plan_revisions(CLIENT_ALPHA, oid)
    check(len(revisions) == 1 and revisions[0]["drivers"] == ["joining_date"], "revision recorded with its driver")
    check([e.action for e in record.plan_events][-1] == "replanned", "plan history shows the new version")
    say("  schedule by team after the change:")
    show_teams(service, oid, date(2026, 11, 9))
    risk = service.joining_risk(HR, oid, date(2026, 11, 27))
    say(f"  joining-date risk on 2026-11-27: at_risk={risk.at_risk} ({'; '.join(risk.reasons) or 'none'})")
    service.store.client.close()
    return {"oid": oid, "revision": record.plan_version}


# ------------------------------------------------------------------------------------------ G
def scenario_g(db: Path) -> dict:
    say("\n=== Scenario G: final HR rejection, correction, resubmission (EMP-011) ===")
    service = open_service(db)
    oid = intake(service, "EMP-011", "CASE-G").onboarding_id
    approve_plan(service, oid)
    run_teams(service, oid, skip=("policy_ack",))
    request1 = service.submit_final(HR, oid, CLOCK0)
    sub1 = service.get(HR, oid).final_submissions[-1]
    say(f"  final submission v1: ready={sub1.ready} outstanding={sub1.outstanding_task_ids}")
    raises(InvalidStateError, "HR cannot reject without a reason",
           lambda: service.decide_final(HR_APPROVER, request1.request_id, DecisionOutcome.REJECTED, ""))
    raises(InvalidStateError, "HR cannot approve an incomplete submission",
           lambda: service.decide_final(HR_APPROVER, request1.request_id, DecisionOutcome.APPROVED))
    record = service.decide_final(HR_APPROVER, request1.request_id, DecisionOutcome.REJECTED,
                                  "Synthetic: policy acknowledgement is outstanding")
    sub1 = record.final_submissions[-1]
    say(f"  HR rejected v1: reason recorded; returned for correction: {sub1.returned_task_ids}")
    check(record.final_status is FinalStatus.REJECTED and sub1.returned_task_ids, "rejection recorded with returned items")
    check(service.joining_readiness(HR, oid, CLOCK0).status == "not_ready", "readiness not granted")
    service.complete_task(TEAM[Role.EMPLOYEE], oid, f"{oid}#policy_ack")
    request2 = service.submit_final(HR, oid, CLOCK0)
    record = service.decide_final(HR_APPROVER, request2.request_id, DecisionOutcome.APPROVED)
    check([s.decision for s in record.final_submissions] == ["rejected", "approved"],
          "previous submission and its rejection are preserved beside the new one")
    final = show_readiness(service, oid, CLOCK0, " (after correction)")
    check(final.status == "granted", "readiness granted only after the corrected submission is approved")
    service.store.client.close()
    return {"oid": oid, "decisions": [s.decision for s in record.final_submissions]}


SCENARIOS = {"A": scenario_a, "B": scenario_b, "C": scenario_c, "D": scenario_d,
             "E": scenario_e, "F": scenario_f, "G": scenario_g}


def run(directory: Path) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, fn in SCENARIOS.items():
        db = directory / f"scenario_{name}.sqlite3"
        if db.exists():
            raise SystemExit(f"{db} already exists; use an empty directory")
        manifest[name] = fn(db)
    (directory / "manifest.json").write_text(json.dumps(manifest, default=str, indent=1))
    say("\nALL SCENARIOS PASSED (process 1). Databases written to " + str(directory))
    return 0


def verify(directory: Path) -> int:
    manifest = json.loads((directory / "manifest.json").read_text())
    say("=== Restart verification: NEW process, same database files ===")
    for name, info in manifest.items():
        service = open_service(directory / f"scenario_{name}.sqlite3")
        assert service.hydrate(CLIENT_ALPHA) == 1, f"{name}: case not found after restart"
        oid = info["oid"]
        record = service.get(HR, oid)
        events = service.audit.events_for(HR)
        say(f"\n  scenario {name}: {oid}  case={record.case_id} plan v{record.plan_version} "
            f"{record.plan_status.value} final={record.final_status.value} "
            f"tasks={len(record.tasks)} audit_events={len(events)}")
        check(record.case_id == f"CASE-{name}" and record.correlation_id == f"corr-CASE-{name}", "case and correlation ids recovered")
        check(len(record.tasks) > 5 and any(t.depends_on for t in record.tasks), "tasks and dependencies recovered")
        check(len({e.event_id for e in events}) == len(events) and all(e.at.tzinfo for e in events), "audit history intact, unique, timestamped")
        check(any(e.action == "plan.submit" for e in events) and any(e.action == "plan.decide" for e in events), "plan approval audit recovered")
        approvals = service.store.load_approvals(CLIENT_ALPHA)
        check(bool(approvals) and all(a.decided_by for a in approvals if a.outcome is not DecisionOutcome.PENDING), "approval/rejection records recovered")
        if name == "A":
            check(record.final_status is FinalStatus.APPROVED and service.joining_readiness(HR, oid, CLOCK0).status == "granted", "A: readiness still granted from recovered state")
        if name == "B":
            check([[e.version, e.action] for e in record.plan_events] == info["history"], "B: plan rejection/revision history recovered")
            check(sum(1 for a in approvals if a.outcome is DecisionOutcome.REJECTED) == 1, "B: the rejection and its reason recovered")
            check(any(a.reason.startswith("Synthetic: orientation") for a in approvals), "B: rejection reason text recovered")
        if name == "C":
            esc = service.escalation_history(HR, oid)
            check(len({e.level for e in esc if e.subject_id.endswith("medical_test")}) == 2 and all(e.status == "resolved" for e in esc if e.subject_id.endswith("medical_test")),
                  "C: escalation records (via audit) recovered, with resolution")
            before = len(service.audit.events_for(HR))
            check(service.monitoring_sweep(HR, oid, date(2026, 11, 9)).newly_recorded_escalations == 0
                  and len(service.audit.events_for(HR)) >= before, "C: re-sweep after restart does not duplicate escalation records")
        if name == "D":
            task = record.task_by_key("collect_documents")
            check(task.is_externally_unresolved and task.status is not TaskStatus.COMPLETE, "D: held document task recovered")
        if name == "F":
            check(record.plan_version == 2 and record.joining_date == date(2026, 11, 30), "F: replanned schedule recovered")
            check(len(service.store.plan_revisions(CLIENT_ALPHA, oid)) == 1, "F: plan revision recovered")
        if name == "G":
            check([s.decision for s in record.final_submissions] == ["rejected", "approved"], "G: both final submissions and decisions recovered")
        service.store.client.close()
    say("\nRESTART VERIFICATION PASSED (process 2).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", required=True, type=Path)
    parser.add_argument("--phase", required=True, choices=("run", "verify"))
    args = parser.parse_args()
    try:
        return run(args.dir) if args.phase == "run" else verify(args.dir)
    except AssertionError as err:
        print(f"\nFAILED: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
