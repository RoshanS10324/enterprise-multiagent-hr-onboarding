"""The authoritative joining date, its revision, and whether the results that used it are current.

Source of truth: `state["joining_date"]` (ISO date). UC01 establishes it at intake. After that, only
an approved and applied UC02 DATE_CHANGE (adapters/uc02_tasks.py `relay_uc02_decision`) changes
it -- `plan_tasks` reads the date and never revises it. Alongside the value:

  joining_date_revision          1 once the date is first known, +1 on every change (0 while unknown)
  joining_date_history           one entry per revision: the date, the previous date, which stage
                                 and UC set it, the approval that authorized it, and when
  field_provenance.joining_date  who set the current value (stage, UC, approval id, revision)

There is no second copy of the date: `verified_context.joining_date` is kept equal to it.

Every stage records the date and revision it was handed (`StageOutcome.inputs`, built by
workflow.stage_inputs). A date-dependent stage's result is CURRENT when it was produced for the
current date. Date equality, not the revision number, decides it: a result produced for the current
date stays valid however many intermediate changes there were, so it is never re-run without need;
a result produced for any other date is STALE. UC05 also reports the start date it holds for the
case (`orientation_schedule_result.uc05_start_date`); UC05 has no operation that changes a case's
start date, so a difference there is a CONFLICT that a person resolves in UC05.

`reconcile` runs before every graph invocation (OnboardingOrchestratorAgent._invoke). It re-opens a
stale COMPLETE stage so the graph runs it again with the current date (a stage in any other runnable
status runs again anyway), leaves UNCERTAIN and FAILED stages to a person, and un-does a day-1
signoff given for another date. Day-1 readiness is refused while `consistency()` is not
`consistent` (orchestrator_agent.node_day_one_readiness).
"""

from __future__ import annotations

from typing import Any

from hr_onboarding_common.orchestration.state import OnboardingCaseState, Stage, StageStatus

# The stages whose result depends on the joining date: UC04 evaluates equipment/access lead times
# against it, and UC05's case holds it as its start date (UC05 schedules every session, the
# joining-day trigger and the welcome from that start date). buddy_assignment and team_introduction
# follow UC05's start date, which orientation_scheduling checks against the joining date -- they are
# covered through it and never re-run for a date change.
DATE_DEPENDENT_STAGES: tuple[Stage, ...] = (Stage.EQUIPMENT_ACCESS, Stage.ORIENTATION_SCHEDULING)
# Statuses the graph runs again on its own. A stale result in one of them is reconciled by that run.
_RERUN = frozenset({StageStatus.IN_PROGRESS.value, StageStatus.BLOCKED.value, StageStatus.AWAITING_APPROVAL.value,
                    StageStatus.RETRY_PENDING.value, StageStatus.WAITING.value, StageStatus.WAITING_FOR_INPUT.value})
# Statuses a person must resolve first: no write while an outcome is unknown, no run while terminal.
_MANUAL = frozenset({StageStatus.UNCERTAIN.value, StageStatus.FAILED.value})
_NOT_RUN = frozenset({StageStatus.PENDING.value, StageStatus.NOT_REQUESTED.value})

CONSISTENT, STALE, CONFLICT, NOT_STARTED = "consistent", "stale", "conflict", "not_started"
CHANGED_CODE = "JOINING_DATE_CHANGED"  # stage_history code of a stage re-opened by a date change


def revision(state: OnboardingCaseState) -> int:
    """The current revision; a state persisted before revisions existed counts its date as revision 1."""
    value = state.get("joining_date_revision")
    if value is None:
        return 1 if state.get("joining_date") else 0
    return int(value)


_FROM_STATE: Any = object()


def record_change(state: OnboardingCaseState, new_date: str, *, stage: Stage, use_case_id: str, source: str,
                  at: str, approval_id: str | None = None, execution_id: str | None = None,
                  previous: str | None = _FROM_STATE) -> dict[str, Any] | None:
    """Make `new_date` the authoritative joining date (in place): the next revision, its provenance
    and its history entry. Returns the history entry, or None when the date did not change.
    `previous` is the date before the change when `state` already carries the new value (a stage
    result); by default it is read from `state`."""
    previous = state.get("joining_date") if previous is _FROM_STATE else previous
    if not new_date or new_date == previous:
        return None
    number = revision(state) + 1 if previous else 1
    entry = {"revision": number, "joining_date": new_date, "previous_joining_date": previous, "stage": stage.value,
             "use_case_id": use_case_id, "source": source, "approval_id": approval_id, "at": at}
    state["joining_date"] = new_date
    state["joining_date_revision"] = number
    state["joining_date_history"] = [*(state.get("joining_date_history") or []), entry]
    provenance = {"stage": stage.value, "use_case_id": use_case_id, "source": source, "revision": number, "at": at}
    if approval_id is not None:
        provenance["approval_id"] = approval_id
    if execution_id is not None:
        provenance["execution_id"] = execution_id
    state["field_provenance"] = {**(state.get("field_provenance") or {}), "joining_date": provenance}
    if state.get("verified_context"):
        state["verified_context"] = {**state["verified_context"], "joining_date": new_date,
                                     "joining_date_revision": number}
    return entry


def used_date(state: OnboardingCaseState, stage: Stage) -> tuple[str | None, int | None]:
    """The joining date and revision `stage` was last handed (None for a stage that never ran, or one
    recorded before stage inputs existed)."""
    inputs = ((state.get("stage_outcomes") or {}).get(stage.value) or {}).get("inputs") or {}
    return inputs.get("joining_date"), inputs.get("joining_date_revision")


