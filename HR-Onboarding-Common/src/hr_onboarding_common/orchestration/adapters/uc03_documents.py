"""Stage adapter: UC03 Document Validation (`build_uc03_graph` / `DocumentValidationService`).

Real invocation: UC03's own compiled LangGraph (`build_uc03_graph(...)`), ASYNC (`graph.
ainvoke(...)`), run as a subgraph node per required document type. In-process (no module-name
collision -- its installed top-level name is "src", not "sub_agent" -- but its own code imports
a sibling `guardrails`/`knowledge` package that its own wheel does not bundle; its own tests only
work via `pythonpath=["."]` at its repo root. This adapter replicates that by inserting UC03's
repo root onto `sys.path` once, exactly as its tests do -- zero UC03 source changes).

What UC03's contract means for routing (read from UC03's validator and rules, not assumed):

  - UC03 never assigns document_status=ACCEPTED. A valid document stays UNDER_REVIEW; it is
    verified when compliance_item_status is COMPLETE, which UC03 grants only with no blocking
    finding, stored evidence (where required) and hr_review_status=APPROVED. hr_review_status is an
    input the caller supplies: here it is the recorded decision of an authenticated HR reviewer
    (OnboardingOrchestratorAgent.review_documents), never the orchestrator's own.
  - UC03 records each document revision as received on its case (`document_revision`): the same or an
    older revision again is a DUPLICATE_REQUEST, a newer (corrected) revision is validated again. Each
    revision is sent to UC03 once: an unchanged revision reuses its earlier result, and an HR decision
    on a revision UC03 already validated is sent as UC03's review call (submission_status UNDER_REVIEW +
    hr_review_status).
  - UC03 validates exactly what the current UC02 plan version requires (document_collection, opened by
    the document_collection stage for that version) and reports a DocumentValidationFeedback for that
    version: what is verified, missing, invalid, expired, awaiting HR review -- and whether the plan
    must change (`replanning_required`): a mandatory document of UC03's catalog the plan does not list
    (`requirement_gaps_for`, e.g. visa documents once a passport shows a visa is needed), or an HR
    requirement change. Replanning takes precedence: the stage is BLOCKED with REPLANNING_REQUIRED and
    the graph routes to UC02's replan_tasks; corrections are kept and asked for under the new plan.
  - Each document is classified from UC03's enums (compliance_item_status, required_next_action);
    free text is never parsed. A UC03 call that raises is a technical failure (RETRY_PENDING), never
    an invalid document.

Verified: UC03 enforces caller identity at this entry point itself (`assert_case_access` against
`case.authorized_actor_ids`) -- no orchestrator-side boundary check is layered on top; this
adapter must add the calling actor to `authorized_actor_ids` when it creates the case, and never
on behalf of a different actor than the one invoking it.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from hr_onboarding_common.orchestration.adapters._shared import add_blocker, record_stage_error, with_stage_status
from hr_onboarding_common.orchestration.deps import OrchestratorDeps
from hr_onboarding_common.orchestration.state import (REPLANNING_WAIT_CODE, OnboardingCaseState, Stage,
                                                      StageStatus)
from hr_onboarding_common.orchestration.workflow_models import (CorrectionRequirement, DocumentValidationFeedback,
                                                                RecommendedTaskChange, ValidationIssue)

STAGE = Stage.VALIDATE_DOCUMENTS
TECHNICAL_ERROR_CODE = "UC03_TECHNICAL_ERROR"
# UC03 catalog flags established by a document -> the UC02 applicability attribute that switches on the
# plan's tasks for it (UC02's synthetic configuration: visa_required -> medical, biometrics, visa tasks).
FLAG_TO_UC02_ATTRIBUTE = {"visa_required": "visa_required"}

# UC03's RequiredAction -> what the workflow needs next for that document.
_CLASS_BY_ACTION = {
    "SEND_REMINDER": "missing",
    "SEND_REPLACEMENT_REQUEST": "correction_required",
    "FLAG_INJECTION": "correction_required",
    "HUMAN_CONFIRMATION": "hr_review_required",
    "HR_REVIEW": "hr_review_required",
}
# classification -> (CorrectionRequirement.action, stage status it implies)
_REQUIREMENT = {
    "missing": ("supply", StageStatus.WAITING_FOR_INPUT),
    "correction_required": ("resubmit", StageStatus.WAITING_FOR_INPUT),
    "hr_review_rejected": ("resubmit", StageStatus.WAITING_FOR_INPUT),
    "hr_review_required": ("hr_review", StageStatus.AWAITING_APPROVAL),
    "blocked": ("resolve", StageStatus.BLOCKED),
}
# The metadata fields of a submitted document that UC03's DocumentValidationRequest takes.
METADATA_FIELDS = ("readable", "required_fields_present", "expiry_date", "evidence_stored", "extracted_name",
                   "extraction_confidence", "attestation_confirmed", "visa_required", "sponsorship_applicable")


def _ensure_uc03_importable(uc03_repo_root: Path) -> None:
    root_str = str(uc03_repo_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def _uc03_module(name: str, deps: OrchestratorDeps):
    """Import a UC03 integration module and prove it came from UC03's own repo root (the top-level name
    `integrations` is generic, so another package of that name on sys.path must never be picked up)."""
    import importlib

    module = importlib.import_module(name)
    origin = Path(getattr(module, "__file__", "") or "").resolve()
    if Path(deps.uc03_repo_root).resolve() not in origin.parents:
        raise ImportError(f"{name} was loaded from outside UC03's repo root")
    return module


def _uc03_advisor(deps: OrchestratorDeps):
    """UC03's advisor over the orchestrator's OWN shared LLM client (AgentContext.llm, chosen by build_llm from
    LLM_MODE). UC03 never builds a client or reads a key. Advisory only: UC03's deterministic rules decide, and
    no document text is sent to a model (name checks use placeholder tokens; drafts use a name placeholder).

    Offline, AgentContext.llm is the shared FakeLLM, which has no handlers for UC03's tasks; UC03's offline handlers are
    added to it (only for task names it does not handle yet), so offline runs exercise the same gateway path."""
    llm = getattr(getattr(deps, "ctx", None), "llm", None)
    if llm is None:
        return None
    gateway = _uc03_module("integrations.gateway", deps)
    handlers = getattr(llm, "handlers", None)
    if isinstance(handlers, dict):  # a FakeLLM (offline mode)
        for task, handler in gateway.offline_llm().handlers.items():
            handlers.setdefault(task, handler)
    return gateway.GatewayAdvisor(llm)


def _build_uc03_runtime(deps: OrchestratorDeps):
    if deps._uc03_graph is not None:
        return deps._uc03_graph, deps._uc03_case_store, deps._uc03_audit_store, deps._uc03_checklist_service

    _ensure_uc03_importable(deps.uc03_repo_root)
    from src.sub_agent.checklist import ChecklistService  # type: ignore
    from src.sub_agent.extraction import MockDocumentExtractor  # type: ignore
    from src.sub_agent.workflow import build_uc03_graph  # type: ignore

    # UC03's own store selection (UC03_STORE_MODE): in-memory by default, Supabase-backed when configured. A bad
    # configuration raises here, before any document is validated; there is no silent fall back to memory.
    stores = _uc03_module("integrations.supabase", deps).build_stores()
    case_store, audit_store = stores.case_store, stores.audit_store
    checklist_service = ChecklistService()
    graph = build_uc03_graph(case_store, audit_store, checklist_service, MockDocumentExtractor(),
                             advisor=_uc03_advisor(deps), result_store=stores.result_store)
    # UC03's graph runs inside an orchestrator node, where LangGraph would lend it the orchestrator's
    # checkpointer as a subgraph. A persisted orchestrator uses the synchronous SqliteSaver, whose async
    # methods raise NotImplementedError, so every validation failed as UC03_TECHNICAL_ERROR. UC03 keeps no
    # checkpoint of its own (its results are folded into the orchestrator's state): never checkpointed.
    graph.checkpointer = False

    deps._uc03_graph = graph
    deps._uc03_case_store = case_store
    deps._uc03_audit_store = audit_store
    deps._uc03_checklist_service = checklist_service
    return graph, case_store, audit_store, checklist_service


def uc03_mode(deps: OrchestratorDeps) -> str:
    """"memory" or "supabase": the deps' setting, else UC03_STORE_MODE from the environment (default memory)."""
    from hr_onboarding_common.orchestration.uc03_supabase import uc03_store_mode

    configured = getattr(deps, "uc03_store_mode", None)  # an older or stand-in deps object has none: the environment
    return uc03_store_mode({"UC03_STORE_MODE": configured} if configured else os.environ)


