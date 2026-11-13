"""MCP (Model Context Protocol) tool definitions for UC-04.

Defines the tool schemas matching the policy allowlist in policy/uc04_policy.json:
- case_context.get_case (read)
- case_context.case_for_employee (read)
- it_service_desk.submit_request (write_requires_approval)
- outlook.send_notification (advisory notification)
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class MCPTool:
    """Definition of an MCP tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    category: str  # "read", "write", or "write_requires_approval"
    handler: Callable[..., Any] | None = None


# Tool schemas conforming to standard JSON-schema
CASE_CONTEXT_GET_CASE = MCPTool(
    name="case_context.get_case",
    description="Retrieve parent onboarding case context for a given synthetic case ID.",
    parameters={
        "type": "object",
        "properties": {
            "case_id": {
                "type": "string",
                "description": "Synthetic case ID, e.g. SYN-CASE-001",
            }
        },
        "required": ["case_id"],
    },
    category="read",
)

CASE_CONTEXT_CASE_FOR_EMPLOYEE = MCPTool(
    name="case_context.case_for_employee",
    description="Retrieve parent onboarding case context for a given synthetic employee reference.",
    parameters={
        "type": "object",
        "properties": {
            "employee_ref": {
                "type": "string",
                "description": "Synthetic employee reference, e.g. SYN-EMP-001",
            }
        },
        "required": ["employee_ref"],
    },
    category="read",
)

IT_SERVICE_DESK_SUBMIT_REQUEST = MCPTool(
    name="it_service_desk.submit_request",
    description="Submit an approved equipment or access request to the mock IT Service Desk.",
    parameters={
        "type": "object",
        "properties": {
            "request_id": {"type": "string", "description": "Synthetic request ID"},
            "case_id": {"type": "string", "description": "Synthetic case ID"},
            "item_kind": {"type": "string", "enum": ["ACCESS", "EQUIPMENT"]},
            "item_id": {"type": "string", "description": "Item identifier"},
        },
        "required": ["request_id", "case_id", "item_kind", "item_id"],
    },
    category="write_requires_approval",
)

OUTLOOK_SEND_NOTIFICATION = MCPTool(
    name="outlook.send_notification",
    description="Dispatch a simulated advisory email notification via mock Outlook connector.",
    parameters={
        "type": "object",
        "properties": {
            "recipient_email": {"type": "string", "description": "Recipient email address"},
            "subject": {"type": "string", "description": "Notification subject"},
            "body": {"type": "string", "description": "Notification body content"},
        },
        "required": ["recipient_email", "subject", "body"],
    },
    category="read",
)

UC04_TOOLS: dict[str, MCPTool] = {
    CASE_CONTEXT_GET_CASE.name: CASE_CONTEXT_GET_CASE,
    CASE_CONTEXT_CASE_FOR_EMPLOYEE.name: CASE_CONTEXT_CASE_FOR_EMPLOYEE,
    IT_SERVICE_DESK_SUBMIT_REQUEST.name: IT_SERVICE_DESK_SUBMIT_REQUEST,
    OUTLOOK_SEND_NOTIFICATION.name: OUTLOOK_SEND_NOTIFICATION,
}
