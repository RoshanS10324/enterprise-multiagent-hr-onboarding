from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from guardrails.injection import scan_for_injection

MAX_TEXT_FOR_LLM = 4_000
LOW_CONFIDENCE = 0.70

_FIELD_PATTERNS: dict[str, dict[str, str]] = {
    "passport": {
        "passport_number": r"(?:passport\s*(?:no|number|#)[:\s]*)?([A-Z]{1,2}\d{6,9})",
        "nationality": r"nationality[:\s]+([A-Za-z ]+)",
        "expiry_date": r"(?:expiry|expiration|date of expiry)[:\s]*([\d]{1,2}[\/\-\. ][\d]{1,2}[\/\-\. ][\d]{2,4})",
        "date_of_birth": r"(?:date of birth|dob|birth date)[:\s]*([\d]{1,2}[\/\-\. ][\d]{1,2}[\/\-\. ][\d]{2,4})",
    },
    "emirates_id": {
        "emirates_id_number": r"(784[-\s]?\d{4}[-\s]?\d{7}[-\s]?\d{1})",
        "expiry_date": r"(?:expiry|expiration)[:\s]*([\d]{1,2}[\/\-\. ][\d]{1,2}[\/\-\. ][\d]{2,4})",
    },
    "degree_certificate": {
        "institution": r"(?:university|college|institute)[:\s]*([^\n]+)",
        "qualification": r"((?:bachelor|master|phd|diploma|degree)[^\n]*)",
        "graduation_year": r"\b((?:19|20)\d{2})\b",
    },
    "signed_offer_letter": {
        "employer": r"(?:from|by|signed by)[:\s]*([^\n]+)",
        "joining_date": r"(?:joining|commencement|start)[:\s]*([\d]{1,2}[\/\-\. ][\d]{1,2}[\/\-\. ][\d]{2,4})",
        "designation": r"(?:position|role|designation|title)[:\s]*([^\n]+)",
    },
}

_NAME_PATTERNS = [
    r"(?:name|full name|surname)[:\s]+([A-Za-z][A-Za-z '\-]{2,60})",
    r"(?:mr|mrs|ms|dr)\.?\s+([A-Za-z][A-Za-z '\-]{2,60})",
]


class ExtractionResult(BaseModel):
    extracted_name: str | None
    extracted_fields: dict[str, str | None]
    confidence: float
    injection_suspected: bool
    language: str | None
    supported_language: bool


@runtime_checkable
class DocumentExtractor(Protocol):
    async def extract(self, document_ref: str, document_type: str) -> ExtractionResult: ...


# ─── Mock (tests / local-dev) ──────────────────────────────────────────────────

class MockDocumentExtractor:
    """Test/local-dev extractor. Returns configurable results."""

    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        self._overrides: dict[str, Any] = overrides or {}

    async def extract(self, document_ref: str, document_type: str) -> ExtractionResult:
        defaults: dict[str, Any] = {
            "extracted_name": None,
            "extracted_fields": {},
            "confidence": 1.0,
            "injection_suspected": False,
            "language": "en",
            "supported_language": True,
        }
        merged = {**defaults, **self._overrides}
        return ExtractionResult(**merged)


# ─── Local (PyMuPDF + pytesseract) ────────────────────────────────────────────

class LocalDocumentExtractor:
    """
    Extracts text from PDFs/images without any external service.

    PyMuPDF handles native-text PDFs. If the page has no selectable text
    (scanned image), pytesseract OCR is used as a fallback.
    Field extraction is regex-based; confidence is derived from text richness.
    """

    async def extract(self, document_ref: str, document_type: str) -> ExtractionResult:
        text = await self._read_text(document_ref)
        if not text.strip():
            return ExtractionResult(
                extracted_name=None,
                extracted_fields={},
                confidence=0.0,
                injection_suspected=False,
                language=None,
                supported_language=False,
            )

        scan = scan_for_injection(text)
        fields = self._extract_fields(text, document_type)
        name = self._extract_name(text)
        confidence = self._compute_confidence(text, fields)

        return ExtractionResult(
            extracted_name=name,
            extracted_fields=fields,
            confidence=confidence,
            injection_suspected=scan.detected,
            language="en",
            supported_language=True,
        )

    async def _read_text(self, document_ref: str) -> str:
        try:
            try:
                import fitz  # PyMuPDF (<=1.x)
            except ImportError:
                import pymupdf as fitz  # type: ignore[no-redef]  # PyMuPDF 2.x
        except ImportError:
            return ""

        try:
            doc = fitz.open(document_ref)
        except Exception:
            return ""

        pages: list[str] = []
        for page in doc:
            page_text = page.get_text()
            if page_text.strip():
                pages.append(page_text)
            else:
                pages.append(self._ocr_page(page))
        doc.close()
        return "\n".join(pages)

    def _ocr_page(self, page: Any) -> str:
        try:
            import io

            import pytesseract
            from PIL import Image
            pix = page.get_pixmap(dpi=200)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            return pytesseract.image_to_string(img)
        except Exception:
            return ""

    def _extract_fields(self, text: str, document_type: str) -> dict[str, str | None]:
        patterns = _FIELD_PATTERNS.get(document_type, {})
        result: dict[str, str | None] = {}
        for field, pattern in patterns.items():
            m = re.search(pattern, text, re.IGNORECASE)
            result[field] = m.group(1).strip() if m else None
        return result

    def _extract_name(self, text: str) -> str | None:
        for pattern in _NAME_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return None

    def _compute_confidence(self, text: str, fields: dict[str, str | None]) -> float:
        word_count = len(text.split())
        if word_count < 20:
            return 0.3
        found = sum(1 for v in fields.values() if v)
        total = max(len(fields), 1)
        field_ratio = found / total
        length_score = min(word_count / 300, 1.0) * 0.3
        return round(0.4 + field_ratio * 0.3 + length_score, 3)


