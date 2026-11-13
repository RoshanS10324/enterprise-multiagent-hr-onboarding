"""UC-02 inside the orchestrator: which store it runs on, the environment it is built from, how its failures map
onto stage statuses, the authoritative-case check, the plan projection, and what it records about each
orchestrated execution. The one path into UC-02's agent stays adapters/uc02_tasks.py (`uc02_sub_agent`, which
builds it from `uc02_environment` and rebuilds it after a restart); this module holds no second one.

    plan_tasks / replan_tasks / date change -> uc02_tasks.uc02_sub_agent(deps) -> OnboardingService(store)
                                                                                  store: Supabase | SQLite | memory

Configuration (HR-Onboarding-Common/.env or the process environment, read when UC-02 is first needed)
------------------------------------------------------------------------------------------------------
Every name below may be given scoped, as ``UC02_WORKER_ENV__<NAME>`` (the workers' convention), and the scoped value
wins. In the shared HR-Onboarding-Common/.env use the SCOPED form: that file is also loaded by UC-02's own processes
and tests (``hr_onboarding_common.config.get_settings``), where a plain UC02_STORE_MODE would switch UC-02's
standalone runtime too, and no worker process ever receives a scoped name.

UC02_STORE_MODE                  ``supabase`` | ``memory`` | ``sqlite``. Unset: ``memory`` -- unit tests and local
                                 demos only; nothing survives a restart. ``supabase``: UC-02 reads the UC-01 case and
                                 the client's approved configuration from Supabase and writes its tasks, dependencies,
                                 plan revisions, audit events and agent runs there. It never falls back: missing
                                 credentials, an unreachable database or an incomplete schema is reported by the stage
                                 (RETRY_PENDING / BLOCKED), never replaced by memory or a mock configuration.
UC02_ENV                         ``development`` | ``test`` | ``staging`` | ``production`` -- required in supabase
                                 mode (UC-02 config_safety; never assumed).
UC02_ALLOW_SYNTHETIC_CONFIG      ``1`` only with UC02_ENV=development|test: accept clients whose configuration is
                                 marked mock or synthetic. Refused in staging and production.
UC02_WORKER_ENV__SUPABASE_URL    UC-02's Supabase project and service-role key, scoped to UC-02 with the workers'
UC02_WORKER_ENV__SUPABASE_SERVICE_ROLE_KEY   convention (UC0X_WORKER_ENV__NAME): workers/config.child_environment
                                 removes every ``*_WORKER_ENV__`` name that is not the worker's own, so no worker
                                 process ever receives them, and a bare SUPABASE_URL never switches UC-01 into its
                                 Supabase mode. A plain SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY is used only when the
                                 scoped names are absent. The key stays on the backend: it is never logged, put in an
                                 error, returned by the API or sent to the browser.

Restart: the sub-agent is process memory. Every UC-02 entry point asks uc02_tasks.uc02_sub_agent for it; after a
restart a new one is built over the same store and UC-02 loads the case from it (Supabase or SQLite). In memory
mode a restarted orchestrator has no UC-02 record, and UC-02 says so (the stage blocks) instead of re-planning
silently.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.orchestration.adapters._shared import add_blocker, record_stage_error, with_stage_status
from hr_onboarding_common.orchestration.agent_contract import current_execution
from hr_onboarding_common.orchestration.state import OnboardingCaseState, Stage, StageStatus

log = logging.getLogger(__name__)

SCOPED_PREFIX = "UC02_WORKER_ENV__"
STORE_MODES = ("memory", "sqlite", "supabase")


def uc02_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment UC-02 is built from: the process environment with UC-02's scoped settings
    (``UC02_WORKER_ENV__NAME``) applied as ``NAME``. An empty scoped value never overrides."""
    env = dict(os.environ if environ is None else environ)
    for key, value in list(env.items()):
        name = key[len(SCOPED_PREFIX):]
        if key.startswith(SCOPED_PREFIX) and name and value:
            env[name] = value
    return env


