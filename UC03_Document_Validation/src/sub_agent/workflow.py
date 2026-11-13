from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from src.state.enums import ComplianceStatus, RequiredAction, ValidationFinding, VisaPackStatus
from src.state.store import AuditStore, CaseStore
from src.sub_agent.checklist import ChecklistService
from src.sub_agent.extraction import DocumentExtractor, MockDocumentExtractor
from src.sub_agent.models import (
    DocumentValidationRequest,
    DocumentValidationResult,
)
from src.sub_agent.rules import (
    check_onboarding_start_conditions,
    determine_next_action,
    determine_overall_status,
)
from src.sub_agent.validator import DocumentValidationService


class UC03State(TypedDict):
    request: DocumentValidationRequest
    result: DocumentValidationResult | None
    on_hold: bool
    injection_detected: bool
    visa_required: bool
    next_action: str
    error: str | None


def _node_verify_start_conditions(state: UC03State) -> UC03State:
    req = state["request"]
    on_hold = not check_onboarding_start_conditions(
        offer_accepted=req.offer_accepted,
        onboarding_form_approved=req.onboarding_form_approved,
    )
    return {**state, "on_hold": on_hold}


def _route_after_start_check(state: UC03State) -> str:
    return "on_hold" if state["on_hold"] else "run_validation"


def _node_on_hold(state: UC03State) -> UC03State:
    return {**state, "next_action": RequiredAction.NONE.value}


def _node_run_validation(
    case_store: CaseStore,
    audit_store: AuditStore,
    checklist_service: ChecklistService,
    extractor: DocumentExtractor,
    advisor: Any = None,
    result_store: Any = None,
) -> Any:
    async def _run(state: UC03State) -> UC03State:
        service = DocumentValidationService(
            case_store=case_store,
            audit_store=audit_store,
            checklist_service=checklist_service,
            extractor=extractor,
            advisor=advisor,
            result_store=result_store,
        )
        result = await service.validate(state["request"])
        injection = result.injection_detected
        next_act = result.required_next_action.value
        return {
            **state,
            "result": result,
            "injection_detected": injection,
            "visa_required": state["request"].visa_required,
            "next_action": next_act,
        }

    return _run


def _route_after_validation(state: UC03State) -> str:
    if state.get("injection_detected"):
        return "flag_injection"
    result = state.get("result")
    if result and result.visa_pack_status in (
        VisaPackStatus.OUTSTANDING_ITEMS,
        VisaPackStatus.READY_FOR_PRO,
    ):
        return "prepare_visa_pack"
    if result and result.required_next_action in (
        RequiredAction.HR_REVIEW,
        RequiredAction.SEND_REMINDER,
        RequiredAction.SEND_REPLACEMENT_REQUEST,
    ):
        return "route_to_hr"
    return "finalize"


def _node_flag_injection(state: UC03State) -> UC03State:
    return {**state, "next_action": RequiredAction.FLAG_INJECTION.value}


def _node_route_to_hr(state: UC03State) -> UC03State:
    return {**state, "next_action": RequiredAction.HR_REVIEW.value}


def _node_prepare_visa_pack(state: UC03State) -> UC03State:
    return {**state, "next_action": RequiredAction.PREPARE_VISA_PACK.value}


def _node_finalize(state: UC03State) -> UC03State:
    return state


def build_uc03_graph(
    case_store: CaseStore,
    audit_store: AuditStore,
    checklist_service: ChecklistService | None = None,
    extractor: DocumentExtractor | None = None,
    advisor: Any = None,
    result_store: Any = None,
) -> Any:
    """Build and compile the UC-03 LangGraph StateGraph.

    `advisor` (optional, a `UC03AdvisorPort`) adds model wording for HR draft messages and advisory notes on name variations already
    routed to HR. The orchestrator passes one built over its shared `LLMClient` (see integrations/gateway). Default None: deterministic.
    `result_store` (optional, a `ValidationResultStore`) stores each validation result, e.g. in
    Supabase (integrations/supabase).
    """
    if checklist_service is None:
        checklist_service = ChecklistService()
    if extractor is None:
        extractor = MockDocumentExtractor()

    graph: StateGraph = StateGraph(UC03State)

    graph.add_node("verify_start_conditions", _node_verify_start_conditions)
    graph.add_node("on_hold", _node_on_hold)
    graph.add_node(
        "run_validation",
        _node_run_validation(
            case_store, audit_store, checklist_service, extractor, advisor, result_store
        ),
    )
    graph.add_node("flag_injection", _node_flag_injection)
    graph.add_node("route_to_hr", _node_route_to_hr)
    graph.add_node("prepare_visa_pack", _node_prepare_visa_pack)
    graph.add_node("finalize", _node_finalize)

    graph.set_entry_point("verify_start_conditions")

    graph.add_conditional_edges(
        "verify_start_conditions",
        _route_after_start_check,
        {"on_hold": "on_hold", "run_validation": "run_validation"},
    )
    graph.add_edge("on_hold", END)

    graph.add_conditional_edges(
        "run_validation",
        _route_after_validation,
        {
            "flag_injection": "flag_injection",
            "prepare_visa_pack": "prepare_visa_pack",
            "route_to_hr": "route_to_hr",
            "finalize": "finalize",
        },
    )
    graph.add_edge("flag_injection", "finalize")
    graph.add_edge("route_to_hr", "finalize")
    graph.add_edge("prepare_visa_pack", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()
