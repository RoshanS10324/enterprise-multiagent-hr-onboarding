"""Stage adapter: UC03 document collection for the current UC02 plan version.

    orchestrator stage `document_collection` -> call_uc03_document_collection
        -> UC03 DocumentCollectionService.open_collection(DocumentCollectionRequest)   (src/sub_agent/collection.py)
        -> the (mock) document request to the employee

OWNERSHIP. UC03 owns the document lifecycle: it receives the document requirements of UC02's plan
version (`onboarding_plan`), opens the collection for that version in its own registry (idempotent per
version; an older version is refused), and keeps every submitted revision. UC02 never collects or
validates documents; it only says which documents a plan version requires.

What it does:
  1. checks the orchestrator's own boundary role (`document_collection_trigger_roles`);
  2. builds the UC02 -> UC03 DocumentCollectionRequest from the plan (validation rules from UC03's catalog);
  3. opens UC03's collection for that plan version;
  4. sends ONE request per plan version through the MOCK notification port (`deps.notifier`), naming only
     the documents not yet on file, under an idempotency key fixed per case and plan version, written
     ahead to the case store before the send so a restart sees "sending" rather than nothing. A revised
     plan that adds nothing to collect needs no request (`not_required`).
Outcomes:
  COMPLETE        the collection is open for the current plan version and the request was accepted by
                  the (mock) transport, now or earlier, or none was needed -- never resent
  RETRY_PENDING   DOCUMENT_REQUEST_DELIVERY_FAILED: the transport refused it (known not delivered);
                  a resume or retry sends again under the same key.
                  DOCUMENT_REQUEST_DELIVERY_UNCERTAIN: the outcome is unknown (transport lost, or the
                  process stopped mid-send). Never resent by a plain resume; only an authorized
                  retry_stage sends again, under the same key.
                  UC03_COLLECTION_UNAVAILABLE: UC03's collection service could not be reached.
  BLOCKED         PLAN_DOCUMENTS_MISSING: the plan lists no document; STALE_PLAN_VERSION /
                  PLAN_VERSION_CONFLICT: UC03 refused the plan version (UC03's own check).
"""

from __future__ import annotations

import copy

from hr_onboarding_common.contracts import now_utc
from hr_onboarding_common.orchestration.adapters._shared import (add_blocker, record_policy_denial,
                                                                 record_stage_error, require_role, with_stage_status)
from hr_onboarding_common.orchestration.adapters.uc03_documents import (_ensure_uc03_importable, checklist_context,
                                                                        collection_service)
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.notifications import NotificationMessage
from hr_onboarding_common.orchestration.state import OnboardingCaseState, Stage, StageStatus
from hr_onboarding_common.orchestration.workflow_models import DocumentCollectionRequest, DocumentRequestNotification
from hr_onboarding_common.platform.policy import PolicyViolation

STAGE = Stage.DOCUMENT_COLLECTION


def _stop(state: OnboardingCaseState, status: StageStatus, code: str, detail: str, *, owner: str = "ops",
          action: str) -> OnboardingCaseState:
    new_state = with_stage_status(state, STAGE, status)
    record_stage_error(new_state, STAGE, code=code, detail=detail, retryable=status != StageStatus.FAILED)
    new_state["next_action"] = f"{'retry' if status == StageStatus.RETRY_PENDING else 'hr_review'}:{STAGE.value}:" \
                               f"{code.lower()}"
    add_blocker(new_state, stage=STAGE, code=code, reason=detail, owner=owner, next_action=action)
    return new_state


def notification_key(case_id: str, plan_version: int) -> str:
    """Fixed per case and plan version (plan version 1 keeps the key the first release used)."""
    return f"doc-collection:{case_id}:r{plan_version}"


def collection_request(state: OnboardingCaseState, deps: OrchestratorDeps) -> DocumentCollectionRequest:
    """The UC02 -> UC03 request for the current plan version (validation rules from UC03's catalog)."""
    plan = state["onboarding_plan"]
    service = collection_service(deps)
    context = checklist_context(state)
    documents = plan["required_documents"]
    return DocumentCollectionRequest(
        case_id=state["case_id"], tenant_id=state["client_id"], employee_id=state["employee_id"],
        correlation_id=state["correlation_id"], plan_id=plan["plan_id"], plan_version=plan["plan_version"],
        joining_date=plan.get("effective_joining_date") or state.get("joining_date"),
        required_documents=[d["document_type"] for d in documents], document_requirements=documents,
        mandatory_documents=[d["document_type"] for d in documents if d.get("required", True)],
        validation_rules={d["document_type"]: service.validation_rules(d["document_type"], context) for d in documents},
        due_dates={d["document_type"]: d.get("due_date") for d in documents},
        metadata={"source": plan.get("source", "uc02"), "data_origin": "synthetic"})


def _open_in_uc03(request: DocumentCollectionRequest, deps: OrchestratorDeps) -> None:
    _ensure_uc03_importable(deps.uc03_repo_root)
    from src.sub_agent.collection import DocumentCollectionRequest as Uc03Request  # type: ignore

    collection_service(deps).open_collection(Uc03Request(
        case_id=request.case_id, tenant_id=request.tenant_id, employee_id=request.employee_id,
        correlation_id=request.correlation_id, plan_id=request.plan_id, plan_version=request.plan_version,
        joining_date=request.joining_date,
        required_documents=[{"document_type": d.document_type, "label": d.label, "mandatory": d.required,
                             "due_date": d.due_date, "validation_rules": request.validation_rules.get(d.document_type, {}),
                             "reason": d.reason or ""} for d in request.document_requirements],
        metadata=request.metadata))


