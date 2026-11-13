"""Onboarding-document intake: read -> extract -> validate -> existing UC-01 flow.

    RECEIVED ─ read_document (.docx/.pdf) ─ REJECTED        (unsupported / unsafe / corrupt)
      │
    EXTRACTED ─ validate_extraction ───── VALIDATION_FAILED (mandatory data missing/invalid)
      │                                └─ NEEDS_REVIEW      (until an HR user confirms)
      │
    open_onboarding_case (unchanged) ──── CREATED | LINKED_EXISTING | HELD | PAUSED
                                       └─ PERSISTENCE_FAILED (storage refused/failed)
      │ (CREATED only)
    form details + budget saved ─ read back and compared ─ case, form and
    person read back ─ EMPLOYEE_RECORD_VERIFIED

This module adds NO new business rule for creating a case. Offer
acceptance, form approval, hiring freeze, duplicates, rehire/transfer and
placement are all decided by `open_onboarding_case`, exactly as for every
other entry point. In particular:

* an extracted form is never "approved" because it was extracted: its
  status comes only from the caller's attestation (`form_status`,
  `approved_by`, `approved_at`), the same contract as
  POST /internal/onboarding-requests;
* placement is passed through as given; without it the existing flow
  returns HELD `location_name_required`.

When a source-document store is configured, the received file is stored
ONCE at a server-made, content-addressed path (`<client>/uc01-intake/<id>`),
downloaded again and its SHA-256 compared, before extraction. Without a
store, only its SHA-256 and size are recorded (`source_document_stored`
False). A document already turned into a case for the same client is a
DUPLICATE: the existing case is returned and nothing new is written.

After a case is CREATED, the details the case and form tables cannot hold
(documents/persistence.py) and the onboarding budget (documents/budget.py)
are saved, read back and compared field by field; the case, the form (with
its decrypted salary) and the person record are read back too. Only then is
`persistence_verified` True. Any failure is PERSISTENCE_FAILED - success is
never reported for data that was not read back. The details store is checked
BEFORE the case is opened, so a missing store writes nothing.

Audit entries carry field names, counts, codes, the budget status and its
totals - never personal values, the salary or the file name.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Callable, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from ..adapters.supabase.client import SupabaseError
from ..adapters.supabase.unsupported import SchemaGapError
from ..audit_scope import audit_client_scope
from ..llm import LlmClient, LlmError, LlmRequest, audit_action_for
from ..models import (
    ActorType,
    AuditEntry,
    CasePlacement,
    CaseStatus,
    FormStatus,
    OnboardingForm,
    VariantFlags,
    WorkLocationType,
)
from ..models.common import new_id, utc_now
from ..onboarding_entry import OnboardingDeps, OpenCaseOutcome, OpenCaseRequest, open_onboarding_case
from .docx_reader import DocumentReadError
from .extraction import extract_onboarding_form
from .pdf_reader import document_source, read_document
from .budget import OnboardingBudget, budget_summary
from .models import FieldStatus, OnboardingDocumentExtraction
from .persistence import (
    FormDetailsConflict,
    FormDetailsRepository,
    IncompleteFormDetails,
    SourceDocumentRef,
    build_form_details,
    verify_saved,
)
from .validation import ValidationOutcome, validate_extraction

#: Model output label that does NOT ask for a review.
_RESPONSIBILITIES_OK = "MATCHES_DESIGNATION"
#: Cap on how much responsibilities text is sent to the advisory model.
MAX_RESPONSIBILITIES_CHARS = 4000
_ACTOR = "uc01-document-intake"


class SourceDocumentStore(Protocol):
    def store(self, client_id: str, data: bytes, extension: str, mime_type: str) -> tuple[SourceDocumentRef, bool]:
        """(reference, already_stored). Idempotent: same client + same bytes = same object."""

    def read(self, ref: SourceDocumentRef) -> bytes: ...


_UC01_OPEN_STATUSES = frozenset(s.value for s in CaseStatus) - {CaseStatus.WITHDRAWN.value, CaseStatus.CLOSED.value}
_MIME = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "pdf": "application/pdf"}


class DocumentIntakeStatus(StrEnum):
    REJECTED = "REJECTED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CREATED = "CREATED"
    LINKED_EXISTING = "LINKED_EXISTING"
    HELD = "HELD"
    PAUSED = "PAUSED"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"


_CASE_STATUS = {
    OpenCaseOutcome.CASE_OPENED: DocumentIntakeStatus.CREATED,
    OpenCaseOutcome.LINKED_EXISTING: DocumentIntakeStatus.LINKED_EXISTING,
    OpenCaseOutcome.HELD: DocumentIntakeStatus.HELD,
    OpenCaseOutcome.PAUSED: DocumentIntakeStatus.PAUSED,
}


class DocumentIntakeContext(BaseModel):
    """What the document cannot say, supplied by the caller. Never derived."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(default_factory=new_id)
    client_id: str
    hr_officer_id: str
    candidate_email: Optional[str] = None
    offer_accepted: bool = False
    work_location_type: Optional[WorkLocationType] = None
    client_site_id: Optional[str] = None
    placement: Optional[CasePlacement] = None
    declared_variants: Optional[VariantFlags] = None
    hiring_freeze: bool = False
    id_number: Optional[str] = None
    # Attestation of the INTERNAL form approval, exactly as on the API.
    form_status: FormStatus = FormStatus.DRAFT
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    # The HR user who reviewed the extraction's review reasons. Only a
    # person clears NEEDS_REVIEW; nothing else does.
    review_confirmed_by: Optional[str] = None


