"""UC-04's link to UC-02's onboarding plan: the plan's service tasks, read from UC-02 itself before UC-04 evaluates.

UC-02 is the source of truth for the onboarding plan. The case keeps only a summary of it (`task_plan`, without task
categories), so the plan is read from UC-02's own service (`OnboardingService.get`, an audited read under UC-02's
client isolation) through the orchestrator's one path into UC-02 (`uc02_tasks.uc02_sub_agent`). Nothing is written to
UC-02, no plan is created, and no other source is used when UC-02 cannot answer.

The plan is accepted only when it is THIS case's plan: its onboarding id is the one the case recorded, and its case id,
client id and employee id are the case's. Employee numbers can repeat between cases (a dev numbering restarts), so the
employee alone never identifies a plan. Otherwise UC-04 is blocked:

  UC02_PLAN_MISSING        the case names no UC-02 plan, or UC-02 holds no record under that id
  UC02_PLAN_CASE_MISMATCH  the record belongs to another case, client or employee (never attached)
  UC02_PLAN_UNAVAILABLE    UC-02 could not be read (retryable)

Service tasks are the plan's tasks in SERVICE_CATEGORIES. `workplace` (desk, building access card) is recorded for
traceability only. Two checks are ADVISORY, recorded on the stage, never blocking:

  UC02_PLAN_NO_SERVICE_TASKS      the plan holds no service task
  UC02_CONFIG_VERSION_MISMATCH    UC-02 planned from another configuration version than UC-04 evaluated
  UC02_DUE_BEFORE_UC04_LEAD_TIME  a UC-02 it_provisioning / access_management task is due before UC-04's earliest
                                  lead-time date (the date by which UC-04 must raise a request). No task-to-item
                                  mapping exists in either use case, so the earliest UC-04 date is the comparison:
                                  a warning means the task is due before UC-04 needs to raise any request at all.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from hr_onboarding_common.observability.logging import get_logger
from hr_onboarding_common.orchestration.state import OnboardingCaseState

log = get_logger("orchestration.uc04_uc02_plan")

SERVICE_CATEGORIES = ("it_provisioning", "access_management", "workplace")
DUE_DATE_CATEGORIES = ("it_provisioning", "access_management")  # workplace: traceability only

PLAN_MISSING = "UC02_PLAN_MISSING"
PLAN_CASE_MISMATCH = "UC02_PLAN_CASE_MISMATCH"
PLAN_UNAVAILABLE = "UC02_PLAN_UNAVAILABLE"
NO_SERVICE_TASKS = "UC02_PLAN_NO_SERVICE_TASKS"
CONFIG_VERSION_MISMATCH = "UC02_CONFIG_VERSION_MISMATCH"
DUE_BEFORE_LEAD_TIME = "UC02_DUE_BEFORE_UC04_LEAD_TIME"

_NEXT_ACTION = {
    PLAN_MISSING: "Run UC-02 task planning for this case, then retry UC04",
    PLAN_CASE_MISMATCH: "Re-plan this case in UC-02 so it has its own plan (another case's plan is never used), "
                        "then retry UC04",
    PLAN_UNAVAILABLE: "Retry UC04 when UC-02's plan store is reachable",
}
# The task fields UC-04 takes from the plan: identifiers, category, owner role, status and dates. No names.
_TASK_FIELDS = ("task_id", "template_key", "category", "owner", "status", "due_date", "approval_role",
                "evidence_required")


class Uc02PlanError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail

    @property
    def next_action(self) -> str:
        return _NEXT_ACTION[self.code]


PlanReader = Callable[[OnboardingCaseState, Any, str], dict]


def service_plan_reader(state: OnboardingCaseState, deps: Any, onboarding_id: str) -> dict:
    """UC-02's stored record for `onboarding_id`, read as HR operations for the case's client (as the orchestrator's
    other UC-02 calls do). UC-02 refuses another client's record itself (DataIsolationError, audited)."""
    from onboarding_task_management.contracts import Actor, Role  # type: ignore
    from onboarding_task_management.errors import DataIsolationError, InvalidStateError  # type: ignore

    from hr_onboarding_common.orchestration.adapters.uc02_tasks import uc02_sub_agent

    actor = Actor(actor_id=state["actor"]["on_behalf_of"], role=Role.HR_OPS, client_id=state["client_id"])
    try:
        record = uc02_sub_agent(deps).service.get(actor, onboarding_id)
    except InvalidStateError:
        raise Uc02PlanError(PLAN_MISSING, f"UC-02 holds no onboarding plan {onboarding_id}") from None
    except DataIsolationError:
        raise Uc02PlanError(PLAN_CASE_MISMATCH, f"UC-02 plan {onboarding_id} belongs to another client") from None
    plan = record.model_dump(mode="json")
    plan["source"] = "uc02"
    return plan


