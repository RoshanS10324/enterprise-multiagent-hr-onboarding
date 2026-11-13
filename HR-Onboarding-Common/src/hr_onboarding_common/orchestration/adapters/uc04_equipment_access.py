"""Stage 3 adapter: UC04 Equipment & Access (`Uc04Agent.evaluate_orchestrated`).

Real invocation: `Uc04Agent().evaluate_orchestrated(OrchestratorRequest) -> OrchestratorResponse`,
synchronous. Runs via the UC04 worker process (module-name collision with UC01/UC05 on
`sub_agent`).

Verified: UC04 enforces caller role in code (`Policy.check_caller`, allowlist includes
`hr_onboarding_officer`/`hiring_manager`/`onboarding_orchestrator`) -- this adapter passes the
REAL verified actor's role (never a fabricated "onboarding_orchestrator" identity) and relies on
UC04's own check; no orchestrator-side boundary check is layered on top here.

Verified: UC04 does not self-register a case (`UnknownCaseError` otherwise) -- its own worker
handler registers the case into UC04's mock case registry before calling evaluate_orchestrated,
exactly as UC04's own test harness (`Env`) does, since UC04 explicitly documents itself as never
creating a case (BR-24) and the orchestrator is the upstream system here.

Which equipment/access items a role should request, and the approver/lead-time matrices
(`CaseConfig`), are client-owned configuration UC04 does not derive itself. It is chosen here and
passed inline, never fabricated, from one source set by UC04_CONFIG_SOURCE:
  file (default)  the JSON file named by `state["uc04_client_config_ref"]`, else config/tenants.yaml
  central         the client's in-force approved configuration in Supabase (uc04_central_config.py);
                  no fallback to a file: a missing or invalid central configuration blocks the stage
The result records which source and version UC04 evaluated against (`config_source`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path

from hr_onboarding_common.orchestration import joining_date, uc04_central_config
from hr_onboarding_common.orchestration.adapters import uc04_uc02_plan
from hr_onboarding_common.orchestration.adapters._shared import (add_blocker, add_pending_approval, record_stage_error,
                                                                 with_stage_status)
from hr_onboarding_common.orchestration.contracts import Actor as WorkerActor
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import OnboardingCaseState, Stage, StageStatus, idempotency_key
from hr_onboarding_common.orchestration.workers.uc_worker_client import WorkerCrashedError, WorkerTimeoutError

_READY_STATUSES = {"READY", "IN_PROGRESS"}
_AT_RISK_STATUSES = {"AT_RISK"}
_APPROVAL_STATES = {"APPROVAL_PENDING", "EXCEPTION_PENDING"}  # UC04 item states that wait for an approval
UC04_AGENT = "dact-uc04-equipment-access"
UC04_ITEM_APPROVAL = "uc04_item_approval"  # an item approval the orchestrator records for UC04 (approval_records)


def _kind(item_id: str | None, payload: dict) -> str:
    """EQUIPMENT or ACCESS for one evaluated item, from the request the orchestrator sent (UC04's approver class)."""
    for kind, key in (("EQUIPMENT", "requested_equipment"), ("ACCESS", "requested_access")):
        if any(r.get("item_id") == item_id for r in payload.get(key) or []):
            return kind
    return "EQUIPMENT"


def _requested_items(items: list, kind: str, requested_by: str) -> list[dict]:
    """HR's recorded item ids as UC04 RequestedItem objects (request_key, item_id, item_kind, requested_by). The key
    is derived from the item, so a re-evaluation sends the same request; already-structured items pass through."""
    out = []
    for item in items or []:
        if isinstance(item, dict):
            out.append(item)
        else:
            out.append({"request_key": f"{'EQ' if kind == 'EQUIPMENT' else 'AC'}-{item}"[:64], "item_id": item,
                        "item_kind": kind, "requested_by": requested_by})
    return out


_ROLE_CODE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")  # valid under both UC04 identity profiles


def uc04_role_code(role_id: str | None) -> str | None:
    """The case's role as a UC04 role code: lower case, every run of other characters as one `_`
    ("Power BI Developer" -> "power_bi_developer"; "software_engineer" is unchanged). A deterministic format mapping,
    not a lookup: two roles that differ only in case or punctuation are the same UC04 role. None when the case names
    no role; a value that still is no code (e.g. starts with a digit) is returned as-is for the caller to refuse."""
    if not role_id or not str(role_id).strip():
        return None
    code = re.sub(r"[^a-z0-9]+", "_", str(role_id).strip().lower()).strip("_")
    return code if _ROLE_CODE.match(code) else str(role_id)


_ITEM_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")  # UC04's item id shape (what HR may record)


def _recommendation(rec: dict | None, payload: dict) -> dict | None:
    """UC04's role-package recommendation (skill recommend_role_standard_items), passed to HR only when it is for this
    case's client and role and lists valid item ids; HR reviews it and records the request. Never applied here."""
    if not isinstance(rec, dict):
        return None
    if (rec.get("client_id"), rec.get("role_id")) != (payload["client_id"], payload["role_id"]):
        return None
    lists = {kind: [i for i in rec.get(kind) or [] if isinstance(i, str) and _ITEM_ID.match(i)]
             if rec.get(kind) is not None else None for kind in ("equipment", "access")}
    if not lists["equipment"] and not lists["access"]:
        return None  # nothing to recommend (missing or empty matrices): HR is asked as before
    return {"skill": rec.get("skill"), "skill_version": rec.get("skill_version"), "source": "uc04",
            "role_code": rec["role_id"], "config_version": rec.get("config_version"), **lists,
            "gaps": [str(g) for g in rec.get("gaps") or []], "advisory_only": True, "note": rec.get("note")}


def uc04_approval_record(state: OnboardingCaseState, entry: dict, *, decision: str, approver_id: str,
                         decided_on: date | None = None) -> dict | None:
    """The UC04 ApprovalRecord (contracts.ApprovalRecord) for a person's decision on one pending UC04 item approval:
    the request and item UC04 evaluated, the approver and role as decided, and the scope UC04 checks (client, the UC04
    role code, the client-site requirement). None when UC04's last result no longer holds that request."""
    import uuid

    result = state.get("equipment_access_result") or {}
    item = next((i for i in result.get("items") or [] if i.get("request_id") == entry.get("record_id")), None)
    if item is None:
        return None
    role_ctx = state.get("uc02_role_context") or {}
    return {"approval_id": f"APR-{uuid.uuid4().hex[:16]}", "request_id": item["request_id"],
            "approval_type": entry["reference"].rsplit(":", 1)[-1], "approver_id": approver_id,
            "approver_role": entry["role"], "decision": "APPROVED" if decision == "approved" else "REJECTED",
            "decided_on": (decided_on or date.today()).isoformat(), "item_id": item["item_id"],
            "client_id": state["client_id"],
            "role_id": result.get("role_code") or uc04_role_code(role_ctx.get("role_id")),
            "client_site_required": bool(role_ctx.get("client_site_required", False))}


def _evaluation_key(state: OnboardingCaseState, base: str, payload: dict) -> tuple[str, str, str]:
    """(key, base, digest) for this UC04 evaluation.

    UC04 replays a key with the SAME payload and refuses one reused with a different payload (IdempotencyConflict,
    BR-21). So a re-evaluation with new input -- HR recorded the requests, approvals arrived, the configuration or the
    as-of date changed -- needs its own key, and an identical retry must keep its key. The first evaluation under a
    base key uses the base itself; a later one with different content gets the base plus a digest of the payload.
    Recorded on the result (the branch keeps only equipment_access_result)."""
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:12]
    previous = state.get("equipment_access_result") or {}
    if not previous:
        return base, base, digest
    # A result recorded before the key was kept was evaluated under the plain base key.
    if previous.get("idempotency_key_base", base) != base:
        return base, base, digest  # a new joining-date revision: its first evaluation
    if previous.get("payload_digest") == digest:
        return previous.get("idempotency_key") or base, base, digest  # the same evaluation again: a replay
    return f"{base}.{digest}", base, digest


