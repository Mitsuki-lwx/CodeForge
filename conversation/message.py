"""消息模型。

内部层（Message）：包含 ID、时间戳、状态、token 用量等元数据。
API 层（APIMessage）：仅 role + content，用于发送给 LLM。

支持工具调用：content 可以是纯文本 (str) 或内容块列表 (list[dict])。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(str, Enum):
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class Message:
    """内部消息模型，含完整元数据。"""
    role: MessageRole
    content: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: MessageStatus = MessageStatus.PENDING
    timestamp: float = field(default_factory=time.time)
    usage: Optional[dict[str, Any]] = None
    # 工具调用相关
    tool_use_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_input: Optional[dict[str, Any]] = None

    def to_api(self) -> APIMessage:
        """转为 API 层消息（仅 role + content）。"""
        return APIMessage(role=self.role.value, content=self.content)


@dataclass
class APIMessage:
    """API 层消息，仅 role + content。

    content 可以是纯文本字符串，也可以是内容块列表（用于工具调用）。
    """
    role: str
    content: str | list[dict[str, Any]]


def make_text_block(text: str) -> dict[str, Any]:
    """创建文本内容块。"""
    return {"type": "text", "text": text}


def make_tool_use_block(id: str, name: str, input: dict[str, Any]) -> dict[str, Any]:
    """创建工具调用内容块。"""
    return {"type": "tool_use", "id": id, "name": name, "input": input}


def make_tool_result_block(tool_use_id: str, content: str) -> dict[str, Any]:
    """创建工具结果内容块。"""
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
