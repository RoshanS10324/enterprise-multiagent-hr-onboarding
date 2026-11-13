from __future__ import annotations

import re
from datetime import date

from knowledge.checklists.loader import DocumentRequirement
from src.state.enums import (
    ApprovalStatus,
    ComplianceStatus,
    EvidenceStatus,
    HRReviewStatus,
    OnboardingStatus,
    RequiredAction,
    ValidationFinding,
)

LOW_CONFIDENCE_THRESHOLD = 0.7

_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"you\s+are\s+now\s+",
        r"disregard\s+(all\s+)?",
        r"forget\s+(all\s+)?previous",
        r"new\s+instructions?:",
        r"system\s*:",
        r"act\s+as\s+(if\s+you\s+are|a\s+)",
        r"jailbreak",
        r"DAN\s+mode",
        r"prompt\s+injection",
        r"override\s+your\s+(instructions?|programming)",
        r"do\s+not\s+follow\s+",
        r"reveal\s+your\s+(system\s+)?prompt",
        r"print\s+your\s+(system\s+)?prompt",
    ]
]


def check_onboarding_start_conditions(
    offer_accepted: bool, onboarding_form_approved: bool
) -> bool:
    """BR-01: Both conditions must be met before document processing begins."""
    return offer_accepted and onboarding_form_approved


def check_name_consistency(
    name_from_form: str,
    extracted_name: str | None,
    confidence: float,
) -> tuple[bool, ValidationFinding | None]:
    """BR-06, BR-07: Detect name mismatches and low-confidence extractions."""
    if extracted_name is None:
        return False, ValidationFinding.LOW_CONFIDENCE
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        return False, ValidationFinding.LOW_CONFIDENCE
    normalized_form = name_from_form.strip().lower()
    normalized_extracted = extracted_name.strip().lower()
    if normalized_form != normalized_extracted:
        return False, ValidationFinding.NAME_MISMATCH
    return True, None


def check_expiry(
    expiry_date: date | None,
    validation_date: date,
    check_required: bool,
) -> ValidationFinding | None:
    """BR-05: Return EXPIRED_DOCUMENT if past expiry and check is required."""
    if not check_required:
        return None
    if expiry_date is None:
        return ValidationFinding.MISSING_FIELDS
    if expiry_date < validation_date:
        return ValidationFinding.EXPIRED_DOCUMENT
    return None


def check_readability(readable: bool | None) -> ValidationFinding | None:
    """BR-05: Unknown or False readability is flagged as UNREADABLE."""
    if readable is False or readable is None:
        return ValidationFinding.UNREADABLE
    return None


def check_required_fields(present: bool | None) -> ValidationFinding | None:
    """BR-05: Unknown or False field presence is flagged as MISSING_FIELDS."""
    if present is False or present is None:
        return ValidationFinding.MISSING_FIELDS
    return None


def check_evidence_stored(evidence_stored: bool, requires_evidence: bool) -> EvidenceStatus:
    """BR-09: Return appropriate EvidenceStatus."""
    if not requires_evidence:
        return EvidenceStatus.STORED
    if evidence_stored:
        return EvidenceStatus.STORED
    return EvidenceStatus.NOT_STORED


def check_extraction_confidence(confidence: float) -> ValidationFinding | None:
    """BR-07: Flag low-confidence extractions for HR review."""
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        return ValidationFinding.LOW_CONFIDENCE
    return None


def check_attestation(attestation_confirmed: bool, requires_attestation: bool) -> ValidationFinding | None:
    """BR-attestation: NDA/policy docs require explicit employee acknowledgement in the system."""
    if requires_attestation and not attestation_confirmed:
        return ValidationFinding.MISSING_FIELDS
    return None


def check_prompt_injection(text: str | None) -> bool:
    """BR-16: Return True if the text contains prompt-injection patterns."""
    if not text:
        return False
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return True
    return False


def is_document_required(
    document_type: str,
    applicable_docs: list[DocumentRequirement],
) -> bool:
    """BR-03, BR-15: Return True only if the document is in the applicable checklist."""
    return any(d.id == document_type for d in applicable_docs)


def determine_compliance_status(
    findings: list[ValidationFinding],
    evidence_status: EvidenceStatus,
    hr_review_status: HRReviewStatus,
    approval_status: ApprovalStatus,
) -> ComplianceStatus:
    """BR-09, BR-11: COMPLETE requires no blocking findings, stored evidence, and HR approval."""
    blocking = {
        ValidationFinding.NAME_MISMATCH,
        ValidationFinding.MISSING_DOCUMENT,
        ValidationFinding.EXPIRED_DOCUMENT,
        ValidationFinding.UNREADABLE,
        ValidationFinding.WRONG_TYPE,
        ValidationFinding.MISSING_FIELDS,
        ValidationFinding.LOW_CONFIDENCE,
        ValidationFinding.UNSUPPORTED_LANGUAGE,
        ValidationFinding.UNREQUESTED_SENSITIVE,
        ValidationFinding.PROMPT_INJECTION_DETECTED,
        ValidationFinding.EVIDENCE_NOT_STORED,
        ValidationFinding.CHECKLIST_NOT_SPECIFIED,
        ValidationFinding.DUPLICATE_REQUEST,
    }
    if any(f in blocking for f in findings):
        return ComplianceStatus.BLOCKED
    if evidence_status not in (EvidenceStatus.STORED,):
        return ComplianceStatus.INCOMPLETE
    if hr_review_status not in (HRReviewStatus.APPROVED, HRReviewStatus.NOT_REQUIRED):
        return ComplianceStatus.PENDING_APPROVAL
    if approval_status not in (ApprovalStatus.APPROVED, ApprovalStatus.NOT_REQUIRED):
        return ComplianceStatus.PENDING_APPROVAL
    return ComplianceStatus.COMPLETE


