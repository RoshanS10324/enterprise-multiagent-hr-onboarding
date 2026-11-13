"""Loading, hashing and validating a client configuration document (the JSON the central configuration is authored in).

The document is the single source of truth for one client + version. The relational rows in the existing shared tables are a deterministic
projection of it (see ``projection.py``); the six section rows in ``client_service_policies`` hold the whole document so it can be rebuilt and
integrity-checked against ``client_config_versions.config_hash``.

Validation is two-layered: the JSON Schema (``client_configuration/schema``) for structure, and semantic checks here for the cross-references a
schema cannot express (dependencies, catalogue references, applicability values, secrets, out-of-scope sections).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The repository's shared ``client_configuration`` folder (schema, profiles, builder), found from this file's location.
CONFIG_DIR = Path(__file__).resolve().parents[4] / "client_configuration"
SCHEMA_PATH = CONFIG_DIR / "schema" / "client_configuration.schema.json"
PROFILE_DIR = CONFIG_DIR / "profiles"

#: Section name -> the document key that holds it. ``shared_configuration`` is stored like the five use-case sections.
SECTIONS = ("shared_configuration", "uc01_employee_record", "uc02_onboarding_tasks", "uc03_document_validation",
            "uc04_equipment_access", "uc05_orientation_policy")
SECTION_CATEGORY = "cfg_section"  # client_service_policies.service_category for the section documents

_SECRET_KEY = re.compile(r"(password|passwd|secret|token|api[_-]?key|service[_-]?role|credential|private[_-]?key|bearer)", re.I)
_SECRET_VALUE = re.compile(r"(eyJ[A-Za-z0-9_-]{20,}|sb_secret|-----BEGIN|Bearer\s+[A-Za-z0-9._-]{10,})")
_OUT_OF_SCOPE = re.compile(r"uc[-_ ]?0?6", re.I)


class ConfigurationDocumentError(ValueError):
    """The document is not a valid client configuration."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def document_hash(doc: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of the whole document. Stored as ``config_hash`` and checked on retrieval."""
    return "sha256:" + hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


