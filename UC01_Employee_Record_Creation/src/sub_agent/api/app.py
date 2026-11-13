"""UC-01 HTTP API. create_app(container) builds a FastAPI app wired to a
given Container (so tests can build a fresh one per test); the
module-level `app` is the default, built from environment settings on
first use (LazyDefaultApp), for `uvicorn sub_agent.api.app:app`.
"""

import base64
import binascii
import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Callable, Optional
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from pydantic import ValidationError

from ..compare_sources import CompareResult
from ..form_changes import ProposeResult
from ..hris_prepare import PrepareResult
from ..hris_write import WriteOutcome
from ..intake.email_intake import (
    IntakeResult,
    handle_acceptance_email,
    handle_form_approved,
    list_pending_starts,
)
from ..models import (
    CasePlacement,
    CaseStatusView,
    FormChangeRequest,
    FormStatus,
    OfferRecord,
    OnboardingForm,
    PendingApprovalView,
    SourceSnapshot,
    VariantFlags,
    Viewer,
)
from ..documents.service import DocumentIntakeContext, safe_extraction_view
from ..onboarding_entry import OpenCaseRequest, open_onboarding_case
from .container import Container, build_container
from .error_handlers import register_exception_handlers
from .identity import get_viewer, make_system_key_dependency
from .schemas import (
    OnboardingDocumentBody,
    OnboardingDocumentResponse,
    AuditEntryView,
    AuditIntegrityView,
    CompareRequestBody,
    DecisionBody,
    DevApproveFormBody,
    DevSeedBody,
    DevSeedResponse,
    DiscrepancyDecisionBody,
    DiscrepancyDecisionResponse,
    FormApprovedBody,
    FormChangeBody,
    FormDecisionResponse,
    HealthResponse,
    HrisRecordSummary,
    InboundEmailBody,
    OnboardingRequestBody,
    OnboardingResponse,
    PendingStartView,
    VariantDecisionResponse,
)
from .settings import Settings

logger = logging.getLogger("uc01.api")


