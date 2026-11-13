"""Tenant-level orchestration settings: the timezone and local run time of the joining-day
scheduler, and how an upstream employee identifier becomes the canonical employee id.

Loaded from `HR-Onboarding-Common/config/tenants.yaml` (or the path in ORCHESTRATOR_TENANT_CONFIG).
This is orchestration configuration, not UC business configuration: UC05's welcome templates and
branding are UC05's own (its ClientConfig).

Canonical employee identifier
-----------------------------
The canonical id is the employee id of the HR system of record -- the id UC05's employee directory
(the future HRMS) uses, so every UC05 operation (scheduling, buddy, welcome) is about the same
person. Upstream, UC01 reports `employee_no`. Per tenant, `employee_identity.scheme` says how one
becomes the other:

  shared   UC01's employee_no IS the HRMS employee id (UC01 writes the employee to the same HRMS).
           The code default, so existing callers keep working; the source is recorded on the case.
  mapped   An explicit table (employee_no -> employee_id) for this tenant. Unlisted -> unresolved.
  none     No upstream mapping: the caller must supply the canonical employee_id itself.

A caller may also supply the canonical `employee_id` explicitly (OnboardingCaseInput.employee_id);
if the scheme also yields one and they differ, that is a conflict. Nothing is ever inferred from a
name or chosen as "the first match"; resolution is always scoped to the case's tenant, so the same
employee_no in two tenants resolves through each tenant's own table.
"""

from __future__ import annotations

import os
import re
from datetime import time
from enum import StrEnum
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_TIMEZONE = os.environ.get("HR_AGENT_TZ", "Asia/Dubai")
DEFAULT_RUN_AT = "06:00"  # the same default as UC05's own joining-day scheduler (SCHEDULER_RUN_AT)
CANONICAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class IdentityScheme(StrEnum):
    SHARED = "shared"
    MAPPED = "mapped"
    NONE = "none"


