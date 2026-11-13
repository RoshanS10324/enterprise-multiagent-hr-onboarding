from __future__ import annotations

from src.state.audit import audit_event
from src.state.enums import ActorType
from src.state.store import AuditStore


async def before_tool_call(
    store: AuditStore,
    case_id: str,
    tool_name: str,
    actor_id: str,
    actor_type: ActorType,
    params: dict,
) -> None:
    await audit_event(
        store=store,
        case_id=case_id,
        actor_id=actor_id,
        actor_type=actor_type,
        action="TOOL_CALL_STARTED",
        detail={"tool": tool_name, "params_keys": list(params.keys())},
    )


async def after_tool_call(
    store: AuditStore,
    case_id: str,
    tool_name: str,
    actor_id: str,
    actor_type: ActorType,
    result_summary: str,
) -> None:
    await audit_event(
        store=store,
        case_id=case_id,
        actor_id=actor_id,
        actor_type=actor_type,
        action="TOOL_CALL_COMPLETED",
        detail={"tool": tool_name, "result_summary": result_summary},
    )