def uc03_tables(deps: OrchestratorDeps):
    """The one table client UC03's Supabase stores and its document registry share (built once)."""
    if getattr(deps, "_uc03_tables", None) is None:
        if getattr(deps, "uc03_tables_factory", None) is not None:
            deps._uc03_tables = deps.uc03_tables_factory()
        else:
            from hr_onboarding_common.orchestration.uc03_supabase import Uc03PostgrestTables

            errors = _uc03_module("integrations.supabase.tables", deps)
            deps._uc03_tables = Uc03PostgrestTables.from_env(os.environ, duplicate_error=errors.DuplicateKey,
                                                             storage_error=errors.StorageError)
    return deps._uc03_tables


def collection_service(deps: OrchestratorDeps):
    """UC03's DocumentCollectionService (collection per plan version, stored revisions), built once per
    orchestrator instance on UC03's own checklist. Its registry: UC03's `uc03_case_state` table in Supabase mode
    (UC03 integrations/supabase/collection_registry.py); else a SQLite file when `deps.uc03_document_store_path` is
    set (survives a restart), in memory otherwise."""
    if deps._uc03_collection_service is not None:
        return deps._uc03_collection_service
    checklist_service = _build_uc03_runtime(deps)[3]
    from src.sub_agent.collection import (DocumentCollectionService, InMemoryDocumentRegistry,  # type: ignore
                                          SqliteDocumentRegistry)

    if uc03_mode(deps) == "supabase":
        registry = _uc03_module("integrations.supabase.collection_registry", deps).SupabaseCollectionRegistry(
            uc03_tables(deps))
    elif deps.uc03_document_store_path:
        registry = SqliteDocumentRegistry(deps.uc03_document_store_path)
    else:
        registry = InMemoryDocumentRegistry()
    deps._uc03_collection_service = DocumentCollectionService(registry, checklist_service)
    return deps._uc03_collection_service


