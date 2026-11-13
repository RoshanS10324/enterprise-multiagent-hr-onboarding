"""Read-only consistency checks over a case's own data. Never changes
anything; callers (e.g. hooks.after_hris_write) decide what to do with the
violations (today: just audit them).
"""

from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, ConfigDict

from .deps import UC01Deps
from .models import ApprovalStatus, FormStatus, HrisRecordStatus, HrisSystem
from .policy import Policy


class InvariantCode(StrEnum):
    MULTIPLE_APPROVED_FORMS = "MULTIPLE_APPROVED_FORMS"
    MULTIPLE_PENDING_FORMS = "MULTIPLE_PENDING_FORMS"
    CASE_FORM_MISMATCH = "CASE_FORM_MISMATCH"
    WRITTEN_WITHOUT_APPROVAL = "WRITTEN_WITHOUT_APPROVAL"
    PAYROLL_WITHOUT_PEOPLE = "PAYROLL_WITHOUT_PEOPLE"
    DUPLICATE_WRITTEN_SYSTEM = "DUPLICATE_WRITTEN_SYSTEM"
    SELF_APPROVED = "SELF_APPROVED"
    DECISION_INCOMPLETE = "DECISION_INCOMPLETE"
    MISSING_CASE_OPENED_AUDIT = "MISSING_CASE_OPENED_AUDIT"
    SENSITIVE_IN_AUDIT = "SENSITIVE_IN_AUDIT"


class Violation(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: InvariantCode
    case_id: str
    # Short text only: field names, versions, ids. Never a value (salary,
    # email, name, ...).
    detail: str


def check_case_invariants(
    case_id: str, deps: UC01Deps, policy: Optional[Policy] = None
) -> list[Violation]:
    violations: list[Violation] = []

    forms = deps.form_repo.list_versions(case_id)
    case = deps.case_repo.get(case_id)
    in_force = deps.form_repo.latest_approved(case_id)
    hris_records = deps.hris_record_repo.list_for_case(case_id)
    approvals = deps.approval_repo.list_for_case(case_id)
    entries = deps.audit_log.entries_for_case(case_id)

    approved_forms = [f for f in forms if f.status == FormStatus.APPROVED]
    if len(approved_forms) > 1:
        violations.append(
            Violation(
                code=InvariantCode.MULTIPLE_APPROVED_FORMS,
                case_id=case_id,
                detail=f"count={len(approved_forms)}",
            )
        )

    pending_forms = [f for f in forms if f.status == FormStatus.AWAITING_APPROVAL]
    if len(pending_forms) > 1:
        violations.append(
            Violation(
                code=InvariantCode.MULTIPLE_PENDING_FORMS,
                case_id=case_id,
                detail=f"count={len(pending_forms)}",
            )
        )

    if case is not None and in_force is not None:
        mismatched_fields = []
        if case.current_form_version != in_force.version:
            mismatched_fields.append("current_form_version")
        if case.joining_date != in_force.joining_date:
            mismatched_fields.append("joining_date")
        if case.work_location_type != in_force.work_location_type:
            mismatched_fields.append("work_location_type")
        if case.candidate_name != in_force.candidate_name:
            mismatched_fields.append("candidate_name")
        if mismatched_fields:
            violations.append(
                Violation(
                    code=InvariantCode.CASE_FORM_MISMATCH,
                    case_id=case_id,
                    detail=f"fields={mismatched_fields}",
                )
            )

    written_records = [r for r in hris_records if r.status == HrisRecordStatus.WRITTEN]
    for record in written_records:
        subject_ref = f"hris:{case_id}:v{record.form_version}"
        matching_approval = next((a for a in approvals if a.subject_ref == subject_ref), None)
        if matching_approval is None or matching_approval.status != ApprovalStatus.APPROVED:
            violations.append(
                Violation(
                    code=InvariantCode.WRITTEN_WITHOUT_APPROVAL,
                    case_id=case_id,
                    detail=f"system={record.system.value},form_version={record.form_version}",
                )
            )

    versions = {r.form_version for r in hris_records}
    for version in versions:
        version_records = [r for r in hris_records if r.form_version == version]
        payroll = next((r for r in version_records if r.system == HrisSystem.ZOHO_PAYROLL), None)
        people = next((r for r in version_records if r.system == HrisSystem.ZOHO_PEOPLE), None)
        if payroll is not None and payroll.status == HrisRecordStatus.WRITTEN:
            if people is None or people.status != HrisRecordStatus.WRITTEN:
                violations.append(
                    Violation(
                        code=InvariantCode.PAYROLL_WITHOUT_PEOPLE,
                        case_id=case_id,
                        detail=f"form_version={version}",
                    )
                )

    for system in HrisSystem:
        written_for_system = [
            r for r in hris_records if r.system == system and r.status == HrisRecordStatus.WRITTEN
        ]
        if len(written_for_system) > 1:
            violations.append(
                Violation(
                    code=InvariantCode.DUPLICATE_WRITTEN_SYSTEM,
                    case_id=case_id,
                    detail=f"system={system.value},count={len(written_for_system)}",
                )
            )

    if policy is not None:
        for approval in approvals:
            if approval.status not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
                continue
            rule = policy.decisions.get(approval.checkpoint)
            if rule is None or not rule.separation_of_duties:
                continue
            if approval.requested_by is not None and approval.decided_by == approval.requested_by:
                violations.append(
                    Violation(
                        code=InvariantCode.SELF_APPROVED,
                        case_id=case_id,
                        detail=f"checkpoint={approval.checkpoint.value}",
                    )
                )

    for approval in approvals:
        if approval.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            if approval.decided_by is None or approval.decided_at is None:
                violations.append(
                    Violation(
                        code=InvariantCode.DECISION_INCOMPLETE,
                        case_id=case_id,
                        detail=f"checkpoint={approval.checkpoint.value}",
                    )
                )

    if not any(e.action == "CASE_OPENED" for e in entries):
        violations.append(
            Violation(
                code=InvariantCode.MISSING_CASE_OPENED_AUDIT,
                case_id=case_id,
                detail="action=CASE_OPENED",
            )
        )

    sensitive_texts: set[str] = set()
    for form in forms:
        salary_text = form.salary_and_terms.get_secret_value()
        if salary_text:
            sensitive_texts.add(salary_text)
        if form.candidate_email:
            sensitive_texts.add(form.candidate_email)
    if case is not None and case.candidate_email:
        sensitive_texts.add(case.candidate_email)

    if sensitive_texts:
        for entry in entries:
            dumped = str(entry.model_dump())
            if any(text in dumped for text in sensitive_texts):
                violations.append(
                    Violation(
                        code=InvariantCode.SENSITIVE_IN_AUDIT,
                        case_id=case_id,
                        detail=f"log_id={entry.log_id}",
                    )
                )

    return violations
