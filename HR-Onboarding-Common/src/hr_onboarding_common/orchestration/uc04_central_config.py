"""UC04's client configuration from the central Supabase client configuration (read only).

UC04 never reads configuration itself: its core takes a `CaseConfig` inline (UC04 CLAUDE.md; its packaging and config
tests forbid file and network access). The orchestrator chooses where that configuration comes from:

  UC04_CONFIG_SOURCE=file     (default) the JSON file named by the case (`uc04_client_config_ref`) or by
                              config/tenants.yaml, exactly as before
  UC04_CONFIG_SOURCE=central  the client's in-force approved configuration version in Supabase. No fallback to a file:
                              any problem blocks the stage with a specific code

Central read, through the shared configuration reader of the UC02 package (the one validated reader of the central
configuration; imported in-process like the UC02 adapter, not changed):

  1. client_configs                 the client row: present, active, and allowed by the shared safety policy
                                    (mock or synthetic clients only where UC02_ENV=development|test and
                                    UC02_ALLOW_SYNTHETIC_CONFIG=1)
  2. client_config_versions         the version in force on the joining date: approved, with approver and timestamp,
                                    a config_hash, and an effective window containing the date
  3. client_service_policies        the six section documents of that version (service_category = 'cfg_section'):
                                    complete, one consistent sections_hash, document_hash equal to the version's
                                    config_hash, and the two sections UC04 uses valid against the shared JSON Schema

Then `to_case_config` maps the `uc04_equipment_access` and `shared_configuration` sections onto UC04's CaseConfig
shape. Nothing is invented: a value the central document does not hold stays MISSING (UC04 decides what that means
for the case), and a value that cannot be mapped exactly is refused, never approximated.

Tenant isolation: every query filters on the case's client_id, and every returned row is checked to belong to that
client and version; a row of another client is refused, never filtered away silently. Nothing is cached across calls.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

SOURCE_ENV = "UC04_CONFIG_SOURCE"
SOURCE_FILE, SOURCE_CENTRAL = "file", "central"
SECTION_UC04, SECTION_SHARED = "uc04_equipment_access", "shared_configuration"

# Error codes (the stage blocker code). Retryable: a transient database problem; the others need a person.
MISSING = "UC04_CONFIGURATION_MISSING"
INVALID = "UC04_CONFIGURATION_INVALID"
NOT_PERMITTED = "UC04_CONFIGURATION_NOT_PERMITTED"
ROLE_NOT_CONFIGURED = "UC04_ROLE_NOT_CONFIGURED"
GRADE_REQUIRED = "UC04_GRADE_REQUIRED"
UNAVAILABLE = "UC04_CENTRAL_CONFIG_UNAVAILABLE"
DENIED = "UC04_CENTRAL_CONFIG_DENIED"
NOT_CONFIGURED = "UC04_CENTRAL_CONFIG_NOT_CONFIGURED"
SOURCE_INVALID = "UC04_CONFIG_SOURCE_INVALID"
SOURCE_CONFLICT = "UC04_CONFIG_SOURCE_CONFLICT"

APPROVAL_CLASSES = ("EQUIPMENT", "ACCESS", "EXCEPTION")  # UC04 states.ApprovalClass
LEAD_KEYS = ("EQUIPMENT", "ACCESS", "CLIENT_SITE_CLEARANCE")  # UC04 states.LeadKey
HANDOVER_MODES = ("COLLECT", "DELIVER", "READY_AT_DESK")  # UC04 states.HandoverMode
# Central evidence_roles keys (lower case) -> UC04 states.EvidenceType.
EVIDENCE_TYPES = {"provisioning": "PROVISIONING", "delivery": "DELIVERY", "collection": "COLLECTION",
                  "client_site_clearance": "CLIENT_SITE_CLEARANCE"}
# UC04 contracts.CountryRules fields the central document may carry. Its calendar fields (timezone, weekend_days,
# holidays) are not accepted from the central country_rules: their day numbering is not defined there.
COUNTRY_FIELDS = {"country_code", "courier_extra_days", "remote_first_day_required"}
CLIENT_SITE_FIELDS = {"required_items", "block_on_late_clearance", "owner_role"}  # UC04 contracts.ClientSiteRules


class Uc04ConfigError(Exception):
    """The configuration could not be resolved. `code` is the stage blocker code; `detail` names the client, the
    version and the reason -- never a credential or a configuration value beyond identifiers."""

    def __init__(self, code: str, detail: str, *, retryable: bool = False):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail, self.retryable = code, detail, retryable


@dataclass(frozen=True)
class CentralUc04Config:
    config: dict[str, Any]  # UC04 CaseConfig (JSON form)
    client_id: str
    version: int
    config_hash: str
    sections_hash: str
    data_origin: str  # "approved_client" or "test_data" (mock or synthetic client)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def provenance(self) -> dict[str, Any]:
        """What the result records about the configuration it was evaluated against (identifiers only)."""
        return {"source": SOURCE_CENTRAL, "client_id": self.client_id, "version": self.version,
                "config_hash": self.config_hash, "sections_hash": self.sections_hash, "data_origin": self.data_origin,
                "notes": list(self.notes)}


def config_source(environ: Mapping[str, str]) -> str:
    """The configured source. Unset means `file` (the established behaviour); anything else but the two names is refused."""
    value = (environ.get(SOURCE_ENV) or "").strip().lower()
    if value in ("", SOURCE_FILE):
        return SOURCE_FILE
    if value == SOURCE_CENTRAL:
        return SOURCE_CENTRAL
    raise Uc04ConfigError(SOURCE_INVALID, f"{SOURCE_ENV} must be '{SOURCE_FILE}' or '{SOURCE_CENTRAL}'")


# --------------------------------------------------------------------------- mapping (pure)


def _weekday_from_iso(day: Any, where: str) -> int:
    """Central day numbers are ISO 8601 (1 = Monday ... 7 = Sunday; UC02's reader also accepts 0 for Sunday); UC04
    counts 0 = Monday ... 6 = Sunday."""
    if not isinstance(day, int) or isinstance(day, bool) or day not in range(0, 8):
        raise Uc04ConfigError(INVALID, f"{where}: {day!r} is not a day number 1-7")
    return 6 if day in (0, 7) else day - 1


def _work_calendar(calendar: Mapping[str, Any]) -> dict[str, Any]:
    working = {_weekday_from_iso(d, "calendar.working_days") for d in calendar.get("working_days") or []}
    if not working:
        raise Uc04ConfigError(INVALID, "calendar.working_days is empty")
    weekend = sorted(set(range(7)) - working)
    stated = calendar.get("weekend_days")
    if stated is not None and sorted({_weekday_from_iso(d, "calendar.weekend_days") for d in stated}) != weekend:
        raise Uc04ConfigError(INVALID, "calendar.weekend_days contradicts calendar.working_days")
    holidays = []
    for h in calendar.get("holidays") or []:
        if not isinstance(h, Mapping) or not isinstance(h.get("observed"), bool) or not isinstance(h.get("date"), str):
            raise Uc04ConfigError(INVALID, "calendar.holidays has an entry without a date and an observed flag")
        try:
            day = date.fromisoformat(h["date"])
        except ValueError:
            raise Uc04ConfigError(INVALID, f"calendar.holidays has an invalid date {h['date']!r}") from None
        if h["observed"]:
            holidays.append(day.isoformat())
    return {"weekend_days": weekend, "holidays": sorted(set(holidays))}


def _role_items(matrix: Any, catalogue: Any, *, kind: str, role: str, grade: str | None) -> list[str] | None:
    """The item codes the client's matrix gives this role (and grade): None when the role has no entry in this matrix."""
    if not isinstance(matrix, list) or not isinstance(catalogue, list):
        raise Uc04ConfigError(INVALID, f"the {kind} matrix or catalogue is not a list")
    known = {}
    for item in catalogue:
        if not isinstance(item, Mapping) or not isinstance(item.get("item_code"), str):
            raise Uc04ConfigError(INVALID, f"the {kind} catalogue has an entry without an item_code")
        if item.get("kind") != kind:
            raise Uc04ConfigError(INVALID, f"catalogue item {item['item_code']!r} is not of kind {kind!r}")
        known[item["item_code"]] = item
    entries = [m for m in matrix if isinstance(m, Mapping) and m.get("role_id") == role]
    if not entries:
        return None
    applicable = []
    for entry in entries:
        grades = entry.get("grades") or []
        if grades:
            if not grade:
                raise Uc04ConfigError(GRADE_REQUIRED, f"the {kind} matrix for role {role!r} depends on the grade "
                                                      f"({', '.join(map(str, grades))}) and the case has none")
            if grade not in grades:
                continue
        applicable.append(entry)
    if not applicable:
        raise Uc04ConfigError(ROLE_NOT_CONFIGURED, f"the {kind} matrix has no entry for role {role!r} at grade {grade!r}")
    codes: list[str] = []
    for entry in applicable:
        items = entry.get("items")
        if not isinstance(items, list) or not all(isinstance(c, str) for c in items):
            raise Uc04ConfigError(INVALID, f"the {kind} matrix entry for role {role!r} has no item list")
        for code in items:
            if code not in known:
                raise Uc04ConfigError(INVALID, f"the {kind} matrix names {code!r}, which is not in the {kind} catalogue")
            if code not in codes:
                codes.append(code)
    return codes


def _verified(value: Any) -> dict[str, Any]:
    return {"state": "VERIFIED", "value": value}


def _exact_keys(value: Any, *, allowed: set[str], required: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise Uc04ConfigError(INVALID, f"{where} is not an object")
    extra, missing = set(value) - allowed, required - set(value)
    if extra or missing:
        raise Uc04ConfigError(INVALID, f"{where} cannot be mapped exactly"
                                       + (f"; unknown: {', '.join(sorted(extra))}" if extra else "")
                                       + (f"; missing: {', '.join(sorted(missing))}" if missing else ""))
    return dict(value)


def to_case_config(sections: Mapping[str, Any], *, client_id: str, role: str, grade: str | None,
                   version: int) -> dict[str, Any]:
    """The UC04 CaseConfig (JSON form) for one client, role and grade from the central sections. Pure: no I/O."""
    uc04, shared = sections[SECTION_UC04], sections[SECTION_SHARED]
    equipment = _role_items(uc04["role_equipment_matrix"], uc04["equipment_catalogue"], kind="equipment", role=role,
                            grade=grade)
    access = _role_items(uc04["role_access_matrix"], uc04["access_catalogue"], kind="access", role=role, grade=grade)
    if equipment is None and access is None:
        raise Uc04ConfigError(ROLE_NOT_CONFIGURED, f"role {role!r} is in neither the equipment nor the access matrix "
                                                   f"of client {client_id}")

    def matrix_block(codes: list[str] | None) -> dict[str, Any]:
        # The role is configured (it is in the other matrix) but has no standard items of this kind: verified empty.
        return {"state": "VERIFIED_EMPTY"} if not codes else _verified(codes)

    approvers = _exact_keys(uc04["approver_config"], allowed=set(APPROVAL_CLASSES), required=set(APPROVAL_CLASSES),
                            where="approver_config")
    approver_config = {}
    for cls in APPROVAL_CLASSES:
        entry = _exact_keys(approvers[cls], allowed={"role", "delegate_role"}, required={"role"},
                            where=f"approver_config.{cls}")
        approver_config[cls] = {"role": entry["role"], "delegate_role": entry.get("delegate_role")}

    lead = _exact_keys(uc04["lead_times"], allowed=set(LEAD_KEYS), required=set(LEAD_KEYS), where="lead_times")
    if not all(isinstance(lead[k], int) and not isinstance(lead[k], bool) and lead[k] >= 0 for k in LEAD_KEYS):
        raise Uc04ConfigError(INVALID, "lead_times must be non-negative whole numbers of working days")

    handover = _exact_keys(uc04["handover_rules"], allowed={"mode", "keep_reserved_on_missed_slot"},
                           required={"mode", "keep_reserved_on_missed_slot"}, where="handover_rules")
    if handover["mode"] not in HANDOVER_MODES or not isinstance(handover["keep_reserved_on_missed_slot"], bool):
        raise Uc04ConfigError(INVALID, "handover_rules has an unknown mode or a non-boolean flag")

    evidence_in = uc04["evidence_roles"]
    if not isinstance(evidence_in, Mapping):
        raise Uc04ConfigError(INVALID, "evidence_roles is not an object")
    unknown = set(evidence_in) - set(EVIDENCE_TYPES)
    if unknown:
        raise Uc04ConfigError(INVALID, f"evidence_roles names unknown evidence types: {', '.join(sorted(unknown))}")
    evidence = {}
    for key, roles in evidence_in.items():
        roles = [roles] if isinstance(roles, str) else roles
        if not isinstance(roles, list) or not roles or not all(isinstance(r, str) and r for r in roles):
            raise Uc04ConfigError(INVALID, f"evidence_roles.{key} names no role")
        evidence[EVIDENCE_TYPES[key]] = list(dict.fromkeys(roles))

    if uc04.get("out_of_matrix_policy") != "exception_approval_required":
        raise Uc04ConfigError(INVALID, "out_of_matrix_policy is not 'exception_approval_required' (UC04 GR-03)")

    config: dict[str, Any] = {
        "client_id": client_id, "role_id": role, "version": f"v{version}",
        "role_equipment_matrix": matrix_block(equipment),
        "role_access_matrix": matrix_block(access),
        "approver_config": _verified(approver_config),
        "lead_times": _verified({k: lead[k] for k in LEAD_KEYS}),
        "work_calendar": _verified(_work_calendar(shared["calendar"])),
        "handover_rules": _verified({"mode": handover["mode"],
                                     "keep_reserved_on_missed_slot": handover["keep_reserved_on_missed_slot"]}),
        "evidence_roles": _verified(evidence) if evidence else {"state": "MISSING"},
        "country_rules": {"state": "MISSING"},
        "client_site_rules": {"state": "MISSING"},
    }
    if uc04.get("country_rules") is not None:
        config["country_rules"] = _verified(_exact_keys(uc04["country_rules"], allowed=COUNTRY_FIELDS,
                                                        required=COUNTRY_FIELDS, where="country_rules"))
    if uc04.get("client_site_rules") is not None:
        config["client_site_rules"] = _verified(_exact_keys(uc04["client_site_rules"], allowed=CLIENT_SITE_FIELDS,
                                                            required=CLIENT_SITE_FIELDS, where="client_site_rules"))
    return config


# --------------------------------------------------------------------------- central read


def _persistence_error(exc: Exception, what: str) -> Uc04ConfigError:
    """The shared REST client's PersistenceError, as a stage code: refused key -> DENIED, unreachable or a server
    error -> UNAVAILABLE (retryable), anything else -> INVALID. Only the status and reason are kept, never a body."""
    context = getattr(exc, "context", None) or {}
    status = context.get("status") if isinstance(context, Mapping) else None
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status in (401, 403):
        return Uc04ConfigError(DENIED, f"{what}: the database refused the credentials (HTTP {status})")
    if status is None or status >= 500 or status == 429:
        reason = f"HTTP {status}" if status else (context.get("reason") if isinstance(context, Mapping) else None) or "unreachable"
        return Uc04ConfigError(UNAVAILABLE, f"{what}: the configuration database is unavailable ({reason})", retryable=True)
    return Uc04ConfigError(INVALID, f"{what}: the configuration database rejected the request (HTTP {status})")


def _reader():
    """The shared configuration reader (UC02 package, in-process). Absent: central mode cannot run."""
    try:
        from onboarding_task_management.client_configuration import document, projection
        from onboarding_task_management.config_safety import UnsafeConfigurationError, synthetic_config_allowed
        from onboarding_task_management.errors import MissingConfigurationError
        from onboarding_task_management.persistence import PersistenceError
        from onboarding_task_management.supabase_config import SupabaseConfigSource
    except ImportError as exc:
        raise Uc04ConfigError(NOT_CONFIGURED, f"the shared configuration reader is not importable ({exc.name})") from None
    return (document, projection, SupabaseConfigSource, MissingConfigurationError, UnsafeConfigurationError,
            PersistenceError, synthetic_config_allowed)


def resolve(rest: Any, *, client_id: str, role: str, grade: str | None, joining_date: date,
            environ: Mapping[str, str]) -> CentralUc04Config:
    """The case's UC04 CaseConfig from the client's in-force central configuration. Raises Uc04ConfigError."""
    (document, projection, SupabaseConfigSource, MissingConfigurationError, UnsafeConfigurationError,
     PersistenceError, synthetic_config_allowed) = _reader()
    try:
        allow_test_data = synthetic_config_allowed(environ)
    except UnsafeConfigurationError as exc:
        raise Uc04ConfigError(NOT_PERMITTED, f"the configuration safety policy is misconfigured: {exc}") from None

    source = SupabaseConfigSource(rest, allow_synthetic=allow_test_data, environ=environ)
    try:
        client_row = source._client_row(client_id)
        version_row = source._version_row(client_id, joining_date)
        rows = rest.select("client_service_policies", {"client_id": client_id, "config_version": version_row["version"],
                                                       "service_category": document.SECTION_CATEGORY,
                                                       "is_active": "true"})
    except UnsafeConfigurationError as exc:
        raise Uc04ConfigError(NOT_PERMITTED, f"client {client_id}: {exc}") from None
    except MissingConfigurationError as exc:
        raise Uc04ConfigError(MISSING, f"client {client_id}: {exc}") from None
    except PersistenceError as exc:
        raise _persistence_error(exc, f"client {client_id}") from None

    version = int(version_row["version"])
    # Tenant isolation: the rows must be this client's (and this version's). A foreign row is refused, never dropped.
    if client_row.get("client_id") != client_id or version_row.get("client_id") != client_id:
        raise Uc04ConfigError(INVALID, f"client {client_id}: the database returned another client's configuration row")
    stored: dict[str, Any] = {}
    for row in rows:
        if row.get("client_id") != client_id or str(row.get("config_version")) != str(version):
            raise Uc04ConfigError(INVALID, f"client {client_id} v{version}: the database returned a configuration "
                                           f"section of another client or version")
        key = row.get("service_key")
        if key in stored:
            raise Uc04ConfigError(INVALID, f"client {client_id} v{version}: section {key!r} is stored twice")
        stored[key] = row.get("policy_details") or {}
    missing = [s for s in document.SECTIONS if s not in stored]
    if missing:
        raise Uc04ConfigError(MISSING, f"client {client_id} v{version}: the configuration version has no complete "
                                       f"section set (missing: {', '.join(missing)})")
    if any(not isinstance(stored[s].get("document"), Mapping) for s in document.SECTIONS):
        raise Uc04ConfigError(INVALID, f"client {client_id} v{version}: a stored section has no document")
    sections = {s: stored[s]["document"] for s in document.SECTIONS}
    expected = projection.sections_hash(sections)
    if {stored[s].get("sections_hash") for s in document.SECTIONS} != {expected}:
        raise Uc04ConfigError(INVALID, f"client {client_id} v{version}: the stored sections are inconsistent "
                                       f"(sections_hash mismatch)")
    if any(stored[s].get("document_hash") != version_row.get("config_hash") for s in document.SECTIONS):
        raise Uc04ConfigError(INVALID, f"client {client_id} v{version}: the stored sections belong to a different "
                                       f"document than the version")
    problems = _schema_problems(document.load_schema(), sections)
    if problems:
        raise Uc04ConfigError(INVALID, f"client {client_id} v{version}: the stored configuration is invalid: "
                                       + "; ".join(problems[:5]))

    config = to_case_config(sections, client_id=client_id, role=role, grade=grade, version=version)
    payload = client_row.get("settings_payload") or {}
    test_data = payload.get("data_origin") == "synthetic" or payload.get("environment") == "mock"
    notes = []
    if uc04_null := [k for k in ("country_rules", "client_site_rules") if sections[SECTION_UC04].get(k) is None]:
        notes.append("not configured centrally (MISSING for UC04): " + ", ".join(uc04_null))
    return CentralUc04Config(config=config, client_id=client_id, version=version,
                             config_hash=str(version_row.get("config_hash")), sections_hash=expected,
                             data_origin="test_data" if test_data else "approved_client", notes=tuple(notes))


def _schema_problems(schema: Mapping[str, Any], sections: Mapping[str, Any]) -> list[str]:
    """The two sections UC04 uses, against the shared JSON Schema (the same definitions UC02's reader checks)."""
    try:
        import jsonschema  # type: ignore[import-untyped]
    except ImportError:
        raise Uc04ConfigError(NOT_CONFIGURED, "jsonschema is not installed; the configuration cannot be validated") from None
    problems = []
    for name, ref in ((SECTION_UC04, "uc04"), (SECTION_SHARED, "shared_configuration")):
        validator = jsonschema.Draft202012Validator({"$defs": schema["$defs"], "$ref": f"#/$defs/{ref}"})
        problems += [f"{name}: {'.'.join(map(str, e.path)) or '<root>'}: {e.message[:120]}"
                     for e in validator.iter_errors(sections[name])]
    return problems


def default_rest_factory(environ: Mapping[str, str]) -> Any:
    """The shared REST client over SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY (never logged). Not configured -> refused."""
    try:
        from onboarding_task_management.persistence import PersistenceError
        from onboarding_task_management.supabase_rest import SupabaseRestClient
    except ImportError as exc:
        raise Uc04ConfigError(NOT_CONFIGURED, f"the shared configuration reader is not importable ({exc.name})") from None
    try:
        return SupabaseRestClient.from_env(environ)
    except PersistenceError:
        raise Uc04ConfigError(NOT_CONFIGURED, "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set for "
                                              f"{SOURCE_ENV}={SOURCE_CENTRAL}") from None


#: Replaced in tests to inject a mocked transport; production uses the environment's Supabase settings.
rest_factory: Callable[[Mapping[str, str]], Any] = default_rest_factory