def read_plan(state: OnboardingCaseState, deps: Any) -> dict:
    """This case's UC-02 plan, validated, projected to its service tasks. Raises Uc02PlanError (never a fallback)."""
    onboarding_id = (state.get("task_plan") or {}).get("onboarding_id")
    if not onboarding_id:
        raise Uc02PlanError(PLAN_MISSING, "the case records no UC-02 onboarding plan (task_plan.onboarding_id)")
    reader: PlanReader = getattr(deps, "uc02_plan_reader", None) or service_plan_reader
    try:
        record = reader(state, deps, onboarding_id)
    except Uc02PlanError:
        raise
    except Exception as exc:  # noqa: BLE001 -- any failure to read UC-02 is reported, never replaced
        raise Uc02PlanError(PLAN_UNAVAILABLE, f"UC-02 plan {onboarding_id} could not be read "
                                              f"({type(exc).__name__})") from None
    if not isinstance(record, dict):
        raise Uc02PlanError(PLAN_UNAVAILABLE, f"UC-02 returned no plan record for {onboarding_id}")
    expected = {"onboarding_id": onboarding_id, "case_id": state.get("case_id"), "client_id": state.get("client_id"),
                "employee_id": state.get("employee_id")}
    # Only the field names are reported: another case's identifiers are not copied into this case.
    differing = [k for k, v in expected.items() if not v or record.get(k) != v]
    if differing:
        raise Uc02PlanError(PLAN_CASE_MISMATCH, f"UC-02 plan {onboarding_id} is not this case's plan "
                                                f"({', '.join(differing)} differ); it was not used")
    tasks = [{k: t.get(k) for k in _TASK_FIELDS} for t in record.get("tasks") or []
             if isinstance(t, dict) and t.get("category") in SERVICE_CATEGORIES]
    return {
        "onboarding_id": onboarding_id, "source": record.get("source", "uc02"),
        "data_origin": record.get("data_origin") or "", "plan_version": record.get("plan_version"),
        "plan_status": record.get("plan_status"), "revision": record.get("revision"),
        "config_version": record.get("config_version") or "",
        "service_tasks": sorted(tasks, key=lambda t: (str(t.get("due_date")), str(t.get("task_id")))),
    }


def _as_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def advisory_checks(plan: dict, *, uc04_config_version: str | None, uc04_items: list[dict]) -> dict:
    """The plan link as recorded on the UC-04 stage: the plan, the two comparisons and their warnings.
    Informational only: nothing here changes UC-04's result or the stage status."""
    warnings: list[dict] = []
    tasks = plan.get("service_tasks") or []
    if not tasks:
        warnings.append({"code": NO_SERVICE_TASKS, "detail": f"UC-02 plan {plan['onboarding_id']} holds no "
                                                             f"{', '.join(SERVICE_CATEGORIES)} task"})

    uc02_version, uc04_version = plan.get("config_version") or "", uc04_config_version or ""
    if not uc02_version or not uc04_version:
        version_check = {"result": "not_comparable", "uc02": uc02_version or None, "uc04": uc04_version or None}
    else:
        version_check = {"result": "match" if uc02_version == uc04_version else "mismatch",
                         "uc02": uc02_version, "uc04": uc04_version}
        if uc02_version != uc04_version:
            warnings.append({"code": CONFIG_VERSION_MISMATCH,
                             "detail": f"UC-02 planned from configuration {uc02_version}; UC-04 evaluated "
                                       f"{uc04_version}"})

    uc04_dates = [d for d in (_as_date(i.get("due_date")) for i in uc04_items) if d]
    earliest = min(uc04_dates) if uc04_dates else None
    early = []
    if earliest:
        early = [t for t in tasks if t.get("category") in DUE_DATE_CATEGORIES
                 and (_as_date(t.get("due_date")) or earliest) < earliest]
        for t in early:
            warnings.append({"code": DUE_BEFORE_LEAD_TIME, "task_id": t.get("task_id"),
                             "template_key": t.get("template_key"),
                             "detail": f"UC-02 task {t.get('template_key')} is due {t.get('due_date')}, before UC-04's "
                                       f"earliest lead-time date {earliest.isoformat()}"})
    due_check = {"result": "not_comparable" if earliest is None else ("early" if early else "ok"),
                 "uc04_earliest_lead_time_date": earliest.isoformat() if earliest else None,
                 "compared_categories": list(DUE_DATE_CATEGORIES)}
    return {**plan, "config_version_check": version_check, "due_date_check": due_check, "warnings": warnings,
            "advisory_only": True}


def log_plan(state: OnboardingCaseState, link: dict) -> None:
    log.info("orchestration.uc04_uc02_plan_linked", extra={
        "case_id": state.get("case_id"), "client_id": state.get("client_id"), "onboarding_id": link["onboarding_id"],
        "plan_version": link.get("plan_version"), "service_task_ids": [t["task_id"] for t in link["service_tasks"]],
        "warnings": [w["code"] for w in link.get("warnings", [])]})


def log_refusal(state: OnboardingCaseState, exc: Uc02PlanError) -> None:
    log.warning("orchestration.uc04_uc02_plan_refused", extra={
        "case_id": state.get("case_id"), "client_id": state.get("client_id"), "code": exc.code})
