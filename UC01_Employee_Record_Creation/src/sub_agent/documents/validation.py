"""Decide what an extraction is good for. Deterministic; no model.

Two kinds of finding, never mixed:

ERRORS -> VALIDATION_FAILED. The data cannot become an onboarding form at
all: the template was not recognised, a mandatory field is missing or
invalid, or the caller did not supply what the document never contains
(candidate email, work location). No case is attempted.

REVIEW REASONS -> NEEDS_REVIEW. A form could be built, but a person must
look first: an uncertain or conflicting value, required onboarding data or
documents still outstanding, responsibilities that are only the template's
example, a joining date already past, or the advisory model asking for a
look. Cleared only by an HR user confirming the review - never by a model.

Required documents do not block case creation: collecting them is the
later document-collection stage. They do require HR to acknowledge what is
outstanding before the case is opened from this document.

Budget: an amount that cannot be used (unsupported currency, two different
values, an amount without a currency) is a review reason - a person decides.
A MISSING or PARTIAL budget is reported and stored with its status, but it
does not block the employee record: the budget is planning information, and
"not stated yet" is a legitimate state of the form.

Caller vs document: the caller's values stay authoritative. When the
document states a supplementary value (email, department, grade) that
differs from the caller's, that is a conflict for HR review - never a
silent overwrite in either direction. When the caller did not supply the
candidate email but the document states a valid one, the document's value
is used (it is extracted, not derived).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..models import CasePlacement
from .budget import AmountProvenance
from .models import DocumentStatus, FieldStatus, OnboardingDocumentExtraction

#: Without these no onboarding form can exist (OnboardingForm requires them).
MANDATORY_FIELDS = (
    "employee.employee_name",
    "employee.designation",
    "employee.joining_date",
    "employee.final_salary",
)
#: Needed for onboarding; missing ones go to HR review, they do not block.
REVIEW_IF_MISSING = (
    "employee.reporting_to",
    "employee.probation_months",
    "personal.father_name",
    "personal.mother_name",
    "personal.marital_status",
    "personal.mobile_number",
)
#: Always required before day one.
REQUIRED_DOCUMENTS = ("passport", "passport_cover", "signed_offer_letter", "photograph", "degree_certificate")
#: Required in addition when the visa is processed inside the country.
INSIDE_COUNTRY_DOCUMENTS = ("visa_copy", "cancellation_paper", "emirates_id")


@dataclass
class ValidationOutcome:
    errors: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)
    outstanding_documents: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def required_documents(extraction: OnboardingDocumentExtraction) -> tuple[str, ...]:
    category = extraction.employment.visa_location_category
    inside = category.status == FieldStatus.PRESENT and category.value == "INSIDE_COUNTRY"
    return REQUIRED_DOCUMENTS + (INSIDE_COUNTRY_DOCUMENTS if inside else ())


def validate_extraction(
    extraction: OnboardingDocumentExtraction,
    *,
    candidate_email: Optional[str],
    work_location_type: Optional[str],
    today: date,
    placement: Optional[CasePlacement] = None,
) -> ValidationOutcome:
    out = ValidationOutcome()
    fields = extraction.fields()
    supplementary = extraction.supplementary_fields()
    document_email = supplementary["supplementary.employee_email"]

    if not extraction.metadata.template_recognised:
        out.errors.append("template_not_recognised")

    for key in MANDATORY_FIELDS:
        status = fields[key].status
        if status == FieldStatus.MISSING:
            out.errors.append(f"missing:{key}")
        elif status == FieldStatus.INVALID:
            out.errors.append(f"invalid:{key}")
    # Not in the document: the caller must supply them, never derived.
    if not (candidate_email or "").strip() and not document_email.usable:
        out.errors.append("missing:context.candidate_email")
    if not work_location_type:
        out.errors.append("missing:context.work_location_type")

    for key, f in sorted(fields.items()):
        if f.status == FieldStatus.UNCERTAIN:
            out.review_reasons.append(f"uncertain:{key}")
        elif f.status == FieldStatus.INVALID and key not in MANDATORY_FIELDS:
            out.review_reasons.append(f"invalid:{key}")
    for key, f in sorted(supplementary.items()):
        if f.status in (FieldStatus.UNCERTAIN, FieldStatus.INVALID):
            out.review_reasons.append(f"{f.status.value.lower()}:{key}")
    caller_email = (candidate_email or "").strip().lower()
    if document_email.usable and caller_email and str(document_email.value).lower() != caller_email:
        out.review_reasons.append("conflict:supplementary.employee_email")
    if placement is not None:
        for name in ("department", "grade"):
            stated = supplementary[f"supplementary.{name}"]
            given = getattr(placement, name)
            if stated.usable and given and _same(str(stated.value)) != _same(given):
                out.review_reasons.append(f"conflict:supplementary.{name}")

    if extraction.budget is not None:
        for line in extraction.budget.lines:
            if line.provenance in (AmountProvenance.INVALID, AmountProvenance.UNCERTAIN):
                out.review_reasons.append(f"budget:{line.provenance.value.lower()}:{line.key}")

    for key in REVIEW_IF_MISSING:
        if fields[key].status == FieldStatus.MISSING:
            out.review_reasons.append(f"missing:{key}")

    for name in required_documents(extraction):
        f = fields[f"documents.{name}"]
        if not (f.status == FieldStatus.PRESENT and f.value == DocumentStatus.RECEIVED):
            out.outstanding_documents.append(name)
    if out.outstanding_documents:
        out.review_reasons.append("documents_outstanding")

    roles = extraction.roles
    if not roles.responsibilities:
        out.review_reasons.append("responsibilities_missing")
    elif roles.is_example:
        out.review_reasons.append("responsibilities_are_template_example")

    doj = fields["employee.joining_date"]
    if doj.value is not None and doj.value < today:
        out.review_reasons.append("joining_date_in_past")
    return out


def _same(text: str) -> str:
    return " ".join(text.split()).casefold()
