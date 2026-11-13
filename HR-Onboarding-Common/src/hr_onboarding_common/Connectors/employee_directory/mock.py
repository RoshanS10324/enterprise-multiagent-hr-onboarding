"""In-memory employee directory for development and tests. Fictional data only."""

from __future__ import annotations

from datetime import date

from onboarding_agent.connectors.employee_directory.base import EmployeeDirectoryRecord


class MockEmployeeDirectory:
    def __init__(self, records: list[EmployeeDirectoryRecord]):
        self.records = {r.employee_id: r for r in records}

    def get_employee(self, employee_id: str) -> EmployeeDirectoryRecord:
        return self.records[employee_id].model_copy(deep=True)


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