# ─── LLM (Claude fallback) ────────────────────────────────────────────────────

_CANDIDATE_PROMPT_PATHS = [
    Path(__file__).resolve().parents[2] / "brain" / "onboarding" / "uc03_extraction.md",
    Path(__file__).resolve().parents[3] / "brain" / "onboarding" / "uc03_extraction.md",
    Path(__file__).parent.parent.parent.parent.parent / "brain" / "onboarding" / "uc03_extraction.md",
]
_PROMPT_PATH = next((p for p in _CANDIDATE_PROMPT_PATHS if p.exists()), _CANDIDATE_PROMPT_PATHS[0])



class LLMDocumentExtractor:
    """
    The low-confidence fallback extractor. Called only when local confidence < threshold.

    It no longer sends document text to any model (see `_call_llm`): it keeps the injection scan and returns the safe low-confidence
    defaults, so such documents go to HR review. The class and its `client` parameter are kept so existing callers and the hybrid
    routing are unchanged; `client` is ignored. No provider SDK is imported.
    """

    def __init__(self, max_text: int = MAX_TEXT_FOR_LLM, client: Any = None) -> None:
        self._max_text = max_text
        self._client = client
        self._system_prompt = _PROMPT_PATH.read_text(encoding="utf-8") if _PROMPT_PATH.exists() else ""

    async def extract(self, document_ref: str, document_type: str) -> ExtractionResult:
        text = await self._read_text_snippet(document_ref)

        scan = scan_for_injection(text)
        if scan.detected:
            return ExtractionResult(
                extracted_name=None,
                extracted_fields={},
                confidence=0.0,
                injection_suspected=True,
                language=None,
                supported_language=False,
            )

        payload = await self._call_llm(text, document_type)
        return self._parse(payload, document_type)

    async def _read_text_snippet(self, document_ref: str) -> str:
        try:
            try:
                import fitz
            except ImportError:
                import pymupdf as fitz  # type: ignore[no-redef]
        except ImportError:
            return ""

        try:
            doc = fitz.open(document_ref)
            text = "".join(page.get_text() for page in doc)
            doc.close()
            return text[: self._max_text]
        except Exception:
            return ""

    async def _call_llm(self, text: str, document_type: str) -> dict[str, Any]:
        """No model call: document text never leaves UC-03.

        This path used to send the first part of the document text (passport, ID, visa, degree contents) to an external model. UC-03's
        privacy rule forbids sending raw identity documents or their contents to any model, so it now returns no fields, exactly what
        the previous no-API-key path returned. `_parse` then gives the safe defaults (no name, confidence 0.5), which is below UC-03's
        0.7 threshold, so the document is routed to HR review instead of being read by a model. `text` and any injected `client` are
        deliberately unused.
        """
        return {}

    def _parse(self, payload: dict[str, Any], document_type: str) -> ExtractionResult:
        fields: dict[str, str | None] = {
            k: (str(v) if v is not None else None)
            for k, v in payload.items()
            if k not in {"extracted_name", "confidence", "injection_suspected", "language", "supported_language"}
        }
        return ExtractionResult(
            extracted_name=payload.get("extracted_name"),
            extracted_fields=fields,
            confidence=float(payload.get("confidence", 0.5)),
            injection_suspected=bool(payload.get("injection_suspected", False)),
            language=payload.get("language", "en"),
            supported_language=bool(payload.get("supported_language", True)),
        )


# ─── Hybrid ───────────────────────────────────────────────────────────────────

class HybridDocumentExtractor:
    """
    Tries local extraction first; falls back to LLM when confidence < threshold.

    Keeps PII on-premises for the common case (native-text PDFs) and uses
    the LLM only for scanned/handwritten/low-quality documents.
    """

    CONFIDENCE_THRESHOLD = LOW_CONFIDENCE

    def __init__(self) -> None:
        self._local: DocumentExtractor = LocalDocumentExtractor()
        self._llm: DocumentExtractor = LLMDocumentExtractor()

    async def extract(self, document_ref: str, document_type: str) -> ExtractionResult:
        local_result = await self._local.extract(document_ref, document_type)
        if local_result.confidence >= self.CONFIDENCE_THRESHOLD:
            return local_result
        return await self._llm.extract(document_ref, document_type)


# ─── Factory ──────────────────────────────────────────────────────────────────

async def get_extractor() -> DocumentExtractor:
    """
    Returns HybridDocumentExtractor in production (HR_AGENT_ENV != local/test/ci).
    Returns MockDocumentExtractor for local dev and CI.
    """
    env = os.environ.get("HR_AGENT_ENV", "local").lower()
    if env in {"local", "test", "ci"}:
        return MockDocumentExtractor()
    return HybridDocumentExtractor()
