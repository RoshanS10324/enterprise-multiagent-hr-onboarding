"""FastAPI endpoints for email-driven document ingestion and mailbox monitoring."""

from __future__ import annotations

import os
from fastapi import APIRouter, Depends, HTTPException

from connectors.email.base import EmailIngestionConnectorBase
from connectors.email.imap_client import IMAPEmailConnector
from connectors.email.mock import MockEmailConnector
from src.ingestion.processor import DocumentSubmissionRecord, EmailIngestionProcessor
from src.state.store import CaseStore, get_case_store
from src.sub_agent.checklist import ChecklistService
from src.sub_agent.extraction import MockDocumentExtractor
from src.sub_agent.validator import DocumentValidationService

router = APIRouter(prefix="/onboarding/uc03/email", tags=["UC-03 Email Ingestion"])

_connector_instance: EmailIngestionConnectorBase | None = None
_processor_instance: EmailIngestionProcessor | None = None


def get_email_connector() -> EmailIngestionConnectorBase:
    """Return configured IMAP connector or fallback Mock connector."""
    global _connector_instance
    if _connector_instance is None:
        imap_host = os.environ.get("IMAP_HOST", "")
        imap_user = os.environ.get("IMAP_USER", "")
        imap_pass = os.environ.get("IMAP_PASSWORD", "")
        if imap_host and imap_user:
            _connector_instance = IMAPEmailConnector(
                host=imap_host,
                user=imap_user,
                password=imap_pass,
                port=int(os.environ.get("IMAP_PORT", "993")),
            )
        else:
            _connector_instance = MockEmailConnector()
    return _connector_instance


def get_ingestion_processor(
    connector: EmailIngestionConnectorBase = Depends(get_email_connector),
    case_store: CaseStore = Depends(get_case_store),
) -> EmailIngestionProcessor:
    """Return singleton instance of EmailIngestionProcessor."""
    global _processor_instance
    if _processor_instance is None:
        checklist = ChecklistService("default")
        extractor = MockDocumentExtractor()
        validator = DocumentValidationService(
            case_store=case_store,
            audit_store=None,
            checklist_service=checklist,
            extractor=extractor,
        )
        _processor_instance = EmailIngestionProcessor(
            email_connector=connector,
            validation_service=validator,
            case_store=case_store,
        )
    return _processor_instance


@router.post("/poll", response_model=list[dict])
async def poll_hr_mailbox(
    limit: int = 10,
    processor: EmailIngestionProcessor = Depends(get_ingestion_processor),
) -> list[dict]:
    """Poll HR mailbox for new candidate document submissions."""
    records = await processor.poll_and_process(limit=limit)
    return [
        {
            "submission_id": r.submission_id,
            "message_id": r.message_id,
            "case_id": r.case_id,
            "employee_id": r.employee_id,
            "sender_email": r.sender_email,
            "original_filename": r.original_filename,
            "document_type": r.document_type,
            "validation_status": r.validation_status,
            "draft_communication_generated": bool(r.draft_communication),
        }
        for r in records
    ]


@router.get("/submissions", response_model=list[dict])
async def list_submissions(
    processor: EmailIngestionProcessor = Depends(get_ingestion_processor),
) -> list[dict]:
    """List tracked document submissions ingested via email."""
    return [
        {
            "submission_id": r.submission_id,
            "message_id": r.message_id,
            "case_id": r.case_id,
            "employee_id": r.employee_id,
            "document_type": r.document_type,
            "original_filename": r.original_filename,
            "validation_status": r.validation_status,
            "created_at": r.created_at.isoformat(),
        }
        for r in processor.submissions.values()
    ]
