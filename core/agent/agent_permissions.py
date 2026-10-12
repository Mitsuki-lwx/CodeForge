"""Agent 的权限判定与 HITL 决策 —— `ask` 级工具调用的最终裁决。

从 `core/agent/agent.py` 抽出的一簇方法：类别归一化、无人值守策略代答、
审批审查（REVIEW 档）、HITL 事件与描述文本。

⚠️ 这里**只做判定，不执行工具**。判定的调用方是 `ToolExecutionMixin`
（`agent_tools.py`），两者刻意分开：判定是同步纯逻辑，执行是异步副作用。
"""

from __future__ import annotations

from typing import Any

from conversation.manager import ConversationManager
from core.agent.tool_category import normalize_tool_category
from core.permissions.checker import Decision, PermissionChecker
from core.permissions.modes import (
    INTERACTIVE_TOOLS,
    ToolCategory,
    UnattendedPolicy,
    unattended_decide,
)
from core.permissions.rules import extract_content
from core.tool.registry import ToolRegistry
from llm.stream_events import ToolUse


class PermissionMixin:
    """`Agent` 的权限判定与 HITL 代答。

    ── 对宿主的依赖契约 ──────────────────────────────────────────────
    属性由 `Agent.__init__` / 各 setter 赋值。纯注解对运行时零影响。
    """

    _registry: ToolRegistry
    _conversation: ConversationManager
    _permission_checker: PermissionChecker
    _unattended_policy: UnattendedPolicy | None
    _dont_ask: bool
    _approval_reviewer: Any

    def _tool_category(self, tu: ToolUse) -> tuple[bool, ToolCategory]:
        """取工具的 `(是否只读, 权限类别)`。

        工具查不到时按最保守的一档处理（非只读 + command）：既不放过任何写权限，
        也让 journal 把它当成有副作用记下来。
        """
        try:
            tool = self._registry.get(tu.name)
            is_read = tool.is_read_only()
            return is_read, normalize_tool_category(tool.category(), is_read)
        except Exception:
            return False, "command"

    def _should_review_approval(self) -> bool:
        """是否把这次 `ask` 交给自动审查者。

        两个条件缺一不可：挂了审查者 **且** 当前是 `REVIEW` 档。
        其余档位（含有人值守的 `None`）一律不走这条路 → 零回归。
        """
        return (
            self._approval_reviewer is not None
            and self._unattended_policy is UnattendedPolicy.REVIEW
        )

    def _recent_user_messages(self, limit: int = 3) -> list[str]:
        """取会话里最近的用户消息 —— 审查者判断"授权范围"的依据。

        只取 user 角色：工具结果、assistant 自述都**不能**当作授权来源
        （否则 agent 可以自己给自己写授权）。
        """
        msgs = getattr(self._conversation, "messages", None) or []
        out: list[str] = []
        for m in reversed(msgs):
            role = getattr(m.role, "value", getattr(m, "role", ""))
            if role != "user":
                continue
            content = getattr(m, "content", "")
            if isinstance(content, list):
                # 内部消息的 content 可能是 Anthropic 块数组（见 llm/adapters）
                content = " ".join(
                    str(b.get("text", ""))
                    for b in content
                    if isinstance(b, dict) and b.get("text")
                )
            if isinstance(content, str) and content.strip():
                out.append(content)
                if len(out) >= limit:
                    break
        return list(reversed(out))

    def _check_tool_permission(self, tu: ToolUse) -> Decision:
        """检查单个工具调用的权限。"""
        # 强制拒绝集：命中即 deny（试验/评测注入）
        if tu.name in getattr(self, "_deny_tools", set()):
            return Decision(effect="deny", reason="tool denied by forced deny_tools")
        is_read, tc = self._tool_category(tu)
        decision = self._permission_checker.check(tu.name, is_read, tc, tu.input)
        if decision.effect == "ask":
            # ── 无人值守策略：代答 ask（在最早点决定，不产生 HITL 事件）──
            policy = self._unattended_policy
            if policy is not None:
                if tu.name in INTERACTIVE_TOOLS:
                    return Decision(
                        effect="deny",
                        reason=f"unattended:{policy.value} 拒绝交互式工具 {tu.name}",
                    )
                if policy is UnattendedPolicy.REVIEW:
                    # `REVIEW` 档**刻意不在这里代答**：审查要调模型（异步）且需要
                    # 完整上下文，而本方法是同步的。保持 `ask` 原样返回，交给
                    # `_execute_tools` 的 ask 分支处理（见 spec_approval_review）。
                    return decision
                effect = unattended_decide(policy, tc)
                return Decision(
                    effect=effect,
                    reason=f"unattended:{policy.value} category={tc}",
                )
            # ── 子 Agent dontAsk 模式：Ask 自动转 Allow ──
            if self._dont_ask:
                return Decision(effect="allow", reason="dontAsk: auto-approved")
        return decision

    def _describe_tool_action(self, tu: ToolUse) -> str:
        """为 HITL 生成人类可读的操作描述。"""
        content = extract_content(tu.name, tu.input)
        if content:
            return f"[{tu.name}] {content}"
        parts = [f"{k}={str(v)[:80]}" for k, v in tu.input.items()]
        return f"[{tu.name}] {', '.join(parts)}" if parts else tu.name
