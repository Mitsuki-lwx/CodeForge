"""权限模式枚举与决策矩阵。

4 档模式：DEFAULT / ACCEPT_EDITS / PLAN / BYPASS，
按工具类别（read / write / command）兜底决策。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

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


# ── 子 Agent 的策略继承（修 D4）────────────────────────────────────────
#
# 子 Agent 没有 TUI 接 HITL，必须有个策略替它代答 ask，否则会挂死。
# 但它**不该因此获得比父更大的授权** —— 早先的做法是直接 `dont_ask=True`
# （子 Agent 全放行）或 `permission_mode=BYPASS`，等于父还被问着、子已经
# 把写权限拿走了；而且 BYPASS 会让决策直接落到 allow、根本进不到 ask，
# 使无人值守策略形同虚设。改为：**继承父的授权范围**。


def policy_from_permission_mode(mode: PermissionMode) -> UnattendedPolicy:
    """把「父的工作模式」映射成子 Agent 可用的无人值守策略。

    - `BYPASS`        父已全放行 → 子 `allow_all`（授权对齐）
    - `DEFAULT`       父自己写文件要**经人批准** —— 这说明"写"本身是用户认可的
      意图，只是需要确认。子 Agent 无人可问，取 `allow_write` 最贴近该意图
      （**命令执行仍拒**）。这比早先的无条件 `BYPASS` 收敛，又不至于让 fork
      这类"派个分身去干活"的功能废掉。
    - `ACCEPT_EDITS`  父本来就放行写编辑 → 子 `allow_write`
    - `PLAN` 及其余  父处在只读规划阶段 → 子保守 `deny_all`
    """
    if mode is PermissionMode.BYPASS:
        return UnattendedPolicy.ALLOW_ALL
    if mode in (PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS):
        return UnattendedPolicy.ALLOW_WRITE
    return UnattendedPolicy.DENY_ALL


def resolve_child_policy(parent: Any) -> UnattendedPolicy:
    """算出子 Agent 该用的无人值守策略。

    父**显式**设过 `unattended_policy`（host 场景）就原样继承；没设（TUI 场景）
    则按父当前的权限模式推导。取不到父的模式时退回最保守档。
    """
    explicit = getattr(parent, "unattended_policy", None)
    if isinstance(explicit, UnattendedPolicy):
        return explicit
    mode = getattr(parent, "permission_mode", None)
    if not isinstance(mode, PermissionMode):
        return UnattendedPolicy.DENY_ALL
    return policy_from_permission_mode(mode)
