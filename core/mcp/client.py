"""MCPClient —— JSON-RPC 会话管理 + MCP 生命周期。

实现 initialize → tools/list → tools/call 流程，
用 asyncio.Future 做 id → 响应异步匹配。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.mcp.transport.base import Transport
from core.mcp.types import (
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    parse_message,
)

logger = logging.getLogger(__name__)


class MCPClient:
    """MCP 客户端。

    持有 Transport，管理 JSON-RPC 会话和 MCP 生命周期。
    """

    def __init__(self, transport: Transport, server_name: str = "") -> None:
        self._transport = transport
        self.server_name = server_name
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._receive_task: asyncio.Task | None = None
        self._connected = False
        self._server_capabilities: dict = {}

    # ── Public API ──────────────────────────────────────────────────

    async def connect(self) -> None:
        """建立连接并启动接收循环。"""
        await self._transport.connect()
        self._receive_task = asyncio.create_task(self._receive_loop())
        self._connected = True

    async def initialize(self) -> dict:
        """MCP 握手：initialize → initialized。"""
        result = await self._call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
                },
                "clientInfo": {
                    "name": "CodeForge",
                    "version": "0.1.0",
                },
            },
        )
        self._server_capabilities = result
        # 发送 initialized 通知
        await self._send_notification("notifications/initialized", {})
        logger.info("MCP initialized with server %s", self.server_name)
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        """发现工具列表。"""
        result = await self._call("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用工具。"""
        return await self._call("tools/call", {"name": name, "arguments": arguments})

    async def close(self) -> None:
        """关闭连接。"""
        self._connected = False
        if self._receive_task:
            self._receive_task.cancel()
            self._receive_task = None
        # Cancel 所有 pending futures
        for fid, future in self._pending.items():
            if not future.done():
                future.set_exception(ConnectionError("MCP connection closed"))
        self._pending.clear()
        await self._transport.close()

    # ── Internal ────────────────────────────────────────────────────

    async def _call(self, method: str, params: dict) -> dict:
        """发送 JSON-RPC 请求并等待响应。"""
        req_id = self._next_id
        self._next_id += 1

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        request = JsonRpcRequest(method=method, params=params, id=req_id)
        await self._transport.send(request.serialize())

        try:
            return await asyncio.wait_for(future, timeout=60.0)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"MCP call '{method}' timed out after 60s")

    async def _send_notification(self, method: str, params: dict) -> None:
        """发送 JSON-RPC 通知（无 id，不等待响应）。"""
        notif = JsonRpcNotification(method=method, params=params)
        await self._transport.send(notif.serialize())

    async def _receive_loop(self) -> None:
        """后台接收循环，按 id 匹配 Future。"""
        try:
            while self._connected:
                try:
                    data = await self._transport.receive()
                    msg = parse_message(data)

                    if isinstance(msg, JsonRpcResponse):
                        future = self._pending.pop(msg.id, None)
                        if future and not future.done():
                            future.set_result(msg.result)

                    elif isinstance(msg, JsonRpcError):
                        future = self._pending.pop(msg.id, None)
                        if future and not future.done():
                            future.set_exception(
                                RuntimeError(
                                    f"MCP error {msg.error.get('code', -1)}: "
                                    f"{msg.error.get('message', 'unknown')}"
                                )
                            )

                except asyncio.CancelledError:
                    break
                except ConnectionError:
                    logger.debug("MCP transport closed for %s", self.server_name)
                    break
                except Exception:
                    logger.debug("MCP receive error for %s", self.server_name, exc_info=True)
        finally:
            # 连接断开 → 取消所有等待中的请求
            self._connected = False
            for fid, future in list(self._pending.items()):
                if not future.done():
                    future.set_exception(
                        ConnectionError(f"MCP connection closed for {self.server_name}")
                    )
            self._pending.clear()
