from __future__ import annotations

import os

# Tests NEVER use the live database. The repository-root .env may set UC03_STORE_MODE=supabase for real runs; this is
# set before anything imports src.main (which loads that .env without overriding values already set), so every test
# runs on the in-memory stores unless it injects its own fake table client explicitly.
os.environ["UC03_STORE_MODE"] = "memory"

from datetime import date  # noqa: E402

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.sub_agent.checklist import ChecklistService
from src.sub_agent.extraction import MockDocumentExtractor
from src.sub_agent.models import DocumentValidationRequest
from src.sub_agent.validator import DocumentValidationService
from src.main import app
from src.state.enums import ActorType, SubmissionStatus
from src.state.store import InMemoryAuditStore, InMemoryCaseStore

FUTURE_DATE = date(2030, 1, 1)
PAST_DATE = date(2020, 1, 1)
TODAY = date(2024, 6, 1)
ACTOR_ID = "hr-user-001"
CASE_ID = "case-001"
EMPLOYEE_ID = "emp-001"
EMPLOYEE_NAME = "John Smith"


@pytest_asyncio.fixture
async def case_store() -> InMemoryCaseStore:
    return InMemoryCaseStore()


@pytest_asyncio.fixture
async def audit_store() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest_asyncio.fixture
async def checklist_service() -> ChecklistService:
    return ChecklistService("default")


@pytest_asyncio.fixture
async def mock_extractor() -> MockDocumentExtractor:
    return MockDocumentExtractor()


@pytest_asyncio.fixture
async def validation_service(
    case_store: InMemoryCaseStore,
    audit_store: InMemoryAuditStore,
    checklist_service: ChecklistService,
    mock_extractor: MockDocumentExtractor,
) -> DocumentValidationService:
    return DocumentValidationService(
        case_store=case_store,
        audit_store=audit_store,
        checklist_service=checklist_service,
        extractor=mock_extractor,
    )


def make_request(**overrides) -> DocumentValidationRequest:
    defaults = dict(
        case_id=CASE_ID,
        employee_id=EMPLOYEE_ID,
        employee_name_form=EMPLOYEE_NAME,
        offer_accepted=True,
        onboarding_form_approved=True,
        document_type="passport",
        submission_status=SubmissionStatus.RECEIVED,
        extracted_name=EMPLOYEE_NAME,
        expiry_date=FUTURE_DATE,
        readable=True,
        required_fields_present=True,
        extraction_confidence=0.95,
        evidence_stored=False,
        requesting_actor_id=ACTOR_ID,
        requesting_actor_type=ActorType.HR_USER,
        validation_date=TODAY,
    )
    defaults.update(overrides)
    return DocumentValidationRequest(**defaults)


@pytest_asyncio.fixture
async def base_request() -> DocumentValidationRequest:
    return make_request()


@pytest_asyncio.fixture
async def async_client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