_CENTRAL_NEXT_ACTION = {
    uc04_central_config.MISSING: "Approve and publish the client's configuration version with its uc04_equipment_access section",
    uc04_central_config.INVALID: "Correct the client's central configuration (uc04_equipment_access / shared_configuration)",
    uc04_central_config.NOT_PERMITTED: "This client's configuration is test data: use an approved client configuration",
    uc04_central_config.ROLE_NOT_CONFIGURED: "Add the role to the client's equipment/access matrices",
    uc04_central_config.GRADE_REQUIRED: "Record the joiner's grade on the case",
    uc04_central_config.UNAVAILABLE: "Retry when the configuration database is reachable",
    uc04_central_config.DENIED: "Ask the platform administrator to check the database credentials",
    uc04_central_config.NOT_CONFIGURED: "Configure the shared configuration reader (Supabase settings)",
}


def _blocked(state: OnboardingCaseState, code: str, detail: str, next_action: str, *, owner: str = "hr_admin",
             extra: dict | None = None) -> OnboardingCaseState:
    new_state = with_stage_status(state, Stage.EQUIPMENT_ACCESS, StageStatus.BLOCKED)
    record_stage_error(new_state, Stage.EQUIPMENT_ACCESS, code=code, detail=detail, retryable=True)
    add_blocker(new_state, stage=Stage.EQUIPMENT_ACCESS, code=code, reason=detail, owner=owner,
                next_action=next_action, extra=extra)
    new_state["next_action"] = f"hr_review:equipment_access:{code.lower()}"
    return new_state


