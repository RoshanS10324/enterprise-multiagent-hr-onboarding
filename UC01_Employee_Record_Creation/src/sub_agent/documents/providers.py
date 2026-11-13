"""Where an OnboardingDocumentExtraction comes from.

Two providers, one result type. Everything downstream -
`DocumentIntakeService.process_extraction` (validation, HR review, the
unchanged open_onboarding_case flow) - is identical for both, so choosing
the mock provider replaces ONLY the document-reading step:

* DocxExtractionProvider - reads a real onboarding-form .docx (docx_reader +
  extraction). The production path.
* MockExtractionProvider - loads a SYNTHETIC JSON fixture that stands for
  "what extraction would have returned" (data/mock/extracted_employee_data.json).
  For demos and tests where parsing a document is not the point.

The mock fixture states values the same way extraction would see them:

    "designation": "Synthetic Data Engineer"          -> PRESENT
    "reporting_to": null                              -> MISSING
    "joining_date": {"value": "2027-01-11", "status": "UNCERTAIN",
                     "note": "two dates on the form"} -> as stated

Types are checked as extraction would check them (a date must be an ISO
date, probation must be 3 or 6, a document status RECEIVED or PENDING);
anything that does not parse becomes INVALID, never a guess. Salary and
personal details become SecretStr exactly as extraction makes them. A
fixture that is not marked synthetic is refused.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from pydantic import SecretStr

from .budget import OnboardingBudget, RowText, build_budget
from .docx_reader import read_docx
from .pdf_reader import read_pdf
from .extraction import extract_onboarding_form
from .models import (
    DocumentStatus,
    ExtractedField,
    ExtractionMetadata,
    FieldStatus,
    OnboardingDocumentExtraction,
    RolesAndResponsibilities,
    TEMPLATE_ID,
)


class ExtractionProvider(Protocol):
    """Produces one extraction. `source` is recorded in the audit trail."""

    source: str

    def extract(self) -> OnboardingDocumentExtraction: ...


class DocxExtractionProvider:
    source = "docx"

    def __init__(self, data: bytes, filename: str) -> None:
        self._data, self._filename = data, filename

    def extract(self) -> OnboardingDocumentExtraction:
        return extract_onboarding_form(read_docx(self._data, self._filename), document_name=self._filename,
                                       sha256=hashlib.sha256(self._data).hexdigest(), size_bytes=len(self._data))


class PdfExtractionProvider:
    """A text-based onboarding-form .pdf, through the same extractor as .docx."""

    source = "pdf"

    def __init__(self, data: bytes, filename: str) -> None:
        self._data, self._filename = data, filename

    def extract(self) -> OnboardingDocumentExtraction:
        return extract_onboarding_form(read_pdf(self._data, self._filename), document_name=self._filename,
                                       sha256=hashlib.sha256(self._data).hexdigest(), size_bytes=len(self._data))


class MockExtractionError(ValueError):
    pass


def _secret(raw: Any) -> SecretStr:
    return SecretStr(str(raw))


def _iso_date(raw: Any) -> date:
    return date.fromisoformat(str(raw))


def _probation(raw: Any) -> int:
    value = int(raw)
    if value not in (3, 6):
        raise ValueError("probation must be 3 or 6 months")
    return value


def _visa_years(raw: Any) -> int:
    value = int(raw)
    if value not in (1, 2):
        raise ValueError("visa duration must be 1 or 2 years")
    return value


def _one_of(*allowed: str) -> Callable[[Any], str]:
    def parse(raw: Any) -> str:
        value = str(raw).strip().upper()
        if value not in allowed:
            raise ValueError(f"expected one of {allowed}")
        return value
    return parse


def _document(raw: Any) -> DocumentStatus:
    return DocumentStatus(str(raw).strip().upper())


def _non_negative_int(raw: Any) -> int:
    value = int(raw)
    if value < 0:
        raise ValueError("must not be negative")
    return value


def _text(raw: Any) -> str:
    value = str(raw).strip()
    if not value:
        raise ValueError("empty")
    return value


# section -> field -> parser. Exactly the fields of OnboardingDocumentExtraction.
_PARSERS: dict[str, dict[str, Callable[[Any], Any]]] = {
    "employee": {"employee_name": _text, "reporting_to": _text, "designation": _text,
                 "joining_date": _iso_date, "probation_months": _probation, "final_salary": _secret},
    "offer": {"issuing_entity": _text, "other_promises": _text},
    "employment": {"visa_duration_years": _visa_years,
                   "visa_location_category": _one_of("INSIDE_COUNTRY", "OUTSIDE_COUNTRY"),
                   "visit_visa_entry_permit": _text, "direct_entry_permit": _text},
    "documents": {name: _document for name in ("passport", "passport_cover", "visa_copy", "cancellation_paper",
                                                "emirates_id", "signed_offer_letter", "photograph",
                                                "degree_certificate")},
    "personal": {"father_name": _secret, "mother_name": _secret,
                 "marital_status": _one_of("SINGLE", "MARRIED", "DIVORCED", "WIDOWED"), "mobile_number": _secret},
    "insurance": {"plan": _text, "employee_insurance": _text, "family_insurance": _text},
    "benefits": {"annual_employee_ticket": _text, "annual_family_ticket": _text,
                 "hotel_stay_days": _non_negative_int, "hotel_budget_and_location": _text},
}


def _field(raw: Any, parser: Callable[[Any], Any], source: str) -> ExtractedField:
    status: Optional[FieldStatus] = None
    note: Optional[str] = None
    if isinstance(raw, dict):
        status = FieldStatus(raw["status"]) if raw.get("status") else None
        note = raw.get("note")
        raw = raw.get("value")
    if raw is None:
        return ExtractedField(status=status or FieldStatus.MISSING, source=source, note=note)
    try:
        value = parser(raw)
    except (TypeError, ValueError) as exc:
        return ExtractedField(status=FieldStatus.INVALID, source=source, note=str(exc) or "unparseable")
    return ExtractedField(status=status or FieldStatus.PRESENT, value=value, source=source, note=note)


class MockExtractionProvider:
    """Loads a synthetic 'extracted' fixture into the real extraction model."""

    source = "mock"

    def __init__(self, path: Path, *, key: str = "extracted") -> None:
        self._path, self._key = Path(path), key

    def load(self) -> dict[str, Any]:
        raw = self._path.read_bytes()
        document = json.loads(raw)
        if document.get("synthetic") is not True:
            raise MockExtractionError("refusing a mock extraction fixture that is not marked synthetic")
        return {"document": document, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}

    def extract(self) -> OnboardingDocumentExtraction:
        loaded = self.load()
        extracted = loaded["document"][self._key]
        unknown = set(extracted) - set(_PARSERS) - {"roles"}
        if unknown:
            raise MockExtractionError(f"unknown sections in mock fixture: {sorted(unknown)}")
        out = OnboardingDocumentExtraction(metadata=ExtractionMetadata(
            source_document_name=f"mock:{self._path.name}", source_sha256=loaded["sha256"],
            source_size_bytes=loaded["size"], template_id=loaded["document"].get("template_id", TEMPLATE_ID),
            template_recognised=loaded["document"].get("template_id", TEMPLATE_ID) == TEMPLATE_ID))
        for section, parsers in _PARSERS.items():
            given = extracted.get(section) or {}
            stray = set(given) - set(parsers)
            if stray:
                raise MockExtractionError(f"unknown fields in mock section {section!r}: {sorted(stray)}")
            block = getattr(out, section)
            for name, parser in parsers.items():
                setattr(block, name, _field(given.get(name), parser,
                                            f"mock:{self._key}.{section}.{name}"))
        roles = extracted.get("roles") or {}
        out.roles = RolesAndResponsibilities(
            responsibilities=[str(r) for r in roles.get("responsibilities") or []],
            is_example=bool(roles.get("is_example", False)), source=f"mock:{self._key}.roles",
            summary=_field(roles.get("summary"), _text, f"mock:{self._key}.roles.summary"))
        fields = out.fields()
        md = out.metadata
        md.missing_fields = sorted(k for k, f in fields.items() if f.status == FieldStatus.MISSING)
        md.invalid_fields = sorted(k for k, f in fields.items() if f.status == FieldStatus.INVALID)
        md.uncertain_fields = sorted(k for k, f in fields.items() if f.status == FieldStatus.UNCERTAIN)
        md.completeness = round(sum(f.status == FieldStatus.PRESENT for f in fields.values()) / len(fields), 3)
        # The fixture is refused unless marked synthetic, so every value is.
        md.synthetic_document = True
        out.budget = _fixture_budget(out, loaded["document"].get("budget_inputs") or {}, self._key)
        return out


def _fixture_budget(out: OnboardingDocumentExtraction, inputs: dict[str, Any], key: str) -> OnboardingBudget:
    """The budget the document reader would compute, from the fixture's own
    row texts. `budget_inputs` (optional, top level of the fixture) can give
    the raw text of a budget row the model has no field for (the visa row's
    "Visa fee: AED ...") and the template's printed price lines; any row it
    names replaces the field-derived text. Nothing is defaulted."""
    def row(field: ExtractedField, name: str) -> RowText:
        if name in inputs:
            return RowText(text=str(inputs[name]) if inputs[name] is not None else None, source=f"mock:{name}")
        return RowText(text=str(field.value) if field.usable else None, source=field.source,
                       conflict=field.status == FieldStatus.UNCERTAIN)

    hotel_text = " ".join(str(f.value) for f in (out.benefits.hotel_budget_and_location,) if f.usable) or None
    rows = {
        "employment.visa": RowText(text=inputs.get("employment.visa"), source="mock:employment.visa"),
        "employment.visit_visa_entry_permit": row(out.employment.visit_visa_entry_permit,
                                                  "employment.visit_visa_entry_permit"),
        "insurance.plan": row(out.insurance.plan, "insurance.plan"),
        "insurance.employee_insurance": row(out.insurance.employee_insurance, "insurance.employee_insurance"),
        "insurance.family_insurance": row(out.insurance.family_insurance, "insurance.family_insurance"),
        "benefits.annual_employee_ticket": row(out.benefits.annual_employee_ticket, "benefits.annual_employee_ticket"),
        "benefits.annual_family_ticket": row(out.benefits.annual_family_ticket, "benefits.annual_family_ticket"),
        "benefits.hotel": (RowText(text=inputs["benefits.hotel"], source="mock:benefits.hotel")
                           if "benefits.hotel" in inputs else
                           RowText(text=hotel_text, source=f"mock:{key}.benefits.hotel_budget_and_location")),
        "offer.other_promises": row(out.offer.other_promises, "offer.other_promises"),
    }
    emp, nights = out.employment, out.benefits.hotel_stay_days
    return build_budget(
        rows, [(str(t), f"mock:reference_charges[{i}]") for i, t in enumerate(inputs.get("reference_charges") or [])],
        visa_duration_years=emp.visa_duration_years.value if emp.visa_duration_years.usable else None,
        visa_location_category=emp.visa_location_category.value if emp.visa_location_category.usable else None,
        hotel_nights=nights.value if nights.usable else None)
