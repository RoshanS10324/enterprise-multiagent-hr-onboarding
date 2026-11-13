"""UC-02 dynamic checklist demonstration: two employees, two checklists.

Shows that the onboarding checklist is generated from the employee context and
the client's approved configuration — not from a fixed list — and that every
task carries the reason it was included or left out.

The script contains **no business logic**. It builds two synthetic contexts,
calls ``OnboardingService.preview_checklist`` / ``OnboardingService.checklist``
and prints what comes back. It is deliberately outside ``src/`` so it is not
part of the package.

Run from the repository root:

    python demo/uc02_dynamic_checklist.py                  # EMP-008 vs EMP-007
    python demo/uc02_dynamic_checklist.py EMP-001 EMP-003  # any two personas

Nothing is written, sent or persisted. Dates are fixed so every run is
identical.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from onboarding_task_management.contracts import (  # noqa: E402
    Actor,
    ChecklistContext,
    ChecklistIssueSeverity,
    ChecklistView,
    Role,
)
from onboarding_task_management.sub_agent.service import OnboardingService  # noqa: E402
from onboarding_task_management.synthetic import (  # noqa: E402
    DEFAULT_JOINING_DATE,
    build_registry,
    make_trigger,
    persona,
)

#: Fixed evaluation date so the demo is reproducible.
AS_OF = date(2026, 10, 26)

DEFAULT_PAIR = ("EMP-008", "EMP-007")


def _enable_utf8() -> bool:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        return True
    except Exception:
        return False


UTF8 = _enable_utf8()


def safe(text: str) -> str:
    if UTF8:
        return text
    return text.encode("ascii", "replace").decode("ascii")


def header(title: str) -> None:
    print()
    print("=" * 94)
    print(f"  {title}")
    print("=" * 94)


def step(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 88 - len(title)))


def build(employee_id: str) -> ChecklistView:
    """Create one onboarding and return its explained checklist."""
    service = OnboardingService(build_registry())
    who = persona(employee_id)
    actor = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id=who.client_id)
    record = service.create_onboarding(
        actor, make_trigger(who, joining_date=DEFAULT_JOINING_DATE)
    )
    return service.checklist(actor, record.onboarding_id, AS_OF)


def show_context(employee_id: str, view: ChecklistView) -> None:
    who = persona(employee_id)
    context = view.context
    step(f"{employee_id} — {who.scenario}")
    print(f"  client        : {context.client_id}")
    print(f"  role / grade  : {context.role_id} / {context.grade or '(not supplied)'}")
    print(f"  location      : {context.location}   project: {context.project}")
    print(f"  joining date  : {context.joining_date}")
    print(
        "  attributes    : "
        + (", ".join(context.attributes) or "(none declared — no extended activity applies)")
    )
    print(
        "  nationality   : not stored by UC-02; group code "
        f"{context.nationality_group or '(not supplied — unfiltered)'}"
    )


def show_checklist(view: ChecklistView) -> None:
    print(f"\n  {len(view.entries)} task(s) generated, {len(view.excluded)} excluded\n")
    print(f"  {'TASK':28}{'OWNER':12}{'STATUS':10}{'DUE':12}{'APPROVER':12}WHY INCLUDED")
    print("  " + "-" * 92)
    for entry in view.entries:
        reason = entry.decision.reason.split(": ", 1)[-1]
        print(
            f"  {entry.template_key:28}{entry.owner.value:12}{entry.status.value:10}"
            f"{entry.due_date.isoformat():12}"
            f"{(entry.approval_role.value if entry.approval_role else '-'):12}"
            f"{reason[:36]}"
        )
        if entry.blocked_by_keys:
            print(f"  {'':28}blocked by: {', '.join(entry.blocked_by_keys)}")

    print("\n  Excluded, with the reason:")
    for decision in view.excluded:
        print(f"    - {decision.template_key:28}{decision.reason[:60]}")


def show_state(view: ChecklistView) -> None:
    def short(ids: list[str]) -> str:
        return ", ".join(i.split("#")[-1] for i in ids) or "(none)"

    print("\n  Derived state at as_of =", view.as_of)
    print(f"    ready            : {short(view.ready_task_ids)}")
    print(f"    blocked          : {short(view.blocked_task_ids)}")
    print(f"    in progress      : {short(view.in_progress_task_ids)}")
    print(f"    complete         : {short(view.complete_task_ids)}")
    print(f"    overdue          : {short(view.overdue_task_ids)}")
    print(f"    awaiting approval: {short(view.awaiting_approval_task_ids)}")
    print(f"    pending gates    : {', '.join(view.pending_gate_request_ids) or '(none)'}")

    print("\n  Issues for human review:")
    if not view.issues:
        print("    (none)")
    for issue in view.issues:
        marker = "!" if issue.severity is not ChecklistIssueSeverity.INFORMATIONAL else "i"
        print(f"    [{marker}] {issue.severity.value:17}{issue.code:26}{issue.subject}")
    print(f"\n  review_required  : {view.review_required}")


def compare(left_id: str, left: ChecklistView, right_id: str, right: ChecklistView) -> None:
    header(f"COMPARISON — {left_id} vs {right_id}")
    left_keys = {e.template_key for e in left.entries}
    right_keys = {e.template_key for e in right.entries}

    print(f"\n  tasks generated : {left_id} = {len(left_keys)}   {right_id} = {len(right_keys)}")
    print(f"\n  only for {left_id}:")
    for key in sorted(left_keys - right_keys) or ["(none)"]:
        print(f"    + {key}")
    print(f"\n  only for {right_id}:")
    for key in sorted(right_keys - left_keys) or ["(none)"]:
        print(f"    + {key}")
    print("\n  shared by both:")
    print("    " + ", ".join(sorted(left_keys & right_keys)))

    print("\n  Same template, different outcome — the decision that differed:")
    for key in sorted(right_keys - left_keys):
        decision = next(
            (d for d in left.excluded if d.template_key == key), None
        )
        if decision is None:
            continue
        print(safe(f"    {key}"))
        print(safe(f"      {left_id}: {decision.reason}"))
        included = next(e for e in right.entries if e.template_key == key)
        print(safe(f"      {right_id}: {included.decision.reason}"))


def show_what_if(employee_id: str) -> None:
    """Re-select the same employee as a relocation hire, without changing anything.

    ``preview_checklist`` answers "what would this context produce?" — it
    creates no record, so it is safe to call for planning questions.
    """
    header(f"WHAT-IF PREVIEW — {employee_id} as a relocation hire")
    who = persona(employee_id)
    actor = Actor(actor_id="U-HR-1", role=Role.HR_OPS, client_id=who.client_id)
    service = OnboardingService(build_registry())

    relocating = ChecklistContext(
        client_id=who.client_id,
        employee_id=who.employee_id,
        joining_date=DEFAULT_JOINING_DATE,
        location=who.location,
        project=who.project,
        role_id=who.role_id,
        grade=who.grade,
        attributes=["visa_required", "needs_travel", "needs_accommodation"],
    )
    preview = service.preview_checklist(actor, relocating)
    print()
    print(f"  declared attributes : {', '.join(relocating.attributes)}")
    print(f"  tasks that would be generated: {len(preview.included)}")
    for decision in preview.included:
        if decision.reason_code == "matched_all_filters":
            print(safe(f"    + {decision.template_key:28}{decision.reason}"))
    print()
    print(f"  review_required     : {preview.review_required}")
    for issue in preview.issues:
        print(f"    [{issue.severity.value}] {issue.code}: {issue.subject}")
    print()
    print("  Nothing was created — this is a selection preview only.")


def main(argv: list[str]) -> int:
    left_id, right_id = (argv + list(DEFAULT_PAIR))[:2] if argv else DEFAULT_PAIR
    try:
        left = build(left_id)
        right = build(right_id)
    except KeyError as exc:
        print(f"unknown persona: {exc}. Known personas are in synthetic.SYNTHETIC_PERSONAS.")
        return 2

    for employee_id, view in ((left_id, left), (right_id, right)):
        header(f"CHECKLIST — {employee_id}")
        show_context(employee_id, view)
        show_checklist(view)
        show_state(view)

    compare(left_id, left, right_id, right)
    show_what_if(left_id)

    header("WHAT THIS SHOWS")
    print(
        """
  * The checklist is selected per employee from the client's approved
    configuration. No task content is generated or inferred by UC-02.
  * Every included and excluded task carries a reason built only from the
    configured filter values and the supplied context values.
  * Readiness, blocking prerequisites, overdue state and approval
    requirements are derived from the record and an explicit as_of date.
  * Nothing was sent, written or persisted by this run.
"""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