def configured_store_mode(environ: Mapping[str, str] | None = None) -> str:
    """UC02_STORE_MODE as UC-02 will read it (``memory`` when unset)."""
    env = uc02_environment(environ)
    return (env.get("UC02_STORE_MODE") or env.get("PERSISTENCE_BACKEND") or "memory").strip().lower()


def build_service(deps: Any, environ: Mapping[str, str] | None = None):
    """UC-02's OnboardingService over the configured store. Raises (never falls back) when the store cannot be used.
    ``deps.uc02_rest_factory``, when set, supplies UC-02's Supabase REST client (tests inject an in-process
    PostgREST there); production leaves it None and the client is built from the environment."""
    from onboarding_task_management.sub_agent import runtime

    env = uc02_environment(environ)
    factory = getattr(deps, "uc02_rest_factory", None)
    if configured_store_mode(env) == "supabase":
        return runtime.build_runtime_service(env, require_plan_approval=False, rest=factory() if factory else None)
    return runtime.build_service(env, require_plan_approval=False)


def store_of(agent: Any) -> str:
    from onboarding_task_management.sub_agent.orchestration_support import store_mode

    return store_mode(agent.service)


# ------------------------------------------------------------------ failures -> stage status


@dataclass(frozen=True)
class Failure:
    status: StageStatus
    error_code: str  # the stage's error code (the existing contract: MISSING_DATA for configuration/preconditions)
    blocker: str  # the specific UC-02 cause, for the blocker and the dependency catalogue
    owner: str
    action: str
    next_action: str
    outcome: str  # orchestration_support outcome: blocked | failed | retry_pending

    @property
    def retryable(self) -> bool:
        return self.status != StageStatus.FAILED


def _failure(status: StageStatus, error_code: str, blocker: str, owner: str, action: str, next_action: str) -> Failure:
    outcome = {StageStatus.RETRY_PENDING: "retry_pending", StageStatus.FAILED: "failed"}.get(status, "blocked")
    return Failure(status, error_code, blocker, owner, action, next_action, outcome)


