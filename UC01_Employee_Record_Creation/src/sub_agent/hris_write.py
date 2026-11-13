from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .errors import HrisUnavailableError
from .deps import UC01Deps
from .hooks import after_hris_write, before_hris_write
from .hris_mapping import build_sensitive_payload
from .models import (
    ActorType,
    Approval,
    ApprovalStatus,
    AuditEntry,
    CaseStatus,
    Checkpoint,
    EmployeeRecordCreatedEvent,
    HrisRecord,
    HrisRecordStatus,
    HrisSystem,
)
from .models.common import utc_now

# People before payroll: payroll must never be written without the people
# record already in place.
WRITE_ORDER: dict[HrisSystem, int] = {
    HrisSystem.ZOHO_PEOPLE: 0,
    HrisSystem.ZOHO_PAYROLL: 1,
}


class WriteOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    written: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    skipped_already_written: list[str] = Field(default_factory=list)
    completed: bool
    blocked_code: Optional[str] = None


def _require_hris_approval(approval_id: str, deps: UC01Deps) -> Approval:
    approval = deps.approval_repo.get(approval_id)
    if approval is None:
        raise ValueError(f"no approval with id {approval_id!r}")
    if approval.status != ApprovalStatus.PENDING:
        raise ValueError(f"approval {approval_id!r} is not PENDING (status {approval.status})")
    if approval.checkpoint != Checkpoint.HC_05_HRIS_WRITE:
        raise ValueError(f"approval {approval_id!r} is not an HC_05_HRIS_WRITE approval")
    if not approval.subject_ref.startswith("hris:"):
        raise ValueError(f"approval {approval_id!r} has an unexpected subject_ref")
    return approval


def _parse_subject_ref(subject_ref: str) -> tuple[str, int]:
    # "hris:{case_id}:v{version}"
    _, case_id, version_part = subject_ref.split(":")
    return case_id, int(version_part[1:])


def _records_for_batch(approval: Approval, deps: UC01Deps) -> tuple[str, int, list[HrisRecord]]:
    case_id, version = _parse_subject_ref(approval.subject_ref)
    records = [
        r for r in deps.hris_record_repo.list_for_case(case_id) if r.form_version == version
    ]
    return case_id, version, records


def approve_hris_write(approval_id: str, decided_by: str, deps: UC01Deps) -> list[HrisRecord]:
    approval = _require_hris_approval(approval_id, deps)
    case_id, version, records = _records_for_batch(approval, deps)

    in_force = deps.form_repo.latest_approved(case_id)
    if in_force is None or in_force.version != version:
        raise ValueError(
            f"approval {approval_id!r} is stale: form version {version} is no longer in force"
        )

    decided_at = utc_now()

    approval.status = ApprovalStatus.APPROVED
    approval.decided_by = decided_by
    approval.decided_at = decided_at
    deps.approval_repo.update(approval)

    for record in records:
        record.status = HrisRecordStatus.APPROVED
        record.approved_by = decided_by
        record.approved_at = decided_at
        deps.hris_record_repo.update(record)

    deps.audit_log.append(
        AuditEntry(
            case_id=case_id,
            actor_type=ActorType.HUMAN,
            actor_id=decided_by,
            action="HRIS_WRITE_APPROVED",
            after={"form_version": version, "record_count": len(records)},
            reference=case_id,
        )
    )

    return records


def reject_hris_write(
    approval_id: str, decided_by: str, note: str, deps: UC01Deps
) -> list[HrisRecord]:
    approval = _require_hris_approval(approval_id, deps)
    case_id, version, records = _records_for_batch(approval, deps)

    decided_at = utc_now()
    approval.status = ApprovalStatus.REJECTED
    approval.decided_by = decided_by
    approval.decided_at = decided_at
    approval.decision_note = note
    deps.approval_repo.update(approval)

    for record in records:
        record.status = HrisRecordStatus.REJECTED
        deps.hris_record_repo.update(record)

    deps.audit_log.append(
        AuditEntry(
            case_id=case_id,
            actor_type=ActorType.HUMAN,
            actor_id=decided_by,
            action="HRIS_WRITE_REJECTED",
            after={"form_version": version, "record_count": len(records)},
            reference=case_id,
        )
    )

    return records


