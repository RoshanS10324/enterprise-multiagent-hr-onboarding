"""UC05 (`employee-onboarding`, Orientation Scheduling) adapter: every UC05 lifecycle capability the
orchestrator drives, through UC05's own public entry points in the UC05 worker process
(module-name collision with UC01/UC04 on `sub_agent`; see workers/uc_worker_runtime.py, whose
`_Uc05Handler` lists the exact UC05 call behind each worker op).

Lifecycle -- the order is UC05's own (its agent.py edges), not invented here:

  orientation_scheduling  offer_accepted event: UC05 intake (case from its employee directory) ->
                          checklist -> induction drafted as a silent tentative hold -> HR approval
                          (gate scheduling_approval, hr_admin). COMPLETE once the induction is
                          CONFIRMED, which only an authorized HR decision does.
  buddy_assignment        UC05 proposes a same-team buddy as soon as the induction is approved
                          (its assign_buddy step, inside that decision's run); HR approves
                          (gate buddy_assignment). COMPLETE once OnboardingRecord.buddy_id is set.
  team_introduction       UC05 drafts it as soon as the buddy is assigned (prepare_team_intro);
                          approved by the team lead ON RECORD (UC05 enforces the specific person).
  buddy_introduction      optional: only on an HR request with a chosen start (UC05's HR-console
                          schedule_buddy_intro, which requires an assigned buddy); approved by the
                          buddy on record. Never blocks readiness.
  joining_day_welcome     on the joining-day event only (UC05's joining_day trigger, keyed by the
                          case's start date), after day-1 readiness signoff; HR approves the draft
                          (gate comms_approval) and UC05 delivers it once through its ledger.

Decisions are made by people, not by this adapter: `relay_uc05_decision` hands an authenticated
approver's decision to UC05's own HITLInbox, where UC05 checks the role, the tenant and (team
intro, buddy intro) the specific person, and UC05's listener applies it and continues its own
workflow. The orchestrator then re-reads UC05's records.

Rules every node follows:
  - Read before write. A node takes a read-only snapshot (`case_snapshot`) and derives the stage
    from UC05's records; it calls a writing UC05 operation only when the snapshot shows the step
    has not happened. Every writing operation UC05 offers is idempotent on its own side too
    (offer events by event id, buddy proposals while pending, welcome drafts per employee,
    bookings and messages through its operation ledger).
  - A stage that is UNCERTAIN (a write whose outcome is unknown: worker crash/timeout, an
    unsettled ledger operation) is never written to again until a fresh snapshot is conclusive.
    If UC05's records for a case it previously had are gone (worker restarted without
    persistence), the stage stays UNCERTAIN -- the event is not resent blindly.
  - UC05 results are validated at this boundary (pydantic models below). A status value UC05
    does not document makes the stage FAILED with UC05_CONTRACT_VIOLATION -- never a guess.
  - The verified actor is threaded through unchanged; an actor whose tenant is not the case's
    is refused before UC05 is called. UC05 enforces tenant isolation again in its own registry.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from hr_onboarding_common.orchestration.adapters._shared import (
    OrchestratorAuthorizationError, add_blocker, add_pending_approval, record_policy_denial, record_stage_error,
    record_uncertain, with_stage_status,
)
from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.orchestration import dependencies
from hr_onboarding_common.orchestration.contracts import Actor as WorkerActor
from hr_onboarding_common.orchestration.contracts import WorkerResponse
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import OnboardingCaseState, Stage, StageStatus, idempotency_key
from hr_onboarding_common.orchestration.tenant_config import EmployeeIdentityError
from hr_onboarding_common.orchestration.workers.uc_worker_client import WorkerCrashedError, WorkerTimeoutError

UC05_AGENT = "employee-onboarding"  # UC05's agent_id: the `agent` of every approval it creates

# UC05 workflow stages (sub_agent.agent.Stage) this adapter interprets. Kept as names, not imports:
# UC05 runs in its own process.
_TERMINAL_RUN_STAGES = {"case_inactive", "event_rejected"}
_SCHEDULING_ERROR_CODES = {
    "CalendarConflictError": "SCHEDULING_CONFLICT", "NoAvailableSlotError": "NO_AVAILABLE_SLOT",
    "InvalidMeetingTimeError": "INVALID_MEETING_TIME", "DuplicateSessionError": "DUPLICATE_SESSION",
    "BuddyNotAssignedError": "BUDDY_NOT_ASSIGNED",
}
# An apply failure retrying cannot fix: the decision itself was not acceptable to UC05.
_NON_RETRYABLE_APPLY_ERRORS = {"UnauthorizedApproverError", "IneligibleBuddyError", "PolicyViolation", "ValueError"}


# ===================================================================== boundary contracts


class Uc05Preflight(BaseModel):
    status: Literal["ok", "incomplete_employee_data", "event_not_found", "malformed_event",
                    "employee_not_in_directory"]
    event_id: str
    employee_id: str | None = None
    joining_date: date | None = None
    missing_fields: list[str] = Field(default_factory=list)
    already_processed: bool = False


class Uc05Session(BaseModel):
    session_id: str
    session_type: Literal["orientation", "induction", "team_intro", "buddy_intro"]
    status: Literal["scheduled", "confirmed", "cancelled"]
    scheduled_start: datetime
    booking_operation_key: str | None = None
    # As UC05 holds them (older workers do not report them; absent stays absent, never inferred):
    scheduled_end: datetime | None = None
    attendees: list[str] = Field(default_factory=list)
    calendar_event_id: str | None = None
    invitations: list[dict[str, Any]] = Field(default_factory=list)  # {participant_id, status}
    responses: list[dict[str, Any]] = Field(default_factory=list)  # {participant_id, response, responded_at}
    meeting_link: str | None = None  # what UC05 holds; None when it holds none (never invented)
    room_location: str | None = None
    # UC05's Supabase session_participants rows {participant_id, role, rsvp_status, responded_at}; None in local mode
    stored_participants: list[dict[str, Any]] | None = None


class Uc05Approval(BaseModel):
    approval_id: str
    gate: str
    proposal_type: str
    status: Literal["pending", "approved", "edited", "rejected"]
    approver_role: str
    subject_id: str | None = None
    created_at: datetime
    decided_at: datetime | None = None
    apply_error: str | None = None
    apply_error_detail: str | None = None
    record_id: str | None = None
    session_type: str | None = None
    buddy_id: str | None = None


class Uc05Draft(BaseModel):
    draft_id: str
    status: Literal["draft", "approved", "rejected", "sent"]
    language: str
    created_at: datetime


class Uc05Operation(BaseModel):
    key: str
    kind: str
    status: Literal["pending", "succeeded", "uncertain", "rejected", "released"]
    last_error: str | None = None
    attempts: int = 0
    resolved_by: str | None = None  # set when an operator resolved an unknown outcome
    resolution_reason: str | None = None
    simulated: bool | None = None  # the delivery ledger's receipt: a simulated notifier accepted it (no real send)


class Uc05Snapshot(BaseModel):
    found: bool
    employee_id: str
    client_id: str | None = None
    case_status: Literal["active", "cancelled", "withdrawn"] | None = None
    start_date: date | None = None
    buddy_id: str | None = None
    team_lead_id: str | None = None  # the team lead on record: the only person who decides a team_lead approval
    # {employee_id: {employee_id, full_name, department, location, is_active, valid, source}} for the assigned and
    # proposed buddy, from the buddy source in use (Supabase buddy_profiles or UC05's candidates).
    buddy_profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)
    stage: str | None = None
    sessions: list[Uc05Session] = Field(default_factory=list)
    approvals: list[Uc05Approval] = Field(default_factory=list)
    superseded_approvals: list[dict[str, Any]] = Field(default_factory=list)
    welcome_drafts: list[Uc05Draft] = Field(default_factory=list)
    operations: list[Uc05Operation] = Field(default_factory=list)

    # ---- derived views (no business rules: selection and ordering only)
    def live_session(self, session_type: str) -> Uc05Session | None:
        live = [s for s in self.sessions if s.session_type == session_type and s.status != "cancelled"]
        return max(live, key=lambda s: s.scheduled_start, default=None)

    def cancelled_sessions(self, session_type: str) -> list[Uc05Session]:
        return [s for s in self.sessions if s.session_type == session_type and s.status == "cancelled"]

    def approvals_for(self, proposal_type: str, *, record_id: str | None = None) -> list[Uc05Approval]:
        items = [a for a in self.approvals if a.proposal_type == proposal_type
                 and (record_id is None or a.record_id == record_id)]
        return sorted(items, key=lambda a: a.created_at, reverse=True)  # newest first

    def unsettled_operations(self, key_prefix: str) -> list[Uc05Operation]:
        return [o for o in self.operations if o.key.startswith(key_prefix) and o.status in ("pending", "uncertain")]

    def operation(self, key: str) -> Uc05Operation | None:
        return next((o for o in self.operations if o.key == key), None)

    def latest_welcome(self) -> Uc05Draft | None:
        return max(self.welcome_drafts, key=lambda d: d.created_at, default=None)


class Uc05RunSummary(BaseModel):
    stage: str | None = None
    meeting_type: str | None = None
    meeting_session_id: str | None = None
    buddy_status: str | None = None
    welcome_status: str | None = None
    conflict_decision: str | None = None  # keep | cancel | replan (an attendance decision's run)
    pending_nodes: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class Uc05DecisionOutcome(BaseModel):
    approval: Uc05Approval
    applied: bool
    apply_error_type: str | None = None
    run: Uc05RunSummary


class Uc05ContractViolation(RuntimeError):
    pass


class Uc05RelayError(RuntimeError):
    """A relayed request UC05 refused or could not answer (API maps `code` to an HTTP status)."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


class PrerequisiteNotMetError(RuntimeError):
    """The requested UC05 operation does not apply to the case's current state."""


# ===================================================================== worker access


def _actor_for(state: OnboardingCaseState, actor: dict[str, Any] | None = None) -> WorkerActor:
    """The verified actor, scoped to the case's tenant. Refuses an actor of another tenant before
    anything reaches UC05 (UC05 refuses it again in its own registry)."""
    actor = actor or state["actor"]
    if actor.get("client_id") not in (None, state["client_id"]):
        raise OrchestratorAuthorizationError(
            f"actor {actor.get('on_behalf_of')} acts for another tenant than case {state['case_id']}")
    return WorkerActor(on_behalf_of=actor["on_behalf_of"], roles=list(actor.get("roles", [])),
                       client_id=state["client_id"])


def resolve_employee_ref(state: OnboardingCaseState, deps: OrchestratorDeps) -> dict[str, Any]:
    """The canonical employee identity for UC05 (tenant_config.TenantConfig.resolve_employee): from
    the caller-supplied employee_id and/or UC01's employee_no, through the tenant's identity scheme.
    Once UC05 holds a case for it, the stored identity is kept as is -- a later configuration change
    can never switch UC05 to a different person mid-lifecycle. Raises EmployeeIdentityError."""
    stored = state.get("employee_ref")
    if stored and stored.get("client_id") == state["client_id"] and (
            (state.get("orientation_schedule_result") or {}).get("uc05_case")
            or stored.get("employee_no") == state.get("employee_id")):
        return stored
    explicit = (state.get("intake_trigger") or {}).get("employee_id")
    return deps.tenants.resolve_employee(client_id=state["client_id"], employee_no=state.get("employee_id"),
                                         employee_id=explicit).model_dump(mode="json")


def _with_employee_ref(state: OnboardingCaseState, deps: OrchestratorDeps, stage: Stage
                       ) -> tuple[OnboardingCaseState | None, OnboardingCaseState | None]:
    """(state carrying employee_ref, None), or (None, the BLOCKED state to return)."""
    try:
        ref = resolve_employee_ref(state, deps)
    except EmployeeIdentityError as exc:
        return None, _set(state, stage, StageStatus.BLOCKED, code=exc.code, detail=exc.detail, retryable=True,
                          next_action=f"hr_review:{stage.value}:{exc.code.lower()}",
                          blocker_action="Correct the employee identifier (UC01's record, the tenant's mapping in "
                                         "config/tenants.yaml, or the supplied employee_id), then resume")
    if state.get("employee_ref") == ref:
        return state, None
    new_state = copy.deepcopy(state)
    new_state["employee_ref"] = ref
    return new_state, None


def _eid(state: OnboardingCaseState) -> str:
    """The canonical (HRMS) employee id -- the only id any UC05 call is made with."""
    return (state.get("employee_ref") or {})["employee_id"]


