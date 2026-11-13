"""What UC-01 stores from an onboarding document beyond the case and form.

The case (`onboarding_cases`), the form with the encrypted salary
(`onboarding_forms`) and the person (`person_records`) are written by the
unchanged `open_onboarding_case` flow. Everything else the document says -
reporting line, probation, visa terms, the documents checklist, personal
details, insurance, travel, hotel, other promises, responsibilities, the
per-field extraction status, and the onboarding BUDGET - had no live column
(decision B9). It is held here as one typed record, keyed by the form it
belongs to:

    FormDetailsRecord ──── uc01_onboarding_form_details      (one row per form)
      ├── field_status ─── uc01_onboarding_form_field_status (one row per field)
      ├── checklist ────── uc01_onboarding_document_checklist(one row per document)
      ├── budget ───────── uc01_onboarding_budget            (one row per form)
      └── budget.lines ─── uc01_onboarding_budget_lines      (one row per expense)

(proposed DDL: docs/schema/proposed/20261105120000_uc01_form_details_and_budget.sql)

Rules:

* Personal details (parents' names, mobile number) are ONE encrypted value,
  never plain columns. The salary is NOT here: it stays encrypted in
  onboarding_forms, copied exactly as written.
* A form version's details are immutable. Saving the same record again is
  a no-op (safe retry); saving DIFFERENT details for a form that already has
  them is refused (`FormDetailsConflict`) - a change is a new form version,
  never a silent overwrite, so a budget can never be counted twice or
  replaced unnoticed.
* Read-back verification (`verify_saved`) compares what was read from the
  store with what was built, field by field, before success is reported.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .budget import OnboardingBudget
from .models import FieldStatus, OnboardingDocumentExtraction


class DataOrigin(StrEnum):
    DOCUMENT = "DOCUMENT"    # read from a real onboarding form
    SYNTHETIC = "SYNTHETIC"  # the document labels itself as synthetic test data


class FieldStatusRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    field_key: str
    status: FieldStatus
    source_ref: Optional[str] = None
    note: Optional[str] = None


class ChecklistRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    item: str
    status: str  # RECEIVED | PENDING | UNCERTAIN | MISSING | INVALID (as stated on the form)
    source_ref: Optional[str] = None


class SourceDocumentRef(BaseModel):
    """Where the source file is stored. Never the uploaded file name in the path."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    bucket: str
    object_path: str
    sha256: str
    size_bytes: int
    mime_type: str


class FormDetailsRecord(BaseModel):
    """Everything from the document that the case and form tables do not hold."""

    form_id: str
    client_id: str
    case_id: str
    template_id: str
    source_document_sha256: str
    source_document: Optional[SourceDocumentRef] = None
    data_origin: DataOrigin
    issuing_entity: Optional[str] = None
    reporting_to: Optional[str] = None
    probation_months: Optional[int] = None
    visa_duration_years: Optional[int] = None
    visa_location_category: Optional[str] = None
    visit_visa_entry_permit: Optional[str] = None
    direct_entry_permit: Optional[str] = None
    insurance_plan: Optional[str] = None
    employee_insurance: Optional[str] = None
    family_insurance: Optional[str] = None
    annual_employee_ticket: Optional[str] = None
    annual_family_ticket: Optional[str] = None
    hotel_stay_days: Optional[int] = None
    hotel_budget_and_location: Optional[str] = None
    other_promises: Optional[str] = None
    roles_summary: Optional[str] = None
    marital_status: Optional[str] = None
    # Supplementary rows (not in the template) as stated on the form.
    nationality: Optional[str] = None
    employment_duration: Optional[str] = None
    work_location_on_form: Optional[str] = None
    # {"father_name": ..., "mother_name": ..., "mobile_number": ...} as JSON.
    # Encrypted at rest; SecretStr keeps it out of logs and dumps.
    personal_details: Optional[SecretStr] = None
    responsibilities: list[str] = Field(default_factory=list)
    responsibilities_is_example: bool = False
    review_confirmed_by: Optional[str] = None
    review_confirmed_at: Optional[datetime] = None
    completeness: float = 0.0
    field_status: list[FieldStatusRow] = Field(default_factory=list)
    checklist: list[ChecklistRow] = Field(default_factory=list)
    budget: OnboardingBudget

    def comparable(self) -> dict:
        """A canonical dict for equality checks (the secret compared by value)."""
        data = self.model_dump(mode="json", exclude={"personal_details"})
        data["personal_details"] = self.personal_details.get_secret_value() if self.personal_details else None
        data["field_status"] = sorted(data["field_status"], key=lambda r: r["field_key"])
        data["checklist"] = sorted(data["checklist"], key=lambda r: r["item"])
        return data


class FormDetailsConflict(RuntimeError):
    """Different details for a form version that already has details stored."""


class IncompleteFormDetails(RuntimeError):
    """A details record whose save was interrupted (its completion marker,
    the budget row, is missing). A retry completes it; nothing is replaced."""


class FormDetailsRepository(Protocol):
    def ensure_ready(self) -> None:
        """Raise when the store cannot accept a record (checked BEFORE a case
        is opened, so a missing store never leaves a half-written employee)."""

    def save(self, record: FormDetailsRecord) -> None: ...

    def get(self, form_id: str) -> Optional[FormDetailsRecord]: ...

    def find_by_document(self, client_id: str, sha256: str) -> list[str]:
        """form_ids already holding details read from this exact file."""


