"""Deterministic projection of a client configuration document onto the EXISTING shared Supabase tables. No new table or column.

The document is the source of truth. These rows exist so the agents that already read those tables (UC-02's configuration reader, UC-03's
document requirements, UC-04's role matrix) keep working, and so the whole document can be rebuilt and integrity-checked:

=========================================  ==================================================================================
document                                   existing table (natural key)
=========================================  ==================================================================================
client_master + shared calendar            ``client_configs`` (client_id); ``settings_payload`` carries the master data
configuration version                      ``client_config_versions`` (client_id, version)
uc02 task templates                        ``task_templates`` (client_id, template_key, config_version)
uc02 assignment rules                      ``client_service_policies`` category ``uc02_assignment`` (the reader's convention)
uc03 documents                             ``client_document_requirements`` (client_id, config_version, requirement_key)
uc02 milestones                            ``client_milestone_templates`` (client_id, config_version, milestone_key)
shared holidays                            ``client_holidays`` (client_id, holiday_date)
uc04 role -> item entitlements (optional)  ``role_access_matrix`` (client_id, role_id, item_id, item_kind) - UC-04's table, unversioned
all six sections, whole                    ``client_service_policies`` category ``cfg_section`` (client_id, config_version, key)
=========================================  ==================================================================================

Employment type has no column in these tables, so it is carried as the attribute ``employment_type:<value>`` inside ``requires_attributes``;
the employee's context must supply the same attribute (see ``retrieval``). A construct a table cannot represent faithfully raises
``ProjectionError`` instead of being dropped (fail closed).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from onboarding_task_management.client_configuration.document import SECTION_CATEGORY, SECTIONS, document_hash
from onboarding_task_management.supabase_synthetic import _uuid

NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "client_configs": ("client_id",),
    "client_config_versions": ("client_id", "version"),
    "task_templates": ("client_id", "template_key", "config_version"),
    "client_document_requirements": ("client_id", "config_version", "requirement_key"),
    "client_service_policies": ("client_id", "config_version", "service_category", "service_key"),
    "client_milestone_templates": ("client_id", "config_version", "milestone_key"),
    "client_holidays": ("client_id", "holiday_date"),
    "role_access_matrix": ("client_id", "role_id", "item_id", "item_kind"),
}
#: Insert order (parents first) and the reverse for rollback. ``client_configs`` is the foreign-key parent of every other table.
INSERT_ORDER = ("client_configs", "client_config_versions", "task_templates", "client_document_requirements", "client_service_policies",
                "client_milestone_templates", "client_holidays", "role_access_matrix")
#: Columns the database (or this projection's comparison) must ignore: server-generated or volatile.
IGNORED_ON_COMPARE = frozenset({"created_at", "updated_at"})
ASSIGNMENT_CATEGORY = "uc02_assignment"


class ProjectionError(ValueError):
    """The document uses something the existing tables cannot represent faithfully."""


def _attrs(a: Mapping[str, Any], owner: str) -> list[str]:
    if a["excludes_attributes"]:
        raise ProjectionError(f"{owner}: excludes_attributes cannot be represented in the existing tables")
    return list(a["requires_attributes"]) + [f"employment_type:{e}" for e in a["employment_types"]]


def project(doc: Mapping[str, Any], *, include_role_access_matrix: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Every row this document owns, keyed by table. Pure: reads nothing, writes nothing.

    ``role_access_matrix`` (UC-04's table) is EXCLUDED by default and requesting it raises ``ProjectionError``: its live CHECK constraints allow only
    ``item_kind`` in HARDWARE / SOFTWARE_ACCESS / PHYSICAL_BADGE / SYSTEM_PERM and ``approval_class`` in STANDARD / MANAGER_EXCEPTION / SECURITY_GATE, and the
    profiles do not (and UC-04 has not confirmed how they should) map onto those values. Nothing is guessed.
    """
    if include_role_access_matrix:
        raise ProjectionError("role_access_matrix mapping is not confirmed by UC-04; the live CHECK constraints allow item_kind HARDWARE, SOFTWARE_ACCESS, "
                              "PHYSICAL_BADGE, SYSTEM_PERM and approval_class STANDARD, MANAGER_EXCEPTION, SECURITY_GATE, which the profiles do not specify")
    m = doc["client_master"]
    client, version = m["client_id"], m["configuration_version"]
    digest = document_hash(doc)
    uc02, uc03, uc04 = doc["uc02_onboarding_tasks"], doc["uc03_document_validation"], doc["uc04_equipment_access"]
    shared = doc["shared_configuration"]

    templates = []
    for t in uc02["task_templates"]:
        a = t["applicability"]
        templates.append({
            "template_key": t["template_key"], "client_id": client, "category": t["category"], "title": t["title"], "title_ar": t["title"],
            "description": t["description"], "owner_role": t["owner_role"], "offset_days": t["offset_days"],
            "default_lead_days": t["sla_working_days"], "depends_on_template_keys": list(t["depends_on"]), "sensitive_to": [],
            "role_ids": list(a["roles"]), "grades": list(a["grades"]), "locations": list(a["locations"]), "projects": list(a["projects"]),
            "nationality_groups": list(a["nationality_groups"]), "requires_attributes": _attrs(a, f"task {t['template_key']}"),
            "evidence_required": t["evidence_required"], "approval_role": t["approval_role"], "default_priority": t["priority"],
            "config_version": version, "is_active": True})

    documents = []
    for d in uc03["documents"]:
        a = d["applicability"]
        extras = {k: v for k, v in d.items() if k not in (
            "document_key", "name", "category", "mandatory", "applicability")}
        documents.append({
            "requirement_id": _uuid("cfgdoc", client, str(version), d["document_key"]), "client_id": client, "config_version": version,
            "service_category": d["category"], "requirement_key": d["document_key"], "document_type": d["document_key"],
            "description": d["name"], "is_mandatory": d["mandatory"], "applicable_roles": list(a["roles"]),
            "applicable_grades": list(a["grades"]), "applicable_locations": list(a["locations"]), "applicable_projects": list(a["projects"]),
            "eligibility_rules": {"nationality_groups": list(a["nationality_groups"]),
                                  "requires_attributes": _attrs(a, f"document {d['document_key']}"),
                                  "allow_not_applicable": not d["mandatory"]},
            "validation_profile": {**extras, "global": uc03["global"]}, "is_active": True})

    policies = [{
        "policy_id": _uuid("cfgpolicy", client, str(version), ASSIGNMENT_CATEGORY, r["category"]), "client_id": client,
        "config_version": version, "service_category": ASSIGNMENT_CATEGORY, "service_key": r["category"], "is_available": True,
        "is_mandatory": False, "eligibility_rules": {},
        "policy_details": {"team": r["team"], "use_case": r["downstream_use_case"] or "", "mode": r["mode"],
                           "escalation_owner": r["escalation_owner"]}, "responsible_role": None, "is_active": True}
        for r in uc02["assignment_rules"]]
    for section in SECTIONS:
        policies.append({
            "policy_id": _uuid("cfgpolicy", client, str(version), SECTION_CATEGORY, section), "client_id": client,
            "config_version": version, "service_category": SECTION_CATEGORY, "service_key": section, "is_available": True,
            "is_mandatory": True, "eligibility_rules": {},
            "policy_details": {"section": section, "schema_version": doc["schema_version"], "document_hash": digest,
                               "sections_hash": sections_hash(doc), "document": doc[section]},
            "responsible_role": None, "effective_from": m["effective_from"], "effective_to": m["effective_until"], "is_active": True})

    milestones = [{
        "milestone_id": _uuid("cfgmilestone", client, str(version), x["milestone_key"]), "client_id": client, "config_version": version,
        "milestone_key": x["milestone_key"], "title": x["title"], "offset_days": x["offset_days"],
        "required_task_keys": list(x["required_task_keys"]), "eligibility_rules": {}, "is_active": True} for x in uc02["milestones"]]

    holidays = [{"client_id": client, "holiday_date": h["date"], "name": h["name"], "is_observed": h["observed"]}
                for h in shared["calendar"]["holidays"]]

    matrix: list[dict[str, Any]] = []
    if include_role_access_matrix:
        for kind, entries in (("EQUIPMENT", uc04["role_equipment_matrix"]), ("ACCESS", uc04["role_access_matrix"])):
            for entry in entries:
                for code in entry["items"]:
                    matrix.append({"matrix_id": _uuid("cfgmatrix", client, entry["role_id"], code, kind), "client_id": client,
                                   "role_id": entry["role_id"], "item_id": code, "item_kind": kind, "is_mandatory": True,
                                   "approval_class": kind, "lead_time_days": uc04["lead_times"][kind]})

    settings = {"data_origin": m["data_origin"], "client_status": m["client_status"], "country": m["country"],
                "supported_locations": m["supported_locations"], "schema_version": doc["schema_version"], "scenario": m["scenario"],
                "active_configuration_version": version, "config_document_hash": digest, "synthetic_notice": m["synthetic_notice"]}
    configs = [{"client_id": client, "company_name": m["client_name"], "config_hash": digest,
                "working_days": list(shared["calendar"]["working_days"]), "timezone": m["timezone"],
                "template_count": len(templates), "milestone_count": len(milestones), "is_active": m["client_status"] == "active",
                "settings_payload": settings}]
    versions = [{"client_id": client, "version": version, "effective_from": m["effective_from"], "effective_to": m["effective_until"],
                 "status": m["approval_status"], "config_hash": digest, "approved_by": m["approved_by"],
                 "approved_at": m["approved_at"] or None}]
    if m.get("existing_client_record"):
        configs = []  # the client's existing client_configs row is preserved, never rewritten (checked by the dry run instead)
    rows = {"client_configs": configs, "client_config_versions": versions, "task_templates": templates,
            "client_document_requirements": documents, "client_service_policies": policies,
            "client_milestone_templates": milestones, "client_holidays": holidays}
    if include_role_access_matrix:
        rows["role_access_matrix"] = matrix
    return rows