class _Transport(Exception):
    """The UC05 worker crashed or timed out: for a write, the outcome is unknown."""


def _call(deps: OrchestratorDeps, state: OnboardingCaseState, op: str, payload: dict, *,
          actor: WorkerActor, stage: Stage) -> WorkerResponse:
    worker = deps.worker_pool.get("uc05")
    try:
        return worker.call(op, payload, actor=actor, run_id=state["correlation_id"],
                           idempotency_key=f"{idempotency_key(state, stage)}:{op}")
    except (WorkerCrashedError, WorkerTimeoutError) as exc:
        raise _Transport(str(exc)) from exc


def _validated(model: type[BaseModel], data: Any) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise Uc05ContractViolation(f"UC05 returned a {model.__name__} outside its documented contract: "
                                    f"{exc.errors()[0].get('loc')} {exc.errors()[0].get('msg')}") from None


def _is_case_uuid(case_id: str) -> bool:
    import uuid as _uuid

    try:
        _uuid.UUID(str(case_id))
    except ValueError:
        return False
    return True


def _set(state: OnboardingCaseState, stage: Stage, status: StageStatus, *, code: str | None = None,
         detail: str = "", retryable: bool = True, next_action: str | None = None, owner: str = "hr_admin",
         blocker_action: str | None = None) -> OnboardingCaseState:
    new_state = with_stage_status(state, stage, status)
    if code is not None:
        record_stage_error(new_state, stage, code=code, detail=detail or code, retryable=retryable)
        if status in (StageStatus.BLOCKED, StageStatus.FAILED, StageStatus.UNCERTAIN):
            add_blocker(new_state, stage=stage, code=code, reason=detail or code, owner=owner,
                        next_action=blocker_action or next_action or "")
    if next_action is not None:
        new_state["next_action"] = next_action
    return new_state


def _contract_failure(state: OnboardingCaseState, stage: Stage, exc: Exception) -> OnboardingCaseState:
    return _set(state, stage, StageStatus.FAILED, code="UC05_CONTRACT_VIOLATION", detail=str(exc), retryable=False,
                owner="ops", next_action=f"ops_review:{stage.value}:uc05_contract")


def _worker_failure(state: OnboardingCaseState, stage: Stage, response: WorkerResponse, *,
                    wrote: bool) -> OnboardingCaseState:
    """Map a classified worker failure. A retryable provider error is resumable (BLOCKED): UC05
    left its run checkpointed on the failed step and its ledger records any unsettled call."""
    code, detail = response.error_code or "INTERNAL_ERROR", response.error_detail or ""
    if code == "POLICY_DENIED":
        return record_policy_denial(state, stage, PermissionError(detail))
    if code == "RETRYABLE_PROVIDER_ERROR":
        return _set(state, stage, StageStatus.BLOCKED, code="UC05_PROVIDER_UNAVAILABLE", detail=detail,
                    retryable=True, next_action=f"retry:{stage.value}:provider_unavailable",
                    blocker_action="Resume once the provider recovers; UC05 resumes the failed step only")
    if code in ("INVALID_INPUT", "NOT_FOUND"):
        return _set(state, stage, StageStatus.FAILED, code=f"UC05_{code}", detail=detail, retryable=False,
                    next_action=f"hr_review:{stage.value}:uc05_rejected_request")
    if wrote:
        return record_uncertain(state, stage, f"UC05 failed mid-operation ({detail}); outcome unknown")
    return _set(state, stage, StageStatus.FAILED, code=code, detail=detail, retryable=False, owner="ops",
                next_action=f"ops_review:{stage.value}:uc05_error")


def _snapshot(deps: OrchestratorDeps, state: OnboardingCaseState, actor: WorkerActor, stage: Stage
              ) -> tuple[Uc05Snapshot | None, OnboardingCaseState | None]:
    """(snapshot, None), or (None, the state to return) when it cannot be read."""
    try:
        response = _call(deps, state, "case_snapshot", {"employee_id": _eid(state)}, actor=actor,
                         stage=stage)
    except _Transport as exc:
        if state["stage_statuses"].get(stage.value) == StageStatus.UNCERTAIN.value:
            return None, record_uncertain(state, stage, f"UC05 still unreachable for re-query: {exc}")
        return None, _set(state, stage, StageStatus.BLOCKED, code="UC05_UNAVAILABLE", detail=str(exc),
                          retryable=True, owner="ops", next_action=f"retry:{stage.value}:uc05_unavailable",
                          blocker_action="Restart the UC05 worker, then resume")
    if not response.ok:
        return None, _worker_failure(state, stage, response, wrote=False)
    try:
        return _validated(Uc05Snapshot, response.result), None
    except Uc05ContractViolation as exc:
        return None, _contract_failure(state, stage, exc)


def _interrupted_run(state: OnboardingCaseState, stage: Stage, result: dict | None) -> OnboardingCaseState | None:
    """A UC05 run that stopped part-way (BaseAgent.run turned an error into a response exception and
    the run's checkpoint is left on the failed node) is reported with UC05's own reason -- never
    read as a finished run. Redelivering the same event resumes it at that node (UC05's remedy)."""
    workflow = (result or {}).get("workflow") or {}
    exceptions = ((result or {}).get("response") or {}).get("exceptions") or []
    if not workflow.get("pending_nodes"):
        return None
    reason = exceptions[-1] if exceptions else {"code": "INTERRUPTED", "detail": "the run stopped part-way"}
    return _set(state, stage, StageStatus.BLOCKED, code=f"UC05_{reason.get('code', 'INTERRUPTED')}", retryable=True,
                detail=f"UC05 stopped at {', '.join(workflow['pending_nodes'])}: {reason.get('detail', '')}",
                next_action=f"hr_review:{stage.value}:uc05_run_interrupted",
                blocker_action="Fix the cause UC05 reports (e.g. its client configuration), then resume")


def _lost_records(state: OnboardingCaseState, stage: Stage) -> OnboardingCaseState:
    new_state = record_uncertain(state, stage, "UC05 no longer holds this employee's onboarding records, which it "
                                 "had (or may have had) -- e.g. its worker restarted without persistence. Nothing "
                                 "is resent; reconcile UC05 and the calendar/messaging providers first.")
    # HR's way out, once reconciled: restart UC05's part of this case (operation restart_after_records_lost).
    for blocker in new_state["blockers"]:
        if blocker["stage"] == stage.value and blocker["code"] == "UNCERTAIN_OUTCOME":
            blocker.update(required_from="HR admin", required_data="uc05_records", dependency="UC05 records",
                           resolution="restart_uc05_after_records_lost", retryable=False,
                           required_action="Check that nothing UC05 booked or sent is still live outside this system "
                                           "(calendar holds, invitations, messages), then restart UC05 for this case: "
                                           "it re-creates the case and HR approves the induction again")
    return new_state


def _surface(state: OnboardingCaseState, stage: Stage, approval: Uc05Approval, description: str, *,
             approver_id: str | None = None) -> None:
    add_pending_approval(state, agent=UC05_AGENT, reference=approval.approval_id, role=approval.approver_role,
                         description=description, stage=stage, gate=approval.gate,
                         proposal_type=approval.proposal_type, record_id=approval.record_id, approver_id=approver_id)


def approver_on_record(state: OnboardingCaseState, pending: dict[str, Any]) -> str | None:
    """The one person who may decide this pending UC05 approval, for the inbox and the case view (UC05 itself enforces
    it on the decision): the approval's own `approver_id`, else -- for one raised before that was recorded -- the
    person on the case: the buddy on record, or the team lead UC05 reported. When UC05 read the joiner from this case
    (UC05_OFFER_EVENT_SOURCE=orchestrator_case: the case is the offer event and its contact was kept), UC05's team
    lead on record IS the form's reporting manager, registered by this adapter. None for a role-wide approval."""
    if pending.get("approver_id"):
        return pending["approver_id"]
    if pending.get("agent") != UC05_AGENT:
        return None
    role = pending.get("role")
    if role == "buddy":
        return (state.get("buddy_assignment_result") or {}).get("buddy_id")
    if role != "team_lead":
        return None
    result = state.get("orientation_schedule_result") or {}
    if result.get("team_lead_id"):
        return result["team_lead_id"]
    case_backed = (state.get("joiner_contact") is not None
                   and (result.get("offer_event") or {}).get("event_id") == state.get("case_id"))
    return (state.get("uc02_role_context") or {}).get("manager_id") if case_backed else None


def _case_tz(state: OnboardingCaseState) -> str | None:
    """The timezone the case's sessions are shown in: the one resolved for the case (its own, else its tenant's) and
    kept on the orientation result, else the case's own."""
    return (state.get("orientation_schedule_result") or {}).get("timezone") or state.get("case_timezone")