class EmployeeIdentityError(ValueError):
    """The case's employee cannot be resolved to one canonical id. `code` is stable."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


class EmployeeRef(BaseModel):
    """The resolved identity, stored on the case (`state["employee_ref"]`)."""

    client_id: str
    employee_id: str  # canonical (HRMS) -- what every UC05 operation uses
    employee_no: str | None = None  # as reported upstream (UC01)
    source: Literal["explicit", "mapped", "shared_scheme"]
    scheme: IdentityScheme


class EmployeeMapEntry(BaseModel):
    employee_no: str
    employee_id: str


class EmployeeIdentityConfig(BaseModel):
    scheme: IdentityScheme = IdentityScheme.SHARED
    employee_no_map: list[EmployeeMapEntry] = Field(default_factory=list)  # a list, so duplicates are visible


def _valid_timezone(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone '{value}'") from None
    return value


# UC04's role code shape (valid under both its LOCAL_SIMULATION and PLATFORM identity profiles).
_UC04_ROLE_CODE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class TenantSettings(BaseModel):
    timezone: str | None = None
    joining_day_run_at: str | None = None  # "HH:MM", local time from which a joining day is due
    employee_identity: EmployeeIdentityConfig | None = None
    # Stage dependency overrides, {stage: {dependency key: required|optional}} (dependencies.py): only a
    # configurable optional dependency can be made required; a required one is never relaxed.
    dependencies: dict[str, dict[str, Literal["required", "optional"]]] | None = None
    # This tenant's UC04 CaseConfig (role/access matrices, approvers): client-owned configuration the
    # UC04 stage reads when the case itself names none. A relative path is resolved against this file's
    # directory. Tenant entries only: a `default` value is never applied to another client.
    uc04_client_config_ref: str | None = None
    # Per-role UC04 CaseConfigs, {UC04 role code: path}: UC04 scopes a configuration to one client AND one role
    # (it refuses another role's with CONFIG_SCOPE_MISMATCH), so a client with several roles names one per role.
    # Looked up by the case's role code (adapters.uc04_equipment_access.uc04_role_code) before the single ref above.
    uc04_client_configs: dict[str, str] | None = None

    @field_validator("uc04_client_configs")
    @classmethod
    def _uc04_roles(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        for role in value or {}:
            if not _UC04_ROLE_CODE.match(role):
                raise ValueError(f"uc04_client_configs keys must be UC04 role codes (snake_case), got {role!r}")
        return value

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        return _valid_timezone(value)

    @field_validator("dependencies")
    @classmethod
    def _dependencies(cls, value: dict[str, dict[str, str]] | None) -> dict[str, dict[str, str]] | None:
        if value is not None:
            from hr_onboarding_common.orchestration.dependencies import validate_overrides
            validate_overrides(value)
        return value

    @field_validator("joining_day_run_at")
    @classmethod
    def _run_at(cls, value: str | None) -> str | None:
        if value is not None:
            time.fromisoformat(value)
        return value


class TenantConfig(BaseModel):
    default: TenantSettings = Field(default_factory=TenantSettings)
    tenants: dict[str, TenantSettings] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str | None = None) -> TenantConfig:
        """`path`, else ORCHESTRATOR_TENANT_CONFIG; neither -> code defaults (no tenant entries)."""
        path = path or os.environ.get("ORCHESTRATOR_TENANT_CONFIG")
        if not path:
            return cls()
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        config = cls.model_validate(data)
        base = Path(path).resolve().parent

        def resolved(ref: str) -> str:
            return ref if Path(ref).is_absolute() else str((base / ref).resolve())

        for settings in config.tenants.values():
            if settings.uc04_client_config_ref:
                settings.uc04_client_config_ref = resolved(settings.uc04_client_config_ref)
            if settings.uc04_client_configs:
                settings.uc04_client_configs = {role: resolved(ref)
                                                for role, ref in settings.uc04_client_configs.items()}
        return config

    def uc04_config_ref_for(self, client_id: str, role_code: str | None = None) -> str | None:
        """The tenant's own UC04 configuration reference for `role_code` (never the default's, never another
        role's): its per-role entry, else its single client-wide ref."""
        own = self.tenants.get(client_id)
        if own is None:
            return None
        if role_code and own.uc04_client_configs and role_code in own.uc04_client_configs:
            return own.uc04_client_configs[role_code]
        return own.uc04_client_config_ref

    def _get(self, client_id: str, field: str):
        own = self.tenants.get(client_id)
        value = getattr(own, field) if own is not None else None
        return value if value is not None else getattr(self.default, field)

    def timezone_for(self, client_id: str, case_timezone: str | None = None) -> ZoneInfo:
        """The case's own timezone when it has one, else its tenant's, else the default."""
        return ZoneInfo(case_timezone or self._get(client_id, "timezone") or DEFAULT_TIMEZONE)

    def run_at_for(self, client_id: str) -> time:
        return time.fromisoformat(self._get(client_id, "joining_day_run_at") or DEFAULT_RUN_AT)

    def identity_for(self, client_id: str) -> EmployeeIdentityConfig:
        return self._get(client_id, "employee_identity") or EmployeeIdentityConfig()

    def dependencies_for(self, client_id: str) -> dict[str, dict[str, str]]:
        """The default's dependency overrides with the tenant's own applied on top, per stage."""
        merged: dict[str, dict[str, str]] = {s: dict(d) for s, d in (self.default.dependencies or {}).items()}
        own = self.tenants.get(client_id)
        for stage, by_key in ((own.dependencies if own is not None else None) or {}).items():
            merged.setdefault(stage, {}).update(by_key)
        return merged

    def resolve_employee(self, *, client_id: str, employee_no: str | None, employee_id: str | None) -> EmployeeRef:
        """The canonical employee id for one case, or EmployeeIdentityError with a stable code:
        EMPLOYEE_ID_MALFORMED, EMPLOYEE_ID_AMBIGUOUS, EMPLOYEE_ID_CONFLICT, EMPLOYEE_ID_UNRESOLVED,
        EMPLOYEE_ID_MISSING. Never a guess."""
        identity = self.identity_for(client_id)
        for label, value in (("employee_no", employee_no), ("employee_id", employee_id)):
            if value is not None and not CANONICAL_ID.fullmatch(str(value)):
                raise EmployeeIdentityError("EMPLOYEE_ID_MALFORMED", f"the upstream {label} is not a valid identifier")
        upstream, source = None, None
        if employee_no and identity.scheme == IdentityScheme.MAPPED:
            targets = {e.employee_id for e in identity.employee_no_map if e.employee_no == employee_no}
            if len(targets) > 1:
                raise EmployeeIdentityError("EMPLOYEE_ID_AMBIGUOUS",
                                            "this employee_no maps to more than one employee id for the tenant")
            if targets:
                upstream, source = targets.pop(), "mapped"
                if len({e.employee_no for e in identity.employee_no_map if e.employee_id == upstream}) > 1:
                    raise EmployeeIdentityError("EMPLOYEE_ID_AMBIGUOUS",
                                                "the mapped employee id is mapped from more than one employee_no")
                if not CANONICAL_ID.fullmatch(upstream):
                    raise EmployeeIdentityError("EMPLOYEE_ID_MALFORMED", "the mapped employee id is not valid")
        elif employee_no and identity.scheme == IdentityScheme.SHARED:
            upstream, source = employee_no, "shared_scheme"
        if employee_id and upstream and employee_id != upstream:
            raise EmployeeIdentityError("EMPLOYEE_ID_CONFLICT",
                                        "the supplied employee_id and the upstream employee_no identify different "
                                        "employees")
        if employee_id:
            return EmployeeRef(client_id=client_id, employee_id=employee_id, employee_no=employee_no,
                               source="explicit", scheme=identity.scheme)
        if upstream:
            return EmployeeRef(client_id=client_id, employee_id=upstream, employee_no=employee_no, source=source,
                               scheme=identity.scheme)
        if employee_no:
            raise EmployeeIdentityError("EMPLOYEE_ID_UNRESOLVED",
                                        f"the tenant's identity scheme '{identity.scheme}' does not resolve this "
                                        f"employee_no; add a mapping or supply employee_id")
        raise EmployeeIdentityError("EMPLOYEE_ID_MISSING", "no employee identifier is known for this case")
