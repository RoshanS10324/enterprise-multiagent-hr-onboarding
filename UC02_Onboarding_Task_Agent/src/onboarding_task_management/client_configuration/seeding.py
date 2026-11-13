"""Dry-run, guarded seeding, read-back verification and rollback planning for client configuration documents.

* ``dry_run`` only READS. It reports, per table, what exists, what would be inserted, what is already there (no-op), and what CONFLICTS
  (a row with the same natural key but different content - never overwritten), plus foreign-key checks, expected counts, coverage and the
  rollback list.
* ``apply`` inserts only MISSING rows, parents first, never updates or deletes, and only in a development environment with the explicit
  opt-in and the target host re-typed. It refuses to run if the dry-run found a conflict.
* ``verify`` re-reads everything and compares it with the projection (read-back), including the stored document hash.
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from onboarding_task_management.client_configuration.document import SECTION_CATEGORY, SECTIONS, assert_valid, document_hash
from onboarding_task_management.client_configuration.projection import (
    INSERT_ORDER,
    NATURAL_KEYS,
    differences,
    natural_key,
    project,
)
from onboarding_task_management.persistence import PersistenceError
from onboarding_task_management.supabase_rest import SupabaseRestClient

ENV_ALLOW = "UC02_ALLOW_CONFIG_SEEDING"


@dataclass
class TableDryRun:
    table: str
    existing_total: int = 0
    existing_for_these_clients: int = 0
    inserts: int = 0
    noops: int = 0
    conflicts: list[str] = field(default_factory=list)

    @property
    def expected_total(self) -> int:
        return self.existing_total + self.inserts


@dataclass
class DryRunReport:
    host: str
    tables: dict[str, TableDryRun]
    foreign_key_problems: list[str]
    document_problems: dict[str, list[str]]
    coverage: dict[str, dict[str, int]]
    rollback: list[tuple[str, dict[str, Any]]]
    pending: dict[str, list[dict[str, Any]]] = field(repr=False, default_factory=dict)

    @property
    def conflicts(self) -> list[str]:
        return [c for t in self.tables.values() for c in t.conflicts]

    @property
    def safe_to_apply(self) -> bool:
        return not (self.conflicts or self.foreign_key_problems or any(self.document_problems.values()))

    @property
    def total_inserts(self) -> int:
        return sum(t.inserts for t in self.tables.values())

    def summary(self) -> dict[str, Any]:
        return {
            "target_host": self.host, "safe_to_apply": self.safe_to_apply, "total_proposed_inserts": self.total_inserts,
            "proposed_updates": 0, "proposed_deletes": 0,
            "tables": {n: {"existing_rows_before": t.existing_total, "existing_for_these_clients": t.existing_for_these_clients,
                           "proposed_inserts": t.inserts, "no_op_already_present": t.noops, "conflicts": len(t.conflicts),
                           "expected_rows_after": t.expected_total} for n, t in self.tables.items()},
            "conflicts": self.conflicts, "foreign_key_problems": self.foreign_key_problems,
            "document_problems": {k: v for k, v in self.document_problems.items() if v},
            "coverage": self.coverage, "rollback_statements": len(self.rollback),
        }


def _host(rest: SupabaseRestClient) -> str:
    return urllib.parse.urlparse(rest._base).hostname or "?"


def coverage(doc: Mapping[str, Any]) -> dict[str, int]:
    """How much each use case's section configures, so 'complete' is measurable."""
    uc01, uc02, uc03, uc04, uc05 = (doc[s] for s in ("uc01_employee_record", "uc02_onboarding_tasks", "uc03_document_validation",
                                                      "uc04_equipment_access", "uc05_orientation_policy"))
    return {"uc01_fields": len(uc01["employee_fields"]), "uc01_roles": len(uc01["supported_roles"]), "uc01_grades": len(uc01["grades"]),
            "uc02_task_templates": len(uc02["task_templates"]), "uc02_assignment_rules": len(uc02["assignment_rules"]),
            "uc03_documents": len(uc03["documents"]), "uc03_mandatory_documents": sum(d["mandatory"] for d in uc03["documents"]),
            "uc04_equipment_items": len(uc04["equipment_catalogue"]), "uc04_access_items": len(uc04["access_catalogue"]),
            "uc05_programmes": len(uc05["orientation_programmes"]), "uc05_policies": len(uc05["policy_acknowledgements"]),
            "shared_holidays": len(doc["shared_configuration"]["calendar"]["holidays"]),
            "shared_approval_rules": len(doc["shared_configuration"]["approval_matrix"]), "sections_stored": len(SECTIONS)}


