"""UC04's equipment/access configuration source: `file` (the per-tenant reference in config/tenants.yaml -- synthetic
JSON files) or `supabase` (the tenant's versioned client configuration in Supabase).

`UC04_CONFIG_SOURCE` (orchestrator process; default `file`). In `supabase` mode no file is read. The client's
`uc04_equipment_access` configuration section lives in `client_service_policies` (`service_category='cfg_section'`,
`service_key='uc04_equipment_access'`, one row per `config_version`). Its shape (role matrices with grades,
catalogues, prerequisites, lower-case evidence roles; no MISSING / VERIFIED_EMPTY block states, no work calendar) is
not UC04's `CaseConfig`, and the projection from one to the other is an open UC04 decision (UC04
docs/common_patches/README.md, D-1/D-2). Until that projection exists the stage is BLOCKED, naming exactly what is
there and what is missing -- the synthetic files are never used in its place and nothing is projected by guesswork.

Reads use the orchestrator's own scoped settings (ORCHESTRATOR_SUPABASE_URL / _SERVICE_ROLE_KEY / _SCHEMA, then the
plain names); the key never leaves this process.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

SOURCES = ("file", "supabase")
TABLE = "client_service_policies"
SECTION = "uc04_equipment_access"


class Uc04ConfigUnavailable(RuntimeError):
    """The configuration could not be read (Supabase unreachable / refused). Retryable; never a 'missing' verdict."""


def config_source(environ: Mapping[str, str]) -> str:
    source = (environ.get("UC04_CONFIG_SOURCE") or "file").strip().lower() or "file"
    if source not in SOURCES:
        raise ValueError(f"UC04_CONFIG_SOURCE={source!r} is not one of {list(SOURCES)}")
    return source


def _setting(environ: Mapping[str, str], name: str) -> str:
    return (environ.get(f"ORCHESTRATOR_{name}") or environ.get(name) or "").strip()


class SupabaseUc04ConfigReader:
    """`(client_id) -> {"config_version": int, "is_active": bool} | None`: the newest active `uc04_equipment_access`
    section of the tenant, metadata only (the section's content is not needed to know it cannot be used yet)."""

    def __init__(self, *, url: str, service_role_key: str, schema: str = "public",
                 transport: httpx.BaseTransport | None = None, timeout_s: float = 15.0):
        if not url or not service_role_key:
            raise ValueError("UC04_CONFIG_SOURCE=supabase needs ORCHESTRATOR_SUPABASE_URL and "
                             "ORCHESTRATOR_SUPABASE_SERVICE_ROLE_KEY (or the plain SUPABASE_* names)")
        self._client = httpx.Client(base_url=url.rstrip("/") + "/rest/v1", timeout=timeout_s, transport=transport,
                                    headers={"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}",
                                             "Accept-Profile": schema})

    @classmethod
    def from_env(cls, environ: Mapping[str, str], *, transport: httpx.BaseTransport | None = None):
        return cls(url=_setting(environ, "SUPABASE_URL"), service_role_key=_setting(environ, "SUPABASE_SERVICE_ROLE_KEY"),
                   schema=_setting(environ, "SUPABASE_SCHEMA") or "public", transport=transport)

    def __call__(self, client_id: str) -> dict[str, Any] | None:
        try:
            response = self._client.get(f"/{TABLE}", params={
                "select": "config_version,is_active", "client_id": f"eq.{client_id}",
                "service_category": "eq.cfg_section", "service_key": f"eq.{SECTION}", "is_active": "is.true",
                "order": "config_version.desc", "limit": "1"})
        except httpx.HTTPError:
            raise Uc04ConfigUnavailable("Supabase unreachable") from None
        if response.status_code != 200:
            raise Uc04ConfigUnavailable(f"Supabase answered HTTP {response.status_code}")
        rows = response.json() or []
        return dict(rows[0]) if rows else None


def blocked_details(client_id: str, section: dict[str, Any] | None) -> tuple[str, str, dict[str, str]]:
    """(code, message, {required_table, required_record, required_configuration}) for UC04 in Supabase mode."""
    record = (f"{TABLE}: client_id={client_id}, service_category=cfg_section, service_key={SECTION}, is_active=true")
    if section is None:
        return ("UC04_CONFIGURATION_MISSING",
                f"no active {SECTION} configuration section exists in Supabase for client {client_id}",
                {"required_table": TABLE, "required_record": record,
                 "required_configuration": f"the client's approved {SECTION} section"})
    return ("UC04_CONFIG_PROJECTION_UNDECIDED",
            f"Supabase holds the {SECTION} section for client {client_id} (config_version "
            f"{section.get('config_version')}), but no approved projection to UC04's CaseConfig exists (UC04 decisions "
            f"D-1/D-2): UC04 is not evaluated on a guessed configuration, and synthetic files are not used",
            {"required_table": TABLE, "required_record": record,
             "required_configuration": f"approved projection {SECTION} -> UC04 CaseConfig (per role: block states, "
                                       f"approvers, calendar, handover, evidence roles, site rules)"})