def _retire_superseded(case_id: str, record: HrisRecord, deps: UC01Deps) -> None:
    """Mark the older WRITTEN record for this system CORRECTED.

    The employee in the target system is the same one - it was updated in
    place - so leaving both rows WRITTEN would trip the
    DUPLICATE_WRITTEN_SYSTEM invariant and misreport two employees.
    """
    for older in deps.hris_record_repo.list_for_case(case_id):
        if (
            older.record_id != record.record_id
            and older.system == record.system
            and older.status == HrisRecordStatus.WRITTEN
            and older.form_version < record.form_version
        ):
            older.status = HrisRecordStatus.CORRECTED
            deps.hris_record_repo.update(older)
            deps.audit_log.append(
                AuditEntry(
                    case_id=case_id,
                    actor_type=ActorType.AGENT,
                    actor_id="uc01-hris-write",
                    action="HRIS_RECORD_SUPERSEDED",
                    after={
                        "system": older.system.value,
                        "record_id": older.record_id,
                        "superseded_by": record.record_id,
                        "from_version": older.form_version,
                        "to_version": record.form_version,
                    },
                    reference=case_id,
                )
            )


def execute_hris_write(case_id: str, deps: UC01Deps) -> WriteOutcome:
    case = deps.case_repo.get(case_id)
    if case is None:
        raise ValueError(f"no case with id {case_id!r}")
    if case.status in (CaseStatus.ON_HOLD, CaseStatus.WITHDRAWN, CaseStatus.CLOSED):
        raise ValueError(f"case {case_id!r} is not active (status {case.status})")

    form = deps.form_repo.latest_approved(case_id)
    if form is None:
        raise ValueError(f"no approved form in force for case {case_id!r}")

    records = [
        r for r in deps.hris_record_repo.list_for_case(case_id) if r.form_version == form.version
    ]
    if not records:
        raise ValueError(
            f"no HRIS records prepared for case {case_id!r} at form version {form.version}"
        )
    if any(r.status == HrisRecordStatus.PREPARED for r in records):
        raise ValueError(f"HRIS records for case {case_id!r} are not yet approved for write")

    writable = any(
        r.status in (HrisRecordStatus.APPROVED, HrisRecordStatus.FAILED) for r in records
    )
    already_written = any(r.status == HrisRecordStatus.WRITTEN for r in records)
    if not writable and not already_written:
        raise ValueError(f"no approved HRIS records to write for case {case_id!r}")

    if all(r.status == HrisRecordStatus.WRITTEN for r in records):
        ids = [r.record_id for r in records]
        deps.audit_log.append(
            AuditEntry(
                case_id=case_id,
                actor_type=ActorType.AGENT,
                actor_id="uc01-hris-write",
                action="HRIS_WRITE_SKIPPED_ALREADY_WRITTEN",
                after={"record_count": len(ids)},
                reference=case_id,
            )
        )
        return WriteOutcome(skipped_already_written=ids, completed=True)

    records_sorted = sorted(records, key=lambda r: WRITE_ORDER.get(r.system, 99))

    batch_approval = next(
        (
            a
            for a in deps.approval_repo.list_for_case(case_id)
            if a.subject_ref == f"hris:{case_id}:v{form.version}"
        ),
        None,
    )

    written_ids: list[str] = []
    failed_ids: list[str] = []
    skipped_ids: list[str] = []

    for record in records_sorted:
        if record.status == HrisRecordStatus.WRITTEN:
            skipped_ids.append(record.record_id)
            continue
        if record.status not in (HrisRecordStatus.APPROVED, HrisRecordStatus.FAILED):
            continue

        if record.system == HrisSystem.ZOHO_PAYROLL:
            people_record = next((r for r in records if r.system == HrisSystem.ZOHO_PEOPLE), None)
            people_ok = people_record is not None and (
                people_record.status == HrisRecordStatus.WRITTEN
                or people_record.record_id in written_ids
            )
            if not people_ok:
                # Payroll must never be written without the people record.
                continue

        source_form = deps.form_repo.get(case_id, record.form_version)
        if source_form is None or source_form.version != form.version:
            raise ValueError(
                f"cannot write record {record.record_id!r}: form version "
                f"{record.form_version} is not the version in force"
            )

        block_code = before_hris_write(record, form, batch_approval, deps)
        if block_code is not None:
            deps.audit_log.append(
                AuditEntry(
                    case_id=case_id,
                    actor_type=ActorType.AGENT,
                    actor_id="uc01-hris-write",
                    action="HRIS_WRITE_BLOCKED_BY_HOOK",
                    after={
                        "code": block_code,
                        "system": record.system.value,
                        "record_id": record.record_id,
                    },
                    reference=case_id,
                )
            )
            return WriteOutcome(
                written=written_ids,
                failed=failed_ids,
                skipped_already_written=skipped_ids,
                completed=False,
                blocked_code=block_code,
            )

        sensitive_payload = build_sensitive_payload(record.system, source_form)

        try:
            if record.updates_external_ref:
                # An approved form change landed after this employee was
                # written: update in place so the target system ends up
                # with one employee, not two.
                result = deps.hris_gateway.update(
                    client_id=case.client_id,
                    system=record.system,
                    idempotency_key=record.idempotency_key,
                    external_ref=record.updates_external_ref,
                    payload=record.prepared_payload,
                    sensitive_payload=sensitive_payload,
                )
            else:
                result = deps.hris_gateway.write(
                    client_id=case.client_id,
                    system=record.system,
                    idempotency_key=record.idempotency_key,
                    payload=record.prepared_payload,
                    sensitive_payload=sensitive_payload,
                )
            record.status = HrisRecordStatus.WRITTEN
            record.external_ref = result.external_ref
            record.last_error = None
            deps.hris_record_repo.update(record)
            if record.updates_external_ref:
                _retire_superseded(case_id, record, deps)
            written_ids.append(record.record_id)
            deps.audit_log.append(
                AuditEntry(
                    case_id=case_id,
                    actor_type=ActorType.AGENT,
                    actor_id="uc01-hris-write",
                    action="HRIS_WRITE_SUCCEEDED",
                    after={"system": record.system.value, "record_id": record.record_id},
                    reference=case_id,
                )
            )
        except HrisUnavailableError as exc:
            record.status = HrisRecordStatus.FAILED
            record.last_error = type(exc).__name__
            deps.hris_record_repo.update(record)
            failed_ids.append(record.record_id)
            deps.audit_log.append(
                AuditEntry(
                    case_id=case_id,
                    actor_type=ActorType.AGENT,
                    actor_id="uc01-hris-write",
                    action="HRIS_WRITE_FAILED",
                    after={"system": record.system.value, "record_id": record.record_id},
                    reference=case_id,
                )
            )
            if record.system == HrisSystem.ZOHO_PEOPLE:
                # Don't attempt payroll this call if people just failed.
                break

    final_records = [deps.hris_record_repo.get(r.record_id) for r in records]
    completed = all(r.status == HrisRecordStatus.WRITTEN for r in final_records)

    if completed:
        event = EmployeeRecordCreatedEvent(
            case_id=case_id,
            client_id=case.client_id,
            employee_no=case.employee_no,
            form_version=form.version,
        )
        deps.publisher.publish(event)

    if written_ids:
        after_hris_write(case_id, deps, deps.policy)

    return WriteOutcome(
        written=written_ids,
        failed=failed_ids,
        skipped_already_written=skipped_ids,
        completed=completed,
    )