def load_document(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationDocumentError(f"{path.name}: not valid JSON ({type(exc).__name__})") from exc


def load_profiles(directory: Path = PROFILE_DIR) -> list[dict[str, Any]]:
    return [load_document(p) for p in sorted(directory.glob("client_*_config.json"))]


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _walk(value: Any, path: str = ""):
    yield path, value
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")


def _cycle(graph: Mapping[str, list[str]]) -> list[str]:
    state: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> list[str]:
        if state.get(node) == 1:
            return trail + [node]
        if state.get(node) == 2:
            return []
        state[node] = 1
        for nxt in graph.get(node, []):
            found = visit(nxt, trail + [node])
            if found:
                return found
        state[node] = 2
        return []

    for n in graph:
        found = visit(n, [])
        if found:
            return found
    return []


def validate_document(doc: Mapping[str, Any]) -> list[str]:
    """Every problem found, as readable strings. Empty means valid. Never raises for a bad document."""
    import jsonschema  # type: ignore[import-untyped]

    problems: list[str] = []
    validator = jsonschema.Draft202012Validator(load_schema())
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.path))):
        problems.append(f"schema: {'.'.join(map(str, err.path)) or '<root>'}: {err.message[:160]}")
    if problems:
        return problems  # the semantic checks assume the structure is right

    for path, value in _walk(doc):
        leaf = path.rsplit(".", 1)[-1]
        if _SECRET_KEY.search(re.sub(r"\[\d+\]", "", leaf)) and leaf not in ("secret",):
            problems.append(f"secret-like key is not allowed in configuration: {path}")
        if isinstance(value, str) and _SECRET_VALUE.search(value):
            problems.append(f"secret-like value is not allowed in configuration: {path}")
    if _OUT_OF_SCOPE.search(canonical_json(doc)):
        problems.append("an out-of-scope use case is referenced; only UC-01..UC-05 belong in client configuration")

    master = doc["client_master"]
    if master["effective_until"] and master["effective_until"] < master["effective_from"]:
        problems.append("effective_until is before effective_from")
    if master["approval_status"] == "approved" and not (master["approved_by"] and master["approved_at"]):
        problems.append("an approved version must carry approved_by and approved_at")
    if master["country"]["code"] not in {loc["country_code"] for loc in master["supported_locations"]}:
        problems.append("no supported location is in the client's country")

    uc01, uc02, uc03, uc04, uc05 = (doc[s] for s in ("uc01_employee_record", "uc02_onboarding_tasks", "uc03_document_validation",
                                                      "uc04_equipment_access", "uc05_orientation_policy"))
    roles = {r["role_id"] for r in uc01["supported_roles"]}
    grades = {g["grade_id"] for g in uc01["grades"]}
    groups = {g["group_code"] for g in uc01["nationality_groups"]}
    locations = {loc["code"] for loc in master["supported_locations"]}
    etypes = set(uc01["employment_types"])

    def check_app(owner: str, a: Mapping[str, Any]) -> None:
        for dim, known in (("roles", roles), ("grades", grades), ("nationality_groups", groups), ("locations", locations),
                           ("employment_types", etypes)):
            for v in a[dim]:
                if v not in known:
                    problems.append(f"{owner}: {dim} value '{v}' is not declared by the client (uc01 / client_master)")

    keys = [t["template_key"] for t in uc02["task_templates"]]
    if len(keys) != len(set(keys)):
        problems.append("duplicate task template keys")
    graph = {t["template_key"]: t["depends_on"] for t in uc02["task_templates"]}
    for t in uc02["task_templates"]:
        check_app(f"task {t['template_key']}", t["applicability"])
        for dep in t["depends_on"]:
            if dep not in graph:
                problems.append(f"task {t['template_key']} depends on unknown task '{dep}'")
    if (cycle := _cycle(graph)):
        problems.append("task dependency cycle: " + " -> ".join(cycle))
    categories = {r["category"] for r in uc02["assignment_rules"]}
    for t in uc02["task_templates"]:
        if t["category"] not in categories:
            problems.append(f"task {t['template_key']}: no assignment rule for category '{t['category']}'")
    for m in uc02["milestones"]:
        for k in m["required_task_keys"]:
            if k not in graph:
                problems.append(f"milestone {m['milestone_key']} requires unknown task '{k}'")

    dkeys = [d["document_key"] for d in uc03["documents"]]
    if len(dkeys) != len(set(dkeys)):
        problems.append("duplicate document keys")
    for d in uc03["documents"]:
        check_app(f"document {d['document_key']}", d["applicability"])
        for k in d["depends_on_tasks"] + d["blocks_tasks"]:
            if k not in graph:
                problems.append(f"document {d['document_key']} references unknown task '{k}'")
        if d["mandatory"] != (d["submission"]["requirement"] == "required"):
            problems.append(f"document {d['document_key']}: mandatory flag and submission.requirement disagree")

    equipment = {i["item_code"] for i in uc04["equipment_catalogue"]}
    access = {i["item_code"] for i in uc04["access_catalogue"]}
    for m in uc04["role_equipment_matrix"]:
        if m["role_id"] not in roles:
            problems.append(f"equipment matrix role '{m['role_id']}' is not a supported role")
        problems += [f"equipment matrix item '{i}' is not in the catalogue" for i in m["items"] if i not in equipment]
    for m in uc04["role_access_matrix"]:
        if m["role_id"] not in roles:
            problems.append(f"access matrix role '{m['role_id']}' is not a supported role")
        problems += [f"access matrix item '{i}' is not in the catalogue" for i in m["items"] if i not in access]

    for p in uc05["orientation_programmes"]:
        check_app(f"programme {p['programme_key']}", p["applicability"])
    for p in uc05["policy_acknowledgements"]:
        check_app(f"policy {p['policy_key']}", p["applicability"])
    mapped = {h["uc02_field"] for h in uc01["uc02_handoff_mapping"]}
    for need in ("client_id", "case_id", "employee_id", "joining_date", "role_id", "grade", "nationality_group", "location"):
        if need not in mapped:
            problems.append(f"uc01 hand-off mapping does not provide '{need}' to UC-02")
    cal = doc["shared_configuration"]["calendar"]
    if cal["timezone"] != master["timezone"]:
        problems.append("shared calendar timezone differs from the client timezone")
    return problems


def assert_valid(doc: Mapping[str, Any]) -> None:
    problems = validate_document(doc)
    if problems:
        raise ConfigurationDocumentError("; ".join(problems[:8]))


__all__ = ["CONFIG_DIR", "ConfigurationDocumentError", "PROFILE_DIR", "SCHEMA_PATH", "SECTIONS", "SECTION_CATEGORY", "assert_valid",
           "canonical_json", "document_hash", "load_document", "load_profiles", "load_schema", "validate_document"]
