from datetime import date
from typing import Optional

from pydantic import SecretStr

from ..audit_integrity import GENESIS_HASH, chain_entry, check_entry
from ..errors import HrisUnavailableError
from ..models import (
    Approval,
    ApprovalStatus,
    AuditEntry,
    Case,
    Discrepancy,
    DomainEvent,
    FormStatus,
    HrisRecord,
    HrisSystem,
    OfferRecord,
    OnboardingForm,
    PendingStart,
    PendingStartStatus,
    PersonRecord,
)
from ..models.common import new_id
from ..ports import WriteResult


class InMemoryPersonDirectory:
    """Test/dev double for the PersonDirectory port. Not for production use."""

    def __init__(self, records: list[PersonRecord] | None = None) -> None:
        self._records = list(records) if records else []

    def add(self, record: PersonRecord) -> None:
        self._records.append(record)

    def find_by_email(self, client_id: str, email: str) -> list[PersonRecord]:
        target = email.strip().lower()
        return [
            r
            for r in self._records
            if r.client_id == client_id and r.email.strip().lower() == target
        ]

    def find_by_id_number(self, client_id: str, id_number: str) -> list[PersonRecord]:
        target = id_number.strip()
        return [
            r
            for r in self._records
            if r.client_id == client_id
            and r.id_number is not None
            and r.id_number.strip() == target
        ]

    def find_by_name(self, client_id: str, full_name: str) -> list[PersonRecord]:
        target = full_name.strip().lower()
        return [
            r
            for r in self._records
            if r.client_id == client_id and r.full_name.strip().lower() == target
        ]


class InMemoryAuditLog:
    """Test/dev double for the AuditLog port. Not for production use.

    Append does two things before storing an entry: it refuses one that
    carries a sensitive key or a secret value, and it links the entry
    into a hash chain so a later edit, removal or reordering is
    detectable. See audit_integrity for what that chain does and does
    not guarantee.
    """

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def append(self, entry: AuditEntry) -> None:
        # Fail closed: a salary in the audit log is a guardrail breach
        # (guardrails/policy.yaml, sensitive_fields.never_in_audit), not
        # something to record and notice later.
        check_entry(entry)
        prev_hash = self._entries[-1].entry_hash if self._entries else GENESIS_HASH
        self._entries.append(chain_entry(entry, len(self._entries), prev_hash or GENESIS_HASH))

    def entries(self) -> list[AuditEntry]:
        return list(self._entries)

    def entries_for_case(self, case_id: str) -> list[AuditEntry]:
        # Append order is preserved since self._entries is only ever
        # appended to, never reordered.
        return [e for e in self._entries if e.case_id == case_id]


class InMemoryCaseRepository:
    """Test/dev double for the CaseRepository port. Not for production use."""

    def __init__(self) -> None:
        self._cases: dict[str, Case] = {}

    def save(self, case: Case) -> None:
        self._cases[case.case_id] = case

    def get(self, case_id: str) -> Optional[Case]:
        return self._cases.get(case_id)

    def list_for_client(self, client_id: str) -> list[Case]:
        return [c for c in self._cases.values() if c.client_id == client_id]

    def case_ids_for_email(self, client_id: str, email: str) -> list[tuple[str, str]]:
        wanted = email.strip().lower()
        return [(c.case_id, c.status.value) for c in self._cases.values()
                if c.client_id == client_id and c.candidate_email.strip().lower() == wanted]


class InMemoryFormRepository:
    """Test/dev double for the FormRepository port. Not for production use."""

    def __init__(self) -> None:
        self._forms: dict[str, OnboardingForm] = {}

    def save(self, form: OnboardingForm) -> None:
        self._forms[form.form_id] = form

    def latest_approved(self, case_id: str) -> Optional[OnboardingForm]:
        # status == APPROVED already excludes SUPERSEDED (a distinct status
        # value), DRAFT, AWAITING_APPROVAL and REJECTED, so the highest
        # version among these is the highest *approved and not superseded*
        # version by construction.
        candidates = [
            f
            for f in self._forms.values()
            if f.case_id == case_id and f.status == FormStatus.APPROVED
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda f: f.version)

    def get(self, case_id: str, version: int) -> Optional[OnboardingForm]:
        for f in self._forms.values():
            if f.case_id == case_id and f.version == version:
                return f
        return None

    def list_versions(self, case_id: str) -> list[OnboardingForm]:
        return sorted(
            (f for f in self._forms.values() if f.case_id == case_id),
            key=lambda f: f.version,
        )


