"""Loader for the synthetic fixtures in data/. Shared by tests, the demo and the evals so they all use the same inputs.
All values are synthetic and labelled as such; none comes from a real client or employee."""

import itertools
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sub_agent.adapters.case_context_mock import MockCaseContext
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk
from sub_agent.audit import AuditLog
from sub_agent.contracts import Uc04Input
from sub_agent.ports import ParentCase
from sub_agent.service import Uc04Service

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
FIXED_NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
CALLER_ROLES = ["hr_onboarding_officer"]
CALLER_ID = "SYN-USER-100"


def fixed_clock() -> datetime:
    return FIXED_NOW


def load(name: str) -> Any:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def deep_merge(base: dict, patch: dict) -> dict:
    """Dicts merge recursively; any other value (including lists) replaces. A patch value of None sets null."""
    out = json.loads(json.dumps(base))
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def build_input_dict(client: str = "alpha", n: int = 1, **patch: Any) -> dict:
    base = load("case_base.json")
    config = load(f"client_{client}_config.json")
    base.update(case_id=f"SYN-CASE-{n:03d}", employee_ref=f"SYN-EMP-{n:03d}", client_id=config["client_id"], config=config)
    return deep_merge(base, patch)


def build_input(client: str = "alpha", n: int = 1, **patch: Any) -> Uc04Input:
    return Uc04Input.model_validate(build_input_dict(client, n, **patch))


# --------------------------------------------------------------------------- scenario builders (synthetic)
ITEMS = {  # request number -> item id (matches data/case_base.json)
    "101": "email-teams-account", "102": "source-control-access", "201": "laptop-standard", "202": "headset-basic",
}
ALL = ["101", "102", "201", "202"]
ACCESS_KEYS, EQUIPMENT_KEYS = ["101", "102"], ["201", "202"]
_apr = itertools.count(1)


def rid(key: str, n: int = 1) -> str:
    return f"SYN-CASE-{n:03d}:SYN-REQ-{key}"


def make(client: str = "alpha", n: int = 1, config: dict | None = None, **patch: Any) -> dict:
    """Base case with optional whole-block config replacement (deep merge cannot delete keys)."""
    data = build_input_dict(client, n, **patch)
    data["config"].update(config or {})
    return data


def approval(key: str, *, n: int = 1, role: str = "hiring_manager", typ: str = "STANDARD", decision: str = "APPROVED",
             on: str = "2026-10-14", item: str | None = None, client: str = "client_alpha",
             role_id: str = "software_engineer", site: bool = False, approver: str = "SYN-USER-900",
             request_id: str | None = None) -> dict:
    return {"approval_id": f"SYN-APR-{next(_apr):03d}", "request_id": request_id or rid(key, n), "approval_type": typ,
            "approver_id": approver, "approver_role": role, "decision": decision, "decided_on": on,
            "item_id": item or ITEMS[key], "client_id": client, "role_id": role_id, "client_site_required": site}


def approvals(keys, **kw: Any) -> list[dict]:
    return [approval(k, **kw) for k in keys]


def ticket(key: str, status: str = "COMPLETED", *, n: int = 1, reason: str | None = None, on: str = "2026-10-17",
           ticket_id: str | None = None) -> dict:
    out = {"request_id": rid(key, n), "ticket_id": ticket_id or f"SYN-TKT-{ALL.index(key) + 1:04d}", "status": status,
           "updated_on": on}
    if reason is not None:
        out["reason"] = reason
    return out


def provisioning(key: str, status: str = "CONFIRMED_ACTIVE", *, evidence: str | None = None, role: str = "it_service_desk",
                 n: int = 1) -> dict:
    return {"request_id": rid(key, n), "status": status, "evidence_ref": evidence or f"SYN-EVD-{key}",
            "asserted_by_role": role}


def delivery(key: str, status: str = "DELIVERED", *, evidence: str | None = None, role: str = "it_service_desk",
             expected: str | None = None, n: int = 1) -> dict:
    out = {"request_id": rid(key, n), "status": status, "evidence_ref": evidence or f"SYN-EVD-{key}",
           "asserted_by_role": role}
    if expected:
        out["expected_date"] = expected
    return out


def collection(key: str, status: str = "COLLECTED", *, evidence: str | None = None, role: str = "it_service_desk",
               next_slot: str | None = None, n: int = 1) -> dict:
    out = {"request_id": rid(key, n), "status": status, "evidence_ref": evidence or f"SYN-EVD-2{key}",
           "asserted_by_role": role}
    if next_slot:
        out["next_slot"] = next_slot
    return out


def ready_payload(n: int = 1, **patch: Any) -> dict:
    """Everything approved, ticketed and evidenced for client_alpha (COLLECT mode): the READY scenario."""
    base = dict(
        approval_records=approvals(ALL, n=n),
        mock_it_ticket_updates=[ticket(k, n=n) for k in ALL],
        provisioning_status=[provisioning(k, n=n) for k in ACCESS_KEYS],
        delivery_status=[delivery(k, n=n) for k in EQUIPMENT_KEYS],
        collection_status=[collection(k, n=n) for k in EQUIPMENT_KEYS],
        as_of_date="2026-10-20",
    )
    base.update(patch)
    return make(n=n, **base)


def new_environment(*, it_desk: MockITServiceDesk | None = None, cases: MockCaseContext | None = None):
    cases = cases or MockCaseContext([ParentCase.model_validate(c) for c in load("parent_cases.json")])
    it_desk = it_desk or MockITServiceDesk()
    service = Uc04Service(cases=cases, it_desk=it_desk, audit=AuditLog(fixed_clock))
    return service, cases, it_desk
