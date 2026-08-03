"""连接池 —— 多 MCP server 连接缓存与复用。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.mcp.client import MCPClient
from core.mcp.transport.base import Transport
from core.mcp.transport.http import StreamableHTTPTransport
from core.mcp.transport.stdio import StdioTransport

logger = logging.getLogger(__name__)


class ConnectionPool:
    """MCP 连接池。

    按 server name 缓存 MCPClient，避免每次工具调用都重连。
    """

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._server_configs: dict[str, dict[str, Any]] = {}

    def configure(self, servers: list[dict[str, Any]]) -> None:
        """加载 server 配置列表。"""
        for cfg in servers:
            name = cfg.get("name", "")
            if name:
                self._server_configs[name] = cfg

    async def get_client(self, name: str) -> MCPClient | None:
        """获取或创建指定 server 的 MCPClient。

        已连接的直接返回缓存；未连接或断线则重新初始化。
        """
        # 返回缓存
        if name in self._clients:
            client = self._clients[name]
            if client._transport.is_connected:
                return client
            # 断线 → 清理并重连
            await self._close_client(name)

        cfg = self._server_configs.get(name)
        if not cfg:
            return None

        transport = self._create_transport(cfg)
        client = MCPClient(transport, server_name=name)
        self._clients[name] = client

        try:
            await client.connect()
            await asyncio.wait_for(client.initialize(), timeout=30.0)
            logger.info("MCP server '%s' connected and initialized", name)
        except (Exception, asyncio.TimeoutError):
            logger.debug("Failed to connect MCP server '%s'", name, exc_info=True)
            await self._close_client(name)
            return None

        return client

    async def list_all_tools(self) -> dict[str, list[dict[str, Any]]]:
        """从所有已连接 server 收集工具列表。"""
        result: dict[str, list[dict[str, Any]]] = {}
        for name in self._server_configs:
            client = await self.get_client(name)
            if client:
                try:
                    tools = await client.list_tools()
                    result[name] = tools
                except Exception:
                    logger.exception("Failed to list tools for server '%s'", name)
        return result

    async def close_all(self) -> None:
        """关闭所有连接。"""
        for name in list(self._clients.keys()):
            await self._close_client(name)
        self._clients.clear()

    # ── Internal ────────────────────────────────────────────────────

    def _create_transport(self, cfg: dict[str, Any]) -> Transport:
        transport_type = cfg.get("type", "stdio")

        if transport_type == "stdio":
            return StdioTransport(
                command=cfg["command"],
                args=cfg.get("args", []),
                env=cfg.get("env", {}),
                cwd=cfg.get("cwd"),
            )
        elif transport_type in ("http", "streamableHttp"):
            return StreamableHTTPTransport(
                url=cfg["url"],
                headers=cfg.get("headers", {}),
                timeout=cfg.get("timeout", 60.0),
            )
        else:
            raise ValueError(f"Unknown transport type: {transport_type}")

    async def _close_client(self, name: str) -> None:
        client = self._clients.pop(name, None)
        if client:
            try:
                await client.close()
            except Exception:
                pass
