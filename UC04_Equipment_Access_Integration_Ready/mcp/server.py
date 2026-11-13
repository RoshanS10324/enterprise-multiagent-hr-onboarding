"""In-memory MCP Server for UC-04 Equipment & Approved Access Management.

Exposes UC-04 capabilities via the Model Context Protocol (MCP) tool contract.
Enforces policy gating (L1 autonomy, write approval requirements) and routes
execution to deterministic mock adapters.
"""
from __future__ import annotations

from typing import Any

from connectors.outlook.base import EmailMessage
from connectors.outlook.mock import MockOutlookConnector
from mcp.tools import UC04_TOOLS, MCPTool
from sub_agent.adapters.case_context_mock import MockCaseContext
from sub_agent.adapters.it_service_desk_mock import MockITServiceDesk
from sub_agent.fixtures import load
from sub_agent.ports import ParentCase


class MCPPolicyError(PermissionError):
    """Raised when an MCP tool invocation violates sub-agent policy."""
    pass


class MCPServer:
    """Deterministic, local MCP Server dispatching tool calls to mock adapters."""

    def __init__(
        self,
        cases: MockCaseContext | None = None,
        it_desk: MockITServiceDesk | None = None,
        outlook: MockOutlookConnector | None = None,
    ) -> None:
        default_cases = [ParentCase.model_validate(c) for c in load("parent_cases.json")]
        self.cases = cases or MockCaseContext(default_cases)
        self.it_desk = it_desk or MockITServiceDesk()
        self.outlook = outlook or MockOutlookConnector()
        self._tools: dict[str, MCPTool] = dict(UC04_TOOLS)

    def list_tools(self) -> list[dict[str, Any]]:
        """List all available MCP tools and their schemas."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "category": t.category,
            }
            for t in self._tools.values()
        ]

    def get_tool(self, tool_name: str) -> MCPTool | None:
        """Retrieve a specific tool definition by name."""
        return self._tools.get(tool_name)

    def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        has_approval: bool = False,
    ) -> dict[str, Any]:
        """Execute an MCP tool deterministically with policy checks."""
        if tool_name not in self._tools:
            return {"error": f"Tool '{tool_name}' not found in UC-04 MCP registry."}

        tool = self._tools[tool_name]

        # Enforce write_requires_approval guardrail (GR-20)
        if tool.category == "write_requires_approval" and not has_approval:
            raise MCPPolicyError(
                f"Tool '{tool_name}' is classified as write_requires_approval. "
                "Explicit approval must be confirmed before dispatch."
            )

        # Dispatch to mock adapters
        if tool_name == "case_context.get_case":
            case = self.cases.get_case(arguments.get("case_id", ""))
            return {"case": case.model_dump() if case else None}

        if tool_name == "case_context.case_for_employee":
            case_id = self.cases.case_for_employee(arguments.get("employee_ref", ""))
            return {"case_id": case_id}

        if tool_name == "it_service_desk.submit_request":
            ticket = self.it_desk.submit_request(
                request_id=arguments["request_id"],
                case_id=arguments["case_id"],
                item_kind=arguments["item_kind"],
                item_id=arguments["item_id"],
            )
            return {
                "ticket_id": ticket.ticket_id,
                "request_id": ticket.request_id,
            }

        if tool_name == "outlook.send_notification":
            msg = EmailMessage(
                to=[arguments["recipient_email"]],
                subject=arguments["subject"],
                body=arguments["body"],
            )
            sent = self.outlook.send_email(msg)
            return {
                "message_id": sent.message_id,
                "to": sent.to,
                "subject": sent.subject,
            }

        return {"error": f"Unhandled tool execution: {tool_name}"}