class InMemoryFormDetailsRepository:
    """Test/dev double. Same immutability rule as the Supabase adapter."""

    def __init__(self) -> None:
        self._records: dict[str, FormDetailsRecord] = {}

    def ensure_ready(self) -> None:
        return None

    def save(self, record: FormDetailsRecord) -> None:
        existing = self._records.get(record.form_id)
        if existing is not None:
            if existing.comparable() != record.comparable():
                raise FormDetailsConflict(f"form {record.form_id} already has different details")
            return
        self._records[record.form_id] = record.model_copy(deep=True)

    def get(self, form_id: str) -> Optional[FormDetailsRecord]:
        found = self._records.get(form_id)
        return found.model_copy(deep=True) if found else None

    def find_by_document(self, client_id: str, sha256: str) -> list[str]:
        return [r.form_id for r in self._records.values()
                if r.client_id == client_id and r.source_document_sha256 == sha256]


# ------------------------------------------------------------------ building

_PERSONAL_SECRET = ("father_name", "mother_name", "mobile_number")


def _value(field) -> Optional[object]:
    return field.value if field.status == FieldStatus.PRESENT else None


def _plain(value: object) -> Optional[str]:
    if value is None:
        return None
    return value.get_secret_value() if isinstance(value, SecretStr) else str(value)


def build_form_details(
    extraction: OnboardingDocumentExtraction,
    *,
    form_id: str,
    case_id: str,
    client_id: str,
    review_confirmed_by: Optional[str],
    review_confirmed_at: Optional[datetime],
    source_document: Optional[SourceDocumentRef],
) -> FormDetailsRecord:
    """The record for one extraction. Only PRESENT values are stored as
    values; every field's status (missing, invalid, uncertain) is stored in
    field_status so "not provided" is never confused with "empty"."""
    if extraction.budget is None:
        raise ValueError("extraction has no budget; build it with documents.budget first")
    e, emp, ins, ben, off, sup = (extraction.employee, extraction.employment, extraction.insurance,
                                 extraction.benefits, extraction.offer, extraction.supplementary)
    personal = {name: _plain(_value(getattr(extraction.personal, name))) for name in _PERSONAL_SECRET}
    personal_json = json.dumps({k: v for k, v in personal.items() if v is not None}, sort_keys=True)
    fields = {**extraction.fields(), **extraction.supplementary_fields()}
    return FormDetailsRecord(
        form_id=form_id, client_id=client_id, case_id=case_id,
        template_id=extraction.metadata.template_id, source_document_sha256=extraction.metadata.source_sha256,
        source_document=source_document,
        data_origin=DataOrigin.SYNTHETIC if extraction.metadata.synthetic_document else DataOrigin.DOCUMENT,
        issuing_entity=_plain(_value(off.issuing_entity)),
        reporting_to=_plain(_value(e.reporting_to)), probation_months=_value(e.probation_months),  # type: ignore[arg-type]
        visa_duration_years=_value(emp.visa_duration_years),  # type: ignore[arg-type]
        visa_location_category=_plain(_value(emp.visa_location_category)),
        visit_visa_entry_permit=_plain(_value(emp.visit_visa_entry_permit)),
        direct_entry_permit=_plain(_value(emp.direct_entry_permit)),
        insurance_plan=_plain(_value(ins.plan)), employee_insurance=_plain(_value(ins.employee_insurance)),
        family_insurance=_plain(_value(ins.family_insurance)),
        annual_employee_ticket=_plain(_value(ben.annual_employee_ticket)),
        annual_family_ticket=_plain(_value(ben.annual_family_ticket)),
        hotel_stay_days=_value(ben.hotel_stay_days),  # type: ignore[arg-type]
        hotel_budget_and_location=_plain(_value(ben.hotel_budget_and_location)),
        other_promises=_plain(_value(off.other_promises)),
        roles_summary=_plain(_value(extraction.roles.summary)),
        marital_status=_plain(_value(extraction.personal.marital_status)),
        nationality=_plain(_value(sup.nationality)), employment_duration=_plain(_value(sup.employment_duration)),
        work_location_on_form=_plain(_value(sup.work_location)),
        personal_details=SecretStr(personal_json) if personal_json != "{}" else None,
        responsibilities=list(extraction.roles.responsibilities),
        responsibilities_is_example=extraction.roles.is_example,
        review_confirmed_by=review_confirmed_by,
        review_confirmed_at=review_confirmed_at if review_confirmed_by else None,
        completeness=extraction.metadata.completeness,
        field_status=[FieldStatusRow(field_key=k, status=f.status, source_ref=f.source, note=f.note)
                      for k, f in sorted(fields.items())],
        checklist=[ChecklistRow(item=name, status=(f.value.value if f.status == FieldStatus.PRESENT and f.value
                                                   else f.status.value), source_ref=f.source)
                   for name, f in ((n, getattr(extraction.documents, n))
                                   for n in type(extraction.documents).model_fields)],
        budget=extraction.budget,
    )


def verify_saved(built: FormDetailsRecord, read_back: Optional[FormDetailsRecord]) -> list[str]:
    """Names of the top-level fields that differ after read-back ([] = verified).
    Values are never returned, only field names."""
    if read_back is None:
        return ["<record not found>"]
    a, b = built.comparable(), read_back.comparable()
    diffs = sorted(k for k in a if a.get(k) != b.get(k))
    if "budget" in diffs:
        ba, bb = a["budget"], b["budget"]
        diffs.remove("budget")
        diffs += sorted(f"budget.{k}" for k in ba if ba.get(k) != bb.get(k))
    return diffs
