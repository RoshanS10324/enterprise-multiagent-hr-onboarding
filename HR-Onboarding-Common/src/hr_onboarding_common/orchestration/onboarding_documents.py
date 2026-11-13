"""Start New Onboarding from an onboarding-form document: UC01's own extraction, for HR to review.

The extraction, its limits and its validation are UC01's (UC01_Employee_Record_Creation/src/sub_agent/documents,
ported from the upstream project, see docs/DOCUMENT_EXTRACTION_SOURCE_ANALYSIS.md): a deterministic reader for the
2026 onboarding-form template, .docx or text-based .pdf, every field PRESENT / MISSING / INVALID / UNCERTAIN, no model
deciding anything. This module only:

  * checks the upload before UC01 sees it (enabled, size, non-empty, extension, media type, file signature);
  * asks the UC01 worker to extract and validate (op extract_onboarding_document) -- never to open a case;
  * turns UC01's answer into the Start New Onboarding states (FAILED / VALIDATION_REQUIRED / READY_FOR_REVIEW) with
    actionable messages, and a PREFILL of the form fields that map exactly (PRESENT values only);
  * remembers a summary per extraction id in process memory, so the case started from it records WHERE its data came
    from (statuses and field names -- never values) and which prefilled fields HR changed. The browser cannot forge
    that provenance: the case carries the server's summary, looked up by id and tenant.

The case itself is created exactly as before: POST /cases -> onboarding_form_approval gate (HR lead) -> UC01
open_onboarding_case. Extraction never approves a form and never creates a record. Sensitive values (salary, mobile,
parents' names) are reported by status only, as UC01's safe view does; the Control Center has never carried salary
(UC01 receives "withheld") and still does not. Nothing here logs document content or extracted values.
"""

from __future__ import annotations

import base64
import os
import re
import uuid
from datetime import timedelta
from typing import Any

from hr_onboarding_common.contracts import now_utc

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # UC01's own cap (docx_reader.MAX_FILE_BYTES)
EXTENSIONS = {".docx": b"PK\x03\x04", ".pdf": b"%PDF-"}
MEDIA_TYPES = {".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                         "application/octet-stream", ""},
               ".pdf": {"application/pdf", "application/octet-stream", ""}}

# Form field -> UC01 extraction field. PRESENT values always prefill; UNCERTAIN values also prefill so HR can
# review and confirm rather than having to retype the value from scratch.
PREFILL = {"candidate_name": "employee.employee_name", "designation": "employee.designation",
           "joining_date": "employee.joining_date", "candidate_email": "supplementary.employee_email",
           "department": "supplementary.department", "grade": "supplementary.grade",
           "location": "supplementary.work_location"}
# Shown to HR for reference, never prefilled: they do not mean the form field (a manager's NAME is not a manager id;
# a nationality is not the client's nationality-group code).
REFERENCE = {"reporting_to": "employee.reporting_to",
             "nationality": "supplementary.nationality", "probation_months": "employee.probation_months",
             "employment_duration": "supplementary.employment_duration", "issuing_entity": "offer.issuing_entity"}
WORK_LOCATION_TYPES = {"OFFICE", "CLIENT_SITE"}  # UC01 WorkLocationType
_CONTEXT_ERROR = re.compile(r"^missing:context\.")

