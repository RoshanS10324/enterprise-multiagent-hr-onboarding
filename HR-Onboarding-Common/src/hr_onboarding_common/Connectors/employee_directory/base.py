"""EmployeeDirectoryPort: the configured employee data source (the future real HRMS).

Read-only: the onboarding agent never writes to the system of record. It reads what the
directory reports and, from that, creates its own onboarding case (OnboardingPort.create_
onboarding_case). Only the in-memory MockEmployeeDirectory exists until a real HRMS product and
credentials are approved -- same "fake adapter first" rule as every other connector in this repo.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from pydantic import BaseModel


class EmployeeDirectoryRecord(BaseModel):
    employee_id: str
    full_name: str
    email: str
    designation: str
    department: str
    team: str
    team_lead_id: str
    client_id: str
    joining_date: date


class EmployeeDirectoryPort(Protocol):
    def get_employee(self, employee_id: str) -> EmployeeDirectoryRecord: ...
