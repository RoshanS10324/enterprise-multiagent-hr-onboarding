"""UC-04 Model Context Protocol (MCP) package.

Exposes deterministic local tool servers and tool schemas for UC-04 Equipment
& Approved Access Management.
"""
from mcp.server import MCPPolicyError, MCPServer
from mcp.tools import UC04_TOOLS, MCPTool

__all__ = ["MCPServer", "MCPTool", "MCPPolicyError", "UC04_TOOLS"]