ACTIONS = {
    "UNSUPPORTED_FORMAT": "Upload the onboarding form as a .docx or a text-based .pdf.",
    "EMPTY_FILE": "The file is empty. Upload the completed onboarding form.",
    "FILE_TOO_LARGE": "Upload only the onboarding form (at most {max_mb} MB).",
    "NOT_A_DOCX": "The file is not a valid Word document. Save it again as .docx and upload it.",
    "CORRUPT_DOCX": "The Word document could not be read. Save it again as .docx and upload it.",
    "TOO_MANY_PARTS": "The document was refused for safety (unusual structure). Upload a clean copy of the form.",
    "TOO_LARGE_UNCOMPRESSED": "The document was refused for safety (expands too much). Upload a clean copy.",
    "UNSAFE_XML": "The document was refused for safety (unsafe content). Upload a clean copy of the form.",
    "NOT_A_PDF": "The file is not a valid PDF. Export the form again and upload it.",
    "CORRUPT_PDF": "The PDF could not be read. Export the form again and upload it.",
    "ENCRYPTED_PDF": "Remove the password protection from the PDF and upload it again.",
    "TOO_MANY_PAGES": "Upload only the onboarding form (the PDF has too many pages).",
    "NO_TEXT_LAYER": "This PDF is a scan without text. Upload the .docx or a text-based PDF (scans are not read).",
    "PDF_SUPPORT_UNAVAILABLE": "PDF reading is not installed on the server (UC01's optional 'pdf' extra). Upload the "
                               ".docx, or ask the administrator to install it.",
    "EXTRACTION_DISABLED": "Document extraction is switched off on this server. Enter the details manually.",
    "UC01_UNAVAILABLE": "UC01 could not be reached. Try again in a moment, or enter the details manually.",
    "TEMPLATE_NOT_RECOGNISED": "This is not the 2026 onboarding form UC01 reads. Upload that form, or enter the "
                               "details manually.",
}
# A mandatory UC01 field the document lacks or states invalidly, and the form field HR must then fill in themselves.
# employee.final_salary has none: the Control Center never carries salary (UC01 receives "withheld"), so HR can only
# acknowledge that the source form lacks it -- recorded on the case for the approver.
MANDATORY_FORM_FIELD = {"employee.employee_name": "candidate_name", "employee.designation": "designation",
                        "employee.joining_date": "joining_date"}


