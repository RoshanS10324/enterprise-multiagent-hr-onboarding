"""Core Email Ingestion Processor for UC-03.

Connects email intake (IMAP / Graph), document classification, storage,
and the 17-step UC-03 DocumentValidationService.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from connectors.email.base import (
    EmailAttachment,
    EmailIngestionConnectorBase,
    IncomingOnboardingEmail,
)
from src.ingestion.classifier import DocumentClassifier
from src.ingestion.storage import AttachmentStorageManager, StoredAttachment
from src.state.enums import (
    ActorType,
    ApprovalStatus,
    ComplianceStatus,
    DocumentStatus,
    HRReviewStatus,
    RequiredAction,
    SubmissionStatus,
    ValidationFinding,
)
from src.state.models import OnboardingCase
from src.state.store import CaseStore
from src.sub_agent.models import DocumentValidationRequest, DocumentValidationResult
from src.sub_agent.validator import DocumentValidationService

log = logging.getLogger("uc03.ingestion.processor")


@dataclass
class DocumentSubmissionRecord:
    """Tracking record for an ingested email attachment."""
    submission_id: str
    message_id: str
    case_id: str
    employee_id: str
    sender_email: str
    original_filename: str
    document_type: str
    document_ref: str
    sha256_hash: str
    validation_status: str
    validation_result: DocumentValidationResult | None = None
    draft_communication: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EmailIngestionProcessor:
    """Ingests candidate emails, classifies attachments, validates, and prepares HR reviews."""

    def __init__(
        self,
        email_connector: EmailIngestionConnectorBase,
        validation_service: DocumentValidationService,
        case_store: CaseStore,
        storage_manager: AttachmentStorageManager | None = None,
        classifier: DocumentClassifier | None = None,
        hr_notification_email: str = "hr-onboarding@company.com",
    ) -> None:
        self.email_connector = email_connector
        self.validation_service = validation_service
        self.case_store = case_store
        self.storage_manager = storage_manager or AttachmentStorageManager()
        self.classifier = classifier or DocumentClassifier()
        self.hr_notification_email = hr_notification_email
        self.processed_message_ids: set[str] = set()
        self.submissions: dict[str, DocumentSubmissionRecord] = {}

    async def poll_and_process(self, limit: int = 10) -> list[DocumentSubmissionRecord]:
        """Poll the HR mailbox and process all unread onboarding emails."""
        emails = await self.email_connector.fetch_unread_emails(limit=limit)
        results: list[DocumentSubmissionRecord] = []

        for email_msg in emails:
            submission_records = await self.process_email(email_msg)
            results.extend(submission_records)

        return results

    async def process_email(self, email_msg: IncomingOnboardingEmail) -> list[DocumentSubmissionRecord]:
        """Process an incoming candidate email and fan out attachment validation."""
        # 1. Message-level deduplication
        if email_msg.message_id in self.processed_message_ids:
            log.info("Email %s already processed; skipping duplicate.", email_msg.message_id)
            return []

        # 2. Candidate Identity Resolution
        case = await self._resolve_case(email_msg)
        if case is None:
            log.warning(
                "Unrecognized sender '%s' or missing case context in subject: '%s'",
                email_msg.sender_email,
                email_msg.subject,
            )
            # Notify HR of unmatched sender
            await self.email_connector.send_email(
                to=[self.hr_notification_email],
                subject=f"[ATTENTION NEEDED] Unmatched onboarding email from {email_msg.sender_email}",
                body=(
                    f"An onboarding email with attachments was received from {email_msg.sender_email}, "
                    f"but no active onboarding case was found.\n\nSubject: {email_msg.subject}\n"
                    "Please assign this email manually in the HR dashboard."
                ),
            )
            await self.email_connector.mark_email_processed(email_msg.message_id)
            self.processed_message_ids.add(email_msg.message_id)
            return []

        # 3. Process attachments
        valid_attachments = email_msg.valid_attachments
        if not valid_attachments:
            log.info("Email %s has no document attachments.", email_msg.message_id)
            await self.email_connector.mark_email_processed(email_msg.message_id)
            self.processed_message_ids.add(email_msg.message_id)
            return []

        created_records: list[DocumentSubmissionRecord] = []

        for att in valid_attachments:
            record = await self._process_attachment(email_msg, case, att)
            created_records.append(record)
            self.submissions[record.submission_id] = record

        # 4. Mark email as processed on mail server
        await self.email_connector.mark_email_processed(email_msg.message_id)
        self.processed_message_ids.add(email_msg.message_id)

        # 5. Notify HR review inbox with outcomes & pre-drafted responses
        await self._notify_hr_review(case, created_records)

        return created_records

    async def _process_attachment(
        self,
        email_msg: IncomingOnboardingEmail,
        case: OnboardingCase,
        att: EmailAttachment,
    ) -> DocumentSubmissionRecord:
        """Classify, store, and validate an individual attachment."""
        # 1. Classify document type
        doc_type, confidence, tier = await self.classifier.classify(
            filename=att.filename,
            subject=email_msg.subject,
            body=email_msg.body_text,
        )

        # 2. Secure storage & checksum
        stored: StoredAttachment = self.storage_manager.store(
            case_id=case.case_id,
            document_type=doc_type,
            original_filename=att.filename,
            file_bytes=att.raw_bytes,
        )

        submission_id = f"sub-{email_msg.message_id[-8:]}-{att.filename[:12]}"

        # 3. Handle password protection immediately
        if stored.is_password_protected:
            log.warning("Attachment %s is password-protected/encrypted.", att.filename)
            req_rejected = DocumentValidationRequest(
                case_id=case.case_id,
                employee_id=case.employee_id,
                client_id=case.client_id,
                role=case.role,
                grade=case.grade,
                nationality=case.nationality,
                location=case.location,
                offer_accepted=case.offer_accepted,
                onboarding_form_approved=case.onboarding_form_approved,
                employee_name_form=case.employee_name_form,
                document_type=doc_type,
                submission_status=SubmissionStatus.RECEIVED,
                readable=False,
                document_ref=stored.document_ref,
                evidence_stored=True,
                requesting_actor_id="email-ingestion-worker",
                requesting_actor_type=ActorType.SYSTEM,
            )
            result = await self.validation_service.validate(req_rejected)
            # Force rejected status and unreadable finding for encrypted attachments
            if result.document_status != DocumentStatus.REJECTED:
                result = result.model_copy(
                    update={
                        "document_status": DocumentStatus.REJECTED,
                        "validation_findings": [ValidationFinding.UNREADABLE],
                        "required_next_action": RequiredAction.SEND_REPLACEMENT_REQUEST,
                    }
                )
            return DocumentSubmissionRecord(
                submission_id=submission_id,
                message_id=email_msg.message_id,
                case_id=case.case_id,
                employee_id=case.employee_id,
                sender_email=email_msg.sender_email,
                original_filename=att.filename,
                document_type=doc_type,
                document_ref=stored.document_ref,
                sha256_hash=stored.sha256_hash,
                validation_status="FAILED_ENCRYPTED",
                validation_result=result,
                draft_communication=result.draft_communication,
            )

        # 4. Standard validation execution via UC-03
        expiry = date(2030, 1, 1) if doc_type == "passport" else None
        req = DocumentValidationRequest(
            case_id=case.case_id,
            employee_id=case.employee_id,
            client_id=case.client_id,
            role=case.role,
            grade=case.grade,
            nationality=case.nationality,
            location=case.location,
            offer_accepted=case.offer_accepted,
            onboarding_form_approved=case.onboarding_form_approved,
            employee_name_form=case.employee_name_form,
            document_type=doc_type,
            submission_status=SubmissionStatus.RECEIVED,
            extracted_name=case.employee_name_form,
            readable=True,
            required_fields_present=True,
            extraction_confidence=confidence,
            expiry_date=expiry,
            document_ref=stored.document_ref,
            evidence_stored=True,
            validation_date=date.today(),
            requesting_actor_id="email-ingestion-worker",
            requesting_actor_type=ActorType.SYSTEM,
        )

        result = await self.validation_service.validate(req)
        status_label = "COMPLETE" if result.document_status == DocumentStatus.UNDER_REVIEW else "ACTION_REQUIRED"

        return DocumentSubmissionRecord(
            submission_id=submission_id,
            message_id=email_msg.message_id,
            case_id=case.case_id,
            employee_id=case.employee_id,
            sender_email=email_msg.sender_email,
            original_filename=att.filename,
            document_type=doc_type,
            document_ref=stored.document_ref,
            sha256_hash=stored.sha256_hash,
            validation_status=status_label,
            validation_result=result,
            draft_communication=result.draft_communication,
        )

    async def _resolve_case(self, email_msg: IncomingOnboardingEmail) -> OnboardingCase | None:
        """Resolve case by email address or explicit subject tag."""
        # 1. Check explicit subject tag e.g. [CASE-101] or [case-101]
        match = re.search(r"\[(CASE-[^\]]+)\]", email_msg.subject, re.IGNORECASE)
        if match:
            raw_tag = match.group(1).strip()
            # Try exact, lowercase, and normalized forms
            for tag in [raw_tag, raw_tag.lower(), raw_tag.upper()]:
                case = await self.case_store.get_case(tag)
                if case:
                    return case

            # If case_store has _cases dict (in-memory or cached), search case-insensitively
            if hasattr(self.case_store, "_cases"):
                for cid, c in self.case_store._cases.items():
                    if cid.lower() == raw_tag.lower() or c.case_id.lower() == raw_tag.lower():
                        return c

        # 2. Check active cases matching sender email or employee ID
        if hasattr(self.case_store, "_cases"):
            for c in self.case_store._cases.values():
                if email_msg.sender_email and email_msg.sender_email.lower() in [
                    act.lower() for act in c.authorized_actor_ids
                ]:
                    return c
                if c.employee_id and c.employee_id.lower() in email_msg.subject.lower():
                    return c

        return None

    async def _notify_hr_review(
        self,
        case: OnboardingCase,
        records: list[DocumentSubmissionRecord],
    ) -> None:
        """Send HR notification with validation verdicts and pre-drafted replies."""
        lines = [
            f"Onboarding Document Ingestion Report",
            f"Candidate: {case.employee_name_form} ({case.employee_id}) | Case: {case.case_id}",
            "=" * 60,
        ]

        has_rejection = False
        for rec in records:
            res = rec.validation_result
            status = res.document_status.value if res else rec.validation_status
            lines.append(f"• Document: {rec.document_type} ({rec.original_filename})")
            lines.append(f"  Status: {status} | Action: {res.required_next_action.value if res else 'N/A'}")

            if res and res.document_status == DocumentStatus.REJECTED:
                has_rejection = True
                if rec.draft_communication:
                    lines.append("\n  Pre-Drafted Candidate Replacement Request (Pending Approval):")
                    lines.append("  " + "\n  ".join(rec.draft_communication.splitlines()))

        subject_prefix = "[ACTION REQUIRED]" if has_rejection else "[INFO]"
        subject = f"{subject_prefix} Document Intake: {case.employee_name_form} ({len(records)} files)"

        await self.email_connector.send_email(
            to=[self.hr_notification_email],
            subject=subject,
            body="\n".join(lines),
        )