class DocumentIntakeResult(BaseModel):
    status: DocumentIntakeStatus
    document_sha256: Optional[str] = None
    rejection_code: Optional[str] = None
    errors: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    outstanding_documents: list[str] = Field(default_factory=list)
    extraction: Optional[OnboardingDocumentExtraction] = None
    case_id: Optional[str] = None
    employee_no: Optional[str] = None
    existing_case_id: Optional[str] = None
    approvals_created: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    # True only when the file was stored AND read back with the same SHA-256.
    source_document_stored: bool = False
    source_document: Optional[SourceDocumentRef] = None
    # The same client already had this exact file (storage or a case).
    duplicate_document: bool = False
    form_id: Optional[str] = None
    # True only after every saved record was read back and matched.
    persistence_verified: bool = False
    # Read-back result per store: True = read back and identical.
    readback: dict[str, bool] = Field(default_factory=dict)


class DocumentIntakeService:
    def __init__(self, deps: OnboardingDeps, *, llm: Optional[LlmClient] = None,
                 today: Callable[[], date] = date.today,
                 details_repo: Optional[FormDetailsRepository] = None,
                 source_store: Optional[SourceDocumentStore] = None,
                 clock: Callable[[], datetime] = utc_now) -> None:
        self._deps = deps
        self._llm = llm
        self._today = today
        self._details = details_repo
        self._source_store = source_store
        self._clock = clock

    # ------------------------------------------------------------------ public

    def extract(self, data: bytes, filename: str) -> OnboardingDocumentExtraction:
        """Read and extract only. Raises DocumentReadError for a bad file."""
        content = read_document(data, filename)
        return extract_onboarding_form(content, document_name=filename,
                                       sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))

    def process(self, data: bytes, filename: str, context: DocumentIntakeContext) -> DocumentIntakeResult:
        with audit_client_scope(context.client_id):
            return self._process(data, filename, context)

    def _process(self, data: bytes, filename: str, context: DocumentIntakeContext) -> DocumentIntakeResult:
        digest = hashlib.sha256(data).hexdigest()
        self._audit(context, "DOCUMENT_RECEIVED", {"sha256": digest, "size_bytes": len(data)})
        try:
            extraction = self.extract(data, filename)
        except DocumentReadError as exc:
            self._audit(context, "DOCUMENT_REJECTED", {"sha256": digest, "code": exc.code})
            return DocumentIntakeResult(status=DocumentIntakeStatus.REJECTED, document_sha256=digest,
                                        rejection_code=exc.code)
        ref, already = None, False
        if self._source_store is not None:
            # Stored only once it parsed: an unreadable or unsafe file is never kept.
            extension = document_source(filename)
            try:
                ref, already = self._source_store.store(context.client_id, data, extension, _MIME[extension])
            except Exception as exc:  # noqa: BLE001 - any storage failure; never echo it
                self._audit_safely(context, "DOCUMENT_STORAGE_FAILED", {"sha256": digest,
                                                                        "error": type(exc).__name__})
                return DocumentIntakeResult(status=DocumentIntakeStatus.PERSISTENCE_FAILED, document_sha256=digest,
                                            extraction=extraction,
                                            errors=[f"persistence:storage:{type(exc).__name__}"])
            self._audit(context, "DOCUMENT_STORED", {
                "sha256": digest, "document_id": ref.document_id, "bucket": ref.bucket,
                "object_path": ref.object_path, "size_bytes": ref.size_bytes, "already_stored": already})
        return self._process_extraction(extraction, context, source=document_source(filename),
                                        source_ref=ref, already_stored=already)

    def process_extraction(self, extraction: OnboardingDocumentExtraction, context: DocumentIntakeContext,
                           *, source: str) -> DocumentIntakeResult:
        """Everything after extraction: validation, HR review, then the
        unchanged open_onboarding_case flow. The single path for every
        extraction provider (the .docx reader, or MockExtractionProvider),
        so no provider can skip a check."""
        with audit_client_scope(context.client_id):
            return self._process_extraction(extraction, context, source=source)

    def _process_extraction(self, extraction: OnboardingDocumentExtraction, context: DocumentIntakeContext,
                            *, source: str, source_ref: Optional[SourceDocumentRef] = None,
                            already_stored: bool = False) -> DocumentIntakeResult:
        md = extraction.metadata
        digest = md.source_sha256
        self._audit(context, "DOCUMENT_EXTRACTED", {
            "source": source,
            "sha256": digest, "template_recognised": md.template_recognised, "completeness": md.completeness,
            "missing_fields": md.missing_fields, "invalid_fields": md.invalid_fields,
            "uncertain_fields": md.uncertain_fields, "responsibilities": len(extraction.roles.responsibilities),
            "synthetic_document": md.synthetic_document,
            "supplementary_present": sorted(k for k, f in extraction.supplementary_fields().items() if f.usable),
        })
        if extraction.budget is not None:
            self._audit(context, "BUDGET_CALCULATED", self._budget_audit(digest, extraction.budget))

        outcome = validate_extraction(
            extraction, candidate_email=context.candidate_email,
            work_location_type=context.work_location_type.value if context.work_location_type else None,
            today=self._today(), placement=context.placement)
        self._second_opinion(extraction, outcome, context)

        def finish(status: DocumentIntakeStatus, **update: Any) -> DocumentIntakeResult:
            return DocumentIntakeResult(
                status=status, document_sha256=digest, extraction=extraction, errors=list(outcome.errors),
                review_reasons=list(outcome.review_reasons),
                outstanding_documents=list(outcome.outstanding_documents),
                source_document_stored=source_ref is not None, source_document=source_ref,
                duplicate_document=already_stored).model_copy(update=update)

        if not outcome.ok:
            self._audit(context, "DOCUMENT_VALIDATION_FAILED", {"sha256": digest, "errors": outcome.errors})
            return finish(DocumentIntakeStatus.VALIDATION_FAILED)
        if outcome.review_reasons:
            if not context.review_confirmed_by:
                self._audit(context, "DOCUMENT_NEEDS_REVIEW",
                            {"sha256": digest, "review_reasons": outcome.review_reasons})
                return finish(DocumentIntakeStatus.NEEDS_REVIEW)
            self._audit(context, "DOCUMENT_REVIEW_CONFIRMED", {
                "sha256": digest, "review_reasons": outcome.review_reasons,
                "confirmed_by": context.review_confirmed_by}, actor=context.review_confirmed_by,
                actor_type=ActorType.HUMAN)

        try:
            form = self._form(extraction, context)
        except ValidationError as exc:
            errors = sorted({"form:" + ".".join(str(p) for p in e["loc"]) if e["loc"] else "form" for e in exc.errors()})
            self._audit(context, "DOCUMENT_VALIDATION_FAILED", {"sha256": digest, "errors": errors})
            return finish(DocumentIntakeStatus.VALIDATION_FAILED, errors=outcome.errors + errors)
        placement, from_document = self._placement(extraction, context)

        if self._details is not None:
            # Checked BEFORE the case: a missing details store writes nothing.
            try:
                self._details.ensure_ready()
                earlier = self._details.find_by_document(context.client_id, digest)
                try:
                    existing = self._details.get(earlier[0]) if earlier else None
                except IncompleteFormDetails:
                    existing = None  # an interrupted save: the resume path below completes it
            except Exception as exc:  # noqa: BLE001 - store unavailable; never echo it
                self._audit_safely(context, "DOCUMENT_PERSISTENCE_FAILED",
                                   {"sha256": digest, "error": type(exc).__name__, "stage": "form_details_ready"})
                return finish(DocumentIntakeStatus.PERSISTENCE_FAILED,
                              errors=[f"persistence:form_details_unavailable:{type(exc).__name__}"])
            if existing is not None:
                # The same file already became a case for this client: link,
                # never create a second employee or a second budget.
                self._audit(context, "DOCUMENT_DUPLICATE", {"sha256": digest, "existing_case_id": existing.case_id,
                                                            "existing_form_id": existing.form_id})
                return finish(DocumentIntakeStatus.LINKED_EXISTING, existing_case_id=existing.case_id,
                              form_id=existing.form_id, duplicate_document=True, reasons=["duplicate_document"])
            # B6 (no multi-table transaction over PostgREST): an earlier run may
            # have stopped after the case insert. Such a case has no person
            # record, so the duplicate check cannot see it - opening another
            # case would create a second employee. Stop and name it instead.
            try:
                orphan = self._incomplete_case(form)
            except Exception as exc:  # noqa: BLE001 - store unavailable; never echo it
                self._audit_safely(context, "DOCUMENT_PERSISTENCE_FAILED",
                                   {"sha256": digest, "error": type(exc).__name__, "stage": "incomplete_case_check"})
                return finish(DocumentIntakeStatus.PERSISTENCE_FAILED,
                              errors=[f"persistence:incomplete_case_check:{type(exc).__name__}"])
            if orphan is not None:
                self._audit(context, "INCOMPLETE_CASE_DETECTED", {"case_id": orphan, "sha256": digest},
                            case_id=orphan)
                return finish(DocumentIntakeStatus.PERSISTENCE_FAILED, existing_case_id=orphan,
                              errors=[f"persistence:incomplete_case_exists:{orphan}"],
                              reasons=["incomplete_case_needs_hr_resolution"])

        try:
            result = open_onboarding_case(OpenCaseRequest(
                request_id=context.request_id, client_id=context.client_id, hr_officer_id=context.hr_officer_id,
                offer_accepted=context.offer_accepted, form=form, hiring_freeze=context.hiring_freeze,
                id_number=context.id_number, declared_variants=context.declared_variants,
                placement=placement), self._deps)
        except (SchemaGapError, SupabaseError) as exc:
            # Never report success when storage refused or failed.
            self._audit_safely(context, "DOCUMENT_PERSISTENCE_FAILED", {"sha256": digest, "error": type(exc).__name__})
            return finish(DocumentIntakeStatus.PERSISTENCE_FAILED, errors=[f"persistence:{type(exc).__name__}"])

        done = finish(
            _CASE_STATUS[result.outcome],
            case_id=result.case.case_id if result.case else None,
            employee_no=result.case.employee_no if result.case else None,
            existing_case_id=result.existing_case_id,
            approvals_created=list(result.approvals_created), reasons=list(result.reasons))
        if result.outcome == OpenCaseOutcome.LINKED_EXISTING and result.existing_case_id and self._details is not None:
            resumed = self._resume_incomplete(done, extraction, context, form, result.existing_case_id, source_ref)
            if resumed is not None:
                return resumed
        if result.outcome != OpenCaseOutcome.CASE_OPENED or result.case is None:
            return done
        if from_document:
            self._audit(context, "PLACEMENT_COMPLETED_FROM_DOCUMENT",
                        {"case_id": result.case.case_id, "fields": from_document}, case_id=result.case.case_id)
        return self._persist_and_verify(done, extraction, context, form, result.case.case_id, source_ref)

    # ------------------------------------------------------------------ partial-write recovery (B6)

    def _incomplete_case(self, form: OnboardingForm) -> Optional[str]:
        """An open case for this client and email that is missing its form or
        its person record: what an interrupted earlier run leaves behind."""
        deps = self._deps
        lookup = getattr(deps.case_repo, "case_ids_for_email", None)
        if lookup is None:  # a repository without the narrow lookup: nothing to check
            return None
        people: Optional[list] = None
        for case_id, status in lookup(form.client_id, form.candidate_email):
            # Only UC-01's own open cases: another use case's row (a status
            # outside UC-01's vocabulary) is not a UC-01 partial write.
            if status not in _UC01_OPEN_STATUSES:
                continue
            if people is None:
                people = deps.person_directory.find_by_email(form.client_id, form.candidate_email)
            has_person = any(p.case_id == case_id for p in people)
            if not has_person or deps.form_repo.get(case_id, 1) is None:
                return case_id
        return None

    def _resume_incomplete(self, done: DocumentIntakeResult, extraction: OnboardingDocumentExtraction,
                           context: DocumentIntakeContext, form: OnboardingForm, case_id: str,
                           source_ref: Optional[SourceDocumentRef]) -> Optional[DocumentIntakeResult]:
        """Finish the details/budget stage of an existing case - ONLY when that
        case's stored form is this document's form (same person, designation,
        joining date and salary) and it has no details yet. Anything else is a
        different submission and stays LINKED_EXISTING, untouched."""
        stored = self._deps.form_repo.get(case_id, 1)
        if stored is None or self._details is None:
            return None
        same = all(getattr(stored, n) == getattr(form, n) for n in (
            "client_id", "candidate_name", "candidate_email", "designation", "joining_date", "work_location_type"))
        if not same or stored.salary_and_terms.get_secret_value() != form.salary_and_terms.get_secret_value():
            return None
        try:
            if self._details.get(stored.form_id) is not None:
                return None  # complete already: a plain LINKED_EXISTING
        except IncompleteFormDetails:
            pass  # interrupted mid-save: complete it
        case = self._deps.case_repo.get(case_id)
        self._audit(context, "INCOMPLETE_RECORD_RESUMED", {"case_id": case_id, "form_id": stored.form_id,
                                                          "missing": ["form_details", "budget"]}, case_id=case_id)
        base = done.model_copy(update=dict(case_id=case_id, employee_no=case.employee_no if case else None,
                                           reasons=done.reasons + ["resumed_incomplete_record"]))
        return self._persist_and_verify(base, extraction, context, stored, case_id, source_ref)

    # ------------------------------------------------------------------ persistence

    def _persist_and_verify(self, done: DocumentIntakeResult, extraction: OnboardingDocumentExtraction,
                            context: DocumentIntakeContext, form: OnboardingForm, case_id: str,
                            source_ref: Optional[SourceDocumentRef]) -> DocumentIntakeResult:
        """Save the details and budget (with the source file's storage
        reference), then read EVERYTHING back. Any failure or mismatch is
        PERSISTENCE_FAILED."""
        digest = extraction.metadata.source_sha256
        readback: dict[str, bool] = {}
        stage = "form_details"
        try:
            if self._details is not None:
                record = build_form_details(
                    extraction, form_id=form.form_id, case_id=case_id, client_id=context.client_id,
                    review_confirmed_by=context.review_confirmed_by,
                    review_confirmed_at=self._clock() if context.review_confirmed_by else None,
                    source_document=source_ref)
                self._details.save(record)
                stage = "form_details_readback"
                mismatches = verify_saved(record, self._details.get(form.form_id))
                readback["form_details"] = not mismatches
                readback["budget"] = not any(m.startswith("budget") for m in mismatches)
                if mismatches:
                    raise _ReadbackMismatch("form_details", mismatches)
                self._audit(context, "FORM_DETAILS_SAVED", {
                    "case_id": case_id, "form_id": form.form_id, "data_origin": record.data_origin.value,
                    "fields": len(record.field_status), "checklist_items": len(record.checklist),
                    "budget_lines": len(record.budget.lines)}, case_id=case_id)
                self._audit(context, "BUDGET_RECORDED", {"case_id": case_id, "form_id": form.form_id,
                                                         **self._budget_audit(digest, record.budget)},
                            case_id=case_id)
            if source_ref is not None and self._source_store is not None:
                stage = "source_document_readback"
                stored = self._source_store.read(source_ref)
                readback["source_document"] = hashlib.sha256(stored).hexdigest() == source_ref.sha256
                if not readback["source_document"]:
                    raise _ReadbackMismatch("source_document", ["sha256"])
            stage = "employee_readback"
            mismatches = self._verify_employee(form, case_id)
            readback["employee_record"] = not mismatches
            if mismatches:
                raise _ReadbackMismatch("employee_record", mismatches)
        except (_ReadbackMismatch, FormDetailsConflict, SchemaGapError, SupabaseError, ValueError) as exc:
            detail = exc.fields if isinstance(exc, _ReadbackMismatch) else []
            self._audit_safely(context, "DOCUMENT_PERSISTENCE_FAILED", {
                "sha256": digest, "case_id": case_id, "stage": stage, "error": type(exc).__name__,
                "mismatched_fields": detail})
            return done.model_copy(update=dict(
                status=DocumentIntakeStatus.PERSISTENCE_FAILED, form_id=form.form_id, readback=readback,
                errors=done.errors + [f"persistence:{stage}:{type(exc).__name__}"] + [f"readback:{f}" for f in detail]))

        self._audit(context, "EMPLOYEE_RECORD_VERIFIED", {
            "case_id": case_id, "form_id": form.form_id, "readback": readback}, case_id=case_id)
        return done.model_copy(update=dict(form_id=form.form_id, persistence_verified=True, readback=readback))

    def _verify_employee(self, form: OnboardingForm, case_id: str) -> list[str]:
        """Case, form (with the decrypted salary) and person record, read back.
        Returns the names of fields that differ - never their values."""
        deps, out = self._deps, []
        case = deps.case_repo.get(case_id)
        if case is None:
            return ["case:<not found>"]
        for name in ("candidate_name", "candidate_email", "joining_date", "work_location_type"):
            if getattr(case, name) != getattr(form, name):
                out.append(f"case.{name}")
        saved = deps.form_repo.get(case_id, form.version)
        if saved is None:
            return out + ["form:<not found>"]
        for name in ("form_id", "candidate_name", "candidate_email", "designation", "joining_date", "status"):
            if getattr(saved, name) != getattr(form, name):
                out.append(f"form.{name}")
        if saved.salary_and_terms.get_secret_value() != form.salary_and_terms.get_secret_value():
            out.append("form.salary_and_terms")
        people = deps.person_directory.find_by_email(form.client_id, form.candidate_email)
        if not any(p.case_id == case_id for p in people):
            out.append("person_record")
        return out

    @staticmethod
    def _budget_audit(digest: str, budget: OnboardingBudget) -> dict[str, Any]:
        s = budget_summary(budget)
        return {"sha256": digest, "budget_status": s["status"], "status_reasons": s["status_reasons"],
                "currency": s["currency"], "budget_total": s["budget_total"],
                "category_totals": s["category_totals"], "totals_by_currency": s["totals_by_currency"],
                "counted_lines": sum(1 for x in budget.lines if x.counted), "lines": len(budget.lines),
                "reference_rates": len(budget.reference_rates),
                "calculation_version": s["calculation_version"]}

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _placement(extraction: OnboardingDocumentExtraction,
                   context: DocumentIntakeContext) -> tuple[Optional[CasePlacement], list[str]]:
        """The caller's placement, with department/grade the caller left empty
        taken from the document when it states them. location_name is NEVER
        taken from the document (decision D3 is open)."""
        placement = context.placement
        if placement is None:
            return None, []
        update = {name: str(f.value) for name in ("department", "grade")
                  if getattr(placement, name) is None and (f := getattr(extraction.supplementary, name)).usable}
        return (placement.model_copy(update=update) if update else placement), sorted(update)

    @staticmethod
    def _form(extraction: OnboardingDocumentExtraction, context: DocumentIntakeContext) -> OnboardingForm:
        e = extraction.employee
        # validate_extraction already guaranteed these (mandatory fields and
        # caller context); a None here is a programming error, not data.
        name, designation, joining = e.employee_name.value, e.designation.value, e.joining_date.value
        salary, location = e.final_salary.value, context.work_location_type
        assert name is not None and designation is not None and joining is not None
        assert salary is not None and location is not None
        return OnboardingForm(
            client_id=context.client_id, version=1,
            candidate_name=name, candidate_email=(context.candidate_email or "").strip() or (
                str(extraction.supplementary.employee_email.value)
                if extraction.supplementary.employee_email.usable else ""),
            designation=designation, joining_date=joining,
            work_location_type=location, client_site_id=context.client_site_id,
            salary_and_terms=salary, status=context.form_status,
            approved_by=context.approved_by, approved_at=context.approved_at)

    def _second_opinion(self, extraction: OnboardingDocumentExtraction, outcome: ValidationOutcome,
                        context: DocumentIntakeContext) -> None:
        """Escalate-only: may add a review reason, never remove one."""
        designation = extraction.employee.designation
        duties = extraction.roles.responsibilities
        if self._llm is None or not duties or designation.status != FieldStatus.PRESENT:
            return
        text = "\n".join(duties)[:MAX_RESPONSIBILITIES_CHARS]
        try:
            response = self._llm.complete(LlmRequest(
                task="responsibilities_review",
                inputs={"designation": str(designation.value), "responsibilities": text}))
        except LlmError as exc:
            self._audit(context, audit_action_for(exc), {"task": "responsibilities_review"})
            return
        self._audit(context, "LLM_ADVISORY", {
            "task": "responsibilities_review", "task_version": response.task_version,
            "label": response.label, "model": response.model, "confidence": round(response.confidence, 3)})
        if response.label != _RESPONSIBILITIES_OK:
            outcome.review_reasons.append("model_flagged_responsibilities")

    def _audit(self, context: DocumentIntakeContext, action: str, after: dict[str, Any], *,
               actor: str = _ACTOR, actor_type: ActorType = ActorType.SYSTEM,
               case_id: Optional[str] = None) -> None:
        self._deps.audit_log.append(AuditEntry(
            request_id=context.request_id, actor_type=actor_type, actor_id=actor, case_id=case_id,
            action=action, after=after, reference=context.request_id))

    def _audit_safely(self, context: DocumentIntakeContext, action: str, after: dict[str, Any]) -> None:
        try:
            self._audit(context, action, after)
        except (SchemaGapError, SupabaseError):
            pass  # the audit store is what failed; the result still says so


