"""Supabase stores for what UC-01 keeps from an onboarding document: the
details and the budget, and the source file itself.

    SupabaseFormDetailsRepository   uc01_onboarding_form_details (+ field status,
                                    checklist, budget, budget lines)  - PROPOSED tables
    SupabaseSourceDocumentStore     Supabase Storage (bucket from UC01_DOCUMENT_BUCKET)

The source file's metadata (document id, bucket, object path, SHA-256, size,
MIME type) is stored on the details row. It is NOT registered in the shared
candidate_documents table: that table holds the documents UC-03 validates,
its type CHECK has no onboarding-form value, and UC-01 does not branch on or
write document types (tests/testcase.py). Registering it there is a pending
decision (docs/uc01-budget-and-persistence.md).

Lives in documents/ because it maps what the onboarding FORM states (visa
terms included) to columns; the only network code stays in
adapters/supabase/client.py.

The five details tables do not exist in the live project until the DBA
applies docs/schema/proposed/20261105120000_uc01_form_details_and_budget.sql.
`ensure_ready()` checks for them BEFORE any case is opened, so a missing
table stops the flow without writing anything (no half-created employee).

Every write is an upsert on the table's natural key, so a retry repeats the
same row instead of adding one. A form's details are immutable: if a record
is already stored with DIFFERENT content, nothing is written and
FormDetailsConflict is raised (a change is a new form version).

Amounts are written as numeric(14,2) and read back as TEXT (`::text`), so no
value passes through a float.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import SecretStr

from ..adapters.supabase.client import (
    PostgrestClient,
    StorageError,
    SupabaseError,
    SupabaseStorage,
    decode_bytea,
    encode_bytea,
)
from ..adapters.supabase.storage_paths import intake_object_path, source_document_id
from ..crypto import FieldCipher
from .budget import (
    AmountProvenance,
    BudgetCategory,
    BudgetLine,
    BudgetStatus,
    ConversionApplied,
    Money,
    OnboardingBudget,
    ReferenceRate,
)
from .models import FieldStatus
from .persistence import (
    IncompleteFormDetails,
    ChecklistRow,
    DataOrigin,
    FieldStatusRow,
    FormDetailsConflict,
    FormDetailsRecord,
    SourceDocumentRef,
)

DETAILS = "uc01_onboarding_form_details"
FIELD_STATUS = "uc01_onboarding_form_field_status"
CHECKLIST = "uc01_onboarding_document_checklist"
BUDGET = "uc01_onboarding_budget"
BUDGET_LINES = "uc01_onboarding_budget_lines"
TABLES = (DETAILS, FIELD_STATUS, CHECKLIST, BUDGET, BUDGET_LINES)


class FormDetailsStoreUnavailable(SupabaseError):
    """The details tables are not in the database (migration not applied)."""


def _personal_aad(form_id: str) -> str:
    # Distinct from the salary's AAD (the bare form_id), so the two
    # ciphertexts of one form can never be swapped between columns.
    return f"{form_id}:personal_details"


def _dec(value: Any) -> Optional[Decimal]:
    return None if value is None else Decimal(str(value))


def _money_json(m: Optional[Money]) -> Optional[dict[str, str]]:
    return None if m is None else {"amount": str(m.amount), "currency": m.currency,
                                   "original_text": m.original_text or ""}


def _money_from(d: Optional[dict[str, Any]]) -> Optional[Money]:
    return None if not d else Money(amount=Decimal(d["amount"]), currency=d["currency"],
                                    original_text=d.get("original_text") or None)


class SupabaseFormDetailsRepository:
    _AMOUNT_COLS = ("visa_budget_amount", "insurance_budget_amount", "travel_budget_amount",
                    "accommodation_budget_amount", "other_expenses_amount", "total_onboarding_budget")

    def __init__(self, client: PostgrestClient, cipher: FieldCipher) -> None:
        self._c = client
        self._cipher = cipher
        self._ready = False

    # ------------------------------------------------------------ readiness

    def ensure_ready(self) -> None:
        if self._ready:
            return
        for table in TABLES:
            try:
                self._c.select(table, columns="form_id", limit=1)
            except SupabaseError as exc:
                raise FormDetailsStoreUnavailable(
                    f"{table} is not available ({exc.__class__.__name__}); apply the proposed migration "
                    "20261105120000_uc01_form_details_and_budget.sql first") from None
        self._ready = True

    # ------------------------------------------------------------ write

    def _details_row(self, r: FormDetailsRecord) -> dict[str, Any]:
        doc = r.source_document
        sealed = (encode_bytea(self._cipher.encrypt(r.personal_details.get_secret_value(),
                                                    aad=_personal_aad(r.form_id)))
                  if r.personal_details else None)
        return {
            "form_id": r.form_id, "client_id": r.client_id, "case_id": r.case_id, "template_id": r.template_id,
            "source_document_sha256": r.source_document_sha256,
            "source_document_id": doc.document_id if doc else None,
            "source_bucket": doc.bucket if doc else None, "source_object_path": doc.object_path if doc else None,
            "source_size_bytes": doc.size_bytes if doc else None, "source_mime_type": doc.mime_type if doc else None,
            "data_origin": r.data_origin.value, "issuing_entity": r.issuing_entity, "reporting_to": r.reporting_to,
            "probation_months": r.probation_months, "visa_duration_years": r.visa_duration_years,
            "visa_location_category": r.visa_location_category, "visit_visa_entry_permit": r.visit_visa_entry_permit,
            "direct_entry_permit": r.direct_entry_permit, "insurance_plan": r.insurance_plan,
            "employee_insurance": r.employee_insurance, "family_insurance": r.family_insurance,
            "annual_employee_ticket": r.annual_employee_ticket, "annual_family_ticket": r.annual_family_ticket,
            "hotel_stay_days": r.hotel_stay_days, "hotel_budget_and_location": r.hotel_budget_and_location,
            "other_promises": r.other_promises, "roles_summary": r.roles_summary, "marital_status": r.marital_status,
            "nationality": r.nationality, "employment_duration": r.employment_duration,
            "work_location_on_form": r.work_location_on_form, "personal_details_encrypted": sealed,
            "responsibilities": list(r.responsibilities), "responsibilities_is_example": r.responsibilities_is_example,
            "review_confirmed_by": r.review_confirmed_by,
            "review_confirmed_at": r.review_confirmed_at.isoformat() if r.review_confirmed_at else None,
            "completeness": str(Decimal(str(r.completeness)).quantize(Decimal("0.001"))),
        }

    @staticmethod
    def _budget_row(r: FormDetailsRecord) -> dict[str, Any]:
        b = r.budget

        def cat(c: BudgetCategory) -> Optional[str]:
            total = b.category_total(c)
            return str(total) if total is not None else None

        return {
            "form_id": r.form_id, "client_id": r.client_id, "case_id": r.case_id,
            "budget_currency": b.currency,
            "visa_budget_amount": cat(BudgetCategory.VISA),
            "insurance_budget_amount": cat(BudgetCategory.INSURANCE),
            "travel_budget_amount": cat(BudgetCategory.TRAVEL),
            "accommodation_budget_amount": cat(BudgetCategory.ACCOMMODATION),
            "other_expenses_amount": cat(BudgetCategory.OTHER),
            "total_onboarding_budget": str(b.budget_total) if b.budget_total is not None else None,
            "budget_status": b.status.value, "status_reasons": list(b.status_reasons),
            "category_totals": {c: {cur: str(v) for cur, v in t.items()} for c, t in b.category_totals.items()},
            "totals_by_currency": {k: str(v) for k, v in b.totals_by_currency.items()},
            "conversion": None if b.conversion is None else {
                "base_currency": b.conversion.base_currency,
                "rates": {k: str(v) for k, v in b.conversion.rates.items()}},
            "reference_rates": [{"key": x.key, "category": x.category.value, "label": x.label,
                                 "money": _money_json(x.money), "source": x.source, "note": x.note}
                                for x in b.reference_rates],
            "reference_differences": list(b.reference_differences),
            "calculation_version": b.calculation_version,
        }

    @staticmethod
    def _line_rows(r: FormDetailsRecord) -> list[dict[str, Any]]:
        rows = []
        for i, x in enumerate(r.budget.lines):
            rows.append({
                "form_id": r.form_id, "line_key": x.key, "line_no": i, "category": x.category.value,
                "label": x.label, "provenance": x.provenance.value, "counted": x.counted,
                "amount": str(x.money.amount) if x.money else None,
                "currency": x.money.currency if x.money else None,
                "original_amount_text": x.money.original_text if x.money else None,
                "source_ref": x.source, "note": x.note,
                "reference_key": x.reference.key if x.reference else None,
                "reference_amount": str(x.reference.money.amount) if x.reference else None,
                "reference_currency": x.reference.money.currency if x.reference else None,
                "details": dict(x.details),
            })
        return rows

    def save(self, record: FormDetailsRecord) -> None:
        """Insert-if-absent, in a fixed order, budget row LAST.

        B6: PostgREST gives one transaction per statement, not per record. So
        each table is ONE statement (all-or-nothing for that table), every
        write is INSERT ... ON CONFLICT DO NOTHING (a retry never updates a
        stored row - the immutability trigger would refuse that anyway), and
        the budget row is the completion marker: a record without it is
        INCOMPLETE and a retry fills in only what is missing. A stored row
        that differs is never overwritten; the caller's read-back reports it.
        """
        self.ensure_ready()
        try:
            existing = self.get(record.form_id)
        except IncompleteFormDetails:
            existing = None  # interrupted earlier: complete it below
        else:
            if existing is not None:
                if existing.comparable() != record.comparable():
                    raise FormDetailsConflict(f"form {record.form_id} already has different details stored")
                return  # identical retry: nothing to write
        fid = record.form_id
        self._c.insert(DETAILS, [self._details_row(record)], returning=False, on_conflict_ignore=True)
        if record.field_status:
            self._c.insert(FIELD_STATUS, [{"form_id": fid, "field_key": f.field_key, "status": f.status.value,
                                           "source_ref": f.source_ref, "note": f.note} for f in record.field_status],
                           returning=False, on_conflict_ignore=True)
        if record.checklist:
            self._c.insert(CHECKLIST, [{"form_id": fid, "item": c.item, "status": c.status,
                                        "source_ref": c.source_ref} for c in record.checklist],
                           returning=False, on_conflict_ignore=True)
        if record.budget.lines:
            self._c.insert(BUDGET_LINES, self._line_rows(record), returning=False, on_conflict_ignore=True)
        self._c.insert(BUDGET, [self._budget_row(record)], returning=False, on_conflict_ignore=True)

    # ------------------------------------------------------------ read

    def get(self, form_id: str) -> Optional[FormDetailsRecord]:
        flt = {"form_id": f"eq.{form_id}"}
        rows = self._c.select(DETAILS, columns="*,completeness::text", filters=flt, limit=1)
        if not rows:
            return None
        d = rows[0]
        budget_rows = self._c.select(BUDGET, columns="*," + ",".join(f"{c}::text" for c in self._AMOUNT_COLS),
                                     filters=flt, limit=1)
        lines = self._c.select(BUDGET_LINES, columns="*,amount::text,reference_amount::text", filters=flt,
                               order="line_no.asc")
        fields = self._c.select(FIELD_STATUS, columns="*", filters=flt, order="field_key.asc")
        checklist = self._c.select(CHECKLIST, columns="*", filters=flt, order="item.asc")
        blob = decode_bytea(d.get("personal_details_encrypted"))
        doc = None
        if d.get("source_document_id"):
            doc = SourceDocumentRef(document_id=d["source_document_id"], bucket=d["source_bucket"],
                                    object_path=d["source_object_path"], sha256=d["source_document_sha256"],
                                    size_bytes=int(d["source_size_bytes"]), mime_type=d["source_mime_type"])
        reviewed = d.get("review_confirmed_at")
        return FormDetailsRecord(
            form_id=d["form_id"], client_id=d["client_id"], case_id=d["case_id"], template_id=d["template_id"],
            source_document_sha256=d["source_document_sha256"], source_document=doc,
            data_origin=DataOrigin(d["data_origin"]),
            **{k: d.get(k) for k in (
                "issuing_entity", "reporting_to", "probation_months", "visa_duration_years",
                "visa_location_category", "visit_visa_entry_permit", "direct_entry_permit", "insurance_plan",
                "employee_insurance", "family_insurance", "annual_employee_ticket", "annual_family_ticket",
                "hotel_stay_days", "hotel_budget_and_location", "other_promises", "roles_summary",
                "marital_status", "nationality", "employment_duration", "work_location_on_form",
                "review_confirmed_by")},
            personal_details=SecretStr(self._cipher.decrypt(blob, aad=_personal_aad(d["form_id"]))) if blob else None,
            responsibilities=list(d.get("responsibilities") or []),
            responsibilities_is_example=bool(d.get("responsibilities_is_example")),
            review_confirmed_at=datetime.fromisoformat(reviewed) if reviewed else None,
            completeness=float(d["completeness"]),
            field_status=[FieldStatusRow(field_key=f["field_key"], status=FieldStatus(f["status"]),
                                         source_ref=f.get("source_ref"), note=f.get("note")) for f in fields],
            checklist=[ChecklistRow(item=c["item"], status=c["status"], source_ref=c.get("source_ref"))
                       for c in checklist],
            budget=self._budget_from(budget_rows[0] if budget_rows else None, lines),
        )

    @staticmethod
    def _budget_from(row: Optional[dict[str, Any]], lines: list[dict[str, Any]]) -> OnboardingBudget:
        if row is None:
            raise IncompleteFormDetails("the budget row (written last) is missing: an interrupted save")
        rates = [ReferenceRate(key=x["key"], category=BudgetCategory(x["category"]), label=x["label"],
                               money=_money_from(x["money"]), source=x["source"], note=x.get("note"))  # type: ignore[arg-type]
                 for x in (row.get("reference_rates") or [])]
        by_key = {r.key: r for r in rates}
        conversion = row.get("conversion")
        return OnboardingBudget(
            lines=[BudgetLine(
                key=x["line_key"], category=BudgetCategory(x["category"]), label=x["label"],
                provenance=AmountProvenance(x["provenance"]),
                money=(Money(amount=Decimal(x["amount"]), currency=x["currency"],
                             original_text=x.get("original_amount_text")) if x.get("amount") is not None else None),
                source=x.get("source_ref"), note=x.get("note"),
                reference=by_key.get(x["reference_key"]) if x.get("reference_key") else None,
                details=dict(x.get("details") or {})) for x in lines],
            reference_rates=rates,
            category_totals={c: {cur: Decimal(v) for cur, v in t.items()}
                             for c, t in (row.get("category_totals") or {}).items()},
            totals_by_currency={k: Decimal(v) for k, v in (row.get("totals_by_currency") or {}).items()},
            currency=row.get("budget_currency"),
            budget_total=_dec(row.get("total_onboarding_budget")),
            conversion=None if not conversion else ConversionApplied(
                base_currency=conversion["base_currency"],
                rates={k: Decimal(v) for k, v in conversion["rates"].items()}),
            status=BudgetStatus(row["budget_status"]), status_reasons=list(row.get("status_reasons") or []),
            reference_differences=list(row.get("reference_differences") or []),
            calculation_version=row["calculation_version"],
        )

    def find_by_document(self, client_id: str, sha256: str) -> list[str]:
        rows = self._c.select(DETAILS, columns="form_id", filters={
            "client_id": f"eq.{client_id}", "source_document_sha256": f"eq.{sha256}"})
        return [r["form_id"] for r in rows]


# ---------------------------------------------------------------- storage


class SupabaseSourceDocumentStore:
    """Stores a received onboarding document once, at a server-made path, and
    proves it by downloading it again and comparing the SHA-256."""

    def __init__(self, storage: SupabaseStorage, bucket: str) -> None:
        self._s = storage
        self._bucket = bucket

    def store(self, client_id: str, data: bytes, extension: str, mime_type: str) -> tuple[SourceDocumentRef, bool]:
        sha = hashlib.sha256(data).hexdigest()
        document_id = source_document_id(client_id, sha)
        path = intake_object_path(client_id, document_id, extension)
        already = self._s.exists(self._bucket, path)
        if not already:
            self._s.upload(self._bucket, path, data, mime_type)
        stored = self._s.download(self._bucket, path)
        if hashlib.sha256(stored).hexdigest() != sha:
            raise StorageError("stored object does not match the received document (SHA-256 differs)")
        return SourceDocumentRef(document_id=document_id, bucket=self._bucket, object_path=path, sha256=sha,
                                 size_bytes=len(data), mime_type=mime_type), already

    def read(self, ref: SourceDocumentRef) -> bytes:
        return self._s.download(ref.bucket, ref.object_path)


class InMemorySourceDocumentStore:
    """Test/dev double with the same idempotent, content-addressed behaviour."""

    def __init__(self, bucket: str = "in-memory") -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}

    def store(self, client_id: str, data: bytes, extension: str, mime_type: str) -> tuple[SourceDocumentRef, bool]:
        sha = hashlib.sha256(data).hexdigest()
        document_id = source_document_id(client_id, sha)
        path = intake_object_path(client_id, document_id, extension)
        already = path in self.objects
        self.objects.setdefault(path, bytes(data))
        return SourceDocumentRef(document_id=document_id, bucket=self.bucket, object_path=path, sha256=sha,
                                 size_bytes=len(data), mime_type=mime_type), already

    def read(self, ref: SourceDocumentRef) -> bytes:
        return self.objects[ref.object_path]
