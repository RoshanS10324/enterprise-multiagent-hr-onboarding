"""Ports to the outside world. Only local mock adapters exist; the real IT service desk (MCP behind API Management)
is a future integration point and is NOT connected."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from sub_agent.states import ParentCaseStatus

if TYPE_CHECKING:
    from sub_agent.runs import AgentRunRecord


class AdapterError(Exception):
    pass


class AdapterTimeout(AdapterError):
    pass


class MockTicket(BaseModel):
    ticket_id: str
    request_id: str


class ParentCase(BaseModel):
    """The existing onboarding case owned by the parent workflow. UC-04 reads it and never creates one (BR-24)."""

    case_id: str
    employee_ref: str
    client_id: str
    status: ParentCaseStatus = ParentCaseStatus.ACTIVE


class SlotOffer(BaseModel):
    """One collection slot IT has made available, with how many people it can take."""

    slot_id: str
    slot_date: str  # ISO date
    capacity: int


class SlotAvailabilityPort(Protocol):
    """What UC-04 may ask IT about collection slots. IT owns availability; the real interface is pending the IT contract."""

    def available_slots(self, *, cohort_ref: str) -> list[SlotOffer]: ...


class ITServiceDeskPort(Protocol):
    def submit_request(self, *, request_id: str, case_id: str, item_kind: str, item_id: str) -> MockTicket: ...


class CaseContextPort(Protocol):
    def get_case(self, case_id: str) -> ParentCase | None: ...

    def case_for_employee(self, employee_ref: str, client_id: str | None = None) -> str | None:
        """The case of this employee **within this client**. `client_id=None` is the legacy unscoped lookup, kept only for the in-memory mock;
        a durable implementation must refuse it, because an employee number is unique per client, not across clients (GR-16)."""
        ...


class AuditSinkPort(Protocol):
    """Receives every audit entry after `AuditLog` has validated and recorded it.

    `entry` is exactly the dictionary `AuditLog.record` returns (fixed field allowlist, sanitised text, nothing else).
    `context` carries what the entry itself does not: `actor_id`, `client_id`, `run_id`, `invocation_mode`, set by the entry points for the
    duration of one call and empty outside a call. Treat both as read-only. An exception raised by `write` propagates: the audit trail is
    mandatory (GR-09), so a failing sink must not be silently ignored. A sink must not be used to carry business decisions.
    """

    def write(self, entry: Mapping[str, Any], context: Mapping[str, Any]) -> None: ...


class AgentRunRecorderPort(Protocol):
    """Receives one `AgentRunRecord` for every direct or orchestrated call that returns a result.

    Calls that raise (refused caller, invalid input, privacy violation, unexpected error) are not recorded here yet: they are in the audit
    trail. An exception raised by `record` propagates.
    """

    def record(self, run: "AgentRunRecord") -> None: ...
