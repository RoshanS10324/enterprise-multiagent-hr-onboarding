"""Structured result of reading one onboarding-form document.

Every extracted value is wrapped in an `ExtractedField`, which records what
was found, WHERE it was found, and whether it is usable:

    PRESENT    a value was read and parsed
    MISSING    the field exists in the form but nothing was filled in
    INVALID    something was filled in but it cannot be parsed
    UNCERTAIN  a value was read but a person must confirm it (two cells
               disagree, an ambiguous date, an example section, ...)

Missing and invalid are deliberately different: "not provided yet" asks the
sender for data; "provided but unreadable" asks someone to correct it.
Nothing here is ever inferred or defaulted -- an absent value stays None.

Sensitive values (final salary, mobile number, parents' names) are held as
`SecretStr`, so a log line, a repr or an API response cannot print them by
accident. Required documents are recorded as a received/pending STATUS only:
UC-01 never reads, stores or extracts a passport or Emirates ID number.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ..models.common import utc_now
from .budget import OnboardingBudget

T = TypeVar("T")

#: The only template this extractor understands (the 2026 onboarding form).
TEMPLATE_ID = "onboarding-form-2026"


class FieldStatus(StrEnum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"
    INVALID = "INVALID"
    UNCERTAIN = "UNCERTAIN"


class ExtractedField(BaseModel, Generic[T]):
    model_config = ConfigDict(frozen=True)

    status: FieldStatus = FieldStatus.MISSING
    value: Optional[T] = None
    # Where it came from, e.g. "table[0].row[6]" or "paragraph[3]".
    source: Optional[str] = None
    # Why it is INVALID/UNCERTAIN. Never contains the value itself.
    note: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.status == FieldStatus.PRESENT


class DocumentStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PENDING = "PENDING"


class Employee(BaseModel):
    employee_name: ExtractedField[str] = ExtractedField()
    reporting_to: ExtractedField[str] = ExtractedField()
    designation: ExtractedField[str] = ExtractedField()
    joining_date: ExtractedField[date] = ExtractedField()
    probation_months: ExtractedField[int] = ExtractedField()
    # The figure HR wrote in "Final Salary". Never a visa or insurance charge.
    final_salary: ExtractedField[SecretStr] = ExtractedField()


class Offer(BaseModel):
    # Header cell of the offer-letter table, e.g. the issuing entity.
    issuing_entity: ExtractedField[str] = ExtractedField()
    other_promises: ExtractedField[str] = ExtractedField()


class EmploymentDetails(BaseModel):
    visa_duration_years: ExtractedField[int] = ExtractedField()
    visa_location_category: ExtractedField[str] = ExtractedField()  # INSIDE_COUNTRY | OUTSIDE_COUNTRY
    visit_visa_entry_permit: ExtractedField[str] = ExtractedField()
    direct_entry_permit: ExtractedField[str] = ExtractedField()


class RequiredDocuments(BaseModel):
    """Received/pending per document. No document content or number."""

    passport: ExtractedField[DocumentStatus] = ExtractedField()
    passport_cover: ExtractedField[DocumentStatus] = ExtractedField()
    visa_copy: ExtractedField[DocumentStatus] = ExtractedField()
    cancellation_paper: ExtractedField[DocumentStatus] = ExtractedField()
    emirates_id: ExtractedField[DocumentStatus] = ExtractedField()
    signed_offer_letter: ExtractedField[DocumentStatus] = ExtractedField()
    photograph: ExtractedField[DocumentStatus] = ExtractedField()
    degree_certificate: ExtractedField[DocumentStatus] = ExtractedField()


class PersonalDetails(BaseModel):
    father_name: ExtractedField[SecretStr] = ExtractedField()
    mother_name: ExtractedField[SecretStr] = ExtractedField()
    marital_status: ExtractedField[str] = ExtractedField()
    mobile_number: ExtractedField[SecretStr] = ExtractedField()


class Insurance(BaseModel):
    plan: ExtractedField[str] = ExtractedField()  # BASIC | WN | RN as selected by HR
    employee_insurance: ExtractedField[str] = ExtractedField()
    family_insurance: ExtractedField[str] = ExtractedField()


class Benefits(BaseModel):
    annual_employee_ticket: ExtractedField[str] = ExtractedField()
    annual_family_ticket: ExtractedField[str] = ExtractedField()
    hotel_stay_days: ExtractedField[int] = ExtractedField()
    hotel_budget_and_location: ExtractedField[str] = ExtractedField()


class RolesAndResponsibilities(BaseModel):
    responsibilities: list[str] = Field(default_factory=list)
    # True when the section is marked as an example ("eg given below"):
    # the text is a template sample, not this employee's confirmed duties.
    is_example: bool = False
    source: Optional[str] = None
    # The short "R&R" cell in the offer-letter table, if filled in.
    summary: ExtractedField[str] = ExtractedField()


class SupplementaryDetails(BaseModel):
    """Employee details the 2026 template has NO row for. Read only when a
    document adds them (for example a separate "employee details" table);
    otherwise they stay MISSING and are not counted against the template's
    completeness. The caller's values stay authoritative: a document value
    that differs from the caller's is a conflict for HR review, never a
    silent overwrite."""

    employee_email: ExtractedField[str] = ExtractedField()
    department: ExtractedField[str] = ExtractedField()
    nationality: ExtractedField[str] = ExtractedField()
    work_location: ExtractedField[str] = ExtractedField()
    grade: ExtractedField[str] = ExtractedField()
    employment_duration: ExtractedField[str] = ExtractedField()


class ReferenceCharge(BaseModel):
    """A price line printed by the template itself (visa, ticket, insurance).

    Company reference information. It is NOT employee data and is never
    used as a salary or written to the employee record.
    """

    section: str
    text: str
    source: str


class ExtractionMetadata(BaseModel):
    source_document_name: str
    source_sha256: str
    source_size_bytes: int
    template_id: str = TEMPLATE_ID
    template_recognised: bool = False
    extracted_at: datetime = Field(default_factory=utc_now)
    missing_fields: list[str] = Field(default_factory=list)
    invalid_fields: list[str] = Field(default_factory=list)
    uncertain_fields: list[str] = Field(default_factory=list)
    unrecognised_rows: list[str] = Field(default_factory=list)
    # Share of expected template fields that were read as PRESENT.
    completeness: float = Field(default=0.0, ge=0.0, le=1.0)
    # The document labels itself as synthetic test data ("SYNTHETIC TEST ...").
    # Every value in it is then synthetic, and is persisted as such.
    synthetic_document: bool = False


class OnboardingDocumentExtraction(BaseModel):
    employee: Employee = Field(default_factory=Employee)
    offer: Offer = Field(default_factory=Offer)
    employment: EmploymentDetails = Field(default_factory=EmploymentDetails)
    documents: RequiredDocuments = Field(default_factory=RequiredDocuments)
    personal: PersonalDetails = Field(default_factory=PersonalDetails)
    insurance: Insurance = Field(default_factory=Insurance)
    benefits: Benefits = Field(default_factory=Benefits)
    roles: RolesAndResponsibilities = Field(default_factory=RolesAndResponsibilities)
    reference_charges: list[ReferenceCharge] = Field(default_factory=list)
    supplementary: SupplementaryDetails = Field(default_factory=SupplementaryDetails)
    # Selected expenses vs the rate card, and the calculated totals
    # (documents/budget.py). None only for an extraction built without it.
    budget: Optional[OnboardingBudget] = None
    metadata: ExtractionMetadata

    def fields(self) -> dict[str, ExtractedField]:
        """Every ExtractedField, keyed "section.field"."""
        out: dict[str, ExtractedField] = {}
        for section in ("employee", "offer", "employment", "documents", "personal", "insurance", "benefits"):
            block = getattr(self, section)
            for name in type(block).model_fields:
                out[f"{section}.{name}"] = getattr(block, name)
        out["roles.summary"] = self.roles.summary
        return out

    def supplementary_fields(self) -> dict[str, ExtractedField]:
        """The optional, non-template employee rows, keyed "supplementary.field"."""
        return {f"supplementary.{n}": getattr(self.supplementary, n) for n in type(self.supplementary).model_fields}
