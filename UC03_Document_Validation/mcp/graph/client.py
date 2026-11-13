from __future__ import annotations

from dataclasses import dataclass

from mcp.base import BaseMCPClient, MCPResponse


@dataclass
class UserProfile:
    user_id: str
    display_name: str
    email: str
    department: str
    job_title: str
    account_enabled: bool


@dataclass
class EmailDraft:
    to: list[str]
    subject: str
    body: str
    draft_id: str


class GraphClient(BaseMCPClient):
    """
    Client for the Microsoft Graph MCP server.

    Tools exposed:
    - get_user              Returns Azure AD user profile
    - create_email_draft    Creates a draft email (does NOT send — requires HR approval)
    - send_email            Sends an approved draft (requires SEND_REMINDER approval record)
    - create_teams_channel  Creates a Teams onboarding channel

    Production setup required:
        GRAPH_TENANT_ID   — Azure AD tenant
        GRAPH_CLIENT_ID   — App registration client ID
        GRAPH_CLIENT_SECRET — From Key Vault

    The mock returns realistic static data. No Azure AD app registration needed
    for local development or CI.
    """

    _SERVICE_NAME = "graph"

    async def get_user(self, user_id: str) -> UserProfile | None:
        resp = await self._call("get_user", {"user_id": user_id})
        if not resp.success:
            return None
        d = resp.data
        return UserProfile(
            user_id=d["user_id"],
            display_name=d["display_name"],
            email=d["email"],
            department=d["department"],
            job_title=d["job_title"],
            account_enabled=d["account_enabled"],
        )

    async def create_email_draft(
        self, to: list[str], subject: str, body: str
    ) -> EmailDraft:
        resp = await self._call("create_email_draft", {"to": to, "subject": subject, "body": body})
        d = resp.data
        return EmailDraft(
            to=to,
            subject=subject,
            body=body,
            draft_id=d.get("draft_id", "draft-mock-001"),
        )

    async def send_email(self, draft_id: str, approval_id: str) -> bool:
        """Send a draft. approval_id must reference an APPROVED ApprovalRecord."""
        resp = await self._call("send_email", {"draft_id": draft_id, "approval_id": approval_id})
        return resp.success

    async def create_teams_channel(self, team_id: str, channel_name: str) -> str:
        resp = await self._call(
            "create_teams_channel", {"team_id": team_id, "channel_name": channel_name}
        )
        return resp.data.get("channel_id", "")

    # ─── Mock implementations ──────────────────────────────────────────────────

    async def _mock_call(self, tool: str, params: dict) -> MCPResponse:
        if tool == "get_user":
            return MCPResponse(
                success=True,
                data={
                    "user_id": params["user_id"],
                    "display_name": "HR User",
                    "email": "hr@company.com",
                    "department": "Human Resources",
                    "job_title": "HR Business Partner",
                    "account_enabled": True,
                },
                source="mock",
            )

        if tool == "create_email_draft":
            return MCPResponse(
                success=True,
                data={"draft_id": "draft-mock-001", "status": "created"},
                source="mock",
            )

        if tool == "send_email":
            return MCPResponse(
                success=True,
                data={"sent": True, "draft_id": params.get("draft_id")},
                source="mock",
            )

        if tool == "create_teams_channel":
            return MCPResponse(
                success=True,
                data={"channel_id": "channel-mock-001"},
                source="mock",
            )

        return MCPResponse(success=False, error=f"Unknown tool: {tool}", source="mock")
