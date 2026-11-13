"""3-Tier Document Classifier for candidate email attachments.

Tiers:
1. Fast-path Filename heuristic (0 token cost, sub-millisecond)
2. Email subject and body context heuristic (0 token cost)
3. LLM / OCR content analysis fallback (invoked only when 1 and 2 are inconclusive)
"""

from __future__ import annotations

import logging
import re
from typing import Tuple

log = logging.getLogger("uc03.ingestion.classifier")

# Canonical checklist document types supported in UC-03
VALID_DOCUMENT_TYPES = {
    "signed_offer_letter",
    "passport",
    "emirates_id",
    "degree_certificate",
    "experience_letter",
    "payslips",
    "non_disclosure_agreement",
    "policy_acknowledgements",
    "medical_fitness_certificate",
    "visa_documents",
}

# Tier 1 & 2 Keyword dictionaries
_DOCUMENT_KEYWORDS: dict[str, list[str]] = {
    "passport": ["passport", "pass_port", "ppt", "travel_doc"],
    "emirates_id": ["emirates_id", "eid", "national_id", "id_card", "resident_id"],
    "degree_certificate": ["degree", "diploma", "graduation", "certificate", "bachelor", "master", "phd", "transcript"],
    "signed_offer_letter": ["offer", "offer_letter", "signed_offer", "employment_contract", "appointment_letter"],
    "experience_letter": ["experience", "relieving", "service_cert", "employment_cert", "recommendation"],
    "payslips": ["payslip", "pay_slip", "salary", "paystub", "wage", "bank_statement", "statement", "bank"],
    "non_disclosure_agreement": ["nda", "non_disclosure", "confidentiality"],
    "policy_acknowledgements": ["policy", "handbook", "code_of_conduct", "acknowledgement"],
    "medical_fitness_certificate": ["medical", "fitness", "health_report", "blood_test"],
    "visa_documents": ["visa", "entry_permit", "residence_visa", "visa_copy"],
}


class DocumentClassifier:
    """Classifies email attachments into canonical UC-03 document types."""

    def __init__(self, confidence_threshold: float = 0.70) -> None:
        self.confidence_threshold = confidence_threshold

    async def classify(
        self,
        filename: str,
        subject: str = "",
        body: str = "",
        first_page_text: str = "",
    ) -> Tuple[str, float, str]:
        """Classify document and return (document_type, confidence, tier_used)."""
        # Tier 1: Filename heuristic
        t1_type = self._classify_text(filename)
        if t1_type:
            log.info("Tier 1 classified %s -> %s (confidence 0.95)", filename, t1_type)
            return t1_type, 0.95, "tier1_filename"

        # Tier 2: Email subject & body heuristic
        combined_context = f"{subject} {body}"
        t2_type = self._classify_text(combined_context)
        if t2_type:
            log.info("Tier 2 classified %s via email text -> %s (confidence 0.85)", filename, t2_type)
            return t2_type, 0.85, "tier2_email_context"

        # Tier 3: First page OCR text heuristic or LLM fallback
        if first_page_text:
            t3_type = self._classify_text(first_page_text)
            if t3_type:
                log.info("Tier 3 classified %s via OCR content -> %s (confidence 0.80)", filename, t3_type)
                return t3_type, 0.80, "tier3_ocr_content"

        # If completely unclassifiable, return generic/unspecified
        log.warning("Could not classify document '%s'", filename)
        return "unspecified_document", 0.30, "unclassified"

    def _classify_text(self, text: str) -> str | None:
        """Scan text using tokenized matching against keyword dictionaries."""
        # Replace non-alphanumeric (including underscores) with spaces
        normalized = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
        tokens = set(normalized.split())

        for doc_type, keywords in _DOCUMENT_KEYWORDS.items():
            for kw in keywords:
                clean_kw = kw.replace("_", " ")
                if " " in clean_kw:
                    if clean_kw in normalized:
                        return doc_type
                elif kw in tokens:
                    return doc_type
        return None
