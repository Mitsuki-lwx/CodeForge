"""MCPToolAdapter —— 将 MCP 远端工具包装成 CodeForge Tool 接口。"""

from __future__ import annotations

from typing import Any

from core.mcp.client import MCPClient
from core.tool.context import ExecutionContext
from core.tool.interface import Tool
from core.tool.result import ToolResult


class MCPToolAdapter(Tool):
    """MCP 远端工具 → CodeForge Tool 适配器。

    Agent 调用 execute() 时，适配器通过 MCPClient 向远端 server 发送
    tools/call 请求，并将结果转换为 ToolResult。
    """

    timeout_seconds = 60.0

    def __init__(
        self,
        client: MCPClient,
        tool_def: dict[str, Any],
        server_name: str = "",
    ) -> None:
        self._client = client
        self._tool_name = tool_def["name"]
        self._description = tool_def.get("description", "")
        self._input_schema = tool_def.get("inputSchema", {})
        self._server_name = server_name or client.server_name

    def name(self) -> str:
        # 用 server_name__tool_name 避免不同 server 的同名工具冲突
        return f"{self._server_name}__{self._tool_name}"

    def description(self) -> str:
        return f"[MCP:{self._server_name}] {self._description}"

    def input_schema(self) -> dict:
        return self._input_schema

    async def execute(self, context: ExecutionContext, input: dict) -> ToolResult:
        try:
            result = await self._client.call_tool(self._tool_name, input)
            return ToolResult(
                success=True,
                data=self._format_result(result),
            )
        except TimeoutError:
            return ToolResult(
                success=False,
                error=f"MCP tool '{self._tool_name}' timed out after 60s",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"MCP tool '{self._tool_name}' error: {e}",
            )

    def is_read_only(self) -> bool:
        return True  # MCP 工具无法预判，默认只读避免 HITL

    def is_destructive(self) -> bool:
        return False

    def is_concurrency_safe(self, input: dict) -> bool:
        return True

    def category(self) -> str:
        return "mcp"

    @staticmethod
    def _format_result(result: dict) -> str:
        """将 MCP tools/call 结果格式化为人类可读字符串。"""
        content = result.get("content", [])
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text", str(block)))
                else:
                    parts.append(str(block))
            return "\n".join(parts) if parts else str(result)
        return str(result)
