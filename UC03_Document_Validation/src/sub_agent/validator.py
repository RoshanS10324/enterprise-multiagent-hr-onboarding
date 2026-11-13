from __future__ import annotations

import uuid
from typing import Any

from guardrails.access import AccessDeniedError, assert_case_access
from guardrails.injection import scan_for_injection
from src.state.audit import audit_event
from src.state.enums import (
    ApprovalStatus,
    ComplianceStatus,
    DocumentStatus,
    EvidenceStatus,
    HRReviewStatus,
    RequiredAction,
    ValidationFinding,
    VisaPackStatus,
)
from src.state.models import OnboardingCase
from src.state.store import AuditStore, CaseStore, ValidationResultStore
from src.sub_agent.advisor_port import UC03AdvisorPort
from src.sub_agent.checklist import ChecklistService
from src.sub_agent.comms import draft_communication_outcome
from src.sub_agent.extraction import DocumentExtractor
from src.sub_agent.models import (
    DocumentValidationRequest,
    DocumentValidationResult,
)
from src.sub_agent.name_agent import check_name_consistency_agent
from src.sub_agent.rules import (
    check_attestation,
    check_evidence_stored,
    check_expiry,
    check_onboarding_start_conditions,
    check_readability,
    check_required_fields,
    determine_compliance_status,
    determine_next_action,
    determine_overall_status,
)


