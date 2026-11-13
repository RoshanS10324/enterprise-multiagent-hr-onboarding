from typing import Optional

from .duplicate_check import DuplicateCheckResult, DuplicateOutcome
from .llm import LlmClient, LlmError, LlmRequest, audit_action_for, draft_fingerprint
from .models import (
    ActorType,
    Approval,
    AuditEntry,
    Case,
    CasePlacement,
    CaseStatus,
    Checkpoint,
    FormStatus,
    OnboardingForm,
    PersonRecord,
    PersonRecordKind,
    VariantFlags,
)
from .models.common import new_id
from .ports import (
    ApprovalRepository,
    AuditLog,
    CaseRepository,
    DocumentStore,
    EmployeeNumberGenerator,
    FormRepository,
    PersonDirectory,
)


def _or_variants(a: VariantFlags, b: VariantFlags) -> VariantFlags:
    fields = VariantFlags.model_fields
    return VariantFlags(**{name: getattr(a, name) or getattr(b, name) for name in fields})


def _draft_variant_explanation(
    case: Case,
    subject_ref: str,
    duplicate_result: DuplicateCheckResult,
    llm: Optional[LlmClient],
    audit_log: AuditLog,
) -> None:
    """Draft an explanation of the match for the HR manager deciding
    HC_12_VARIANT. DRAFT_FOR_HUMAN only: the match itself was computed
    deterministically and is not changed here.

    Which signals matched, not what they matched on -- no email address
    or name is sent to the model.
    """
    if llm is None:
        return

    try:
        response = llm.complete(
            LlmRequest(
                task="duplicate_explanation",
                inputs={
                    "case_id": case.case_id,
                    "matched_on": "email or id number",
                    "match_kind": duplicate_result.outcome.value,
                    "existing_record_kind": subject_ref,
                    "variant_flags": ", ".join(
                        name for name, on in case.variants.model_dump().items() if on
                    )
                    or "none",
                },
            )
        )
    except LlmError as exc:
        # An outage or refusal must never stop a variant reaching HR.
        audit_log.append(
            AuditEntry(
                case_id=case.case_id,
                actor_type=ActorType.SYSTEM,
                actor_id="uc01-open-case",
                action=audit_action_for(exc),
                after={"task": "duplicate_explanation"},
                reference=case.case_id,
            )
        )
        return

    audit_log.append(
        AuditEntry(
            case_id=case.case_id,
            actor_type=ActorType.AGENT,
            actor_id="uc01-open-case",
            action="VARIANT_EXPLANATION_DRAFTED",
            after={
                "task": "duplicate_explanation",
                "task_version": response.task_version,
                "model": response.model,
                "subject_ref": subject_ref,
                # Provenance, not content: the model's text is never
                # written to the audit log (guardrails llm.audited_without_content).
                **draft_fingerprint(response.text),
            },
            reference=case.case_id,
        )
    )