def rollback_plan(documents: Sequence[Mapping[str, Any]], *, include_role_access_matrix: bool = False) -> list[tuple[str, dict[str, Any]]]:
    """The exact deletions that would undo an insert-only seeding: children first, by natural key. Never executed here."""
    out: list[tuple[str, dict[str, Any]]] = []
    for table in reversed(INSERT_ORDER):
        for doc in documents:
            for row in project(doc, include_role_access_matrix=include_role_access_matrix).get(table, []):
                out.append((table, {c: row[c] for c in NATURAL_KEYS[table]}))
    return out


def dry_run(rest: SupabaseRestClient, documents: Sequence[Mapping[str, Any]], *, include_role_access_matrix: bool = False) -> DryRunReport:
    """READ-ONLY. Selects only; never inserts, updates or deletes."""
    problems = {d["client_master"]["client_id"]: _problems(d) for d in documents}
    ids = [d["client_master"]["client_id"] for d in documents]
    if len(ids) != len(set(ids)):
        problems.setdefault("<set>", []).append("duplicate client ids in the seed set")
    projected = [project(d, include_role_access_matrix=include_role_access_matrix) if not problems[d["client_master"]["client_id"]] else {}
                 for d in documents]
    tables: dict[str, TableDryRun] = {}
    pending: dict[str, list[dict[str, Any]]] = {}
    existing_rows: dict[str, list[dict[str, Any]]] = {}
    for table in INSERT_ORDER:
        if include_role_access_matrix is False and table == "role_access_matrix":
            continue
        existing_rows[table] = rest.select(table, {})
        run = TableDryRun(table, existing_total=len(existing_rows[table]))
        index = {natural_key(table, r): r for r in existing_rows[table]}
        pending[table] = []
        for doc_rows in projected:
            for row in doc_rows.get(table, []):
                key = natural_key(table, row)
                stored = index.get(key)
                if stored is None:
                    run.inserts += 1
                    pending[table].append(row)
                else:
                    run.existing_for_these_clients += 1
                    diff = differences(row, stored)
                    if diff:
                        run.conflicts.append(f"{table} {dict(zip(NATURAL_KEYS[table], key))} exists with different {', '.join(diff)} "
                                             "(would NOT be overwritten)")
                    else:
                        run.noops += 1
        tables[table] = run
    fk = _foreign_keys(projected, existing_rows)
    for doc in documents:  # an existing client's master row must exist and agree with the profile; it is never rewritten
        m = doc["client_master"]
        if not m.get("existing_client_record") or problems[m["client_id"]]:
            continue
        stored = next((r for r in existing_rows["client_configs"] if r["client_id"] == m["client_id"]), None)
        if stored is None:
            problems[m["client_id"]].append("existing_client_record is set but no client_configs row exists")
            continue
        want = {"timezone": m["timezone"], "working_days": list(doc["shared_configuration"]["calendar"]["working_days"]),
                "is_active": m["client_status"] == "active"}
        diff = [k for k, v in want.items() if stored.get(k) != v]
        if diff:
            tables["client_configs"].conflicts.append(f"client_configs {m['client_id']}: stored {', '.join(diff)} differ from the profile "
                                                      "(the stored row is preserved; fix the profile or get approval to change the row)")
        else:
            tables["client_configs"].existing_for_these_clients += 1
            tables["client_configs"].noops += 1
    return DryRunReport(
        host=_host(rest), tables=tables, foreign_key_problems=fk, document_problems=problems,
        coverage={d["client_master"]["client_id"]: coverage(d) for d in documents if not problems[d["client_master"]["client_id"]]},
        rollback=rollback_plan([d for d in documents if not problems[d["client_master"]["client_id"]]],
                               include_role_access_matrix=include_role_access_matrix), pending=pending)


def _problems(doc: Mapping[str, Any]) -> list[str]:
    from onboarding_task_management.client_configuration.document import validate_document

    return validate_document(doc)


