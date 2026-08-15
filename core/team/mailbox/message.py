"""邮箱消息 —— Message / MessageType。

`MessageType` 分流纯文本与三种结构化协议消息。`from_` 字段对应 config/JSON 里的 `"from"`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class MessageType(str, Enum):
    TEXT = "text"
    SHUTDOWN_REQUEST = "shutdown_request"
    SHUTDOWN_RESPONSE = "shutdown_response"
    PLAN_APPROVAL_RESPONSE = "plan_approval_response"


@dataclass
class Message:
    """一条邮箱消息。"""

    from_: str
    to: str
    type: MessageType = MessageType.TEXT
    summary: str = ""
    content: str = ""
    payload: dict[str, Any] | None = None
    timestamp: int = 0
    read: bool = False

    def to_dict(self) -> dict:
        return {
            "from": self.from_,
            "to": self.to,
            "type": self.type.value,
            "summary": self.summary,
            "content": self.content,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "read": self.read,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Message:
        return cls(
            from_=d.get("from", ""),
            to=d.get("to", ""),
            type=MessageType(d.get("type", "text")),
            summary=d.get("summary", ""),
            content=d.get("content", ""),
            payload=d.get("payload"),
            timestamp=d.get("timestamp", 0),
            read=bool(d.get("read", False)),
        )


def message_to_json(msg: Message) -> str:
    return json.dumps(msg.to_dict(), ensure_ascii=False)


def message_from_json(raw: str) -> Message:
    return Message.from_dict(json.loads(raw))


__all__ = ["Message", "MessageType", "message_from_json", "message_to_json"]
