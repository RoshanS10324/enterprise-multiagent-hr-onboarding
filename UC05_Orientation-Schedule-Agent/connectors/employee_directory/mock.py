"""TEST / DEMO ONLY. MockEmployeeDirectory: an in-memory, simulated EmployeeDirectoryPort.

Not an HRMS: no network call, no credential, and every record is synthetic (example.com
addresses, made-up names). Read-only, like the real directory will be.

  - get_employee returns a copy of the deterministic synthetic record: name, email, designation,
    department, team, team lead (manager) and joining date.
  - A missing employee raises KeyError.
  - Tenant isolation: with `client_id`, another client's employee raises the same KeyError as a
    missing one, so the directory never confirms another tenant's employee exists.
  - EMP-TEST-003 is deliberately incomplete (no team, no team lead) for the "stop and flag, never
    invent" requirement; EMP-TEST-010 belongs to CLIENT-TEST-B.
reset() restores the records the mock was constructed with.
"""

from __future__ import annotations

from datetime import date

from connectors.employee_directory.base import EmployeeDirectoryRecord


class MockEmployeeDirectory:
    def __init__(self, records: list[EmployeeDirectoryRecord]):
        self._initial = [r.model_copy(deep=True) for r in records]
        self.reset()

    def reset(self) -> None:
        self.records: dict[str, EmployeeDirectoryRecord] = {r.employee_id: r.model_copy(deep=True)
                                                            for r in self._initial}

    def get_employee(self, employee_id: str, *, client_id: str | None = None) -> EmployeeDirectoryRecord:
        record = self.records.get(employee_id)
        if record is None or (client_id is not None and record.client_id != client_id):
            raise KeyError(employee_id)
        return record.model_copy(deep=True)


def build_mock_employee_directory() -> MockEmployeeDirectory:
    return MockEmployeeDirectory([
        EmployeeDirectoryRecord(employee_id="EMP-TEST-001", full_name="Alex Morgan",
                                email="alex.morgan@example.com", designation="Software Engineer",
                                department="Engineering", team="Platform", team_lead_id="TL-TEST-001",
                                client_id="CLIENT-TEST-A", joining_date=date(2026, 12, 1)),
        EmployeeDirectoryRecord(employee_id="EMP-TEST-002", full_name="Jamie Chen",
                                email="jamie.chen@example.com", designation="Data Analyst",
                                department="Data", team="Analytics", team_lead_id="TL-TEST-002",
                                client_id="CLIENT-TEST-A", joining_date=date(2026, 12, 8)),
        # incomplete directory record (no team_lead assigned yet) -- used to test the "stop and
        # flag, never invent" requirement.
        EmployeeDirectoryRecord(employee_id="EMP-TEST-003", full_name="Priya Nair",
                                email="priya.nair@example.com", designation="QA Engineer",
                                department="Quality", team="", team_lead_id="",
                                client_id="CLIENT-TEST-A", joining_date=date(2026, 12, 15)),
        EmployeeDirectoryRecord(employee_id="EMP-TEST-010", full_name="Morgan Lee",
                                email="morgan.lee@example.com", designation="Support Engineer",
                                department="Support", team="Tier 2", team_lead_id="TL-TEST-003",
                                client_id="CLIENT-TEST-B", joining_date=date(2026, 12, 1)),
    ])
