"""`DatabaseAuditSink`: UC-04's audit entries written to `audit_events` (append-only; one row per entry).

The entry is exactly what `AuditLog` already validated (fixed fields, sanitised text, no personal data). Only the shape changes: UC-04's
fields go into the table's columns, and the few fields the table has no column for go into the jsonb `detail` as a small bag of codes and
ids (never free text beyond the already-sanitised reason).

What the table requires and UC-04's entry does not carry comes from the per-call context: `client_id` (the verified parent's client) and the
actor. An event with no client (it happened before the case could be resolved) is refused unless an explicit stand-in client is configured:
that choice is PENDING with the team, so by default the sink fails closed instead of inventing one.
"""

from collections.abc import Callable, Mapping
from typing import Any

from sub_agent.persistence.errors import PersistenceError
from sub_agent.persistence.mappers import AUDIT_TABLE, audit_actor_type, audit_outcome, is_uuid
from sub_agent.persistence.table_client import TableClient

_DETAIL_KEYS = ("rule_id", "reason", "next_action", "owner")


class DatabaseAuditSink:
    def __init__(self, client: TableClient, *, unresolved_client_id: str | None = None,
                 actor_type_for: Callable[[Mapping[str, Any]], str] = audit_actor_type):
        self._client = client
        self._unresolved_client_id = unresolved_client_id
        self._actor_type_for = actor_type_for

    def row(self, entry: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
        case_id = entry.get("case_id")
        if case_id is not None and not is_uuid(case_id):
            raise PersistenceError("audit case id is not a uuid: the shared table needs the PLATFORM identifier profile")
        client_id = context.get("client_id") or self._unresolved_client_id
        if not client_id:
            raise PersistenceError("audit event has no client: refused (no stand-in client is configured)")
        actor_id = context.get("actor_id")
        if not actor_id:
            raise PersistenceError("audit event has no actor: refused")
        detail: dict[str, Any] = {k: entry[k] for k in _DETAIL_KEYS if entry.get(k) is not None}
        if context.get("invocation_mode"):
            detail["invocation_mode"] = context["invocation_mode"]
        row: dict[str, Any] = {
            "case_id": case_id, "client_id": client_id, "actor_type": self._actor_type_for(context), "actor_id": actor_id,
            "action": entry["event_type"], "outcome": audit_outcome(entry["event_type"]), "detail": detail,
            "before_state": {"status": entry["previous_status"]} if entry.get("previous_status") is not None else None,
            "after_state": {"status": entry["new_status"]} if entry.get("new_status") is not None else None,
            "correlation_id": context.get("run_id"), "reference": entry.get("request_id"), "occurred_at": entry["timestamp"],
        }
        return {k: v for k, v in row.items() if v is not None}

    def write(self, entry: Mapping[str, Any], context: Mapping[str, Any]) -> None:
        row = self.row(entry, context)
        try:
            self._client.insert(AUDIT_TABLE, row)
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(f"audit write failed ({type(exc).__name__})") from None
