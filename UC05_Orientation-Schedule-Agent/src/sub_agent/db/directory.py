"""UC05's employee directory and intake trigger in supabase mode, both read from the authoritative
onboarding case (onboarding_cases, owned by UC01) instead of the synthetic fakes.

SupabaseEmployeeDirectory (EmployeeDirectoryPort)
  The employee's details exactly as the case holds them: designation = role_id, team = department,
  team lead = manager_id, joining date = joining_date. A missing value stays missing, so intake flags
  the case as incomplete (IncompleteEmployeeDataError) instead of inventing one.

SupabaseCaseEventSource (EmailPort)
  In supabase mode the "offer accepted" trigger is the authoritative case itself: UC01 opens the case
  once the offer is accepted, and the orchestrator starts UC05 only after its prerequisites (UC02/UC03)
  are satisfied. The event id is the case's uuid (case_id). A synthetic mailbox would mix fake
  employees into a real workflow, so none is used. "Processed" means UC05 has linked the case
  (SupabaseOnboardingRepository.create_onboarding_case); mark_processed is therefore a no-op.

Both are tenant-scoped exactly like the repository: bound with for_tenant(...) by the tool registry,
or given client_id explicitly. An unbound event source can resolve a case only by its uuid (globally
unique and unguessable); it never lists cases.
"""

from __future__ import annotations

from datetime import datetime, timezone

from connectors.email.base import OfferAcceptanceEvent
from connectors.employee_directory.base import EmployeeDirectoryRecord
from sub_agent.db import mapping as m
from sub_agent.db.repository import SupabaseOnboardingRepository, _date, _ts
from sub_agent.models import TenantMismatchError, TenantScopeRequiredError


class SupabaseEmployeeDirectory:
    def __init__(self, repository: SupabaseOnboardingRepository, *, tenant: str | None = None) -> None:
        self.repository, self.tenant = repository, tenant

    def for_tenant(self, client_id: str) -> "SupabaseEmployeeDirectory":
        if self.tenant is not None and self.tenant != client_id:
            raise TenantMismatchError("this directory is already bound to another client")
        return SupabaseEmployeeDirectory(self.repository.for_tenant(client_id), tenant=client_id)

    def get_employee(self, employee_id: str, *, client_id: str | None = None) -> EmployeeDirectoryRecord:
        tenant = self.tenant or client_id
        if self.tenant and client_id and client_id != self.tenant:
            raise KeyError(employee_id)  # another tenant's employee is reported exactly like an unknown one
        if not tenant:
            raise TenantScopeRequiredError("an employee lookup requires a verified client_id")
        repo = self.repository.for_tenant(tenant) if self.repository.tenant is None else self.repository
        row = repo._case_row(tenant, employee_id)  # KeyError (OnboardingCaseNotFoundError) if absent
        if row.get("is_deleted") is True:
            raise KeyError(employee_id)
        return EmployeeDirectoryRecord(
            employee_id=row["employee_no"], full_name=row.get("candidate_name") or "",
            email=row.get("candidate_email") or "", designation=row.get("role_id") or "",
            department=row.get("department") or "", team=row.get("department") or "",
            team_lead_id=row.get("manager_id") or "", client_id=row["client_id"],
            joining_date=_date(row["joining_date"]))


class SupabaseCaseEventSource:
    def __init__(self, repository: SupabaseOnboardingRepository, *, tenant: str | None = None) -> None:
        self.repository, self.tenant = repository, tenant

    def for_tenant(self, client_id: str) -> "SupabaseCaseEventSource":
        if self.tenant is not None and self.tenant != client_id:
            raise TenantMismatchError("this event source is already bound to another client")
        return SupabaseCaseEventSource(self.repository.for_tenant(client_id), tenant=client_id)

    def _case(self, event_id: str) -> dict:
        if not m.is_uuid(event_id):
            raise KeyError(event_id)  # in supabase mode an offer event IS a case uuid
        filters = [("case_id", f"eq.{event_id}")]
        if self.tenant:
            filters.append(("client_id", f"eq.{self.tenant}"))
        rows = self.repository.client.select("onboarding_cases", columns="case_id,client_id,employee_no,opened_at,"
                                             "is_deleted", filters=filters)
        if not rows or rows[0].get("is_deleted") is True:
            raise KeyError(event_id)
        return rows[0]

    def get_event(self, event_id: str) -> OfferAcceptanceEvent:
        row = self._case(event_id)
        opened = row.get("opened_at")
        return OfferAcceptanceEvent(event_id=event_id, employee_id=row.get("employee_no") or None,
                                    received_at=_ts(opened) if opened else datetime.now(timezone.utc),
                                    raw_subject="", raw_body="")

    def is_processed(self, event_id: str) -> bool:
        try:
            row = self._case(event_id)
        except KeyError:
            return False
        return self.repository.is_case_linked(row["case_id"], row["client_id"])

    def mark_processed(self, event_id: str) -> None:
        """No-op: linking the case (create_onboarding_case) is what marks it processed."""