def _utc(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


_PARTICIPANT_ROLES = (("employee_id", "joiner"), ("team_lead_id", "team_lead"), ("buddy_id", "buddy"))


def _session_fields(state: OnboardingCaseState, snap: Uc05Snapshot, live: Uc05Session) -> dict[str, Any]:
    """A UC05 session for the case record and the UI: what UC05 holds -- type, status, start and end (UTC), the
    timezone it is shown in, the calendar reference, and its participants (id, role from UC05's own record,
    invitation status, newest reply). A participant's name only where an authoritative source holds it (the buddy's
    profile); the joiner's name stays with the identity-gated profile. Nothing absent is filled in."""
    invitations = {i.get("participant_id"): i.get("status") for i in live.invitations}
    replies = {r.get("participant_id"): r for r in live.responses}
    roles = {getattr(snap, attr): role for attr, role in _PARTICIPANT_ROLES if getattr(snap, attr, None)}
    participants = []
    for participant_id in live.attendees:
        role = roles.get(participant_id, "attendee")
        entry: dict[str, Any] = {"participant_id": participant_id, "role": role,
                                 "invitation_status": invitations.get(participant_id),
                                 "rsvp": (replies.get(participant_id) or {}).get("response"),
                                 "responded_at": (replies.get(participant_id) or {}).get("responded_at")}
        profile = snap.buddy_profiles.get(participant_id) if role == "buddy" else None
        if profile and profile.get("valid"):
            entry["name"] = profile.get("full_name")
        participants.append(entry)
    return {"session_id": live.session_id, "session_type": live.session_type, "session_status": live.status,
            "start_utc": _utc(live.scheduled_start), "end_utc": _utc(live.scheduled_end), "timezone": _case_tz(state),
            "meeting_link": live.meeting_link, "location": live.room_location,
            "calendar_event_id": live.calendar_event_id, "participants": participants,
            "stored_participants": live.stored_participants}


def _buddy_fields(snap: Uc05Snapshot, buddy_id: str | None) -> dict[str, Any]:
    """The buddy's profile from the buddy source: name, department, location -- or, for a profile holding placeholder
    or invalid values, a flag and nothing presented as a valid buddy."""
    profile = snap.buddy_profiles.get(buddy_id or "") if buddy_id else None
    if not profile:
        return {"buddy_profile_found": False} if buddy_id else {}
    if not profile.get("valid"):
        return {"buddy_profile_found": True, "buddy_profile_valid": False}
    return {"buddy_profile_found": True, "buddy_profile_valid": True, "buddy_name": profile.get("full_name"),
            "buddy_department": profile.get("department"), "buddy_location": profile.get("location"),
            "buddy_profile_source": profile.get("source")}


def _person_on_record(snap: Uc05Snapshot, approver_role: str | None) -> str | None:
    """Who UC05 lets decide a person-specific approval: the team lead or the buddy on record (UC05's
    validate_decision). None for a role-wide approval (any hr_admin)."""
    return {"team_lead": snap.team_lead_id, "buddy": snap.buddy_id}.get(approver_role or "")


def _apply_failure(state: OnboardingCaseState, stage: Stage, approval: Uc05Approval) -> OnboardingCaseState:
    error = approval.apply_error or "UnknownError"
    retryable = error not in _NON_RETRYABLE_APPLY_ERRORS
    code = {"UnauthorizedApproverError": "UNAUTHORIZED_APPROVER",
            "IneligibleBuddyError": "BUDDY_INELIGIBLE"}.get(error, "DECISION_NOT_APPLIED")
    action = ("retry the decision's follow-up (POST .../approvals/{id}/retry-apply)" if retryable else
              "the decision was recorded but UC05 refused to apply it; HR re-requests this step")
    new_state = _set(state, stage, StageStatus.BLOCKED, code=code, retryable=retryable,
                     detail=f"approval {approval.approval_id}: {approval.apply_error_detail or error}",
                     next_action=f"hr_review:{stage.value}:{code.lower()}", blocker_action=action)
    if retryable:
        # The decision stands; only its follow-up is re-run (UC05 HITLInbox.retry_apply) -- nothing is decided again.
        for blocker in new_state["blockers"]:
            if blocker["stage"] == stage.value and blocker["code"] == code:
                blocker.update(resolution="retry_apply_decision", approval_id=approval.approval_id,
                               required_from="HR admin", retryable=True,
                               required_action="Retry applying the recorded decision once its cause is fixed (e.g. "
                                               "the calendar holds the booking again); nothing is decided again")
    return new_state


def _uncertain_and_inconclusive(state: OnboardingCaseState, stage: Stage, unsettled: list[Uc05Operation]) -> bool:
    return state["stage_statuses"].get(stage.value) == StageStatus.UNCERTAIN.value and bool(unsettled)


def _last_run_for(state: OnboardingCaseState, *, meeting_type: str) -> dict[str, Any] | None:
    runs = [d for d in state.get("approval_decisions", []) if d.get("agent") == UC05_AGENT
            and d.get("run_meeting_type") == meeting_type]
    return runs[-1] if runs else None


# ===================================================================== stage nodes


def call_uc05_orientation(state: OnboardingCaseState, deps: OrchestratorDeps) -> OnboardingCaseState:
    """orientation_scheduling: UC05 intake + induction scheduling (see module docstring)."""
    stage = Stage.ORIENTATION_SCHEDULING
    if not state.get("joining_date"):
        return _set(state, stage, StageStatus.BLOCKED, code="MISSING_DATA",
                    detail="joining_date not confirmed -- cannot schedule orientation",
                    next_action="hr_review:orientation_scheduling:unconfirmed_joining_date")
    state, stop = _with_employee_ref(state, deps, stage)
    if stop is not None:
        return stop
    event_id = (state.get("intake_trigger") or {}).get("event_id")
    source = getattr(deps, "uc05_offer_event_source", "mailbox")
    if source == "orchestrator_case":
        # UC05 reads the joiner from this case (the HR-approved form): the case is the offer-acceptance event. A
        # reference typed on the form is not used (no mailbox is read).
        event_id = state["case_id"]
    case_is_event = source == "case" and _is_case_uuid(state["case_id"])
    if not event_id and case_is_event:
        # UC05's Supabase mode: the authoritative onboarding case (UC01's onboarding_cases row) IS the offer-acceptance
        # event, referenced by its case id (UC05 sub_agent/db/directory.py). UC05 still checks the row exists.
        event_id = state["case_id"]
    if not event_id:
        return _set(state, stage, StageStatus.BLOCKED, code="UC05_OFFER_EVENT_MISSING",
                    detail="no offer-acceptance event reference (offer_acceptance_event_id) for UC05",
                    next_action="hr_review:orientation_scheduling:offer_event_missing")
    try:
        actor = _actor_for(state)
    except OrchestratorAuthorizationError as exc:
        return record_policy_denial(state, stage, exc)

    result = dict(state.get("orientation_schedule_result") or {})
    # The timezone UC05's sessions are shown in: the case's own, else its tenant's (config/tenants.yaml).
    result["timezone"] = deps.tenants.timezone_for(state["client_id"], state.get("case_timezone")).key
    uncertain = state["stage_statuses"].get(stage.value) == StageStatus.UNCERTAIN.value
    just_sent = False

    first_send = not result.get("uc05_case") and not uncertain
    if first_send:
        other = _employee_in_uc05_for_other_case(state, deps)
        if other is not None:
            # UC05 keeps ONE onboarding record per (client, employee id): sending this case's event would attach this
            # joiner to the other case's UC05 record (another person's induction, buddy and welcome).
            return _set(state, stage, StageStatus.BLOCKED, code="EMPLOYEE_ID_NOT_UNIQUE", retryable=True,
                        detail=f"employee id {_eid(state)} is already used in UC05 by another case of this client "
                               f"({other}); UC05 keeps one record per employee, so nothing was sent for this case",
                        next_action="hr_review:orientation_scheduling:employee_id_not_unique")

    if source == "orchestrator_case":
        # After the guard: a case that may not use this employee id never registers it with UC05.
        stop = _register_case(state, deps, actor, stage)
        if stop is not None:
            return stop

    if first_send:
        sent = _send_offer_event(state, deps, actor, event_id, result, preflight=True)
        if isinstance(sent, tuple):  # (new_state, done): a stop before or at the event
            return sent[0]
        result, just_sent = sent, True

    snap, stop = _snapshot(deps, state, actor, stage)
    if stop is not None:
        return stop
    if not snap.found:
        if uncertain or result.get("uc05_case"):
            return _lost_records(state, stage)
        return _set(state, stage, StageStatus.BLOCKED, code="UC05_CASE_NOT_CREATED", retryable=True,
                    detail=f"UC05 processed offer event {event_id} but holds no case for {_eid(state)}",
                    next_action="hr_review:orientation_scheduling:uc05_case_missing")
    if snap.team_lead_id:
        result["team_lead_id"] = snap.team_lead_id
    result.update(uc05_case=True, uc05_employee_id=snap.employee_id, uc05_start_date=snap.start_date.isoformat()
                  if snap.start_date else None, stage=snap.stage)
    unsettled = snap.unsettled_operations(f"calendar:{state['client_id']}:{snap.employee_id}:induction:")

    # Redelivering the same event is UC05's own remedy for a run that ended "redeliverable" (data
    # fixed, calendar freed) or stopped part-way (it resumes from its checkpoint; a completed run is
    # reported as a duplicate). Never while a booking outcome is unsettled, never after HR rejected
    # or a sweep cancelled the induction (that needs an HR reschedule), never twice in one tick.
    if (not just_sent and snap.live_session("induction") is None and not snap.cancelled_sessions("induction")
            and not unsettled):
        sent = _send_offer_event(state, deps, actor, event_id, result, preflight=False)
        if isinstance(sent, tuple):
            return sent[0]
        result = sent
        snap, stop = _snapshot(deps, state, actor, stage)
        if stop is not None:
            return stop
        unsettled = snap.unsettled_operations(f"calendar:{state['client_id']}:{snap.employee_id}:induction:")

    return _derive_orientation(state, snap, result, unsettled)


def _register_case(state: OnboardingCaseState, deps: OrchestratorDeps, actor: WorkerActor,
                   stage: Stage) -> OnboardingCaseState | None:
    """UC05_OFFER_EVENT_SOURCE=orchestrator_case: hand UC05 this case's directory facts, exactly as the HR-approved
    form states them (the email kept for UC05, joiner_contact; department as team, as UC05's own Supabase mapping does;
    manager as team lead). Repeated before every pass, so a restarted UC05 worker knows the case again. Nothing is
    filled in: a missing value stays empty and UC05's intake reports it (INCOMPLETE_EMPLOYEE_DATA)."""
    trigger, role = state.get("intake_trigger") or {}, state.get("uc02_role_context") or {}
    facts = {"case_id": state["case_id"], "client_id": state["client_id"], "employee_id": _eid(state),
             "full_name": trigger.get("candidate_name"), "email": (state.get("joiner_contact") or {}).get("email"),
             "designation": trigger.get("designation") or role.get("role_id"), "department": role.get("department"),
             "team": role.get("department"), "team_lead_id": role.get("manager_id"),
             "joining_date": str(state["joining_date"])[:10]}
    try:
        response = _call(deps, state, "register_case_offer_event", {"case": facts}, actor=actor, stage=stage)
    except _Transport as exc:  # an in-memory registration only: nothing was written
        return _set(state, stage, StageStatus.BLOCKED, code="UC05_UNAVAILABLE", detail=str(exc), owner="ops",
                    next_action="retry:orientation_scheduling:uc05_unavailable")
    if not response.ok:
        return _worker_failure(state, stage, response, wrote=False)
    status = (response.result or {}).get("status")
    if status == "employee_id_conflict":
        return _set(state, stage, StageStatus.BLOCKED, code="EMPLOYEE_ID_NOT_UNIQUE", retryable=True,
                    detail=f"employee id {_eid(state)} is already registered in UC05 for another case of this client "
                           f"({response.result.get('case_id')}); nothing was sent for this case",
                    next_action="hr_review:orientation_scheduling:employee_id_not_unique")
    if status != "registered":
        return _set(state, stage, StageStatus.BLOCKED, code="IDENTITY_MISMATCH", retryable=False,
                    detail=f"UC05 refused this case's registration ({status}); nothing was scheduled",
                    next_action="hr_review:orientation_scheduling:identity_mismatch")
    return None


def _employee_in_uc05_for_other_case(state: OnboardingCaseState, deps: OrchestratorDeps) -> str | None:
    """Another case of this client that has already taken this employee id into UC05 (its offer event was sent): its
    case id, else None. Employee numbers that are not unique (e.g. UC01's in-memory numbering, which restarts with
    each worker process) would otherwise merge two joiners in UC05."""
    store, eid = getattr(deps, "case_states", None), _eid(state)
    if store is None or not eid:
        return None
    for case_id in list(store):
        other = store[case_id]
        if case_id == state["case_id"] or "alias_of" in other or other.get("case_id") == state["case_id"]:
            continue
        if other.get("client_id") != state["client_id"] or (other.get("employee_ref") or {}).get("employee_id") != eid:
            continue
        sent = other.get("orientation_schedule_result") or {}
        if sent.get("uc05_case") or sent.get("offer_event"):
            return other.get("case_id") or case_id
    return None


def _send_offer_event(state, deps, actor, event_id, result, *, preflight: bool):
    """Returns the updated result dict, or (state_to_return, True) when the node must stop."""
    stage = Stage.ORIENTATION_SCHEDULING
    if preflight:
        try:
            response = _call(deps, state, "preflight_offer_event", {"event_id": event_id}, actor=actor, stage=stage)
        except _Transport as exc:  # read-only: nothing was written
            return _set(state, stage, StageStatus.BLOCKED, code="UC05_UNAVAILABLE", detail=str(exc), owner="ops",
                        next_action="retry:orientation_scheduling:uc05_unavailable"), True
        if not response.ok:
            return _worker_failure(state, stage, response, wrote=False), True
        try:
            pre: Uc05Preflight = _validated(Uc05Preflight, response.result)
        except Uc05ContractViolation as exc:
            return _contract_failure(state, stage, exc), True
        stop = _check_preflight(state, pre, source=getattr(deps, "uc05_offer_event_source", "mailbox"))
        if stop is not None:
            return stop, True

    try:
        response = _call(deps, state, "run_event", {"event": {"event": "offer_accepted", "event_id": event_id},
                                                    "instance": event_id}, actor=actor, stage=stage)
    except _Transport as exc:
        return record_uncertain(state, stage, f"UC05 worker unavailable during the offer-accepted event; whether the "
                                f"case and induction hold were created is unknown: {exc}"), True
    if not response.ok:
        if response.error_code == "RETRYABLE_PROVIDER_ERROR":
            result = {**result, "offer_event": {"event_id": event_id, "outcome": "provider_error",
                                                "error": response.error_type}}
            new_state = _worker_failure(state, stage, response, wrote=True)
            new_state["orientation_schedule_result"] = result
            return new_state, True
        return _worker_failure(state, stage, response, wrote=True), True
    workflow = (response.result or {}).get("workflow") or {}
    run_stage = workflow.get("stage")
    errors = workflow.get("errors") or []
    error_type = errors[-1].get("error") if errors else None
    result = {**result, "offer_event": {"event_id": event_id, "outcome": run_stage, "error": error_type}}
    interrupted = _interrupted_run(state, stage, response.result)
    if interrupted is not None:  # the case may exist; the next pass reads UC05 and redelivers
        result["offer_event"]["outcome"] = "interrupted"
        interrupted["orientation_schedule_result"] = {**result, "uc05_case": run_stage not in (None, "received")}
        return interrupted, True
    if run_stage in _TERMINAL_RUN_STAGES:
        new_state = _set(state, stage, StageStatus.FAILED, code=f"UC05_{run_stage.upper()}", retryable=False,
                         detail=workflow.get("rejection_reason") or f"UC05 ended the offer event at '{run_stage}'",
                         next_action="hr_review:orientation_scheduling:uc05_case_not_active")
        new_state["orientation_schedule_result"] = result
        return new_state, True
    if run_stage == "invalid_employee_data":
        new_state = _set(state, stage, StageStatus.BLOCKED, code="INVALID_EMPLOYEE_DATA", retryable=True,
                         detail=f"UC05 intake refused offer event {event_id}: {error_type}",
                         next_action="hr_review:orientation_scheduling:invalid_employee_data",
                         blocker_action="Correct the employee directory record, then resume (UC05 accepts the "
                                        "same event again)")
        new_state["orientation_schedule_result"] = result
        return new_state, True
    return {**result, "uc05_case": True}


def _check_preflight(state: OnboardingCaseState, pre: Uc05Preflight, *,
                     source: str = "mailbox") -> OnboardingCaseState | None:
    stage = Stage.ORIENTATION_SCHEDULING
    not_found = (f"case {pre.event_id} is not in onboarding_cases (UC05's Supabase mode reads the offer acceptance "
                 f"from the case UC01 persisted there)" if source == "case" else
                 f"offer event {pre.event_id} is not in UC05's mailbox. UC05 has no real mailbox integration: its "
                 f"mailbox is simulated and holds only UC05's own synthetic test messages, so a real offer reference "
                 f"cannot be found there")
    stops = {
        "event_not_found": ("UC05_OFFER_EVENT_NOT_FOUND", not_found),
        "malformed_event": ("UC05_OFFER_EVENT_MALFORMED", f"offer event {pre.event_id} has no employee reference"),
        "employee_not_in_directory": ("EMPLOYEE_NOT_IN_UC05_DIRECTORY",
                                      "the event's employee is unknown to UC05's employee directory for this tenant"),
        "incomplete_employee_data": ("INCOMPLETE_EMPLOYEE_DATA",
                                     f"UC05's directory record lacks: {', '.join(pre.missing_fields)}"),
    }
    if pre.status in stops:
        code, detail = stops[pre.status]
        return _set(state, stage, StageStatus.BLOCKED, code=code, detail=detail, retryable=True,
                    next_action=f"hr_review:orientation_scheduling:{code.lower()}",
                    blocker_action="Correct the source record/event reference, then resume")
    if pre.employee_id != _eid(state):
        return _set(state, stage, StageStatus.BLOCKED, code="IDENTITY_MISMATCH", retryable=False,
                    detail=f"offer event {pre.event_id} resolves to UC05 employee {pre.employee_id}, not the case's "
                           f"employee {state['employee_id']}; nothing was scheduled",
                    next_action="hr_review:orientation_scheduling:identity_mismatch",
                    blocker_action="Correct the offer-acceptance event reference or the employee record")
    if pre.joining_date is None or pre.joining_date.isoformat() != str(state["joining_date"])[:10]:
        return _set(state, stage, StageStatus.BLOCKED, code="JOINING_DATE_MISMATCH", retryable=True,
                    detail=f"UC05's directory joining date {pre.joining_date} differs from the case's "
                           f"{state['joining_date']}; nothing was scheduled",
                    next_action="hr_review:orientation_scheduling:joining_date_mismatch",
                    blocker_action="Align the joining date in the HRMS and the onboarding form, then resume")
    return None


def _derive_orientation(state, snap: Uc05Snapshot, result: dict, unsettled: list[Uc05Operation]):
    stage = Stage.ORIENTATION_SCHEDULING
    new_state = copy.deepcopy(state)
    new_state["orientation_schedule_result"] = result
    if snap.case_status != "active":
        return _set(new_state, stage, StageStatus.FAILED, code="UC05_CASE_INACTIVE", retryable=False,
                    detail=f"UC05 case is {snap.case_status}", next_action="hr_review:orientation_scheduling:inactive")
    if (not unsettled and snap.start_date is not None and state.get("joining_date")
            and snap.start_date.isoformat() != str(state["joining_date"])[:10]):
        # UC05 fixed the case's start date at its intake and schedules every session, the joining-day
        # trigger and the welcome from it. It offers no operation that changes it, so a revised joining
        # date cannot be pushed into UC05 from here: a person aligns UC05's record, then resumes.
        return _set(new_state, stage, StageStatus.BLOCKED, code="JOINING_DATE_MISMATCH", retryable=True,
                    detail=f"UC05 holds start date {snap.start_date} for this case; the case's joining date is "
                           f"{state['joining_date']}. UC05 has no operation to change a case's start date, so "
                           f"nothing was rescheduled",
                    next_action="hr_review:orientation_scheduling:joining_date_mismatch",
                    blocker_action="Align UC05's start date for this case with the joining date (UC05 owner), "
                                   "then resume")
    live = snap.live_session("induction")
    if live is not None:
        approval = next(iter(snap.approvals_for("session_booking", record_id=live.session_id)), None)
        result.update(_session_fields(new_state, snap, live))  # session_id, session_status, times, link, location
        result.update(scheduled_start=live.scheduled_start.isoformat(),
                      operation_key=live.booking_operation_key,
                      approval_id=approval.approval_id if approval else None,
                      approval_status=approval.status if approval else None)
        if live.status == "confirmed":
            return with_stage_status(new_state, stage, StageStatus.COMPLETE)
        if approval is not None and approval.status == "pending":
            _surface(new_state, stage, approval, "UC05 induction (orientation) booking -- tentative hold awaiting HR")
            return _set(new_state, stage, StageStatus.AWAITING_APPROVAL,
                        next_action="hr_review:orientation_scheduling:induction_awaiting_approval")
        if approval is not None and approval.apply_error:
            return _apply_failure(new_state, stage, approval)
        return _set(new_state, stage, StageStatus.BLOCKED, code="INDUCTION_WITHOUT_DECIDABLE_APPROVAL",
                    retryable=False, owner="ops",
                    detail=f"induction hold {live.session_id} has no open approval (see UC05 reconciliation)",
                    next_action="ops_review:orientation_scheduling:reconcile")
    if unsettled:
        result["unsettled_operations"] = [o.key for o in unsettled]
        return _set(new_state, stage, StageStatus.UNCERTAIN, code="INDUCTION_BOOKING_UNCERTAIN", retryable=False,
                    owner="ops", detail="a calendar booking for the induction may or may not exist "
                                        f"({', '.join(o.key for o in unsettled)})",
                    next_action="ops_review:orientation_scheduling:uncertain_booking",
                    blocker_action="Check the calendar, then resolve the operation (succeeded | released)")
    cancelled = snap.cancelled_sessions("induction")
    if cancelled:
        latest = next(iter(snap.approvals_for("session_booking", record_id=cancelled[-1].session_id)), None)
        rejected = latest is not None and latest.status == "rejected"
        code = "INDUCTION_REJECTED" if rejected else "INDUCTION_CANCELLED"
        return _set(new_state, stage, StageStatus.BLOCKED, code=code, retryable=True,
                    detail="HR rejected the induction booking; its hold was released" if rejected
                    else "the induction hold was cancelled (approval timed out)",
                    next_action=f"hr_review:orientation_scheduling:{code.lower()}",
                    blocker_action="Reschedule the induction with an HR-chosen start (operation reschedule_session)")
    offer = result.get("offer_event") or {}
    if offer.get("outcome") == "scheduling_failed":
        if offer.get("error") == "BookingOutcomeUncertainError":
            return _set(new_state, stage, StageStatus.UNCERTAIN, code="INDUCTION_BOOKING_UNCERTAIN", retryable=False,
                        owner="ops", detail="UC05 cannot tell whether an earlier induction booking was created",
                        next_action="ops_review:orientation_scheduling:uncertain_booking")
        code = _SCHEDULING_ERROR_CODES.get(offer.get("error") or "", "SCHEDULING_FAILED")
        return _set(new_state, stage, StageStatus.BLOCKED, code=code, retryable=True,
                    detail=f"UC05 could not draft the induction: {offer.get('error')}",
                    next_action=f"hr_review:orientation_scheduling:{code.lower()}",
                    blocker_action="Free the participants' calendars and resume (UC05 accepts the event again), "
                                   "or reschedule with an HR-chosen start")
    return _set(new_state, stage, StageStatus.BLOCKED, code="INDUCTION_NOT_DRAFTED", retryable=True,
                detail="UC05 holds the case but no induction session", owner="ops",
                next_action="retry:orientation_scheduling:induction_not_drafted")


def _prerequisite_block(state: OnboardingCaseState, deps: OrchestratorDeps, stage: Stage) -> OnboardingCaseState | None:
    """The stage BLOCKED (PREREQUISITE_NOT_MET) when a required dependency of it is unmet -- the same
    registry the stage wrapper checks (dependencies.py), re-checked here for a direct adapter call."""
    unmet = dependencies.unmet(stage, state, deps.tenants)
    return dependencies.dependency_block(state, stage, unmet) if unmet else None


def call_uc05_buddy_assignment(state: OnboardingCaseState, deps: OrchestratorDeps) -> OnboardingCaseState:
    stage = Stage.BUDDY_ASSIGNMENT
    blocked = _prerequisite_block(state, deps, stage)  # the induction is confirmed first (UC05's own order)
    if blocked is not None:
        return blocked
    state, stop = _with_employee_ref(state, deps, stage)
    if stop is not None:
        return stop
    try:
        actor = _actor_for(state)
    except OrchestratorAuthorizationError as exc:
        return record_policy_denial(state, stage, exc)
    snap, stop = _snapshot(deps, state, actor, stage)
    if stop is not None:
        return stop
    if not snap.found:
        return _lost_records(state, stage)
    new_state = copy.deepcopy(state)
    result = dict(state.get("buddy_assignment_result") or {})
    new_state["buddy_assignment_result"] = result
    if snap.case_status != "active":
        return _set(new_state, stage, StageStatus.FAILED, code="UC05_CASE_INACTIVE", retryable=False,
                    detail=f"UC05 case is {snap.case_status}", next_action="hr_review:buddy_assignment:inactive")
    items = snap.approvals_for("buddy_assignment")
    if snap.buddy_id:
        approved = next((i for i in items if i.status in ("approved", "edited")), None)
        result.update(buddy_id=snap.buddy_id, approval_id=approved.approval_id if approved else None,
                      status="assigned", **_buddy_fields(snap, snap.buddy_id))
        return with_stage_status(new_state, stage, StageStatus.COMPLETE)
    pending = [i for i in items if i.status == "pending"]
    if pending:
        result.update(proposed_buddy_id=pending[0].buddy_id, approval_id=pending[0].approval_id, status="proposed",
                      **_buddy_fields(snap, pending[0].buddy_id))
        _surface(new_state, stage, pending[0], f"UC05 buddy proposal ({pending[0].buddy_id}) -- HR approves")
        return _set(new_state, stage, StageStatus.AWAITING_APPROVAL,
                    next_action="hr_review:buddy_assignment:buddy_awaiting_approval")
    latest = items[0] if items else None
    if latest is not None and latest.apply_error:
        return _apply_failure(new_state, stage, latest)
    if latest is not None and latest.status == "rejected":
        result.update(status="rejected", approval_id=latest.approval_id)
        return _set(new_state, stage, StageStatus.BLOCKED, code="BUDDY_REJECTED", retryable=True,
                    detail=f"HR rejected the proposed buddy {latest.buddy_id}",
                    next_action="hr_review:buddy_assignment:buddy_rejected",
                    blocker_action="Request a new buddy proposal (operation propose_buddy)")
    # No buddy decision at all (UC05's assign_buddy found no candidate, or the induction was confirmed
    # outside UC05's workflow): ask UC05 to propose -- its own step, idempotent while one is pending.
    return _propose_buddy(new_state, deps, actor)


def _propose_buddy(state: OnboardingCaseState, deps: OrchestratorDeps, actor: WorkerActor) -> OnboardingCaseState:
    stage = Stage.BUDDY_ASSIGNMENT
    try:
        response = _call(deps, state, "propose_buddy", {"employee_id": _eid(state)}, actor=actor, stage=stage)
    except _Transport as exc:
        return record_uncertain(state, stage, f"UC05 worker unavailable while proposing a buddy: {exc}")
    if not response.ok:
        return _worker_failure(state, stage, response, wrote=True)
    out = response.result or {}
    result = state["buddy_assignment_result"] = dict(state.get("buddy_assignment_result") or {})
    if out.get("outcome") == "no_candidates":
        result.update(status="no_candidates")
        if out.get("buddy_source") == "supabase":
            where = ("UC05 reads buddies from the Supabase buddy_profiles table: add an active profile for this "
                     "client (department = the joiner's department for a same-team match, max_active_buddies >= 1)")
        else:
            where = ("UC05's local buddy pool holds only its synthetic test buddies; set UC05_BUDDY_SOURCE=supabase so "
                     "UC05 reads the Supabase buddy_profiles table, and add an active profile for this client there")
        return _set(state, stage, StageStatus.BLOCKED, code="NO_BUDDY_CANDIDATES", retryable=True,
                    detail=f"{out.get('detail') or 'no eligible buddy candidate is configured'}. {where}",
                    next_action="hr_review:buddy_assignment:no_buddy_candidates",
                    blocker_action="Add an active buddy profile for this client, then resume")
    if out.get("outcome") == "already_assigned":
        result.update(buddy_id=out.get("buddy_id"), status="assigned")
        return with_stage_status(state, stage, StageStatus.COMPLETE)
    if out.get("outcome") in ("proposed", "already_pending"):
        result.update(proposed_buddy_id=out.get("buddy_id"), approval_id=out.get("approval_id"), status="proposed")
        add_pending_approval(state, agent=UC05_AGENT, reference=out["approval_id"], role="hr_admin",
                             description=f"UC05 buddy proposal ({out.get('buddy_id')}) -- HR approves",
                             stage=stage, gate="buddy_assignment", proposal_type="buddy_assignment")
        return _set(state, stage, StageStatus.AWAITING_APPROVAL,
                    next_action="hr_review:buddy_assignment:buddy_awaiting_approval")
    return _contract_failure(state, stage, Uc05ContractViolation(f"unknown propose_buddy outcome {out.get('outcome')}"))


def call_uc05_team_introduction(state: OnboardingCaseState, deps: OrchestratorDeps) -> OnboardingCaseState:
    stage = Stage.TEAM_INTRODUCTION
    blocked = _prerequisite_block(state, deps, stage)  # a buddy is assigned first (UC05's own order)
    if blocked is not None:
        return blocked
    state, stop = _with_employee_ref(state, deps, stage)
    if stop is not None:
        return stop
    try:
        actor = _actor_for(state)
    except OrchestratorAuthorizationError as exc:
        return record_policy_denial(state, stage, exc)
    snap, stop = _snapshot(deps, state, actor, stage)
    if stop is not None:
        return stop
    if not snap.found:
        return _lost_records(state, stage)
    new_state = copy.deepcopy(state)
    refresh_buddy_introduction(new_state, snap)
    if snap.case_status != "active":
        return _set(new_state, stage, StageStatus.FAILED, code="UC05_CASE_INACTIVE", retryable=False,
                    detail=f"UC05 case is {snap.case_status}", next_action="hr_review:team_introduction:inactive")
    return _derive_session_stage(new_state, snap, stage, "team_intro", "team_introduction_result",
                                 approver="the team lead on record")


def _derive_session_stage(state, snap: Uc05Snapshot, stage: Stage, session_type: str, result_key: str, *,
                          approver: str) -> OnboardingCaseState:
    """Team introduction / buddy introduction from UC05's sessions, approvals and ledger."""
    result = dict(state.get(result_key) or {})
    state[result_key] = result
    live = snap.live_session(session_type)
    if live is not None:
        approval = next(iter(snap.approvals_for("session_booking", record_id=live.session_id)), None)
        result.update(_session_fields(state, snap, live))  # session_id, session_status, times, link, location
        result.update(scheduled_start=live.scheduled_start.isoformat(), operation_key=live.booking_operation_key,
                      approval_id=approval.approval_id if approval else None,
                      approval_status=approval.status if approval else None,
                      approver_role=approval.approver_role if approval else None)
        if live.status == "confirmed":
            return with_stage_status(state, stage, StageStatus.COMPLETE)
        if approval is not None and approval.status == "pending":
            person = _person_on_record(snap, approval.approver_role)
            _surface(state, stage, approval, f"UC05 {session_type} booking -- decided by {approver}"
                                             f"{f' ({person})' if person else ''}", approver_id=person)
            return _set(state, stage, StageStatus.AWAITING_APPROVAL,
                        next_action=f"approval:{stage.value}:{approval.approver_role}")
        if approval is not None and approval.apply_error:
            return _apply_failure(state, stage, approval)
        return _set(state, stage, StageStatus.BLOCKED, code=f"{session_type.upper()}_WITHOUT_DECIDABLE_APPROVAL",
                    retryable=False, owner="ops", detail=f"{session_type} hold {live.session_id} has no open approval",
                    next_action=f"ops_review:{stage.value}:reconcile")
    unsettled = snap.unsettled_operations(f"calendar:{snap.client_id}:{snap.employee_id}:{session_type}:")
    if unsettled:
        result["unsettled_operations"] = [o.key for o in unsettled]
        return _set(state, stage, StageStatus.UNCERTAIN, code=f"{session_type.upper()}_BOOKING_UNCERTAIN",
                    retryable=False, owner="ops", detail=f"a {session_type} calendar booking may or may not exist",
                    next_action=f"ops_review:{stage.value}:uncertain_booking",
                    blocker_action="Check the calendar, then resolve the operation (succeeded | released)")
    cancelled = snap.cancelled_sessions(session_type)
    if cancelled:
        latest = next(iter(snap.approvals_for("session_booking", record_id=cancelled[-1].session_id)), None)
        code = f"{session_type.upper()}_REJECTED" if latest and latest.status == "rejected" \
            else f"{session_type.upper()}_CANCELLED"
        return _set(state, stage, StageStatus.BLOCKED, code=code, retryable=True,
                    detail=f"the {session_type} booking was {'rejected' if 'REJECTED' in code else 'cancelled'}",
                    next_action=f"hr_review:{stage.value}:{code.lower()}",
                    blocker_action="Schedule it again with an HR-chosen start")
    request = result.get("last_request") or {}
    if request.get("outcome") in ("rejected", "uncertain"):
        code = _SCHEDULING_ERROR_CODES.get(request.get("error_type") or "", "SCHEDULING_FAILED")
        status = StageStatus.UNCERTAIN if request["outcome"] == "uncertain" else StageStatus.BLOCKED
        return _set(state, stage, status, code=code, retryable=status == StageStatus.BLOCKED,
                    detail=f"UC05 refused the requested {session_type}: {request.get('detail', '')}",
                    next_action=f"hr_review:{stage.value}:{code.lower()}")
    if stage == Stage.BUDDY_INTRODUCTION:
        return with_stage_status(state, stage, StageStatus.NOT_REQUESTED)
    run = _last_run_for(state, meeting_type=session_type)
    if run is not None and run.get("run_stage") == "scheduling_failed":
        error = (run.get("run_errors") or [{}])[-1].get("error")
        code = _SCHEDULING_ERROR_CODES.get(error or "", "SCHEDULING_FAILED")
        return _set(state, stage, StageStatus.BLOCKED, code=code, retryable=True,
                    detail=f"UC05 could not draft the {session_type} after approval {run.get('reference')}: {error}",
                    next_action=f"hr_review:{stage.value}:{code.lower()}",
                    blocker_action="Schedule it with an HR-chosen start (operation reschedule_session)")
    return _set(state, stage, StageStatus.BLOCKED, code=f"{session_type.upper()}_NOT_DRAFTED", retryable=True,
                detail=f"no {session_type} has been drafted by UC05 yet",
                next_action=f"hr_review:{stage.value}:not_drafted",
                blocker_action="Schedule it with an HR-chosen start (operation reschedule_session)")


def refresh_buddy_introduction(state: OnboardingCaseState, snap: Uc05Snapshot) -> None:
    """The optional buddy introduction: derived from UC05's records whenever a snapshot is taken.
    Changes only that stage (in place); never blocks anything else."""
    stage = Stage.BUDDY_INTRODUCTION
    state["pending_approvals"] = [p for p in state["pending_approvals"] if p.get("stage") != stage.value]
    state["blockers"] = [b for b in state["blockers"] if b["stage"] != stage.value]
    error_code, error_detail, next_action = state.get("error_code"), state.get("error_detail"), state.get("next_action")
    current = state.get("current_stage")
    derived = _derive_session_stage(state, snap, stage, "buddy_intro", "buddy_introduction_result",
                                    approver="the buddy on record")
    for key in ("stage_statuses", "buddy_introduction_result", "pending_approvals", "blockers", "stage_errors"):
        state[key] = derived[key]
    # Optional stage: the case's own position and last-error fields are not its to change.
    state["error_code"], state["error_detail"], state["next_action"] = error_code, error_detail, next_action
    state["current_stage"] = current
    if derived["stage_statuses"][stage.value] == StageStatus.COMPLETE.value:
        state.get("stage_errors", {}).pop(stage.value, None)


def refresh_uc05_optional_stages(state: OnboardingCaseState, deps: OrchestratorDeps) -> OnboardingCaseState:
    """What UC05 changes outside the stage sequence, re-read after every advance: the optional buddy
    introduction (not a graph node; it never blocks) once requested, and attendance conflicts once an
    RSVP has been forwarded (independent events; their decisions surface as pending approvals). A
    failed read leaves the state as it was."""
    stage = Stage.BUDDY_INTRODUCTION
    intro = state["stage_statuses"].get(stage.value) not in (StageStatus.NOT_REQUESTED.value,
                                                             StageStatus.COMPLETE.value)
    conflicts = bool(state.get("participant_responses")) or any(
        p.get("proposal_type") == "attendee_declined" for p in state.get("pending_approvals", []))
    if not (intro or conflicts) or not (state.get("orientation_schedule_result") or {}).get("uc05_case"):
        return state
    if not state.get("employee_ref"):
        return state
    try:
        snap, stop = _snapshot(deps, state, _actor_for(state), stage)
    except OrchestratorAuthorizationError:
        return state
    if stop is not None or not snap.found:
        return state
    refreshed = copy.deepcopy(state)
    if intro:
        refresh_buddy_introduction(refreshed, snap)
    surface_attendance_conflicts(refreshed, snap)
    return refreshed


def call_uc05_joining_day_welcome(state: OnboardingCaseState, deps: OrchestratorDeps) -> OnboardingCaseState:
    stage = Stage.JOINING_DAY_WELCOME
    blocked = _prerequisite_block(state, deps, stage)  # HR's day-1 readiness signoff first
    if blocked is not None:
        return blocked
    state, stop = _with_employee_ref(state, deps, stage)
    if stop is not None:
        return stop
    event = state.get("joining_day_event")
    if not event:
        new_state = _set(state, stage, StageStatus.WAITING,
                         next_action=f"await_joining_day:{state.get('joining_date')}")
        new_state["welcome_message_result"] = {**(state.get("welcome_message_result") or {}),
                                               "scheduled_for": state.get("joining_date")}
        return new_state
    try:
        actor = _actor_for(state)
    except OrchestratorAuthorizationError as exc:
        return record_policy_denial(state, stage, exc)
    snap, stop = _snapshot(deps, state, actor, stage)
    if stop is not None:
        return stop
    if not snap.found:
        return _lost_records(state, stage)
    new_state = copy.deepcopy(state)
    result = dict(state.get("welcome_message_result") or {})
    new_state["welcome_message_result"] = result
    if snap.case_status != "active":
        return _set(new_state, stage, StageStatus.FAILED, code="UC05_CASE_INACTIVE", retryable=False,
                    detail=f"UC05 case is {snap.case_status}; no welcome is drafted",
                    next_action="hr_review:joining_day_welcome:inactive")
    welcome_ops = snap.unsettled_operations(f"welcome:{snap.client_id}:")
    if snap.latest_welcome() is None:
        if _uncertain_and_inconclusive(new_state, stage, welcome_ops):
            return record_uncertain(new_state, stage, "a welcome delivery is still unsettled")
        drafted = _run_joining_day(new_state, deps, actor, event, result)
        if drafted is not None:
            return drafted
        snap, stop = _snapshot(deps, new_state, actor, stage)
        if stop is not None:
            return stop
    return _derive_welcome(new_state, snap, result)


def _run_joining_day(state, deps, actor, event, result) -> OnboardingCaseState | None:
    """UC05's joining-day trigger for the case's start date. It drafts for every ACTIVE case of the
    tenant starting that day (UC05's contract; the same run its scheduler makes) and skips anyone
    already drafted. Returns a state to stop with, or None to re-read."""
    stage = Stage.JOINING_DAY_WELCOME
    as_of = event["as_of"]
    try:
        response = _call(deps, state, "run_event", {"event": {"event": "joining_day", "as_of": as_of},
                                                    "instance": f"joining-day:{as_of}"}, actor=actor, stage=stage)
    except _Transport as exc:
        return record_uncertain(state, stage, f"UC05 worker unavailable during the joining-day run: {exc}")
    if not response.ok:
        return _worker_failure(state, stage, response, wrote=True)
    interrupted = _interrupted_run(state, stage, response.result)
    if interrupted is not None:
        result["joining_day_run"] = {"as_of": as_of, "outcome": "interrupted"}
        interrupted["welcome_message_result"] = result
        return interrupted
    workflow = (response.result or {}).get("workflow") or {}
    if workflow.get("stage") == "welcome_not_configured":
        # UC05 could not draft for this tenant (its ClientConfig). Typed by UC05's error, never by
        # parsing its message; the run is complete, so a resume after HR configures it runs again.
        error = ((workflow.get("errors") or [{}])[-1]).get("error")
        code = ("UC05_CLIENT_CONFIG_INVALID" if error in ("InvalidClientConfigError", "UnknownTemplateError")
                else "UC05_CLIENT_CONFIG_MISSING")
        result["joining_day_run"] = {"as_of": as_of, "outcome": "not_configured"}
        new_state = _set(state, stage, StageStatus.BLOCKED, code=code, retryable=True, detail=_config_detail(workflow),
                         next_action=f"hr_review:joining_day_welcome:{code.lower()}",
                         blocker_action="Set up the tenant's welcome configuration in UC05 (languages, branding, "
                                        "a valid 'welcome' template), then resume")
        new_state["welcome_message_result"] = result
        return new_state
    actions = (response.result or {}).get("response", {}).get("actions_taken", [])
    results = next((a.get("detail", {}).get("results", []) for a in actions if a.get("tool") == "joining_trigger.run"),
                   [])
    mine = next((r for r in results if r.get("employee_id") == _eid(state)), None)
    result["joining_day_run"] = {"as_of": as_of, "outcome": mine.get("status") if mine else "not_matched"}
    if mine is None:
        return _set(state, stage, StageStatus.BLOCKED, code="UC05_START_DATE_MISMATCH", retryable=True,
                    detail=f"UC05's joining-day run for {as_of} did not include this employee (its start date differs)",
                    next_action="hr_review:joining_day_welcome:start_date_mismatch")
    if mine.get("status") == "skipped_inactive":
        return _set(state, stage, StageStatus.FAILED, code="UC05_CASE_INACTIVE", retryable=False,
                    detail="UC05 skipped the welcome: the case is not active",
                    next_action="hr_review:joining_day_welcome:inactive")
    return None


def _derive_welcome(state, snap: Uc05Snapshot, result: dict) -> OnboardingCaseState:
    stage = Stage.JOINING_DAY_WELCOME
    draft = snap.latest_welcome()
    if draft is None:
        return _set(state, stage, StageStatus.BLOCKED, code="WELCOME_NOT_DRAFTED", retryable=True,
                    detail="UC05 drafted no welcome message", next_action="retry:joining_day_welcome")
    approval = next(iter(snap.approvals_for("welcome_message", record_id=draft.draft_id)), None)
    operation = snap.operation(f"welcome:{snap.client_id}:{draft.draft_id}")
    result.update(draft_id=draft.draft_id, draft_status=draft.status, language=draft.language,
                  approval_id=approval.approval_id if approval else None,
                  approval_status=approval.status if approval else None,
                  operation_key=operation.key if operation else None,
                  operation_status=operation.status if operation else None)
    result["delivery"] = _delivery_state(draft, operation)
    result["delivery_status"] = delivery_status(result["delivery"], operation)
    result["simulated_delivery"] = bool(operation is not None and operation.simulated)
    if operation is not None and operation.resolved_by:
        result["delivery_resolution"] = {"by": operation.resolved_by, "reason": operation.resolution_reason}
    if draft.status == "sent":
        return with_stage_status(state, stage, StageStatus.COMPLETE)
    if draft.status == "draft":
        if approval is not None and approval.status == "pending":
            _surface(state, stage, approval, "UC05 joining-day welcome message -- HR approves before it is sent")
        return _set(state, stage, StageStatus.AWAITING_APPROVAL,
                    next_action="hr_review:joining_day_welcome:welcome_awaiting_approval")
    if draft.status == "rejected":
        return _set(state, stage, StageStatus.BLOCKED, code="WELCOME_REJECTED", retryable=True,
                    detail="HR rejected the welcome draft; it is never sent",
                    next_action="hr_review:joining_day_welcome:welcome_rejected",
                    blocker_action="Redraft the welcome (operation redraft_welcome) if one should still go out")
    # approved but not marked sent: the delivery ledger is the source of truth
    if operation is not None and operation.status == "succeeded":
        return with_stage_status(state, stage, StageStatus.COMPLETE)
    if operation is not None and operation.status in ("pending", "uncertain"):
        retry = (f"retry-apply approval {approval.approval_id} (UC05 resends under the same idempotency key and "
                 f"refuses if the provider cannot deduplicate), or " if approval is not None and approval.apply_error
                 else "")
        return _set(state, stage, StageStatus.UNCERTAIN, code="WELCOME_DELIVERY_UNCERTAIN", retryable=False,
                    owner="hr_admin", detail="the welcome message may already have been delivered; it is not resent",
                    next_action="hr_review:joining_day_welcome:delivery_uncertain",
                    blocker_action=f"{retry}check with the recipient, then operation resolve_welcome_delivery "
                                   f"(delivered | not_delivered | keep_unresolved, with the evidence as reason)")
    retry_action = "When the cause is fixed, an operator authorizes a new attempt (operation retry_welcome_delivery)"
    if operation is not None and operation.status == "rejected":
        return _set(state, stage, StageStatus.BLOCKED, code="WELCOME_DELIVERY_REJECTED", retryable=False,
                    detail=f"the provider refused the welcome permanently ({operation.last_error}); nothing was "
                           f"delivered", next_action="hr_review:joining_day_welcome:delivery_rejected",
                    blocker_action=retry_action)
    if operation is not None and operation.status == "released":
        return _set(state, stage, StageStatus.BLOCKED, code="WELCOME_NOT_DELIVERED", retryable=False,
                    detail="an operator confirmed the welcome was not delivered",
                    next_action="hr_review:joining_day_welcome:not_delivered", blocker_action=retry_action)
    if approval is not None and approval.apply_error:
        return _apply_failure(state, stage, approval)
    return _set(state, stage, StageStatus.BLOCKED, code="WELCOME_DELIVERY_FAILED", retryable=False,
                detail="the approved welcome was not delivered (see UC05's audit log)",
                next_action="hr_review:joining_day_welcome:delivery_failed", blocker_action=retry_action)


def delivery_status(delivery: str, operation: Uc05Operation | None) -> str:
    """The welcome's delivery status for the record: SENT_SIMULATED when the provider that accepted it is a simulation
    (UC05's MockNotifier receipt says so), SENT for a real provider's receipt, CONFIRMED_BY_OPERATOR, else the
    delivery state in upper case. A simulated send is never reported as a real one."""
    if delivery == "delivered":
        return "SENT_SIMULATED" if operation is not None and operation.simulated else "SENT"
    return delivery.upper()


def _delivery_state(draft: Uc05Draft, operation: Uc05Operation | None) -> str:
    """Where the welcome's delivery stands, from UC05's draft and its delivery ledger only -- never a
    receipt this system has not seen:
      not_attempted          no delivery operation yet (still a draft, rejected, or approved but unsent)
      delivered              the provider accepted it (draft SENT, or the ledger's own receipt)
      confirmed_by_operator  an operator confirmed delivery from outside evidence (no provider receipt)
      failed_before_delivery the provider refused it permanently; nothing was delivered
      uncertain              the outcome is unknown (pending/uncertain); never resent automatically
      not_delivered          an operator confirmed it was not delivered; a new attempt may be authorized"""
    if draft.status == "sent":
        return "delivered"
    if operation is None:
        return "not_attempted"
    if operation.status == "succeeded":
        return "confirmed_by_operator" if operation.resolved_by else "delivered"
    return {"pending": "uncertain", "uncertain": "uncertain", "rejected": "failed_before_delivery",
            "released": "not_delivered"}[operation.status]


# ===================================================================== relays (human decisions, HR operations)

# Which orchestrator stage a UC05 session type belongs to.
_SESSION_STAGE = {"induction": Stage.ORIENTATION_SCHEDULING, "orientation": Stage.ORIENTATION_SCHEDULING,
                  "team_intro": Stage.TEAM_INTRODUCTION, "buddy_intro": Stage.BUDDY_INTRODUCTION}


def _require_employee_ref(state: OnboardingCaseState, deps: OrchestratorDeps) -> OnboardingCaseState:
    """Relays act for an identified employee only (PrerequisiteNotMetError otherwise)."""
    try:
        ref = resolve_employee_ref(state, deps)
    except EmployeeIdentityError as exc:
        raise PrerequisiteNotMetError(f"{exc.code}: {exc.detail}") from None
    if state.get("employee_ref") == ref:
        return state
    new_state = copy.deepcopy(state)
    new_state["employee_ref"] = ref
    return new_state


def _approval_entry(state: OnboardingCaseState, approval_id: str) -> dict[str, Any]:
    """This case's record of one UC05 approval: its open entry, else the last decision relayed on it.
    KeyError for an approval that is not this case's."""
    for p in state.get("pending_approvals", []):
        if p["agent"] == UC05_AGENT and p["reference"] == approval_id and p.get("stage"):
            return p
    for d in reversed(state.get("approval_decisions", [])):
        if d.get("agent") == UC05_AGENT and d.get("reference") == approval_id:
            return d
    raise KeyError(approval_id)


def _stage_of_approval(state: OnboardingCaseState, approval_id: str) -> Stage:
    return Stage(_approval_entry(state, approval_id)["stage"])


def _relay_result(state: OnboardingCaseState, stage: Stage, response: WorkerResponse, *, approval_id: str,
                  decision: str, approver: str, kind: str) -> OnboardingCaseState:
    if not response.ok:
        raise Uc05RelayError(response.error_code or "INTERNAL_ERROR", response.error_detail or "")
    try:
        outcome: Uc05DecisionOutcome = _validated(Uc05DecisionOutcome, response.result)
    except Uc05ContractViolation as exc:
        raise Uc05RelayError("UC05_CONTRACT_VIOLATION", str(exc)) from None
    new_state = copy.deepcopy(state)
    new_state.setdefault("approval_decisions", []).append({
        "agent": UC05_AGENT, "reference": approval_id, "stage": stage.value, "kind": kind,
        "proposal_type": outcome.approval.proposal_type, "record_id": outcome.approval.record_id,
        "decision": decision if kind == "decision" else outcome.approval.status, "approver": approver,
        "decided_at": outcome.approval.decided_at.isoformat() if outcome.approval.decided_at else None,
        "applied": outcome.applied, "apply_error_type": outcome.apply_error_type, "run_stage": outcome.run.stage,
        "run_meeting_type": outcome.run.meeting_type, "conflict_decision": outcome.run.conflict_decision,
        "run_errors": [{"error": e.get("error"), "detail": str(e.get("detail", ""))[:200]} for e in outcome.run.errors],
    })
    # An attendance conflict HR resolved by cancelling or replanning a session changes a stage that
    # may already be COMPLETE: it is re-opened so it re-derives from UC05's records (a cancelled
    # induction blocks; a replanned one awaits its own approval). Nothing is re-executed.
    if (outcome.approval.proposal_type == "attendee_declined" and outcome.applied
            and outcome.run.conflict_decision in ("cancel", "replan")
            and new_state["stage_statuses"].get(stage.value) == StageStatus.COMPLETE.value):
        new_state["stage_statuses"][stage.value] = StageStatus.IN_PROGRESS.value
        new_state.setdefault("stage_history", []).append({
            "stage": stage.value, "status": StageStatus.IN_PROGRESS.value, "at": now_utc().isoformat(),
            "code": f"REOPENED_BY_ATTENDANCE_DECISION_{outcome.run.conflict_decision.upper()}"})
    return new_state


def relay_uc05_decision(state: OnboardingCaseState, deps: OrchestratorDeps, *, approval_id: str, decision: str,
                        approver: dict[str, Any], reason_code: str | None = None, note: str | None = None,
                        edited_payload: dict[str, Any] | None = None) -> OnboardingCaseState:
    """Hand an authenticated person's decision on one of THIS case's UC05 approvals to UC05. The
    worker first runs UC05's validate_decision (the specific team lead/buddy, applicability, edited
    payload) and checks the approval is the activity this case knows it as (proposal type, record);
    only then does UC05's HITLInbox record it (role, tenant), and UC05's listener applies it. A
    refused decision is raised (Uc05RelayError) and leaves the approval PENDING and usable. KeyError
    for an approval that is not this case's."""
    state = _require_employee_ref(state, deps)
    entry = _approval_entry(state, approval_id)
    stage = Stage(entry["stage"])
    actor = _actor_for(state, approver)
    try:
        response = _call(deps, state, "decide_approval",
                         {"approval_id": approval_id, "decision": decision, "reason_code": reason_code, "note": note,
                          "edited_payload": edited_payload, "expected_subject_id": _eid(state),
                          "expected_proposal_type": entry.get("proposal_type"),
                          "expected_record_id": entry.get("record_id")},
                         actor=actor, stage=stage)
    except _Transport as exc:
        return record_uncertain(state, stage, f"UC05 worker unavailable while deciding {approval_id}; whether the "
                                f"decision was recorded and applied is unknown: {exc}")
    return _relay_result(state, stage, response, approval_id=approval_id, decision=decision,
                         approver=actor.on_behalf_of, kind="decision")


def relay_uc05_retry(state: OnboardingCaseState, deps: OrchestratorDeps, *, approval_id: str,
                     actor: dict[str, Any]) -> OnboardingCaseState:
    """UC05's recovery path for a decision whose follow-up failed (HITLInbox.retry_apply)."""
    state = _require_employee_ref(state, deps)
    stage = _stage_of_approval(state, approval_id)
    worker_actor = _actor_for(state, actor)
    try:
        response = _call(deps, state, "retry_apply", {"approval_id": approval_id,
                                                      "expected_subject_id": _eid(state)},
                         actor=worker_actor, stage=stage)
    except _Transport as exc:
        return record_uncertain(state, stage, f"UC05 worker unavailable while retrying {approval_id}: {exc}")
    return _relay_result(state, stage, response, approval_id=approval_id, decision="retry_apply",
                         approver=worker_actor.on_behalf_of, kind="retry_apply")


RSVP_RESPONSES = ("accepted", "tentative", "declined", "no_response")


def relay_uc05_participant_response(state: OnboardingCaseState, deps: OrchestratorDeps, *, session_id: str,
                                    participant_id: str, response: str, responded_at: str | None,
                                    actor: dict[str, Any]) -> tuple[OnboardingCaseState, dict[str, Any]]:
    """An RSVP: an independent UC05 event (UC05's participant_responded), not a lifecycle stage --
    participants answer whenever they do, and UC05 keeps the newest answer per participant. The
    session must be one of THIS case's (checked against UC05's records first: another case's or
    another tenant's session is "not found"). UC05 applies its own rules: only the participant
    (or hr_admin) may answer, only invited participants of a live session, an older answer never
    overrides a newer one, and a decline opens one HR attendance decision (keep / cancel / replan),
    surfaced on the case and decided through relay_uc05_decision. Re-sending the same RSVP is safe."""
    state = _require_employee_ref(state, deps)
    if not (state.get("orientation_schedule_result") or {}).get("uc05_case"):
        raise PrerequisiteNotMetError("UC05 holds no onboarding case for this employee yet")
    if response not in RSVP_RESPONSES:
        raise PrerequisiteNotMetError(f"response must be one of {', '.join(RSVP_RESPONSES)}")
    worker_actor = _actor_for(state, actor)
    snap, stop = _snapshot(deps, state, worker_actor, Stage.ORIENTATION_SCHEDULING)
    if stop is not None or snap is None:
        raise Uc05RelayError("UC05_UNAVAILABLE", "UC05 could not be read; the RSVP was not recorded")
    session = next((s for s in snap.sessions if s.session_id == session_id), None)
    if not snap.found or session is None:
        raise Uc05RelayError("NOT_FOUND", "session not found for this case")
    event = {"event": "participant_responded", "employee_id": _eid(state), "session_id": session_id,
             "participant_id": participant_id, "response": response}
    if responded_at:
        event["responded_at"] = responded_at
    try:
        reply = _call(deps, state, "run_event", {"event": event, "instance": _eid(state)}, actor=worker_actor,
                      stage=_SESSION_STAGE.get(session.session_type, Stage.ORIENTATION_SCHEDULING))
    except _Transport:
        raise Uc05RelayError("UC05_UNAVAILABLE", "the RSVP may not have reached UC05; re-sending it is safe (UC05 "
                                                 "keeps the newest answer per participant)") from None
    if not reply.ok:
        raise Uc05RelayError(reply.error_code or "INTERNAL_ERROR", reply.error_detail or "")
    workflow = (reply.result or {}).get("workflow") or {}
    exceptions = ((reply.result or {}).get("response") or {}).get("exceptions") or []
    outcome = workflow.get("stage")
    if outcome == "event_rejected" or exceptions:
        raise Uc05RelayError("INVALID_INPUT", (exceptions[-1].get("detail") if exceptions else None)
                             or "UC05 rejected the RSVP")
    if outcome == "response_superseded":
        raise Uc05RelayError("CONFLICT", "a newer answer from this participant is already recorded")
    new_state = copy.deepcopy(state)
    new_state.setdefault("participant_responses", []).append({
        "session_id": session_id, "session_type": session.session_type, "participant_id": participant_id,
        "response": response, "outcome": outcome, "recorded_by": worker_actor.on_behalf_of,
        "at": now_utc().isoformat()})
    return new_state, {"outcome": outcome, "session_id": session_id, "session_type": session.session_type}


def surface_attendance_conflicts(state: OnboardingCaseState, snap: Uc05Snapshot) -> None:
    """Undecided UC05 attendance decisions (a participant declined) become pending approvals of the
    session's stage, decided through relay_uc05_decision like any UC05 approval (in place)."""
    open_items = {a.approval_id: a for a in snap.approvals_for("attendee_declined") if a.status == "pending"}
    superseded = {s.get("id") for s in snap.superseded_approvals}
    state["pending_approvals"] = [p for p in state["pending_approvals"]
                                  if p.get("proposal_type") != "attendee_declined" or p["reference"] in open_items]
    for approval in open_items.values():
        if approval.approval_id in superseded:
            continue  # the decline was withdrawn: UC05 already told HR no decision is needed
        add_pending_approval(state, agent=UC05_AGENT, reference=approval.approval_id, role=approval.approver_role,
                             description=f"UC05 attendance conflict on the {approval.session_type} -- HR decides "
                                         f"keep (approve), cancel (reject) or replan (edit with new_start)",
                             stage=_SESSION_STAGE.get(approval.session_type or "", Stage.ORIENTATION_SCHEDULING),
                             gate=approval.gate, proposal_type=approval.proposal_type, record_id=approval.record_id)


UC05_OPERATIONS = ("reschedule_session", "schedule_buddy_intro", "propose_buddy", "redraft_welcome",
                   "resolve_operation", "resolve_welcome_delivery", "retry_welcome_delivery",
                   "restart_after_records_lost")
_ORIENTATION_RESULTS = ("orientation_schedule_result", "buddy_assignment_result", "team_introduction_result",
                        "buddy_introduction_result")
WELCOME_RESOLUTIONS = {"delivered": "succeeded", "not_delivered": "released", "keep_unresolved": None}


def _restart_after_records_lost(state: OnboardingCaseState, deps: OrchestratorDeps, worker_actor: WorkerActor,
                                reason: str) -> tuple[OnboardingCaseState, dict]:
    """HR restarts UC05's part of this case after UC05 lost its records (e.g. a worker restart without
    persistence). Only when a UC05 stage is UNCERTAIN, UC05 itself confirms it holds nothing for this employee, and HR
    states what was reconciled. The orientation branch goes back to pending, so UC05 re-creates the case from the
    approved form and every UC05 approval (the induction first) is asked for again: nothing from the lost records is
    replayed or assumed approved. Joining-day welcome is not touched (it runs only after day-1 sign-off)."""
    statuses = state["stage_statuses"]
    uncertain = [s for s in (Stage.ORIENTATION_SCHEDULING, Stage.BUDDY_ASSIGNMENT, Stage.TEAM_INTRODUCTION,
                             Stage.BUDDY_INTRODUCTION) if statuses.get(s.value) == StageStatus.UNCERTAIN.value]
    if not uncertain:
        raise PrerequisiteNotMetError("no UC05 stage of this case is uncertain; nothing to restart")
    if not reason:
        raise PrerequisiteNotMetError("a reason is required: what was checked (calendar holds, invitations, "
                                      "messages) before UC05 is restarted for this case")
    snap, stop = _snapshot(deps, state, worker_actor, uncertain[0])
    if stop is not None:
        raise PrerequisiteNotMetError("UC05 could not be asked whether it still holds this employee's records; "
                                      "nothing was changed")
    if snap.found:
        raise PrerequisiteNotMetError("UC05 still holds this employee's onboarding records: nothing was lost, so "
                                      "nothing is restarted; resume the case instead")
    from hr_onboarding_common.orchestration.state import BRANCH_STAGES, Branch, _initial_statuses
    from hr_onboarding_common.orchestration.workflow import refresh_workflow_view

    stages = {s.value for s in BRANCH_STAGES[Branch.ORIENTATION]}
    initial = _initial_statuses()
    new_state = copy.deepcopy(state)
    now = datetime.now(timezone.utc).isoformat()
    for stage in stages:
        # A completed stage is re-opened (it leaves COMPLETE only that way, agent_contract.LEAVE_COMPLETE); any
        # other goes back to where a new case starts. Either way the next run derives it from UC05 afresh.
        before = new_state["stage_statuses"].get(stage)
        after = StageStatus.IN_PROGRESS.value if before == StageStatus.COMPLETE.value else initial[stage]
        if before != after:
            new_state["stage_statuses"][stage] = after
            new_state["stage_history"].append({"stage": stage, "status": after, "at": now,
                                               "code": "UC05_RESTARTED_AFTER_RECORDS_LOST"})
    for key in _ORIENTATION_RESULTS:
        new_state[key] = None
    new_state["blockers"] = [b for b in new_state["blockers"] if b["stage"] not in stages]
    new_state["pending_approvals"] = [p for p in new_state["pending_approvals"]
                                      if p.get("stage") not in stages and p.get("agent") != UC05_AGENT]
    for bucket in ("stage_errors", "retry_metadata"):
        for stage in stages:
            (new_state.get(bucket) or {}).pop(stage, None)
    new_state["uc05_restarts"] = [*(state.get("uc05_restarts") or []),
                                  {"at": now, "by": worker_actor.on_behalf_of, "reason": reason,
                                   "uncertain_stages": [s.value for s in uncertain]}]
    refresh_workflow_view(new_state)
    return new_state, {"operation": "restart_after_records_lost", "outcome": "restarted",
                       "reset_stages": sorted(stages)}


def refresh_uc05_details(state: OnboardingCaseState, deps: OrchestratorDeps, *,
                         actor: dict[str, Any]) -> tuple[OnboardingCaseState, dict[str, Any]]:
    """Re-read UC05 for this case and update ONLY the display details of its UC05 stage results -- session times,
    timezone, calendar reference, participants, the buddy's profile -- for results recorded before UC05 reported them.
    Statuses, approvals, decisions and history are not touched, nothing is sent to UC05, and a value UC05 does not
    hold stays absent."""
    if not (state.get("employee_ref") or {}).get("employee_id") or \
            not (state.get("orientation_schedule_result") or {}).get("uc05_case"):
        raise PrerequisiteNotMetError("UC05 holds no onboarding case for this employee yet")
    snap, stop = _snapshot(deps, state, _actor_for(state, actor), Stage.ORIENTATION_SCHEDULING)
    if stop is not None or snap is None or not snap.found:
        raise PrerequisiteNotMetError("UC05 could not be read for this case; nothing was changed")
    new_state = copy.deepcopy(state)
    refreshed = []
    orientation = new_state.setdefault("orientation_schedule_result", {})
    orientation.setdefault("timezone", deps.tenants.timezone_for(state["client_id"], state.get("case_timezone")).key)
    for key, session_type in (("orientation_schedule_result", "induction"), ("team_introduction_result", "team_intro"),
                              ("buddy_introduction_result", "buddy_intro")):
        result = new_state.get(key) or {}
        live = next((s for s in snap.sessions if s.session_id == result.get("session_id")), None) \
            or snap.live_session(session_type)
        if result.get("session_id") and live is not None:
            result.update(_session_fields(new_state, snap, live))
            new_state[key] = result
            refreshed.append(key)
    buddy = new_state.get("buddy_assignment_result") or {}
    buddy_id = buddy.get("buddy_id") or buddy.get("proposed_buddy_id")
    if buddy_id:
        buddy.update(_buddy_fields(snap, buddy_id))
        new_state["buddy_assignment_result"] = buddy
        refreshed.append("buddy_assignment_result")
    return new_state, {"operation": "refresh_details", "refreshed": refreshed}


def read_welcome_message(state: OnboardingCaseState, deps: OrchestratorDeps, *,
                         actor: dict[str, Any]) -> dict[str, Any]:
    """The case's joining-day welcome as UC05 holds it (read-only): the draft UC05 wrote on the joining day, else a
    preview from the client's approved template (worker op welcome_message). Holds the joiner's name: callers gate it
    by case_identity_reader_roles."""
    welcome = state.get("welcome_message_result") or {}
    view: dict[str, Any] = {"case_id": state["case_id"], "joining_date": state.get("joining_date"),
                            "joining_day_event": bool(state.get("joining_day_event")),
                            "draft_status": welcome.get("draft_status"),
                            "delivery_status": welcome.get("operation_status")}
    if not (state.get("employee_ref") or {}).get("employee_id") or \
            not (state.get("orientation_schedule_result") or {}).get("uc05_case"):
        return {**view, "status": "no_uc05_case"}
    try:
        response = _call(deps, state, "welcome_message", {"employee_id": _eid(state)},
                         actor=_actor_for(state, actor), stage=Stage.JOINING_DAY_WELCOME)
    except _Transport as exc:
        return {**view, "status": "uc05_unavailable", "detail": str(exc)[:200]}
    if not response.ok:
        return {**view, "status": "uc05_error", "detail": (response.error_detail or "")[:200]}
    return {**view, **(response.result or {})}


def request_uc05_operation(state: OnboardingCaseState, deps: OrchestratorDeps, *, operation: str,
                           params: dict[str, Any], actor: dict[str, Any]) -> tuple[OnboardingCaseState, dict]:
    """An HR operation on this case's UC05 lifecycle, each one an existing UC05 capability with its
    prerequisite checked here first (PrerequisiteNotMetError otherwise). Returns (state, outcome)."""
    state = _require_employee_ref(state, deps)
    statuses = state["stage_statuses"]
    complete = StageStatus.COMPLETE.value
    worker_actor = _actor_for(state, actor)
    if not (state.get("orientation_schedule_result") or {}).get("uc05_case"):
        raise PrerequisiteNotMetError("UC05 holds no onboarding case for this employee yet")
    welcome = state.get("welcome_message_result") or {}
    reason = (params.get("reason") or "").strip()

    if operation == "restart_after_records_lost":
        return _restart_after_records_lost(state, deps, worker_actor, reason)
    if operation == "reschedule_session":
        session_type = params.get("session_type")
        stage = {"induction": Stage.ORIENTATION_SCHEDULING, "team_intro": Stage.TEAM_INTRODUCTION}.get(session_type)
        if stage is None:
            raise PrerequisiteNotMetError("session_type must be induction or team_intro")
        if statuses.get(stage.value) in (complete, StageStatus.AWAITING_APPROVAL.value):
            raise PrerequisiteNotMetError(f"the {session_type} is already {statuses[stage.value]}")
        if stage == Stage.TEAM_INTRODUCTION and statuses.get(Stage.BUDDY_ASSIGNMENT.value) != complete:
            raise PrerequisiteNotMetError("the team introduction follows buddy assignment (UC05 order)")
        op, payload = "schedule_session", {"employee_id": _eid(state), "session_type": session_type,
                                           "start": _aware(params.get("start"))}
    elif operation == "schedule_buddy_intro":
        stage = Stage.BUDDY_INTRODUCTION
        if statuses.get(Stage.BUDDY_ASSIGNMENT.value) != complete:
            raise PrerequisiteNotMetError("a buddy introduction needs an assigned buddy (UC05 BuddyNotAssignedError)")
        if statuses.get(stage.value) in (complete, StageStatus.AWAITING_APPROVAL.value):
            raise PrerequisiteNotMetError(f"the buddy introduction is already {statuses[stage.value]}")
        op, payload = "schedule_session", {"employee_id": _eid(state), "session_type": "buddy_intro",
                                           "start": _aware(params.get("start"))}
    elif operation == "propose_buddy":
        stage = Stage.BUDDY_ASSIGNMENT
        if statuses.get(Stage.ORIENTATION_SCHEDULING.value) != complete:
            raise PrerequisiteNotMetError("UC05 proposes a buddy only after the induction is confirmed")
        if statuses.get(stage.value) in (complete, StageStatus.AWAITING_APPROVAL.value):
            raise PrerequisiteNotMetError(f"buddy assignment is already {statuses[stage.value]}")
        op, payload = "propose_buddy", {"employee_id": _eid(state)}
    elif operation == "redraft_welcome":
        stage = Stage.JOINING_DAY_WELCOME
        if not state.get("joining_day_event"):
            raise PrerequisiteNotMetError("a welcome is drafted only on the joining-day event")
        if welcome.get("draft_status") != "rejected":
            raise PrerequisiteNotMetError("only a rejected welcome draft is redrafted")
        op, payload = "run_event", {"event": {"event": "draft_welcome", "employee_id": _eid(state)},
                                    "instance": _eid(state)}
    elif operation == "resolve_operation":
        key, stage = params.get("key") or "", _stage_for_operation_key(state, params.get("key") or "")
        if params.get("outcome") not in ("succeeded", "released"):
            raise PrerequisiteNotMetError("outcome must be succeeded or released")
        op, payload = "resolve_operation", {"key": key, "outcome": params["outcome"],
                                            "provider_ref": params.get("provider_ref"), "reason": reason or None}
    elif operation == "resolve_welcome_delivery":
        # An operator's verdict on a welcome whose delivery outcome is unknown, based on evidence
        # outside this system (the recipient, the provider's console). Reason required and audited.
        stage = Stage.JOINING_DAY_WELCOME
        resolution = params.get("resolution")
        if resolution not in WELCOME_RESOLUTIONS:
            raise PrerequisiteNotMetError("resolution must be delivered, not_delivered or keep_unresolved")
        if not reason:
            raise PrerequisiteNotMetError("a reason (the evidence checked) is required")
        if welcome.get("operation_status") not in ("pending", "uncertain") or not welcome.get("operation_key"):
            raise PrerequisiteNotMetError("the welcome has no delivery with an unknown outcome to resolve")
        if resolution == "keep_unresolved":
            new_state = copy.deepcopy(state)
            _record_delivery_review(new_state, resolution, reason, params.get("provider_ref"), worker_actor)
            return new_state, {"operation": operation, "outcome": "kept_unresolved"}
        op, payload = "resolve_operation", {"key": welcome["operation_key"], "outcome": WELCOME_RESOLUTIONS[resolution],
                                            "provider_ref": params.get("provider_ref"), "reason": reason}
    elif operation == "retry_welcome_delivery":
        # A new attempt only where the welcome is known NOT to have been delivered: an operator
        # resolved it not_delivered (released), the provider refused it permanently (rejected), or
        # it was never attempted. Never while the outcome is unknown, never once delivered.
        stage = Stage.JOINING_DAY_WELCOME
        if not reason:
            raise PrerequisiteNotMetError("a reason (why a new attempt is appropriate) is required")
        if welcome.get("draft_status") != "approved" or not welcome.get("draft_id"):
            raise PrerequisiteNotMetError("only an approved, unsent welcome can be retried")
        if welcome.get("operation_status") not in (None, "released", "rejected"):
            raise PrerequisiteNotMetError(f"the last delivery is {welcome.get('operation_status')}; resolve it first"
                                          if welcome.get("operation_status") in ("pending", "uncertain")
                                          else "the welcome was delivered")
        op, payload = "redeliver_welcome", {"draft_id": welcome["draft_id"], "reason": reason}
    else:
        raise PrerequisiteNotMetError(f"unknown UC05 operation '{operation}' (supported: {', '.join(UC05_OPERATIONS)})")

    try:
        response = _call(deps, state, op, payload, actor=worker_actor, stage=stage)
    except _Transport as exc:
        return record_uncertain(state, stage, f"UC05 worker unavailable during {operation}: {exc}"), \
            {"operation": operation, "outcome": "unknown"}
    if not response.ok:
        raise Uc05RelayError(response.error_code or "INTERNAL_ERROR", response.error_detail or "")
    if op == "run_event":
        workflow = (response.result or {}).get("workflow") or {}
        if workflow.get("pending_nodes"):
            exceptions = (response.result.get("response") or {}).get("exceptions") or [{}]
            raise Uc05RelayError("UC05_RUN_INTERRUPTED", f"UC05 stopped part-way: {exceptions[-1].get('detail', '')}")
        if workflow.get("stage") == "welcome_not_configured":
            raise Uc05RelayError("UC05_CLIENT_CONFIG_MISSING", _config_detail(workflow))
    out = {"operation": operation, **{k: v for k, v in (response.result or {}).items()
                                      if k in ("outcome", "session_id", "status", "approval_id", "approver_role",
                                               "buddy_id", "error_type", "detail", "key")}}
    if op == "run_event":
        out["outcome"] = ((response.result or {}).get("workflow") or {}).get("stage")
    elif op == "resolve_operation":
        out["outcome"] = (response.result or {}).get("status")
    new_state = copy.deepcopy(state)
    result_key = {Stage.ORIENTATION_SCHEDULING: "orientation_schedule_result",
                  Stage.TEAM_INTRODUCTION: "team_introduction_result",
                  Stage.BUDDY_INTRODUCTION: "buddy_introduction_result",
                  Stage.BUDDY_ASSIGNMENT: "buddy_assignment_result",
                  Stage.JOINING_DAY_WELCOME: "welcome_message_result"}[stage]
    new_state[result_key] = {**(new_state.get(result_key) or {}), "last_request": {
        "operation": operation, "outcome": out.get("outcome"), "error_type": out.get("error_type"),
        "detail": out.get("detail"), "requested_by": worker_actor.on_behalf_of}}
    if operation in ("resolve_welcome_delivery", "retry_welcome_delivery"):
        _record_delivery_review(new_state, params.get("resolution") or "retry", reason, params.get("provider_ref"),
                                worker_actor, outcome=out.get("outcome"))
    if operation == "schedule_buddy_intro" and out.get("outcome") == "scheduled":
        new_state["stage_statuses"][stage.value] = StageStatus.AWAITING_APPROVAL.value
    # The stage re-derives from UC05's records on resume; clear the stale verdict so it does.
    if (stage != Stage.BUDDY_INTRODUCTION
            and out.get("outcome") in ("scheduled", "proposed", "succeeded", "released", "welcome_awaiting_approval",
                                       "sent", "rejected")
            and new_state["stage_statuses"].get(stage.value) in (StageStatus.BLOCKED.value,
                                                                 StageStatus.UNCERTAIN.value)):
        new_state["stage_statuses"][stage.value] = StageStatus.IN_PROGRESS.value
    return new_state, out


def _record_delivery_review(state: OnboardingCaseState, resolution: str, reason: str, provider_ref: str | None,
                            actor: WorkerActor, *, outcome: str | None = None) -> None:
    """The case's own trail of operator actions on a welcome delivery (UC05 keeps the resolution on
    its operation record and audits it; the orchestrator audits the request as well)."""
    result = state["welcome_message_result"] = dict(state.get("welcome_message_result") or {})
    result.setdefault("delivery_reviews", []).append({
        "action": resolution, "reason": reason[:500], "provider_ref": provider_ref, "by": actor.on_behalf_of,
        "at": now_utc().isoformat(), "outcome": outcome})


def _config_detail(workflow: dict) -> str:
    errors = workflow.get("errors") or [{}]
    return str(errors[-1].get("detail") or "the tenant's welcome configuration is missing or invalid")


def _aware(value: Any) -> str:
    try:
        start = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise PrerequisiteNotMetError("start must be an ISO-8601 datetime") from None
    if start.tzinfo is None:
        raise PrerequisiteNotMetError("start must carry a timezone offset, e.g. +04:00")
    return start.isoformat()


def _stage_for_operation_key(state: OnboardingCaseState, key: str) -> Stage:
    """Only an operation of THIS case may be resolved through it."""
    employee_id, client_id = _eid(state), state["client_id"]
    welcome = (state.get("welcome_message_result") or {}).get("draft_id")
    if key.startswith(f"calendar:{client_id}:{employee_id}:"):
        session_type = key.split(":")[3]
        return _SESSION_STAGE.get(session_type, Stage.ORIENTATION_SCHEDULING)
    if welcome and key == f"welcome:{client_id}:{welcome}":
        return Stage.JOINING_DAY_WELCOME
    raise PrerequisiteNotMetError(f"operation '{key}' does not belong to case {state['case_id']}")