def _foreign_keys(projected: list[dict[str, list[dict[str, Any]]]], existing: Mapping[str, list[dict[str, Any]]]) -> list[str]:
    """Every row's client must exist (already stored or planned in client_configs), and every version reference must exist."""
    known_clients = {r["client_id"] for r in existing.get("client_configs", [])} | {r["client_id"] for p in projected
                                                                                     for r in p.get("client_configs", [])}
    known_versions = {(r["client_id"], r["version"]) for r in existing.get("client_config_versions", [])} | {
        (r["client_id"], r["version"]) for p in projected for r in p.get("client_config_versions", [])}
    problems: list[str] = []
    for p in projected:
        for table, rows in p.items():
            for row in rows:
                if row["client_id"] not in known_clients:
                    problems.append(f"{table}: client '{row['client_id']}' has no client_configs row")
                if "config_version" in row and (row["client_id"], row["config_version"]) not in known_versions:
                    problems.append(f"{table}: version {row['config_version']} of '{row['client_id']}' has no client_config_versions row")
    return problems


def _guard(environ: Mapping[str, str]) -> None:
    if (environ.get("UC02_ENV") or "").strip().lower() != "development" or environ.get(ENV_ALLOW) != "1":
        raise PersistenceError(f"configuration seeding needs UC02_ENV=development and {ENV_ALLOW}=1; refused")


def apply(rest: SupabaseRestClient, documents: Sequence[Mapping[str, Any]], *, confirm_target: str,
          environ: Mapping[str, str] | None = None, include_role_access_matrix: bool = False) -> dict[str, dict[str, int]]:
    """Insert every MISSING row, parents first. Never updates or deletes. Refuses on any conflict, FK or document problem."""
    env = os.environ if environ is None else environ
    _guard(env)
    if confirm_target != _host(rest):
        raise PersistenceError(f"refusing to write: --confirm-write must equal the target host ({_host(rest)})")
    for doc in documents:
        assert_valid(doc)
    report = dry_run(rest, documents, include_role_access_matrix=include_role_access_matrix)
    if not report.safe_to_apply:
        raise PersistenceError("the dry run is not clean; nothing was written: " + "; ".join((report.conflicts + report.foreign_key_problems)[:5]))
    result: dict[str, dict[str, int]] = {}
    for table in INSERT_ORDER:
        rows = report.pending.get(table, [])
        for row in rows:
            rest.insert(table, [row])
        if table in report.tables:
            result[table] = {"inserted": len(rows), "already_present": report.tables[table].noops}
    return result


def verify(rest: SupabaseRestClient, documents: Sequence[Mapping[str, Any]], *, include_role_access_matrix: bool = False) -> list[str]:
    """READ-ONLY read-back: every projected row must be stored with the same content, and the stored document must hash to the version's
    ``config_hash``. Returns problems; empty means the database holds exactly what the documents say."""
    problems: list[str] = []
    for doc in documents:
        client = doc["client_master"]["client_id"]
        rows = project(doc, include_role_access_matrix=include_role_access_matrix)
        for table in INSERT_ORDER:
            for row in rows.get(table, []):
                key = {c: row[c] for c in NATURAL_KEYS[table]}
                found = rest.select(table, key)
                if len(found) != 1:
                    problems.append(f"{client}: {table} {key} found {len(found)} rows (expected 1)")
                    continue
                diff = differences(row, found[0])
                if diff:
                    problems.append(f"{client}: {table} {key} differs in {', '.join(diff)}")
        version = doc["client_master"]["configuration_version"]
        stored = {r["service_key"]: r["policy_details"] for r in rest.select(
            "client_service_policies", {"client_id": client, "config_version": version, "service_category": SECTION_CATEGORY})}
        rebuilt = {s: stored[s]["document"] for s in SECTIONS if s in stored}
        if set(rebuilt) != set(SECTIONS):
            problems.append(f"{client}: sections stored {sorted(rebuilt)} (expected {sorted(SECTIONS)})")
        else:
            whole = {**{k: doc[k] for k in ("schema_version", "client_master", "awaiting_confirmation")}, **rebuilt}
            if document_hash(whole) != document_hash(doc):
                problems.append(f"{client}: the stored sections do not rebuild the document (hash differs)")
        versions = rest.select("client_config_versions", {"client_id": client, "version": version})
        if versions and versions[0]["config_hash"] != document_hash(doc):
            problems.append(f"{client}: client_config_versions.config_hash does not match the document")
    return problems


__all__ = ["DryRunReport", "ENV_ALLOW", "TableDryRun", "apply", "coverage", "dry_run", "rollback_plan", "verify"]
