from datetime import date
from typing import Optional, Protocol

from pydantic import BaseModel, ConfigDict, SecretStr

from .models import (
    Approval,
    AuditEntry,
    Case,
    Discrepancy,
    DomainEvent,
    HrisRecord,
    HrisSystem,
    OfferRecord,
    OnboardingForm,
    PendingStart,
    PersonRecord,
)


class PersonDirectory(Protocol):
    def find_by_email(self, client_id: str, email: str) -> list[PersonRecord]: ...

    def find_by_id_number(self, client_id: str, id_number: str) -> list[PersonRecord]: ...

    def find_by_name(self, client_id: str, full_name: str) -> list[PersonRecord]: ...

    def add(self, record: PersonRecord) -> None: ...


class AuditLog(Protocol):
    def append(self, entry: AuditEntry) -> None: ...

    def entries(self) -> list[AuditEntry]: ...

    def entries_for_case(self, case_id: str) -> list[AuditEntry]: ...


class CaseRepository(Protocol):
    def save(self, case: Case) -> None: ...

    def get(self, case_id: str) -> Optional[Case]: ...

    def list_for_client(self, client_id: str) -> list[Case]: ...


class FormRepository(Protocol):
    def save(self, form: OnboardingForm) -> None: ...

    def latest_approved(self, case_id: str) -> Optional[OnboardingForm]: ...

    def get(self, case_id: str, version: int) -> Optional[OnboardingForm]: ...

    def list_versions(self, case_id: str) -> list[OnboardingForm]: ...


class ApprovalRepository(Protocol):
    def add(self, approval: Approval) -> None: ...

    def get(self, approval_id: str) -> Optional[Approval]: ...

    def list_for_case(self, case_id: str) -> list[Approval]: ...

    def update(self, approval: Approval) -> None: ...

    def list_pending_for(self, requested_to: str) -> list[Approval]: ...


class DiscrepancyRepository(Protocol):
    def add(self, discrepancy: Discrepancy) -> None: ...

    def get(self, discrepancy_id: str) -> Optional[Discrepancy]: ...

    def list_for_case(self, case_id: str) -> list[Discrepancy]: ...

    def update(self, discrepancy: Discrepancy) -> None: ...


class HrisRecordRepository(Protocol):
    def add(self, record: HrisRecord) -> None: ...

    def get(self, record_id: str) -> Optional[HrisRecord]: ...

    def list_for_case(self, case_id: str) -> list[HrisRecord]: ...

    def update(self, record: HrisRecord) -> None: ...


class DocumentStore(Protocol):
    def create_case_folder(self, client_id: str, case_id: str) -> str: ...


class EmployeeNumberGenerator(Protocol):
    def next(self, client_id: str) -> str: ...


class EventPublisher(Protocol):
    def publish(self, event: DomainEvent) -> None: ...


class HolidayCalendar(Protocol):
    def is_holiday(self, client_id: str, day: date) -> bool: ...


class OfferRepository(Protocol):
    """[Proposed] Dev simulator only; see intake/email_intake.py (OQ-01/Q-11)."""

    def get(self, client_id: str, offer_ref: str) -> Optional[OfferRecord]: ...

    def find_by_ref(self, offer_ref: str) -> list[OfferRecord]: ...

    def save(self, offer: OfferRecord) -> None:
        """Raises ValueError if another client already has this offer_ref:
        [Proposed] offer refs are unique across clients."""


class OnboardingFormSource(Protocol):
    """[Proposed] Dev simulator only; see intake/email_intake.py (OQ-01/Q-11)."""

    def latest_for_offer(self, client_id: str, offer_ref: str) -> Optional[OnboardingForm]: ...

    def save_for_offer(self, client_id: str, offer_ref: str, form: OnboardingForm) -> None: ...


class PendingStartRepository(Protocol):
    """[Proposed] Dev simulator only; see intake/email_intake.py (OQ-01/Q-11)."""

    def add(self, pending: PendingStart) -> None: ...

    def get_waiting(self, client_id: str, offer_ref: str) -> Optional[PendingStart]: ...

    def update(self, pending: PendingStart) -> None: ...

    def list_waiting(self, client_id: str) -> list[PendingStart]: ...


class WriteResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    external_ref: str
    already_existed: bool


class HrisGateway(Protocol):
    def write(
        self,
        client_id: str,
        system: HrisSystem,
        idempotency_key: str,
        payload: dict[str, str],
        sensitive_payload: dict[str, SecretStr],
    ) -> WriteResult: ...

    def update(
        self,
        client_id: str,
        system: HrisSystem,
        idempotency_key: str,
        external_ref: str,
        payload: dict[str, str],
        sensitive_payload: dict[str, SecretStr],
    ) -> WriteResult:
        """Update an employee that already exists in the target system.

        Returns the SAME external_ref it was given: an update must never
        mint a new employee. Used when an approved form change lands
        after the record was written.
        """
        ...