def _uc03_module(name: str, deps: OrchestratorDeps):
    """Import a UC03 integration module and prove it came from UC03's own repo root (the top-level name
    `integrations` is generic, so another package of that name on sys.path must never be picked up)."""
    import importlib

    module = importlib.import_module(name)
    origin = Path(getattr(module, "__file__", "") or "").resolve()
    if Path(deps.uc03_repo_root).resolve() not in origin.parents:
        raise ImportError(f"{name} was loaded from outside UC03's repo root")
    return module


def checklist_context(state: OnboardingCaseState) -> dict[str, Any]:
    role_ctx = state.get("uc02_role_context", {})
    return {"role": role_ctx.get("role_id", ""), "grade": role_ctx.get("grade", ""),
            "nationality": role_ctx.get("nationality", ""), "location": role_ctx.get("location", "")}


def applicable_documents(state: OnboardingCaseState, deps: OrchestratorDeps) -> list[Any]:
    """UC03's own document requirements for this case (its ChecklistService; read-only)."""
    checklist_service = _build_uc03_runtime(deps)[3]
    return checklist_service.get_applicable_documents(checklist_context(state))


def document_catalog(state: OnboardingCaseState, deps: OrchestratorDeps) -> list[Any]:
    """UC03's catalog of validatable documents applicable to this employee (RequirementGap: type, label,
    mandatory, reason, validation rules) -- what UC02's initial plan picks its document requirements
    from. UC03 owns the catalog and its validation rules; UC02 owns which documents the plan requires."""
    return collection_service(deps).catalog(checklist_context(state))


def requirement_gaps_for(state: OnboardingCaseState, deps: OrchestratorDeps
                         ) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    """(gaps, flags): the mandatory documents UC03's catalog requires for this employee -- given what
    their documents show -- that the current plan does not list (UC03's
    DocumentCollectionService.requirement_gaps), and the flags their documents establish."""
    service = collection_service(deps)
    plan = state.get("onboarding_plan") or {}
    flags = service.derived_context(submitted_documents(state).values())
    if not plan:  # a case collected before UC02 owned the plan: nothing to compare against, nothing to replan
        return [], flags
    planned = [d["document_type"] for d in plan.get("required_documents") or []]
    gaps = service.requirement_gaps({**checklist_context(state), **flags}, [*planned, *waived_documents(state)])
    return [g.model_dump(mode="json") for g in gaps], flags


def submitted_documents(state: OnboardingCaseState) -> dict[str, dict[str, Any]]:
    """The submitted documents (SubmittedDocument shape). A state from before document submission
    existed carried them in intake_trigger["documents"]; those still validate as revision 1."""
    if state.get("submitted_documents"):
        return state["submitted_documents"]
    legacy = (state.get("intake_trigger") or {}).get("documents") or {}
    return {doc_type: {"document_type": doc_type, "document_ref": doc.get("document_ref") or "", "revision": 1,
                       "metadata": {k: doc[k] for k in METADATA_FIELDS if k in doc}}
            for doc_type, doc in legacy.items() if doc}


