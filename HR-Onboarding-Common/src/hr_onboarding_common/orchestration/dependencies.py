"""The one place stage-to-stage dependencies are defined.

Each stage lists what it needs from earlier stages: the upstream stage must be COMPLETE, and must
have produced the named state fields. The stage wrapper (`OnboardingOrchestratorAgent._execute_stage`)
evaluates them before any UC is called; a stage whose required dependency is unmet is BLOCKED and
its UC is never invoked:

  PREREQUISITE_NOT_MET  the upstream stage is not complete yet -- a wait (canonical status PENDING)
  MISSING_DATA          the upstream stage completed without a field this stage needs -- a person
                        corrects the upstream record, then resumes

The graph's edges already order the stages; this registry makes the prerequisites explicit, checks
them on every path that can run a stage (resume, retry, re-dispatch, a stage adapter override), and
records each check in the stage's outcome. UC-specific input validation (UC02's five role fields,
UC04's client configuration, UC05's offer-acceptance event) stays in each adapter: it belongs to that
UC's contract, not to the order of the workflow.

Optional dependencies never block: when unmet, the stage runs and its response carries a
DEPENDENCY_OPTIONAL_UNMET warning. A tenant may make a *configurable* optional dependency required
(config/tenants.yaml, `dependencies:`) -- e.g. require UC04 equipment/access readiness before UC05
schedules the orientation. A required dependency can never be relaxed by configuration.

Business rationale of each dependency (the UC01 -> UC05 chain of the onboarding process):

  UC01 -> UC02  a valid employee record with the employee number and the joining date (initial plan)
  UC02 -> UC03  the plan's versioned document requirements (onboarding_plan.required_documents):
                UC03 collects, stores and validates exactly what the current plan version requires
  UC03 -> UC02  replanning is not a stage dependency: UC03's feedback routes the graph to replan_tasks
                (orchestrator_agent.evaluate_document_result), which needs the plan it revises
  UC03 -> UC04  documents verified (no equipment or access for an unverified hire)
  UC03 -> UC05  documents verified; UC04 -> UC05 equipment/access readiness is optional for the
                orientation (no UC05 contract consumes it; a tenant may require it) and required for
                the joining-day welcome, through HR's day-1 signoff (which requires UC04 complete)
  UC02 -> UC04  the onboarding task plan must exist before UC04 evaluates equipment/access readiness
  UC02 -> UC05  the same task plan, for the same reason, before UC05 schedules the orientation
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from hr_onboarding_common.orchestration.agent_contract import USE_CASE_OF, AgentWarning, DependencyCheck
from hr_onboarding_common.orchestration.state import (DEPENDENCY_WAIT_CODE, READINESS_STAGES, OnboardingCaseState,
                                                      Stage, StageStatus)

if TYPE_CHECKING:  # tenant_config imports this module (to validate overrides); never the reverse at runtime
    from hr_onboarding_common.orchestration.tenant_config import TenantConfig

PREREQUISITE_NOT_MET = DEPENDENCY_WAIT_CODE
MISSING_DATA = "MISSING_DATA"


class DependencyKind(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


@dataclass(frozen=True)
class Dependency:
    key: str  # stable id, e.g. "uc03.documents_verified"; tenant overrides refer to it
    upstream: Stage
    provides: str  # what the downstream stage needs from it, in business terms
    # State fields the upstream must have produced (dotted paths) -- only those the downstream stage
    # actually consumes; a readiness-only dependency lists none.
    fields: tuple[str, ...] = ()
    kind: DependencyKind = DependencyKind.REQUIRED
    configurable: bool = False  # an optional dependency a tenant may make required


def _dep(key: str, upstream: Stage, provides: str, *fields: str, kind: DependencyKind = DependencyKind.REQUIRED,
         configurable: bool = False) -> Dependency:
    return Dependency(key=key, upstream=upstream, provides=provides, fields=fields, kind=kind,
                      configurable=configurable)


_EMPLOYEE_RECORD = _dep("uc01.employee_record", Stage.INTAKE,
                        "a valid employee record and the employee number UC01 issued", "employee_id")
_JOINING_DATE = _dep("uc01.joining_date", Stage.INTAKE, "the confirmed joining date", "joining_date")
_DOCUMENT_REQUIREMENTS = _dep("uc02.document_requirements", Stage.PLAN_TASKS,
                              "the plan's versioned document requirements",
                              "onboarding_plan.required_documents")
_DOCUMENT_REQUEST = _dep("uc03.document_collection", Stage.DOCUMENT_COLLECTION,
                         "the document collection opened for the current plan version and the document request",
                         "document_collection.required_documents")
_DOCUMENTS_SUPPLIED = _dep("uc03.documents_supplied", Stage.DOCUMENT_SUBMISSION,
                           "every required document supplied or on file")
_DOCUMENTS_VERIFIED = _dep("uc03.documents_verified", Stage.VALIDATE_DOCUMENTS,
                           "every required document verified (document clearance)",
                           "verified_context.documents_verified")
_TASK_PLAN = _dep("uc02.task_plan", Stage.PLAN_TASKS,
                  "the onboarding task plan must exist before equipment/access and orientation begin", "task_plan")
_PLAN_TO_REVISE = _dep("uc02.onboarding_plan", Stage.PLAN_TASKS, "the onboarding plan a replan revises",
                       "onboarding_plan.plan_id")
# Readiness only: no UC05 operation consumes UC04's result itself.
_EQUIPMENT_READY = _dep("uc04.equipment_access_ready", Stage.EQUIPMENT_ACCESS,
                        "equipment and system access ready, approvals applied")

STAGE_DEPENDENCIES: dict[Stage, tuple[Dependency, ...]] = {
    Stage.INTAKE: (),
    Stage.PLAN_TASKS: (_EMPLOYEE_RECORD, _JOINING_DATE),
    Stage.DOCUMENT_COLLECTION: (_EMPLOYEE_RECORD, _DOCUMENT_REQUIREMENTS),
    Stage.DOCUMENT_SUBMISSION: (_DOCUMENT_REQUEST,),
    Stage.VALIDATE_DOCUMENTS: (_EMPLOYEE_RECORD, _DOCUMENT_REQUEST, _DOCUMENTS_SUPPLIED),
    Stage.REPLAN_TASKS: (_PLAN_TO_REVISE, _EMPLOYEE_RECORD),
    Stage.EQUIPMENT_ACCESS: (_DOCUMENTS_VERIFIED, _EMPLOYEE_RECORD, _JOINING_DATE, _TASK_PLAN),
    Stage.ORIENTATION_SCHEDULING: (
        _DOCUMENTS_VERIFIED, _JOINING_DATE, _TASK_PLAN,
        _dep(_EQUIPMENT_READY.key, Stage.EQUIPMENT_ACCESS, _EQUIPMENT_READY.provides, *_EQUIPMENT_READY.fields,
             kind=DependencyKind.OPTIONAL, configurable=True),
    ),
    Stage.BUDDY_ASSIGNMENT: (_dep("uc05.induction_confirmed", Stage.ORIENTATION_SCHEDULING,
                                  "the induction confirmed by HR (UC05 proposes the buddy after it)"),),
    Stage.TEAM_INTRODUCTION: (_dep("uc05.buddy_assigned", Stage.BUDDY_ASSIGNMENT,
                                   "a buddy assigned (UC05 drafts the team introduction after it)"),),
    Stage.BUDDY_INTRODUCTION: (_dep("uc05.buddy_assigned", Stage.BUDDY_ASSIGNMENT, "a buddy assigned"),),
    Stage.DAY_ONE_READINESS: tuple(
        _EQUIPMENT_READY if s == Stage.EQUIPMENT_ACCESS else _dep(f"{USE_CASE_OF[s].lower()}.{s.value}", s,
                                                                  f"{s.value} complete")
        for s in READINESS_STAGES),
    # UC04 readiness reaches the welcome through HR's day-1 signoff, which requires it (above).
    Stage.JOINING_DAY_WELCOME: (
        _dep("orchestrator.day_one_signoff", Stage.DAY_ONE_READINESS,
             "day-1 readiness signed off by HR (every UC01-UC05 readiness stage complete, UC04 included)"),
    ),
}


class DependencyConfigurationError(ValueError):
    pass


def validate_overrides(overrides: dict[str, dict[str, str]]) -> None:
    """Tenant overrides (`{stage: {dependency key: kind}}`) may only make a configurable optional
    dependency required (or restate a dependency's own kind). Raises DependencyConfigurationError."""
    for stage_value, by_key in overrides.items():
        try:
            stage = Stage(stage_value)
        except ValueError:
            raise DependencyConfigurationError(f"unknown stage '{stage_value}' in dependency overrides") from None
        declared = {d.key: d for d in STAGE_DEPENDENCIES[stage]}
        for key, kind in by_key.items():
            dependency = declared.get(key)
            if dependency is None:
                raise DependencyConfigurationError(f"{stage_value} has no dependency '{key}'")
            if DependencyKind(kind) == dependency.kind:
                continue
            if not dependency.configurable or dependency.kind == DependencyKind.REQUIRED:
                raise DependencyConfigurationError(
                    f"{stage_value}: '{key}' is {dependency.kind} and cannot be configured as {kind}")


def dependencies_for(stage: Stage, client_id: str | None = None,
                     tenants: TenantConfig | None = None) -> tuple[Dependency, ...]:
    """The stage's dependencies with the tenant's overrides applied."""
    overrides = (tenants.dependencies_for(client_id) if tenants is not None and client_id else {}).get(stage.value, {})
    result = []
    for dependency in STAGE_DEPENDENCIES[stage]:
        kind = overrides.get(dependency.key)
        if kind is not None and dependency.configurable:
            dependency = Dependency(key=dependency.key, upstream=dependency.upstream, provides=dependency.provides,
                                    fields=dependency.fields, kind=DependencyKind(kind), configurable=True)
        result.append(dependency)
    return tuple(result)


def _iso_date(value: Any) -> bool:
    try:
        date.fromisoformat(str(value))
    except ValueError:
        return False
    return True


# Fields that must also be well formed, not just present: an unparseable joining date is no date,
# and no stage that needs one (plan_tasks, equipment_access, orientation_scheduling) runs on it.
_WELL_FORMED = {"joining_date": _iso_date}


def _present(state: OnboardingCaseState, path: str) -> bool:
    value: Any = state
    for part in path.split("."):
        if not isinstance(value, dict):
            return False
        value = value.get(part)
    if not value:
        return False
    check = _WELL_FORMED.get(path)
    return check(value) if check is not None else True


def evaluate(stage: Stage, state: OnboardingCaseState, tenants: TenantConfig | None = None) -> list[DependencyCheck]:
    """Every dependency of `stage`, checked against `state`."""
    checks = []
    for dependency in dependencies_for(stage, state.get("client_id"), tenants):
        status = (state.get("stage_statuses") or {}).get(dependency.upstream.value, StageStatus.PENDING.value)
        missing = [f for f in dependency.fields if not _present(state, f)]
        if status != StageStatus.COMPLETE.value:
            code, detail = PREREQUISITE_NOT_MET, f"{dependency.upstream.value} is {status}: {dependency.provides}"
        elif missing:
            code, detail = MISSING_DATA, (f"{dependency.upstream.value} completed without a valid "
                                          f"{', '.join(missing)}: {dependency.provides}")
        else:
            code, detail = None, ""
        checks.append(DependencyCheck(key=dependency.key, upstream_stage=dependency.upstream.value,
                                      use_case_id=USE_CASE_OF[dependency.upstream], kind=dependency.kind.value,
                                      satisfied=code is None, code=code, detail=detail))
    return checks


def blocking(checks: list[DependencyCheck]) -> list[DependencyCheck]:
    return [c for c in checks if not c.satisfied and c.kind == DependencyKind.REQUIRED.value]


def optional_warnings(checks: list[DependencyCheck]) -> list[AgentWarning]:
    return [AgentWarning(code="DEPENDENCY_OPTIONAL_UNMET", detail=f"{c.key}: {c.detail}")
            for c in checks if not c.satisfied and c.kind == DependencyKind.OPTIONAL.value]


def unmet(stage: Stage, state: OnboardingCaseState, tenants: TenantConfig | None = None) -> list[DependencyCheck]:
    """The required dependencies of `stage` that `state` does not satisfy."""
    return blocking(evaluate(stage, state, tenants))


def dependency_block(state: OnboardingCaseState, stage: Stage, unmet_checks: list[DependencyCheck]
                     ) -> OnboardingCaseState:
    """`stage` BLOCKED by its unmet required dependencies; nothing else is changed and no UC is called.
    MISSING_DATA when an upstream stage completed without what is needed (a correction), otherwise
    PREREQUISITE_NOT_MET (a wait for the earliest unmet upstream stage)."""
    from hr_onboarding_common.orchestration.adapters._shared import add_blocker, record_stage_error, \
        with_stage_status

    missing = [c for c in unmet_checks if c.code == MISSING_DATA]
    code = MISSING_DATA if missing else PREREQUISITE_NOT_MET
    first = (missing or unmet_checks)[0]
    detail = "; ".join(c.detail for c in (missing or unmet_checks))
    new_state = with_stage_status(copy.deepcopy(state), stage, StageStatus.BLOCKED)
    record_stage_error(new_state, stage, code=code, detail=detail, retryable=True)
    if code == MISSING_DATA:
        new_state["next_action"] = f"hr_review:{stage.value}:missing_data"
        action = f"Correct the {first.upstream_stage} record ({first.use_case_id}), then resume"
    else:
        new_state["next_action"] = f"await:{first.upstream_stage}"
        action = f"Wait for {first.upstream_stage} ({first.use_case_id}) to complete"
    add_blocker(new_state, stage=stage, code=code, reason=detail, owner="hr_admin", next_action=action)
    return new_state


def dependency_graph(client_id: str | None = None, tenants: TenantConfig | None = None) -> dict[str, Any]:
    """The registry as data (for the API and the documentation), with a tenant's overrides applied."""
    return {stage.value: [{"key": d.key, "upstream_stage": d.upstream.value, "use_case_id": USE_CASE_OF[d.upstream],
                           "provides": d.provides, "fields": list(d.fields), "kind": d.kind.value,
                           "configurable": d.configurable}
                          for d in dependencies_for(stage, client_id, tenants)]
            for stage in STAGE_DEPENDENCIES}
