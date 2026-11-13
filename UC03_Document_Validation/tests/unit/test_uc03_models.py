from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from src.sub_agent.models import DocumentValidationRequest
from src.state.enums import ActorType, SubmissionStatus


def _base() -> dict:
    return dict(
        case_id="case-001",
        employee_id="emp-001",
        employee_name_form="John Smith",
        offer_accepted=True,
        onboarding_form_approved=True,
        document_type="passport",
        submission_status=SubmissionStatus.RECEIVED,
        requesting_actor_id="hr-001",
        requesting_actor_type=ActorType.HR_USER,
    )


def test_valid_request():
    req = DocumentValidationRequest(**_base())
    assert req.case_id == "case-001"


def test_confidence_above_one_rejected():
    with pytest.raises(ValidationError):
        DocumentValidationRequest(**{**_base(), "extraction_confidence": 1.1})


def test_confidence_below_zero_rejected():
    with pytest.raises(ValidationError):
        DocumentValidationRequest(**{**_base(), "extraction_confidence": -0.1})


def test_empty_case_id_rejected():
    with pytest.raises(ValidationError):
        DocumentValidationRequest(**{**_base(), "case_id": ""})


def test_empty_employee_name_rejected():
    with pytest.raises(ValidationError):
        DocumentValidationRequest(**{**_base(), "employee_name_form": "   "})


def test_empty_document_type_rejected():
    with pytest.raises(ValidationError):
        DocumentValidationRequest(**{**_base(), "document_type": ""})


def test_expiry_date_as_string():
    req = DocumentValidationRequest(**{**_base(), "expiry_date": "2030-01-01"})
    assert req.expiry_date == date(2030, 1, 1)


def test_invalid_submission_status_rejected():
    with pytest.raises(ValidationError):
        DocumentValidationRequest(**{**_base(), "submission_status": "INVALID_STATUS"})


def test_invalid_actor_type_rejected():
    with pytest.raises(ValidationError):
        DocumentValidationRequest(**{**_base(), "requesting_actor_type": "UNKNOWN"})


def test_defaults_applied():
    req = DocumentValidationRequest(**_base())
    assert req.extraction_confidence == 1.0
    assert req.evidence_stored is False
    assert req.readable is None
    assert req.required_fields_present is None
