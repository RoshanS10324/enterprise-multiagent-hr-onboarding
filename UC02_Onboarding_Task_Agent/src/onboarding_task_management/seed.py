"""Idempotent seeding of synthetic demo data into a UC-02 store.

Which data goes where
---------------------

There is exactly one authoritative source for each kind of data, and seeding
does not blur them:

===============================  =========================================
Kind                             Authoritative source
===============================  =========================================
Client checklist configuration   Python (``ClientOnboardingConfig``). Only a
                                 content hash is recorded in the database.
Runtime cases, tasks, approvals  The store, once persistence is wired.
Test fixtures                    ``data/`` JSON. Inputs to a run; never the
                                 system of record.
===============================  =========================================

So this module does **not** upload every JSON file into a table. It reads the
selected trigger fixtures, runs them through the ordinary
:meth:`OnboardingService.create_onboarding` path — preconditions, client
config, planner, calendar, audit, all of it — and lets the normal persistence
hooks write the result. Seeded data is therefore indistinguishable from data
produced by real use, which is the only kind of seed worth having.

Idempotency
-----------

Re-running is safe. Onboarding ids are derived
(``ONB-<client>-<employee>``), so a second run upserts the same primary keys
rather than creating a second case; and a fixture whose case already exists is
skipped outright, leaving any progress made since the first seed untouched.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from onboarding_task_management.contracts import Actor, Role
from onboarding_task_management.errors import OnboardingError
from onboarding_task_management.persistence import OnboardingStore
from onboarding_task_management.sub_agent.checklist import ConfigRegistry
from onboarding_task_management.sub_agent.service import (
    OnboardingService,
    OnboardingTrigger,
)

#: Fixtures that describe a *valid* onboarding. The remaining files in
#: ``data/mock/`` deliberately describe rejected inputs (offer not accepted,
#: form unapproved, missing configuration); seeding those would create nothing
#: and only produce noise, so they stay as test inputs.
DEFAULT_SEED_FIXTURES: tuple[str, ...] = (
    "01_valid_onboarding.json",
    "06_visa_activities.json",
    "07_full_relocation.json",
    "08_evidence_required.json",
    "14_local_hire_no_extras.json",
)


@dataclass
class SeedReport:
    """What a seed run actually did. Nothing is inferred."""

    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.skipped) + len(self.rejected)

    def summary(self) -> str:
        return (
            f"{len(self.created)} created, {len(self.skipped)} already present, "
            f"{len(self.rejected)} rejected"
        )


def load_trigger(path: Path) -> OnboardingTrigger:
    """Parse one fixture into a validated trigger.

    ``_mock_note`` and any other annotation key is ignored by pydantic, so a
    fixture can carry documentation without polluting the contract.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    return OnboardingTrigger.model_validate(payload)


def seed_from_fixtures(
    service: OnboardingService,
    fixture_dir: Path,
    *,
    filenames: Sequence[str] = DEFAULT_SEED_FIXTURES,
    actor_id: str = "seed",
) -> SeedReport:
    """Create each fixture's onboarding through the normal service path.

    Returns a :class:`SeedReport` describing exactly what happened. A fixture
    the service rejects is recorded with its reason rather than skipped
    silently — a seed that quietly creates nothing is worse than one that
    fails loudly.
    """
    report = SeedReport()
    for filename in filenames:
        path = fixture_dir / filename
        if not path.is_file():
            report.rejected.append((filename, "fixture file not found"))
            continue
        try:
            trigger = load_trigger(path)
        except Exception as exc:  # noqa: BLE001 - malformed fixture, report it
            report.rejected.append((filename, f"unreadable: {exc}"))
            continue

        actor = Actor(
            actor_id=actor_id, role=Role.HR_OPS, client_id=trigger.client_id
        )
        if _already_seeded(service, actor, trigger):
            report.skipped.append(filename)
            continue
        try:
            service.create_onboarding(actor, trigger)
        except OnboardingError as exc:
            report.rejected.append((filename, f"{exc.code}: {exc.message}"))
            continue
        report.created.append(filename)
    return report


def _already_seeded(
    service: OnboardingService, actor: Actor, trigger: OnboardingTrigger
) -> bool:
    """Whether a live case for this employee already exists, in store or memory."""
    if service.store is not None:
        for record in service.store.list_onboardings(trigger.client_id):
            if (
                record.employee_id == trigger.employee_id
                and record.status.value != "cancelled"
            ):
                return True
    return any(
        existing.employee_id == trigger.employee_id
        and existing.status.value != "cancelled"
        for existing in service._client_records(actor.client_id).values()
    )


def build_seeded_service(
    registry: ConfigRegistry,
    store: OnboardingStore,
    fixture_dir: Path,
    *,
    filenames: Sequence[str] = DEFAULT_SEED_FIXTURES,
) -> tuple[OnboardingService, SeedReport]:
    """A service backed by ``store``, hydrated then seeded. Safe to re-run."""
    service = OnboardingService(registry, store=store)
    for client_id in _fixture_clients(fixture_dir, filenames):
        service.hydrate(client_id)
    report = seed_from_fixtures(service, fixture_dir, filenames=filenames)
    return service, report


def _fixture_clients(fixture_dir: Path, filenames: Iterable[str]) -> set[str]:
    clients: set[str] = set()
    for filename in filenames:
        path = fixture_dir / filename
        if not path.is_file():
            continue
        try:
            clients.add(json.loads(path.read_text(encoding="utf-8"))["client_id"])
        except Exception:  # noqa: BLE001 - a bad fixture is reported during seeding
            continue
    return clients


__all__ = [
    "DEFAULT_SEED_FIXTURES",
    "SeedReport",
    "build_seeded_service",
    "load_trigger",
    "seed_from_fixtures",
]