class DocumentValidationService:
    def __init__(
        self,
        case_store: CaseStore,
        audit_store: AuditStore,
        checklist_service: ChecklistService,
        extractor: DocumentExtractor,
        advisor: UC03AdvisorPort | None = None,
        result_store: ValidationResultStore | None = None,
    ) -> None:
        self._case_store = case_store
        self._audit_store = audit_store
        self._checklist = checklist_service
        self._extractor = extractor
        # Optional model help (message wording, advisory name notes). None = fully deterministic, the default. It never decides anything:
        # every finding, status, action and approval below comes from the rules.
        self._advisor = advisor
        # Optional durable store for the validation result itself (e.g. Supabase).
        # None = the result is only returned, as before.
        self._result_store = result_store

    async def validate(self, request: DocumentValidationRequest) -> DocumentValidationResult:
        """Validate one document, then store the result when a result store is configured.

        The decision is made entirely by `_validate`. Storing happens afterwards and never
        changes it. A store that cannot write raises (the caller sees the failure); a store that
        declines for a documented reason reports it in `persistence_status`.
        """
        advisory: dict[str, Any] = {}
        result = await self._validate(request, advisory)
        if self._result_store is None:
            return result
        status = await self._result_store.save_result(request, result, advisory or None)
        return result.model_copy(update={"persistence_status": status})

    async def _validate(
        self, request: DocumentValidationRequest, advisory: dict[str, Any]
    ) -> DocumentValidationResult:
        findings: list[ValidationFinding] = []
        name_used_llm = False
        draft_outcome = None
        injection_detected = False
        access_denied = False

        # 1. Load or initialise the onboarding case
        case = await self._case_store.get_case(request.case_id)
        if case is None:
            case = OnboardingCase(
                case_id=request.case_id,
                employee_id=request.employee_id,
                client_id=request.client_id,
                role=request.role,
                grade=request.grade,
                nationality=request.nationality,
                location=request.location,
                offer_accepted=request.offer_accepted,
                onboarding_form_approved=request.onboarding_form_approved,
                employee_name_form=request.employee_name_form,
                authorized_actor_ids={request.requesting_actor_id},
            )
            await self._case_store.save_case(case)

        # 2. Access control (BR-13)
        try:
            assert_case_access(
                requesting_actor_id=request.requesting_actor_id,
                case_actor_ids=case.authorized_actor_ids | {request.requesting_actor_id},
                case_id=request.case_id,
            )
        except AccessDeniedError:
            access_denied = True
            await audit_event(
                store=self._audit_store,
                case_id=request.case_id,
                actor_id=request.requesting_actor_id,
                actor_type=request.requesting_actor_type,
                action="ACCESS_DENIED",
                detail={"document_type": request.document_type},
                correlation_id=request.correlation_id,
            )
            return DocumentValidationResult(
                case_id=request.case_id,
                document_type=request.document_type,
                document_status=DocumentStatus.REJECTED,
                validation_findings=[ValidationFinding.CHECKLIST_NOT_SPECIFIED],
                outstanding_requirements=[],
                evidence_status=EvidenceStatus.NOT_STORED,
                hr_decision=HRReviewStatus.PENDING,
                compliance_item_status=ComplianceStatus.BLOCKED,
                required_next_action=RequiredAction.DENY_ACCESS,
                approval_status=ApprovalStatus.PENDING,
                audit_reference=str(uuid.uuid4()),
                detail="Access denied: actor not authorised for this case.",
                injection_detected=False,
            )

        # 3. BR-01: onboarding start conditions
        on_hold = not check_onboarding_start_conditions(
            offer_accepted=request.offer_accepted,
            onboarding_form_approved=request.onboarding_form_approved,
        )
        if on_hold:
            audit_ref = await audit_event(
                store=self._audit_store,
                case_id=request.case_id,
                actor_id=request.requesting_actor_id,
                actor_type=request.requesting_actor_type,
                action="VALIDATION_SKIPPED_ON_HOLD",
                detail={"document_type": request.document_type},
                correlation_id=request.correlation_id,
            )
            return DocumentValidationResult(
                case_id=request.case_id,
                document_type=request.document_type,
                document_status=DocumentStatus.REQUESTED,
                validation_findings=[],
                outstanding_requirements=[],
                evidence_status=EvidenceStatus.PENDING_UPLOAD,
                hr_decision=HRReviewStatus.PENDING,
                compliance_item_status=ComplianceStatus.INCOMPLETE,
                required_next_action=RequiredAction.NONE,
                approval_status=ApprovalStatus.NOT_REQUIRED,
                audit_reference=audit_ref.event_id,
                detail="Onboarding is on hold. Offer acceptance or form approval is pending.",
            )

        # 4. Injection scan on untrusted text fields (BR-16)
        for field_text in [request.extracted_name, request.document_ref]:
            if field_text:
                scan = scan_for_injection(field_text)
                if scan.detected:
                    injection_detected = True
                    findings.append(ValidationFinding.PROMPT_INJECTION_DETECTED)
                    break

        # 5. Checklist lookup (BR-03)
        ctx = self._checklist.build_context(request.model_dump())
        requirement = self._checklist.get_requirement(request.document_type, ctx)

        if requirement is None and request.submission_status.value == "RECEIVED":
            findings.append(ValidationFinding.UNREQUESTED_SENSITIVE)
        elif requirement is None:
            findings.append(ValidationFinding.CHECKLIST_NOT_SPECIFIED)

        # 5b. Duplicate document detection. With a revision number (UC-03's document collection),
        # only the same or an older revision is a duplicate: a corrected revision is validated again.
        if request.submission_status.value == "RECEIVED":
            if request.document_revision is not None:
                seen = case.received_revisions.get(request.document_type)
                duplicate = seen is not None and request.document_revision <= seen
            else:
                duplicate = request.document_type in case.received_documents
            if duplicate:
                findings.append(ValidationFinding.DUPLICATE_REQUEST)

        # 6. Missing document check
        if request.submission_status.value == "MISSING":
            findings.append(ValidationFinding.MISSING_DOCUMENT)

        # 7. Deterministic validation rules (only if document was received and not a duplicate)
        if (
            requirement is not None
            and request.submission_status.value == "RECEIVED"
            and ValidationFinding.DUPLICATE_REQUEST not in findings
        ):
            validations = requirement.validations

            if validations.get("check_readability"):
                finding = check_readability(request.readable)
                if finding:
                    findings.append(finding)

            if validations.get("check_required_fields") and not injection_detected:
                finding = check_required_fields(request.required_fields_present)
                if finding:
                    findings.append(finding)

            if validations.get("check_expiry"):
                finding = check_expiry(
                    expiry_date=request.expiry_date,
                    validation_date=request.validation_date,
                    check_required=True,
                )
                if finding:
                    findings.append(finding)

            if (
                validations.get("check_name_consistency")
                and ValidationFinding.UNREADABLE not in findings
                and not injection_detected
            ):
                name_result = await check_name_consistency_agent(
                    name_from_form=request.employee_name_form,
                    extracted_name=request.extracted_name,
                    confidence=request.extraction_confidence,
                    advisor=self._advisor,
                )
                name_used_llm = name_result.used_llm
                if name_result.finding:
                    findings.append(name_result.finding)

            if request.extraction_confidence < 0.7:
                if ValidationFinding.LOW_CONFIDENCE not in findings:
                    findings.append(ValidationFinding.LOW_CONFIDENCE)

            # Attestation: NDA/policy docs require explicit employee acknowledgement
            if validations.get("check_attestation"):
                finding = check_attestation(
                    attestation_confirmed=request.attestation_confirmed,
                    requires_attestation=True,
                )
                if finding:
                    findings.append(finding)

        # 8. Evidence storage (BR-09)
        requires_evidence = bool(
            requirement and requirement.validations.get("requires_evidence_stored", False)
        )
        evidence_status = check_evidence_stored(
            evidence_stored=request.evidence_stored,
            requires_evidence=requires_evidence,
        )
        if evidence_status == EvidenceStatus.NOT_STORED and requires_evidence:
            findings.append(ValidationFinding.EVIDENCE_NOT_STORED)

        # 9. If no issues found so far, mark VALID
        if not findings:
            findings.append(ValidationFinding.VALID)

        # 10. Document status
        if ValidationFinding.VALID in findings and len(findings) == 1:
            doc_status = DocumentStatus.UNDER_REVIEW
        elif request.submission_status.value == "MISSING":
            doc_status = DocumentStatus.MISSING
        elif any(
            f in findings
            for f in [
                ValidationFinding.UNREADABLE,
                ValidationFinding.WRONG_TYPE,
                ValidationFinding.EXPIRED_DOCUMENT,
            ]
        ):
            doc_status = DocumentStatus.REJECTED
        else:
            doc_status = DocumentStatus.RECEIVED

        # 11. Compliance status
        compliance_status = determine_compliance_status(
            findings=findings,
            evidence_status=evidence_status,
            hr_review_status=request.hr_review_status,
            approval_status=ApprovalStatus.NOT_REQUIRED
            if request.hr_review_status == HRReviewStatus.APPROVED
            else ApprovalStatus.PENDING,
        )

        # 12. Visa pack status
        visa_pack_status = VisaPackStatus.NOT_APPLICABLE
        if request.visa_required:
            if request.document_type == "visa_documents":
                if ValidationFinding.MISSING_DOCUMENT in findings:
                    visa_pack_status = VisaPackStatus.OUTSTANDING_ITEMS
                elif compliance_status == ComplianceStatus.COMPLETE:
                    visa_pack_status = VisaPackStatus.READY_FOR_PRO
                else:
                    visa_pack_status = VisaPackStatus.OUTSTANDING_ITEMS

        # 13. Outstanding requirements
        outstanding = self._checklist.get_outstanding(ctx, already_complete=set())

        # 14. Next action and overall status
        next_action = determine_next_action(
            findings=findings,
            compliance_status=compliance_status,
            onboarding_on_hold=on_hold,
            injection_detected=injection_detected,
            access_denied=access_denied,
        )
        overall_status = determine_overall_status(
            onboarding_on_hold=on_hold,
            findings=findings,
            compliance_status=compliance_status,
        )

        # 14b. Draft communication when a reminder or replacement/rejection request is needed
        draft_comm: str | None = None
        should_draft = (
            next_action in (RequiredAction.SEND_REMINDER, RequiredAction.SEND_REPLACEMENT_REQUEST)
            or doc_status == DocumentStatus.REJECTED
        )
        if should_draft:
            action_type = (
                RequiredAction.SEND_REMINDER.value
                if next_action == RequiredAction.SEND_REMINDER
                else RequiredAction.SEND_REPLACEMENT_REQUEST.value
            )
            doc_label = requirement.label if requirement else request.document_type
            outstanding_labels: list[str] = []
            for d in outstanding:
                req = self._checklist.get_requirement(d, ctx)
                outstanding_labels.append(req.label if req is not None else d)

            reasons: list[str] = [
                f.value.replace("_", " ").title()
                for f in findings
                if f not in (ValidationFinding.VALID, ValidationFinding.MISSING_DOCUMENT)
            ]

            draft_outcome = await draft_communication_outcome(
                action=action_type,
                employee_name=request.employee_name_form,
                document_label=doc_label,
                outstanding_labels=outstanding_labels,
                reasons=reasons,
                advisor=self._advisor,
            )
            draft_comm = draft_outcome.text

        # 15. Approval status
        approval_record = await self._case_store.get_approval(
            request.case_id, "COMPLETE_COMPLIANCE"
        )
        approval_status = (
            approval_record.status if approval_record else ApprovalStatus.PENDING
        )

        # 16. Update received_documents on the case for future dedup checks
        if (
            request.submission_status.value == "RECEIVED"
            and ValidationFinding.DUPLICATE_REQUEST not in findings
        ):
            update: dict[str, Any] = {
                "received_documents": case.received_documents | {request.document_type}
            }
            if request.document_revision is not None:
                update["received_revisions"] = {
                    **case.received_revisions, request.document_type: request.document_revision
                }
            updated_case = case.model_copy(update=update)
            await self._case_store.save_case(updated_case)

        # 17. Audit (BR-14) — log document type and findings only, never raw content
        detail: dict[str, Any] = {
            "document_type": request.document_type,
            "findings": [f.value for f in findings],
            "compliance_status": compliance_status.value,
            "overall_status": overall_status.value,
            "injection_detected": injection_detected,
        }
        if self._advisor is not None:  # recorded only when model help is configured, so the default audit is unchanged
            detail["advisor_provider"] = getattr(self._advisor, "provider", None)
            detail["name_check_model_note"] = name_used_llm
            if draft_outcome is not None:
                detail["communication_source"] = draft_outcome.source
                detail["communication_degraded_reason"] = draft_outcome.degraded_reason  # an error class name or None
            # The same non-authoritative facts, kept apart from the decision when stored.
            advisory_keys = ("advisor_provider", "name_check_model_note",
                             "communication_source", "communication_degraded_reason")
            advisory.update({k: detail[k] for k in advisory_keys if k in detail})
        audit_ev = await audit_event(
            store=self._audit_store,
            case_id=request.case_id,
            actor_id=request.requesting_actor_id,
            actor_type=request.requesting_actor_type,
            action="DOCUMENT_VALIDATED",
            detail=detail,
            correlation_id=request.correlation_id,
        )

        # 18. Build result summary
        detail_parts = [f"Document type: {request.document_type}"]
        detail_parts.append(f"Status: {overall_status.value}")
        detail_parts.append(f"Findings: {', '.join(f.value for f in findings)}")
        if injection_detected:
            detail_parts.append("WARNING: Prompt injection content detected in document data.")

        return DocumentValidationResult(
            case_id=request.case_id,
            document_type=request.document_type,
            document_status=doc_status,
            validation_findings=findings,
            outstanding_requirements=outstanding,
            evidence_status=evidence_status,
            hr_decision=request.hr_review_status,
            compliance_item_status=compliance_status,
            visa_pack_status=visa_pack_status,
            required_next_action=next_action,
            approval_status=approval_status,
            audit_reference=audit_ev.event_id,
            detail=" | ".join(detail_parts),
            draft_communication=draft_comm,
            injection_detected=injection_detected,
            extracted_fields=request.extracted_fields,
        )