class Uc03TenantConflict(Exception):
    """UC03's stored case belongs to another tenant than the case being validated: refused, never re-used."""


async def _ensure_case_registered(case_store, state: OnboardingCaseState) -> None:
    from src.state.models import OnboardingCase  # type: ignore

    existing = await case_store.get_case(state["case_id"])
    actor_id = state["actor"]["on_behalf_of"]
    if existing is not None and existing.client_id != state["client_id"]:
        raise Uc03TenantConflict(f"UC03's stored case {state['case_id']} belongs to another tenant")
    if existing is None:
        case = OnboardingCase(
            case_id=state["case_id"], employee_id=state["employee_id"], client_id=state["client_id"],
            role=state.get("uc02_role_context", {}).get("role_id", ""), grade=state.get("uc02_role_context", {}).get("grade", ""),
            nationality=state.get("uc02_role_context", {}).get("nationality", ""),
            location=state.get("uc02_role_context", {}).get("location", ""),
            offer_accepted=True, onboarding_form_approved=True,
            employee_name_form=state["intake_trigger"].get("candidate_name", ""),
            authorized_actor_ids={actor_id},
        )
        await case_store.save_case(case)
    elif actor_id not in existing.authorized_actor_ids:
        existing.authorized_actor_ids.add(actor_id)
        await case_store.save_case(existing)


def _classify(result: dict[str, Any]) -> str:
    if result.get("compliance_item_status") == "COMPLETE":
        return "verified"
    if result.get("document_status") == "ON_HOLD":  # UC03's start conditions are not met
        return "blocked"
    action = result.get("required_next_action")
    if action == "NONE":  # not complete and nothing UC03 asks for: evidence or approval still missing
        return "correction_required"
    return _CLASS_BY_ACTION.get(action, "blocked")


def _project(result: Any, *, revision: int | None, review: dict[str, Any] | None) -> dict[str, Any]:
    """Reference and statuses only -- never extracted values (see state.py module docstring). UC03's
    `detail` carries the document type, its status and finding codes, no document values."""
    r = result.model_dump(mode="json") if result is not None else {
        "document_status": "ON_HOLD", "detail": "start conditions not met"}
    projection = {
        "document_status": r.get("document_status"),
        "compliance_item_status": r.get("compliance_item_status"),
        "hr_decision": r.get("hr_decision"),
        "evidence_status": r.get("evidence_status"),
        "findings": list(r.get("validation_findings") or []),
        "required_next_action": r.get("required_next_action"),
        "audit_reference": r.get("audit_reference"),
        "detail": r.get("detail", ""),
        # Whether UC03 stored this result (PERSISTED / NOT_PERSISTED:<reason> / NOT_CONFIGURED in memory mode).
        "persistence_status": r.get("persistence_status", "NOT_CONFIGURED"),
        "revision": revision,
        "review_decision": (review or {}).get("decision"),
    }
    projection["classification"] = _classify(projection)
    return projection


def _requirements(state: OnboardingCaseState, checklist_service) -> list[tuple[str, bool]]:
    """(document type, mandatory) of what the current plan version requires -- the collection UC03 opened
    for it. A case from before the plan-driven collection falls back to UC03's checklist."""
    collection = state.get("document_collection") or {}
    if collection.get("plan_version") is not None:
        return [(d["document_type"], bool(d.get("required", True))) for d in collection.get("required_documents", [])]
    return [(r.id, r.required) for r in checklist_service.get_applicable_documents(checklist_context(state))]