def sections_hash(doc: Mapping[str, Any]) -> str:
    """sha256 over the six stored sections only. Lets a reader verify the stored sections without the client master, which for an existing
    client stays in its own preserved ``client_configs`` row."""
    from onboarding_task_management.client_configuration.document import canonical_json

    return "sha256:" + hashlib.sha256(canonical_json({s: doc[s] for s in SECTIONS}).encode("utf-8")).hexdigest()


def natural_key(table: str, row: Mapping[str, Any]) -> tuple:
    return tuple(row[c] for c in NATURAL_KEYS[table])


def normalise(value: Any) -> Any:
    """Comparison form: lists keep order, dates/timestamps become ISO strings, so 'same content' is stable across the wire."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {k: normalise(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normalise(v) for v in value]
    return value


def differences(projected: Mapping[str, Any], stored: Mapping[str, Any]) -> list[str]:
    """Columns where the stored row differs from the projected one (server-generated columns ignored)."""
    out = []
    for column, want in projected.items():
        if column in IGNORED_ON_COMPARE or column not in stored:
            continue
        have = stored[column]
        if column.endswith(("_at",)) and have is not None and want is not None:
            if str(have)[:19] == str(want)[:19]:
                continue
        if normalise(have) != normalise(want):
            out.append(column)
    return out


__all__ = ["ASSIGNMENT_CATEGORY", "INSERT_ORDER", "NATURAL_KEYS", "ProjectionError", "differences", "natural_key", "normalise", "project"]
