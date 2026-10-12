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
        # MCP 规范的 `annotations`（服务端自述这条工具的性质）
        ann = tool_def.get("annotations")
        self._annotations: dict[str, Any] = ann if isinstance(ann, dict) else {}

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
        """按 MCP 规范里服务端**自述**的 `annotations.readOnlyHint` 判断。

        ⚠️ 旧实现无条件 `return True`（注释写"无法预判，默认只读避免 HITL"）——
        那是个 **fail-open**：`normalize_tool_category("mcp", True)` 把每个 MCP 工具
        都归成 `read` 档，Layer 1 直接放行，**连审批都不进**。也就是说挂一个 MCP
        server 等于把它的全部工具（包括 `browser_click`、任意写操作）无条件授权。

        现在：
        * 服务端声明 `readOnlyHint: true` → 只读（走 read 档，自动放行）；
        * **没声明** → 非只读（走 command 档，要过审批/策略）。

        这条判据之所以可信，是因为它是 MCP 规范的一部分、由**写工具的人**声明；
        服务端不声明说明它自己都不确定，那就按最保守的处理。
        """
        return self._annotations.get("readOnlyHint") is True

    def is_destructive(self) -> bool:
        """服务端声明了 `destructiveHint: true` 才算破坏性。"""
        return self._annotations.get("destructiveHint") is True

    def is_concurrency_safe(self, input: dict) -> bool:
        return self.is_read_only()

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
