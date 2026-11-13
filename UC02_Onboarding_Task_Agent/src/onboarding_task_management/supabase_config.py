"""Approved client configuration, read from the shared Supabase tables (read-only).

``SupabaseConfigRegistry`` is a drop-in :class:`ConfigRegistry`: the service asks it for a client's
configuration by the *verified* ``client_id`` and gets a :class:`ClientOnboardingConfig` built from

* ``client_configs``          - the client is active, its working days, whether the row is marked mock;
* ``client_config_versions``  - the version in force on the joining date, which must be ``approved`` by a
                                named approver with a timestamp and have a ``config_hash``;
* ``task_templates``          - the task templates of that exact version (``is_active``);
* ``client_milestone_templates`` - the milestones of that version.

Nothing is invented. Anything missing, draft, retired, not yet effective, mock or malformed raises
:class:`MissingConfigurationError` listing every gap, so the caller reports a configuration gap and
asks an authorised person to act. Nothing here writes to the database, and no request input can name a
configuration: the only key is the client id the service already verified against the case.

Known limits (reported, not guessed)
* ``client_service_policies`` and ``client_document_requirements`` are not mapped: UC-02 only plans tasks,
  and which service key gates which task template is not defined in the schema. UC-03 owns documents.
* The stored ``config_hash`` algorithm is not documented, so the hash is pinned (reported) but not recomputed.
* ``task_templates.default_lead_days`` has no defined meaning against UC-02's ``duration_days`` and is ignored.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date
from typing import Any

from pydantic import ValidationError

from onboarding_task_management.calendar_uae import WorkingCalendar
from onboarding_task_management.config_safety import environment, require_synthetic_allowed, synthetic_config_allowed
from onboarding_task_management.contracts import ChangeDriver, Role, TaskCategory
from onboarding_task_management.errors import CalendarConfigurationError, MissingConfigurationError
from onboarding_task_management.sub_agent.checklist import (
    AssignmentRule,
    ClientOnboardingConfig,
    ConfigRegistry,
    DocumentRequirement,
    MilestoneTemplate,
    TaskTemplate,
)
from onboarding_task_management.supabase_rest import SupabaseRestClient

MOCK_HASH_PREFIX = "mock-"


def _gap(message: str, **context: object) -> MissingConfigurationError:
    return MissingConfigurationError(message, **context)


class SupabaseConfigSource:
    """Reads and validates one client's approved configuration."""

    def __init__(self, rest: SupabaseRestClient, *, hash_check: Callable[[ClientOnboardingConfig], str] | None = None,
                 allow_synthetic: bool = False, environ: Mapping[str, str] | None = None) -> None:
        self.rest = rest
        #: The environment the safety policy is evaluated against (``os.environ`` when not given), read again at every load.
        self._environ = environ
        if allow_synthetic:
            # A caller asking for test data must be in an explicit development/test environment that opted in; otherwise this fails
            # loudly here instead of trusting the boolean (config_safety is the single policy).
            require_synthetic_allowed(environ, purpose="a configuration reader that accepts synthetic or mock clients")
        #: Accept clients whose ``settings_payload.data_origin`` is ``synthetic`` (development demos only). Off by default:
        #: a runtime that did not opt in refuses them, so synthetic configuration can never drive a real onboarding.
        self.allow_synthetic = allow_synthetic
        #: ``config -> expected hash``. The stored ``config_hash`` algorithm is not documented, so verification is
        #: opt-in: when the teams agree one (``persistence.config_hash`` is a candidate), pass it here and a mismatch
        #: is a configuration gap. Without it the stored hash is required and pinned, not recomputed.
        self.hash_check = hash_check
        #: ``client_id -> {"version", "config_hash"}`` of what was last loaded, for audit and pinning.
        self.loaded: dict[str, dict[str, Any]] = {}

    # -- selection ------------------------------------------------------------

    def _client_row(self, client_id: str) -> dict[str, Any]:
        rows = self.rest.select("client_configs", {"client_id": client_id})
        if not rows:
            raise _gap("client is not in client_configs", client_id=client_id)
        row = rows[0]
        if not row.get("is_active"):
            raise _gap("client is not active", client_id=client_id)
        payload = row.get("settings_payload") or {}
        is_test_data = (payload.get("data_origin") == "synthetic" or payload.get("environment") == "mock"
                        or str(row.get("config_hash", "")).startswith(MOCK_HASH_PREFIX))
        if is_test_data and self.allow_synthetic and not synthetic_config_allowed(self._environ):
            # Defence in depth: the environment changed after this reader was built (or was never valid). Never serve test data then.
            raise _gap("synthetic or mock configuration is denied in this environment", client_id=client_id,
                       environment=environment(self._environ) or "(unset)")
        if payload.get("data_origin") == "synthetic":
            if not self.allow_synthetic:
                raise _gap("this client is a synthetic test fixture and is not accepted by this runtime", client_id=client_id)
        elif str(row.get("config_hash", "")).startswith(MOCK_HASH_PREFIX) or payload.get("environment") == "mock":
            # A mock client is test data exactly like a synthetic one: usable only by a runtime that opted in to synthetic
            # configuration (never production); refused everywhere else.
            if not self.allow_synthetic:
                raise _gap(
                    "client configuration is marked mock and is not approved for real onboarding",
                    client_id=client_id,
                )
        return row

    def _eligibility(self, client_id: str, version: int):
        """The client's eligibility catalogue from the ``uc01_employee_record`` section document of this version, if one is stored."""
        from onboarding_task_management.sub_agent.checklist import AttributeFamily, EmployeeEligibility

        rows = [r for r in self.rest.select("client_service_policies", {"client_id": client_id, "config_version": version,
                                                                       "service_category": "cfg_section", "is_active": "true"})
                if r.get("service_key") == "uc01_employee_record"]
        if not rows:
            return None
        doc = ((rows[0].get("policy_details") or {}).get("document")) or {}
        try:
            return EmployeeEligibility(
                roles=tuple(r["role_id"] for r in doc.get("supported_roles", [])),
                grades=tuple(g["grade_id"] for g in doc.get("grades", [])),
                locations=tuple(v for loc in doc.get("work_locations", []) for v in (loc["code"], loc["name"])),
                nationality_groups=tuple(g["group_code"] for g in doc.get("nationality_groups", [])),
                employment_types=tuple(doc.get("employment_types", [])),
                attribute_families=tuple(AttributeFamily(family=f["family"], values=tuple(f.get("values", [])),
                                                         required=bool(f.get("required", True)), reason=f.get("reason", ""))
                                         for f in doc.get("checklist_attributes", [])))
        except (KeyError, TypeError, ValidationError) as exc:
            raise _gap("the client's eligibility catalogue is malformed", client_id=client_id, version=version,
                       problem=str(exc).splitlines()[0][:160]) from None

    def _version_row(self, client_id: str, on: date) -> dict[str, Any]:
        versions = self.rest.select("client_config_versions", {"client_id": client_id}, order_by="version.desc")
        if not versions:
            raise _gap("client has no configuration version", client_id=client_id)
        reasons: list[str] = []
        for row in versions:
            tag = f"v{row['version']}"
            if row.get("status") != "approved":
                reasons.append(f"{tag} is {row.get('status')!r}, not approved")
            elif not row.get("approved_by") or not row.get("approved_at"):
                reasons.append(f"{tag} is marked approved without an approver and timestamp")
            elif not row.get("config_hash"):
                reasons.append(f"{tag} has no config_hash")
            elif row.get("effective_from") and on < date.fromisoformat(row["effective_from"]):
                reasons.append(f"{tag} is not effective until {row['effective_from']}")
            elif row.get("effective_to") and on > date.fromisoformat(row["effective_to"]):
                reasons.append(f"{tag} ended on {row['effective_to']}")
            else:
                return row
        raise _gap(
            "no approved configuration version is in force on the joining date",
            client_id=client_id, on=on.isoformat(), reasons="; ".join(reasons),
        )

    # -- mapping --------------------------------------------------------------

    @staticmethod
    def _template(row: dict[str, Any]) -> TaskTemplate:
        title = row.get("title") or ""
        return TaskTemplate(
            key=row["template_key"],
            category=TaskCategory(row["category"]),
            title=title,
            # The schema allows an empty Arabic title; the display falls back to the English one rather than
            # inventing a translation.
            title_ar=row.get("title_ar") or title,
            owner=Role(row["owner_role"]),
            offset_days=_required_int(row, "offset_days"),
            depends_on=tuple(row.get("depends_on_template_keys") or ()),
            sensitive_to=tuple(ChangeDriver(v) for v in (row.get("sensitive_to") or ())) or (ChangeDriver.JOINING_DATE,),
            locations=tuple(row.get("locations") or ()),
            projects=tuple(row.get("projects") or ()),
            role_ids=tuple(row.get("role_ids") or ()),
            grades=tuple(row.get("grades") or ()),
            nationality_groups=tuple(row.get("nationality_groups") or ()),
            requires_attributes=tuple(row.get("requires_attributes") or ()),
            evidence_required=bool(row.get("evidence_required")),
            approval_role=Role(row["approval_role"]) if row.get("approval_role") else None,
            priority=row.get("default_priority") or "normal",
        )

    @staticmethod
    def _milestone(row: dict[str, Any]) -> MilestoneTemplate:
        rules = row.get("eligibility_rules") or {}
        title = row.get("title") or ""
        return MilestoneTemplate(
            key=row["milestone_key"], title=title, title_ar=title,
            offset_days=_required_int(row, "offset_days"),
            requires=tuple(row.get("required_task_keys") or ()),
            locations=tuple(rules.get("locations") or ()), projects=tuple(rules.get("projects") or ()),
            role_ids=tuple(rules.get("role_ids") or ()), grades=tuple(rules.get("grades") or ()),
        )

    @staticmethod
    def _document(row: dict[str, Any]) -> DocumentRequirement:
        rules = row.get("eligibility_rules") or {}
        return DocumentRequirement(
            key=row["requirement_key"], document_type=row["document_type"], mandatory=bool(row["is_mandatory"]),
            reason=row.get("description") or "required by the client's approved configuration",
            role_ids=tuple(row.get("applicable_roles") or ()), grades=tuple(row.get("applicable_grades") or ()),
            locations=tuple(row.get("applicable_locations") or ()), projects=tuple(row.get("applicable_projects") or ()),
            nationality_groups=tuple(rules.get("nationality_groups") or ()),
            requires_attributes=tuple(rules.get("requires_attributes") or ()),
            allow_not_applicable=bool(rules.get("allow_not_applicable", False)),
        )

    #: Convention for the team mapping, which the schema has no column for (PROPOSED, awaiting the configuration owners'
    #: approval): one ``client_service_policies`` row per task category, ``service_category = "uc02_assignment"``,
    #: ``service_key`` = the task category, ``policy_details`` = ``{"team": ..., "use_case": ..., "mode": ...,
    #: "escalation_owner": ...}``. Without such rows every category is an assignment gap, reported and never guessed.
    ASSIGNMENT_CATEGORY = "uc02_assignment"

    @staticmethod
    def _assignment(row: dict[str, Any]) -> AssignmentRule:
        details = row.get("policy_details") or {}
        return AssignmentRule(
            category=TaskCategory(row["service_key"]), team=details.get("team", ""), use_case=details.get("use_case", ""),
            mode=details.get("mode", "role"),
            escalation_owner=Role(details["escalation_owner"]) if details.get("escalation_owner") else None)

    def load(self, client_id: str, on: date) -> ClientOnboardingConfig:
        client = self._client_row(client_id)
        version = self._version_row(client_id, on)
        number = version["version"]
        rows = self.rest.select(
            "task_templates", {"client_id": client_id, "config_version": number, "is_active": "true"},
            order_by="template_key",
        )
        if not rows:
            raise _gap("the approved version has no active task templates", client_id=client_id, version=number)
        problems: list[str] = []
        templates = self._build(rows, self._template, "template_key", problems)
        milestones = self._build(
            self.rest.select("client_milestone_templates",
                             {"client_id": client_id, "config_version": number, "is_active": "true"},
                             order_by="milestone_key"),
            self._milestone, "milestone_key", problems)
        documents = self._build(
            self.rest.select("client_document_requirements",
                             {"client_id": client_id, "config_version": number, "is_active": "true"},
                             order_by="requirement_key"),
            self._document, "requirement_key", problems)
        assignments = self._build(
            [r for r in self.rest.select("client_service_policies",
                                         {"client_id": client_id, "config_version": number, "is_active": "true"})
             if r.get("service_category") == self.ASSIGNMENT_CATEGORY and r.get("is_available", True)],
            self._assignment, "service_key", problems)
        if problems:
            raise _gap("the approved configuration is invalid", client_id=client_id, version=number,
                       problems="; ".join(problems))
        try:
            config = ClientOnboardingConfig(
                client_id=client_id, version=str(number), approval_status="approved",
                effective_from=date.fromisoformat(version["effective_from"]) if version.get("effective_from") else None,
                effective_to=date.fromisoformat(version["effective_to"]) if version.get("effective_to") else None,
                templates=tuple(templates), milestones=tuple(milestones),
                document_requirements=tuple(documents), assignments=tuple(assignments),
                data_origin=("synthetic" if (client.get("settings_payload") or {}).get("data_origin") == "synthetic"
                             or (client.get("settings_payload") or {}).get("environment") == "mock"
                             or str(client.get("config_hash", "")).startswith(MOCK_HASH_PREFIX) else ""),
                eligibility=self._eligibility(client_id, number),
            )
        except (ValidationError, ValueError) as exc:
            raise _gap("the approved configuration is inconsistent", client_id=client_id, version=number,
                       problem=str(exc).splitlines()[0][:200]) from None
        if self.hash_check is not None and self.hash_check(config) != version["config_hash"]:
            raise _gap("the configuration content does not match its stored config_hash", client_id=client_id,
                       version=number)
        self.loaded[client_id] = {"version": number, "config_hash": version["config_hash"]}
        return config

    @staticmethod
    def _build(rows: Iterable[dict[str, Any]], make, key_name: str, problems: list[str]) -> list:
        built = []
        for row in rows:
            try:
                built.append(make(row))
            except (ValidationError, ValueError, KeyError, TypeError) as exc:
                problems.append(f"{key_name}={row.get(key_name)!r}: {str(exc).splitlines()[0][:120]}")
        return built

    # -- calendar ---------------------------------------------------------------

    def calendar(self, client_id: str) -> WorkingCalendar:
        """The client's working calendar. Refused when it has no holiday rows: an empty table cannot be told
        apart from 'not yet configured', and a calendar is never assumed."""
        row = self._client_row(client_id)
        holidays = self.rest.select("client_holidays", {"client_id": client_id})
        if not holidays:
            raise CalendarConfigurationError("client has no holidays configured", client_id=client_id)
        working = {6 if d in (0, 7) else d - 1 for d in row["working_days"]}  # 0/7 = Sunday, 1..6 = Mon..Sat
        weekend = {d for d in range(7) if d not in working}
        return WorkingCalendar(
            [date.fromisoformat(h["holiday_date"]) for h in holidays if h.get("is_observed", True)],
            name=f"{client_id} (client_configs/client_holidays)", weekend_weekdays=weekend,
            approved=False, source="supabase client_configs + client_holidays",
        )


