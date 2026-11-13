"""UC-02 end-to-end demonstration runner.

Read-only demonstration of the existing UC-02 implementation: input JSON ->
validation -> task generation -> planning -> tracking -> approval-gated
re-planning -> joining handoff.

This script contains **no business logic**. It only loads the canonical
synthetic fixture, calls the public ``OnboardingService`` API and prints what
comes back. It is deliberately outside ``src/`` so it is not part of the
package and cannot be imported by business code.

It adds no dependency: standard library plus the UC-02 package only.

Run from the repository root:

    python demo/uc02_end_to_end.py                      # canonical fixture
    python demo/uc02_end_to_end.py data/mock/07_full_relocation.json

Any input file is opened **read-only** and never written to. Invalid-input
cases are built as in-memory copies, so no fixture is ever altered.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from onboarding_task_management.contracts import (  # noqa: E402
    Actor,
    DecisionOutcome,
    Role,
    TaskStatus,
)
from onboarding_task_management.errors import (  # noqa: E402
    EvidenceRequiredError,
    OnboardingError,
    PrerequisiteNotMetError,
)
from onboarding_task_management.sub_agent.service import (  # noqa: E402
    OnboardingService,
    OnboardingTrigger,
)
from onboarding_task_management.synthetic import build_registry  # noqa: E402

FIXTURE = ROOT / "data" / "UC02_Synthetic_Onboarding_Test_Data.json"

#: Fixed evaluation dates so the demo is reproducible.
AS_OF_PLANNING = date(2026, 10, 26)
AS_OF_LATE = date(2026, 11, 10)

HR_OPS = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id="CLIENT-ALPHA")
HR_OPS_APPROVER = Actor(actor_id="U-HR-2", role=Role.HR_OPS, client_id="CLIENT-ALPHA")
IT_MANAGER = Actor(actor_id="U-IT-1", role=Role.IT_MANAGER, client_id="CLIENT-ALPHA")
EMPLOYEE = Actor(actor_id="U-EMP-1", role=Role.EMPLOYEE, client_id="CLIENT-ALPHA")


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


def _enable_utf8() -> bool:
    """Best-effort UTF-8 stdout so Arabic titles render. Never fatal."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        return True
    except Exception:
        return False


UTF8 = _enable_utf8()


def safe(text: str) -> str:
    """Drop non-ASCII if the console cannot encode it."""
    if UTF8:
        return text
    return text.encode("ascii", "replace").decode("ascii")


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def step(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 72 - len(title)))


# --------------------------------------------------------------------------
# 1. Input
# --------------------------------------------------------------------------


def resolve_input() -> Path:
    """Optional CLI argument selects a different mock payload."""
    if len(sys.argv) > 1:
        candidate = Path(sys.argv[1])
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        if not candidate.exists():
            raise SystemExit(f"input file not found: {candidate}")
        return candidate
    return FIXTURE


def load_fixture(path: Path) -> dict:
    """Load a synthetic input payload. Read-only.

    ``_mock_note`` is a fixture label, not part of the contract, so it is
    stripped before the payload reaches OnboardingTrigger.
    """
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    payload.pop("_mock_note", None)
    return payload


def show_input(payload: dict, path: Path) -> None:
    header(f"1. INPUT  —  {path.relative_to(ROOT)}")
    print(f"  employee_id   : {payload['employee_id']}")
    print(f"  client_id     : {payload['client_id']}")
    print(f"  role_id       : {payload['role_id']}   department: {payload['department']}")
    print(f"  manager_id    : {payload['manager_id']}")
    print(f"  location      : {payload['location']}   project: {payload['project']}")
    print(f"  joining_date  : {payload['joining_date']}")
    print(f"  grade         : {payload.get('grade', '') or '(not supplied)'}")
    print(f"  nationality_grp: {payload.get('nationality_group', '') or '(not supplied — unfiltered)'}")
    print(f"  attributes    : {payload.get('attributes', []) or '(none — no extended activities apply)'}")
    print(f"  offer         : accepted={payload['offer']['accepted']} "
          f"ref={payload['offer']['offer_reference']} at={payload['offer']['accepted_at']}")
    print(f"  form          : submitted={payload['form']['submitted']} "
          f"approved={payload['form']['approved']} "
          f"by={payload['form']['approved_by_role']} "
          f"missing={payload['form']['missing_fields']}")
    print("\n  NOTE: no employee name, nationality or contact field exists in the")
    print("        UC-02 contract. Those are excluded by design (UC02-BR-13).")


