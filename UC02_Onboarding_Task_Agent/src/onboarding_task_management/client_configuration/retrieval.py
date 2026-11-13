"""Central client-configuration retrieval: one client, one in-force version, one employee -> a normalized, employee-specific configuration.

``retrieve(rest, EmployeeProfile(...))`` reads (SELECT only):

1. the client row and the in-force approved version, through the same gates as UC-02's reader (missing / inactive client, mock or synthetic
   client without the runtime opt-in, draft / retired / expired / future / unapproved version);
2. the six stored section documents of that version (``client_service_policies``, ``service_category = 'cfg_section'``), checked for completeness, for
   a consistent ``sections_hash`` and against the shared JSON Schema;
3. the employee's eligibility against the client's own catalogue (``uc01_employee_record``): work country and location, role, grade, nationality
   (mapped to the client's nationality group), employment type. Anything outside it raises ``EmployeeNotEligibleError`` naming the dimension;
4. the required checklist attributes (for example ``visa_route``, ``work_authorization``, ``work_state``): a missing one is returned as an HR-review
   item, never silently dropped;
5. the employee slices: applicable tasks, documents, immigration / medical / insurance / payroll tasks, equipment and access items, orientation and
   policy items, milestones, calendar and holidays, each with the reason it applies; and the excluded tasks and documents with why.

It never falls back to another client or version, and nothing is guessed: a gap is an error or a review item.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from onboarding_task_management.client_configuration.document import SECTION_CATEGORY, SECTIONS, canonical_json, load_schema
from onboarding_task_management.contracts import ChecklistContext
from onboarding_task_management.errors import EmployeeNotEligibleError, MissingConfigurationError
from onboarding_task_management.supabase_config import SupabaseConfigSource
from onboarding_task_management.supabase_rest import SupabaseRestClient

_SCHEMA_DEF = {"shared_configuration": "shared_configuration", "uc01_employee_record": "uc01", "uc02_onboarding_tasks": "uc02",
               "uc03_document_validation": "uc03", "uc04_equipment_access": "uc04", "uc05_orientation_policy": "uc05"}


@dataclass(frozen=True)
class EmployeeProfile:
    """IDs and codes only; no personal data."""

    client_id: str
    joining_date: date
    work_country: str
    work_location: str
    nationality: str
    role: str
    grade: str
    employment_type: str
    #: ``<family>:<value>`` facts and flags, e.g. ``visa_route:overseas_entry``, ``work_authorization:citizen``, ``work_state:XX``, ``family_cover``.
    attributes: tuple[str, ...] = ()
    project: str = ""


@dataclass
class ClientConfigurationResponse:
    client_id: str
    configuration_version: int
    data_origin: str
    nationality_group: str
    review_required: list[str]
    sections: dict[str, Any]
    applicable: dict[str, list[dict[str, Any]]]
    excluded: dict[str, list[dict[str, Any]]]
    calendar: dict[str, Any]
    attributes: tuple[str, ...] = field(default_factory=tuple)

    def checklist_context(self, profile: EmployeeProfile) -> ChecklistContext:
        """The UC-02 checklist context for this employee, built from the validated profile (same attribute tags as the stored filters)."""
        return ChecklistContext(client_id=self.client_id, joining_date=profile.joining_date, location=profile.work_location,
                                project=profile.project, role_id=profile.role, grade=profile.grade, nationality_group=self.nationality_group,
                                attributes=list(self.attributes))


def _sections_hash(sections: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json({s: sections[s] for s in SECTIONS}).encode("utf-8")).hexdigest()


def _validate_sections(sections: Mapping[str, Any]) -> list[str]:
    import jsonschema  # type: ignore[import-untyped]

    schema = load_schema()
    problems = []
    for name, ref in _SCHEMA_DEF.items():
        v = jsonschema.Draft202012Validator({"$defs": schema["$defs"], "$ref": f"#/$defs/{ref}"})
        problems += [f"{name}: {'.'.join(map(str, e.path)) or '<root>'}: {e.message[:120]}" for e in v.iter_errors(sections[name])]
    return problems


def matches(applicability: Mapping[str, Any], *, role: str, grade: str, group: str, location_values: Sequence[str], attributes: Sequence[str],
            project: str = "") -> tuple[bool, str]:
    """The same rule the stored filters use: an empty list means "all"; every required attribute must be present; none excluded."""
    for dim, value in (("roles", role), ("grades", grade), ("nationality_groups", group), ("projects", project)):
        allowed = applicability.get(dim) or []
        if allowed and value not in allowed:
            return False, f"excluded_by_{dim}"
    allowed_loc = applicability.get("locations") or []
    if allowed_loc and not set(allowed_loc) & set(location_values):
        return False, "excluded_by_locations"
    etypes = applicability.get("employment_types") or []
    if etypes and not any(f"employment_type:{t}" in attributes for t in etypes):
        return False, "excluded_by_employment_types"
    missing = [a for a in applicability.get("requires_attributes") or [] if a not in attributes]
    if missing:
        return False, "excluded_by_attributes:" + ",".join(missing)
    if any(a in attributes for a in applicability.get("excludes_attributes") or []):
        return False, "excluded_by_excluded_attribute"
    narrowed = any(applicability.get(d) for d in ("roles", "grades", "nationality_groups", "locations", "employment_types", "projects", "requires_attributes"))
    return True, "matched_all_filters" if narrowed else "applies_to_all"


def retrieve(rest: SupabaseRestClient, profile: EmployeeProfile, *, allow_synthetic: bool = False,
             environ: Mapping[str, str] | None = None) -> ClientConfigurationResponse:
    """``allow_synthetic=True`` is honoured only in an explicit development/test environment that opted in (checked by the reader);
    otherwise it raises ``UnsafeConfigurationError``."""
    source = SupabaseConfigSource(rest, allow_synthetic=allow_synthetic, environ=environ)
    client_row = source._client_row(profile.client_id)
    version_row = source._version_row(profile.client_id, profile.joining_date)
    version = int(version_row["version"])
    rows = rest.select("client_service_policies", {"client_id": profile.client_id, "config_version": version,
                                                   "service_category": SECTION_CATEGORY, "is_active": "true"})
    stored = {r["service_key"]: r.get("policy_details") or {} for r in rows if r.get("client_id") == profile.client_id}
    missing = [s for s in SECTIONS if s not in stored]
    if missing:
        raise MissingConfigurationError("the configuration version has no complete section set", client_id=profile.client_id,
                                        version=version, missing=", ".join(missing))
    sections = {s: stored[s]["document"] for s in SECTIONS}
    hashes = {stored[s].get("sections_hash") for s in SECTIONS}
    if len(hashes) != 1 or hashes.pop() != _sections_hash(sections):
        raise MissingConfigurationError("the stored sections are inconsistent (sections_hash mismatch)", client_id=profile.client_id, version=version)
    if any(stored[s].get("document_hash") != version_row.get("config_hash") for s in SECTIONS):
        raise MissingConfigurationError("the stored sections belong to a different document than the version", client_id=profile.client_id,
                                        version=version)
    problems = _validate_sections(sections)
    if problems:
        raise MissingConfigurationError("the stored configuration is invalid", client_id=profile.client_id, version=version,
                                        problems="; ".join(problems[:5]))

    uc01, uc02, uc03, uc04, uc05 = (sections[s] for s in ("uc01_employee_record", "uc02_onboarding_tasks", "uc03_document_validation",
                                                          "uc04_equipment_access", "uc05_orientation_policy"))
    # -- eligibility (strict; the client's own catalogue)
    errors: list[str] = []
    locs = uc01.get("work_locations") or []
    loc = next((x for x in locs if profile.work_location in (x["code"], x["name"])), None)
    if locs and loc is None:
        errors.append(f"work location {profile.work_location!r} is not supported")
    if loc is not None and profile.work_country not in (loc["country_code"],):
        errors.append(f"work country {profile.work_country!r} does not match the location's country {loc['country_code']!r}")
    roles = {r["role_id"] for r in uc01["supported_roles"]}
    if profile.role not in roles:
        errors.append(f"role {profile.role!r} is not supported")
    grades = {g["grade_id"] for g in uc01["grades"]}
    if profile.grade not in grades:
        errors.append(f"grade {profile.grade!r} is not supported")
    group = next((g["group_code"] for g in uc01["nationality_groups"] if profile.nationality in g["nationality_codes"]), "")
    if not group:
        errors.append(f"nationality {profile.nationality!r} is not in any of the client's nationality groups")
    if profile.employment_type not in uc01["employment_types"]:
        errors.append(f"employment type {profile.employment_type!r} is not supported")
    if errors:
        raise EmployeeNotEligibleError("the employee is outside the client's configured eligibility", client_id=profile.client_id,
                                       problems="; ".join(errors))
    attributes = tuple(dict.fromkeys([*profile.attributes, f"employment_type:{profile.employment_type}"]))
    review = []
    stated = {a.split(":", 1)[0]: a.split(":", 1)[1] for a in attributes if ":" in a}
    for fam in uc01.get("checklist_attributes") or []:
        if fam["family"] not in stated:
            if fam["required"]:
                review.append(f"{fam['family']} is not stated: {fam['reason']}")
        elif fam["values"] and stated[fam["family"]] not in fam["values"]:
            review.append(f"{fam['family']} value {stated[fam['family']]!r} is not one of {fam['values']}")

    location_values = [profile.work_location] + ([loc["code"], loc["name"]] if loc else [])

    def pick(items, key):
        inc, exc = [], []
        for x in items:
            ok, why = matches(x["applicability"], role=profile.role, grade=profile.grade, group=group, location_values=location_values,
                              attributes=attributes, project=profile.project)
            (inc if ok else exc).append({"key": x[key], "reason": why, **{k: x[k] for k in ("category", "mandatory", "owner_role", "depends_on",
                                                                                              "responsible_team", "blocking", "awaiting_confirmation",
                                                                                              "name", "title") if k in x}})
        return inc, exc

    tasks, tasks_x = pick(uc02["task_templates"], "template_key")
    docs, docs_x = pick(uc03["documents"], "document_key")
    programmes, _ = pick(uc05["orientation_programmes"], "programme_key")
    policies, _ = pick(uc05["policy_acknowledgements"], "policy_key")

    def by_cat(*cats):
        return [t for t in tasks if t.get("category") in cats]

    def items(matrix, catalogue):
        codes = [c for m in matrix if m["role_id"] == profile.role and (not m.get("grades") or profile.grade in m["grades"]) for c in m["items"]]
        index = {i["item_code"]: i for i in catalogue}
        return [{"key": c, **{k: index[c][k] for k in index[c] if k != "item_code"}} for c in dict.fromkeys(codes)]

    return ClientConfigurationResponse(
        client_id=profile.client_id, configuration_version=version,
        data_origin="synthetic" if (client_row.get("settings_payload") or {}).get("data_origin") == "synthetic"
        or (client_row.get("settings_payload") or {}).get("environment") == "mock" else "approved_client",
        nationality_group=group, review_required=review, sections=sections, attributes=attributes,
        applicable={"tasks": tasks, "documents": docs, "immigration_tasks": by_cat("visa", "biometrics") + [t for t in tasks if t["key"] in (
                        "work_authorization_check", "employment_eligibility_verification")],
                    "medical_tasks": by_cat("medical"), "insurance_tasks": by_cat("insurance"), "payroll_tasks": by_cat("payroll", "banking_letter"),
                    "equipment": items(uc04["role_equipment_matrix"], uc04["equipment_catalogue"]),
                    "access": items(uc04["role_access_matrix"], uc04["access_catalogue"]),
                    "orientation": programmes, "policies": policies, "milestones": list(uc02["milestones"]),
                    "assignment_rules": list(uc02["assignment_rules"])},
        excluded={"tasks": tasks_x, "documents": docs_x},
        calendar=dict(sections["shared_configuration"]["calendar"]),
    )


__all__ = ["ClientConfigurationResponse", "EmployeeProfile", "matches", "retrieve"]