async def _validate_all(state: OnboardingCaseState, deps: OrchestratorDeps) -> dict[str, dict[str, Any]]:
    _ensure_uc03_importable(deps.uc03_repo_root)  # must precede any `src.*` import below
    from src.state.enums import ActorType, HRReviewStatus, SubmissionStatus  # type: ignore
    from src.sub_agent.models import DocumentValidationRequest  # type: ignore

    graph, case_store, audit_store, checklist_service = _build_uc03_runtime(deps)
    await _ensure_case_registered(case_store, state)

    role_ctx = state.get("uc02_role_context", {})
    submitted = submitted_documents(state)
    previous = state.get("document_validation_result") or {}
    plan_version = ((state.get("document_collection") or {}).get("plan_version"))
    # What the employee's documents establish (UC03's own derivation): UC03's validator applies its
    # catalog in the same context the collection was planned in (e.g. visa documents once a visa is needed).
    flags = collection_service(deps).derived_context(submitted.values())

    results: dict[str, dict[str, Any]] = {}
    for doc_type, mandatory in _requirements(state, checklist_service):
        doc = submitted.get(doc_type)
        if doc is None and not mandatory:
            continue  # an optional document nobody supplied
        revision = doc["revision"] if doc else None
        review = (doc or {}).get("hr_review")
        review = review if review and review.get("revision") == revision else None
        prev = previous.get(doc_type) or {}
        same_revision = doc is not None and prev.get("revision") == revision
        if same_revision and prev.get("review_decision") == (review or {}).get("decision"):
            results[doc_type] = prev  # unchanged: never sent to UC03 twice (UC03 would flag a duplicate)
            continue
        if review and review["decision"] == "rejected":
            # The HR reviewer's decision, recorded as such; a new revision is required.
            results[doc_type] = {**prev, "revision": revision, "review_decision": "rejected",
                                 "classification": "hr_review_rejected"}
            continue
        meta = (doc or {}).get("metadata") or {}
        request = DocumentValidationRequest(
            case_id=state["case_id"], employee_id=state["employee_id"],
            employee_name_form=state["intake_trigger"].get("candidate_name", ""),
            offer_accepted=True, onboarding_form_approved=True, document_type=doc_type,
            submission_status=(SubmissionStatus.UNDER_REVIEW if same_revision and review else
                               SubmissionStatus.RECEIVED if doc else SubmissionStatus.MISSING),
            hr_review_status=HRReviewStatus.APPROVED if review else HRReviewStatus.PENDING,
            extracted_name=meta.get("extracted_name"), expiry_date=meta.get("expiry_date"),
            readable=meta.get("readable"), required_fields_present=meta.get("required_fields_present"),
            extraction_confidence=meta.get("extraction_confidence", 1.0),
            attestation_confirmed=meta.get("attestation_confirmed", False),
            evidence_stored=meta.get("evidence_stored", False),
            document_ref=(doc or {}).get("document_ref") or None, client_id=state["client_id"],
            role=role_ctx.get("role_id", ""), grade=role_ctx.get("grade", ""),
            nationality=role_ctx.get("nationality", ""), location=role_ctx.get("location", ""),
            visa_required=bool(flags.get("visa_required")), sponsorship_applicable=bool(flags.get("sponsorship")),
            requesting_actor_id=state["actor"]["on_behalf_of"], requesting_actor_type=ActorType.SYSTEM,
            correlation_id=state["correlation_id"], document_revision=revision,
        )
        graph_state = {"request": request, "result": None, "on_hold": False, "injection_detected": False,
                       "visa_required": False, "next_action": "", "error": None}
        outcome = await graph.ainvoke(graph_state)
        results[doc_type] = {**_project(outcome["result"], revision=revision, review=review),
                             "plan_version": plan_version}
    return results


def call_uc03_validate_documents(state: OnboardingCaseState, deps: OrchestratorDeps) -> OnboardingCaseState:
    """UC03 runs in-process; its graph is async, so a run that exceeds `deps.uc03_timeout_s` is
    cancelled at its next await and reported like any technical failure (RETRY_PENDING). A revision
    UC03 had already recorded before the timeout comes back from UC03 as a duplicate needing HR review
    on the retry -- never as a verified document."""
    try:
        results = asyncio.run(asyncio.wait_for(_validate_all(state, deps), timeout=deps.uc03_timeout_s))
        gaps, flags = requirement_gaps_for(state, deps)
    except Uc03TenantConflict as exc:
        return tenant_conflict(state, exc)
    except Exception as exc:  # noqa: BLE001 -- a technical failure (TimeoutError included), never a document verdict
        return validation_failed(state, exc)
    new_state = apply_validation_results(state, results, requirement_gaps=gaps, flags=flags)
    if getattr(deps, "_uc03_resolved_mode", None) == "supabase":
        new_state = require_persisted_results(new_state, results)
    return new_state


