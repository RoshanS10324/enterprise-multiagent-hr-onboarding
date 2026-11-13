"""Integration boundaries for UC-02, with **mock/local adapters only**.

Each Protocol below is the seam a future production adapter will implement.
Nothing here performs a live call: there is no HTTP client, no database
driver, no credential handling and no external SDK anywhere in this module.

===========================  ================================================
Protocol                     Mock adapter provided here
===========================  ================================================
:class:`EmployeeDataProvider`      :class:`JsonEmployeeDataProvider`
:class:`ClientConfigurationProvider` :class:`RegistryConfigurationProvider`
:class:`CalendarProvider`          :class:`JsonCalendarProvider`,
                                   :class:`BuiltInCalendarProvider`
:class:`OnboardingRepository`      :class:`InMemoryOnboardingRepository`,
                                   :class:`JsonFileOnboardingRepository`
:class:`NotificationProvider`      :class:`DraftOnlyNotificationProvider`
:class:`UC06HandoffProvider`       :class:`LocalUC06HandoffProvider`
===========================  ================================================

**Future integration contract** — for each Protocol the production adapter must:

* accept and return the same models, which carry **no personal data**
  (identifiers, roles, dates, counts and status codes only);
* raise :class:`~onboarding_task_management.errors.OnboardingError` subclasses
  rather than transport-specific exceptions, so business logic is unchanged;
* preserve client isolation — every method is scoped by ``client_id``;
* be idempotent where the method is a write, keyed on the record identifier.

Endpoint URLs, authentication schemes, credential storage and retry policy are
**deliberately absent**: none has been approved. See the unresolved decisions
in ``docs/UC02_BUSINESS_RULES.md``.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Protocol, runtime_checkable

from onboarding_task_management.calendar_uae import UAE_PUBLIC_HOLIDAYS
from onboarding_task_management.contracts import (
    CalendarStatus,
    JoiningHandoff,
    OnboardingRecord,
    ReminderDraft,
)
from onboarding_task_management.errors import (
    MissingConfigurationError,
    OnboardingError,
)
from onboarding_task_management.sub_agent.checklist import (
    ClientOnboardingConfig,
    ConfigRegistry,
)


class ProviderDataError(OnboardingError):
    """A mock provider's backing data is missing, unreadable or invalid."""

    code = "MISSING_DATA"


# ---------------------------------------------------------------------------
# Employee / trigger data
# ---------------------------------------------------------------------------


@runtime_checkable
class EmployeeDataProvider(Protocol):
    """Supplies the onboarding trigger payload for an employee.

    Production adapter (UC-01 or the HRIS) must return the same dict shape as
    ``OnboardingTrigger``: identifiers, dates, role/grade/location/project,
    applicability attributes, and the offer/form approval state. It must not
    return names, nationalities or contact details.
    """

    def get_trigger_payload(self, client_id: str, employee_id: str) -> dict: ...