def _required_int(row: dict[str, Any], column: str) -> int:
    if row.get(column) is None:
        raise ValueError(f"{column} is not set")
    return int(row[column])


class SupabaseConfigRegistry(ConfigRegistry):
    """Serves configurations from Supabase, keeping each client's loaded versions for the process."""

    source: Any = None
    cache: dict[tuple[str, str], ClientOnboardingConfig] = {}

    def get(self, client_id: str, *, on: date | None = None) -> ClientOnboardingConfig:
        if self.source is None:
            raise _gap("no configuration source is attached", client_id=client_id)
        if on is None and client_id in self.configs:
            return self.configs[client_id]  # already loaded and validated for this client's case
        when = on or date.today()
        key = (client_id, when.isoformat())
        if key not in self.cache:
            self.cache[key] = self.source.load(client_id, when)
        config = self.cache[key]
        self.configs[client_id] = config
        return super().get(client_id, on=on)


class SupabaseCalendars(dict):
    """``service.calendars`` backed by Supabase: loaded on first use, never defaulted."""

    lazy = True  # the service keeps this object rather than copying it into a plain dict

    def __init__(self, source: SupabaseConfigSource) -> None:
        super().__init__()
        self._source = source

    def __contains__(self, client_id: object) -> bool:
        if not super().__contains__(client_id):
            super().__setitem__(client_id, self._source.calendar(str(client_id)))  # raises a clear gap if absent
        return True


__all__ = ["SupabaseCalendars", "SupabaseConfigRegistry", "SupabaseConfigSource"]