# --------------------------------------------------------------------------
# 2. Validation
# --------------------------------------------------------------------------


def show_validation(service: OnboardingService, payload: dict) -> OnboardingTrigger:
    header("2. VALIDATION  —  service.validate_preconditions (UC02-BR-01)")

    trigger = OnboardingTrigger(**payload)
    print(f"  [schema ] OnboardingTrigger parsed OK "
          f"(grade={trigger.grade!r}, attributes={list(trigger.attributes)})")

    print("\n  Invalid-input cases (in-memory copies — the fixture is untouched):")
    cases = {
        "offer not accepted": {"offer": {**payload["offer"], "accepted": False}},
        "form not approved": {"form": {**payload["form"], "approved": False}},
        "form incomplete": {
            "form": {**payload["form"], "missing_fields": ["bank_details"]}
        },
        "approved by a non-HR role": {
            "form": {**payload["form"], "approved_by_role": "employee"}
        },
        "offer belongs to another employee": {
            "offer": {**payload["offer"], "employee_id": "EMP-999"}
        },
        "joining date outside calendar coverage": {"joining_date": "2029-03-01"},
    }
    for label, patch in cases.items():
        bad = OnboardingTrigger(**{**payload, **patch})
        try:
            service.create_onboarding(HR_OPS, bad)
            print(f"    {label:38} -> ACCEPTED (unexpected)")
        except OnboardingError as exc:
            print(f"    {label:38} -> {type(exc).__name__}: {exc.message}")

    print("\n  Unauthorized / isolation cases:")
    for label, actor in (
        ("employee tries to create", EMPLOYEE),
        ("another client tries to create",
         Actor(actor_id="U-B", role=Role.HR_OPS, client_id="CLIENT-BETA")),
    ):
        try:
            service.create_onboarding(actor, trigger)
            print(f"    {label:38} -> ACCEPTED (unexpected)")
        except OnboardingError as exc:
            print(f"    {label:38} -> {type(exc).__name__}: {exc.message}")

    return trigger


# --------------------------------------------------------------------------
# 3. Task generation
# --------------------------------------------------------------------------


def show_plan(service: OnboardingService, onboarding_id: str) -> None:
    header("3. GENERATED PLAN  —  planner.build_tasks from client configuration")
    record = service.get(HR_OPS, onboarding_id)
    print(f"  onboarding_id : {record.onboarding_id}")
    print(f"  status        : {record.status.value}   revision: {record.revision}")
    print(f"  tasks         : {len(record.tasks)}")
    print()
    print(f"  {'TASK':24}{'OWNER':12}{'STATUS':10}{'DUE':12}{'EVID':6}{'APPROVER':11}DEPENDS ON")
    print("  " + "-" * 92)
    for task in record.tasks:
        deps = ",".join(d.split("#")[-1] for d in task.depends_on) or "-"
        approver = task.approval_role.value if task.approval_role else "-"
        print(f"  {task.template_key:24}{task.owner.value:12}{task.status.value:10}"
              f"{task.due_date.isoformat():12}{task.evidence_required!s:6}"
              f"{approver:11}{deps}")

    step("Bilingual titles (UC02-BR / blueprint D3)")
    for task in record.tasks[:3]:
        print(safe(f"  {task.template_key:24} EN: {task.title}"))
        print(safe(f"  {'':24} AR: {task.title_ar}"))