class JsonEmployeeDataProvider:
    """Reads trigger payloads from local JSON files. Mock only."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _candidates(self, client_id: str, employee_id: str) -> list[Path]:
        return [
            self.directory / f"{client_id}__{employee_id}.json",
            self.directory / f"{employee_id}.json",
        ]

    def get_trigger_payload(self, client_id: str, employee_id: str) -> dict:
        for path in self._candidates(client_id, employee_id):
            if path.exists():
                return self.load_file(path)
        raise ProviderDataError(
            "no mock trigger payload found",
            client_id=client_id,
            employee_id=employee_id,
            searched=str(self.directory),
        )

    @staticmethod
    def load_file(path: Path) -> dict:
        """Load and shape-check one payload file."""
        try:
            with Path(path).open(encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError as exc:
            raise ProviderDataError("mock payload file not found", path=str(path)) from exc
        except json.JSONDecodeError as exc:
            raise ProviderDataError(
                "mock payload file is not valid JSON", path=str(path), detail=str(exc)
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderDataError(
                "mock payload must be a JSON object", path=str(path)
            )
        return payload


# ---------------------------------------------------------------------------
# Client configuration
# ---------------------------------------------------------------------------


@runtime_checkable
class ClientConfigurationProvider(Protocol):
    """Supplies a client's checklist configuration.

    Production adapter must raise ``MissingConfigurationError`` when a client
    has no configuration — UC-02 must never invent tasks (UC02-BR-02).
    """

    def get_config(self, client_id: str) -> ClientOnboardingConfig: ...


class RegistryConfigurationProvider:
    """Adapts the existing in-process :class:`ConfigRegistry`."""

    def __init__(self, registry: ConfigRegistry) -> None:
        self.registry = registry

    def get_config(self, client_id: str) -> ClientOnboardingConfig:
        return self.registry.get(client_id)


# ---------------------------------------------------------------------------
# Working calendar
# ---------------------------------------------------------------------------


@runtime_checkable
class CalendarProvider(Protocol):
    """Supplies holiday data and states whether it is authoritative."""

    def holidays(self) -> frozenset[date]: ...
    def status(self) -> CalendarStatus: ...


class BuiltInCalendarProvider:
    """The built-in provisional UAE list. Always reports ``approved=False``."""

    def holidays(self) -> frozenset[date]:
        return UAE_PUBLIC_HOLIDAYS

    def status(self) -> CalendarStatus:
        from onboarding_task_management.calendar_uae import calendar_status

        return calendar_status(UAE_PUBLIC_HOLIDAYS)


class JsonCalendarProvider:
    """Loads holiday data from a local JSON fixture. Mock only.

    The fixture must declare its own ``approved`` flag and ``source``. A
    fixture that does not claim approval is reported as provisional, so mock
    holiday dates are never presented as authoritative (UC02-BR-06).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data = self._load()

    def _load(self) -> dict:
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError as exc:
            raise ProviderDataError(
                "calendar fixture not found", path=str(self.path)
            ) from exc
        except json.JSONDecodeError as exc:
            raise ProviderDataError(
                "calendar fixture is not valid JSON",
                path=str(self.path),
                detail=str(exc),
            ) from exc
        if "holidays" not in data:
            raise ProviderDataError(
                "calendar fixture has no 'holidays' key", path=str(self.path)
            )
        return data

    def holidays(self) -> frozenset[date]:
        try:
            return frozenset(date.fromisoformat(d) for d in self._data["holidays"])
        except (TypeError, ValueError) as exc:
            raise ProviderDataError(
                "calendar fixture holiday is not an ISO date",
                path=str(self.path),
                detail=str(exc),
            ) from exc

    def status(self) -> CalendarStatus:
        from onboarding_task_management.calendar_uae import calendar_status

        holidays = self.holidays()
        return calendar_status(
            holidays,
            approved=bool(self._data.get("approved", False)),
            source=str(self._data.get("source", f"mock fixture {self.path.name}")),
            coverage_start=date.fromisoformat(self._data["coverage_start"])
            if "coverage_start" in self._data
            else None,
            coverage_end=date.fromisoformat(self._data["coverage_end"])
            if "coverage_end" in self._data
            else None,
        )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@runtime_checkable
class OnboardingRepository(Protocol):
    """Stores onboarding records, partitioned by client.

    Production adapter must enforce the same partitioning at the storage layer
    and must never return another client's record (UC02-BR-13).
    """

    def save(self, record: OnboardingRecord) -> None: ...
    def get(self, client_id: str, onboarding_id: str) -> OnboardingRecord | None: ...
    def list_for_client(self, client_id: str) -> list[OnboardingRecord]: ...


class InMemoryOnboardingRepository:
    """Process-local storage. **Does not survive a restart.**"""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, OnboardingRecord]] = {}

    def save(self, record: OnboardingRecord) -> None:
        self._records.setdefault(record.client_id, {})[record.onboarding_id] = (
            record.model_copy(deep=True)
        )

    def get(self, client_id: str, onboarding_id: str) -> OnboardingRecord | None:
        found = self._records.get(client_id, {}).get(onboarding_id)
        return found.model_copy(deep=True) if found else None

    def list_for_client(self, client_id: str) -> list[OnboardingRecord]:
        return [r.model_copy(deep=True) for r in self._records.get(client_id, {}).values()]


