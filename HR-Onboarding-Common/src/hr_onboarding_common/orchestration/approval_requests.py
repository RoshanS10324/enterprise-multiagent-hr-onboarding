"""The orchestrator's own approval requests (its HITL gates) written to the shared `approval_requests` table.

The HITL inbox (platform/hitl.py; SQLite under the orchestrator's data directory) stays the authoritative
store the orchestrator decides against. Every change to one of the orchestrator's own requests -- raised,
decided, withdrawn -- is written through to `approval_requests`, so the request and its decision are visible
in Supabase next to UC02's (which writes the same table with workflow_module='uc02_onboarding_task_management').

Columns (verified against the live schema) <- ApprovalItem:
  request_id <- id                 gate, approver_role, requested_by, payload, rationale, final_payload, apply_error
  workflow_module <- MODULE        subject_ref <- subject_id (the case id the request is for)
  confidence_score <- confidence   outcome <- status, upper-case (UC02's vocabulary: PENDING, APPROVED, ...)
  decided_by / decided_at / decision_reason_code / decision_notes <- approver / decided_at / reason_code / note
                                   -- written ONLY once a person actually decided; never filled in advance
  sla_due_at <- created_at + sla_hours                    created_at <- created_at
  case_id <- the case's uuid only when its onboarding_cases row exists in the same tenant (foreign key);
             NULL otherwise -- e.g. an internal-onboarding-form approval, which precedes UC01's case.

The table's client_id is a foreign key to client_configs: a tenant without a client_configs row cannot be
written, and the result says so. Nothing is inserted into client_configs or onboarding_cases here.
Write failures never undo or block a decision: the result (persisted, reason) is returned for the caller to
record on the case, and logged without row values.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from hr_onboarding_common.observability.logging import get_logger

log = get_logger("orchestration.approval_requests")

TABLE, CASES = "approval_requests", "onboarding_cases"
MODULE = "onboarding_orchestrator"


def row_of(item: Any) -> dict[str, Any]:
    decided = str(item.status) not in ("pending",)
    row = {
        "request_id": item.id, "client_id": item.client_id, "gate": item.gate, "approver_role": item.approver_role,
        "requested_by": item.requested_by or "unknown", "workflow_module": MODULE,
        "subject_ref": item.subject_id or item.workflow_instance, "payload": item.payload,
        "rationale": item.rationale, "confidence_score": item.confidence, "outcome": str(item.status).upper(),
        "final_payload": item.final_payload, "apply_error": item.apply_error,
        "sla_due_at": (item.created_at + timedelta(hours=item.sla_hours)).isoformat(),
        "created_at": item.created_at.isoformat(),
        "decided_by": item.approver if decided else None,
        "decided_at": item.decided_at.isoformat() if decided and item.decided_at else None,
        "decision_reason_code": (str(item.reason_code) if item.reason_code else None) if decided else None,
        "decision_notes": item.note if decided else None,
    }
    return row


class SupabaseApprovalRequests:
    def __init__(self, *, url: str, service_role_key: SecretStr | str, schema: str = "public",
                 transport: httpx.BaseTransport | None = None, timeout_s: float = 15.0):
        key = service_role_key if isinstance(service_role_key, SecretStr) else SecretStr(service_role_key)
        if not url or not key.get_secret_value().strip():
            raise ValueError("approval_requests needs a Supabase URL and service-role key")
        self._client = httpx.Client(base_url=url.rstrip("/") + "/rest/v1", timeout=timeout_s, transport=transport,
                                    headers={"apikey": key.get_secret_value(),
                                             "Authorization": f"Bearer {key.get_secret_value()}",
                                             "Accept-Profile": schema, "Content-Profile": schema})

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "SupabaseApprovalRequests | None":
        from hr_onboarding_common.orchestration.documents import STORAGE_KEY_VARS, STORAGE_URL_VARS, _first_env

        url, key = _first_env(environ, STORAGE_URL_VARS), _first_env(environ, STORAGE_KEY_VARS)
        if not url or not key:
            return None
        return cls(url=url, service_role_key=key, schema=(environ.get("SUPABASE_SCHEMA") or "public").strip() or "public")

    def _case_uuid(self, item: Any) -> str | None:
        ref = item.subject_id or item.workflow_instance
        try:
            uuid.UUID(str(ref))
        except ValueError:
            return None
        rows = self._client.get(f"/{CASES}", params={"select": "case_id", "case_id": f"eq.{ref}",
                                                    "client_id": f"eq.{item.client_id}"})
        return ref if rows.status_code == 200 and rows.json() else None

    def sync(self, item: Any) -> dict[str, Any]:
        """Upsert the request's row. Returns {"persisted": bool, "reason": str | None, "outcome": str}."""
        row = row_of(item)
        try:
            row["case_id"] = self._case_uuid(item)
            response = self._client.post(f"/{TABLE}", json=row, params={"on_conflict": "request_id"},
                                         headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        except httpx.HTTPError:
            result = {"persisted": False, "reason": "SUPABASE_UNREACHABLE"}
        else:
            if response.status_code in (200, 201, 204):
                result = {"persisted": True, "reason": None}
            else:
                try:
                    body = response.json() or {}
                except ValueError:
                    body = {}
                sqlstate = body.get("code")
                detail = str(body.get("details") or body.get("message") or "")
                if sqlstate == "23503" and "client_configs" in detail:
                    reason = f"TENANT_NOT_IN_CLIENT_CONFIGS ({item.client_id})"
                else:
                    reason = f"HTTP_{response.status_code}" + (f"_SQLSTATE_{sqlstate}" if sqlstate else "")
                result = {"persisted": False, "reason": reason}
        result["outcome"] = row["outcome"]
        if not result["persisted"]:
            log.warning("orchestration.approval_request_not_persisted",
                        extra={"request_id": item.id, "gate": item.gate, "reason": result["reason"]})
        return result


class InMemoryApprovalRequests:
    """Tests only: the same contract, keyed by request_id, with an optional set of known tenants."""

    def __init__(self, known_tenants: set[str] | None = None):
        self.known_tenants = known_tenants
        self.rows: dict[str, dict[str, Any]] = {}

    def sync(self, item: Any) -> dict[str, Any]:
        row = row_of(item)
        if self.known_tenants is not None and item.client_id not in self.known_tenants:
            return {"persisted": False, "reason": f"TENANT_NOT_IN_CLIENT_CONFIGS ({item.client_id})",
                    "outcome": row["outcome"]}
        self.rows[item.id] = row
        return {"persisted": True, "reason": None, "outcome": row["outcome"]}