def _status(state: OnboardingCaseState, stage: Stage) -> str:
    return (state.get("stage_statuses") or {}).get(stage.value, StageStatus.PENDING.value)


def stale_results(state: OnboardingCaseState) -> list[dict[str, Any]]:
    """Date-dependent stages whose last run was for a date other than the current one."""
    current = state.get("joining_date")
    stale = []
    for stage in DATE_DEPENDENT_STAGES:
        status = _status(state, stage)
        used, used_revision = used_date(state, stage)
        if status in _NOT_RUN or used is None or used == current:
            continue
        action = "re_execute" if status == StageStatus.COMPLETE.value or status in _RERUN else "manual"
        stale.append({"stage": stage.value, "status": status, "used_joining_date": used,
                      "used_revision": used_revision, "action": action})
    return stale


def uc05_conflict(state: OnboardingCaseState) -> dict[str, Any] | None:
    """UC05's start date for the case when it differs from the joining date."""
    current = state.get("joining_date")
    held = (state.get("orientation_schedule_result") or {}).get("uc05_start_date")
    if current and held and str(held)[:10] != str(current)[:10]:
        return {"stage": Stage.ORIENTATION_SCHEDULING.value, "uc05_start_date": str(held)[:10],
                "status": _status(state, Stage.ORIENTATION_SCHEDULING)}
    return None


def consistency(state: OnboardingCaseState) -> dict[str, Any]:
    """The derived view: every date-dependent stage, the date it last used, and an overall status --
    `consistent`, `stale` (a result was produced for another date; it is being reconciled), `conflict`
    (UC05 holds another start date; a person aligns it) or `not_started`."""
    current = state.get("joining_date")
    stages = {}
    for stage in DATE_DEPENDENT_STAGES:
        used, used_revision = used_date(state, stage)
        status = _status(state, stage)
        stages[stage.value] = {"status": status, "used_joining_date": used, "used_revision": used_revision,
                               "current": None if status in _NOT_RUN or used is None else used == current}
    stale, conflict = stale_results(state), uc05_conflict(state)
    if stale:
        overall = STALE
    elif conflict:
        overall = CONFLICT
    elif all(v["current"] is None for v in stages.values()):
        overall = NOT_STARTED
    else:
        overall = CONSISTENT
    return {"joining_date": current, "revision": revision(state), "status": overall, "stages": stages,
            "stale": stale, "uc05_conflict": conflict}


def readiness_blocker(state: OnboardingCaseState) -> str | None:
    """Why day-1 readiness must not be signed off for the current joining date, or None."""
    view = consistency(state)
    if view["status"] == STALE:
        stages = ", ".join(f"{s['stage']} (ran for {s['used_joining_date']})" for s in view["stale"])
        return f"results produced for an earlier joining date are not reconciled yet: {stages}; " \
               f"the joining date is {view['joining_date']} (revision {view['revision']})"
    if view["status"] == CONFLICT:
        conflict = view["uc05_conflict"]
        return f"UC05 holds start date {conflict['uc05_start_date']} for this case; the joining date is " \
               f"{view['joining_date']} (revision {view['revision']})"
    return None


def reconcile(state: OnboardingCaseState, *, at: str) -> list[dict[str, Any]]:
    """Prepare the case for a run after a joining-date change (in place). Returns one action per
    stage it touched; an empty list when nothing was produced for another date.

      - a stale COMPLETE date-dependent stage is re-opened (IN_PROGRESS) so the graph runs it again
        with the current date; a stale stage in a runnable status runs again on its own;
      - a stale UNCERTAIN or FAILED stage is left for a person (reported, not re-run);
      - day-1 readiness evaluated for another date is re-opened (whether it was signed off, awaiting
        signoff or blocked), and its signoff no longer counts.

    Nothing else is changed: the UC results stay until the re-run replaces them, the history keeps
    every earlier event, and stages that do not depend on the date are not touched."""
    current = state.get("joining_date")
    actions = []
    for entry in stale_results(state):
        if entry["status"] == StageStatus.COMPLETE.value:
            _reopen(state, entry["stage"], at, entry["used_joining_date"], current)
        actions.append(entry)
    readiness_used, readiness_revision = used_date(state, Stage.DAY_ONE_READINESS)
    readiness = _status(state, Stage.DAY_ONE_READINESS)
    if readiness not in _NOT_RUN and readiness_used is not None and readiness_used != current:
        # Re-opened whatever it concluded: the graph only revisits readiness once every branch is
        # complete again, and until then an old "awaiting signoff" or "signed off" must not stand.
        if readiness not in (StageStatus.IN_PROGRESS.value, StageStatus.FAILED.value):
            _reopen(state, Stage.DAY_ONE_READINESS.value, at, readiness_used, current)
        if state.get("day_one_signoff_approved"):
            state["day_one_signoff_approved"] = False
        actions.append({"stage": Stage.DAY_ONE_READINESS.value, "status": readiness,
                        "used_joining_date": readiness_used, "used_revision": readiness_revision,
                        "action": "re_evaluate"})
    return actions


def _reopen(state: OnboardingCaseState, stage: str, at: str, used: str | None, current: str | None) -> None:
    state["stage_statuses"][stage] = StageStatus.IN_PROGRESS.value
    state.setdefault("stage_history", []).append({
        "stage": stage, "status": StageStatus.IN_PROGRESS.value, "at": at, "code": CHANGED_CODE,
        "detail": f"ran for {used}; the joining date is now {current}"})