def call_uc03_document_collection(state: OnboardingCaseState, deps: OrchestratorDeps) -> OnboardingCaseState:
    try:
        require_role(deps, state, settings_key="document_collection_trigger_roles")
    except PolicyViolation as exc:
        return record_policy_denial(state, STAGE, exc)
    if not state.get("employee_id"):
        return _stop(state, StageStatus.BLOCKED, "MISSING_DATA", "UC01 reported no employee number; documents "
                     "cannot be requested for an unidentified employee", owner="hr_admin",
                     action="Correct the employee record in UC01, then resume")
    plan = state.get("onboarding_plan") or {}
    if not any(d.get("required", True) for d in plan.get("required_documents") or []):
        return _stop(state, StageStatus.BLOCKED, "PLAN_DOCUMENTS_MISSING", "UC02's plan lists no required document",
                     owner="hr_admin", action="Review the plan's document requirements in UC02, then resume")
    try:
        request = collection_request(state, deps)
        _open_in_uc03(request, deps)
    except Exception as exc:  # noqa: BLE001 -- a UC03 refusal (CollectionError) or a technical failure
        code = getattr(exc, "code", None)
        if code in ("STALE_PLAN_VERSION", "PLAN_VERSION_CONFLICT", "TENANT_MISMATCH"):
            return _stop(state, StageStatus.BLOCKED, code, f"UC03 refused plan version {plan.get('plan_version')}: "
                         f"{getattr(exc, 'detail', '')}", owner="hr_admin",
                         action="Reconcile the plan version with UC02, then resume")
        return _stop(state, StageStatus.RETRY_PENDING, "UC03_COLLECTION_UNAVAILABLE",
                     f"UC03's document collection could not be opened ({type(exc).__name__})",
                     action="Retry once UC03 is available")

    new_state = copy.deepcopy(state)
    record = new_state["document_collection"] = dict(new_state.get("document_collection") or {})
    previous_version = record.get("plan_version") or (1 if record.get("notification") else None)
    record.update(required_documents=[dict(d) for d in plan["required_documents"]], source="uc02_plan",
                  plan_id=plan["plan_id"], plan_version=plan["plan_version"], request=request.model_dump(mode="json"))
    record.setdefault("outstanding", [])
    record.setdefault("previous_notifications", [])
    if record.get("notification") and previous_version != plan["plan_version"]:
        record["previous_notifications"] = [*record["previous_notifications"], record["notification"]]
        record["notification"] = None
    submitted = state.get("submitted_documents") or {}
    to_request = [d for d in plan["required_documents"] if d.get("required", True) and d["document_type"] not in submitted]
    notification = record["notification"] = dict(record.get("notification") or DocumentRequestNotification(
        idempotency_key=notification_key(state["case_id"], plan["plan_version"]),
        recipient=f"employee:{state['employee_id']}", document_types=[d["document_type"] for d in to_request],
    ).model_dump())

    if notification["status"] in ("sent", "not_required"):
        return with_stage_status(new_state, STAGE, StageStatus.COMPLETE)
    if not to_request:
        notification.update(status="not_required", detail="every required document of this plan version is on file")
        return with_stage_status(new_state, STAGE, StageStatus.COMPLETE)
    if notification["status"] in ("sending", "uncertain") and not notification.get("retry_authorized"):
        notification["status"] = "uncertain"
        notification["detail"] = notification.get("detail") or "the process stopped while the request was being sent"
        return _stop(new_state, StageStatus.RETRY_PENDING, "DOCUMENT_REQUEST_DELIVERY_UNCERTAIN",
                     "the document request may or may not have been delivered; it is not resent automatically",
                     owner="hr_admin", action="Check with the employee, then retry the stage (it sends again under "
                                              "the same idempotency key)")

    notification.update(status="sending", attempts=notification.get("attempts", 0) + 1,
                        last_attempt_at=now_utc().isoformat(), retry_authorized=False, detail=None)
    if deps.persist_progress is not None:  # write-ahead: a restart now finds "sending", not nothing
        deps.persist_progress(with_stage_status(new_state, STAGE, StageStatus.IN_PROGRESS))
    message = NotificationMessage(
        idempotency_key=notification["idempotency_key"], client_id=state["client_id"], case_id=state["case_id"],
        recipient=notification["recipient"], document_types=[d["document_type"] for d in to_request],
        subject="[SYNTHETIC] Onboarding documents requested",
        body=f"Case {state['case_id']} (plan version {plan['plan_version']}): please provide "
             + ", ".join(d.get("label") or d["document_type"] for d in to_request),
    )
    try:
        receipt = deps.notifier.send(message)
    except Exception as exc:  # noqa: BLE001 -- transport lost: the outcome is unknown
        notification.update(status="uncertain", detail=f"transport failure ({type(exc).__name__})")
        return _stop(new_state, StageStatus.RETRY_PENDING, "DOCUMENT_REQUEST_DELIVERY_UNCERTAIN",
                     "the document request may or may not have been delivered; it is not resent automatically",
                     owner="hr_admin", action="Check with the employee, then retry the stage (it sends again under "
                                              "the same idempotency key)")
    if receipt.status in ("simulated_sent", "duplicate_ignored"):
        notification.update(status="sent", message_id=receipt.message_id, detail=receipt.detail)
        record["requested_at"] = now_utc().isoformat()
        return with_stage_status(new_state, STAGE, StageStatus.COMPLETE)
    notification.update(status="failed", detail=receipt.detail)
    return _stop(new_state, StageStatus.RETRY_PENDING, "DOCUMENT_REQUEST_DELIVERY_FAILED",
                 f"the (mock) transport refused the document request: {receipt.detail}",
                 action="Retry the stage; nothing was delivered")


__all__ = ["STAGE", "call_uc03_document_collection", "collection_request", "notification_key"]