def show_readiness(service: OnboardingService, onboarding_id: str) -> None:
    header("4. READINESS AND PARALLELISM  —  planner.ready_tasks / parallel_batches")
    ready = service.ready_tasks(HR_OPS, onboarding_id)
    blocked = service.blocked_tasks(HR_OPS, onboarding_id)
    print(f"  READY   ({len(ready)}): {[t.template_key for t in ready]}")
    print(f"  BLOCKED ({len(blocked)}): {[t.template_key for t in blocked]}")
    print("\n  Dependency layers — every task in a layer may run in parallel:")
    for index, batch in enumerate(service.parallel_batches(HR_OPS, onboarding_id), 1):
        print(f"    layer {index}: {[t.split('#')[-1] for t in batch]}")

    step("UC02-BR-04: a blocked task cannot be started")
    try:
        service.start_task(HR_OPS, onboarding_id, f"{onboarding_id}#collect_documents")
    except PrerequisiteNotMetError as exc:
        print(f"    PrerequisiteNotMetError: {exc.message}")


# --------------------------------------------------------------------------
# 4. Execution
# --------------------------------------------------------------------------


def run_progress(service: OnboardingService, onboarding_id: str) -> None:
    header("5. EXECUTION  —  tracker.start_task / complete_task")
    root = f"{onboarding_id}#hr_record_create"

    print("  UC02-BR-03/13: only the owner or an admin role may act")
    try:
        service.complete_task(EMPLOYEE, onboarding_id, root)
    except OnboardingError as exc:
        print(f"    employee completing an HR task -> {type(exc).__name__}: {exc.message}")

    service.start_task(HR_OPS, onboarding_id, root)
    service.complete_task(HR_OPS, onboarding_id, root)
    print(f"    hr_record_create -> {service.get(HR_OPS, onboarding_id).task_by_key('hr_record_create').status.value}")

    ready = [t.template_key for t in service.ready_tasks(HR_OPS, onboarding_id)]
    print(f"    unblocked in parallel: {ready}")

    service.complete_task(IT_MANAGER, onboarding_id, f"{onboarding_id}#it_laptop")
    print("    it_laptop -> complete (owner: it_manager)")


def show_evidence_rule(service: OnboardingService) -> None:
    header("6. COMPLETION EVIDENCE  —  UC02-BR-12 (relocation hire EMP-007)")
    from onboarding_task_management.synthetic import make_trigger, persona

    record = service.create_onboarding(HR_OPS, make_trigger(persona("EMP-007")))
    oid = record.onboarding_id
    print(f"  {oid}: {len(record.tasks)} tasks "
          f"(extended activities apply via declared attributes)")

    extended = [
        t for t in record.tasks
        if t.category.value in {
            "background_verification", "visa", "medical", "biometrics",
            "insurance", "travel", "accommodation", "food",
            "banking_letter", "hr_system_activation",
        }
    ]
    print(f"\n  {'EXTENDED ACTIVITY':28}{'CATEGORY':26}{'OWNER':12}{'DUE':12}EVID")
    print("  " + "-" * 88)
    for task in extended:
        print(f"  {task.template_key:28}{task.category.value:26}"
              f"{task.owner.value:12}{task.due_date.isoformat():12}"
              f"{task.evidence_required}")

    step("Completing an evidence-required task without evidence")
    service.complete_task(HR_OPS, oid, f"{oid}#hr_record_create")
    try:
        service.complete_task(HR_OPS, oid, f"{oid}#salary_transfer_letter")
    except EvidenceRequiredError as exc:
        print(f"    EvidenceRequiredError: {exc.message}")
    task = service.complete_task(
        HR_OPS, oid, f"{oid}#salary_transfer_letter", "LTR-2026-0001"
    )
    print(f"    with evidence -> {task.status.value}, evidence_ref={task.evidence_ref}")


# --------------------------------------------------------------------------
# 5. Tracking, risk and handoff
# --------------------------------------------------------------------------


