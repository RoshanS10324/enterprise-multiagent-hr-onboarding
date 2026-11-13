"""Unit tests for the email-driven document ingestion pipeline in UC-03."""

import pytest
from datetime import datetime, timezone
from pathlib import Path

from connectors.email.base import EmailAttachment, IncomingOnboardingEmail
from connectors.email.mock import MockEmailConnector
from src.ingestion.classifier import DocumentClassifier
from src.ingestion.processor import EmailIngestionProcessor
from src.ingestion.storage import AttachmentStorageManager
from src.state.enums import DocumentStatus
from src.state.models import OnboardingCase
from src.state.store import InMemoryAuditStore, InMemoryCaseStore
from src.sub_agent.checklist import ChecklistService
from src.sub_agent.extraction import MockDocumentExtractor
from src.sub_agent.validator import DocumentValidationService


@pytest.fixture
def mock_stores():
    case_store = InMemoryCaseStore()
    audit_store = InMemoryAuditStore()
    checklist = ChecklistService("default")
    extractor = MockDocumentExtractor()
    validator = DocumentValidationService(
        case_store=case_store,
        audit_store=audit_store,
        checklist_service=checklist,
        extractor=extractor,
    )
    return case_store, audit_store, validator


@pytest.fixture
def active_case():
    return OnboardingCase(
        case_id="case-101",
        employee_id="emp-101",
        client_id="client-001",
        role="Software Engineer",
        grade="L3",
        nationality="EXP_RESIDENT",
        location="Dubai",
        offer_accepted=True,
        onboarding_form_approved=True,
        employee_name_form="Jane Doe",
        authorized_actor_ids={"email-ingestion-worker", "hr-001"},
    )


@pytest.mark.asyncio
async def test_single_attachment_email_ingestion(mock_stores, active_case, tmp_path):
    case_store, _, validator = mock_stores
    await case_store.save_case(active_case)

    email_connector = MockEmailConnector()
    storage = AttachmentStorageManager(base_storage_dir=tmp_path)
    processor = EmailIngestionProcessor(
        email_connector=email_connector,
        validation_service=validator,
        case_store=case_store,
        storage_manager=storage,
    )

    # Queue an email with a valid passport attachment
    passport_bytes = b"%PDF-1.4 Mock valid passport content"
    email_msg = IncomingOnboardingEmail(
        message_id="msg-001",
        sender_email="jane.doe@example.com",
        sender_name="Jane Doe",
        subject="[CASE-101] My Passport Submission",
        body_text="Hi HR, please find attached my passport copy.",
        attachments=[
            EmailAttachment(
                filename="Jane_Passport.pdf",
                content_type="application/pdf",
                size_bytes=len(passport_bytes),
                raw_bytes=passport_bytes,
            )
        ],
    )
    email_connector.queue_email(email_msg)

    # Process
    records = await processor.poll_and_process()

    assert len(records) == 1
    rec = records[0]
    assert rec.case_id == "case-101"
    assert rec.document_type == "passport"
    assert rec.original_filename == "Jane_Passport.pdf"
    assert rec.validation_status == "COMPLETE"
    assert rec.validation_result.document_status == DocumentStatus.UNDER_REVIEW

    # Email marked as processed
    assert "msg-001" in email_connector.processed_ids

    # HR was notified
    assert len(email_connector.sent_emails) == 1
    assert "Jane Doe" in email_connector.sent_emails[0]["subject"]


@pytest.mark.asyncio
async def test_multi_attachment_fan_out(mock_stores, active_case, tmp_path):
    case_store, _, validator = mock_stores
    await case_store.save_case(active_case)

    email_connector = MockEmailConnector()
    storage = AttachmentStorageManager(base_storage_dir=tmp_path)
    processor = EmailIngestionProcessor(
        email_connector=email_connector,
        validation_service=validator,
        case_store=case_store,
        storage_manager=storage,
    )

    email_msg = IncomingOnboardingEmail(
        message_id="msg-002",
        sender_email="jane.doe@example.com",
        sender_name="Jane Doe",
        subject="[CASE-101] All my onboarding documents",
        body_text="Here is my passport and degree certificate.",
        attachments=[
            EmailAttachment(
                filename="passport.pdf",
                content_type="application/pdf",
                size_bytes=100,
                raw_bytes=b"%PDF passport",
            ),
            EmailAttachment(
                filename="degree_certificate.pdf",
                content_type="application/pdf",
                size_bytes=200,
                raw_bytes=b"%PDF degree",
            ),
        ],
    )
    email_connector.queue_email(email_msg)

    records = await processor.poll_and_process()
    assert len(records) == 2
    types = {r.document_type for r in records}
    assert types == {"passport", "degree_certificate"}


@pytest.mark.asyncio
async def test_email_deduplication(mock_stores, active_case, tmp_path):
    case_store, _, validator = mock_stores
    await case_store.save_case(active_case)

    email_connector = MockEmailConnector()
    storage = AttachmentStorageManager(base_storage_dir=tmp_path)
    processor = EmailIngestionProcessor(
        email_connector=email_connector,
        validation_service=validator,
        case_store=case_store,
        storage_manager=storage,
    )

    email_msg = IncomingOnboardingEmail(
        message_id="msg-dup-001",
        sender_email="jane.doe@example.com",
        sender_name="Jane Doe",
        subject="[CASE-101] Duplicate email test",
        body_text="Passport attached",
        attachments=[
            EmailAttachment(
                filename="passport.pdf",
                content_type="application/pdf",
                size_bytes=100,
                raw_bytes=b"%PDF passport",
            )
        ],
    )

    # First run processes successfully
    r1 = await processor.process_email(email_msg)
    assert len(r1) == 1

    # Second run with same message_id is ignored
    r2 = await processor.process_email(email_msg)
    assert len(r2) == 0


@pytest.mark.asyncio
async def test_password_protected_pdf_rejection(mock_stores, active_case, tmp_path):
    case_store, _, validator = mock_stores
    await case_store.save_case(active_case)

    email_connector = MockEmailConnector()
    storage = AttachmentStorageManager(base_storage_dir=tmp_path)
    processor = EmailIngestionProcessor(
        email_connector=email_connector,
        validation_service=validator,
        case_store=case_store,
        storage_manager=storage,
    )

    # Simulated encrypted PDF containing /Encrypt dictionary
    encrypted_bytes = b"%PDF-1.5 \n trailer << /Encrypt 12 0 R >>"
    email_msg = IncomingOnboardingEmail(
        message_id="msg-enc-001",
        sender_email="jane.doe@example.com",
        sender_name="Jane Doe",
        subject="[CASE-101] Encrypted document",
        body_text="Here is my encrypted statement",
        attachments=[
            EmailAttachment(
                filename="bank_statement.pdf",
                content_type="application/pdf",
                size_bytes=len(encrypted_bytes),
                raw_bytes=encrypted_bytes,
            )
        ],
    )

    records = await processor.process_email(email_msg)
    assert len(records) == 1
    rec = records[0]
    assert rec.validation_status == "FAILED_ENCRYPTED"
    assert rec.validation_result.document_status == DocumentStatus.REJECTED
    assert rec.draft_communication is not None
    assert "replacement" in rec.draft_communication.lower()