class JsonFileOnboardingRepository:
    """Local JSON storage for the mock prototype.

    One file per client, written atomically (temp file + ``os.replace``) so an
    interrupted write cannot leave a truncated file. A corrupt or unreadable
    file raises :class:`ProviderDataError` rather than silently losing data.

    This is **not** a production database and introduces no database
    dependency. Supabase, PostgreSQL and Cosmos DB remain unapproved.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, client_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in client_id)
        return self.directory / f"{safe}.json"

    def _read(self, client_id: str) -> dict[str, dict]:
        path = self._path(client_id)
        if not path.exists():
            return {}
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ProviderDataError(
                "onboarding store file is corrupt", path=str(path), detail=str(exc)
            ) from exc
        if not isinstance(data, dict):
            raise ProviderDataError(
                "onboarding store file must be a JSON object", path=str(path)
            )
        return data

    def _write(self, client_id: str, data: dict[str, dict]) -> None:
        path = self._path(client_id)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.directory, delete=False, suffix=".tmp"
        )
        try:
            with handle:
                json.dump(data, handle, indent=2, default=str)
            os.replace(handle.name, path)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise

    def save(self, record: OnboardingRecord) -> None:
        data = self._read(record.client_id)
        data[record.onboarding_id] = json.loads(record.model_dump_json())
        self._write(record.client_id, data)

    def get(self, client_id: str, onboarding_id: str) -> OnboardingRecord | None:
        raw = self._read(client_id).get(onboarding_id)
        if raw is None:
            return None
        try:
            return OnboardingRecord(**raw)
        except Exception as exc:
            raise ProviderDataError(
                "stored onboarding record is invalid",
                client_id=client_id,
                onboarding_id=onboarding_id,
                detail=str(exc),
            ) from exc

    def list_for_client(self, client_id: str) -> list[OnboardingRecord]:
        return [OnboardingRecord(**raw) for raw in self._read(client_id).values()]


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


@runtime_checkable
class NotificationProvider(Protocol):
    """Accepts reminder drafts.

    **UC02-BR-11 / GR-01:** nothing may be sent to a joiner without HR
    approval. The mock adapter therefore only records drafts. A production
    adapter must still refuse to send until the approval gate has passed.
    """

    def queue_draft(self, draft: ReminderDraft) -> str: ...
    def outcomes(self) -> list[dict]: ...


class DraftOnlyNotificationProvider:
    """Records drafts and never delivers. Mock only — there is no transport."""

    def __init__(self) -> None:
        self._outcomes: list[dict] = []

    def queue_draft(self, draft: ReminderDraft) -> str:
        if draft.delivery_status != "draft_not_sent":
            raise ProviderDataError(
                "only undelivered drafts may be queued",
                draft_id=draft.draft_id,
                delivery_status=draft.delivery_status,
            )
        self._outcomes.append(
            {
                "draft_id": draft.draft_id,
                "onboarding_id": draft.onboarding_id,
                "recipient_role": draft.recipient_role.value,
                "reason": draft.reason,
                "task_count": len(draft.task_ids),
                "outcome": "queued_not_sent",
            }
        )
        return draft.draft_id

    def outcomes(self) -> list[dict]:
        return [dict(o) for o in self._outcomes]


# ---------------------------------------------------------------------------
# UC-06 handoff
# ---------------------------------------------------------------------------


@runtime_checkable
class UC06HandoffProvider(Protocol):
    """Receives the planning package UC-02 hands to UC-06.

    UC-06 is **not implemented and not connected**. The production adapter
    owns monitoring, follow-up delivery, joining confirmation and case
    closure; UC-02 only publishes.
    """

    def publish(self, handoff: JoiningHandoff) -> str: ...
    def published(self) -> list[JoiningHandoff]: ...


class LocalUC06HandoffProvider:
    """Collects handoffs in memory. Mock only — no UC-06 exists to receive them."""

    def __init__(self) -> None:
        self._published: list[JoiningHandoff] = []

    def publish(self, handoff: JoiningHandoff) -> str:
        if handoff.case_closed_by_uc02:
            raise ProviderDataError(
                "UC-02 must never publish a closed case",
                onboarding_id=handoff.onboarding_id,
            )
        self._published.append(handoff.model_copy(deep=True))
        return handoff.onboarding_id

    def published(self) -> list[JoiningHandoff]:
        return [h.model_copy(deep=True) for h in self._published]


__all__ = [
    "ProviderDataError",
    "EmployeeDataProvider",
    "JsonEmployeeDataProvider",
    "ClientConfigurationProvider",
    "RegistryConfigurationProvider",
    "CalendarProvider",
    "BuiltInCalendarProvider",
    "JsonCalendarProvider",
    "OnboardingRepository",
    "InMemoryOnboardingRepository",
    "JsonFileOnboardingRepository",
    "NotificationProvider",
    "DraftOnlyNotificationProvider",
    "UC06HandoffProvider",
    "LocalUC06HandoffProvider",
    "MissingConfigurationError",
]