# NOT_PERSISTED:<reason> from UC03's result store -> (owner, what resolves it). In Supabase mode the authoritative
# record of a submitted document's validation is its `document_validations` row: a result that could not be stored
# blocks the stage (never a silent success). A MISSING document has no file and so no row -- not a gap.
NOT_PERSISTED_CODE = "UC03_RESULT_NOT_PERSISTED"
_NOT_PERSISTED_FIX = {
    "CASE_NOT_AN_ONBOARDING_CASE": ("ops", "UC01 must store the case in onboarding_cases (CASE_NOT_IN_DATABASE)"),
    "DOCUMENT_TYPE_NOT_IN_SCHEMA": ("ops", "The schema owner widens the candidate_documents.document_type CHECK for "
                                           "this document type (docs/SUPABASE_UC_TABLE_OWNERSHIP.md, section 8)"),
    "DOCUMENT_NOT_REGISTERED": ("hr_admin", "Upload the file through the document upload (Supabase Storage + "
                                            "candidate_documents); a reference alone cannot be stored"),
}


def require_persisted_results(state: OnboardingCaseState, results: dict[str, dict[str, Any]]) -> OnboardingCaseState:
    submitted = submitted_documents(state)
    unpersisted = {t: str(r.get("persistence_status") or "") for t, r in results.items()
                   if t in submitted and str(r.get("persistence_status") or "").startswith("NOT_PERSISTED")}
    if not unpersisted:
        return state
    new_state = with_stage_status(state, STAGE, StageStatus.BLOCKED)
    record_stage_error(new_state, STAGE, code=NOT_PERSISTED_CODE, retryable=True,
                       detail="validation result(s) not stored in Supabase: "
                              + ", ".join(f"{t} ({s.partition(':')[2] or 'unknown'})" for t, s in sorted(unpersisted.items())))
    new_state["next_action"] = "resolve:validate_documents:not_persisted"
    for doc_type, status in sorted(unpersisted.items()):
        reason = status.partition(":")[2] or "UNKNOWN"
        owner, fix = _NOT_PERSISTED_FIX.get(reason, ("ops", "Investigate why UC03 could not store the result"))
        add_blocker(new_state, stage=STAGE, code=NOT_PERSISTED_CODE, reason=f"{doc_type}: {reason}", owner=owner,
                    next_action=fix)
    return new_state


def tenant_conflict(state: OnboardingCaseState, exc: Exception) -> OnboardingCaseState:
    """UC03's stored case is another tenant's: FAILED with TENANT_CONTEXT_CONFLICT (never retried into success)."""
    new_state = with_stage_status(state, STAGE, StageStatus.FAILED)
    record_stage_error(new_state, STAGE, code="TENANT_CONTEXT_CONFLICT", retryable=False, detail=str(exc))
    new_state["next_action"] = "investigate:validate_documents:tenant_conflict"
    add_blocker(new_state, stage=STAGE, code="TENANT_CONTEXT_CONFLICT", reason=str(exc), owner="ops",
                next_action="Investigate the case's tenant: UC03 refuses another tenant's stored case")
    return new_state


def validation_failed(state: OnboardingCaseState, exc: Exception) -> OnboardingCaseState:
    """UC03 could not run: RETRY_PENDING with UC03_TECHNICAL_ERROR. Earlier per-document results and
    correction requirements are kept as they were. Only the exception type is recorded: a
    request-validation message could echo submitted metadata."""
    new_state = with_stage_status(state, STAGE, StageStatus.RETRY_PENDING)
    record_stage_error(new_state, STAGE, code=TECHNICAL_ERROR_CODE, retryable=True,
                       detail=f"UC03 could not complete validation ({type(exc).__name__}); no document "
                              f"was judged -- retry the stage")
    new_state["next_action"] = "retry:validate_documents:technical_error"
    add_blocker(new_state, stage=STAGE, code=TECHNICAL_ERROR_CODE, reason=new_state["error_detail"],
                owner="ops", next_action="Retry the validation stage once UC03 is available")
    return new_state


def pending_requirement_changes(state: OnboardingCaseState) -> list[dict[str, Any]]:
    return [r for r in state.get("requirement_change_requests") or [] if r.get("status") == "pending"]


def waived_documents(state: OnboardingCaseState) -> set[str]:
    """Documents HR removed from the requirements (pending or applied): an HR decision, which UC03's
    catalog never re-adds as a gap."""
    return {t for r in state.get("requirement_change_requests") or [] for t in r.get("remove_documents") or []}


