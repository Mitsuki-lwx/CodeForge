"""HITL (Human-In-The-Loop) 数据模型。

定义交互请求与响应类型，供 PermissionChecker 与 TUI 之间传递。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class HITLChoice(str, Enum):
    ALLOW_ONCE = "allow_once"       # 本次允许
    ALLOW_SESSION = "allow_session" # 本会话允许（写入内存缓存）
    ALLOW_SAVE = "allow_save"       # 允许并保存到项目规则
    DENY = "deny"                   # 拒绝


@dataclass
class HITLRequest:
    """HITL 确认请求——从 PermissionChecker 发给 TUI。"""

    tool_name: str
    description: str       # 人类可读的操作描述
    arguments: dict = field(default_factory=dict)
    risk_hint: str = ""    # 风险提示（如有）


@dataclass
class HITLResponse:
    """HITL 确认响应——从 TUI 返回给 Agent。"""

    choice: HITLChoice
    tool_name: str
    content: str = ""      # 用于规则匹配的内容文本
    feedback: str = ""     # 用户附带的反馈文本（如有）