def determine_next_action(
    findings: list[ValidationFinding],
    compliance_status: ComplianceStatus,
    onboarding_on_hold: bool,
    injection_detected: bool,
    access_denied: bool,
) -> RequiredAction:
    """Map the current state to the single most important required action."""
    if access_denied:
        return RequiredAction.DENY_ACCESS
    if injection_detected or ValidationFinding.PROMPT_INJECTION_DETECTED in findings:
        return RequiredAction.FLAG_INJECTION
    if ValidationFinding.DUPLICATE_REQUEST in findings:
        return RequiredAction.HR_REVIEW
    if onboarding_on_hold:
        return RequiredAction.NONE
    if ValidationFinding.UNREQUESTED_SENSITIVE in findings:
        return RequiredAction.APPLY_RETENTION_POLICY
    if ValidationFinding.CHECKLIST_NOT_SPECIFIED in findings:
        return RequiredAction.HR_REVIEW
    if ValidationFinding.NAME_MISMATCH in findings:
        return RequiredAction.HR_REVIEW
    if ValidationFinding.LOW_CONFIDENCE in findings:
        return RequiredAction.HR_REVIEW
    if ValidationFinding.UNSUPPORTED_LANGUAGE in findings:
        return RequiredAction.HR_REVIEW
    if ValidationFinding.MISSING_DOCUMENT in findings:
        return RequiredAction.SEND_REMINDER
    if ValidationFinding.EXPIRED_DOCUMENT in findings:
        return RequiredAction.SEND_REPLACEMENT_REQUEST
    if ValidationFinding.UNREADABLE in findings:
        return RequiredAction.SEND_REPLACEMENT_REQUEST
    if ValidationFinding.WRONG_TYPE in findings:
        return RequiredAction.SEND_REPLACEMENT_REQUEST
    if ValidationFinding.MISSING_FIELDS in findings:
        return RequiredAction.SEND_REPLACEMENT_REQUEST
    if ValidationFinding.EVIDENCE_NOT_STORED in findings:
        return RequiredAction.HR_REVIEW
    if compliance_status == ComplianceStatus.COMPLETE:
        return RequiredAction.NONE
    if compliance_status == ComplianceStatus.PENDING_APPROVAL:
        return RequiredAction.HUMAN_CONFIRMATION
    return RequiredAction.NONE


def determine_overall_status(
    onboarding_on_hold: bool,
    findings: list[ValidationFinding],
    compliance_status: ComplianceStatus,
) -> OnboardingStatus:
    """Map findings and compliance to the top-level OnboardingStatus."""
    if onboarding_on_hold:
        return OnboardingStatus.ONBOARDING_ON_HOLD
    blocking_hr = {
        ValidationFinding.NAME_MISMATCH,
        ValidationFinding.LOW_CONFIDENCE,
        ValidationFinding.UNSUPPORTED_LANGUAGE,
        ValidationFinding.UNREQUESTED_SENSITIVE,
        ValidationFinding.CHECKLIST_NOT_SPECIFIED,
        ValidationFinding.PROMPT_INJECTION_DETECTED,
        ValidationFinding.EVIDENCE_NOT_STORED,
    }
    action_required = {
        ValidationFinding.MISSING_DOCUMENT,
        ValidationFinding.EXPIRED_DOCUMENT,
        ValidationFinding.UNREADABLE,
        ValidationFinding.WRONG_TYPE,
        ValidationFinding.MISSING_FIELDS,
    }
    if any(f in blocking_hr for f in findings):
        return OnboardingStatus.HR_REVIEW_REQUIRED
    if any(f in action_required for f in findings):
        return OnboardingStatus.ACTION_REQUIRED
    if compliance_status == ComplianceStatus.COMPLETE:
        return OnboardingStatus.COMPLETE
    if compliance_status == ComplianceStatus.PENDING_APPROVAL:
        return OnboardingStatus.READY_FOR_HUMAN_CONFIRMATION
    if not findings or findings == [ValidationFinding.VALID]:
        return OnboardingStatus.NO_AUTOMATED_ISSUES_FOUND
    return OnboardingStatus.INCOMPLETE
