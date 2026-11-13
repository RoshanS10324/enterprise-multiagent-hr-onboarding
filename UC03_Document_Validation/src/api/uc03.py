from __future__ import annotations

import os
import tempfile
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from guardrails.access import AccessDeniedError
from hooks.approval import ApprovalRequiredError
from src.sub_agent.checklist import ChecklistService
from src.sub_agent.extraction import get_extractor
from src.sub_agent.models import (
    DocumentValidationRequest,
    DocumentValidationResult,
)
from src.sub_agent.validator import DocumentValidationService
from src.state.audit import audit_event
from src.state.enums import ActorType, ApprovalStatus, HRReviewStatus, SubmissionStatus
from src.state.models import ApprovalRecord, AuditEvent
from src.state.store import (
    InMemoryAuditStore,
    InMemoryCaseStore,
    get_audit_store,
    get_case_store,
    get_result_store,
)
from datetime import date

router = APIRouter(prefix="/onboarding/uc03", tags=["UC-03 Document Validation"])


@router.post("/upload-and-validate", response_model=DocumentValidationResult)
async def upload_and_validate(
    file: UploadFile = File(...),
    case_id: str = Form(...),
    employee_id: str = Form(...),
    client_id: str = Form("client-001"),
    role: str = Form(""),
    grade: str = Form(""),
    nationality: str = Form(""),
    location: str = Form(""),
    employee_name_form: str = Form(...),
    document_type: str = Form(...),
    visa_required: bool = Form(False),
    requesting_actor_id: str = Form("hr-001"),
    requesting_actor_type: ActorType = Form(ActorType.HR_USER),
    case_store: InMemoryCaseStore = Depends(get_case_store),
    audit_store: InMemoryAuditStore = Depends(get_audit_store),
) -> DocumentValidationResult:
    """
    Upload a real document file (PDF or image). The agent extracts fields
    automatically using the hybrid extractor, then runs the full validation pipeline.
    Raw file bytes are never logged — only the document_ref (file path) is recorded.
    """
    allowed_types = {"application/pdf", "image/jpeg", "image/png", "image/tiff"}
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{file.content_type}'. Allowed: PDF, JPEG, PNG, TIFF.",
        )

    file_bytes = await file.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    # Write to a temp file so the extractor can open it by path
    suffix = os.path.splitext(file.filename or "doc.pdf")[1] or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        extractor = await get_extractor()
        extraction = await extractor.extract(tmp_path, document_type)
    finally:
        os.unlink(tmp_path)

    document_ref = f"upload://{case_id}/{document_type}/{file.filename}"

    # Parse expiry date from extracted fields (formats: DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY)
    expiry_date: date | None = None
    raw_expiry = extraction.extracted_fields.get("expiry_date")
    if raw_expiry:
        import re as _re
        from datetime import datetime as _dt
        _m = _re.match(r"(\d{1,2})[\/\-\. ](\d{1,2})[\/\-\. ](\d{2,4})", raw_expiry)
        if _m:
            d, mo, y = int(_m.group(1)), int(_m.group(2)), int(_m.group(3))
            y = 2000 + y if y < 100 else y
            try:
                expiry_date = date(y, mo, d)
            except ValueError:
                expiry_date = None

    request = DocumentValidationRequest(
        case_id=case_id,
        employee_id=employee_id,
        client_id=client_id,
        role=role,
        grade=grade,
        nationality=nationality,
        location=location,
        employee_name_form=employee_name_form,
        offer_accepted=True,
        onboarding_form_approved=True,
        document_type=document_type,
        submission_status=SubmissionStatus.RECEIVED,
        extracted_name=extraction.extracted_name,
        extracted_fields=extraction.extracted_fields,
        expiry_date=expiry_date,
        readable=extraction.confidence > 0.0,
        required_fields_present=bool(extraction.extracted_fields),
        extraction_confidence=extraction.confidence,
        evidence_stored=True,
        hr_review_status=HRReviewStatus.PENDING,
        visa_required=visa_required,
        requesting_actor_id=requesting_actor_id,
        requesting_actor_type=requesting_actor_type,
        validation_date=date.today(),
        document_ref=document_ref,
    )

    checklist_service = ChecklistService("default")
    service = DocumentValidationService(
        case_store=case_store,
        audit_store=audit_store,
        checklist_service=checklist_service,
        extractor=extractor,
        result_store=get_result_store(),
    )
    try:
        return await service.validate(request)
    except AccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/validate", response_model=DocumentValidationResult)
async def validate_document(
    request: DocumentValidationRequest,
    case_store: InMemoryCaseStore = Depends(get_case_store),
    audit_store: InMemoryAuditStore = Depends(get_audit_store),
) -> DocumentValidationResult:
    extractor = await get_extractor()
    checklist_service = ChecklistService("default")
    service = DocumentValidationService(
        case_store=case_store,
        audit_store=audit_store,
        checklist_service=checklist_service,
        extractor=extractor,
        result_store=get_result_store(),
    )
    try:
        return await service.validate(request)
    except AccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/approval", response_model=ApprovalRecord)
async def submit_approval(
    case_id: str,
    action_type: str,
    actor_id: str,
    actor_type: ActorType,
    status: ApprovalStatus,
    notes: str | None = None,
    case_store: InMemoryCaseStore = Depends(get_case_store),
    audit_store: InMemoryAuditStore = Depends(get_audit_store),
) -> ApprovalRecord:
    record = ApprovalRecord(
        approval_id=str(uuid.uuid4()),
        case_id=case_id,
        action_type=action_type,
        actor_id=actor_id,
        actor_type=actor_type,
        status=status,
        created_at=datetime.now(UTC),
        approved_at=datetime.now(UTC) if status == ApprovalStatus.APPROVED else None,
        notes=notes,
    )
    await case_store.save_approval(record)
    await audit_event(
        store=audit_store,
        case_id=case_id,
        actor_id=actor_id,
        actor_type=actor_type,
        action="APPROVAL_SUBMITTED",
        detail={
            "action_type": action_type,
            "status": status.value,
            "approval_id": record.approval_id,
        },
    )
    return record


@router.get("/audit/{case_id}", response_model=list[AuditEvent])
async def get_audit_trail(
    case_id: str,
    requesting_actor_id: str,
    audit_store: InMemoryAuditStore = Depends(get_audit_store),
) -> list[AuditEvent]:
    return await audit_store.get_events(case_id)