def build_feedback(state: OnboardingCaseState, results: dict[str, dict[str, Any]], *,
                   requirement_gaps: list[dict[str, Any]] | None = None,
                   flags: dict[str, bool] | None = None) -> dict[str, Any]:
    """UC03 -> UC02: the structured outcome of validating against the current plan version
    (workflow_models.DocumentValidationFeedback). Replanning is required when UC03's catalog requires
    a mandatory document the plan does not list, when the documents establish a UC02 applicability
    attribute the plan does not have yet, or when HR changed the requirements."""
    plan = state.get("onboarding_plan") or {}
    planned = {d["document_type"] for d in plan.get("required_documents") or []}
    changes: list[RecommendedTaskChange] = []
    reasons: list[str] = []
    for gap in requirement_gaps or []:
        if gap["document_type"] not in planned | waived_documents(state):
            changes.append(RecommendedTaskChange(action="add_document", target=gap["document_type"],
                                                 reason=gap.get("reason") or "NEW_MANDATORY_DOCUMENT"))
            reasons.append(f"NEW_MANDATORY_DOCUMENT:{gap['document_type']}")
    for flag, attribute in FLAG_TO_UC02_ATTRIBUTE.items():
        if plan and (flags or {}).get(flag) and attribute not in (plan.get("attributes") or []):
            changes.append(RecommendedTaskChange(action="add_attribute", target=attribute,
                                                 reason=f"a submitted document establishes {flag}"))
            reasons.append(f"DOCUMENT_DERIVED_ATTRIBUTE:{attribute}")
    change_ids = []
    for request in pending_requirement_changes(state):
        change_ids.append(request["request_id"])
        for doc_type in request.get("add_documents") or []:
            if doc_type not in planned:
                changes.append(RecommendedTaskChange(action="add_document", target=doc_type,
                                                     reason="HR_REQUIREMENT_CHANGE"))
        for doc_type in request.get("remove_documents") or []:
            if doc_type in planned:
                changes.append(RecommendedTaskChange(action="remove_document", target=doc_type,
                                                     reason="HR_REQUIREMENT_CHANGE"))
        reasons.append(f"HR_REQUIREMENT_CHANGE:{request['request_id']}")
    by_class: dict[str, list[str]] = {}
    for doc_type, r in sorted(results.items()):
        by_class.setdefault(r.get("classification", ""), []).append(doc_type)
    expired = sorted(t for t, r in results.items() if "EXPIRED_DOCUMENT" in (r.get("findings") or []))
    invalid = sorted(set(by_class.get("correction_required", [])) | set(by_class.get("hr_review_rejected", [])))
    replanning = bool(changes)
    status = ("replanning_required" if replanning else "blocked" if by_class.get("blocked")
              else "correction_required" if invalid or by_class.get("missing")
              else "hr_review_pending" if by_class.get("hr_review_required")
              else "verified" if results else "awaiting_documents")
    # The version UC03 validated against: the collection it holds open (the plan's, for a legacy case).
    validated_version = (state.get("document_collection") or {}).get("plan_version") or plan.get("plan_version")
    key = json.dumps({"case": state.get("case_id"), "plan_version": validated_version,
                      "changes": sorted((c.action, c.target) for c in changes), "requests": sorted(change_ids)},
                     sort_keys=True)
    return DocumentValidationFeedback(
        feedback_id=f"FB-{hashlib.sha256(key.encode()).hexdigest()[:20]}", case_id=state["case_id"],
        tenant_id=state["client_id"], employee_id=state.get("employee_id"), plan_id=plan.get("plan_id"),
        plan_version=validated_version, validation_status=status,
        verified_documents=by_class.get("verified", []), missing_documents=by_class.get("missing", []),
        invalid_documents=invalid, expired_documents=expired,
        correction_required=bool(invalid or by_class.get("missing")), replanning_required=replanning,
        replanning_reasons=reasons, recommended_task_changes=changes,
        pending_hr_reviews=by_class.get("hr_review_required", []), change_request_ids=change_ids,
        document_revision={t: r.get("revision") for t, r in sorted(results.items())},
    ).model_dump(mode="json")


def mark_replanning_required(state: OnboardingCaseState, stage: Stage, feedback: dict[str, Any]) -> OnboardingCaseState:
    """`stage` (a UC03 stage) waits for UC02's replan: BLOCKED with REPLANNING_REQUIRED."""
    new_state = with_stage_status(state, stage, StageStatus.BLOCKED)
    record_stage_error(new_state, stage, code=REPLANNING_WAIT_CODE, retryable=True,
                       detail=f"plan version {feedback.get('plan_version')} must change: "
                              f"{', '.join(feedback.get('replanning_reasons') or [])}")
    new_state["next_action"] = "replan:replan_tasks"
    return new_state


