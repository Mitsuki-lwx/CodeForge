"""JSON-RPC 2.0 消息类型定义与编解码。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class JsonRpcRequest:
    """JSON-RPC 2.0 请求。"""
    method: str
    params: dict[str, Any] | None = None
    id: int = 0
    jsonrpc: str = "2.0"

    def serialize(self) -> bytes:
        obj: dict[str, Any] = {
            "jsonrpc": self.jsonrpc,
            "method": self.method,
            "id": self.id,
        }
        if self.params is not None:
            obj["params"] = self.params
        return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


@dataclass
class JsonRpcNotification:
    """JSON-RPC 2.0 通知（无 id，不需要响应）。"""
    method: str
    params: dict[str, Any] | None = None
    jsonrpc: str = "2.0"

    def serialize(self) -> bytes:
        obj: dict[str, Any] = {
            "jsonrpc": self.jsonrpc,
            "method": self.method,
        }
        if self.params is not None:
            obj["params"] = self.params
        return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


@dataclass
class JsonRpcResponse:
    """JSON-RPC 2.0 成功响应。"""
    id: int
    result: Any = None
    jsonrpc: str = "2.0"


@dataclass
class JsonRpcError:
    """JSON-RPC 2.0 错误响应。"""
    id: int
    error: dict[str, Any] = field(default_factory=dict)
    jsonrpc: str = "2.0"


def parse_message(data: bytes) -> JsonRpcResponse | JsonRpcError | None:
    """解析 JSON-RPC 响应或错误。

    Returns:
        JsonRpcResponse, JsonRpcError, or None (for notifications/unknown)
    """
    text = data.decode("utf-8").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not isinstance(obj, dict):
        return None
    if obj.get("jsonrpc") != "2.0":
        return None

    msg_id = obj.get("id", 0)
    if "result" in obj:
        return JsonRpcResponse(id=msg_id, result=obj["result"])
    if "error" in obj:
        return JsonRpcError(id=msg_id, error=obj["error"])
    if "method" in obj and "id" not in obj:
        # Notification — no response needed
        return None

    return None
