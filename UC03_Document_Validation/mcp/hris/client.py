from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp.base import BaseMCPClient, MCPResponse


@dataclass
class EmployeeRecord:
    employee_id: str
    full_name: str
    email: str
    department: str
    job_title: str
    location: str
    nationality: str
    joining_date: str | None
    contract_type: str
    manager_id: str | None
    status: str


@dataclass
class OnboardingFormData:
    case_id: str
    employee_id: str
    approved: bool
    form_version: str
    submitted_at: str
    fields: dict[str, Any]


class HRISClient(BaseMCPClient):
    """
    Client for the HRIS MCP server.

    Tools exposed:
    - get_employee          Returns employee master record
    - get_onboarding_form   Returns approved onboarding form data
    - update_employee       Writes employee data (requires WRITE_SYSTEM approval)
    - list_open_cases       Returns open onboarding cases for a client

    Production setup required:
        HRIS_BASE_URL   — base URL of the HRIS REST API
        HRIS_API_KEY    — API key or OAuth token (from Key Vault)

    The mock returns realistic static data so UC-03 can be developed
    and tested without a live HRIS connection.
    """

    _SERVICE_NAME = "hris"

    async def get_employee(self, employee_id: str) -> EmployeeRecord | None:
        resp = await self._call("get_employee", {"employee_id": employee_id})
        if not resp.success:
            return None
        d = resp.data
        return EmployeeRecord(
            employee_id=d["employee_id"],
            full_name=d["full_name"],
            email=d["email"],
            department=d["department"],
            job_title=d["job_title"],
            location=d["location"],
            nationality=d["nationality"],
            joining_date=d.get("joining_date"),
            contract_type=d["contract_type"],
            manager_id=d.get("manager_id"),
            status=d["status"],
        )

    async def get_onboarding_form(self, case_id: str) -> OnboardingFormData | None:
        resp = await self._call("get_onboarding_form", {"case_id": case_id})
        if not resp.success:
            return None
        d = resp.data
        return OnboardingFormData(
            case_id=d["case_id"],
            employee_id=d["employee_id"],
            approved=d["approved"],
            form_version=d["form_version"],
            submitted_at=d["submitted_at"],
            fields=d["fields"],
        )

    async def update_employee(self, employee_id: str, fields: dict[str, Any]) -> bool:
        resp = await self._call("update_employee", {"employee_id": employee_id, "fields": fields})
        return resp.success

    async def list_open_cases(self, client_id: str) -> list[dict]:
        resp = await self._call("list_open_cases", {"client_id": client_id})
        return resp.data.get("cases", [])

    # ─── Mock implementations ──────────────────────────────────────────────────

    async def _mock_call(self, tool: str, params: dict) -> MCPResponse:
        if tool == "get_employee":
            return MCPResponse(
                success=True,
                data={
                    "employee_id": params["employee_id"],
                    "full_name": "Mohammed Al Rashidi",
                    "email": "m.alrashidi@client.com",
                    "department": "Engineering",
                    "job_title": "Senior Software Engineer",
                    "location": "UAE",
                    "nationality": "Jordanian",
                    "joining_date": "2024-11-01",
                    "contract_type": "full_time",
                    "manager_id": "mgr-001",
                    "status": "pre_joining",
                },
                source="mock",
            )

        if tool == "get_onboarding_form":
            return MCPResponse(
                success=True,
                data={
                    "case_id": params["case_id"],
                    "employee_id": "emp-0042",
                    "approved": True,
                    "form_version": "v2",
                    "submitted_at": "2024-10-01T09:00:00Z",
                    "fields": {
                        "full_name": "Mohammed Al Rashidi",
                        "designation": "Senior Software Engineer",
                        "joining_date": "2024-11-01",
                        "grade": "L4",
                        "sponsorship_applicable": True,
                        "visa_required": True,
                    },
                },
                source="mock",
            )

        if tool == "update_employee":
            return MCPResponse(success=True, data={"updated": True}, source="mock")

        if tool == "list_open_cases":
            return MCPResponse(
                success=True,
                data={"cases": [{"case_id": "case-2024-001", "status": "in_progress"}]},
                source="mock",
            )

        return MCPResponse(success=False, error=f"Unknown tool: {tool}", source="mock")