class OnboardingDocumentError(Exception):
    """Refused before or by UC01's reader: a code, a reason and the action HR can take (never a stack trace)."""

    def __init__(self, code: str, reason: str, *, http_status: int = 422):
        super().__init__(f"{code}: {reason}")
        self.code, self.reason, self.http_status = code, reason, http_status

    def view(self, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
        return {"status": "FAILED", "error": {"code": self.code, "reason": self.reason,
                                              "action": ACTIONS.get(self.code, "Check the file and try again.")
                                              .format(max_mb=max_bytes // (1024 * 1024))}}


def enabled() -> bool:
    return (os.environ.get("ONBOARDING_DOCUMENT_EXTRACTION_ENABLED") or "true").strip().lower() not in (
        "0", "false", "no", "off")


def max_bytes() -> int:
    raw = (os.environ.get("ONBOARDING_DOCUMENT_MAX_BYTES") or "").strip()
    return min(int(raw), DEFAULT_MAX_BYTES) if raw.isdigit() and int(raw) > 0 else DEFAULT_MAX_BYTES


def safe_filename(name: str | None) -> str:
    """The base name only (no directory part, no control characters), for the reader's extension check and display.
    The file itself is never written anywhere by the orchestrator."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()[:200]
    return base or "document"


def check_upload(data: bytes, filename: str, content_type: str, limit: int) -> str:
    """The extension, after the size, media-type and signature checks. Raises OnboardingDocumentError."""
    if not data:
        raise OnboardingDocumentError("EMPTY_FILE", "the uploaded file is empty")
    if len(data) > limit:
        raise OnboardingDocumentError("FILE_TOO_LARGE", f"the file exceeds {limit} bytes", http_status=413)
    ext = os.path.splitext(filename.lower())[1]
    if ext not in EXTENSIONS:
        raise OnboardingDocumentError("UNSUPPORTED_FORMAT", f"'{ext or 'no extension'}' is not supported")
    media = (content_type or "").split(";")[0].strip().lower()
    if media not in MEDIA_TYPES[ext]:
        raise OnboardingDocumentError("UNSUPPORTED_FORMAT", f"media type '{media}' does not match '{ext}'")
    if not data.startswith(EXTENSIONS[ext]):
        raise OnboardingDocumentError("NOT_A_DOCX" if ext == ".docx" else "NOT_A_PDF",
                                      f"the content is not a {ext} file")
    return ext


def _fields(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """UC01's safe view keys the template fields "section.field" under `fields`, and its supplementary rows
    "supplementary.field" under `supplementary`."""
    extraction = view.get("extraction") or {}
    return {**(extraction.get("fields") or {}), **(extraction.get("supplementary") or {})}


def prefill(view: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """(form values, form field -> extraction field) for PRESENT and UNCERTAIN fields.
    UNCERTAIN values are prefilled so HR can review and confirm in-place rather than retyping."""
    fields, values, sources = _fields(view), {}, {}
    for form_field, key in PREFILL.items():
        f = fields.get(key) or {}
        if f.get("status") in ("PRESENT", "UNCERTAIN") and f.get("value") not in (None, ""):
            values[form_field], sources[form_field] = f["value"], key
    location = fields.get("supplementary.work_location") or {}
    if location.get("status") == "PRESENT" and str(location.get("value", "")).upper() in WORK_LOCATION_TYPES:
        values["work_location_type"] = str(location["value"]).upper()
        sources["work_location_type"] = "supplementary.work_location"
    return values, sources


def reference(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Fields HR may want to see that do not fill a form field (non-sensitive only: UC01's safe view has no values for
    secrets)."""
    fields = _fields(view)
    return {name: {k: v for k, v in (fields.get(key) or {}).items() if k in ("status", "value", "note")}
            for name, key in REFERENCE.items() if fields.get(key)}


def extract(agent: Any, *, data: bytes, filename: str, content_type: str, actor: dict[str, Any]) -> dict[str, Any]:
    """Run UC01's extraction for an uploaded onboarding form. Returns the Start New Onboarding view; raises
    OnboardingDocumentError for a refused file (UC01's reader codes included)."""
    from hr_onboarding_common.orchestration.contracts import Actor as WorkerActor
    from hr_onboarding_common.orchestration.workers.uc_worker_client import WorkerCrashedError, WorkerTimeoutError

    if not enabled():
        raise OnboardingDocumentError("EXTRACTION_DISABLED", "ONBOARDING_DOCUMENT_EXTRACTION_ENABLED is off",
                                      http_status=503)
    agent.require_role(actor, "case_operator_roles")  # who may start a case may extract its form
    limit = max_bytes()
    name = safe_filename(filename)
    check_upload(data, name, content_type, limit)
    try:
        response = agent.deps.worker_pool.get("uc01").call(
            "extract_onboarding_document",
            {"content_base64": base64.b64encode(data).decode("ascii"), "filename": name},
            actor=WorkerActor(on_behalf_of=actor["on_behalf_of"], roles=actor.get("roles", [])),
            run_id=f"extract-{uuid.uuid4().hex[:12]}", idempotency_key=f"extract-{uuid.uuid4().hex}")
    except (WorkerCrashedError, WorkerTimeoutError) as exc:
        raise OnboardingDocumentError("UC01_UNAVAILABLE", type(exc).__name__, http_status=503) from None
    if not response.ok:
        raise OnboardingDocumentError("UC01_UNAVAILABLE", response.error_code or "UC01 refused the request",
                                      http_status=502)
    result = response.result or {}
    if result.get("status") == "REJECTED":
        raise OnboardingDocumentError(result.get("code") or "UNSUPPORTED_FORMAT", result.get("reason") or "")

    errors = list(result.get("errors") or [])
    if "template_not_recognised" in errors:
        # Not the form UC01 reads: no field of it is reliable enough to review or prefill.
        raise OnboardingDocumentError("TEMPLATE_NOT_RECOGNISED", "the document is not the onboarding-form template "
                                      f"'{(result.get('source_document') or {}).get('template_id') or 'unknown'}'")
    form_inputs = [e for e in errors if _CONTEXT_ERROR.match(e)]  # supplied on the form (email, location type)
    document_errors = [e for e in errors if not _CONTEXT_ERROR.match(e)]
    values, sources = prefill(result)
    extraction = result.get("extraction") or {}
    source = {**(result.get("source_document") or {}), "filename": name,
              "content_type": (content_type or "").split(";")[0].strip(), "size_bytes": len(data)}
    status = "VALIDATION_REQUIRED" if document_errors else "READY_FOR_REVIEW"
    extraction_id = f"ext-{uuid.uuid4().hex[:16]}"
    _prune(agent.deps.document_extractions)
    view = {
        "extraction_id": extraction_id, "status": status, "uc01_status": result.get("status"),
        "source_document": source, "extraction": extraction,
        "prefill": values, "prefill_sources": sources, "reference": reference(result),
        "missing_fields": extraction.get("missing_fields", []), "invalid_fields": extraction.get("invalid_fields", []),
        "uncertain_fields": extraction.get("uncertain_fields", []),
        "validation_errors": document_errors, "form_inputs_required": form_inputs,
        "review_reasons": list(result.get("review_reasons") or []),
        "outstanding_documents": list(result.get("outstanding_documents") or []),
        "completeness": extraction.get("completeness"),
        "review_required": bool(document_errors or result.get("review_reasons")),
    }
    # The provenance a case started from this extraction will carry: statuses and names, never values (the prefill
    # values are kept only to tell which ones HR changed, and are dropped when the case records the summary).
    agent.deps.document_extractions[extraction_id] = {
        "client_id": actor.get("client_id"), "extracted_by": actor.get("on_behalf_of"),
        "extracted_at": now_utc().isoformat(), "status": status, "uc01_status": result.get("status"),
        "source_document": {k: source.get(k) for k in ("sha256", "size_bytes", "template_id", "template_recognised",
                                                         "content_type")},
        "completeness": view["completeness"], "missing_fields": view["missing_fields"],
        "invalid_fields": view["invalid_fields"], "uncertain_fields": view["uncertain_fields"],
        "validation_errors": document_errors, "review_reasons": view["review_reasons"],
        "outstanding_documents": view["outstanding_documents"], "prefill_sources": sources, "_prefill": values,
    }
    return view


KEEP_FOR = timedelta(hours=24)  # an extraction not submitted within a day is extracted again
KEEP_AT_MOST = 500


def _prune(store: dict[str, dict[str, Any]]) -> None:
    """Bounded process memory: entries older than KEEP_FOR go, and at most KEEP_AT_MOST stay (oldest dropped)."""
    cutoff = (now_utc() - KEEP_FOR).isoformat()
    for key in [k for k, v in store.items() if (v.get("extracted_at") or "") < cutoff]:
        store.pop(key, None)
    for key in sorted(store, key=lambda k: store[k].get("extracted_at") or "")[:max(0, len(store) - KEEP_AT_MOST + 1)]:
        store.pop(key, None)


def provenance_for_case(agent: Any, *, extraction_id: str, client_id: str, submitted: dict[str, Any],
                        reviewed: bool, actor: dict[str, Any]) -> dict[str, Any]:
    """The extraction summary a new case records. Refused (WorkflowRequestError) for an unknown extraction or one of
    another tenant, and -- when UC01 found errors or review reasons -- unless HR confirmed the review."""
    from hr_onboarding_common.orchestration.orchestrator_agent import WorkflowRequestError

    _prune(agent.deps.document_extractions)
    summary = agent.deps.document_extractions.get(extraction_id)
    if summary is None or summary.get("client_id") != client_id:
        raise WorkflowRequestError("unknown source_extraction_id for this client (extract the document again)")
    if (summary["validation_errors"] or summary["review_reasons"]) and not reviewed:
        raise WorkflowRequestError("the extracted data needs HR review: confirm extraction_reviewed after checking "
                                   "the flagged fields")
    uncorrected = sorted({form_field for error in summary["validation_errors"]
                          for key, form_field in MANDATORY_FORM_FIELD.items()
                          if error.endswith(f":{key}") and not str(submitted.get(form_field) or "").strip()})
    if uncorrected:
        raise WorkflowRequestError("the document lacks a valid value for " + ", ".join(uncorrected)
                                   + ": enter it on the form (UC01 needs it; nothing is taken from an invalid field)")
    changed = sorted(f for f, v in summary["_prefill"].items() if str(submitted.get(f) or "") != str(v))
    entered = sorted(f for f in [*PREFILL, "work_location_type"]
                     if f not in summary["_prefill"] and str(submitted.get(f) or "").strip())
    return {**{k: v for k, v in summary.items() if not k.startswith("_")}, "extraction_id": extraction_id,
            "fields_changed_by_hr": changed, "fields_entered_by_hr": entered, "reviewed": bool(reviewed),
            "reviewed_by": actor.get("on_behalf_of") if reviewed else None}
