"""Typed stage handoffs: UC01Output .. UC05Output.

Every stage run records one handoff in its StageOutcome (`stage_outcomes.<stage>.handoff`), which is a column of
`case_orchestration_state` in Supabase mode. The next stage reads the persisted case state, never a process-memory
hand-off; the handoff is the contract of what that state carries for the stage: the same envelope for every UC --

    contract            UC01Output | UC02Output | UC03Output | UC04Output | UC05Output
    case_id, client_id  the case and its tenant
    stage, status       the orchestrator stage and how it ended
    result              the UC's result for the next stage: ids, versions, counts, codes and statuses only
    warnings            the UC's warnings (codes)
    blockers            what blocks the stage: code, reason, owner, next action (+ required table/record when known)
    approval_requirements  the approvals the stage waits for: id, gate, approver role
    audit_reference     the execution that produced it (joins the UC's audit_events / agent_runs rows)
    timestamp           when the stage run finished (UTC)
    version             the stage run's attempt (monotonic; a later run's handoff supersedes this one)

Never a name, an e-mail address, a document value or a message body: `result` is built from an allow-list of keys
per UC, and nested values are reduced to scalars and lists of scalars.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1.0"


class StageHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract: str
    contract_version: Literal["1.0"] = CONTRACT_VERSION
    case_id: str
    client_id: str
    stage: str
    status: str
    result: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    approval_requirements: list[dict[str, Any]] = Field(default_factory=list)
    audit_reference: str | None = None
    timestamp: str
    version: int = Field(ge=1)


class UC01Output(StageHandoff):
    contract: Literal["UC01Output"] = "UC01Output"


class UC02Output(StageHandoff):
    contract: Literal["UC02Output"] = "UC02Output"


class UC03Output(StageHandoff):
    contract: Literal["UC03Output"] = "UC03Output"


class UC04Output(StageHandoff):
    contract: Literal["UC04Output"] = "UC04Output"


class UC05Output(StageHandoff):
    contract: Literal["UC05Output"] = "UC05Output"


CONTRACTS: dict[str, type[StageHandoff]] = {"UC01": UC01Output, "UC02": UC02Output, "UC03": UC03Output,
                                            "UC04": UC04Output, "UC05": UC05Output}

# Per UC: the state fields (and, for a dict, the keys inside it) the next stage relies on. Everything else is left out.
_UC01_FIELDS = ("employee_id", "employee_ref", "joining_date", "joining_date_revision", "case_timezone")
_UC02_PLAN_KEYS = ("plan_id", "plan_version", "previous_plan_version", "attributes", "config_version")
_UC03_FEEDBACK_KEYS = ("feedback_id", "plan_id", "plan_version", "validation_status", "verified_documents",
                       "missing_documents", "invalid_documents", "expired_documents", "pending_hr_reviews",
                       "correction_required", "replanning_required", "replanning_reasons")
_UC03_DOCUMENT_KEYS = ("revision", "classification", "document_status", "compliance_item_status",
                       "required_next_action", "persistence_status", "audit_reference")
_UC04_KEYS = ("overall_status", "readiness_reason_code", "items", "item_count", "approval_status", "evaluated_at",
              "store_version", "state_mode")
_UC05_KEYS = ("uc05_case", "uc05_employee_id", "uc05_start_date", "stage", "team_lead_id", "session_id",
              "session_type", "session_status", "status", "start_utc", "end_utc", "timezone", "meeting_link", "location",
              "calendar_event_id", "approval_id", "approval_status", "buddy_id", "buddy_valid", "draft_id",
              "draft_status", "language", "operation_status", "delivery", "delivery_status", "simulated_delivery")
_NEVER = frozenset({"name", "full_name", "email", "candidate_name", "candidate_email", "extracted_name", "body",
                    "subject", "personal_note", "metadata", "extracted_fields", "detail"})


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        items = [v for v in value if v is None or isinstance(v, (str, int, float, bool))]
        return items if len(items) == len(value) else len(value)  # a list of records is reported as its count
    if isinstance(value, dict):
        return {k: _scalar(v) for k, v in value.items() if k not in _NEVER
                and (v is None or isinstance(v, (str, int, float, bool)))}
    return str(value)


def _pick(source: dict[str, Any] | None, keys: tuple[str, ...]) -> dict[str, Any]:
    source = source or {}
    return {k: _scalar(source[k]) for k in keys if k in source and k not in _NEVER}


def _result(uc: str, stage: str, result_key: str | None, state: dict[str, Any],
            summary: dict[str, Any] | None) -> dict[str, Any]:
    if uc == "UC01":
        return {k: _scalar(state.get(k)) for k in _UC01_FIELDS if state.get(k) is not None}
    if uc == "UC02":
        return {**_pick(state.get("onboarding_plan"), _UC02_PLAN_KEYS), **(summary or {})}
    if uc == "UC03":
        if stage == "validate_documents":
            documents = {t: _pick(r, _UC03_DOCUMENT_KEYS) for t, r in (state.get("document_validation_result") or {}).items()}
            return {**_pick(state.get("document_validation_feedback"), _UC03_FEEDBACK_KEYS), "documents": documents}
        if stage == "document_collection":
            collection = state.get("document_collection") or {}
            return {"plan_version": collection.get("plan_version"),
                    "required_documents": [d.get("document_type") for d in collection.get("required_documents") or []]}
        submitted = state.get("submitted_documents") or {}
        return {"submitted": {t: {"revision": d.get("revision"), "source": d.get("source")} for t, d in submitted.items()}}
    if uc == "UC04":
        return _pick(state.get(result_key) if result_key else None, _UC04_KEYS)
    if uc == "UC05":
        return _pick(state.get(result_key) if result_key else None, _UC05_KEYS)
    return {}


def build_handoff(*, uc: str, stage: str, status: str, result_key: str | None, state: dict[str, Any],
                  warnings: list[Any], attempt: int, finished_at: str, execution_id: str | None,
                  summary: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The stage's typed handoff (JSON), or None for an orchestrator-internal stage."""
    contract = CONTRACTS.get(uc)
    if contract is None:
        return None
    blockers = [{k: b[k] for k in ("code", "reason", "owner", "next_action", "required_table", "required_record",
                                   "required_configuration") if b.get(k) is not None}
                for b in state.get("blockers") or [] if b.get("stage") == stage]
    approvals = [{k: a[k] for k in ("reference", "gate", "role", "proposal_type", "record_id") if a.get(k) is not None}
                 for a in state.get("pending_approvals") or [] if a.get("stage") == stage]
    codes = [getattr(w, "code", None) or (w.get("code") if isinstance(w, dict) else str(w)) for w in warnings or []]
    return contract(case_id=state["case_id"], client_id=state["client_id"], stage=stage, status=status,
                    result=_result(uc, stage, result_key, state, summary), warnings=[c for c in codes if c],
                    blockers=blockers, approval_requirements=approvals, audit_reference=execution_id,
                    timestamp=finished_at, version=max(int(attempt or 1), 1)).model_dump(mode="json")
