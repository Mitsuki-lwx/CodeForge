"""PermissionChecker —— 纵深防御分层检查管线。

5 层递进检查：
  Layer 0:  Plan mode 白名单工具放行
  Layer 1:  安全只读命令 → allow
  Layer 1b: 危险命令黑名单 → deny
  Layer 2:  路径沙箱检查
  Layer 3:  规则引擎匹配 → allow|deny|ask
  Layer 4:  权限模式兜底
  Layer 5:  返回 ask → 触发 HITL
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from core.permissions.dangerous import DangerousCommandDetector
from core.permissions.modes import DecisionEffect, PermissionMode, ToolCategory, is_plan_mode_allowed, mode_decide
from core.permissions.rules import RuleEngine, extract_content, is_safe_command
from core.permissions.sandbox import PathSandbox


@dataclass
class Decision:
    """权限检查结果。"""
    effect: DecisionEffect
    reason: str


_PLAN_MODE_ALLOWED_TOOLS = frozenset({"ExitPlanMode"})

# Layer 4b: 会话级放行缓存（格式 "ToolName:pattern"）
_SESSION_ALLOW_PREFIX = "session_allow:"


class PermissionChecker:
    """纵深防御检查管线。"""

    def __init__(
        self,
        mode: PermissionMode = PermissionMode.DEFAULT,
        sandbox: PathSandbox | None = None,
        detector: DangerousCommandDetector | None = None,
        rule_engine: RuleEngine | None = None,
    ) -> None:
        self.mode = mode
        self._sandbox = sandbox or PathSandbox()
        self._detector = detector or DangerousCommandDetector()
        self._rule_engine = rule_engine or RuleEngine()
        self.plan_file_path: str = ""
        self._session_allowed: set[str] = set()

    # ── Public API ──────────────────────────────────────────────────

    def check(
        self,
        tool_name: str,
        tool_is_read_only: bool,
        tool_category: ToolCategory = "command",
        arguments: dict | None = None,
    ) -> Decision:
        """分层检查工具调用权限。

        Args:
            tool_name: 工具名
            tool_is_read_only: 工具是否只读
            tool_category: 工具类别 ("read" | "write" | "command")
            arguments: 工具参数

        Returns:
            Decision(effect="allow"|"deny"|"ask", reason)
        """
        args = arguments or {}
        content = extract_content(tool_name, args)
        is_command = tool_category == "command"

        # Layer 0: Plan mode 白名单
        if self.mode == PermissionMode.PLAN:
            if tool_name in _PLAN_MODE_ALLOWED_TOOLS:
                return Decision(effect="allow", reason="Plan mode: allowed tool")
            if tool_name in ("WriteFile", "EditFile") and content:
                if self._is_plan_file(content):
                    return Decision(effect="allow", reason="Plan mode: plan file write")

        # Layer 1: 安全只读命令自动放行
        if tool_category == "read" and tool_is_read_only:
            return Decision(effect="allow", reason="Read-only tool")

        # Layer 1: 命令类工具 → 安全命令检查
        if is_command and content:
            if is_safe_command(content):
                return Decision(effect="allow", reason="Safe read-only command")

        # Layer 1b: 危险命令黑名单
        if is_command and content:
            hit = self._detector.detect(content)
            if hit.is_dangerous:
                return Decision(effect="deny", reason=f"Dangerous command: {hit.reason}")

        # Layer 2: 路径沙箱（文件读写类工具）
        if tool_category in ("read", "write") and content:
            sandbox_result = self._sandbox.check(content)
            if not sandbox_result.ok:
                if self.mode == PermissionMode.BYPASS:
                    pass  # bypass 绕过沙箱
                else:
                    return Decision(effect="deny", reason=f"Path sandbox: {sandbox_result.reason}")

        # Layer 3: 规则引擎匹配
        rule_result = self._rule_engine.evaluate(tool_name, content)
        if rule_result == "allow":
            return Decision(effect="allow", reason="Rule: allow")
        if rule_result == "deny":
            return Decision(effect="deny", reason="Rule: deny")
        if rule_result == "ask":
            return Decision(effect="ask", reason="Rule: ask")

        # Layer 4b: 会话级放行缓存（HITL "本会话允许"）
        if self._check_session_allowed(tool_name, content):
            return Decision(effect="allow", reason="Session allow cache")

        # Layer 4: 模式兜底
        effect = mode_decide(self.mode, tool_category)
        if effect == "allow":
            return Decision(effect="allow", reason=f"Mode {self.mode.value}: allow")
        if effect == "deny":
            return Decision(effect="deny", reason=f"Mode {self.mode.value}: deny")

        # Layer 5: HITL
        return Decision(effect="ask", reason="Need user confirmation")

    def add_session_allow(self, tool_name: str, content: str) -> None:
        """HITL 选择「本会话允许」后记录。"""
        key = f"{tool_name}:{content}"
        self._session_allowed.add(key)

    # ── Helpers ──────────────────────────────────────────────────────

    def _check_session_allowed(self, tool_name: str, content: str) -> bool:
        """检查是否命中会话级放行缓存。"""
        if not self._session_allowed:
            return False
        key = f"{tool_name}:{content}"
        if key in self._session_allowed:
            return True
        # 前缀匹配
        for allowed in self._session_allowed:
            if allowed.endswith("*") and key.startswith(allowed[:-1]):
                return True
        return False

    def _is_plan_file(self, target_path: str) -> bool:
        """检测目标路径是否是 plan 文件。"""
        if not self.plan_file_path:
            return ".codeforge/plans/" in target_path or "docs/plans/" in target_path
        try:
            if os.path.abspath(target_path) == os.path.abspath(self.plan_file_path):
                return True
        except Exception:
            pass
        return os.path.basename(target_path) == os.path.basename(self.plan_file_path)


# Re-export for convenience
__all__ = ["PermissionChecker", "Decision", "DecisionEffect"]