def show_tracking(service: OnboardingService, onboarding_id: str) -> None:
    header(f"7. TRACKING AND RISK  —  as_of {AS_OF_LATE} (after several due dates)")
    summary = service.status(HR_OPS, onboarding_id, AS_OF_LATE)
    print(f"  total={summary.total_tasks}  complete={summary.complete}  "
          f"ready={summary.ready}  blocked={summary.blocked}  "
          f"in_progress={summary.in_progress}")
    print(f"  days_to_joining={summary.days_to_joining}  on_track={summary.is_on_track}")
    print(f"  overdue: {[t.split('#')[-1] for t in summary.overdue_task_ids]}")

    step("Outstanding activities with owner and follow-up (UC02-BR-10)")
    print(f"  {'TASK':24}{'OWNER':12}{'DUE':12}{'OVERDUE':9}FOLLOW-UP")
    print("  " + "-" * 92)
    for activity in service.outstanding_activities(HR_OPS, onboarding_id, AS_OF_LATE)[:8]:
        print(f"  {activity.template_key:24}{activity.owner.value:12}"
              f"{activity.due_date.isoformat():12}{activity.is_overdue!s:9}"
              f"{activity.required_follow_up}")

    step("Joining-date risk")
    risk = service.joining_risk(HR_OPS, onboarding_id, AS_OF_LATE)
    print(f"  at_risk={risk.at_risk}  days_to_joining={risk.days_to_joining}")
    for reason in risk.reasons:
        print(f"    - {reason}")

    step("Milestones")
    for milestone in service.milestones(HR_OPS, onboarding_id, AS_OF_LATE):
        print(f"  {milestone.template_key:24}target={milestone.target_date} "
              f"status={milestone.status.value:9} "
              f"outstanding={len(milestone.outstanding_task_ids)}")

    step("Escalations — GR-12 / Framework §10 routing (UC02-BR-10)")
    items = service.escalations(HR_OPS, onboarding_id, AS_OF_LATE)
    if not items:
        print("  none")
    for item in items[:8]:
        owner = item.owner.value if item.owner else "-"
        print(f"  {item.subject_type:14}{item.subject_id.split('#')[-1].split('@')[-1]:24}"
              f"-> {item.escalate_to.value:11}({item.level.value:12}) "
              f"owner={owner:11}{item.reason}")

    step("Reminder drafts — prepared, never sent (UC02-BR-11)")
    drafts = service.reminder_drafts(HR_OPS, onboarding_id, AS_OF_LATE)
    for draft in drafts:
        print(f"  {draft.recipient_role.value:12}{draft.reason:10}"
              f"tasks={len(draft.task_ids):2}  delivery_status={draft.delivery_status}")
    if drafts:
        print(safe(f"\n  Example subject EN: {drafts[0].subject_en}"))
        print(safe(f"  Example subject AR: {drafts[0].subject_ar}"))


def show_replan(service: OnboardingService, onboarding_id: str) -> None:
    header("8. APPROVAL-GATED RE-PLANNING  —  UC02-BR-09")
    before = service.get(HR_OPS, onboarding_id)
    new_date = date(2026, 11, 30)
    print(f"  current joining_date={before.joining_date}  revision={before.revision}")

    request = service.request_change(HR_OPS, onboarding_id, joining_date=new_date)
    print(f"  requested change -> gate={request.gate.value} "
          f"approver_role={request.approver_role.value} outcome={request.outcome.value}")

    try:
        service.apply_change(HR_OPS, request.request_id)
    except OnboardingError as exc:
        print(f"  apply before approval -> {type(exc).__name__}: {exc.message}")

    try:
        service.decide_change(HR_OPS, request.request_id, DecisionOutcome.APPROVED)
    except OnboardingError as exc:
        print(f"  requester self-approves -> {type(exc).__name__}: {exc.message}")

    service.decide_change(HR_OPS_APPROVER, request.request_id, DecisionOutcome.APPROVED)
    outcome = service.apply_change(HR_OPS, request.request_id)
    after = service.get(HR_OPS, onboarding_id)
    print(f"  approved by {HR_OPS_APPROVER.actor_id} -> applied")
    print(f"  joining_date={after.joining_date}  revision={after.revision}")
    print(f"  rescheduled={len(outcome.rescheduled_task_ids)} "
          f"added={len(outcome.added_task_ids)} removed={len(outcome.removed_task_ids)} "
          f"retained={len(outcome.retained_task_ids)}")
    print("  completed tasks are never rescheduled or reopened:")
    for task in after.tasks:
        if task.status is TaskStatus.COMPLETE:
            print(f"    {task.template_key:24}{task.status.value:10}due={task.due_date}")