def classify(exc: BaseException, stage: Stage = Stage.PLAN_TASKS) -> Failure:
    """How a UC-02 failure is reported. Business refusals are BLOCKED (a person fixes the cause, then the stage is
    resumed); an unreachable database is RETRY_PENDING (technical, counted against the retry budget); a case of
    another tenant is FAILED (an operator reconciles). Nothing here turns a failure into a success."""
    from onboarding_task_management.config_safety import UnsafeConfigurationError
    from onboarding_task_management.errors import (CalendarConfigurationError, CalendarRangeError, DataIsolationError,
                                                   EmployeeNotEligibleError, MissingConfigurationError,
                                                   PreconditionError)
    from onboarding_task_management.persistence import PersistenceError
    from onboarding_task_management.supabase_rest import SupabaseUnavailableError
    from onboarding_task_management.supabase_store import CaseNotFoundError, OwnershipConflictError

    s = stage.value
    if isinstance(exc, SupabaseUnavailableError):
        return _failure(StageStatus.RETRY_PENDING, "UC02_STORE_UNAVAILABLE", "UC02_STORE_UNAVAILABLE", "ops",
                        "Restore the Supabase connection, then retry this stage", f"retry:{s}:uc02_store_unavailable")
    if isinstance(exc, UnsafeConfigurationError):
        return _failure(StageStatus.BLOCKED, "UC02_RUNTIME_MISCONFIGURED", "UC02_RUNTIME_MISCONFIGURED", "ops",
                        "Correct UC-02's runtime settings (UC02_ENV, UC02_ALLOW_SYNTHETIC_CONFIG), restart, then resume",
                        f"ops_review:{s}:uc02_runtime_misconfigured")
    if isinstance(exc, DataIsolationError):
        return _failure(StageStatus.FAILED, "TENANT_CONTEXT_CONFLICT", "TENANT_CONTEXT_CONFLICT", "ops",
                        "Reconcile the case's tenant with UC-01's record before retrying",
                        f"ops_review:{s}:tenant_context_conflict")
    if isinstance(exc, CaseNotFoundError):
        return _failure(StageStatus.BLOCKED, "MISSING_DATA", "UC02_CASE_NOT_FOUND", "ops",
                        "UC-01 must store the onboarding case in Supabase (onboarding_cases); then resume",
                        f"hr_review:{s}:case_not_found")
    if isinstance(exc, OwnershipConflictError):
        return _failure(StageStatus.BLOCKED, "UC02_OWNERSHIP_CONFLICT", "UC02_OWNERSHIP_CONFLICT", "ops",
                        "Another use case holds tasks UC-02 would write; reconcile them, then resume",
                        f"ops_review:{s}:uc02_ownership_conflict")
    if isinstance(exc, (CalendarConfigurationError, CalendarRangeError)):
        return _failure(StageStatus.BLOCKED, "MISSING_DATA", "UC02_CALENDAR_MISSING", "hr_admin",
                        "Configure the client's working calendar (client_configs / client_holidays), then resume",
                        f"hr_review:{s}:calendar_missing")
    if isinstance(exc, MissingConfigurationError):
        return _failure(StageStatus.BLOCKED, "MISSING_DATA", "UC02_CONFIG_MISSING", "hr_admin",
                        "Provide and approve the client's onboarding configuration (an approved, effective "
                        "client_config_versions row with active task templates), then resume",
                        f"hr_review:{s}:configuration_missing")
    if isinstance(exc, EmployeeNotEligibleError):
        return _failure(StageStatus.BLOCKED, "MISSING_DATA", "UC02_EMPLOYEE_NOT_ELIGIBLE", "hr_admin",
                        "Correct the employee's role, grade, location or nationality group, or extend the client's "
                        "configuration, then resume", f"hr_review:{s}:employee_not_eligible")
    if isinstance(exc, PreconditionError):
        return _failure(StageStatus.BLOCKED, "MISSING_DATA", "UC02_PRECONDITION_FAILED", "hr_admin",
                        "Supply the verified offer acceptance and form approval, then resume",
                        f"hr_review:{s}:precondition_failed")
    if isinstance(exc, PersistenceError):
        return _failure(StageStatus.BLOCKED, "UC02_STORE_ERROR", "UC02_STORE_ERROR", "ops",
                        "UC-02's database refused the request (configuration, schema or constraint); fix it, "
                        "then resume", f"ops_review:{s}:uc02_store_error")
    # Anything else (an invalid trigger, a domain refusal): the stage's existing contract.
    return _failure(StageStatus.BLOCKED, "MISSING_DATA", "UC02_INPUT_REJECTED", "hr_admin",
                    "Correct the planning input UC-02 refused, then resume", f"hr_review:{s}:precondition_failed")


def stop(state: OnboardingCaseState, stage: Stage, failure: Failure, detail: str) -> OnboardingCaseState:
    """The stage's state for a UC-02 failure (a new dict): status, error, next action and one blocker."""
    new_state = with_stage_status(state, stage, failure.status)
    record_stage_error(new_state, stage, code=failure.error_code, detail=detail, retryable=failure.retryable)
    new_state["next_action"] = failure.next_action
    add_blocker(new_state, stage=stage, code=failure.blocker, reason=detail[:500], owner=failure.owner,
                next_action=failure.action)
    return new_state


def describe(exc: BaseException) -> str:
    """The failure's own message (UC-02's errors carry ids and codes, never secrets)."""
    return str(exc) or type(exc).__name__


# ------------------------------------------------------------------ the authoritative UC-01 case (Supabase mode)


