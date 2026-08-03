"""Transport 抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Transport(ABC):
    """MCP 传输层抽象。"""

    @abstractmethod
    async def connect(self) -> None:
        """建立连接（stdio: 启动子进程，http: 验证端点可达）。"""
        ...

    @abstractmethod
    async def send(self, message: bytes) -> None:
        """发送 JSON-RPC 消息。"""
        ...

    @abstractmethod
    async def receive(self) -> bytes:
        """接收一条 JSON-RPC 消息（阻塞直到有消息）。"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭连接并清理资源。"""
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """连接是否活跃。"""
        ...
