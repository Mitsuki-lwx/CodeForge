"""权限模式枚举与决策矩阵。

4 档模式：DEFAULT / ACCEPT_EDITS / PLAN / BYPASS，
按工具类别（read / write / command）兜底决策。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

DecisionEffect = Literal["allow", "deny", "ask"]
ToolCategory = Literal["read", "write", "command"]


class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    BYPASS = "bypassPermissions"


# 决策矩阵：mode → category → effect
_MODE_MATRIX: dict[PermissionMode, dict[ToolCategory, DecisionEffect]] = {
    PermissionMode.DEFAULT: {"read": "allow", "write": "ask", "command": "ask"},
    PermissionMode.ACCEPT_EDITS: {"read": "allow", "write": "allow", "command": "ask"},
    PermissionMode.PLAN: {"read": "allow", "write": "ask", "command": "ask"},
    PermissionMode.BYPASS: {"read": "allow", "write": "allow", "command": "allow"},
}

# Plan mode 下始终放行的工具白名单
_PLAN_MODE_ALLOWED_TOOLS: frozenset[str] = frozenset({"ExitPlanMode"})


def mode_decide(mode: PermissionMode, category: ToolCategory) -> DecisionEffect:
    """返回当前模式对给定工具类别的兜底决策。"""
    return _MODE_MATRIX[mode][category]


def is_plan_mode_allowed(tool_name: str) -> bool:
    """Plan Mode 下始终放行的工具白名单。"""
    return tool_name in _PLAN_MODE_ALLOWED_TOOLS