def verify_case(agent: Any, state: OnboardingCaseState, stage: Stage = Stage.PLAN_TASKS) -> OnboardingCaseState | None:
    """In Supabase mode, before UC-02 plans: the case must be UC-01's ``onboarding_cases`` row of THIS tenant, and its
    employee number (when UC-01 assigned one) must be the workflow's employee. Returns the refusal, or None."""
    if store_of(agent) != "supabase":
        return None
    from onboarding_task_management.supabase_rest import SupabaseGateway

    from hr_onboarding_common.orchestration.supabase_state_store import is_case_uuid

    case_id, client_id = state["case_id"], state["client_id"]
    if not is_case_uuid(case_id):
        return stop(state, stage, classify_case_missing(), f"case {case_id} is not a UC-01 case id; UC-02 plans only "
                                                         f"for a case stored in onboarding_cases")
    rest = agent.service.store.client.rest
    case = SupabaseGateway(rest).get_case(client_id, case_id)
    if case is None:
        other = rest.select("onboarding_cases", {"case_id": case_id}, columns="case_id,client_id")
        if other and other[0].get("client_id") != client_id:
            return stop(state, stage, _failure(StageStatus.FAILED, "TENANT_CONTEXT_CONFLICT", "TENANT_CONTEXT_CONFLICT",
                                               "ops", "Reconcile the case's tenant with UC-01's record before retrying",
                                               f"ops_review:{stage.value}:tenant_context_conflict"),
                        f"case {case_id} belongs to another tenant in onboarding_cases; nothing was planned")
        return stop(state, stage, classify_case_missing(),
                    f"case {case_id} has no onboarding_cases row for client {client_id} (UC-01 has not stored it); "
                    f"UC-02 never creates a case")
    employee_no = str(case.get("employee_no") or "").strip()
    if employee_no and employee_no != str(state.get("employee_id") or ""):
        return stop(state, stage, _failure(StageStatus.BLOCKED, "UC02_CASE_IDENTITY_MISMATCH",
                                           "UC02_CASE_IDENTITY_MISMATCH", "ops",
                                           "Reconcile the workflow's employee with UC-01's case record, then resume",
                                           f"ops_review:{stage.value}:case_identity_mismatch"),
                    f"case {case_id}: onboarding_cases.employee_no is not the workflow's employee; nothing was planned")
    return None


def classify_case_missing() -> Failure:
    return _failure(StageStatus.BLOCKED, "MISSING_DATA", "UC02_CASE_NOT_FOUND", "ops",
                    "UC-01 must store the onboarding case in Supabase (onboarding_cases); then resume",
                    "hr_review:plan_tasks:case_not_found")


# ------------------------------------------------------------------ the execution trace UC-02 keeps


