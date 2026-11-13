"""Offer-acceptance email intake (UC steps 1-2): parse the trigger event, retrieve full employee
details from the employee directory (the future real HRMS), validate completeness, and create the
onboarding case -- idempotently, so the same event processed twice never creates a second case.

Required-field validation and duplicate-event protection are both plain code: nothing here calls a
model, and no employee detail is ever invented when the directory's record is incomplete.
"""

from __future__ import annotations

from hr_onboarding_common.platform.audit import AuditLog
from connectors.email.base import EmailPort
from connectors.employee_directory.base import EmployeeDirectoryPort
from sub_agent.models import DuplicateOnboardingCaseError, OnboardingRecord, TenantMismatchError
from sub_agent.repository import OnboardingPort

AGENT_ID = "employee-onboarding"
REQUIRED_DIRECTORY_FIELDS = ("email", "department", "team", "team_lead_id")


class MalformedOfferAcceptanceEmailError(ValueError):
    pass


class IncompleteEmployeeDataError(ValueError):
    pass


def process_offer_acceptance_event(port: OnboardingPort, email_port: EmailPort, directory: EmployeeDirectoryPort,
                                   audit: AuditLog, *, event_id: str,
                                   expected_client_id: str | None = None) -> OnboardingRecord:
    """Idempotent: a second call with the same event_id returns the existing case rather than
    raising or creating a duplicate. The event is marked processed only after the case exists (or
    is confirmed to already exist), so a crash mid-way leaves the event safely reprocessable.

    The tenant check applies to a redelivered event too: an already-processed event of another
    client is refused exactly like an unprocessed one, never answered with that client's case."""
    if email_port.is_processed(event_id):
        existing = email_port.get_event(event_id)
        if existing.employee_id is None:  # never marked processed today; refuse rather than guess
            raise MalformedOfferAcceptanceEmailError(f"offer-acceptance email {event_id} has no employee reference")
        record = port.get_onboarding_record(existing.employee_id)
        if expected_client_id is not None and record.client_id != expected_client_id:
            audit.record("intake.tenant_mismatch", agent=AGENT_ID, event_id=event_id, subject_id=existing.employee_id)
            raise TenantMismatchError(f"offer-acceptance event {event_id} is not accessible to this client")
        audit.record("intake.duplicate_event_skipped", agent=AGENT_ID, event_id=event_id)
        return record

    event = email_port.get_event(event_id)  # EmailServiceError propagates untouched: no data, no case
    if event.employee_id is None:
        audit.record("intake.malformed_email", agent=AGENT_ID, event_id=event_id)
        raise MalformedOfferAcceptanceEmailError(
            f"offer-acceptance email {event_id} could not be parsed into an employee reference")

    directory_record = directory.get_employee(event.employee_id)  # KeyError if unknown

    if expected_client_id is not None and directory_record.client_id != expected_client_id:
        audit.record("intake.tenant_mismatch", agent=AGENT_ID, event_id=event_id, subject_id=event.employee_id)
        raise TenantMismatchError(f"offer-acceptance event {event_id} is not accessible to this client")

    missing = [f for f in REQUIRED_DIRECTORY_FIELDS if not getattr(directory_record, f)]
    if missing:
        audit.record("intake.flagged_missing_data", agent=AGENT_ID, event_id=event_id,
                     subject_id=event.employee_id, missing_fields=missing)
        raise IncompleteEmployeeDataError(f"{event.employee_id} is missing required field(s) "
                                          f"{', '.join(missing)}; onboarding case was not created")

    try:
        case = port.create_onboarding_case(OnboardingRecord(
            employee_id=directory_record.employee_id, full_name=directory_record.full_name,
            email=directory_record.email, role_title=directory_record.designation,
            department=directory_record.department, team=directory_record.team,
            team_lead_id=directory_record.team_lead_id, client_id=directory_record.client_id,
            start_date=directory_record.joining_date))
    except DuplicateOnboardingCaseError:
        audit.record("intake.duplicate_case_skipped", agent=AGENT_ID, event_id=event_id,
                     subject_id=event.employee_id)
        email_port.mark_processed(event_id)
        return port.get_onboarding_record(event.employee_id)

    email_port.mark_processed(event_id)
    audit.record("intake.case_created", agent=AGENT_ID, event_id=event_id, subject_id=case.employee_id,
                 client_id=case.client_id)
    return case
