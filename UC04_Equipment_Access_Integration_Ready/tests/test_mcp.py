"""Tests for the UC-04 MCP (Model Context Protocol) Server and Tools.

Verifies:
- Tool listing and schema conformity.
- Read tools execution (case_context.get_case, case_context.case_for_employee).
- Write tool policy gating (it_service_desk.submit_request requires approval).
- Approved write tool execution.
- Outlook notification tool execution.
- Error handling on unknown tools.
"""
from __future__ import annotations

import pytest

from mcp.server import MCPPolicyError, MCPServer
from mcp.tools import UC04_TOOLS


@pytest.fixture
def mcp_server() -> MCPServer:
    return MCPServer()


def test_list_tools_returns_all_registered_tools(mcp_server: MCPServer) -> None:
    tools = mcp_server.list_tools()
    assert len(tools) == len(UC04_TOOLS)
    names = {t["name"] for t in tools}
    expected = {
        "case_context.get_case",
        "case_context.case_for_employee",
        "it_service_desk.submit_request",
        "outlook.send_notification",
    }
    assert expected == names


def test_get_case_read_tool(mcp_server: MCPServer) -> None:
    res = mcp_server.execute_tool("case_context.get_case", {"case_id": "SYN-CASE-001"})
    assert "case" in res
    assert res["case"] is not None
    assert res["case"]["case_id"] == "SYN-CASE-001"


def test_case_for_employee_read_tool(mcp_server: MCPServer) -> None:
    res = mcp_server.execute_tool("case_context.case_for_employee", {"employee_ref": "SYN-EMP-001"})
    assert "case_id" in res
    assert res["case_id"] == "SYN-CASE-001"


def test_submit_request_without_approval_raises_policy_error(mcp_server: MCPServer) -> None:
    args = {
        "request_id": "SYN-REQ-101",
        "case_id": "SYN-CASE-001",
        "item_kind": "ACCESS",
        "item_id": "email-teams-account",
    }
    with pytest.raises(MCPPolicyError) as exc_info:
        mcp_server.execute_tool("it_service_desk.submit_request", args, has_approval=False)
    assert "write_requires_approval" in str(exc_info.value)


def test_submit_request_with_approval_succeeds(mcp_server: MCPServer) -> None:
    args = {
        "request_id": "SYN-REQ-101",
        "case_id": "SYN-CASE-001",
        "item_kind": "ACCESS",
        "item_id": "email-teams-account",
    }
    res = mcp_server.execute_tool("it_service_desk.submit_request", args, has_approval=True)
    assert "ticket_id" in res
    assert res["ticket_id"].startswith("SYN-TKT-")
    assert res["request_id"] == "SYN-REQ-101"


def test_outlook_send_notification_tool(mcp_server: MCPServer) -> None:
    args = {
        "recipient_email": "hr-officer@example.com",
        "subject": "Case Update",
        "body": "Equipment ready for dispatch.",
    }
    res = mcp_server.execute_tool("outlook.send_notification", args)
    assert "message_id" in res
    assert res["message_id"].startswith("SYN-MSG-")
    assert mcp_server.outlook.email_count == 1


def test_unknown_tool_returns_error(mcp_server: MCPServer) -> None:
    res = mcp_server.execute_tool("non_existent_tool", {})
    assert "error" in res