def create_case(
    form: OnboardingForm,
    hr_officer_id: str,
    id_number: Optional[str],
    duplicate_result: DuplicateCheckResult,
    declared_variants: Optional[VariantFlags],
    placement: CasePlacement,
    case_repo: CaseRepository,
    form_repo: FormRepository,
    approval_repo: ApprovalRepository,
    person_directory: PersonDirectory,
    folder_store: DocumentStore,
    number_generator: EmployeeNumberGenerator,
    audit_log: AuditLog,
    llm: Optional[LlmClient] = None,
) -> tuple[Case, list[str]]:
    if form.status != FormStatus.APPROVED:
        raise ValueError(f"cannot open a case from a form that is not APPROVED (got {form.status})")
    if form.version != 1:
        raise ValueError(
            f"cannot open a new case from form version {form.version}; a new case requires version 1"
        )

    declared = declared_variants or VariantFlags()
    variants = _or_variants(duplicate_result.variants, declared)

    case_id = new_id()
    employee_no = number_generator.next(form.client_id)
    folder_ref = folder_store.create_case_folder(form.client_id, case_id)

    case = Case(
        case_id=case_id,
        client_id=form.client_id,
        employee_no=employee_no,
        candidate_name=form.candidate_name,
        candidate_email=form.candidate_email,
        hr_officer_id=hr_officer_id,
        joining_date=form.joining_date,
        work_location_type=form.work_location_type,
        variants=variants,
        status=CaseStatus.PLAN_PENDING,
        current_form_version=form.version,
        folder_ref=folder_ref,
        reactivates_record_id=duplicate_result.reactivation_record_id,
        reactivates_external_ref=duplicate_result.reactivation_external_ref,
        **placement.model_dump(),
    )
    case_repo.save(case)

    linked_form = form.with_case(case.case_id)
    form_repo.save(linked_form)

    person_directory.add(
        PersonRecord(
            client_id=form.client_id,
            email=form.candidate_email,
            id_number=id_number,
            full_name=form.candidate_name,
            kind=PersonRecordKind.OPEN_CASE,
            case_id=case.case_id,
        )
    )

    audit_log.append(
        AuditEntry(
            case_id=case.case_id,
            actor_type=ActorType.AGENT,
            actor_id="uc01-open-case",
            action="CASE_OPENED",
            after={
                "case_id": case.case_id,
                "employee_no": case.employee_no,
                "status": case.status.value,
                "form_version": form.version,
                "variants": variants.model_dump(),
            },
            reference=case.case_id,
        )
    )
    audit_log.append(
        AuditEntry(
            case_id=case.case_id,
            actor_type=ActorType.AGENT,
            actor_id="uc01-open-case",
            action="FORM_LINKED",
            before={"case_id": None},
            after={"case_id": case.case_id},
            reference=case.case_id,
        )
    )

    approvals_created: list[str] = []

    variant_subjects: list[str] = []
    if duplicate_result.outcome == DuplicateOutcome.REHIRE:
        variant_subjects.append("is_rehire")
    if duplicate_result.outcome == DuplicateOutcome.INTERNAL_TRANSFER:
        variant_subjects.append("is_internal_transfer")
    if declared.is_minor:
        variant_subjects.append("is_minor")
    if declared.is_agency_contractor:
        variant_subjects.append("is_agency_contractor")

    for subject_ref in variant_subjects:
        # For a rehire or internal transfer, the decision HR is being
        # asked to make is explicitly "reactivate this existing employee"
        # (EC-43), not just "is this a rehire".
        reason = None
        if subject_ref in ("is_rehire", "is_internal_transfer"):
            if case.reactivates_record_id:
                reason = (
                    f"proposes reactivating existing person record "
                    f"{case.reactivates_record_id} rather than creating a second employee"
                )
            elif duplicate_result.reactivation_ambiguous:
                # Several real employees matched. Naming one would be an
                # arbitrary pick, so HR is told what matched and asked to
                # choose; nothing is proposed automatically.
                reason = (
                    f"several existing records match "
                    f"({duplicate_result.matched_record_ids}); no reactivation is "
                    f"proposed automatically - HR must choose which, if any, to reactivate"
                )
        approval = Approval(
            case_id=case.case_id,
            checkpoint=Checkpoint.HC_12_VARIANT,
            subject_ref=subject_ref,
            requested_to=hr_officer_id,
            reason=reason,
        )
        approval_repo.add(approval)
        approvals_created.append(approval.approval_id)
        audit_log.append(
            AuditEntry(
                case_id=case.case_id,
                actor_type=ActorType.AGENT,
                actor_id="uc01-open-case",
                action="APPROVAL_REQUESTED",
                after={
                    "approval_id": approval.approval_id,
                    "checkpoint": approval.checkpoint.value,
                    "subject_ref": subject_ref,
                    "reactivates_record_id": case.reactivates_record_id,
                },
                reference=case.case_id,
            )
        )
        _draft_variant_explanation(case, subject_ref, duplicate_result, llm, audit_log)

    if duplicate_result.outcome == DuplicateOutcome.POSSIBLE_DUPLICATE_NEEDS_HR:
        # Same-name-only match is an HR notice, not a block (EC-39). Confirm
        # with process SME.
        approval = Approval(
            case_id=case.case_id,
            checkpoint=Checkpoint.HC_12_VARIANT,
            subject_ref="possible_duplicate",
            requested_to=hr_officer_id,
            reason=f"matched_record_ids={duplicate_result.matched_record_ids}",
        )
        approval_repo.add(approval)
        approvals_created.append(approval.approval_id)
        audit_log.append(
            AuditEntry(
                case_id=case.case_id,
                actor_type=ActorType.AGENT,
                actor_id="uc01-open-case",
                action="APPROVAL_REQUESTED",
                after={
                    "approval_id": approval.approval_id,
                    "checkpoint": approval.checkpoint.value,
                    "subject_ref": "possible_duplicate",
                    "matched_record_ids": duplicate_result.matched_record_ids,
                },
                reference=case.case_id,
            )
        )
        _draft_variant_explanation(
            case, "possible_duplicate", duplicate_result, llm, audit_log
        )

    return case, approvals_created