def create_app(container: Container, include_dev_routes: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Verify the database before serving, and close the pool after.

        Deliberately here rather than in build_container: this runs when
        a server actually starts, so importing the module never performs
        network I/O. There is no fallback - an unreachable database
        stops startup instead of silently degrading to in-memory
        storage.
        """
        if container.postgrest is not None:
            container.postgrest.ping()
        try:
            yield
        finally:
            if container.postgrest is not None:
                container.postgrest.close()

    app = FastAPI(title="UC-01 Employee Record API", lifespan=lifespan)
    register_exception_handlers(app)

    require_system_key = make_system_key_dependency(container.settings)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.post(
        "/internal/onboarding-requests",
        response_model=OnboardingResponse,
        dependencies=[Depends(require_system_key)],
    )
    def open_onboarding_request(body: OnboardingRequestBody) -> OnboardingResponse:
        # The approval is an attestation from the upstream caller (open
        # question OQ-01/Q-11: who approves the form). UC-01 never approves
        # a form itself: the form is built in whatever status the caller
        # states, and only carries approved_by/approved_at when the caller
        # asserts APPROVED. Anything else is passed through unchanged, so
        # the start gate below is what decides to hold it.
        form = OnboardingForm(
            client_id=body.client_id,
            version=1,
            candidate_name=body.candidate_name,
            candidate_email=body.candidate_email,
            designation=body.designation,
            joining_date=body.joining_date,
            work_location_type=body.work_location_type,
            client_site_id=body.client_site_id,
            salary_and_terms=body.salary_and_terms.get_secret_value(),
            status=FormStatus(body.form_status),
            approved_by=body.approved_by,
            approved_at=body.approved_at,
        )

        declared_variants = (
            VariantFlags(**body.declared_variants.model_dump())
            if body.declared_variants is not None
            else None
        )

        onboarding_request = OpenCaseRequest(
            request_id=body.request_id or f"req-{uuid4().hex}",
            client_id=body.client_id,
            hr_officer_id=body.hr_officer_id,
            offer_accepted=body.offer_accepted,
            form=form,
            hiring_freeze=body.hiring_freeze,
            id_number=body.id_number,
            declared_variants=declared_variants,
            # CasePlacement applies the approved rules: location_name
            # required and trimmed, the four descriptive fields optional
            # (blank -> NULL), current_stage -> INTAKE and
            # onboarding_attributes -> empty when omitted.
            placement=CasePlacement(
                location_name=body.location_name,
                department=body.department,
                grade=body.grade,
                nationality_group=body.nationality_group,
                project_name=body.project_name,
                current_stage=body.current_stage,
                onboarding_attributes=body.onboarding_attributes,
            ),
        )
        result = open_onboarding_case(onboarding_request, container.onboarding_deps)

        return OnboardingResponse(
            outcome=result.outcome.value,
            case_id=result.case.case_id if result.case else None,
            employee_no=result.case.employee_no if result.case else None,
            approvals_created=result.approvals_created,
            existing_case_id=result.existing_case_id,
            reasons=result.reasons,
        )

    @app.post(
        "/internal/onboarding-documents",
        response_model=OnboardingDocumentResponse,
        dependencies=[Depends(require_system_key)],
    )
    def onboarding_document(body: OnboardingDocumentBody) -> OnboardingDocumentResponse:
        """Read an onboarding-form .docx or text-based .pdf, extract and validate it, and -
        only when validation and HR review allow - pass it to the same
        open_onboarding_case flow as /internal/onboarding-requests."""
        try:
            data = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=422, detail="content_base64 is not valid base64") from None
        placement = (
            CasePlacement(location_name=body.location_name, department=body.department, grade=body.grade,
                          nationality_group=body.nationality_group, project_name=body.project_name)
            if body.location_name and body.location_name.strip() else None
        )
        context = DocumentIntakeContext(
            request_id=body.request_id or f"doc-{uuid4().hex}",
            client_id=body.client_id, hr_officer_id=body.hr_officer_id, candidate_email=body.candidate_email,
            offer_accepted=body.offer_accepted, work_location_type=body.work_location_type,
            client_site_id=body.client_site_id, placement=placement,
            declared_variants=VariantFlags(**body.declared_variants.model_dump()) if body.declared_variants else None,
            hiring_freeze=body.hiring_freeze, form_status=FormStatus(body.form_status),
            approved_by=body.approved_by, approved_at=body.approved_at,
            review_confirmed_by=body.review_confirmed_by,
        )
        result = container.documents.process(data, body.filename, context)
        return OnboardingDocumentResponse(
            **result.model_dump(exclude={"extraction", "status", "source_document"}),
            source_document=result.source_document.model_dump() if result.source_document else None,
            status=result.status.value,
            extraction=safe_extraction_view(result.extraction) if result.extraction else None,
        )

    # --- [Proposed] dev trigger simulator (OQ-01/Q-11); see intake/email_intake.py ---

    @app.post(
        "/internal/inbound-email",
        response_model=IntakeResult,
        dependencies=[Depends(require_system_key)],
    )
    def inbound_email(body: InboundEmailBody) -> IntakeResult:
        return handle_acceptance_email(body.raw_email, container.intake_deps)

    @app.post(
        "/internal/form-approved",
        response_model=IntakeResult,
        dependencies=[Depends(require_system_key)],
    )
    def form_approved(body: FormApprovedBody) -> IntakeResult:
        return handle_form_approved(body.client_id, body.offer_ref, container.intake_deps)

    @app.get("/internal/pending-starts", response_model=list[PendingStartView])
    def pending_starts(client_id: str, viewer: Viewer = Depends(get_viewer)) -> list[PendingStartView]:
        pending = list_pending_starts(viewer, client_id, container.intake_deps, container.deps.audit_log)
        return [
            PendingStartView(
                pending_id=p.pending_id, offer_ref=p.offer_ref, reasons=p.reasons, status=p.status.value
            )
            for p in pending
        ]

    if include_dev_routes and container.settings.app_env == "dev":

        @app.post(
            "/dev/seed", response_model=DevSeedResponse, dependencies=[Depends(require_system_key)]
        )
        def dev_seed(body: DevSeedBody) -> DevSeedResponse:
            offer = OfferRecord(
                client_id=body.client_id,
                offer_ref=body.offer_ref,
                candidate_email=body.candidate_email,
                hr_officer_id=body.hr_officer_id,
            )
            container.intake_deps.offer_repo.save(offer)

            form = OnboardingForm(
                client_id=body.client_id,
                version=1,
                candidate_name=body.candidate_name,
                candidate_email=body.candidate_email,
                designation=body.designation,
                joining_date=body.joining_date,
                work_location_type=body.work_location_type,
                client_site_id=body.client_site_id,
                salary_and_terms=body.salary_and_terms.get_secret_value(),
                status=FormStatus(body.form_status),
                approved_by=body.approved_by,
                approved_at=body.approved_at,
            )
            container.intake_deps.form_source.save_for_offer(body.client_id, body.offer_ref, form)

            return DevSeedResponse(offer_ref=body.offer_ref, form_status=form.status.value)

        @app.post(
            "/dev/approve-form", response_model=IntakeResult, dependencies=[Depends(require_system_key)]
        )
        def dev_approve_form(body: DevApproveFormBody) -> IntakeResult:
            form = container.intake_deps.form_source.latest_for_offer(body.client_id, body.offer_ref)
            if form is None:
                raise ValueError(f"no form seeded for offer {body.offer_ref!r}")
            approved_form = form.approve(approved_by=body.approved_by, approved_at=datetime.now(timezone.utc))
            container.intake_deps.form_source.save_for_offer(body.client_id, body.offer_ref, approved_form)
            return handle_form_approved(body.client_id, body.offer_ref, container.intake_deps)

    @app.get("/cases/{case_id}/status", response_model=CaseStatusView)
    def get_case_status(case_id: str, viewer: Viewer = Depends(get_viewer)) -> CaseStatusView:
        return container.service.get_status(viewer, case_id)

    @app.get("/cases/{case_id}/audit", response_model=list[AuditEntryView])
    def get_case_audit(case_id: str, viewer: Viewer = Depends(get_viewer)) -> list[AuditEntryView]:
        # AuditEntryView, not AuditEntry: the hash-chain fields stay out
        # of a per-case read (see schemas.AuditEntryView).
        return [AuditEntryView.of(e) for e in container.service.get_audit_trail(viewer, case_id)]

    @app.get("/audit/integrity", response_model=AuditIntegrityView)
    def get_audit_integrity(viewer: Viewer = Depends(get_viewer)) -> AuditIntegrityView:
        """Verify the hash chain over the complete log. AUDITOR only."""
        checked, violations = container.service.verify_audit_integrity(viewer)
        return AuditIntegrityView(
            entries_checked=checked, intact=not violations, violations=violations
        )

    @app.get("/approvals/pending", response_model=list[PendingApprovalView])
    def get_pending_approvals(viewer: Viewer = Depends(get_viewer)) -> list[PendingApprovalView]:
        return container.service.list_pending_approvals(viewer)

    @app.post("/cases/{case_id}/compare", response_model=CompareResult)
    def compare_case(
        case_id: str, body: CompareRequestBody, viewer: Viewer = Depends(get_viewer)
    ) -> CompareResult:
        snapshots = [SourceSnapshot(source=s.source, values=s.values) for s in body.snapshots]
        return container.service.compare_sources(viewer, case_id, snapshots)

    @app.post(
        "/discrepancies/{discrepancy_id}/decision", response_model=DiscrepancyDecisionResponse
    )
    def decide_discrepancy(
        discrepancy_id: str, body: DiscrepancyDecisionBody, viewer: Viewer = Depends(get_viewer)
    ) -> DiscrepancyDecisionResponse:
        discrepancy = container.service.resolve_discrepancy(viewer, discrepancy_id, body.decision)
        return DiscrepancyDecisionResponse(
            discrepancy_id=discrepancy.discrepancy_id,
            field=discrepancy.field,
            other_source=discrepancy.other_source,
            status=discrepancy.status,
            decided_by=discrepancy.decided_by,
            decided_at=discrepancy.decided_at,
        )

    @app.post("/cases/{case_id}/form-changes", response_model=ProposeResult)
    def propose_form_change(
        case_id: str, body: FormChangeBody, viewer: Viewer = Depends(get_viewer)
    ) -> ProposeResult:
        request = FormChangeRequest(
            case_id=case_id,
            requested_by=viewer.user_id,
            change_reason=body.change_reason,
            candidate_name=body.candidate_name,
            designation=body.designation,
            joining_date=body.joining_date,
            work_location_type=body.work_location_type,
            client_site_id=body.client_site_id,
            salary_and_terms=body.salary_and_terms,
            source_discrepancy_id=body.source_discrepancy_id,
        )
        return container.service.propose_form_change(viewer, request, body.approver_id)

    @app.post("/form-changes/{approval_id}/decision", response_model=FormDecisionResponse)
    def decide_form_change(
        approval_id: str, body: DecisionBody, viewer: Viewer = Depends(get_viewer)
    ) -> FormDecisionResponse:
        return container.service.decide_form_change(viewer, approval_id, body.approve, body.note)

    @app.post("/variant-approvals/{approval_id}/decision", response_model=VariantDecisionResponse)
    def decide_variant(
        approval_id: str, body: DecisionBody, viewer: Viewer = Depends(get_viewer)
    ) -> VariantDecisionResponse:
        approval = container.service.decide_variant(viewer, approval_id, body.approve, body.note)
        return VariantDecisionResponse(
            approval_id=approval.approval_id,
            checkpoint=approval.checkpoint,
            status=approval.status.value,
            decided_by=approval.decided_by,
            decided_at=approval.decided_at,
        )

    @app.post("/cases/{case_id}/hris/prepare", response_model=PrepareResult)
    def prepare_hris(case_id: str, viewer: Viewer = Depends(get_viewer)) -> PrepareResult:
        return container.service.prepare_hris(viewer, case_id)

    @app.post("/hris-approvals/{approval_id}/decision", response_model=list[HrisRecordSummary])
    def decide_hris_write(
        approval_id: str, body: DecisionBody, viewer: Viewer = Depends(get_viewer)
    ) -> list[HrisRecordSummary]:
        records = container.service.decide_hris_write(viewer, approval_id, body.approve, body.note)
        return [
            HrisRecordSummary(
                record_id=r.record_id, system=r.system, status=r.status, external_ref=r.external_ref
            )
            for r in records
        ]

    @app.post("/cases/{case_id}/hris/write", response_model=WriteOutcome)
    def execute_hris_write(case_id: str, viewer: Viewer = Depends(get_viewer)) -> WriteOutcome:
        return container.service.execute_hris_write(viewer, case_id)

    return app


def _build_default_app() -> FastAPI:
    settings = Settings.from_env()
    container = build_container(settings)
    return create_app(container)


def _configuration_error_message(exc: Exception) -> str:
    """Why the default app could not be built: variable names, never values.

    str() of a pydantic ValidationError would echo the input values
    (input_value=...), so only its messages are kept.
    """
    if isinstance(exc, ValidationError):
        return "UC-01 configuration refused: " + "; ".join(e["msg"] for e in exc.errors())
    return f"UC-01 configuration refused: {exc}"


class ConfigurationRefused(RuntimeError):
    """The default app's configuration was refused; the server must not start."""


class LazyDefaultApp:
    """The default ASGI app, built on first use instead of at import.

    Importing sub_agent.api must not need live credentials (tests, scripts
    and tooling import it). The configuration is still enforced, only later:

    * ASGI lifespan startup (what uvicorn sends first) builds the app. If
      Settings or build_container refuses - e.g. SUPABASE_URL without
      UC01_FIELD_ENCRYPTION_KEY, or in-memory adapters outside dev - it
      answers ``lifespan.startup.failed`` and the server does not start.
    * An HTTP request that arrives without startup having succeeded gets
      503; it is never served by a half-configured app.

    The real app is built once and then used for everything, including its
    own lifespan (the Supabase connection test in create_app).
    """

    def __init__(self, factory: Callable[[], FastAPI] = _build_default_app) -> None:
        self._factory = factory
        self._app: Optional[FastAPI] = None
        self._lock = threading.Lock()

    def build(self) -> FastAPI:
        if self._app is None:
            with self._lock:
                if self._app is None:
                    self._app = self._factory()
        return self._app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        try:
            app = self.build()
        except Exception as exc:  # noqa: BLE001 - any refusal must keep the server down
            message = _configuration_error_message(exc)
            logger.error("uc01.startup_refused", extra={"error_type": type(exc).__name__})
            if scope["type"] == "lifespan":
                # Starlette's own convention: report the failure, then raise,
                # so uvicorn and test clients both stop. The raised error is
                # the sanitised message, never the original (it may echo values).
                await receive()  # lifespan.startup
                await send({"type": "lifespan.startup.failed", "message": message})
                raise ConfigurationRefused(message) from None
            if scope["type"] == "http":
                body = json.dumps({"detail": "UC-01 is not configured"}).encode()
                await send({"type": "http.response.start", "status": 503,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
            return
        await app(scope, receive, send)

    def __getattr__(self, name: str):
        # Public attributes (e.g. `app.router` for tooling) build and
        # validate on access. Private/dunder probes - ASGI servers and test
        # clients check things like `__await__` - must not trigger a build.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.build(), name)


app = LazyDefaultApp()
