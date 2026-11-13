"""CaseRecord to and from plain JSON, for the table-backed state backend. Only codes, ids, dates and counts are stored (the same content
the in-memory aggregate holds); the privacy guard has already run on every result before it is stored."""

from typing import Any

from sub_agent.contracts import Uc04Result
from sub_agent.states import ItemKind, ItemState
from sub_agent.store import CaseRecord, ItemRecord


def item_to_json(item: ItemRecord) -> dict[str, Any]:
    return {"request_id": item.request_id, "request_key": item.request_key, "item_kind": item.item_kind.value, "item_id": item.item_id,
            "state": item.state.value, "ticket_id": item.ticket_id, "history": [list(h) for h in item.history], "client_site": item.client_site,
            "submission_attempts": item.submission_attempts}


def item_from_json(data: dict[str, Any]) -> ItemRecord:
    return ItemRecord(request_id=data["request_id"], request_key=data["request_key"], item_kind=ItemKind(data["item_kind"]), item_id=data["item_id"],
                      state=ItemState(data["state"]), ticket_id=data.get("ticket_id"), history=[tuple(h) for h in data.get("history", [])],
                      client_site=bool(data.get("client_site", False)), submission_attempts=int(data.get("submission_attempts", 0)))


def record_to_json(record: CaseRecord) -> dict[str, Any]:
    """Everything except the replay results, which have their own table."""
    return {"fingerprint": record.fingerprint, "items": {k: item_to_json(v) for k, v in record.items.items()},
            "adapter_errors": dict(record.adapter_errors), "archived": {k: item_to_json(v) for k, v in record.archived.items()}}


def record_from_json(case_id: str, employee_ref: str, client_id: str, data: dict[str, Any]) -> CaseRecord:
    return CaseRecord(case_id=case_id, employee_ref=employee_ref, client_id=client_id, fingerprint=dict(data.get("fingerprint", {})),
                      items={k: item_from_json(v) for k, v in data.get("items", {}).items()},
                      adapter_errors=dict(data.get("adapter_errors", {})),
                      archived={k: item_from_json(v) for k, v in data.get("archived", {}).items()})


def result_to_row(case_id: str, key: str, entry: tuple[str, int, str, Uc04Result]) -> dict[str, Any]:
    payload_hash, store_version, parent_status, result = entry
    return {"case_id": case_id, "idempotency_key": key, "payload_hash": payload_hash, "store_version": store_version,
            "parent_status": parent_status, "result": result.model_dump(mode="json")}


def result_from_row(row: dict[str, Any]) -> tuple[str, tuple[str, int, str, Uc04Result]]:
    return row["idempotency_key"], (row["payload_hash"], int(row["store_version"]), row["parent_status"], Uc04Result.model_validate(row["result"]))