class InMemoryApprovalRepository:
    """Test/dev double for the ApprovalRepository port. Not for production use."""

    def __init__(self) -> None:
        self._approvals: list[Approval] = []

    def add(self, approval: Approval) -> None:
        self._approvals.append(approval)

    def get(self, approval_id: str) -> Optional[Approval]:
        return next((a for a in self._approvals if a.approval_id == approval_id), None)

    def list_for_case(self, case_id: str) -> list[Approval]:
        return [a for a in self._approvals if a.case_id == case_id]

    def update(self, approval: Approval) -> None:
        for i, existing in enumerate(self._approvals):
            if existing.approval_id == approval.approval_id:
                self._approvals[i] = approval
                return
        raise KeyError(f"no approval with id {approval.approval_id!r} to update")

    def list_pending_for(self, requested_to: str) -> list[Approval]:
        return [
            a
            for a in self._approvals
            if a.status == ApprovalStatus.PENDING and a.requested_to == requested_to
        ]


class InMemoryDiscrepancyRepository:
    """Test/dev double for the DiscrepancyRepository port. Not for production use."""

    def __init__(self) -> None:
        self._discrepancies: dict[str, Discrepancy] = {}

    def add(self, discrepancy: Discrepancy) -> None:
        self._discrepancies[discrepancy.discrepancy_id] = discrepancy

    def get(self, discrepancy_id: str) -> Optional[Discrepancy]:
        return self._discrepancies.get(discrepancy_id)

    def list_for_case(self, case_id: str) -> list[Discrepancy]:
        return [d for d in self._discrepancies.values() if d.case_id == case_id]

    def update(self, discrepancy: Discrepancy) -> None:
        if discrepancy.discrepancy_id not in self._discrepancies:
            raise KeyError(f"no discrepancy with id {discrepancy.discrepancy_id!r} to update")
        self._discrepancies[discrepancy.discrepancy_id] = discrepancy


class InMemoryDocumentStore:
    """Test/dev double for the DocumentStore port. Not for production use."""

    def __init__(self) -> None:
        self._folders: dict[str, str] = {}

    def create_case_folder(self, client_id: str, case_id: str) -> str:
        folder_ref = f"/cases/{client_id}/{case_id}"
        self._folders[case_id] = folder_ref
        return folder_ref


