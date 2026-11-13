from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from src.state.enums import ActorType
from src.state.models import AuditEvent
from src.state.store import AuditStore


async def audit_event(
    store: AuditStore,
    case_id: str,
    actor_id: str,
    actor_type: ActorType,
    action: str,
    detail: dict[str, Any],
    correlation_id: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        event_id=str(uuid.uuid4()),
        case_id=case_id,
        actor_id=actor_id,
        actor_type=actor_type,
        action=action,
        detail=detail,
        timestamp=datetime.now(UTC),
        correlation_id=correlation_id,
    )
    await store.log(event)
    return event
