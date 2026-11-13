"""An in-memory `TableClient` that behaves like the shared Supabase tables UC-03 uses, strictly.

Columns and required flags come from a read-only snapshot of the live project
(tests/fixtures/uc03_live_schema.json)
plus the proposed UC-03 migration. CHECK lists, primary/unique keys and foreign keys follow
consolidated_database_schema.sql. audit_events is append-only. A write the real database would
reject raises here too,
so a mapping mistake fails a test instead of passing silently. No network.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from integrations.supabase.tables import DuplicateKey, StorageError

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "uc03_live_schema.json"

MIGRATION_COLUMNS = {
    "document_validations": [
        "uc03_case_ref",
        "request_key",
        "document_status",
        "required_next_action",
        "approval_status",
        "evidence_status",
        "uc03_compliance_status",
        "uc03_visa_pack_status",
        "decision_detail",
        "audit_reference",
        "advisory",
    ],
}
MIGRATION_TABLES = {
    "uc03_case_state": {
        "columns": [
            "case_ref",
            "onboarding_case_id",
            "client_id",
            "employee_id",
            "status",
            "state",
            "version",
            "created_at",
            "updated_at",
        ],
        "required": ["case_ref", "client_id", "employee_id", "status", "state"],
    },
}
PRIMARY_KEYS = {
    "onboarding_cases": "case_id",
    "candidate_documents": "document_id",
    "document_validations": "validation_id",
    "approval_requests": "request_id",
    "audit_events": "event_id",
    "client_configs": "client_id",
    "uc03_case_state": "case_ref",
}
GENERATED = {
    "candidate_documents": "document_id",
    "document_validations": "validation_id",
    "audit_events": "event_id",
}
DEFAULTS = {
    "candidate_documents": {"status": "UPLOADED", "uploaded_at": "now"},
    "document_validations": {
        "validated_at": "now",
        "is_readable": True,
        "required_fields_present": True,
        "extraction_confidence": 1.0,
        "compliance_item_status": "INCOMPLETE",
        "visa_pack_status": "NOT_APPLICABLE",
        "hr_review_status": "PENDING",
        "injection_detected": False,
    },
    "audit_events": {"occurred_at": "now", "actor_role": "system", "outcome": "SUCCESS"},
    "approval_requests": {
        "created_at": "now",
        "sla_due_at": "now",
        "rationale": "",
        "confidence_score": 1.0,
        "outcome": "PENDING",
    },
    "uc03_case_state": {"version": 1, "created_at": "now", "updated_at": "now"},
}
UNIQUE = {
    "candidate_documents": [("case_id", "document_type", "checksum_sha256")],
    "document_validations": [("request_key",)],
}
FOREIGN = {
    "candidate_documents": {"case_id": "onboarding_cases"},
    "document_validations": {"document_id": "candidate_documents", "case_id": "onboarding_cases"},
    "approval_requests": {"case_id": "onboarding_cases", "client_id": "client_configs"},
    "audit_events": {"case_id": "onboarding_cases"},
    "uc03_case_state": {"onboarding_case_id": "onboarding_cases"},
}
CHECKS = {
    "candidate_documents": {
        "document_type": {
            "PASSPORT",
            "NATIONAL_ID",
            "VISA",
            "EDUCATIONAL_CERTIFICATE",
            "OFFER_LETTER",
            "MEDICAL_FITNESS",
            "BACKGROUND_CHECK",
            "SIGNED_POLICY",
        },
        "status": {"UPLOADED", "PARSING", "VALIDATED", "FAILED", "REJECTED", "EXPIRED"},
    },
    "document_validations": {
        "compliance_item_status": {"COMPLIANT", "INCOMPLETE", "NON_COMPLIANT", "WAIVED"},
        "visa_pack_status": {"NOT_APPLICABLE", "READY", "INCOMPLETE", "BLOCKED"},
        "hr_review_status": {"NOT_REQUIRED", "PENDING", "ACCEPTED", "REJECTED"},
    },
    "audit_events": {"actor_type": {"HUMAN", "AGENT", "SYSTEM"}},
    "approval_requests": {
        "outcome": {
            "PENDING",
            "APPROVED",
            "EDITED_AND_APPROVED",
            "REJECTED",
            "CANCELLED",
            "EXPIRED",
        }
    },
}
APPEND_ONLY = {"audit_events"}


class FakeTables:
    def __init__(self) -> None:
        snap = json.loads(_FIXTURE.read_text(encoding="utf-8"))["tables"]
        self.columns: dict[str, set[str]] = {t: set(v["columns"]) for t, v in snap.items()}
        self.required: dict[str, set[str]] = {t: set(v["required"]) for t, v in snap.items()}
        for t, cols in MIGRATION_COLUMNS.items():
            self.columns[t] |= set(cols)
        for t, spec in MIGRATION_TABLES.items():
            self.columns[t], self.required[t] = set(spec["columns"]), set(spec["required"])
        self.rows: dict[str, list[dict[str, Any]]] = {t: [] for t in self.columns}
        self.fail: set[tuple[str, str]] = (
            set()
        )  # (operation, table) pairs that raise a database error
        self.calls: list[tuple[str, str]] = []

    # ---------------------------------------------------------------- seeding (stands in for
    # data owned by other UCs)
    def seed(self, table: str, **row: Any) -> dict[str, Any]:
        self.rows[table].append(row)
        return row

    # ---------------------------------------------------------------- TableClient
    def select(self, table, *, columns="*", filters=None, order=None, desc=False, limit=None):
        self._check(("select", table))
        rows = [
            r
            for r in self.rows[self._table(table)]
            if all(r.get(k) == v for k, v in (filters or {}).items())
        ]
        if order:
            rows = sorted(rows, key=lambda r: str(r.get(order)), reverse=desc)
        if limit:
            rows = rows[:limit]
        wanted = None if columns == "*" else [c.strip() for c in columns.split(",")]
        return [
            copy.deepcopy({k: v for k, v in r.items() if wanted is None or k in wanted})
            for r in rows
        ]

    def insert(self, table, row):
        self._check(("insert", table))
        t = self._table(table)
        unknown = set(row) - self.columns[t]
        if unknown:
            raise StorageError("DATABASE_ERROR", f"unknown column(s) {sorted(unknown)}")
        full = dict(row)
        if t in GENERATED and not full.get(GENERATED[t]):
            full[GENERATED[t]] = str(uuid.uuid4())
        for col, val in DEFAULTS.get(t, {}).items():
            if full.get(col) is None:
                full[col] = datetime.now(UTC).isoformat() if val == "now" else val
        missing = [c for c in self.required[t] if full.get(c) is None]
        if missing:
            raise StorageError("DATABASE_ERROR", f"not-null violation {missing}")
        self._constraints(t, full)
        pk = PRIMARY_KEYS[t]
        if any(r.get(pk) == full[pk] for r in self.rows[t]):
            raise DuplicateKey("DUPLICATE_KEY", f"{t}.{pk}")
        for cols in UNIQUE.get(t, []):
            if all(full.get(c) is not None for c in cols) and any(
                all(r.get(c) == full[c] for c in cols) for r in self.rows[t]
            ):
                raise DuplicateKey("DUPLICATE_KEY", f"{t}{cols}")
        self.rows[t].append(full)
        return copy.deepcopy(full)

    def update(self, table, values, filters):
        self._check(("update", table))
        t = self._table(table)
        if t in APPEND_ONLY:
            raise StorageError("DATABASE_ERROR", f"{t} is append-only")
        unknown = set(values) - self.columns[t]
        if unknown:
            raise StorageError("DATABASE_ERROR", f"unknown column(s) {sorted(unknown)}")
        out = []
        for r in self.rows[t]:
            if all(r.get(k) == v for k, v in filters.items()):
                candidate = {**r, **values}
                self._constraints(t, candidate)
                r.update(values)
                out.append(copy.deepcopy(r))
        return out

    # ---------------------------------------------------------------- helpers
    def _table(self, table: str) -> str:
        if table not in self.columns:
            raise StorageError("DATABASE_ERROR", f"unknown table {table}")
        return table

    def _check(self, op: tuple[str, str]) -> None:
        self.calls.append(op)
        if op in self.fail:
            raise StorageError("DATABASE_ERROR", "http 503 (injected)")

    def _constraints(self, t: str, row: dict[str, Any]) -> None:
        for col, allowed in CHECKS.get(t, {}).items():
            if row.get(col) is not None and row[col] not in allowed:
                raise StorageError("DATABASE_ERROR", f"check violation {t}.{col}={row[col]!r}")
        for col, ref in FOREIGN.get(t, {}).items():
            if row.get(col) is not None and not any(
                r.get(PRIMARY_KEYS[ref]) == row[col] for r in self.rows[ref]
            ):
                raise StorageError("DATABASE_ERROR", f"foreign key violation {t}.{col}")
        if t == "approval_requests":
            if row.get("decided_by") is not None and row.get("decided_by") == row.get(
                "requested_by"
            ):
                raise StorageError("DATABASE_ERROR", "chk_no_self_approval")
            if row.get("outcome") != "PENDING" and not row.get("decided_at"):
                raise StorageError("DATABASE_ERROR", "chk_decided_has_timestamp")
