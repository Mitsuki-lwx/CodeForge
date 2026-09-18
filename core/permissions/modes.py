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


# ── 无人值守策略 ────────────────────────────────────────────────────────
#
# host / 无头运行没有人工确认通道，`ask` 级决策必须由策略代答，不能挂在
# 等人按键上。取值表达「放行到什么程度」，默认取最保守的可用档。


class UnattendedPolicy(str, Enum):
    """无人值守下对 `ask` 级工具决策的处置策略。

    刻意**不提供** `allow_readonly` 档：只读工具在权限矩阵里本来就是 `allow`
    （从不进入 `ask`），于是「只放只读」与 `deny_all` 的行为完全一致——是个
    名不副实的冗余档，写出来只会让人误以为多了一层保护。
    """

    ALLOW_ALL = "allow_all"  # 全部放行（等价旧的 dontAsk 语义）
    ALLOW_WRITE = "allow_write"  # 放行读与写，命令执行仍拒
    DENY_ALL = "deny_all"  # ask 一律拒绝（默认，与无策略时的保守行为一致）


# 任何无人值守策略下都必须拒绝的交互式工具：它们存在的意义就是向人索取决策，
# 无人环境里放行等于替人签字，且放行后也拿不到答案。
INTERACTIVE_TOOLS: frozenset[str] = frozenset(
    {"ExitPlanMode", "AskUserQuestion", "Input"}
)


def unattended_decide(
    policy: UnattendedPolicy, category: ToolCategory
) -> DecisionEffect:
    """按无人值守策略把 `ask` 级决策代答为 allow / deny（绝不返回 ask）。"""
    if policy is UnattendedPolicy.ALLOW_ALL:
        return "allow"
    if policy is UnattendedPolicy.ALLOW_WRITE:
        return "allow" if category in ("read", "write") else "deny"
    return "deny"