class _ReadbackMismatch(RuntimeError):
    def __init__(self, store: str, fields: list[str]) -> None:
        self.store, self.fields = store, list(fields)
        super().__init__(f"{store}: {len(fields)} field(s) differ after read-back")


def safe_extraction_view(extraction: OnboardingDocumentExtraction) -> dict[str, Any]:
    """Extraction for an API response: status, source and note per field,
    and the value ONLY for non-sensitive fields. Secret values (salary,
    mobile, parents' names) are reported by status alone."""
    fields: dict[str, Any] = {}
    for key, f in extraction.fields().items():
        entry: dict[str, Any] = {"status": f.status.value, "source": f.source}
        if f.note:
            entry["note"] = f.note
        if f.value is not None and not isinstance(f.value, SecretStr):
            entry["value"] = f.value.isoformat() if hasattr(f.value, "isoformat") else (
                f.value.value if hasattr(f.value, "value") else f.value)
        fields[key] = entry
    md = extraction.metadata
    return {
        "template_id": md.template_id, "template_recognised": md.template_recognised,
        "completeness": md.completeness, "fields": fields,
        "responsibilities": {"items": list(extraction.roles.responsibilities),
                             "is_example": extraction.roles.is_example, "source": extraction.roles.source},
        "reference_charges": [c.model_dump() for c in extraction.reference_charges],
        "supplementary": {k: {"status": f.status.value, "source": f.source,
                              **({"value": f.value} if f.value is not None else {}),
                              **({"note": f.note} if f.note else {})}
                          for k, f in extraction.supplementary_fields().items()},
        "synthetic_document": md.synthetic_document,
        "budget": budget_summary(extraction.budget) if extraction.budget is not None else None,
        "missing_fields": md.missing_fields, "invalid_fields": md.invalid_fields,
        "uncertain_fields": md.uncertain_fields,
    }