def show_handoff(service: OnboardingService, onboarding_id: str) -> None:
    header("9. JOINING HANDOFF  —  the UC-02 / UC-06 boundary")
    handoff = service.joining_handoff(HR_OPS, onboarding_id, AS_OF_LATE)
    print(f"  onboarding_id        : {handoff.onboarding_id}")
    print(f"  plan_revision        : {handoff.plan_revision}")
    print(f"  risk.at_risk         : {handoff.risk.at_risk}")
    print(f"  outstanding          : {len(handoff.outstanding)}")
    print(f"  milestones           : {len(handoff.milestones)}")
    print(f"  owner_issues         : {len(handoff.owner_issues)}")
    print(f"  escalations          : {len(handoff.escalations)}")
    print(f"  calendar.approved    : {handoff.calendar.approved}")
    for warning in handoff.calendar.warnings:
        print(f"    calendar warning   : {warning}")
    print(f"  case_closed_by_uc02  : {handoff.case_closed_by_uc02}")
    print(f"\n  {handoff.handoff_note}")


def show_audit(service: OnboardingService) -> None:
    header("10. AUDIT TRAIL  —  UC02-BR-14 (IDs, roles, counts and outcomes only)")
    events = service.audit.events_for(HR_OPS)
    print(f"  {len(events)} events for client {HR_OPS.client_id}\n")
    print(f"  {'EVENT':12}{'ACTION':26}{'OUTCOME':24}DETAIL")
    print("  " + "-" * 92)
    for event in events[:16]:
        print(f"  {event.event_id:12}{event.action:26}{event.outcome:24}{event.detail}")
    if len(events) > 16:
        print(f"  ... {len(events) - 16} more")


# --------------------------------------------------------------------------


def main() -> int:
    print(safe("UC-02 — ONBOARDING TASK MANAGEMENT: END-TO-END DEMONSTRATION"))
    print(f"repository root : {ROOT}")
    print(f"input fixture   : {resolve_input().relative_to(ROOT)}")
    print("storage         : in-memory only (no database, no API, no network)")

    source = resolve_input()
    payload = load_fixture(source)
    show_input(payload, source)

    service = OnboardingService(build_registry())
    trigger = show_validation(service, payload)

    record = service.create_onboarding(HR_OPS, trigger)
    onboarding_id = record.onboarding_id

    show_plan(service, onboarding_id)
    show_readiness(service, onboarding_id)
    run_progress(service, onboarding_id)
    show_evidence_rule(service)
    show_tracking(service, onboarding_id)
    show_replan(service, onboarding_id)
    show_handoff(service, onboarding_id)
    show_audit(service)

    header("DONE")
    print("  Synthetic data only. No external system was contacted.")
    print("  The canonical fixture was opened read-only and not modified.")
    return 0


if __name__ == "__main__":
    # RETIRED as a runnable demo: this script ran the agent on an in-memory store, which the UC-02 runtime no longer
    # allows (every agent workflow must persist to Supabase). Its functions stay importable for tests.
    print("This demo ran the UC-02 agent on memory and is retired. Use the Supabase-only synthetic demo: "
          "  python demo/uc02_supabase_agentic_workflow.py preflight   (see its docstring for the stages)", file=sys.stderr)
    raise SystemExit(2)