def apply_validation_results(state: OnboardingCaseState, results: dict[str, dict[str, Any]], *,
                             requirement_gaps: list[dict[str, Any]] | None = None,
                             flags: dict[str, bool] | None = None) -> OnboardingCaseState:
    """The stage outcome from per-document results (`_project` shape) and the feedback to UC02.
    Replanning required (the plan must change): BLOCKED with REPLANNING_REQUIRED -- UC02 replans first.
    Otherwise COMPLETE when every document is verified, else the most pressing of BLOCKED (a UC03
    finding to resolve), WAITING_FOR_INPUT (documents to supply or correct) and AWAITING_APPROVAL (an
    HR review decision), with each document's issues and what it needs recorded."""
    new_state = copy.deepcopy(state)
    submitted = submitted_documents(state)
    new_state["document_validation_result"] = results
    feedback = new_state["document_validation_feedback"] = build_feedback(
        state, results, requirement_gaps=requirement_gaps, flags=flags)
    new_state["evidence_references"] = sorted({d["document_ref"] for t, d in submitted.items()
                                               if t in results and d.get("document_ref")})
    # Optional documents (the plan marks them required: false) are validated when supplied and their findings and
    # corrections are recorded, but only MANDATORY documents decide the gate: an optional document never holds UC04/UC05.
    mandatory = {d["document_type"] for d in (state.get("document_collection") or {}).get("required_documents") or []
                 if d.get("required", True)}
    issues, requirements = [], []
    for doc_type, r in results.items():
        findings = [f for f in r.get("findings") or [] if f != "VALID"]
        if findings:
            issues.append(ValidationIssue(document_type=doc_type, revision=r.get("revision"), findings=findings,
                                          document_status=r.get("document_status"),
                                          compliance_item_status=r.get("compliance_item_status"),
                                          required_next_action=r.get("required_next_action")).model_dump(mode="json"))
        if r["classification"] != "verified":
            action, _ = _REQUIREMENT[r["classification"]]
            requirements.append(CorrectionRequirement(
                document_type=doc_type, revision=r.get("revision"), action=action,
                reason_codes=findings or [r["classification"].upper()],
                detail=(("optional document: " if mandatory and doc_type not in mandatory else "")
                        + (r.get("detail") or r["classification"]))).model_dump(mode="json"))
    new_state["validation_issues"] = issues
    new_state["correction_requirements"] = requirements
    if feedback["replanning_required"]:
        return mark_replanning_required(new_state, STAGE, feedback)
    blocking = [req for req in requirements if not mandatory or req["document_type"] in mandatory]
    if not blocking:
        return with_stage_status(new_state, STAGE, StageStatus.COMPLETE)
    requirements = blocking  # only mandatory documents decide the status below

    statuses = {_REQUIREMENT[results[req["document_type"]]["classification"]][1] for req in requirements}
    status = next(s for s in (StageStatus.BLOCKED, StageStatus.WAITING_FOR_INPUT, StageStatus.AWAITING_APPROVAL)
                  if s in statuses)
    code, next_action = {
        StageStatus.BLOCKED: ("DOCUMENT_VALIDATION_BLOCKED", "hr_review:validate_documents:blocked"),
        StageStatus.WAITING_FOR_INPUT: ("DOCUMENTS_NEED_CORRECTION", "request_or_correct_documents"),
        StageStatus.AWAITING_APPROVAL: ("DOCUMENT_REVIEW_REQUIRED", "hr_review:validate_documents:document_review"),
    }[status]
    outstanding = [req["document_type"] for req in requirements]
    new_state["outstanding_tasks"] = sorted(set(new_state.get("outstanding_tasks", [])) | set(outstanding))
    new_state["next_action"] = next_action
    record_stage_error(new_state, STAGE, code=code, retryable=True,
                       detail=f"{len(outstanding)} document(s) not verified: {', '.join(outstanding)}")
    for req in requirements:
        r = results[req["document_type"]]
        add_blocker(new_state, stage=STAGE, code=str(r.get("document_status") or r["classification"].upper()),
                    reason=f"{req['document_type']}: {req['detail']}", owner="hr_admin",
                    next_action=_BLOCKER_ACTIONS[req["action"]])
    return with_stage_status(new_state, STAGE, status)


_BLOCKER_ACTIONS = {"supply": "Supply the document", "resubmit": "Collect a corrected document",
                    "hr_review": "An HR reviewer decides on the document",
                    "resolve": "Resolve UC03's finding before the document can be verified"}
