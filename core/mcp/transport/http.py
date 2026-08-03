"""HTTP 传输：通过 HTTP POST 发送 JSON-RPC。"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.mcp.transport.base import Transport

logger = logging.getLogger(__name__)


class StreamableHTTPTransport(Transport):
    """通过 HTTP POST 发送 JSON-RPC 请求，返回响应。"""

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._url = url
        self._headers = headers or {}
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout,
        )
        self._connected = True
        logger.info("HTTP transport connected to %s", self._url)

    async def send(self, message: bytes) -> None:
        """HTTP transport: send 和 receive 合并为 call。

        send 方法暂存消息，receive 时实际发送。"""
        self._pending = message

    async def receive(self) -> bytes:
        """发送 HTTP POST 并返回响应。"""
        if not self._client:
            raise RuntimeError("Transport not connected")
        if not hasattr(self, "_pending"):
            raise RuntimeError("No pending message to send")

        message = self._pending
        del self._pending

        response = await self._client.post(
            self._url,
            content=message,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.content

    async def close(self) -> None:
        self._connected = False
        if self._client:
            await self._client.aclose()
            self._client = None
