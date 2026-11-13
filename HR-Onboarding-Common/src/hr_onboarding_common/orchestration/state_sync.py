"""State synchronization: what a stage execution may change in the shared case state, and how its
changes are validated before they enter it.

Every field of `OnboardingCaseState` has one authoritative owner:

  FIELD_OWNERS      business fields a stage produces (UC01 the employee identity and joining date, UC03
                    the validation results, UC04 the equipment/access result, ...). Only the owning
                    stage may change one.
  SCOPED_FIELDS     per-stage collections (statuses, errors, blockers, approvals, idempotency keys): a
                    stage may change only its own entries (and those of STAGE_SCOPE extras).
  BOOKKEEPING       derived or last-error fields every stage writes (recomputed by
                    workflow.refresh_workflow_view).
  everything else   the orchestrator's own: the tenant, the actor, the correlation id, submitted
                    documents, approval decisions, gate flags, the joining-day event, the stage wrapper's
                    history/attempt/outcome records. No stage adapter may change them.

`synchronize` compares the state handed to an adapter with what it returned and either accepts the
update -- keeping a previously known value the owner returned as missing (`NULL_OVERWRITE_IGNORED`),
and recording which execution produced each owned field (`field_provenance`) -- or rejects it whole:

  TENANT_CONTEXT_CONFLICT    the tenant changed, or a canonical employee identity of another tenant
  IDENTITY_CONFLICT          a known employee number, or UC05's canonical employee, replaced by another
  STATE_OWNERSHIP_VIOLATION  a field or another stage's entries changed by a stage that does not own them

A rejected update never enters the case state: the stage is FAILED (non-retryable, an operator
reconciles) and everything the adapter returned is discarded. The UC's own record of what it did is
untouched -- the next run re-reads it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from hr_onboarding_common.orchestration.agent_contract import USE_CASE_OF, AgentWarning
from hr_onboarding_common.orchestration.state import OnboardingCaseState, Stage

_UC05_STAGES = frozenset({Stage.ORIENTATION_SCHEDULING, Stage.BUDDY_ASSIGNMENT, Stage.TEAM_INTRODUCTION,
                          Stage.BUDDY_INTRODUCTION, Stage.JOINING_DAY_WELCOME})

FIELD_OWNERS: dict[str, frozenset[Stage]] = {
    # UC01: the employee record. UC01 also renames the case (its own case id) or links an existing one.
    "case_id": frozenset({Stage.INTAKE}),
    "employee_id": frozenset({Stage.INTAKE}),
    # UC01 sets the initial joining date; UC02 may later revise it through its own date-change gate
    # (adapters/uc02_tasks.py's relay_uc02_decision, outside the graph -- see its module docstring).
    # Its revision and history (joining_date.py) are the orchestrator's own: no stage writes them.
    "joining_date": frozenset({Stage.INTAKE, Stage.PLAN_TASKS}),
    "intake_trigger": frozenset({Stage.INTAKE}),
    # UC02 owns the onboarding plan: its initial version (plan_tasks) and every revision (replan_tasks).
    "task_plan": frozenset({Stage.PLAN_TASKS, Stage.REPLAN_TASKS}),
    "onboarding_plan": frozenset({Stage.PLAN_TASKS, Stage.REPLAN_TASKS}),
    "plan_history": frozenset({Stage.PLAN_TASKS, Stage.REPLAN_TASKS}),
    "revised_plan": frozenset({Stage.REPLAN_TASKS}),
    "requirement_change_requests": frozenset({Stage.REPLAN_TASKS}),  # marks the requests a replan applied
    # UC03 owns the document lifecycle: collection for a plan version, what is outstanding, validation,
    # and the structured feedback to UC02 (the submission wait reports HR requirement changes too).
    "document_collection": frozenset({Stage.DOCUMENT_COLLECTION, Stage.DOCUMENT_SUBMISSION}),
    "document_validation_result": frozenset({Stage.VALIDATE_DOCUMENTS}),
    "validation_issues": frozenset({Stage.VALIDATE_DOCUMENTS}),
    "correction_requirements": frozenset({Stage.VALIDATE_DOCUMENTS}),
    "evidence_references": frozenset({Stage.VALIDATE_DOCUMENTS}),
    "document_validation_feedback": frozenset({Stage.DOCUMENT_SUBMISSION, Stage.VALIDATE_DOCUMENTS}),
    # UC04
    "equipment_access_result": frozenset({Stage.EQUIPMENT_ACCESS}),
    # UC05: the canonical employee identity every UC05 call uses, and one result per UC05 stage. The team
    # introduction re-reads the optional buddy introduction from the same UC05 snapshot.
    "employee_ref": _UC05_STAGES,
    "orientation_schedule_result": frozenset({Stage.ORIENTATION_SCHEDULING}),
    "buddy_assignment_result": frozenset({Stage.BUDDY_ASSIGNMENT}),
    "team_introduction_result": frozenset({Stage.TEAM_INTRODUCTION}),
    "buddy_introduction_result": frozenset({Stage.TEAM_INTRODUCTION, Stage.BUDDY_INTRODUCTION}),
    "welcome_message_result": frozenset({Stage.JOINING_DAY_WELCOME}),
}

# The other stages whose statuses, errors, blockers and approvals a stage may also write.
STAGE_SCOPE: dict[Stage, frozenset[Stage]] = {
    Stage.TEAM_INTRODUCTION: frozenset({Stage.BUDDY_INTRODUCTION}),
}

SCOPED_FIELDS = frozenset({"stage_statuses", "stage_errors", "blockers", "pending_approvals", "idempotency_keys"})

BOOKKEEPING = frozenset({"current_stage", "overall_status", "workflow_status", "next_action", "error_code",
                         "error_detail", "outstanding_tasks", "updated_at", "pending_actions", "branch_statuses",
                         "completed_stages", "joining_date_consistency", "document_route"})

# Fields whose change is an identity/tenancy conflict rather than a plain ownership violation.
TENANT_FIELDS = frozenset({"client_id"})
# Owned fields that carry inputs, not outputs: never reported as updated and never given provenance.
# UC01's intake-only personal inputs pass through intake_trigger for one call and are stripped after it.
UNRECORDED_FIELDS = frozenset({"intake_trigger"})

_MISSING = object()


@dataclass
class SyncViolation:
    code: str
    field: str
    detail: str


@dataclass
class SyncResult:
    state: OnboardingCaseState
    violations: list[SyncViolation] = field(default_factory=list)
    warnings: list[AgentWarning] = field(default_factory=list)
    updated_fields: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return not self.violations


def _scope(stage: Stage) -> set[str]:
    return {stage.value} | {s.value for s in STAGE_SCOPE.get(stage, frozenset())}


def _scoped_violation(key: str, old: Any, new: Any, scope: set[str]) -> str | None:
    """Why `key` (a per-stage collection) changed outside the stage's scope, or None."""
    if key in ("stage_statuses", "stage_errors", "idempotency_keys"):
        old, new = old if isinstance(old, dict) else {}, new if isinstance(new, dict) else {}
        changed = {k for k in set(old) | set(new) if old.get(k, _MISSING) != new.get(k, _MISSING)}
        foreign = sorted(changed - scope)
        return f"{key} of {', '.join(foreign)}" if foreign else None
    old, new = old if isinstance(old, list) else [], new if isinstance(new, list) else []
    # blockers always name their stage; pending approvals may arrive untagged (UC01/UC02/UC04) and are
    # tagged with the running stage by the wrapper -- only entries tagged with another stage are protected.
    foreign_old = [e for e in old if e.get("stage") is not None and e.get("stage") not in scope]
    foreign_new = [e for e in new if e.get("stage") is not None and e.get("stage") not in scope]
    if foreign_old != foreign_new:
        stages = sorted({e["stage"] for e in foreign_old + foreign_new if e not in foreign_old or e not in foreign_new})
        return f"{key} of {', '.join(stages)}"
    return None


