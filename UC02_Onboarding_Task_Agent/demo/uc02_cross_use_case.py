"""UC-02 demonstration: HR owner assignment and cross-use-case status updates.

What this shows
---------------

The coordination half of UC-02, which the other demos do not cover:

1. a checklist is generated and **every applicable task starts unassigned**;
2. **HR manually assigns** the responsible individual to each task;
3. UC-03 reports a document INVALID -> the prerequisite stays unresolved and
   everything downstream stays blocked;
4. a follow-up flag names the person to chase;
5. a corrected document validates -> downstream readiness is recalculated;
6. UC-04 reports the laptop DELAYED, then COMPLETED;
7. UC-05 reports orientation complete;
8. the operational team reports visa / medical / insurance / travel work;
9. updates that must be refused are refused;
10. a consolidated plan and readiness report.

What this is NOT
----------------

**No external system is contacted, and no approved cross-use-case interface
exists.** Every "UC-03 said" below is a ``StatusUpdate`` built by
``sub_agent.mock_handoff``, a pure function. The run demonstrates that UC-02
handles a report correctly. It is not evidence that a document was validated,
a laptop was shipped, a visa was filed or anyone was notified.

This script contains no business logic: it calls the public
``OnboardingService`` API and prints what comes back.

Run from the repository root:

    python demo/uc02_cross_use_case.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from onboarding_task_management.contracts import (  # noqa: E402
    Actor,
    DecisionOutcome,
    ExternalOutcome,
    Role,
    SourceUseCase,
    TaskStatus,
)
from onboarding_task_management.sub_agent import mock_handoff  # noqa: E402
from onboarding_task_management.sub_agent.service import (  # noqa: E402
    OnboardingService,
)
from onboarding_task_management.synthetic import (  # noqa: E402
    CLIENT_ALPHA,
    build_registry,
    make_trigger,
    persona,
)

HR = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id=CLIENT_ALPHA)
HR_APPROVER = Actor(actor_id="U-HR-2", role=Role.HR_OPS, client_id=CLIENT_ALPHA)

#: Explicit: UC-02 reads no clock on an inbound path.
OCCURRED = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
AS_OF = date(2026, 11, 2)

#: HR's assignment decisions. A human made these; UC-02 only records them.
ASSIGNMENTS = [
    ("hr_record_create", "U-HR-1", "HR Operations"),
    ("collect_documents", "U-HR-9", "HR Shared Services"),
    ("it_laptop", "U-IT-7", "IT Service Desk"),
    ("orientation_schedule", "U-HR-3", "Talent Development"),
    ("medical_test", "U-HR-5", "Employee Services"),
    ("insurance_enrolment", "U-HR-6", "Benefits"),
    ("travel_booking", "U-HR-4", "Mobility"),
    ("accommodation_booking", "U-FAC-2", "Facilities"),
    ("salary_transfer_letter", "U-HRO-2", "HR Operations"),
]


def header(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


class Demo:
    def __init__(self) -> None:
        # DEMO-ONLY identity registration: the demo reports as U-HR-1 for every source.
        self.service = OnboardingService(
            build_registry(),
            source_principals={s: frozenset({"U-HR-1"}) for s in SourceUseCase},
        )
        record = self.service.create_onboarding(
            HR, make_trigger(persona("EMP-007"))
        )
        self.oid = record.onboarding_id

    # -- helpers ---------------------------------------------------------

    @property
    def record(self):
        return self.service.get(HR, self.oid)

    def task(self, key: str):
        return self.record.task_by_key(key)

    def task_id(self, key: str) -> str:
        return f"{self.oid}#{key}"

    def status_of(self, key: str) -> str:
        task = self.task(key)
        return task.status.value if task else "-"

    def report(
        self,
        key: str,
        source: SourceUseCase,
        outcome: ExternalOutcome,
        *,
        blocking_reason: str = "",
        attempt: int = 1,
    ):
        """Mint a MOCK report and hand it to UC-02."""
        update = mock_handoff.MOCK_ADAPTERS[source](
            self.task(key),
            outcome,
            OCCURRED,
            blocking_reason=blocking_reason,
            attempt=attempt,
        )
        return self.service.apply_status_update(HR, self.oid, update)

    # -- steps -----------------------------------------------------------

    def show_plan(self) -> None:
        header("1. CHECKLIST GENERATED - EVERY TASK STARTS UNASSIGNED")
        record = self.record
        unassigned = self.service.unassigned_tasks(HR, self.oid)
        print(f"  employee            : {record.employee_id}")
        print(f"  joining date        : {record.joining_date}")
        print(f"  tasks generated     : {len(record.tasks)}")
        print(f"  awaiting assignment : {len(unassigned)}")
        print("\n  A template's owner ROLE is not an assignment:")
        for task in unassigned[:4]:
            print(
                f"    {task.template_key:28} owner_role={task.owner.value:12} "
                f"-> {task.assignment_status.value}"
            )

    def assign_owners(self) -> None:
        header("2. HR MANUALLY ASSIGNS OWNERS")
        print("  No agent and no model chooses these. HR decides; UC-02 records.\n")
        for key, who, team in ASSIGNMENTS:
            if self.task(key) is None:
                continue
            task = self.service.assign_owner(
                HR, self.oid, self.task_id(key), who, team=team
            )
            print(f"    {key:28} -> {task.assignee_id:9} ({task.assignee_team})")
        print(
            f"\n  still awaiting HR assignment: "
            f"{len(self.service.unassigned_tasks(HR, self.oid))}"
        )

    def document_rejection(self) -> None:
        header("3. UC-03 (MOCK) REPORTS THE DOCUMENT INVALID")
        self.service.complete_task(HR, self.oid, self.task_id("hr_record_create"))
        result = self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.INVALID,
            blocking_reason="identity document scan unreadable; re-submission requested",
        )
        print(f"  accepted            : {result.accepted}")
        print(f"  task status         : {self.status_of('collect_documents')}")
        print(f"  follow-up raised    : {result.follow_up_raised}")
        print("\n  Dependants stay blocked because the prerequisite is unresolved:")
        for key in ("payroll_setup", "visa_pack_handover", "background_verification"):
            print(f"    {key:28} {self.status_of(key)}")
        print("\n  INVALID is not fraud. UC-02 never promotes one to the other.")
        print(
            f"    recorded outcome          : "
            f"{self.task('collect_documents').external_outcome.value}"
        )

    def show_follow_ups(self) -> None:
        header("4. FOLLOW-UP FLAGS NAME WHO SHOULD BE CHASED")
        flags = self.service.follow_up_flags(HR, self.oid, AS_OF)
        shown = [f for f in flags if f.code != "unassigned"][:3]
        for flag in shown or flags[:3]:
            print(f"    [{flag.code}] {flag.template_key}")
            print(f"        chase    : {flag.follow_up_with}")
            print(f"        reason   : {flag.reason}")
        print(f"\n  total flags: {len(flags)}")
        print("  NOT DELIVERED. UC-02 has no email/Teams transport of any kind:")
        print(f"    delivered={flags[0].delivered}  {flags[0].delivery_note}")

    def document_correction(self) -> None:
        header("5. A CORRECTED DOCUMENT VALIDATES (MOCK UC-03)")
        result = self.report(
            "collect_documents",
            SourceUseCase.UC03_DOCUMENT_VALIDATION,
            ExternalOutcome.VALID,
            attempt=2,
        )
        print(f"  task status         : {self.status_of('collect_documents')}")
        print("  unblocked by this update:")
        for task_id in result.unblocked_task_ids:
            print(f"    {task_id.split('#', 1)[1]}")

    def equipment(self) -> None:
        header("6. UC-04 (MOCK): LAPTOP DELAYED, THEN COMPLETED")
        self.report(
            "it_laptop",
            SourceUseCase.UC04_EQUIPMENT_ACCESS,
            ExternalOutcome.DELAYED,
            blocking_reason="stock arrives week 47",
        )
        print(f"  after DELAYED  : it_laptop={self.status_of('it_laptop')}, "
              f"it_accounts={self.status_of('it_accounts')}")
        result = self.report(
            "it_laptop",
            SourceUseCase.UC04_EQUIPMENT_ACCESS,
            ExternalOutcome.COMPLETED,
            attempt=2,
        )
        print(f"  after COMPLETED: it_laptop={self.status_of('it_laptop')}, "
              f"it_accounts={self.status_of('it_accounts')}")
        print(f"  reference recorded: {self.task('it_laptop').external_reference}")
        print("    ^ the MOCK- prefix marks this as simulated, not a real ticket")
        _ = result

    def orientation_and_operations(self) -> None:
        header("7. UC-05 AND THE OPERATIONAL TEAM (ALL MOCK)")
        self.report(
            "orientation_schedule",
            SourceUseCase.UC05_ORIENTATION,
            ExternalOutcome.COMPLETED,
        )
        print(f"  orientation_schedule  {self.status_of('orientation_schedule'):10} "
              f"-> policy_ack {self.status_of('policy_ack')}")
        for key in (
            "medical_test",
            "insurance_enrolment",
            "travel_booking",
            "accommodation_booking",
            "food_arrangement",
            "salary_transfer_letter",
            "non_liability_letter",
            "visa_pack_handover",
        ):
            if self.task(key) is None:
                continue
            result = self.report(
                key, SourceUseCase.OPERATIONS, ExternalOutcome.COMPLETED
            )
            verdict = "ok" if result.accepted else f"REFUSED ({result.rejection_code})"
            print(f"    {key:28} {self.status_of(key):10} {verdict}")
        print(f"\n  biometrics_capture now {self.status_of('biometrics_capture')} "
              f"(it depends on the medical test)")

    def refusals(self) -> None:
        header("8. UPDATES THAT MUST BE REFUSED")
        cases = [
            (
                "UC-03 trying to close an equipment task",
                mock_handoff.uc03_document_result(
                    self.task("desk_allocation"), ExternalOutcome.COMPLETED, OCCURRED
                ),
            ),
            (
                "UC-04 alleging fraud (only the validator may)",
                mock_handoff.uc04_equipment_result(
                    self.task("desk_allocation"),
                    ExternalOutcome.SUSPECTED_FRAUD,
                    OCCURRED,
                    blocking_reason="wrong workflow",
                ),
            ),
            (
                "completing a task whose prerequisite is unresolved",
                mock_handoff.uc04_equipment_result(
                    self.task("access_apollo"), ExternalOutcome.COMPLETED, OCCURRED
                ),
            ),
        ]
        for label, update in cases:
            result = self.service.apply_status_update(HR, self.oid, update)
            print(f"    {label}")
            print(f"        accepted={result.accepted}  code={result.rejection_code}")

        header("9. A REPLAYED UPDATE IS APPLIED ONCE")
        update = mock_handoff.uc05_orientation_result(
            self.task("policy_ack"), ExternalOutcome.COMPLETED, OCCURRED
        )
        first = self.service.apply_status_update(HR, self.oid, update)
        second = self.service.apply_status_update(HR, self.oid, update)
        print(f"    first  : accepted={first.accepted} duplicate={first.duplicate_replay}")
        print(f"    replay : accepted={second.accepted} duplicate={second.duplicate_replay}")

    def replan(self) -> None:
        header("10. JOINING-DATE CHANGE REPLANS OPEN WORK")
        done_before = {
            t.template_key
            for t in self.record.tasks
            if t.status is TaskStatus.COMPLETE
        }
        before = self.task("desk_allocation").due_date
        new_joining = self.record.joining_date + timedelta(days=14)
        request = self.service.request_change(HR, self.oid, joining_date=new_joining)
        self.service.decide_change(
            HR_APPROVER, request.request_id, DecisionOutcome.APPROVED
        )
        self.service.apply_change(HR, request.request_id)
        done_after = {
            t.template_key
            for t in self.record.tasks
            if t.status is TaskStatus.COMPLETE
        }
        print(f"  joining date        : {new_joining} (gate-approved)")
        print(f"  desk_allocation due : {before} -> "
              f"{self.task('desk_allocation').due_date}")
        print(f"  completed preserved : {done_before == done_after} "
              f"({len(done_after)} tasks)")
        keys = [t.template_key for t in self.record.tasks]
        print(f"  no duplicate tasks  : {len(keys) == len(set(keys))}")

    def readiness(self) -> None:
        header("11. CONSOLIDATED PLAN AND READINESS REPORT")
        summary = self.service.status(HR, self.oid, AS_OF)
        print(f"  total tasks   : {summary.total_tasks}")
        print(f"  complete      : {summary.complete}")
        print(f"  in progress   : {summary.in_progress}")
        print(f"  ready         : {summary.ready}")
        print(f"  blocked       : {summary.blocked}")
        print(f"  on track      : {summary.is_on_track}")
        unassigned = self.service.unassigned_tasks(HR, self.oid)
        print(f"  still awaiting HR assignment : {len(unassigned)}")
        for task in unassigned[:5]:
            print(f"    {task.template_key:28} due {task.due_date}")
        flags = self.service.follow_up_flags(HR, self.oid, AS_OF)
        print(f"  follow-up flags : {len(flags)} (none delivered)")


def main() -> int:
    demo = Demo()
    demo.show_plan()
    demo.assign_owners()
    demo.document_rejection()
    demo.show_follow_ups()
    demo.document_correction()
    demo.equipment()
    demo.orientation_and_operations()
    demo.refusals()
    demo.replan()
    demo.readiness()

    header("DONE")
    print("  Synthetic data only. No external system was contacted.")
    print("  Every UC-03 / UC-04 / UC-05 / operations result above came from")
    print("  a deterministic MOCK adapter. No approved cross-use-case")
    print("  interface exists, and UC-02 ships no transport: nothing was")
    print("  validated, provisioned, booked, filed or notified for real.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