class StageRun:
    """UC-02's own record of one orchestrated execution (audit_events + agent_runs; see UC-02's
    sub_agent/orchestration_support.py). Best effort: a trace that cannot be written is logged and reported as
    ``recorded: False`` -- it never changes the stage's business outcome, which UC-02 already committed or refused."""

    def __init__(self, agent: Any, state: OnboardingCaseState, *, stage: Stage, operation: str):
        from onboarding_task_management.contracts import Actor, Role

        self.agent, self.stage, self.operation = agent, stage, operation
        self.case_id, self.correlation_id = state["case_id"], state.get("correlation_id")
        self.actor = Actor(actor_id=state["actor"]["on_behalf_of"], role=Role.HR_OPS, client_id=state["client_id"])
        execution = current_execution.get()
        self.execution_id = execution.execution_id if execution is not None else None
        self.started_at = now_utc().isoformat()
        self.recorded = True

    def _record(self, fn, **kwargs) -> None:
        try:
            fn(self.agent.service, self.actor, stage=self.stage.value, case_id=self.case_id, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- the trace must not change the business outcome
            self.recorded = False
            log.warning("orchestration.uc02_trace_not_recorded",
                        extra={"case_id": self.case_id, "stage": self.stage.value, "error_type": type(exc).__name__})

    def started(self, *, record_exists: bool) -> None:
        from onboarding_task_management.sub_agent.orchestration_support import record_stage_event

        self._record(record_stage_event, outcome="started", record_exists=record_exists,
                     correlation_id=self.correlation_id, execution_id=self.execution_id)

    def finished(self, outcome: str, *, record_exists: bool, code: str | None = None, **counts: int) -> None:
        from onboarding_task_management.sub_agent.orchestration_support import record_stage_event, record_stage_run

        self._record(record_stage_event, outcome=outcome, record_exists=record_exists,
                     correlation_id=self.correlation_id, execution_id=self.execution_id, code=code, **counts)
        self._record(record_stage_run, run_id=f"UC02-{self.execution_id or now_utc().strftime('%Y%m%dT%H%M%S%f')}",
                     operation=self.operation, outcome=outcome, record_exists=record_exists,
                     started_at=self.started_at, code=code, action_count=1 if outcome in ("completed", "replanned") else 0)

    def finished_from(self, new_state: OnboardingCaseState, *, record_exists: bool) -> None:
        """Record the outcome a stage state carries (COMPLETE -> completed, else its failure)."""
        status = new_state["stage_statuses"].get(self.stage.value)
        error = (new_state.get("stage_errors") or {}).get(self.stage.value) or {}
        blocker = next((b["code"] for b in reversed(new_state.get("blockers") or []) if b.get("stage") == self.stage.value),
                       None)
        outcome = {StageStatus.COMPLETE.value: "completed", StageStatus.RETRY_PENDING.value: "retry_pending",
                   StageStatus.FAILED.value: "failed"}.get(status, "blocked")
        if outcome == "completed" and self.stage == Stage.REPLAN_TASKS:
            outcome = "replanned"
        self.finished(outcome, record_exists=record_exists,
                      code=None if outcome in ("completed", "replanned") else (blocker or error.get("code")))


# ------------------------------------------------------------------ the plan as the orchestrator keeps it


def evaluation_date(state: OnboardingCaseState) -> date:
    """UC-02 never reads a clock: the caller's as_of when given, else today (never the joining date)."""
    role_ctx = state.get("uc02_role_context") or {}
    return date.fromisoformat(role_ctx["as_of"]) if role_ctx.get("as_of") else date.today()


def required_task_ids(agent: Any, actor: Any, onboarding_id: str, as_of: date) -> list[str] | None:
    """The tasks the client's milestones require (e.g. day_one_ready), or None when they cannot be evaluated."""
    try:
        return sorted({t for m in agent.service.milestones(actor, onboarding_id, as_of) for t in m.required_task_ids})
    except Exception as exc:  # noqa: BLE001 -- informational; never blocks a plan UC-02 committed
        log.warning("orchestration.uc02_milestones_unavailable", extra={"error_type": type(exc).__name__})
        return None


def plan_fields(agent: Any, actor: Any, record: Any, as_of: date) -> dict[str, Any]:
    """The persisted projection of UC-02's authoritative record (ids, statuses, dates, counts, the dependency graph).
    Informational: when it cannot be built the plan UC-02 committed still stands, and the projection says so
    (``projection_unavailable``) instead of guessing counts."""
    from onboarding_task_management.sub_agent.orchestration_support import plan_projection

    try:
        required = required_task_ids(agent, actor, record.onboarding_id, as_of)
        fields = plan_projection(record, required_task_ids=required or (), store=store_of(agent))
    except Exception as exc:  # noqa: BLE001
        log.warning("orchestration.uc02_projection_unavailable", extra={"error_type": type(exc).__name__})
        return {"store": store_of(agent), "projection_unavailable": True}
    if required is None:
        fields["required_task_count"], fields["required_task_keys"] = None, []
    return fields


def refreshed_task_plan(state: OnboardingCaseState, checklist: dict[str, Any], projection: dict[str, Any]) -> dict[str, Any]:
    """``task_plan`` after a replan: the identifiers of the first planning kept, the task view and projection of
    UC-02's current record (built by uc02_tasks.refresh_task_plan)."""
    return {**dict(state.get("task_plan") or {}), "tasks": checklist.get("tasks", []),
            "ready": checklist.get("ready", []), "blocked": checklist.get("blocked", []),
            "review_required": bool(checklist.get("review_required")), "issues": checklist.get("issues", []),
            **projection, "refreshed_at": now_utc().isoformat(), "refresh_pending": False}


__all__ = ["SCOPED_PREFIX", "Failure", "StageRun", "build_service", "classify", "configured_store_mode", "describe",
           "evaluation_date", "plan_fields", "refreshed_task_plan", "stop", "store_of", "uc02_environment",
           "verify_case"]