def _identity_violation(key: str, old: Any, new: Any, before: OnboardingCaseState) -> SyncViolation | None:
    if key == "employee_id" and old not in (None, _MISSING) and new not in (None, _MISSING) and old != new:
        return SyncViolation("IDENTITY_CONFLICT", key, "the employee number of a case cannot change once known")
    if key == "employee_ref" and isinstance(new, dict):
        if new.get("client_id") not in (None, before.get("client_id")):
            return SyncViolation("TENANT_CONTEXT_CONFLICT", key,
                                 "the canonical employee identity belongs to another tenant")
        uc05_holds_case = bool((before.get("orientation_schedule_result") or {}).get("uc05_case"))
        if isinstance(old, dict) and uc05_holds_case and old.get("employee_id") != new.get("employee_id"):
            return SyncViolation("IDENTITY_CONFLICT", key,
                                 "UC05 already holds this case for another canonical employee id")
    return None


def synchronize(before: OnboardingCaseState, after: OnboardingCaseState, stage: Stage, *, execution_id: str,
                at: str) -> SyncResult:
    """Validate the update one execution of `stage` made (`before` -> `after`) and return the state to
    keep. Rejected updates return `before` unchanged with the violations; accepted ones return `after`,
    with null overwrites of owned fields undone and `field_provenance` updated."""
    scope = _scope(stage)
    merged = copy.deepcopy(after)
    violations: list[SyncViolation] = []
    warnings: list[AgentWarning] = []
    updated: list[str] = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key, _MISSING), after.get(key, _MISSING)
        if old == new or key in BOOKKEEPING:
            continue
        if key in SCOPED_FIELDS:
            foreign = _scoped_violation(key, None if old is _MISSING else old, None if new is _MISSING else new, scope)
            if foreign:
                violations.append(SyncViolation("STATE_OWNERSHIP_VIOLATION", key,
                                                f"{stage.value} changed {foreign}, which it does not own"))
            continue
        if key in TENANT_FIELDS:
            violations.append(SyncViolation("TENANT_CONTEXT_CONFLICT", key, "the tenant of a case cannot change"))
            continue
        owners = FIELD_OWNERS.get(key)
        if owners is None or stage not in owners:
            violations.append(SyncViolation("STATE_OWNERSHIP_VIOLATION", key,
                                            f"{stage.value} changed '{key}', which it does not own"))
            continue
        if new in (None, _MISSING) and old not in (None, _MISSING):
            merged[key] = copy.deepcopy(old)  # never overwrite a known value with nothing
            warnings.append(AgentWarning(code="NULL_OVERWRITE_IGNORED",
                                         detail=f"{stage.value} returned no '{key}'; the previous value is kept"))
            continue
        conflict = _identity_violation(key, old, new, before)
        if conflict is not None:
            violations.append(conflict)
            continue
        if key not in UNRECORDED_FIELDS:
            updated.append(key)
    if violations:
        return SyncResult(state=copy.deepcopy(before), violations=violations, warnings=warnings)
    provenance = merged.setdefault("field_provenance", {})
    for key in updated:
        provenance[key] = {"stage": stage.value, "use_case_id": USE_CASE_OF[stage], "execution_id": execution_id,
                           "at": at}
    return SyncResult(state=merged, warnings=warnings, updated_fields=updated)