def _config_source(deps: OrchestratorDeps) -> str:
    from hr_onboarding_common.orchestration.uc04_config import config_source

    configured = getattr(deps, "uc04_config_source", None)
    return config_source({"UC04_CONFIG_SOURCE": configured} if configured else os.environ)


def _config_reader(deps: OrchestratorDeps):
    if getattr(deps, "uc04_config_reader", None) is None:
        from hr_onboarding_common.orchestration.uc04_config import SupabaseUc04ConfigReader

        deps.uc04_config_reader = SupabaseUc04ConfigReader.from_env(os.environ)
    return deps.uc04_config_reader


def call_uc04_equipment_access(state: OnboardingCaseState, deps: OrchestratorDeps) -> OnboardingCaseState:
    role_ctx = state.get("uc02_role_context", {})
    role_code = uc04_role_code(role_ctx.get("role_id"))
    if role_code is not None and not _ROLE_CODE.match(role_code):
        return _blocked(state, "UC04_ROLE_CODE_INVALID",
                        f"the case's role {role_ctx.get('role_id')!r} is not a UC04 role code (letters, digits and _, "
                        f"starting with a letter)", "Correct the case's role code")
    try:
        source = uc04_central_config.config_source(os.environ)
    except uc04_central_config.Uc04ConfigError as exc:
        return _blocked(state, exc.code, exc.detail, "Set UC04_CONFIG_SOURCE to 'file' or 'central'")
    if source == uc04_central_config.SOURCE_CENTRAL:
        # The client's in-force central configuration, and nothing else: no fallback to a file (uc04_central_config).
        if state.get("uc04_client_config_ref"):
            return _blocked(state, uc04_central_config.SOURCE_CONFLICT,
                            "the case names a configuration file while UC04_CONFIG_SOURCE=central; one source only",
                            "Remove the case's uc04_client_config_ref or switch UC04_CONFIG_SOURCE to 'file'")
        if role_code is None:
            return _blocked(state, uc04_central_config.ROLE_NOT_CONFIGURED,
                            f"the case names no role; the central configuration of client {state['client_id']} is per role",
                            "Record the joiner's role on the case")
        try:
            resolved = uc04_central_config.resolve(
                uc04_central_config.rest_factory(os.environ), client_id=state["client_id"], role=role_code,
                grade=role_ctx.get("grade") or None, joining_date=date.fromisoformat(str(state["joining_date"])),
                environ=os.environ)
        except uc04_central_config.Uc04ConfigError as exc:
            return _blocked(state, exc.code, exc.detail, _CENTRAL_NEXT_ACTION.get(exc.code, "Review the client's configuration"))
        config, config_provenance = resolved.config, resolved.provenance()
    else:
        # The case's own reference (start request), else the tenant's configured one for this role (config/tenants.yaml).
        config_ref = (state.get("uc04_client_config_ref")
                      or deps.tenants.uc04_config_ref_for(state["client_id"], role_code))
        if not config_ref or not Path(config_ref).exists():
            # No configuration is ever borrowed from another client or role, or invented: the stage waits for its own.
            role_part = f", role {role_code}" if role_code else ""
            return _blocked(state, "UC04_CONFIGURATION_MISSING",
                            f"no approved equipment/access configuration (uc04_equipment_access) is available for client "
                            f"{state['client_id']}{role_part}",
                            "Provide and approve the client's equipment/access configuration")
        config = json.loads(Path(config_ref).read_text(encoding="utf-8"))
        config_provenance = {"source": uc04_central_config.SOURCE_FILE, "file": Path(config_ref).name}
    # This case's UC-02 onboarding plan (uc04_uc02_plan.py): read from UC-02 itself and accepted only when it is this
    # case's plan; otherwise the stage waits for it. Never another case's plan, never a stand-in.
    try:
        uc02_plan = uc04_uc02_plan.read_plan(state, deps)
    except uc04_uc02_plan.Uc02PlanError as exc:
        uc04_uc02_plan.log_refusal(state, exc)
        return _blocked(state, exc.code, exc.detail, exc.next_action)
    recorded_by = (state.get("equipment_access_requests") or {}).get("recorded_by") or state["actor"]["on_behalf_of"]
    input_payload = {
        "case_id": state["case_id"], "employee_ref": state["employee_id"], "client_id": state["client_id"],
        "role_id": role_code or config.get("role_id", ""), "joining_date": state["joining_date"],
        "offer_accepted": True, "onboarding_form_approved": True, "form_version": "v1",
        "work_location": role_ctx.get("work_location", "Office"), "work_mode": role_ctx.get("work_mode", "OFFICE"),
        "client_site_required": role_ctx.get("client_site_required", False),
        "requested_access": _requested_items(role_ctx.get("requested_access", []), "ACCESS", recorded_by),
        "requested_equipment": _requested_items(role_ctx.get("requested_equipment", []), "EQUIPMENT", recorded_by),
        "approval_records": role_ctx.get("approval_records", []),
        "as_of_date": role_ctx.get("as_of_date", date.today().isoformat()),
        "config": config,
    }

    actor = state["actor"]
    # UC04's CallerContext.idempotency_key is pattern-restricted to [A-Za-z0-9._-]{1,80} -- the
    # generic colon-separated key (state.idempotency_key) doesn't satisfy it; sanitize locally
    # rather than loosening the shared helper for one UC's stricter contract.
    key = idempotency_key(state, Stage.EQUIPMENT_ACCESS).replace(":", ".")
    revision = joining_date.revision(state)
    if revision > 1:
        # A revised joining date is a new evaluation for UC04 (its EC-11 reassessment re-plans due
        # dates), never a replay: UC04 refuses a key reused with a different payload. Each revision
        # keeps one key, so a retry within it is still idempotent.
        key = f"{key}.jd{revision}"
    key, base, digest = _evaluation_key(state, key, input_payload)
    worker = deps.worker_pool.get("uc04")
    try:
        response = worker.call(
            "evaluate_orchestrated", {"input": input_payload},
            actor=WorkerActor(on_behalf_of=actor["on_behalf_of"], roles=actor.get("roles", [])),
            run_id=state["correlation_id"], idempotency_key=key,
        )
    except (WorkerCrashedError, WorkerTimeoutError) as exc:
        from hr_onboarding_common.orchestration.adapters._shared import record_uncertain
        # Re-query before assuming failure: re-call with the SAME idempotency_key. If the
        # worker's in-memory store survived (no restart), UC04's own idempotent evaluation
        # returns the same result; if the worker was restarted, its store is gone and this
        # re-call will legitimately re-evaluate from scratch (documented limitation, see
        # docs/ONBOARDING_COMMON_ARCHITECTURE.md Worker restart & durable state).
        if not deps.worker_pool.get("uc04").is_alive:
            return record_uncertain(state, Stage.EQUIPMENT_ACCESS,
                                     f"uc04 worker crashed; in-memory case/ticket state may be lost: {exc}")
        return record_uncertain(state, Stage.EQUIPMENT_ACCESS, f"uc04 worker call failed: {exc}")

    blocked = (response.result or {}).get("blocked") if response.ok else None
    if blocked:
        # The UC04 worker refused before evaluating, for a dependency it names (e.g. its identity profile):
        # a resumable block, not a technical failure.
        new_state = with_stage_status(state, Stage.EQUIPMENT_ACCESS, StageStatus.BLOCKED)
        record_stage_error(new_state, Stage.EQUIPMENT_ACCESS, code=blocked["code"], detail=blocked.get("detail", ""),
                           retryable=True)
        add_blocker(new_state, stage=Stage.EQUIPMENT_ACCESS, code=blocked["code"], reason=blocked.get("detail", ""),
                    owner=blocked.get("owner", "ops"), next_action=blocked.get("next_action", "Resolve the dependency"))
        new_state["next_action"] = f"ops_review:equipment_access:{blocked['code'].lower()}"
        return new_state

    if not response.ok:
        new_state = with_stage_status(state, Stage.EQUIPMENT_ACCESS, StageStatus.FAILED)
        new_state["error_code"] = response.error_code
        new_state["error_detail"] = response.error_detail
        # The stage's own error, so the case shows THIS failure (not an earlier one) and its cause.
        record_stage_error(new_state, Stage.EQUIPMENT_ACCESS, code=response.error_code or "UC04_FAILED",
                           detail=(response.error_detail or "")[:500], retryable=bool(response.retryable))
        return new_state

    result = response.result["result"]
    if result.get("case_id") not in (None, state["case_id"]):
        # UC04 evaluated a different case than the one asked for: none of it is this case's readiness.
        failed = with_stage_status(state, Stage.EQUIPMENT_ACCESS, StageStatus.FAILED)
        record_stage_error(failed, Stage.EQUIPMENT_ACCESS, code="IDENTITY_CONFLICT", retryable=False,
                           detail="UC04 answered for a different case than the one requested")
        failed["next_action"] = "ops_review:equipment_access:identity_conflict"
        return failed
    overall_status = result["overall_status"]
    new_state = dict(state)
    new_state["equipment_access_result"] = {
        "overall_status": overall_status,
        "items": [{"request_id": i["request_id"], "item_id": i["item_id"], "state": i["state"],
                   "ticket_id": i.get("ticket_id")} for i in result.get("items", [])],
        # The date this evaluation was for, and UC04's own record of a re-plan for a changed date.
        "joining_date": state["joining_date"], "joining_date_revision": revision,
        "reassessment": result.get("reassessment"),
        # Which configuration UC04 evaluated against, so the outcome is traceable to it.
        "role_code": input_payload["role_id"], "config_version": config.get("version"),
        "config_source": config_provenance,
        # The evaluation this result answers (see _evaluation_key).
        "idempotency_key": key, "idempotency_key_base": base, "payload_digest": digest,
        # The UC-02 plan this evaluation ran with, its service tasks and the advisory comparisons (never blocking).
        "uc02_plan": uc04_uc02_plan.advisory_checks(uc02_plan, uc04_config_version=config.get("version"),
                                                    uc04_items=result.get("items", []) or []),
    }
    uc04_uc02_plan.log_plan(state, new_state["equipment_access_result"]["uc02_plan"])

    advisories = []
    for proposal in response.result.get("proposals", []) or []:
        owner = proposal.get("requires_approval_by") or "hiring_manager"
        if owner.isupper():
            # UC04's own owner codes (HR_OFFICER, IT_SERVICE_DESK, ...) mark ADVICE for that owner -- a loan device,
            # another joining date -- not an approval anyone decides here: kept as advice, HR acts on it.
            advisories.append({"type": proposal.get("type"), "subject_id": proposal.get("subject_id"), "owner": owner,
                               "rationale": proposal.get("rationale")})
            continue
        # A case-level proposal has no request as subject: give it a stable reference of its own instead of none.
        reference = proposal.get("subject_id") or f"{state['case_id']}:{proposal.get('type') or 'proposal'}"
        add_pending_approval(new_state, agent=UC04_AGENT, reference=reference, role=owner,
                              description=proposal.get("rationale") or "UC04 approval/exception needed")
    new_state["equipment_access_result"]["advisories"] = advisories

    # Each requested item UC04 holds for an approval: one approval for the role UC04's approver configuration names
    # for that item's class (decide_uc04_item_approval passes the decision to UC04 as an approval record).
    awaiting = [i for i in result.get("items", []) or [] if i.get("state") in _APPROVAL_STATES]
    approvers = (config.get("approver_config") or {}).get("value") or {}
    for item in awaiting:
        exception = item.get("state") == "EXCEPTION_PENDING"
        approval_class = "EXCEPTION" if exception else _kind(item.get("item_id"), input_payload)
        role = (approvers.get(approval_class) or {}).get("role")
        if not role or not item.get("request_id"):
            continue  # UC04 reports a missing approver configuration itself
        approval_type = "EXCEPTION" if exception else "STANDARD"
        add_pending_approval(new_state, agent=UC04_AGENT, reference=f"{item['request_id']}:{approval_type}", role=role,
                              description=f"UC04 {approval_type.lower()} approval: {approval_class.lower()} item "
                                          f"{item['item_id']} for this joiner",
                              stage=Stage.EQUIPMENT_ACCESS, proposal_type=UC04_ITEM_APPROVAL,
                              record_id=item["request_id"])

    if overall_status in _AT_RISK_STATUSES:
        advice = "; ".join(a["rationale"] for a in advisories if a.get("rationale"))
        add_blocker(new_state, stage=Stage.EQUIPMENT_ACCESS, code="AT_RISK",
                    reason=f"UC04: equipment/access is at risk of not being ready by the joining date "
                           f"{state['joining_date']} (lead times)" + (f". UC04 suggests: {advice}" if advice else ""),
                    owner="hr_admin", next_action="Review UC04's suggestions and expedite")
    # Approved and under way (at risk or not): UC04 owns provisioning from here; the stage is done.
    if overall_status in _READY_STATUSES or (overall_status in _AT_RISK_STATUSES and not awaiting
                                            and not result.get("blockers")):
        return with_stage_status(new_state, Stage.EQUIPMENT_ACCESS, StageStatus.COMPLETE)

    recommendation = _recommendation(response.result.get("role_package_recommendation"), input_payload)
    for blocker in result.get("blockers", []) or []:
        code = blocker.get("code", "BLOCKED")
        extra = {"recommendation": recommendation} if code == "NO_ITEMS_REQUESTED" and recommendation else None
        add_blocker(new_state, stage=Stage.EQUIPMENT_ACCESS, code=code,
                    reason=blocker.get("reason", ""), owner=blocker.get("owner", "hr_admin"),
                    next_action=blocker.get("next_action", "Resolve blocker"), extra=extra)
    if awaiting:
        roles = sorted({p["role"] for p in new_state["pending_approvals"]
                        if p.get("proposal_type") == UC04_ITEM_APPROVAL} or {"approver"})
        add_blocker(new_state, stage=Stage.EQUIPMENT_ACCESS, code="UC04_APPROVALS_PENDING",
                    reason=f"{len(awaiting)} requested item(s) await approval by {' or '.join(roles)} in UC04: "
                           f"{', '.join(sorted(i['item_id'] for i in awaiting))}",
                    owner=roles[0], next_action="Decide each item in the approvals inbox")
    new_state["next_action"] = f"hr_review:equipment_access:{overall_status.lower()}"
    waiting = overall_status == "AWAITING_APPROVAL" or (overall_status in _AT_RISK_STATUSES and awaiting)
    status = StageStatus.AWAITING_APPROVAL if waiting else StageStatus.BLOCKED
    return with_stage_status(new_state, Stage.EQUIPMENT_ACCESS, status)
