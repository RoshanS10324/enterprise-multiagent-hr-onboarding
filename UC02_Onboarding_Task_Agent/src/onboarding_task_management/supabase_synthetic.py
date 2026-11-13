"""The SYNTHETIC development fixture, stored in Supabase and clearly marked, never mistaken for a real client.

A development demonstration needs a UC-01 case, an accepted offer, an approved form, and an approved client configuration. None
of that exists for a made-up employee, so this module builds the rows and (only when explicitly told to) inserts them.

Ownership and safety, stated plainly
* ``onboarding_cases``, ``onboarding_forms``, ``offers`` and ``offer_forms`` belong to UC-01. A real deployment never has UC-02
  write them. Here they are written ONLY by :func:`ensure_fixture`, which the demo's ``setup`` stage runs after an explicit
  ``--confirm-write`` naming the target, and which refuses without ``UC02_ALLOW_FIXTURE_WRITES=1`` and ``UC02_ENV=development``.
* Every row is insert-if-missing by natural key. Nothing existing is updated, overwritten or deleted.
* Marking: ``client_configs.settings_payload.data_origin = "synthetic"`` and the id/name prefix ``SYN-`` / ``SYNTHETIC``. The config
  reader refuses a synthetic client unless a development runtime opts in (``UC02_ALLOW_SYNTHETIC_CONFIG=1``), and a production
  runtime never accepts one. The records UC-02 writes carry ``data_origin = "synthetic"`` too.
* The task templates, document requirements, team mapping and holidays are the synthetic profile of
  :mod:`synthetic_profiles`. They prove the machinery, not any real client's rules.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from onboarding_task_management import synthetic_profiles as sp
from onboarding_task_management.errors import PreconditionError
from onboarding_task_management.supabase_rest import SupabaseRestClient

_NS = uuid.UUID("7c0f2a54-9d51-4c9e-8a4e-5a11c02f1003")
FIXTURE_TABLES = ("client_configs", "client_config_versions", "task_templates", "client_document_requirements",
                  "client_service_policies", "client_holidays", "onboarding_cases", "offers", "onboarding_forms",
                  "offer_forms")
SYNTHETIC_MARK = "synthetic"


def _uuid(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "|".join(parts)))


@dataclass(frozen=True)
class FixtureSpec:
    client_id: str = sp.UAE_CLIENT
    profile: str = "uae_inside"
    employee_no: str = "SYN-EMP-0001"
    joining_date: date = field(default_factory=lambda: next_working_day(date.today() + timedelta(days=21)))

    @property
    def case_id(self) -> str:
        return _uuid("case", self.client_id, self.employee_no)

    @property
    def form_id(self) -> str:
        return _uuid("form", self.client_id, self.employee_no)

    @property
    def offer_ref(self) -> str:
        return f"SYN-OFFER-{self.employee_no}"


def next_working_day(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def build_rows(spec: FixtureSpec) -> dict[str, list[dict[str, Any]]]:
    """Every fixture row, keyed by table. Pure; writes nothing."""
    config = sp.uae_config()
    profile = sp.PROFILES[spec.profile]
    version = 1
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    templates = [{
        "template_key": t.key, "client_id": spec.client_id, "category": t.category.value, "title": t.title,
        "title_ar": t.title_ar, "owner_role": t.owner.value, "offset_days": t.offset_days,
        "depends_on_template_keys": list(t.depends_on), "sensitive_to": [d.value for d in t.sensitive_to],
        "role_ids": list(t.role_ids), "grades": list(t.grades), "locations": list(t.locations),
        "projects": list(t.projects), "nationality_groups": list(t.nationality_groups),
        "requires_attributes": list(t.requires_attributes), "evidence_required": t.evidence_required,
        "approval_role": t.approval_role.value if t.approval_role else None, "default_priority": t.priority,
        "config_version": version, "is_active": True} for t in config.templates]
    documents = [{
        "requirement_id": _uuid("doc", spec.client_id, d.key), "client_id": spec.client_id, "config_version": version,
        "service_category": "documents", "requirement_key": d.key, "document_type": d.document_type,
        "description": d.reason, "is_mandatory": d.mandatory, "applicable_roles": list(d.role_ids),
        "applicable_grades": list(d.grades), "applicable_locations": list(d.locations),
        "applicable_projects": list(d.projects),
        "eligibility_rules": {"nationality_groups": list(d.nationality_groups),
                              "requires_attributes": list(d.requires_attributes),
                              "allow_not_applicable": d.allow_not_applicable},
        "validation_profile": {}, "is_active": True} for d in config.document_requirements]
    policies = [{
        "policy_id": _uuid("policy", spec.client_id, a.category.value), "client_id": spec.client_id,
        "config_version": version, "service_category": "uc02_assignment", "service_key": a.category.value,
        "is_available": True, "is_mandatory": False, "eligibility_rules": {},
        "policy_details": {"team": a.team, "use_case": a.use_case, "mode": a.mode,
                           "escalation_owner": a.escalation_owner.value if a.escalation_owner else None},
        "responsible_role": None, "is_active": True} for a in config.assignments]
    holidays = [{"client_id": spec.client_id, "holiday_date": d.isoformat(), "name": "SYNTHETIC holiday",
                 "is_observed": True} for d in (date(2026, 12, 2), date(2026, 12, 3))]
    attributes = list(profile.attributes) + ([f"location_status:{profile.current_location_status}"]
                                             if profile.current_location_status else [])
    return {
        "client_configs": [{
            "client_id": spec.client_id, "company_name": "SYNTHETIC TEST CLIENT (not a real client)",
            "config_hash": f"synthetic-{_uuid('hash', spec.client_id)[:8]}", "working_days": [1, 2, 3, 4, 5],
            "timezone": "Asia/Dubai", "template_count": len(templates), "milestone_count": 0, "is_active": True,
            "settings_payload": {"data_origin": SYNTHETIC_MARK, "country": "synthetic",
                                 "note": "test-only fixture; never a real client configuration"}}],
        "client_config_versions": [{
            "client_id": spec.client_id, "version": version, "effective_from": "2026-01-01", "status": "approved",
            "config_hash": f"synthetic-{_uuid('hash', spec.client_id)[:8]}", "approved_by": "synthetic-fixture",
            "approved_at": moment}],
        "task_templates": templates, "client_document_requirements": documents, "client_service_policies": policies,
        "client_holidays": holidays,
        "onboarding_cases": [{
            "case_id": spec.case_id, "client_id": spec.client_id, "employee_no": spec.employee_no,
            "candidate_name": "SYNTHETIC CANDIDATE", "candidate_email": "synthetic.candidate@example.invalid",
            "hr_officer_id": "SYNTHETIC-HR-OFFICER", "joining_date": spec.joining_date.isoformat(),
            "work_location_type": "OFFICE", "location_name": profile.location, "project_name": "synthetic-project",
            "role_id": profile.role_id, "grade": profile.grade, "department": "Engineering",
            "manager_id": f"SYN-MGR-{spec.client_id}", "nationality_group": profile.nationality_group,
            "onboarding_attributes": attributes}],
        "offers": [{
            "client_id": spec.client_id, "offer_ref": spec.offer_ref, "candidate_name": "SYNTHETIC CANDIDATE",
            "candidate_email": "synthetic.candidate@example.invalid", "designation": "SYNTHETIC ENGINEER",
            "hr_officer_id": "SYNTHETIC-HR-OFFICER", "status": "ACCEPTED", "accepted_at": "2026-10-20T00:00:00+00:00",
            "offer_payload": {"data_origin": SYNTHETIC_MARK}}],
        "onboarding_forms": [{
            "form_id": spec.form_id, "client_id": spec.client_id, "case_id": spec.case_id, "version": 1,
            "candidate_name": "SYNTHETIC CANDIDATE", "candidate_email": "synthetic.candidate@example.invalid",
            "designation": "SYNTHETIC ENGINEER", "joining_date": spec.joining_date.isoformat(),
            "work_location_type": "OFFICE", "salary_and_terms_encrypted": "synthetic-placeholder",
            "status": "APPROVED", "approved_by": "SYNTHETIC-HR-OFFICER", "approved_at": "2026-10-22T00:00:00+00:00"}],
        "offer_forms": [{"client_id": spec.client_id, "offer_ref": spec.offer_ref, "form_id": spec.form_id}],
    }


_NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "client_configs": ("client_id",), "client_config_versions": ("client_id", "version"),
    "task_templates": ("client_id", "template_key", "config_version"),
    "client_document_requirements": ("client_id", "config_version", "requirement_key"),
    "client_service_policies": ("client_id", "config_version", "service_category", "service_key"),
    "client_holidays": ("client_id", "holiday_date"), "onboarding_cases": ("case_id",),
    "offers": ("client_id", "offer_ref"), "onboarding_forms": ("case_id", "client_id", "version"),
    "offer_forms": ("client_id", "offer_ref", "form_id"),
}


def _guard(environ: Mapping[str, str]) -> None:
    if (environ.get("UC02_ENV") or "").strip().lower() != "development" or environ.get("UC02_ALLOW_FIXTURE_WRITES") != "1":
        raise PreconditionError("fixture writes need UC02_ENV=development and UC02_ALLOW_FIXTURE_WRITES=1; refused")


def fixture_status(rest: SupabaseRestClient, spec: FixtureSpec) -> dict[str, dict[str, int]]:
    """READ-ONLY: for each fixture table, how many rows already exist and how many are missing."""
    out = {}
    for table, rows in build_rows(spec).items():
        present = sum(1 for r in rows if rest.select(table, {k: r[k] for k in _NATURAL_KEYS[table]}, columns=_NATURAL_KEYS[table][0]))
        out[table] = {"present": present, "missing": len(rows) - present}
    return out


def ensure_fixture(rest: SupabaseRestClient, spec: FixtureSpec, environ: Mapping[str, str] | None = None) -> dict[str, dict[str, int]]:
    """Insert every MISSING fixture row (never update or delete). Refused outside an explicit development opt-in."""
    _guard(os.environ if environ is None else environ)
    result = {}
    for table in FIXTURE_TABLES:
        rows = build_rows(spec)[table]
        added = existing = 0
        for row in rows:
            key = {k: row[k] for k in _NATURAL_KEYS[table]}
            if rest.select(table, key, columns=_NATURAL_KEYS[table][0]):
                existing += 1
            else:
                rest.insert(table, [row])
                added += 1
        result[table] = {"inserted": added, "already_present": existing}
    return result


__all__ = ["FIXTURE_TABLES", "FixtureSpec", "SYNTHETIC_MARK", "build_rows", "ensure_fixture", "fixture_status",
           "next_working_day"]