class InMemoryEmployeeNumberGenerator:
    """Test/dev double for the EmployeeNumberGenerator port. Not for production use.

    Placeholder format; real rules are open question Q-21.
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def next(self, client_id: str) -> str:
        count = self._counters.get(client_id, 0) + 1
        self._counters[client_id] = count
        return f"EMP-{count:06d}"


class InMemoryEventPublisher:
    """Test/dev double for the EventPublisher port. Not for production use."""

    def __init__(self) -> None:
        self._events: list[DomainEvent] = []

    def publish(self, event: DomainEvent) -> None:
        self._events.append(event)

    @property
    def events(self) -> list[DomainEvent]:
        return list(self._events)


class InMemoryHolidayCalendar:
    """Test/dev double for the HolidayCalendar port. Not for production use."""

    def __init__(self, holidays: set[tuple[str, date]] | None = None) -> None:
        self._holidays = set(holidays) if holidays else set()

    def add(self, client_id: str, day: date) -> None:
        self._holidays.add((client_id, day))

    def is_holiday(self, client_id: str, day: date) -> bool:
        return (client_id, day) in self._holidays


class InMemoryHrisRecordRepository:
    """Test/dev double for the HrisRecordRepository port. Not for production use."""

    def __init__(self) -> None:
        self._records: dict[str, HrisRecord] = {}

    def add(self, record: HrisRecord) -> None:
        self._records[record.record_id] = record

    def get(self, record_id: str) -> Optional[HrisRecord]:
        return self._records.get(record_id)

    def list_for_case(self, case_id: str) -> list[HrisRecord]:
        return [r for r in self._records.values() if r.case_id == case_id]

    def update(self, record: HrisRecord) -> None:
        if record.record_id not in self._records:
            raise KeyError(f"no HrisRecord with id {record.record_id!r} to update")
        self._records[record.record_id] = record


class InMemoryOfferRepository:
    """Test/dev double for the OfferRepository port. Not for production use.

    [Proposed] Dev simulator only; see intake/email_intake.py (OQ-01/Q-11).
    """

    def __init__(self) -> None:
        self._offers: dict[tuple[str, str], OfferRecord] = {}

    def get(self, client_id: str, offer_ref: str) -> Optional[OfferRecord]:
        return self._offers.get((client_id, offer_ref))

    def find_by_ref(self, offer_ref: str) -> list[OfferRecord]:
        return [o for o in self._offers.values() if o.offer_ref == offer_ref]

    def save(self, offer: OfferRecord) -> None:
        key = (offer.client_id, offer.offer_ref)
        if key not in self._offers:
            # [Proposed] offer refs are unique across clients: a fresh
            # insert under a ref another client already owns is rejected,
            # but re-saving (e.g. marking ACCEPTED) the same client's own
            # offer is always allowed.
            other_client = next(
                (
                    o
                    for o in self._offers.values()
                    if o.offer_ref == offer.offer_ref and o.client_id != offer.client_id
                ),
                None,
            )
            if other_client is not None:
                raise ValueError(
                    f"offer_ref {offer.offer_ref!r} is already used by client {other_client.client_id!r}"
                )
        self._offers[key] = offer


class InMemoryOnboardingFormSource:
    """Test/dev double for the OnboardingFormSource port. Not for production use.

    [Proposed] Dev simulator only; see intake/email_intake.py (OQ-01/Q-11).
    """

    def __init__(self) -> None:
        self._forms: dict[tuple[str, str], OnboardingForm] = {}

    def latest_for_offer(self, client_id: str, offer_ref: str) -> Optional[OnboardingForm]:
        return self._forms.get((client_id, offer_ref))

    def save_for_offer(self, client_id: str, offer_ref: str, form: OnboardingForm) -> None:
        self._forms[(client_id, offer_ref)] = form


class InMemoryPendingStartRepository:
    """Test/dev double for the PendingStartRepository port. Not for production use.

    [Proposed] Dev simulator only; see intake/email_intake.py (OQ-01/Q-11).
    """

    def __init__(self) -> None:
        self._pending: list[PendingStart] = []

    def add(self, pending: PendingStart) -> None:
        self._pending.append(pending)

    def get_waiting(self, client_id: str, offer_ref: str) -> Optional[PendingStart]:
        return next(
            (
                p
                for p in self._pending
                if p.client_id == client_id
                and p.offer_ref == offer_ref
                and p.status == PendingStartStatus.WAITING
            ),
            None,
        )

    def update(self, pending: PendingStart) -> None:
        for i, existing in enumerate(self._pending):
            if existing.pending_id == pending.pending_id:
                self._pending[i] = pending
                return
        raise KeyError(f"no PendingStart with id {pending.pending_id!r} to update")

    def list_waiting(self, client_id: str) -> list[PendingStart]:
        return [
            p
            for p in self._pending
            if p.client_id == client_id and p.status == PendingStartStatus.WAITING
        ]


class StubHrisGateway:
    """Deterministic stand-in for a real HRIS/payroll API. Not for production use.

    Keyed by (client_id, idempotency_key): a repeated write with the same
    key returns the same external_ref with already_existed=True and never
    creates a second entry. fail_next(n) makes the next n write() calls
    raise HrisUnavailableError instead of succeeding.
    """

    def __init__(self) -> None:
        self._external_refs: dict[tuple[str, str], str] = {}
        self._updated: list[tuple[str, HrisSystem, str]] = []
        self._fail_remaining = 0

    def fail_next(self, n: int) -> None:
        self._fail_remaining += n

    def write(
        self,
        client_id: str,
        system: HrisSystem,
        idempotency_key: str,
        payload: dict[str, str],
        sensitive_payload: dict[str, SecretStr],
    ) -> WriteResult:
        key = (client_id, idempotency_key)
        if key in self._external_refs:
            return WriteResult(external_ref=self._external_refs[key], already_existed=True)

        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise HrisUnavailableError(f"stub write failure for {system.value}")

        external_ref = f"ext-{new_id()}"
        self._external_refs[key] = external_ref
        return WriteResult(external_ref=external_ref, already_existed=False)

    def update(
        self,
        client_id: str,
        system: HrisSystem,
        idempotency_key: str,
        external_ref: str,
        payload: dict[str, str],
        sensitive_payload: dict[str, SecretStr],
    ) -> WriteResult:
        """Update in place. Returns the external_ref it was given and
        never mints a new one, so an update can never create a second
        employee. Idempotent on (client_id, idempotency_key) like write.
        """
        key = (client_id, idempotency_key)
        if key in self._external_refs:
            return WriteResult(external_ref=self._external_refs[key], already_existed=True)

        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise HrisUnavailableError(f"stub update failure for {system.value}")

        self._external_refs[key] = external_ref
        self._updated.append((client_id, system, external_ref))
        return WriteResult(external_ref=external_ref, already_existed=False)

    def written_count(self) -> int:
        """How many distinct employees exist in the target system.

        Counts distinct external refs, not calls: an in-place update
        reuses an existing employee, so it must not increase this. For
        create-only flows every write mints a new ref, so the number is
        the same as it always was.
        """
        return len(set(self._external_refs.values()))

    def updated_count(self) -> int:
        return len(self._updated)
