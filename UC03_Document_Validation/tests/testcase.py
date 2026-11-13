from __future__ import annotations

"""
Base test utilities shared across unit and integration test suites.
Import from here to avoid duplicating fixtures and helper builders.
"""

import pytest
from datetime import date

from src.sub_agent.models import DocumentValidationRequest
from src.state.enums import ActorType, SubmissionStatus


FUTURE_DATE = date(2030, 1, 1)
PAST_DATE = date(2020, 1, 1)
TODAY = date(2024, 6, 1)


def make_request(**overrides) -> DocumentValidationRequest:
    defaults = dict(
        case_id="case-001",
        employee_id="emp-001",
        employee_name_form="John Smith",
        offer_accepted=True,
        onboarding_form_approved=True,
        document_type="passport",
        submission_status=SubmissionStatus.RECEIVED,
        extracted_name="John Smith",
        expiry_date=FUTURE_DATE,
        readable=True,
        required_fields_present=True,
        extraction_confidence=0.95,
        evidence_stored=False,
        requesting_actor_id="hr-user-001",
        requesting_actor_type=ActorType.HR_USER,
        validation_date=TODAY,
    )
    defaults.update(overrides)
    return DocumentValidationRequest(**defaults)
