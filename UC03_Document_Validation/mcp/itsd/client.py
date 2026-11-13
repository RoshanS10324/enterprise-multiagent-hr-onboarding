from __future__ import annotations

from dataclasses import dataclass

from mcp.base import BaseMCPClient, MCPResponse


@dataclass
class ServiceTicket:
    ticket_id: str
    case_id: str
    employee_id: str
    category: str
    summary: str
    status: str
    priority: str
    created_at: str


@dataclass
class AccessRequest:
    request_id: str
    employee_id: str
    systems: list[str]
    status: str
    approval_id: str | None


class ITSDClient(BaseMCPClient):
    """
    Client for the IT Service Desk MCP server.

    Tools exposed:
    - create_ticket         Raise an IT service ticket for an onboarding task
    - get_ticket_status     Check ticket progress
    - create_access_request Request system access for the new employee (requires ACCESS_REQUEST approval)
    - get_access_status     Check access provisioning status

    Production setup required:
        ITSD_BASE_URL   — IT service desk REST API base URL
        ITSD_API_KEY    — API key (from Key Vault)

    Access requests always require an approved ApprovalRecord with
    action_type='ACCESS_REQUEST' before this client will submit them.
    The mock simulates this check but does NOT enforce it — enforcement
    is done in hooks/approval.py before this client is called.
    """

    _SERVICE_NAME = "itsd"

    async def create_ticket(
        self,
        case_id: str,
        employee_id: str,
        category: str,
        summary: str,
        priority: str = "medium",
    ) -> ServiceTicket:
        resp = await self._call(
            "create_ticket",
            {
                "case_id": case_id,
                "employee_id": employee_id,
                "category": category,
                "summary": summary,
                "priority": priority,
            },
        )
        d = resp.data
        return ServiceTicket(
            ticket_id=d.get("ticket_id", "TKT-MOCK-001"),
            case_id=case_id,
            employee_id=employee_id,
            category=category,
            summary=summary,
            status=d.get("status", "open"),
            priority=priority,
            created_at=d.get("created_at", "2024-10-16T00:00:00Z"),
        )

    async def get_ticket_status(self, ticket_id: str) -> str:
        resp = await self._call("get_ticket_status", {"ticket_id": ticket_id})
        return resp.data.get("status", "unknown")

    async def create_access_request(
        self,
        employee_id: str,
        systems: list[str],
        approval_id: str,
    ) -> AccessRequest:
        resp = await self._call(
            "create_access_request",
            {"employee_id": employee_id, "systems": systems, "approval_id": approval_id},
        )
        d = resp.data
        return AccessRequest(
            request_id=d.get("request_id", "REQ-MOCK-001"),
            employee_id=employee_id,
            systems=systems,
            status=d.get("status", "pending"),
            approval_id=approval_id,
        )

    async def get_access_status(self, request_id: str) -> str:
        resp = await self._call("get_access_status", {"request_id": request_id})
        return resp.data.get("status", "unknown")

    # ─── Mock implementations ──────────────────────────────────────────────────

    async def _mock_call(self, tool: str, params: dict) -> MCPResponse:
        if tool == "create_ticket":
            return MCPResponse(
                success=True,
                data={
                    "ticket_id": "TKT-MOCK-001",
                    "status": "open",
                    "created_at": "2024-10-16T00:00:00Z",
                },
                source="mock",
            )

        if tool == "get_ticket_status":
            return MCPResponse(
                success=True,
                data={"ticket_id": params["ticket_id"], "status": "in_progress"},
                source="mock",
            )

        if tool == "create_access_request":
            return MCPResponse(
                success=True,
                data={"request_id": "REQ-MOCK-001", "status": "pending"},
                source="mock",
            )

        if tool == "get_access_status":
            return MCPResponse(
                success=True,
                data={"request_id": params["request_id"], "status": "provisioned"},
                source="mock",
            )

        return MCPResponse(success=False, error=f"Unknown tool: {tool}", source="mock")
