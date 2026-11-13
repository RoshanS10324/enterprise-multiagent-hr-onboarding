"""Test doubles for the UC-01 sub-agent.

The in-memory adapters from `sub_agent.adapters.memory` plus
the mock Outlook connector, in one import. These are the only adapters
that exist today -- the container refuses to start outside APP_ENV=dev
for exactly that reason.

All fixtures used with these are synthetic. Never load real joiner data.
"""

import sys
from pathlib import Path

from .adapters.memory import (
    InMemoryApprovalRepository,
    InMemoryAuditLog,
    InMemoryCaseRepository,
    InMemoryDiscrepancyRepository,
    InMemoryDocumentStore,
    InMemoryEmployeeNumberGenerator,
    InMemoryEventPublisher,
    InMemoryFormRepository,
    InMemoryHolidayCalendar,
    InMemoryHrisRecordRepository,
    InMemoryOfferRepository,
    InMemoryOnboardingFormSource,
    InMemoryPendingStartRepository,
    InMemoryPersonDirectory,
    StubHrisGateway,
)
from .llm import FakeLlm, load_all_tasks

# `connectors/` is a repo-root package, not part of the installed
# sub_agent distribution, so make it importable regardless of
# the invoking cwd (same trick as tests/conftest.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from connectors.outlook import MailMessage, MockOutlookConnector  # noqa: E402

__all__ = [
    "FakeLlm",
    "InMemoryApprovalRepository",
    "InMemoryAuditLog",
    "InMemoryCaseRepository",
    "InMemoryDiscrepancyRepository",
    "InMemoryDocumentStore",
    "InMemoryEmployeeNumberGenerator",
    "InMemoryEventPublisher",
    "InMemoryFormRepository",
    "InMemoryHolidayCalendar",
    "InMemoryHrisRecordRepository",
    "InMemoryOfferRepository",
    "InMemoryOnboardingFormSource",
    "InMemoryPendingStartRepository",
    "InMemoryPersonDirectory",
    "MailMessage",
    "MockOutlookConnector",
    "StubHrisGateway",
    "load_all_tasks",
]
