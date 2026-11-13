from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MCPResponse:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    source: str = "mock"


class BaseMCPClient:
    """
    Base class for all MCP clients.

    In production each subclass replaces _call() with a real HTTP/gRPC call
    to the corresponding MCP server. The mock implementation returns
    configurable data so all dependent code can be developed and tested
    without live credentials.
    """

    _SERVICE_NAME: str = "base"

    def __init__(self, base_url: str = "", api_key: str = "", mock: bool = True) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._mock = mock

    async def _call(self, tool: str, params: dict) -> MCPResponse:
        if self._mock:
            return await self._mock_call(tool, params)
        return await self._real_call(tool, params)

    async def _mock_call(self, tool: str, params: dict) -> MCPResponse:
        raise NotImplementedError(f"Mock not implemented for tool '{tool}' in {self._SERVICE_NAME}")

    async def _real_call(self, tool: str, params: dict) -> MCPResponse:
        raise NotImplementedError(
            f"Real MCP call not implemented for {self._SERVICE_NAME}. "
            "Configure base_url and api_key and implement _real_call()."
        )
